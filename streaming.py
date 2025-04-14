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
import shutil

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
# ストリーミング状態を記録
camera_stream_status = {}
# 健全性チェックの間隔（秒）
HEALTH_CHECK_INTERVAL = 10
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 20
# 再試行カウンターを追跡するディクショナリ
retry_counters = {}

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    if camera['id'] not in streaming_processes:
        try:
            camera_id = camera['id']
            rtsp_url = camera['rtsp_url']
            
            # カメラの状態を「初期化中」に設定
            camera_stream_status[camera_id] = "initializing"
            
            # 一時ディレクトリをクリーンアップ
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            if os.path.exists(camera_tmp_dir):
                try:
                    # ディレクトリ内のファイルを削除
                    for file in os.listdir(camera_tmp_dir):
                        file_path = os.path.join(camera_tmp_dir, file)
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                except Exception as e:
                    logging.warning(f"Error cleaning temporary directory for camera {camera_id}: {e}")
            
            # ディレクトリが存在しない場合は作成
            fs_utils.ensure_directory_exists(camera_tmp_dir)

            # 前回の残骸を削除
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            time.sleep(1)  # プロセス終了待ち
            
            # RTSPストリームに接続できるか確認
            logging.info(f"Testing RTSP connection for camera {camera_id}: {rtsp_url}")
            connection_successful, error_message = ffmpeg_utils.check_rtsp_connection(rtsp_url)
            
            if not connection_successful:
                logging.warning(f"RTSP connection failed for camera {camera_id}: {error_message}")
                # 接続に失敗しても、プロセスは一応起動させる（後で再試行するため）
                camera_stream_status[camera_id] = "connection_failed"
            else:
                camera_stream_status[camera_id] = "connected"
                logging.info(f"RTSP connection successful for camera {camera_id}")

            # 出力ファイルパス
            hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8").replace('/', '\\')
            log_path = os.path.join(camera_tmp_dir, f"{camera_id}.log").replace('/', '\\')

            # Nginx用に最適化されたHLSセグメントパス
            segment_path = os.path.join(camera_tmp_dir, f"{camera_id}_%03d.ts").replace('/', '\\')
            
            # HLSストリーミング用FFmpegコマンド生成
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
                rtsp_url, 
                hls_path,
                segment_path
            )

            # プロセス起動
            process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command, log_path=log_path)
            streaming_processes[camera_id] = process
            
            # 初期化時点で更新情報を記録
            hls_last_update[camera_id] = time.time()
            m3u8_last_size[camera_id] = 0
            retry_counters[camera_id] = 0

            # HLSファイルが作成されるのを待つ
            if not ffmpeg_utils.wait_for_hls_file(hls_path, timeout=5):
                logging.warning(f"HLS file not created for camera {camera_id} after 5 seconds")
                # ファイルが作成されなくても処理は続行

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
            logging.error(f"Error starting streaming for camera {camera['id']}: {e}")
            # エラーが発生した場合は状態を「エラー」に設定
            camera_stream_status[camera['id']] = "error"
            return False

    # 既存のプロセスがある場合、状態を確認
    elif camera['id'] in streaming_processes:
        process = streaming_processes[camera['id']]
        # プロセスが実行中かチェック
        if process and process.poll() is None:
            return True
        else:
            # プロセスが終了している場合は再起動
            restart_streaming(camera['id'])
            return True

    return True

