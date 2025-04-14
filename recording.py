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

def start_recording(camera_id, rtsp_url):
    """
    録画を開始する関数（改善版）

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        # 既存のプロセスが存在する場合は終了
        if camera_id in recording_processes:
            logging.info(f"既存の録画プロセスを停止: {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)  # 確実にプロセスが終了するのを待つ

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
            
            # 古いファイルを削除して空き容量を確保
            try:
                cleanup_old_recordings(camera_id)
                # 再度空き容量をチェック
                available_space = fs_utils.get_free_space(camera_dir)
                if available_space < required_space:
                    raise Exception(error_msg)
                else:
                    logging.info(f"クリーンアップ後の空き容量: {available_space / (1024*1024*1024):.2f} GB")
            except Exception as cleanup_err:
                logging.error(f"クリーンアップエラー: {cleanup_err}")
                raise Exception(error_msg)

        # RTSPストリームの接続確認を行う
        logging.info(f"RTSPストリームの接続確認: {rtsp_url}")
        success, error_msg = camera_utils.test_rtsp_connection(rtsp_url, timeout=5)
        if not success:
            logging.error(f"RTSPストリーム接続失敗: {error_msg}")
            # 接続失敗でもとりあえず録画を試みる

        # 新しい録画を開始
        start_new_recording(camera_id, rtsp_url)
        return True

    except Exception as e:
        error_msg = f"Error starting recording for camera {camera_id}: {e}"
        logging.error(error_msg)
        raise Exception(error_msg)

def start_new_recording(camera_id, rtsp_url):
    """
    新しい録画プロセスを開始する（改善版）

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL
    """
    try:
        logging.info(f"Starting new recording for camera {camera_id} with URL {rtsp_url}")

        # 音声ストリームの確認を追加
        has_audio = ffmpeg_utils.check_audio_stream(rtsp_url)
        if not has_audio:
            logging.warning(f"Camera {camera_id} may not have audio capability or audio stream is not available")

        # 既存の録画を停止
        if camera_id in recording_processes:
            logging.info(f"Stopping existing recording for camera {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)

        # 録画ファイルパスの生成
        file_path = fs_utils.get_record_file_path(config.RECORD_PATH, camera_id)
        logging.info(f"Recording will be saved to: {file_path}")

        # 録画保存ディレクトリのチェック
        save_dir = os.path.dirname(file_path)
        if not os.path.exists(save_dir):
            fs_utils.ensure_directory_exists(save_dir)

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

        logging.info(f"Recording process started with PID {process.pid}")

        # エラー出力を監視するスレッド
        error_thread = threading.Thread(
            target=ffmpeg_utils.monitor_ffmpeg_output, 
            args=(process,), 
            daemon=True
        )
        error_thread.start()

        # 録画時間監視スレッドの開始または再起動
        if camera_id in recording_threads and recording_threads[camera_id].is_alive():
            logging.info(f"録画監視スレッドが実行中です: {camera_id}")
        else:
            monitor_thread = threading.Thread(target=check_recording_duration, args=(camera_id,), daemon=True)
            monitor_thread.start()
            recording_threads[camera_id] = monitor_thread
            logging.info(f"録画監視スレッドを開始しました: {camera_id}")

        # プロセスの状態を確認
        time.sleep(2)  # プロセスの起動を待つ

        if process.poll() is not None:
            return_code = process.poll()
            error_output = process.stderr.read().decode('utf-8', errors='replace') if process.stderr else "No error output"
            logging.error(f"FFmpeg process failed to start. Return code: {return_code}")
            logging.error(f"FFmpeg error output: {error_output}")
            raise Exception(f"FFmpeg failed to start: {error_output}")

    except Exception as e:
        logging.error(f"Error starting new recording for camera {camera_id}: {e}")
        logging.exception("Full stack trace:")
        raise

def stop_recording(camera_id):
    """
    録画を停止する関数（改善版）

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
                    logging.warning("Recording file is empty - deleting")
                    try:
                        os.remove(file_path)
                    except Exception as del_err:
                        logging.error(f"Error deleting empty file: {del_err}")
            else:
                logging.error(f"Recording file not found: {file_path}")

            return True

        except Exception as e:
            logging.error(f"Error in stop_recording: {e}")
            logging.exception("Full stack trace:")
            return False
    else:
        logging.warning(f"No recording process found for camera {camera_id}")
        return False

