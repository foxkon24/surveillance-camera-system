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
import json
import shutil
import random

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
# 各カメラの最終録画再起動時間
last_recording_restart = {}
# 録画再試行までの間隔（秒）
RECORDING_RETRY_DELAY = 30
# 最小再起動間隔（秒）
MIN_RESTART_INTERVAL = 60
# 録画ファイルの整合性チェック間隔
INTEGRITY_CHECK_INTERVAL = 300  # 5分
# 最後の整合性チェック時間
last_integrity_check = time.time()
# 録画ファイルのローテーション間隔（秒）
ROTATION_INTERVAL = 3600  # 1時間
# 各カメラの最終チェック時間
camera_last_check = {}
# 最大エラー回数（これを超えるとより長い待機時間になる）
MAX_ERROR_COUNT = 3
# 録画エラー回数
recording_error_counts = {}

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
        error_count = recording_error_counts.get(camera_id, 0)
        
        # エラー回数に応じて遅延を計算
        retry_delay = RECORDING_RETRY_DELAY
        if error_count > 0:
            retry_delay = min(RECORDING_RETRY_DELAY * error_count, 300)  # 最大5分
            
        if time_since_last_attempt < retry_delay:
            # エラー状態になっているカメラは接続しない
            if camera_id in recording_status and recording_status[camera_id] == 3:
                logging.warning(f"Camera {camera_id} recording is in error state. Waiting for retry delay to expire ({int(retry_delay - time_since_last_attempt)}s remaining).")
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

            # ディスク容量チェック
            required_space = 1024 * 1024 * 1024 * config.MIN_DISK_SPACE_GB
            available_space = fs_utils.get_free_space(camera_dir)

            if available_space < required_space:
                error_msg = f"Insufficient disk space for camera {camera_id}. " \
                            f"Available: {available_space / (1024*1024*1024):.2f} GB, " \
                            f"Required: {config.MIN_DISK_SPACE_GB} GB"
                logging.error(error_msg)
                
                # 古い録画ファイルを自動削除して空き容量を確保
                deleted_count = fs_utils.cleanup_directory(
                    camera_dir, 
                    file_pattern='.mp4', 
                    max_files=config.MAX_RECORD_FILES // 2  # 半分を残して削除
                )
                
                if deleted_count > 0:
                    logging.info(f"Deleted {deleted_count} old recording files to free up space")
                    
                    # 再度容量をチェック
                    available_space = fs_utils.get_free_space(camera_dir)
                    if available_space < required_space:
                        recording_status[camera_id] = 3  # エラー状態
                        raise Exception(error_msg)
                else:
                    recording_status[camera_id] = 3  # エラー状態
                    raise Exception(error_msg)

            # 新しい録画を開始
            success = start_new_recording(camera_id, rtsp_url)
            if success:
                recording_status[camera_id] = 1  # 録画中状態
                recording_error_counts[camera_id] = 0  # エラーカウントをリセット
            else:
                recording_status[camera_id] = 3  # エラー状態
                recording_error_counts[camera_id] = recording_error_counts.get(camera_id, 0) + 1
                
            return success

        except Exception as e:
            error_msg = f"Error starting recording for camera {camera_id}: {e}"
            logging.error(error_msg)
            recording_status[camera_id] = 3  # エラー状態
            recording_error_counts[camera_id] = recording_error_counts.get(camera_id, 0) + 1
            return False

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

        # 古い録画ファイルを管理（最大数を超えたら古いものを削除）
        camera_dir = os.path.join(config.RECORD_PATH, camera_id)
        fs_utils.cleanup_directory(
            camera_dir, 
            file_pattern='.mp4', 
            max_files=config.MAX_RECORD_FILES
        )

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
            'file_path': file_path,
            'rtsp_url': rtsp_url
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
                try:
                    error_output = process.stderr.read().decode('utf-8', errors='replace')
                except:
                    error_output = "Cannot read stderr"
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
        else:
            logging.info(f"Recording process (PID: {process.pid}) has already terminated")
        
        # ファイル存在確認
        if file_path and os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            logging.info(f"Recording file exists. Size: {file_size} bytes")

            if file_size > 10240:  # 10KB以上なら有効とみなす
                finalize_result = ffmpeg_utils.finalize_recording(file_path)
                if finalize_result:
                    logging.info(f"Successfully finalized recording file: {file_path}")
                else:
                    logging.warning(f"Failed to finalize recording file: {file_path}")
            else:
                logging.warning(f"Recording file is too small: {file_path} ({file_size} bytes)")
                
                # 小さすぎるファイルは削除
                try:
                    os.remove(file_path)
                    logging.info(f"Deleted small recording file: {file_path}")
                except Exception as e:
                    logging.error(f"Failed to delete small recording file: {file_path}, Error: {e}")
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
            # 短い間隔で定期的にチェック
            time.sleep(10)
            
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
                # 現在時刻を記録
                now = time.time()
                
                # 最後の再起動から十分な時間が経過しているか確認
                if camera_id in last_recording_restart:
                    time_since_restart = now - last_recording_restart[camera_id]
                    if time_since_restart < MIN_RESTART_INTERVAL:
                        logging.info(f"Too frequent restart attempt for camera {camera_id}, waiting...")
                        continue
                
                # 再起動時間を記録
                last_recording_restart[camera_id] = now
                
                # 再起動操作
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config:
                    logging.info(f"Restarting recording for camera {camera_id} due to duration limit")
                    restart_recording(camera_id, camera_config['rtsp_url'])
                else:
                    logging.error(f"Camera configuration not found for camera {camera_id}")

            # 録画ファイルの整合性チェック（定期的に）
            global last_integrity_check
            now = time.time()
            if now - last_integrity_check > INTEGRITY_CHECK_INTERVAL:
                last_integrity_check = now
                check_recording_integrity(camera_id)

        except Exception as e:
            logging.error(f"Error in check_recording_duration for camera {camera_id}: {e}")

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
                recording_error_counts[camera_id] = recording_error_counts.get(camera_id, 0) + 1
                logging.error(f"Failed to restart recording for camera {camera_id}")
            
            return result
            
        except Exception as e:
            logging.error(f"Error restarting recording for camera {camera_id}: {e}")
            recording_status[camera_id] = 3  # エラー状態
            recording_error_counts[camera_id] = recording_error_counts.get(camera_id, 0) + 1
            return False

