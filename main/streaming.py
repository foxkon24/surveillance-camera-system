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
# ストリーミングステータスを保持
streaming_status = {}  # 追加
# 健全性チェックの間隔（秒）
HEALTH_CHECK_INTERVAL = 15
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 30

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
            # ストリーミングの開始をステータスに記録
            streaming_status[camera['id']] = {
                'status': 'starting',
                'message': 'Starting streaming process',
                'timestamp': time.time()
            }
            
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera['id'])
            fs_utils.ensure_directory_exists(camera_tmp_dir)

            hls_path = os.path.join(camera_tmp_dir, f"{camera['id']}.m3u8").replace('/', '\\')
            log_path = os.path.join(camera_tmp_dir, f"{camera['id']}.log").replace('/', '\\')

            if os.path.exists(hls_path):
                os.remove(hls_path)

            # 既存のffmpegプロセスが残っている場合、強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera['id'])
            time.sleep(1)  # プロセス終了待ち

            # RTSPストリームに接続できるかテスト
            if not test_rtsp_connection(camera['rtsp_url']):
                error_msg = f"Cannot connect to RTSP stream: {camera['rtsp_url']}"
                logging.error(error_msg)
                streaming_status[camera['id']] = {
                    'status': 'error',
                    'message': error_msg,
                    'timestamp': time.time()
                }
                return False

            # Nginx用に最適化されたHLSセグメントパス
            segment_path = os.path.join(camera_tmp_dir, f"{camera['id']}_%03d.ts").replace('/', '\\')
            
            # HLSストリーミング用FFmpegコマンド生成
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
                camera['rtsp_url'], 
                hls_path,
                segment_path
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

            # エラー出力を監視するスレッド
            error_thread = threading.Thread(
                target=ffmpeg_utils.monitor_ffmpeg_output,
                args=(process, camera['id']),
                daemon=True
            )
            error_thread.start()

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

            # ストリーミング開始を確認
            success = verify_streaming_started(camera['id'], hls_path)
            if success:
                logging.info(f"Started streaming for camera {camera['id']}")
                streaming_status[camera['id']] = {
                    'status': 'streaming',
                    'message': 'Streaming in progress',
                    'timestamp': time.time()
                }
            else:
                logging.error(f"Failed to start streaming for camera {camera['id']} - no .m3u8 file created")
                streaming_status[camera['id']] = {
                    'status': 'error',
                    'message': 'Failed to start streaming - no .m3u8 file created',
                    'timestamp': time.time()
                }
                # プロセスを終了
                if camera['id'] in streaming_processes:
                    try:
                        ffmpeg_utils.terminate_process(streaming_processes[camera['id']])
                        del streaming_processes[camera['id']]
                    except Exception as e:
                        logging.error(f"Error terminating process: {e}")
                return False
                
            return True

        except Exception as e:
            logging.error(f"Error starting streaming for camera {camera['id']}: {e}")
            streaming_status[camera['id']] = {
                'status': 'error',
                'message': f"Failed to start streaming: {str(e)}",
                'timestamp': time.time()
            }
            return False

    return True

def test_rtsp_connection(rtsp_url, timeout=5):
    """
    RTSPストリームに接続できるかテストする関数
    
    Args:
        rtsp_url (str): テストするRTSP URL
        timeout (int): 接続タイムアウト（秒）
        
    Returns:
        bool: 接続に成功したかどうか
    """
    try:
        # FFprobeを使用して接続テスト（短時間で終了するオプション）
        ffprobe_command = [
            'ffprobe',
            '-v', 'error',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_type',
            '-of', 'json',
            '-timeout', str(timeout * 1000000)  # マイクロ秒単位
        ]
        
        result = subprocess.run(
            ffprobe_command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            timeout=timeout+2  # プロセス自体のタイムアウト
        )
        
        # 成功ならば終了コードは0
        if result.returncode == 0:
            logging.info(f"RTSP connection test successful: {rtsp_url}")
            return True
            
        # エラー内容をログに記録
        error_output = result.stderr.decode('utf-8', errors='replace')
        logging.warning(f"RTSP connection test failed: {rtsp_url} - {error_output}")
        return False
        
    except subprocess.TimeoutExpired:
        logging.warning(f"RTSP connection test timed out: {rtsp_url}")
        return False
    except Exception as e:
        logging.error(f"Error testing RTSP connection: {e}")
        return False