def check_recording_duration(camera_id):
    """
    録画時間をチェックし、必要に応じて録画を再開する（改善版）

    Args:
        camera_id (str): チェックするカメラID
    """
    check_interval = 30  # 秒単位でチェック間隔を設定
    
    while True:
        try:
            if camera_id not in recording_processes:
                logging.info(f"Recording process for camera {camera_id} no longer exists. Stopping monitor thread.")
                break

            current_time = datetime.now()
            start_time = recording_start_times.get(camera_id)

            if not start_time:
                logging.warning(f"No start time found for camera {camera_id}")
                time.sleep(check_interval)
                continue

            duration = current_time - start_time
            duration_seconds = duration.total_seconds()

            # 設定された時間経過で録画を再開
            max_duration = config.MAX_RECORDING_HOURS * 3600  # 時間を秒に変換
            if duration_seconds >= max_duration:
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config:
                    logging.info(f"Restarting recording for camera {camera_id} due to duration limit")
                    
                    # 現在の録画を停止
                    old_recording_info = recording_processes.get(camera_id)
                    old_file_path = old_recording_info['file_path'] if old_recording_info else None
                    
                    stop_recording(camera_id)
                    time.sleep(2)
                    
                    # 新しい録画を開始
                    start_new_recording(camera_id, camera_config['rtsp_url'])
                    
                    # 古いファイルをバックアップ
                    if old_file_path and os.path.exists(old_file_path):
                        try:
                            backup_file(old_file_path, camera_id)
                        except Exception as backup_err:
                            logging.error(f"Error backing up file: {backup_err}")
                else:
                    logging.error(f"Camera configuration not found for camera {camera_id}")
            else:
                # 残り時間をログに記録（10分単位）
                remaining_minutes = int((max_duration - duration_seconds) / 60)
                if remaining_minutes % 10 == 0 and duration_seconds % 600 < check_interval:
                    logging.info(f"Camera {camera_id} recording time: {int(duration_seconds/60)} min, {remaining_minutes} min remaining")
                
                # プロセスの状態を確認
                recording_info = recording_processes.get(camera_id)
                if recording_info and recording_info['process']:
                    process = recording_info['process']
                    if process.poll() is not None:
                        logging.warning(f"録画プロセスが終了しています: Camera {camera_id}, Return code: {process.poll()}")
                        
                        # カメラ設定を取得して録画を再開
                        camera_config = camera_utils.get_camera_by_id(camera_id)
                        if camera_config:
                            logging.info(f"録画プロセスを再開します: Camera {camera_id}")
                            stop_recording(camera_id)  # クリーンアップのため
                            time.sleep(2)
                            start_new_recording(camera_id, camera_config['rtsp_url'])
                        else:
                            logging.error(f"Camera configuration not found for camera {camera_id}")
                            break

        except Exception as e:
            logging.error(f"Error in check_recording_duration for camera {camera_id}: {e}")

        time.sleep(check_interval)