def restart_streaming(camera_id):
    """
    特定カメラのストリーミングを再起動する

    Args:
        camera_id (str): 再起動するカメラID
    
    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        logging.warning(f"Restarting streaming for camera {camera_id}")
        
        # カメラの状態を「再起動中」に設定
        camera_stream_status[camera_id] = "restarting"
        
        # 既存のffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        
        # ストリーミングプロセスを削除
        if camera_id in streaming_processes:
            del streaming_processes[camera_id]
        
        # 再試行カウンタの更新
        retry_counters[camera_id] = retry_counters.get(camera_id, 0) + 1
        
        # カメラ設定を読み込んでストリーミングを再開
        camera = camera_utils.get_camera_by_id(camera_id)
        if camera:
            # ディレクトリをクリーンアップ
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            if os.path.exists(camera_tmp_dir):
                try:
                    # ディレクトリ内のファイルを削除
                    for file in os.listdir(camera_tmp_dir):
                        file_path = os.path.join(camera_tmp_dir, file)
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                except Exception as e:
                    logging.warning(f"Error cleaning temporary directory: {e}")
            
            # 一定の遅延を入れる（再試行回数に応じて増加）
            delay = min(1 + (retry_counters[camera_id] - 1) * 2, 10)  # 最大10秒
            logging.info(f"Waiting {delay} seconds before restarting camera {camera_id}")
            time.sleep(delay)
            
            success = get_or_start_streaming(camera)
            if success:
                logging.info(f"Successfully restarted streaming for camera {camera_id}")
                return True
            else:
                logging.error(f"Failed to restart streaming for camera {camera_id}")
                return False
        else:
            logging.error(f"Camera config not found for camera {camera_id}")
            camera_stream_status[camera_id] = "config_not_found"
            return False
    
    except Exception as e:
        logging.error(f"Error restarting streaming for camera {camera_id}: {e}")
        camera_stream_status[camera_id] = "error"
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
            if camera_id not in streaming_processes:
                # ストリーミングが停止していたら監視も終了
                logging.info(f"Streaming process for camera {camera_id} no longer exists. Stopping HLS monitor.")
                break
            
            # プロセスが終了していないか確認
            process = streaming_processes.get(camera_id)
            if process and process.poll() is not None:
                logging.warning(f"Streaming process for camera {camera_id} has died with code {process.poll()}")
                restart_streaming(camera_id)
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
                        # 更新を検出したら成功状態に設定
                        camera_stream_status[camera_id] = "streaming"
                except Exception as e:
                    logging.warning(f"Error checking m3u8 file size for camera {camera_id}: {e}")
            
            # TSファイルの更新も確認
            try:
                ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                if ts_files:
                    newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                    newest_ts_path = os.path.join(camera_tmp_dir, newest_ts)
                    ts_mtime = os.path.getmtime(newest_ts_path)
                    
                    if ts_mtime > hls_last_update.get(camera_id, 0):
                        hls_last_update[camera_id] = current_time
                        file_updated = True
                        failures = 0  # 正常更新を検出したらカウンタをリセット
                        # 更新を検出したら成功状態に設定
                        camera_stream_status[camera_id] = "streaming"
            except Exception as e:
                logging.warning(f"Error checking TS files for camera {camera_id}: {e}")
            
            # ファイル更新が停止しているかチェック
            last_update = hls_last_update.get(camera_id, 0)
            if not file_updated and (current_time - last_update) > HLS_UPDATE_TIMEOUT:
                logging.warning(f"HLS files for camera {camera_id} have not been updated for {current_time - last_update:.2f} seconds")
                failures += 1
                camera_stream_status[camera_id] = "stalled"
                
                if failures >= max_failures:
                    logging.error(f"HLS update timeout detected for camera {camera_id}. Restarting streaming.")
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
    if not process:
        logging.warning(f"Cannot monitor streaming process for camera {camera_id}: Invalid process")
        return
    
    try:
        # プロセスの終了を待機
        exit_code = process.wait()
        
        # プロセスが終了した場合
        logging.warning(f"Streaming process for camera {camera_id} has died. Return code: {exit_code}")
        
        # エラー出力をチェック
        if process.stderr:
            try:
                error_output = process.stderr.read()
                if error_output:
                    error_output = error_output.decode('utf-8', errors='replace')
                    logging.error(f"FFmpeg error output for camera {camera_id}: {error_output}")
            except Exception as e:
                logging.warning(f"Failed to read stderr: {e}")
        
        # ストリーミングプロセスの状態を更新
        camera_stream_status[camera_id] = "process_died"
        
        # プロセスの異常終了を検出したら再起動
        restart_streaming(camera_id)
        
    except Exception as e:
        logging.error(f"Error in monitor_streaming_process for camera {camera_id}: {e}")

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
                        os.remove(file_path)
                        logging.info(f"Removed file {file_path}")

                except Exception as e:
                    logging.error(f"Error removing file {file}: {e}")

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
        for file in os.listdir(camera_tmp_dir):
            if file.endswith('.ts'):
                file_path = os.path.join(camera_tmp_dir, file)

                try:
                    # ファイルが以下の条件を満たす場合に削除:
                    # 1. プレイリストに含まれていない
                    # 2. 作成から60秒以上経過している
                    if (file not in active_segments and current_time - os.path.getctime(file_path) > 60):
                        os.remove(file_path)
                        logging.debug(f"Removed old segment file: {file}")

                except Exception as e:
                    logging.error(f"Error removing file {file}: {e}")

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
        for camera_id, process in list(streaming_processes.items()):
            try:
                if process and process.poll() is None:
                    ffmpeg_utils.terminate_process(process)
                if camera_id in streaming_processes:
                    del streaming_processes[camera_id]
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
    # 一時ディレクトリをクリーンアップ
    try:
        if os.path.exists(config.TMP_PATH):
            for camera_dir in os.listdir(config.TMP_PATH):
                camera_path = os.path.join(config.TMP_PATH, camera_dir)
                if os.path.isdir(camera_path):
                    # 各カメラディレクトリ内のファイルを削除
                    for file in os.listdir(camera_path):
                        file_path = os.path.join(camera_path, file)
                        if os.path.isfile(file_path):
                            try:
                                os.remove(file_path)
                                logging.debug(f"Cleaned up old file: {file_path}")
                            except Exception as e:
                                logging.warning(f"Failed to remove file {file_path}: {e}")
    except Exception as e:
        logging.warning(f"Error cleaning up temporary directory: {e}")
    
    # 実行中のFFmpegプロセスを停止
    ffmpeg_utils.kill_ffmpeg_processes()
    
    # クリーンアップスレッドの起動
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    logging.info("Started segment cleanup scheduler thread")

def get_camera_streaming_status():
    """
    各カメラのストリーミング状態を取得

    Returns:
        dict: カメラIDをキー、状態を値とする辞書
    """
    return camera_stream_status
