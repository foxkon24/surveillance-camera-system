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
HEALTH_CHECK_INTERVAL = 10
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 20

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する（改善版）

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    if camera['id'] not in streaming_processes:
        try:
            # カメラ情報のログ出力（デバッグ用）
            logging.info(f"Starting streaming for camera {camera['id']} with URL {camera['rtsp_url']}")
            
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera['id'])
            fs_utils.ensure_directory_exists(camera_tmp_dir)

            # パスの正規化 - OSに適したパス区切り文字を使用
            hls_path = os.path.normpath(os.path.join(camera_tmp_dir, f"{camera['id']}.m3u8"))
            log_path = os.path.normpath(os.path.join(camera_tmp_dir, f"{camera['id']}.log"))

            # 既存のHLSファイルがあれば削除（クリーンスタート）
            for f in os.listdir(camera_tmp_dir):
                if f.endswith('.m3u8') or f.endswith('.ts'):
                    try:
                        os.remove(os.path.join(camera_tmp_dir, f))
                        logging.debug(f"Removed existing file: {f}")
                    except Exception as e:
                        logging.warning(f"Could not remove file {f}: {e}")

            # 既存のffmpegプロセスが残っている場合、強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera['id'])
            time.sleep(1)  # プロセス終了待ち

            # RTSPでカメラが応答するか確認
            try:
                probe_command = [
                    'ffprobe',
                    '-rtsp_transport', 'tcp',
                    '-v', 'error',
                    '-timeout', '5000000',  # 5秒タイムアウト
                    '-i', camera['rtsp_url'],
                    '-show_entries', 'stream=codec_type',
                    '-of', 'json'
                ]
                
                result = subprocess.run(probe_command, capture_output=True, timeout=10)
                
                if result.returncode != 0:
                    error_output = result.stderr.decode('utf-8', errors='replace')
                    if "401 Unauthorized" in error_output:
                        logging.warning(f"RTSP connection failed: {camera['rtsp_url']}, Error: {error_output.strip()}")
                        logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
                    elif "Connection timed out" in error_output or "Operation timed out" in error_output:
                        logging.error(f"RTSP connection timeout: {camera['rtsp_url']}")
                        logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
                    else:
                        logging.warning(f"RTSP connection failed: {camera['rtsp_url']}, Error: {error_output.strip()}")
                        logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
                else:
                    logging.info(f"RTSP connection successful: {camera['rtsp_url']}")
            except subprocess.TimeoutExpired:
                logging.error(f"RTSP connection timeout: {camera['rtsp_url']}")
                logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
            except Exception as e:
                logging.error(f"Error checking RTSP stream: {e}")

            # Nginx用に最適化されたHLSセグメントパス
            segment_path = os.path.normpath(os.path.join(camera_tmp_dir, f"{camera['id']}_%03d.ts"))
            
            # HLSストリーミング用FFmpegコマンド生成
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
                camera['rtsp_url'], 
                hls_path,
                segment_path,
                segment_time=2,  # 短いセグメントでレイテンシを減らす
                list_size=10     # より多くのセグメントをプレイリストに保持
            )

            # プロセス起動
            process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command, log_path=log_path)
            streaming_processes[camera['id']] = process
            
            # 初期化時点で更新情報を記録
            hls_last_update[camera['id']] = time.time()
            if os.path.exists(hls_path):
                m3u8_last_size[camera['id']] = os.path.getsize(hls_path)
            else:
                m3u8_last_size[camera['id']] = 0

            # 監視スレッドを開始
            monitor_thread = threading.Thread(
                target=monitor_streaming_process,
                args=(camera['id'], process),
                daemon=True
            )
            monitor_thread.start()

            # ファイル更新監視スレッドを開始
            hls_monitor_thread = threading.Thread(
                target=monitor_hls_updates,
                args=(camera['id'],),
                daemon=True
            )
            hls_monitor_thread.start()

            # HLSファイルが生成されるまで少し待機
            wait_time = 0
            max_wait = 5  # 最大5秒待機
            while not os.path.exists(hls_path) and wait_time < max_wait:
                time.sleep(0.5)
                wait_time += 0.5

            if not os.path.exists(hls_path):
                logging.warning(f"HLS file not created for camera {camera['id']} after {max_wait} seconds")
            else:
                logging.info(f"HLS file created for camera {camera['id']}")

            # エラー出力を監視するスレッド
            error_thread = threading.Thread(
                target=ffmpeg_utils.monitor_ffmpeg_output, 
                args=(process,), 
                daemon=True
            )
            error_thread.start()

            logging.info(f"Started streaming for camera {camera['id']}")
            return True

        except Exception as e:
            logging.error(f"Error starting streaming for camera {camera['id']}: {e}")
            logging.exception("Full stack trace:")
            return False

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
        
        # 既存のffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        
        # ストリーミングプロセスを削除
        if camera_id in streaming_processes:
            del streaming_processes[camera_id]
        
        # カメラ設定を読み込んでストリーミングを再開する前に少し待機
        time.sleep(2)
        
        # カメラ設定を読み込んでストリーミングを再開
        camera = camera_utils.get_camera_by_id(camera_id)
        if camera:
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
        logging.exception("Full stack trace:")
        return False