def backup_file(file_path, camera_id):
    """
    録画ファイルをバックアップする

    Args:
        file_path (str): バックアップするファイルパス
        camera_id (str): カメラID

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        if not os.path.exists(file_path):
            logging.warning(f"Backup source file does not exist: {file_path}")
            return False
            
        # ファイル名から日時部分を抽出
        file_name = os.path.basename(file_path)
        backup_dir = os.path.join(config.BACKUP_PATH, camera_id)
        
        # バックアップディレクトリを作成
        fs_utils.ensure_directory_exists(backup_dir)
        
        # バックアップファイルパスを生成
        backup_path = os.path.join(backup_dir, file_name)
        
        # ファイルコピー
        if fs_utils.copy_file(file_path, backup_path, overwrite=True):
            logging.info(f"Successfully backed up file to {backup_path}")
            return True
        else:
            return False
            
    except Exception as e:
        logging.error(f"Error backing up file {file_path}: {e}")
        return False

def cleanup_old_recordings(camera_id):
    """
    古い録画ファイルを削除する

    Args:
        camera_id (str): カメラID

    Returns:
        int: 削除したファイル数
    """
    try:
        camera_dir = os.path.join(config.RECORD_PATH, camera_id)
        if not os.path.exists(camera_dir):
            return 0
            
        # MP4ファイルのリストを取得
        mp4_files = []
        for file_name in os.listdir(camera_dir):
            if file_name.endswith('.mp4'):
                file_path = os.path.join(camera_dir, file_name)
                file_time = os.path.getmtime(file_path)
                file_size = os.path.getsize(file_path)
                mp4_files.append((file_path, file_time, file_size))
                
        # 日付でソート（古い順）
        mp4_files.sort(key=lambda x: x[1])
        
        # 最大ファイル数を超える場合、古いファイルから削除
        deleted_count = 0
        if len(mp4_files) > config.MAX_RECORDINGS_PER_CAMERA:
            for file_path, _, _ in mp4_files[:-config.MAX_RECORDINGS_PER_CAMERA]:
                try:
                    os.remove(file_path)
                    logging.info(f"Removed old recording: {file_path}")
                    deleted_count += 1
                except OSError as e:
                    logging.error(f"Error removing old recording {file_path}: {e}")
                    
        return deleted_count
        
    except Exception as e:
        logging.error(f"Error cleaning up old recordings for camera {camera_id}: {e}")
        return 0

def monitor_recording_processes():
    """
    すべての録画プロセスを監視し、必要に応じて再起動する（改善版）
    """
    while True:
        try:
            cameras = camera_utils.read_config()
            for camera in cameras:
                camera_id = camera['id']
                if camera_id in recording_processes:
                    recording_info = recording_processes[camera_id]
                    process = recording_info['process']

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

        except Exception as e:
            logging.error(f"Error in monitor_recording_processes: {e}")

        time.sleep(30)  # 30秒ごとにチェック

def initialize_recording():
    """
    録画システムの初期化（改善版）
    """
    # 監視スレッドの起動
    monitor_thread = threading.Thread(target=monitor_recording_processes, daemon=True)
    monitor_thread.start()
    logging.info("Started recording process monitor thread")
    
    # 録画ディレクトリの確認
    if not os.path.exists(config.RECORD_PATH):
        fs_utils.ensure_directory_exists(config.RECORD_PATH)
    
    # バックアップディレクトリの確認
    if not os.path.exists(config.BACKUP_PATH):
        fs_utils.ensure_directory_exists(config.BACKUP_PATH)

def start_all_recordings():
    """
    すべてのカメラの録画を開始（改善版）

    Returns:
        bool: 操作が成功したかどうか
    """
    success = True
    cameras = camera_utils.read_config()
    
    if not cameras:
        logging.warning("No cameras configured for recording")
        return False
        
    started_count = 0
    failed_count = 0
    
    for camera in cameras:
        try:
            if camera['rtsp_url']:
                start_recording(camera['id'], camera['rtsp_url'])
                started_count += 1

        except Exception as e:
            logging.error(f"Failed to start recording for camera {camera['id']}: {e}")
            failed_count += 1
            success = False

    logging.info(f"Recording started for {started_count} cameras, failed for {failed_count} cameras")
    return success

def stop_all_recordings():
    """
    すべてのカメラの録画を停止（改善版）
    
    Returns:
        bool: 操作が成功したかどうか
    """
    success = True
    camera_ids = list(recording_processes.keys())
    
    if not camera_ids:
        logging.info("No active recordings to stop")
        return True
        
    stopped_count = 0
    failed_count = 0
    
    for camera_id in camera_ids:
        try:
            if stop_recording(camera_id):
                stopped_count += 1
            else:
                failed_count += 1
                success = False

        except Exception as e:
            logging.error(f"Failed to stop recording for camera {camera_id}: {e}")
            failed_count += 1
            success = False

    logging.info(f"Recording stopped for {stopped_count} cameras, failed for {failed_count} cameras")
    return success