def check_recording_integrity(camera_id):
    """
    録画ファイルの整合性をチェック

    Args:
        camera_id (str): チェックするカメラID
    """
    try:
        # 現在の録画ファイルを取得
        if camera_id not in recording_processes or not recording_processes[camera_id]:
            return
            
        recording_info = recording_processes[camera_id]
        file_path = recording_info.get('file_path')
        
        if not file_path or not os.path.exists(file_path):
            return
            
        # ファイルサイズをチェック
        file_size = os.path.getsize(file_path)
        
        # サイズが小さすぎる場合は問題がある可能性
        if file_size < 1024 * 1024:  # 1MB未満
            current_time = datetime.now()
            start_time = recording_start_times.get(camera_id)
            
            if start_time:
                # 録画開始から一定時間経過しているのにファイルサイズが小さい場合
                duration = current_time - start_time
                duration_seconds = duration.total_seconds()
                
                # 3分以上経過してもファイルサイズが小さい場合は再起動
                if duration_seconds > 180:
                    logging.warning(f"Recording file is too small after {duration_seconds:.1f} seconds: {file_path} ({file_size} bytes)")
                    
                    # RTSPURLを取得
                    rtsp_url = recording_info.get('rtsp_url')
                    if rtsp_url:
                        logging.info(f"Restarting recording for camera {camera_id} due to file integrity issue")
                        restart_recording(camera_id, rtsp_url)
        
    except Exception as e:
        logging.error(f"Error checking recording integrity for camera {camera_id}: {e}")

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
                    
                    # 現在時刻を取得
                    now = time.time()
                    
                    # 最後の再起動から十分な時間が経過しているか確認
                    if camera_id in last_recording_restart:
                        time_since_restart = now - last_recording_restart[camera_id]
                        if time_since_restart < MIN_RESTART_INTERVAL:
                            logging.info(f"Too frequent restart for camera {camera_id}, waiting...")
                            continue
                    
                    # 再起動時間を記録
                    last_recording_restart[camera_id] = now
                    
                    # 録画を再開（内部でロック取得）
                    try:
                        rtsp_url = camera['rtsp_url']
                        restart_recording(camera_id, rtsp_url)
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
    global last_recording_restart, recording_error_counts
    
    recording_processes = {}
    recording_threads = {}
    recording_start_times = {}
    recording_locks = {}
    recording_status = {}
    last_recording_attempt = {}
    last_recording_restart = {}
    recording_error_counts = {}
    
    # 起動時に残っているffmpegプロセスをクリーンアップ
    ffmpeg_utils.kill_ffmpeg_processes()
    
    # 監視スレッドの起動
    monitor_thread = threading.Thread(target=monitor_recording_processes, daemon=True)
    monitor_thread.start()
    logging.info("Started recording process monitor thread")
    
    # 録画ディレクトリのチェック
    for camera in camera_utils.read_config():
        camera_id = camera['id']
        camera_dir = os.path.join(config.RECORD_PATH, camera_id)
        fs_utils.ensure_directory_exists(camera_dir)
    
    # 録画ファイルの整合性チェック
    integrity_thread = threading.Thread(target=check_all_recordings_integrity, daemon=True)
    integrity_thread.start()
    logging.info("Started recording integrity check thread")

