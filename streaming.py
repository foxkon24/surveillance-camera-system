"""
ストリーミング管理モジュール
HLSストリーミングプロセスの管理機能を提供します
"""
import os
import logging
import threading
import time
import subprocess
import psutil

import config
import ffmpeg_utils
import fs_utils
import camera_utils

# グローバル変数としてストリーミングプロセスを管理
streaming_processes = {}
# HLSファイルの最終更新時間を追跡
hls_last_update = {}
# m3u8ファイルの前回のサイズを追跡
m3u8_last_size = {}
# 健全性チェックの間隔（秒）
HEALTH_CHECK_INTERVAL = 15
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 30

# カメラごとのロックを管理する辞書
camera_locks = {}
# カメラの状態を管理する辞書（0=未接続, 1=接続中, 2=一時停止, 3=エラー）
camera_status = {}
# 各カメラの最終接続試行時間
last_connection_attempt = {}
# 接続の再試行待機時間（秒）
CONNECTION_RETRY_DELAY = 30

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    camera_id = camera['id']
    
    # カメラのロックがまだ存在しない場合は作成
    if camera_id not in camera_locks:
        camera_locks[camera_id] = threading.Lock()
    
    # 最後の接続試行から十分な時間が経過していないなら、すぐに再接続しない
    current_time = time.time()
    if camera_id in last_connection_attempt:
        time_since_last_attempt = current_time - last_connection_attempt[camera_id]
        if time_since_last_attempt < CONNECTION_RETRY_DELAY:
            # エラー状態になっているカメラは接続しない
            if camera_id in camera_status and camera_status[camera_id] == 3:
                logging.warning(f"Camera {camera_id} is in error state. Waiting for retry delay to expire.")
                return False
    
    # ロックを取得してプロセス操作
    with camera_locks[camera_id]:
        # すでにストリーミング中で、プロセスが実行中ならそのまま返す
        if camera_id in streaming_processes and streaming_processes[camera_id] and _is_process_running(streaming_processes[camera_id]):
            return True
        
        # ここから新しいストリーミングを開始
        try:
            # 最終接続試行時間を記録
            last_connection_attempt[camera_id] = current_time
            
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            fs_utils.ensure_directory_exists(camera_tmp_dir)

            hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8").replace('/', '\\')
            log_path = os.path.join(camera_tmp_dir, f"{camera_id}.log").replace('/', '\\')

            # 一旦既存のHLSファイルを削除
            if os.path.exists(hls_path):
                try:
                    os.remove(hls_path)
                except Exception as e:
                    logging.warning(f"Failed to remove existing HLS file for camera {camera_id}: {e}")

            # 実行中プロセスの確認と終了処理
            # この関数呼び出し前にロックを取得しているので競合はない
            if camera_id in streaming_processes:
                _stop_streaming_process(camera_id)
            
            # 同じカメラのすべてのffmpegプロセスを強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            time.sleep(1)  # プロセス終了待ち
            
            # RTSPストリームの接続確認
            if not ffmpeg_utils.check_rtsp_connection(camera['rtsp_url']):
                logging.warning(f"Failed to connect to RTSP stream for camera {camera_id}: {camera['rtsp_url']}")
                # カメラ状態をエラーに設定
                camera_status[camera_id] = 3
                # 接続に失敗しても続行する - 後でリトライするため
            else:
                # カメラ状態を接続中に設定
                camera_status[camera_id] = 1

            # Nginx用に最適化されたHLSセグメントパス
            segment_path = os.path.join(camera_tmp_dir, f"{camera_id}_%03d.ts").replace('/', '\\')
            
            # HLSストリーミング用FFmpegコマンド生成
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
                camera['rtsp_url'], 
                hls_path,
                segment_path
            )

            # プロセス起動前の最終チェック - 同じカメラIDのプロセスがないことを確認
            # ロックで保護されているのでこの時点で同時起動はないはず
            existing_ffmpeg = ffmpeg_utils.find_ffmpeg_processes(camera_id)
            if existing_ffmpeg:
                logging.warning(f"Existing ffmpeg processes for camera {camera_id} found before starting. Killing them.")
                for pid in existing_ffmpeg:
                    try:
                        process = psutil.Process(pid)
                        ffmpeg_utils.terminate_process(process)
                    except Exception as e:
                        logging.error(f"Error terminating existing process {pid}: {e}")
                time.sleep(1)  # 完全に終了するのを待つ
            
            # プロセス起動
            process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command, log_path=log_path)
            
            # 起動確認
            if process is None or process.poll() is not None:
                logging.error(f"Failed to start ffmpeg process for camera {camera_id}")
                camera_status[camera_id] = 3  # エラー状態に設定
                return False
                
            # プロセス情報を保存
            streaming_processes[camera_id] = process
            
            # 初期化時点で更新情報を記録
            hls_last_update[camera_id] = time.time()
            if os.path.exists(hls_path):
                m3u8_last_size[camera_id] = os.path.getsize(hls_path)
            else:
                m3u8_last_size[camera_id] = 0

            # 監視スレッドを開始
            monitor_thread = threading.Thread(
                target=monitor_streaming_process,
                args=(camera_id, process),
                daemon=True
            )
            monitor_thread.start()

            # ファイル更新監視スレッドを開始
            hls_monitor_thread = threading.Thread(
                target=monitor_hls_updates,
                args=(camera_id,),
                daemon=True
            )
            hls_monitor_thread.start()

            logging.info(f"Started streaming for camera {camera_id}")
            return True

        except Exception as e:
            logging.error(f"Error starting streaming for camera {camera_id}: {e}")
            camera_status[camera_id] = 3  # エラー状態に設定
            return False