def verify_streaming_started(camera_id, hls_path, max_attempts=10, check_interval=1):
    """
    ストリーミングが正常に開始されたことを検証する
    
    Args:
        camera_id (str): カメラID
        hls_path (str): .m3u8ファイルのパス
        max_attempts (int): 最大確認回数
        check_interval (float): 確認間隔（秒）
        
    Returns:
        bool: ストリーミングが正常に開始されたかどうか
    """
    attempts = 0
    
    while attempts < max_attempts:
        if os.path.exists(hls_path) and os.path.getsize(hls_path) > 0:
            # TSファイルも確認
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
            
            if ts_files:
                return True
                
        attempts += 1
        time.sleep(check_interval)
    
    return False

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
        streaming_status[camera_id] = {
            'status': 'restarting',
            'message': 'Restarting streaming',
            'timestamp': time.time()
        }
        
        # 既存のffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        
        # ストリーミングプロセスを削除
        if camera_id in streaming_processes:
            del streaming_processes[camera_id]
        
        # カメラ設定を読み込んでストリーミングを再開
        camera = camera_utils.get_camera_by_id(camera_id)
        if camera:
            success = get_or_start_streaming(camera)
            if success:
                logging.info(f"Successfully restarted streaming for camera {camera_id}")
                return True
            else:
                logging.error(f"Failed to restart streaming for camera {camera_id}")
                streaming_status[camera_id] = {
                    'status': 'error',
                    'message': 'Failed to restart streaming',
                    'timestamp': time.time()
                }
                return False
        else:
            logging.error(f"Camera config not found for camera {camera_id}")
            streaming_status[camera_id] = {
                'status': 'error',
                'message': 'Camera configuration not found',
                'timestamp': time.time()
            }
            return False
    
    except Exception as e:
        logging.error(f"Error restarting streaming for camera {camera_id}: {e}")
        streaming_status[camera_id] = {
            'status': 'error',
            'message': f"Error restarting streaming: {str(e)}",
            'timestamp': time.time()
        }
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
                    streaming_status[camera_id] = {
                        'status': 'stalled',
                        'message': f"HLS files not updated for {current_time - last_update:.2f} seconds",
                        'timestamp': current_time
                    }
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
            # プロセスが終了しているか確認
            if process.poll() is not None:
                consecutive_failures += 1
                current_delay = min(retry_delay * consecutive_failures, max_retry_delay)

                logging.warning(f"Streaming process for camera {camera_id} has died. "
                                f"Attempt {consecutive_failures}/{max_failures}. "
                                f"Waiting {current_delay} seconds before retry.")
                
                # ステータスを更新
                streaming_status[camera_id] = {
                    'status': 'process_died',
                    'message': f"Streaming process died. Attempt {consecutive_failures}/{max_failures}",
                    'timestamp': time.time()
                }

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

                # プロセスは実行中だがHLSファイルが存在するか確認
                camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8").replace('/', '\\')
                
                if not os.path.exists(hls_path) and camera_id in hls_last_update:
                    # 最後の更新から30秒以上経過していて、ファイルが存在しない場合
                    current_time = time.time()
                    last_update = hls_last_update.get(camera_id, 0)
                    
                    if (current_time - last_update) > 30:
                        logging.error(f"HLS file does not exist for camera {camera_id} after 30 seconds. Restarting streaming.")
                        streaming_status[camera_id] = {
                            'status': 'no_output',
                            'message': "HLS file not created despite running process",
                            'timestamp': current_time
                        }
                        restart_streaming(camera_id)
                        break

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
                        logging.info(f"Removed old segment file: {file}")

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
                
                # ステータスを更新
                streaming_status[camera_id] = {
                    'status': 'stopped',
                    'message': 'Streaming stopped by user',
                    'timestamp': time.time()
                }

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
    # クリーンアップスレッドの起動
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    logging.info("Started segment cleanup scheduler thread")

def get_streaming_status():
    """
    すべてのカメラのストリーミング状態を取得
    
    Returns:
        dict: カメラIDをキー、状態情報を値とする辞書
    """
    # 現在のストリーミングプロセスの状態を確認して更新
    for camera_id in streaming_processes:
        if camera_id not in streaming_status:
            streaming_status[camera_id] = {
                'status': 'streaming',
                'message': 'Streaming in progress',
                'timestamp': time.time()
            }
        elif streaming_status[camera_id]['status'] != 'error':
            # エラー状態でなければプロセスの状態を確認
            process = streaming_processes[camera_id]
            if process.poll() is not None:
                # プロセスが終了している
                streaming_status[camera_id] = {
                    'status': 'stopped',
                    'message': 'Streaming process has stopped unexpectedly',
                    'timestamp': time.time()
                }
    
    return streaming_status