def check_all_recordings_integrity():
    """
    すべての録画ファイルの整合性をチェックする
    """
    try:
        # 起動直後は少し待機
        time.sleep(60)
        
        while True:
            try:
                # 記録フォルダの検証
                if os.path.exists(config.RECORD_PATH):
                    camera_dirs = os.listdir(config.RECORD_PATH)
                    
                    for camera_id in camera_dirs:
                        camera_path = os.path.join(config.RECORD_PATH, camera_id)
                        
                        if os.path.isdir(camera_path):
                            # 各カメラの録画ファイルをチェック
                            check_camera_recordings(camera_id, camera_path)
                            
                # バックアップフォルダの検証（あれば）
                if os.path.exists(config.BACKUP_PATH):
                    camera_dirs = os.listdir(config.BACKUP_PATH)
                    
                    for camera_id in camera_dirs:
                        camera_path = os.path.join(config.BACKUP_PATH, camera_id)
                        
                        if os.path.isdir(camera_path):
                            # 各カメラのバックアップファイルをチェック
                            check_camera_recordings(camera_id, camera_path, is_backup=True)
                
            except Exception as e:
                logging.error(f"Error in check_all_recordings_integrity: {e}")
                
            # 一定時間待機してから次のチェック
            time.sleep(INTEGRITY_CHECK_INTERVAL)
            
    except Exception as e:
        logging.error(f"Error starting integrity check thread: {e}")

