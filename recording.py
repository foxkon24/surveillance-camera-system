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

# 録画プロセスのロック
recording_locks = {}
# カメラの録画状態（0=未録画, 1=録画中, 2=一時停止, 3=エラー）
recording_status = {}
# 各カメラの最終録画試行時間
last_recording_attempt = {}
# 録画再試行までの間隔（秒）
RECORDING_RETRY_DELAY = 60

def start_recording(camera_id, rtsp_url):
    """
    録画を開始する関数

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL

    Returns:
        bool: 操作が成功したかどうか
    """
    # カメラのロックがまだ存在しない場合は作成
    if camera_id not in recording_locks:
        recording_locks[camera_id] = threading.Lock()
        
    # 最後の録画試行から十分な時間が経過していないなら、すぐに再試行しない
    current_time = time.time()
    if camera_id in last_recording_attempt:
        time_since_last_attempt = current_time - last_recording_attempt[camera_id]
        if time_since_last_attempt < RECORDING_RETRY_DELAY:
            # エラー状態になっているカメラは接続しない
            if camera_id in recording_status and recording_status[camera_id] == 3:
                logging.warning(f"Camera {camera_id} recording is in error state. Waiting for retry delay to expire.")
                return False
    
    # 最終録画試行時間を記録
    last_recording_attempt[camera_id] = current_time
    
    # ロックを取得してプロセス操作
    with recording_locks[camera_id]:
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
                recording_status[camera_id] = 3  # エラー状態
                raise Exception(error_msg)

            # 新しい録画を開始
            success = start_new_recording(camera_id, rtsp_url)
            if success:
                recording_status[camera_id] = 1  # 録画中状態
            else:
                recording_status[camera_id] = 3  # エラー状態
                
            return success

        except Exception as e:
            error_msg = f"Error starting recording for camera {camera_id}: {e}"
            logging.error(error_msg)
            recording_status[camera_id] = 3  # エラー状態
            raise Exception(error_msg)