def monitor_hls_updates(camera_id):
    """
    HLSファイルの更新状態を監視する関数（改善版）

    Args:
        camera_id (str): 監視するカメラID
    """
    camera_tmp_dir = os.path.normpath(os.path.join(config.TMP_PATH, camera_id))
    hls_path = os.path.normpath(os.path.join(camera_tmp_dir, f"{camera_id}.m3u8"))
    
    failures = 0
    max_failures = 2  # 連続でこの回数分問題が検出されたら再起動
    check_interval = HEALTH_CHECK_INTERVAL  # 正常時の確認間隔
    problem_check_interval = 5  # 問題検出時の短い確認間隔
    
    while True:
        try:
            if camera_id not in streaming_processes:
                # ストリーミングが停止していたら監視も終了
                logging.info(f"Streaming process for camera {camera_id} no longer exists. Stopping HLS monitor.")
                break
            
            current_time = time.time()
            file_updated = False
            
            # m3u8ファイルの存在と更新チェック
            if os.path.exists(hls_path):
                # ファイルサイズをチェック
                current_size = os.path.getsize(hls_path)
                last_size = m3u8_last_size.get(camera_id, 0)
                
                if current_size != last_size:
                    # ファイルサイズが変わっていれば更新されている
                    m3u8_last_size[camera_id] = current_size
                    hls_last_update[camera_id] = current_time
                    file_updated = True
                    failures = 0  # 正常更新を検出したらカウンタをリセット
                    
                    # ファイル内容を確認してセグメント数をチェック（デバッグレベルでログを出力）
                    try:
                        with open(hls_path, 'r') as f:
                            content = f.read()
                            # セグメント参照が少なくとも1つあるか確認
                            if ".ts" in content:
                                logging.debug(f"HLS playlist for camera {camera_id} contains segment references")
                            else:
                                logging.warning(f"HLS playlist for camera {camera_id} does not contain any segment references")
                                # セグメント参照がない場合も問題とみなす
                                file_updated = False
                    except Exception as read_err:
                        logging.error(f"Error reading HLS playlist for camera {camera_id}: {read_err}")
            
            # TSファイルの更新も確認
            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
            if ts_files:
                newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                newest_ts_path = os.path.join(camera_tmp_dir, newest_ts)
                ts_mtime = os.path.getmtime(newest_ts_path)
                
                if ts_mtime > hls_last_update.get(camera_id, 0):
                    hls_last_update[camera_id] = current_time
                    file_updated = True
                    failures = 0  # 正常更新を検出したらカウンタをリセット
            
            # ファイル更新が停止しているかチェック
            last_update = hls_last_update.get(camera_id, 0)
            if not file_updated and (current_time - last_update) > HLS_UPDATE_TIMEOUT:
                logging.warning(f"HLS files for camera {camera_id} have not been updated for {current_time - last_update:.2f} seconds")
                failures += 1
                
                if failures >= max_failures:
                    logging.error(f"HLS update timeout detected for camera {camera_id}. Restarting streaming.")
                    restart_streaming(camera_id)
                    failures = 0
                    
                    # 監視を終了（新しいスレッドが開始されるため）
                    break
                
                # 問題検出時は頻繁にチェック
                time.sleep(problem_check_interval)
                continue
            
        except Exception as e:
            logging.error(f"Error monitoring HLS updates for camera {camera_id}: {e}")
            time.sleep(problem_check_interval)
            continue
        
        # 正常時は通常間隔でチェック
        time.sleep(check_interval)

def monitor_streaming_process(camera_id, process):
    """
    ストリーミングプロセス監視関数（改善版）

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
            # プロセスが終了しているか確認
            if process is None or process.poll() is not None:
                return_code = process.poll() if process else None
                logging.warning(f"Streaming process for camera {camera_id} has died. Return code: {return_code}")
                
                consecutive_failures += 1
                current_delay = min(retry_delay * consecutive_failures, max_retry_delay)

                logging.warning(f"Attempt {consecutive_failures}/{max_failures}. "
                                f"Waiting {current_delay} seconds before retry.")

                # 既存のffmpegプロセスを強制終了
                ffmpeg_utils.kill_ffmpeg_processes(camera_id)
                time.sleep(current_delay)

                if consecutive_failures >= max_failures:
                    logging.error(f"Too many consecutive failures for camera {camera_id}. Performing full restart.")
                    cleanup_camera_resources(camera_id)
                    consecutive_failures = 0

                # ストリーミングプロセスを削除
                if camera_id in streaming_processes:
                    del streaming_processes[camera_id]

                # カメラ設定を読み込んでストリーミングを再開
                camera = camera_utils.get_camera_by_id(camera_id)
                if camera:
                    # 接続試行前に一時停止（ネットワーク安定化のため）
                    time.sleep(2)
                    
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
                        os.remove(file_path)
                        logging.debug(f"Removed file: {file_path}")

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
    # 開始時にログを出力
    logging.info("============= アプリケーション起動 =============")
    logging.info(f"実行パス: {os.getcwd()}")
    logging.info(f"Pythonバージョン: {os.sys.version}")
    logging.info(f"OSバージョン: {os.name}")
    
    # クリーンアップスレッドの起動
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    logging.info("Started segment cleanup scheduler thread")