def check_camera_recordings(camera_id, camera_path, is_backup=False):
    """
    特定カメラの録画ファイルをチェック

    Args:
        camera_id (str): カメラID
        camera_path (str): カメラディレクトリのパス
        is_backup (bool): バックアップファイルかどうか
    """
    try:
        # カメラの最終チェック時間を取得
        last_check = camera_last_check.get(camera_id, 0)
        current_time = time.time()
        
        # あまりに頻繁なチェックを避ける
        if current_time - last_check < INTEGRITY_CHECK_INTERVAL / 2:
            return
            
        # チェック時間を更新
        camera_last_check[camera_id] = current_time
        
        # このカメラのMP4ファイルをすべて取得
        mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
        
        # ファイル数が多すぎる場合は古いものを削除
        if len(mp4_files) > config.MAX_RECORD_FILES:
            # 作成日時でソート（古い順）
            mp4_files.sort(key=lambda f: os.path.getctime(os.path.join(camera_path, f)))
            
            # 削除すべきファイル数
            delete_count = len(mp4_files) - config.MAX_RECORD_FILES
            
            # 古いファイルから削除
            for i in range(delete_count):
                try:
                    file_path = os.path.join(camera_path, mp4_files[i])
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logging.info(f"Deleted old recording file: {file_path}")
                except Exception as e:
                    logging.error(f"Error deleting old file {mp4_files[i]}: {e}")
        
        # ファイルサイズの小さいファイルを検出して削除
        for filename in mp4_files:
            try:
                file_path = os.path.join(camera_path, filename)
                
                # ファイルサイズをチェック
                file_size = os.path.getsize(file_path)
                
                # 非常に小さいファイルは壊れている可能性が高い
                if file_size < 10240:  # 10KB未満
                    # 現在録画中ではないファイルのみ削除
                    if not is_recording_file(camera_id, file_path):
                        try:
                            os.remove(file_path)
                            logging.info(f"Deleted small/corrupted recording file: {file_path} ({file_size} bytes)")
                        except Exception as e:
                            logging.error(f"Error deleting small file {file_path}: {e}")
                
                # 大きなファイルは整合性チェック
                elif file_size > 1024 * 1024:  # 1MB以上
                    # このファイルが現在録画中でなければチェック
                    if not is_recording_file(camera_id, file_path):
                        # ランダムに選択（すべてのファイルをチェックすると負荷が高い）
                        if random.random() < 0.2:  # 20%の確率
                            fs_utils.repair_mp4_file(file_path)
            
            except Exception as e:
                logging.error(f"Error checking file {filename}: {e}")
                
    except Exception as e:
        logging.error(f"Error checking recordings for camera {camera_id}: {e}")

def is_recording_file(camera_id, file_path):
    """
    ファイルが現在録画中かどうかをチェック

    Args:
        camera_id (str): カメラID
        file_path (str): ファイルパス

    Returns:
        bool: 現在録画中のファイルであればTrue
    """
    if camera_id not in recording_processes or not recording_processes[camera_id]:
        return False
        
    current_file = recording_processes[camera_id].get('file_path', '')
    return current_file == file_path

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

def export_recording_status(output_path):
    """
    録画状態をJSONファイルにエクスポート

    Args:
        output_path (str): 出力ファイルパス

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        status = {}
        
        # 各カメラの状態を収集
        for camera_id in recording_processes:
            if recording_processes[camera_id]:
                file_path = recording_processes[camera_id].get('file_path', '')
                
                # ファイル情報を取得
                file_info = {}
                if file_path and os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    file_mtime = os.path.getmtime(file_path)
                    
                    file_info = {
                        'path': file_path,
                        'size': file_size,
                        'mtime': file_mtime
                    }
                
                # 開始時間を取得
                start_time = None
                if camera_id in recording_start_times:
                    start_time = recording_start_times[camera_id].timestamp()
                
                # 状態を追加
                status[camera_id] = {
                    'status': recording_status.get(camera_id, 0),
                    'start_time': start_time,
                    'duration': (time.time() - start_time) if start_time else 0,
                    'file': file_info,
                    'error_count': recording_error_counts.get(camera_id, 0)
                }
        
        # JSONファイルに書き込み
        with open(output_path, 'w', encoding='utf-8') as file:
            json.dump({
                'recordings': status,
                'timestamp': time.time(),
                'version': config.VERSION
            }, file, indent=2)
            
        logging.info(f"Recording status exported to: {output_path}")
        return True
        
    except Exception as e:
        logging.error(f"Error exporting recording status: {e}")
        return False