def _is_process_running(process):
    """
    プロセスが実行中かどうかを確認する関数

    Args:
        process (subprocess.Popen): 確認するプロセス

    Returns:
        bool: プロセスが実行中かどうか
    """
    if process is None:
        return False
    
    try:
        return process.poll() is None
    except Exception:
        return False

def _stop_streaming_process(camera_id):
    """
    特定カメラのストリーミングプロセスを停止する内部関数
    この関数を呼び出す前にロックを取得していることが前提

    Args:
        camera_id (str): 停止するカメラID
    """
    if camera_id not in streaming_processes:
        return
    
    process = streaming_processes[camera_id]
    if process and _is_process_running(process):
        try:
            ffmpeg_utils.terminate_process(process)
            # waitを追加してプロセスの完全終了を確認
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logging.warning(f"Process for camera {camera_id} did not terminate gracefully. Force killing.")
                process.kill()
                
            logging.info(f"Stopped streaming process for camera {camera_id}")
        except Exception as e:
            logging.error(f"Error stopping streaming process for camera {camera_id}: {e}")
    
    # いずれにしてもプロセス情報をクリア
    streaming_processes[camera_id] = None

def restart_streaming(camera_id):
    """
    特定カメラのストリーミングを再起動する

    Args:
        camera_id (str): 再起動するカメラID
    
    Returns:
        bool: 操作が成功したかどうか
    """
    # カメラのロックがまだ存在しない場合は作成
    if camera_id not in camera_locks:
        camera_locks[camera_id] = threading.Lock()
    
    # ロックを取得してプロセス操作
    with camera_locks[camera_id]:
        try:
            logging.warning(f"Restarting streaming for camera {camera_id}")
            
            # 現在のプロセスを停止
            _stop_streaming_process(camera_id)
            
            # 同じカメラのすべてのffmpegプロセスを強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            time.sleep(1)  # プロセス終了待ち
            
            # ストリーミングプロセスを削除して再起動
            if camera_id in streaming_processes:
                streaming_processes[camera_id] = None
            
            # カメラ設定を読み込んでストリーミングを再開
            camera = camera_utils.get_camera_by_id(camera_id)
            if camera:
                # 最終接続試行時間をリセットして強制的に再接続
                if camera_id in last_connection_attempt:
                    del last_connection_attempt[camera_id]
                
                success = get_or_start_streaming(camera)
                if success:
                    logging.info(f"Successfully restarted streaming for camera {camera_id}")
                    return True
                else:
                    logging.error(f"Failed to restart streaming for camera {camera_id}")
                    return False
            else:
                logging.error(f"Camera config not found for camera {camera_id}")
                return False
        
        except Exception as e:
            logging.error(f"Error restarting streaming for camera {camera_id}: {e}")
            return False