def start_new_recording(camera_id, rtsp_url):
    """
    新しい録画プロセスを開始する

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL
        
    Returns:
        bool: 録画の開始が成功したかどうか
    """
    try:
        logging.info(f"Starting new recording for camera {camera_id} with URL {rtsp_url}")

        # RTSP接続の確認
        if not ffmpeg_utils.check_rtsp_connection(rtsp_url):
            error_msg = f"Cannot connect to RTSP stream: {rtsp_url}"
            logging.error(error_msg)
            raise Exception(error_msg)

        # 音声ストリームの確認を追加
        has_audio = ffmpeg_utils.check_audio_stream(rtsp_url)
        if not has_audio:
            logging.warning(f"Camera {camera_id} may not have audio capability or audio stream is not available")

        # 既存の録画を停止
        if camera_id in recording_processes:
            logging.info(f"Stopping existing recording for camera {camera_id}")
            _stop_recording_process(camera_id)
            time.sleep(2)

        # 録画ファイルパスの生成
        file_path = fs_utils.get_record_file_path(config.RECORD_PATH, camera_id)
        logging.info(f"Recording will be saved to: {file_path}")

        # FFmpegコマンドを生成
        ffmpeg_command = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, file_path)
        logging.info(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

        # 既存のffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        time.sleep(1)  # プロセス終了待ち

        # プロセスを開始
        process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command)
        
        # プロセスが正常に開始されたか確認
        if process is None or process.poll() is not None:
            logging.error(f"Failed to start ffmpeg process for camera {camera_id}")
            return False

        # プロセス情報を保存
        recording_processes[camera_id] = {
            'process': process,
            'file_path': file_path
        }
        recording_start_times[camera_id] = datetime.now()

        logging.info(f"Recording process started with PID {process.pid}")

        # エラー出力を監視するスレッド
        error_thread = threading.Thread(
            target=ffmpeg_utils.monitor_ffmpeg_output, 
            args=(process,), 
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
            error_output = ""
            if process.stderr:
                error_output = process.stderr.read().decode('utf-8', errors='replace')
            logging.error(f"FFmpeg process failed to start. Return code: {return_code}")
            if error_output:
                logging.error(f"FFmpeg error output: {error_output}")
            return False

        return True

    except Exception as e:
        logging.error(f"Error starting new recording for camera {camera_id}: {e}")
        logging.exception("Full stack trace:")
        return False

def _stop_recording_process(camera_id):
    """
    特定カメラの録画プロセスを停止する内部関数
    この関数を呼び出す前にロックを取得していることが前提

    Args:
        camera_id (str): 停止するカメラID
        
    Returns:
        bool: 停止が成功したかどうか
    """
    recording_info = recording_processes.get(camera_id)
    if not recording_info:
        return False
    
    process = recording_info.get('process')
    file_path = recording_info.get('file_path')
    
    if not process:
        return False
    
    try:
        # プロセスが実行中かチェック
        if process.poll() is None:
            logging.info(f"Stopping recording process (PID: {process.pid}) for file: {file_path}")
            # プロセスを終了
            ffmpeg_utils.terminate_process(process)
        
        # ファイル存在確認
        if file_path and os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            logging.info(f"Recording file exists. Size: {file_size} bytes")

            if file_size > 0:
                ffmpeg_utils.finalize_recording(file_path)
            else:
                logging.warning("Recording file is empty")
        else:
            logging.error(f"Recording file not found: {file_path}")
        
        # いずれにしてもプロセス情報をクリア
        return True
    
    except Exception as e:
        logging.error(f"Error in _stop_recording_process: {e}")
        logging.exception("Full stack trace:")
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
    
    # カメラのロックがまだ存在しない場合は作成
    if camera_id not in recording_locks:
        recording_locks[camera_id] = threading.Lock()
    
    # ロックを取得してプロセス操作
    with recording_locks[camera_id]:
        result = _stop_recording_process(camera_id)
        
        # 録画開始時間情報をクリア
        if camera_id in recording_start_times:
            del recording_start_times[camera_id]
        
        # プロセス情報をクリア
        if camera_id in recording_processes:
            recording_processes[camera_id] = None
        
        # 状態を更新
        recording_status[camera_id] = 0  # 未録画状態
        
        return result

def check_recording_duration(camera_id):
    """
    録画時間をチェックし、必要に応じて録画を再開する

    Args:
        camera_id (str): チェックするカメラID
    """
    while True:
        try:
            # プロセス情報の確認
            if camera_id not in recording_processes or not recording_processes[camera_id]:
                logging.info(f"Recording process for camera {camera_id} no longer exists. Stopping monitor thread.")
                break

            # 録画開始時間の確認
            current_time = datetime.now()
            start_time = recording_start_times.get(camera_id)

            if not start_time:
                logging.warning(f"No start time found for camera {camera_id}")
                time.sleep(10)  # 短縮して頻繁にチェック
                continue

            duration = current_time - start_time
            duration_seconds = duration.total_seconds()

            # 設定された時間経過で録画を再開
            max_duration = config.MAX_RECORDING_HOURS * 3600  # 時間を秒に変換
            if duration_seconds >= max_duration:
                # 再起動操作はロックの外で行う
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config:
                    logging.info(f"Restarting recording for camera {camera_id} due to duration limit")
                    restart_recording(camera_id, camera_config['rtsp_url'])
                else:
                    logging.error(f"Camera configuration not found for camera {camera_id}")

        except Exception as e:
            logging.error(f"Error in check_recording_duration for camera {camera_id}: {e}")

        time.sleep(10)  # より頻繁なチェック間隔

def restart_recording(camera_id, rtsp_url):
    """
    特定カメラの録画を再起動する

    Args:
        camera_id (str): 再起動するカメラID
        rtsp_url (str): RTSP URL
        
    Returns:
        bool: 操作が成功したかどうか
    """
    # カメラのロックがまだ存在しない場合は作成
    if camera_id not in recording_locks:
        recording_locks[camera_id] = threading.Lock()
    
    # ロックを取得してプロセス操作
    with recording_locks[camera_id]:
        try:
            logging.info(f"Restarting recording for camera {camera_id}")
            
            # 現在のプロセスを停止
            _stop_recording_process(camera_id)
            
            # プロセス情報をクリア
            if camera_id in recording_processes:
                recording_processes[camera_id] = None
            
            # 最終録画試行時間をリセットして強制的に再接続
            if camera_id in last_recording_attempt:
                del last_recording_attempt[camera_id]
            
            # 録画を再開
            time.sleep(2)  # 少し待機
            result = start_new_recording(camera_id, rtsp_url)
            
            if result:
                recording_status[camera_id] = 1  # 録画中状態
                logging.info(f"Successfully restarted recording for camera {camera_id}")
            else:
                recording_status[camera_id] = 3  # エラー状態
                logging.error(f"Failed to restart recording for camera {camera_id}")
            
            return result
            
        except Exception as e:
            logging.error(f"Error restarting recording for camera {camera_id}: {e}")
            recording_status[camera_id] = 3  # エラー状態
            return False

def monitor_recording_processes():
    """
    すべての録画プロセスを監視し、必要に応じて再起動する
    """
    while True:
        try:
            cameras = camera_utils.read_config()
            for camera in cameras:
                camera_id = camera['id']
                
                # カメラが録画対象でなければスキップ
                if camera_id not in recording_processes or not recording_processes[camera_id]:
                    continue
                
                # プロセス情報の取得
                recording_info = recording_processes[camera_id]
                if not recording_info:
                    continue
                    
                process = recording_info.get('process')
                if not process:
                    continue
                
                # プロセスの状態確認（ロックなし）
                if process.poll() is not None:  # プロセスが終了している場合
                    logging.warning(f"Recording process for camera {camera_id} has died. Restarting...")
                    
                    # 録画を再開（内部でロック取得）
                    try:
                        restart_recording(camera_id, camera['rtsp_url'])
                    except Exception as e:
                        logging.error(f"Failed to restart recording for camera {camera_id}: {e}")

        except Exception as e:
            logging.error(f"Error in monitor_recording_processes: {e}")

        time.sleep(30)  # 30秒ごとにチェック

def initialize_recording():
    """
    録画システムの初期化
    """
    # グローバル変数の初期化
    global recording_processes, recording_threads, recording_start_times, recording_locks, recording_status, last_recording_attempt
    
    recording_processes = {}
    recording_threads = {}
    recording_start_times = {}
    recording_locks = {}
    recording_status = {}
    last_recording_attempt = {}
    
    # 起動時に残っているffmpegプロセスをクリーンアップ
    ffmpeg_utils.kill_ffmpeg_processes()
    
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
                # 録画開始（内部でロック取得）
                result = start_recording(camera['id'], camera['rtsp_url'])
                if not result:
                    success = False
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
            # 録画停止（内部でロック取得）
            result = stop_recording(camera_id)
            if not result:
                success = False
        except Exception as e:
            logging.error(f"Failed to stop recording for camera {camera_id}: {e}")
            success = False

    return success
