"""
録画管理モジュール
録画プロセスの管理機能を提供します
"""
import os
import logging
import threading
import time
from datetime import datetime
import subprocess

import config
import ffmpeg_utils
import fs_utils
import camera_utils

# グローバル変数
recording_processes = {}
recording_threads = {}
recording_start_times = {}  # 録画開始時刻を保持する辞書
recording_status = {}       # 録画ステータスを保持する辞書（追加）

def start_recording(camera_id, rtsp_url):
    """
    録画を開始する関数

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        # 既存のプロセスが存在する場合は終了
        if camera_id in recording_processes:
            stop_recording(camera_id)

        # 録画用ディレクトリの確認と作成
        camera_dir = os.path.join(config.RECORD_PATH, camera_id)
        fs_utils.ensure_directory_exists(camera_dir)

        # ディスク容量チェック（最小1GB必要）
        required_space = 1024 * 1024 * 1024 * config.MIN_DISK_SPACE_GB
        available_space = fs_utils.get_free_space(camera_dir)

        if available_space < required_space:
            error_msg = f"Insufficient disk space for camera {camera_id}. " \
                        f"Available: {available_space / (1024*1024*1024):.2f} GB, " \
                        f"Required: {config.MIN_DISK_SPACE_GB} GB"
            logging.error(error_msg)
            raise Exception(error_msg)

        # 新しい録画を開始
        start_new_recording(camera_id, rtsp_url)
        return True

    except Exception as e:
        error_msg = f"Error starting recording for camera {camera_id}: {e}"
        logging.error(error_msg)
        recording_status[camera_id] = {
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now()
        }
        raise Exception(error_msg)

def start_new_recording(camera_id, rtsp_url):
    """
    新しい録画プロセスを開始する

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL
    """
    try:
        logging.info(f"Starting new recording for camera {camera_id} with URL {rtsp_url}")
        recording_status[camera_id] = {
            'status': 'starting',
            'message': 'Starting recording process',
            'timestamp': datetime.now()
        }

        # RTSPストリームに接続できるかテスト
        if not test_rtsp_connection(rtsp_url):
            error_msg = f"Cannot connect to RTSP stream: {rtsp_url}"
            logging.error(error_msg)
            recording_status[camera_id] = {
                'status': 'error',
                'message': error_msg,
                'timestamp': datetime.now()
            }
            raise Exception(error_msg)

        # 音声ストリームの確認を追加
        try:
            has_audio = ffmpeg_utils.check_audio_stream(rtsp_url)
            if not has_audio:
                logging.warning(f"Camera {camera_id} may not have audio capability or audio stream is not available")
        except Exception as e:
            logging.error(f"Error checking audio stream: {e}")
            # 音声チェックは失敗しても続行

        # 既存の録画を停止
        if camera_id in recording_processes:
            logging.info(f"Stopping existing recording for camera {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)

        # 録画ファイルパスの生成
        file_path = fs_utils.get_record_file_path(config.RECORD_PATH, camera_id)
        logging.info(f"Recording will be saved to: {file_path}")

        # FFmpegコマンドを生成
        ffmpeg_command = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, file_path)
        logging.info(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

        # プロセスを開始
        process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command)

        # プロセス情報を保存
        recording_processes[camera_id] = {
            'process': process,
            'file_path': file_path
        }
        recording_start_times[camera_id] = datetime.now()
        recording_status[camera_id] = {
            'status': 'recording',
            'message': 'Recording in progress',
            'timestamp': datetime.now()
        }

        logging.info(f"Recording process started with PID {process.pid}")

        # エラー出力を監視するスレッド
        error_thread = threading.Thread(
            target=ffmpeg_utils.monitor_ffmpeg_output, 
            args=(process, camera_id), 
            daemon=True
        )
        error_thread.start()

        # 録画時間監視スレッドの開始
        if camera_id not in recording_threads or not recording_threads[camera_id].is_alive():
            monitor_thread = threading.Thread(target=check_recording_duration, args=(camera_id,), daemon=True)
            monitor_thread.start()
            recording_threads[camera_id] = monitor_thread

        # プロセスの状態を確認
        time.sleep(2)  # プロセスの起動を待つ

        if process.poll() is not None:
            return_code = process.poll()
            error_output = process.stderr.read().decode('utf-8', errors='replace') if process.stderr else "No error output available"
            logging.error(f"FFmpeg process failed to start. Return code: {return_code}")
            logging.error(f"FFmpeg error output: {error_output}")
            
            # ステータスを更新
            recording_status[camera_id] = {
                'status': 'error',
                'message': f"FFmpeg failed to start: {error_output}",
                'timestamp': datetime.now()
            }
            
            # プロセス情報をクリア
            if camera_id in recording_processes:
                del recording_processes[camera_id]
                
            raise Exception(f"FFmpeg failed to start: {error_output}")

        # ファイルが実際に作成されたか確認
        verify_attempts = 0
        max_verify_attempts = 10
        
        while verify_attempts < max_verify_attempts:
            if os.path.exists(file_path):
                logging.info(f"Recording file confirmed: {file_path}")
                break
                
            verify_attempts += 1
            time.sleep(1)
            
        if verify_attempts >= max_verify_attempts:
            error_msg = f"Recording file was not created: {file_path}"
            logging.error(error_msg)
            recording_status[camera_id] = {
                'status': 'error',
                'message': error_msg,
                'timestamp': datetime.now()
            }
            
            # プロセスを終了
            if camera_id in recording_processes:
                try:
                    ffmpeg_utils.terminate_process(recording_processes[camera_id]['process'])
                    del recording_processes[camera_id]
                except Exception as e:
                    logging.error(f"Error terminating process: {e}")
                
            raise Exception(error_msg)

    except Exception as e:
        logging.error(f"Error starting new recording for camera {camera_id}: {e}")
        logging.exception("Full stack trace:")
        recording_status[camera_id] = {
            'status': 'error',
            'message': str(e),
            'timestamp': datetime.now()
        }
        raise

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

def stop_recording(camera_id):
    """
    録画を停止する関数

    Args:
        camera_id (str): カメラID

    Returns:
        bool: 操作が成功したかどうか
    """
    logging.info(f"Attempting to stop recording for camera {camera_id}")

    recording_info = recording_processes.pop(camera_id, None)

    if camera_id in recording_start_times:
        del recording_start_times[camera_id]

    if recording_info:
        process = recording_info['process']
        file_path = recording_info['file_path']

        try:
            logging.info(f"Stopping recording process (PID: {process.pid}) for file: {file_path}")

            # プロセスを終了
            ffmpeg_utils.terminate_process(process)

            # ファイル存在確認
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                logging.info(f"Recording file exists. Size: {file_size} bytes")

                if file_size > 0:
                    ffmpeg_utils.finalize_recording(file_path)
                else:
                    logging.warning("Recording file is empty")
            else:
                logging.error(f"Recording file not found: {file_path}")
                # ファイルが見つからなくてもエラーを発生させない
                # 代わりにステータスを更新
                recording_status[camera_id] = {
                    'status': 'warning',
                    'message': f"Recording stopped but file not found: {file_path}",
                    'timestamp': datetime.now()
                }

            # 録画停止のステータスを設定
            recording_status[camera_id] = {
                'status': 'stopped',
                'message': 'Recording stopped',
                'timestamp': datetime.now()
            }

            return True

        except Exception as e:
            logging.error(f"Error in stop_recording: {e}")
            logging.exception("Full stack trace:")
            
            recording_status[camera_id] = {
                'status': 'error',
                'message': f"Error stopping recording: {str(e)}",
                'timestamp': datetime.now()
            }
            
            return False
    else:
        logging.warning(f"No recording process found for camera {camera_id}")
        
        recording_status[camera_id] = {
            'status': 'warning',
            'message': 'No recording process was active',
            'timestamp': datetime.now()
        }
        
        return False

def check_recording_duration(camera_id):
    """
    録画時間をチェックし、必要に応じて録画を再開する

    Args:
        camera_id (str): チェックするカメラID
    """
    while True:
        try:
            if camera_id not in recording_processes:
                logging.info(f"Recording process for camera {camera_id} no longer exists. Stopping monitor thread.")
                break

            current_time = datetime.now()
            start_time = recording_start_times.get(camera_id)

            if not start_time:
                logging.warning(f"No start time found for camera {camera_id}")
                time.sleep(10)  # 短縮して頻繁にチェック
                continue

            duration = current_time - start_time
            duration_seconds = duration.total_seconds()

            # プロセスの状態を確認
            if camera_id in recording_processes:
                process_info = recording_processes[camera_id]
                process = process_info['process']
                
                # プロセスが終了している場合は再起動
                if process.poll() is not None:
                    logging.warning(f"Recording process for camera {camera_id} has stopped unexpectedly. Restarting...")
                    camera_config = camera_utils.get_camera_by_id(camera_id)
                    if camera_config:
                        stop_recording(camera_id)
                        time.sleep(2)
                        try:
                            start_new_recording(camera_id, camera_config['rtsp_url'])
                        except Exception as e:
                            logging.error(f"Failed to restart recording for camera {camera_id}: {e}")
                    else:
                        logging.error(f"Camera configuration not found for camera {camera_id}")
                    
                    # この後、プロセスが再起動するまで少し待機
                    time.sleep(30)
                    continue

            # 設定された時間経過で録画を再開
            max_duration = config.MAX_RECORDING_HOURS * 3600  # 時間を秒に変換
            if duration_seconds >= max_duration:
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config:
                    logging.info(f"Restarting recording for camera {camera_id} due to duration limit")
                    stop_recording(camera_id)
                    time.sleep(2)
                    try:
                        start_new_recording(camera_id, camera_config['rtsp_url'])
                    except Exception as e:
                        logging.error(f"Failed to restart recording for camera {camera_id} due to duration limit: {e}")
                else:
                    logging.error(f"Camera configuration not found for camera {camera_id}")

            # ファイルの存在を確認
            if camera_id in recording_processes:
                file_path = recording_processes[camera_id]['file_path']
                if not os.path.exists(file_path) and duration_seconds > 30:  # 録画開始から30秒以上経過している場合
                    logging.error(f"Recording file does not exist after 30 seconds: {file_path}. Restarting recording.")
                    camera_config = camera_utils.get_camera_by_id(camera_id)
                    if camera_config:
                        stop_recording(camera_id)
                        time.sleep(2)
                        try:
                            start_new_recording(camera_id, camera_config['rtsp_url'])
                        except Exception as e:
                            logging.error(f"Failed to restart recording for missing file for camera {camera_id}: {e}")
                    else:
                        logging.error(f"Camera configuration not found for camera {camera_id}")

                # ファイルサイズが0の場合も再起動
                elif os.path.exists(file_path) and os.path.getsize(file_path) == 0 and duration_seconds > 30:
                    logging.error(f"Recording file is empty after 30 seconds: {file_path}. Restarting recording.")
                    camera_config = camera_utils.get_camera_by_id(camera_id)
                    if camera_config:
                        stop_recording(camera_id)
                        time.sleep(2)
                        try:
                            start_new_recording(camera_id, camera_config['rtsp_url'])
                        except Exception as e:
                            logging.error(f"Failed to restart recording for empty file for camera {camera_id}: {e}")
                    else:
                        logging.error(f"Camera configuration not found for camera {camera_id}")

        except Exception as e:
            logging.error(f"Error in check_recording_duration for camera {camera_id}: {e}")

        time.sleep(10)  # より頻繁なチェック間隔

def monitor_recording_processes():
    """
    すべての録画プロセスを監視し、必要に応じて再起動する
    """
    while True:
        try:
            cameras = camera_utils.read_config()
            for camera in cameras:
                camera_id = camera['id']
                if camera_id in recording_processes:
                    recording_info = recording_processes[camera_id]
                    process = recording_info['process']
                    file_path = recording_info['file_path']

                    # プロセスの状態を確認
                    if process.poll() is not None:  # プロセスが終了している場合
                        logging.warning(f"Recording process for camera {camera_id} has died. Restarting...")

                        # 録画を再開
                        try:
                            stop_recording(camera_id)  # 念のため停止処理を実行
                            time.sleep(2)  # 少し待機
                            start_recording(camera_id, camera['rtsp_url'])
                            logging.info(f"Successfully restarted recording for camera {camera_id}")

                        except Exception as e:
                            logging.error(f"Failed to restart recording for camera {camera_id}: {e}")
                            
                    # ファイルの存在を確認
                    elif not os.path.exists(file_path) and camera_id in recording_start_times:
                        start_time = recording_start_times[camera_id]
                        duration = (datetime.now() - start_time).total_seconds()
                        
                        if duration > 30:  # 録画開始から30秒以上経過している場合
                            logging.error(f"Recording file does not exist: {file_path}. Restarting recording.")
                            try:
                                stop_recording(camera_id)
                                time.sleep(2)
                                start_recording(camera_id, camera['rtsp_url'])
                                logging.info(f"Successfully restarted recording for camera {camera_id} due to missing file")
                            except Exception as e:
                                logging.error(f"Failed to restart recording for camera {camera_id} due to missing file: {e}")

        except Exception as e:
            logging.error(f"Error in monitor_recording_processes: {e}")

        time.sleep(30)  # 30秒ごとにチェック

def initialize_recording():
    """
    録画システムの初期化
    """
    # 監視スレッドの起動
    monitor_thread = threading.Thread(target=monitor_recording_processes, daemon=True)
    monitor_thread.start()
    logging.info("Started recording process monitor thread")

def start_all_recordings():
    """
    すべてのカメラの録画を開始

    Returns:
        bool: 操作が成功したかどうか
    """
    success = True
    cameras = camera_utils.read_config()
    for camera in cameras:
        try:
            if camera['rtsp_url']:
                start_recording(camera['id'], camera['rtsp_url'])

        except Exception as e:
            logging.error(f"Failed to start recording for camera {camera['id']}: {e}")
            success = False

    return success

def stop_all_recordings():
    """
    すべてのカメラの録画を停止
    
    Returns:
        bool: 操作が成功したかどうか
    """
    success = True
    camera_ids = list(recording_processes.keys())
    for camera_id in camera_ids:
        try:
            stop_recording(camera_id)

        except Exception as e:
            logging.error(f"Failed to stop recording for camera {camera_id}: {e}")
            success = False

    return success

def get_recording_status():
    """
    すべてのカメラの録画状態を取得
    
    Returns:
        dict: カメラIDをキー、状態情報を値とする辞書
    """
    # 現在の録画プロセスの状態を確認して更新
    for camera_id in recording_processes:
        if camera_id not in recording_status:
            recording_status[camera_id] = {
                'status': 'recording',
                'message': 'Recording in progress',
                'timestamp': datetime.now()
            }
        elif recording_status[camera_id]['status'] != 'error':
            # エラー状態でなければプロセスの状態を確認
            process = recording_processes[camera_id]['process']
            if process.poll() is not None:
                # プロセスが終了している
                recording_status[camera_id] = {
                    'status': 'stopped',
                    'message': 'Recording process has stopped unexpectedly',
                    'timestamp': datetime.now()
                }
    
    return recording_status