def monitor_hls_updates(camera_id):
    """
    HLSファイルの更新状態を監視する関数

    Args:
        camera_id (str): 監視するカメラID
    """
    camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
    hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8").replace('/', '\\')
    
    failures = 0
    max_failures = 2  # 連続でこの回数分問題が検出されたら再起動
    
    while True:
        try:
            # ロックを取得してチェック
            with camera_locks.get(camera_id, threading.Lock()):
                if camera_id not in streaming_processes or streaming_processes[camera_id] is None:
                    # ストリーミングが停止していたら監視も終了
                    logging.info(f"Streaming process for camera {camera_id} no longer exists. Stopping HLS monitor.")
                    break
                
                current_time = time.time()
                file_updated = False
                
                # m3u8ファイルの存在と更新チェック
                if os.path.exists(hls_path):
                    # ファイルサイズをチェック
                    try:
                        current_size = os.path.getsize(hls_path)
                        last_size = m3u8_last_size.get(camera_id, 0)
                        
                        if current_size != last_size:
                            # ファイルサイズが変わっていれば更新されている
                            m3u8_last_size[camera_id] = current_size
                            hls_last_update[camera_id] = current_time
                            file_updated = True
                            failures = 0  # 正常更新を検出したらカウンタをリセット
                    except Exception as e:
                        logging.error(f"Error checking m3u8 file size for camera {camera_id}: {e}")
                
                # TSファイルの更新も確認
                try:
                    if os.path.exists(camera_tmp_dir):
                        ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                        if ts_files:
                            newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                            newest_ts_path = os.path.join(camera_tmp_dir, newest_ts)
                            ts_mtime = os.path.getmtime(newest_ts_path)
                            
                            if ts_mtime > hls_last_update.get(camera_id, 0):
                                hls_last_update[camera_id] = current_time
                                file_updated = True
                                failures = 0  # 正常更新を検出したらカウンタをリセット
                except Exception as e:
                    logging.error(f"Error checking TS files for camera {camera_id}: {e}")
                
                # ファイル更新が停止しているかチェック
                last_update = hls_last_update.get(camera_id, 0)
                if not file_updated and (current_time - last_update) > HLS_UPDATE_TIMEOUT:
                    logging.warning(f"HLS files for camera {camera_id} have not been updated for {current_time - last_update:.2f} seconds")
                    failures += 1
                    
                    if failures >= max_failures:
                        logging.error(f"HLS update timeout detected for camera {camera_id}. Restarting streaming.")
                        # ロックは既に取得済みなので、ここでは内部関数を直接呼び出さない
                        # restart_streaming関数はロックを取得するので、ここでは一旦ロックを解放
            
            # ロックの外で再起動操作を行う
            if failures >= max_failures:
                restart_streaming(camera_id)
                failures = 0
                
                # 監視を終了（新しいスレッドが開始されるため）
                break
            
        except Exception as e:
            logging.error(f"Error monitoring HLS updates for camera {camera_id}: {e}")
        
        time.sleep(HEALTH_CHECK_INTERVAL)

def monitor_streaming_process(camera_id, process):
    """
    ストリーミングプロセス監視関数

    Args:
        camera_id (str): 監視するカメラID
        process (subprocess.Popen): 監視するプロセス
    """
    consecutive_failures = 0
    max_failures = config.RETRY_ATTEMPTS
    retry_delay = config.RETRY_DELAY
    max_retry_delay = config.MAX_RETRY_DELAY

    while True:
        try:
            # ロックなしでプロセス状態をチェック
            if not _is_process_running(process):
                consecutive_failures += 1
                current_delay = min(retry_delay * consecutive_failures, max_retry_delay)

                logging.warning(f"Streaming process for camera {camera_id} has died. "
                                f"Attempt {consecutive_failures}/{max_failures}. "
                                f"Waiting {current_delay} seconds before retry.")

                # 一旦待機してから再起動
                time.sleep(current_delay)

                if consecutive_failures >= max_failures:
                    logging.error(f"Too many consecutive failures for camera {camera_id}. Performing full restart.")
                    
                    # ロックを取得して操作
                    with camera_locks.get(camera_id, threading.Lock()):
                        cleanup_camera_resources(camera_id)
                        consecutive_failures = 0
                        
                        # ストリーミングプロセスをクリア
                        streaming_processes[camera_id] = None

                # カメラ設定を読み込んでストリーミングを再開
                camera = camera_utils.get_camera_by_id(camera_id)
                if camera:
                    success = get_or_start_streaming(camera)
                    if success:
                        consecutive_failures = 0
                        logging.info(f"Successfully restarted streaming for camera {camera_id}")
                    else:
                        logging.error(f"Failed to restart streaming for camera {camera_id}")
                break
            else:
                # プロセスが正常な場合、失敗カウントをリセット
                consecutive_failures = 0

        except Exception as e:
            logging.error(f"Error monitoring streaming process for camera {camera_id}: {e}")
            time.sleep(5)
            continue

        time.sleep(5)  # 5秒ごとにチェック

def cleanup_camera_resources(camera_id):
    """
    カメラリソースのクリーンアップ処理

    Args:
        camera_id (str): クリーンアップするカメラID
    """
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        if os.path.exists(camera_tmp_dir):
            for file in os.listdir(camera_tmp_dir):
                try:
                    file_path = os.path.join(camera_tmp_dir, file)
                    if os.path.isfile(file_path):
                        try:
                            os.remove(file_path)
                        except PermissionError:
                            logging.warning(f"Permission denied when removing file {file_path}. Might be in use.")
                        except Exception as e:
                            logging.error(f"Error removing file {file}: {e}")

                except Exception as e:
                    logging.error(f"Error processing file {file}: {e}")

    except Exception as e:
        logging.error(f"Error in cleanup_camera_resources for camera {camera_id}: {e}")

def cleanup_old_segments(camera_id):
    """
    HLSセグメントファイルをクリーンアップする

    Args:
        camera_id (str): クリーンアップするカメラID
    """
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        if not os.path.exists(camera_tmp_dir):
            return

        current_time = time.time()
        m3u8_file = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
        active_segments = set()

        # .m3u8ファイルから現在使用中のセグメントを取得
        if os.path.exists(m3u8_file):
            try:
                with open(m3u8_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.endswith('.ts'):
                            active_segments.add(os.path.basename(line))

            except Exception as e:
                logging.error(f"Error reading m3u8 file for camera {camera_id}: {e}")
                return

        # ディレクトリ内のtsファイルをチェック
        if os.path.exists(camera_tmp_dir):
            for file in os.listdir(camera_tmp_dir):
                if file.endswith('.ts'):
                    file_path = os.path.join(camera_tmp_dir, file)

                    try:
                        # ファイルが以下の条件を満たす場合に削除:
                        # 1. プレイリストに含まれていない
                        # 2. 作成から60秒以上経過している
                        if (file not in active_segments and current_time - os.path.getctime(file_path) > 60):
                            try:
                                os.remove(file_path)
                                logging.info(f"Removed old segment file: {file}")
                            except PermissionError:
                                logging.warning(f"Permission denied when removing segment {file_path}. Might be in use.")
                            except Exception as e:
                                logging.error(f"Error removing file {file}: {e}")

                    except Exception as e:
                        logging.error(f"Error checking segment file {file}: {e}")

    except Exception as e:
        logging.error(f"Error in cleanup_old_segments for camera {camera_id}: {e}")

def cleanup_scheduler():
    """
    すべてのカメラに対して定期的にクリーンアップを実行するスケジューラー
    """
    while True:
        try:
            cameras = camera_utils.read_config()
            for camera in cameras:
                cleanup_old_segments(camera['id'])

        except Exception as e:
            logging.error(f"Error in cleanup_scheduler: {e}")

        time.sleep(15)  # 15秒ごとに実行

def stop_all_streaming():
    """
    すべてのストリーミングプロセスを停止

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        camera_ids = list(streaming_processes.keys())
        
        for camera_id in camera_ids:
            # カメラごとにロックを取得して操作
            with camera_locks.get(camera_id, threading.Lock()):
                try:
                    _stop_streaming_process(camera_id)
                    logging.info(f"Stopped streaming for camera {camera_id}")
                except Exception as e:
                    logging.error(f"Error stopping streaming for camera {camera_id}: {e}")

        # 残っているffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes()
        return True

    except Exception as e:
        logging.error(f"Error stopping all streaming processes: {e}")
        return False

def initialize_streaming():
    """
    ストリーミングシステムの初期化
    """
    # グローバル変数の初期化
    global streaming_processes, hls_last_update, m3u8_last_size, camera_locks, camera_status, last_connection_attempt
    
    streaming_processes = {}
    hls_last_update = {}
    m3u8_last_size = {}
    camera_locks = {}
    camera_status = {}
    last_connection_attempt = {}
    
    # 起動時に残っているffmpegプロセスをクリーンアップ
    ffmpeg_utils.kill_ffmpeg_processes()
    
    # クリーンアップスレッドの起動
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    logging.info("Started segment cleanup scheduler thread")
