"""
録画管理モジュール
録画プロセスの管理機能を提供します
"""
import os
import logging
import threading
import time
import queue
from datetime import datetime
import subprocess
import random
import glob

import config
import ffmpeg_utils
import fs_utils
import camera_utils

# グローバル変数
recording_processes = {}
recording_threads = {}
recording_start_times = {}  # 録画開始時刻を保持する辞書
recording_health_checks = {}  # 録画プロセスのヘルスチェック情報
recording_task_queue = queue.Queue()  # 録画タスクのキュー
MAX_CONCURRENT_RECORDINGS = config.MAX_CONCURRENT_STREAMS  # 同時録画数の制限

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

        # 全てのカメラでHLSストリームを使用
        logging.info(f"カメラ{camera_id}にHLS録画処理を適用します: {rtsp_url}")
        
        # HLSストリームのURLとセグメントファイルの存在を確認
        hls_url = f"http://localhost:5000/system/cam/tmp/{camera_id}/{camera_id}.m3u8"
        hls_segment_pattern = os.path.join(config.TMP_PATH, camera_id, f"{camera_id}_*.ts")
        
        # セグメントファイルの存在を確認（少なくとも1つのセグメントが必要）
        segment_files = glob.glob(hls_segment_pattern)
        if not segment_files:
            error_msg = f"HLSストリームのセグメントファイルが見つかりません: {hls_segment_pattern}"
            logging.error(error_msg)
            # 再試行タスクをキューに追加（10秒後）
            recording_task_queue.put({
                'action': 'retry_recording',
                'camera_id': camera_id,
                'rtsp_url': rtsp_url,
                'retry_count': 0,
                'next_retry': time.time() + 10
            })
            raise Exception(error_msg)
        
        logging.info(f"カメラ{camera_id}: HLSストリームが利用可能、{len(segment_files)}個のセグメントファイルを確認")
        has_audio = True  # 音声ありと仮定

        # 既存の録画を停止
        if camera_id in recording_processes:
            logging.info(f"Stopping existing recording for camera {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)

        # 録画ファイルパスの生成
        file_path = fs_utils.get_record_file_path(config.RECORD_PATH, camera_id)
        logging.info(f"Recording will be saved to: {file_path}")

        # FFmpegコマンドを生成
        ffmpeg_command = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, file_path, camera_id)
        logging.info(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

        # プロセスを開始
        process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command)

        # プロセス情報を保存
        recording_processes[camera_id] = {
            'process': process,
            'file_path': file_path,
            'rtsp_url': rtsp_url
        }
        recording_start_times[camera_id] = datetime.now()
        recording_health_checks[camera_id] = {
            'last_check': time.time(),
            'status': 'starting',
            'error_count': 0
        }

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
            error_output = process.stderr.read().decode('utf-8', errors='replace')
            logging.error(f"FFmpeg process failed to start. Return code: {return_code}")
            logging.error(f"FFmpeg error output: {error_output}")
            
            # 起動失敗時はキューに再試行タスクを追加
            recording_task_queue.put({
                'action': 'retry_recording',
                'camera_id': camera_id,
                'rtsp_url': rtsp_url,
                'retry_count': 0,
                'next_retry': time.time() + 10  # 10秒後に再試行
            })
            
            raise Exception(f"FFmpeg failed to start: {error_output}")

    except Exception as e:
        logging.error(f"Error starting new recording for camera {camera_id}: {e}")
        logging.exception("Full stack trace:")
        
        # すでにキューに追加していない場合のみ追加
        if 'Cannot connect to RTSP stream' not in str(e) and 'FFmpeg failed to start' not in str(e) and 'HLSストリームのセグメント' not in str(e):
            recording_task_queue.put({
                'action': 'retry_recording',
                'camera_id': camera_id,
                'rtsp_url': rtsp_url,
                'retry_count': 0,
                'next_retry': time.time() + 20  # 20秒後に再試行
            })
        
        raise

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
    
    if camera_id in recording_health_checks:
        del recording_health_checks[camera_id]

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

            # ヘルスチェック情報の更新
            recording_health_checks[camera_id]['last_check'] = time.time()
            
            # プロセスの状態確認
            recording_info = recording_processes.get(camera_id)
            if recording_info and recording_info['process'].poll() is not None:
                logging.warning(f"Recording process for camera {camera_id} has died. Will be restarted by monitor.")
                recording_health_checks[camera_id]['status'] = 'failed'
                recording_health_checks[camera_id]['error_count'] += 1
                break  # モニタリングスレッドを終了（メインモニターが再起動）

            # 設定された時間経過で録画を再開
            max_duration = config.MAX_RECORDING_HOURS * 3600  # 時間を秒に変換
            if duration_seconds >= max_duration:
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config:
                    logging.info(f"Restarting recording for camera {camera_id} due to duration limit")
                    
                    # キューにローテーションタスクを追加
                    recording_task_queue.put({
                        'action': 'rotate_recording',
                        'camera_id': camera_id,
                        'rtsp_url': camera_config['rtsp_url']
                    })
                    break  # モニタリングスレッドを終了（キューが新しいスレッドを作成）
                else:
                    logging.error(f"Camera configuration not found for camera {camera_id}")

        except Exception as e:
            logging.error(f"Error in check_recording_duration for camera {camera_id}: {e}")

        time.sleep(10)  # より頻繁なチェック間隔

def process_recording_tasks():
    """
    録画タスクキューを処理するスレッド関数
    """
    while True:
        try:
            # 現在の録画数をチェック - 同時録画数制限を超えないようにする
            if len(recording_processes) >= MAX_CONCURRENT_RECORDINGS:
                time.sleep(5)  # 容量に余裕ができるまで待機
                continue
                
            # キューからタスクを取得（1秒タイムアウト）
            try:
                task = recording_task_queue.get(timeout=1)
            except queue.Empty:
                continue  # タスクがなければ次のループへ
                
            # 再試行タスクの場合、次の再試行時間を確認
            if task['action'] == 'retry_recording' and task.get('next_retry', 0) > time.time():
                # まだ再試行時間になっていない場合は、タスクを再度キューに入れる
                recording_task_queue.put(task)
                time.sleep(1)
                continue
                
            # タスク処理
            if task['action'] == 'retry_recording':
                camera_id = task['camera_id']
                rtsp_url = task['rtsp_url']
                retry_count = task['retry_count']
                
                if retry_count >= config.RETRY_ATTEMPTS:
                    logging.warning(f"Maximum retry attempts reached for camera {camera_id}. Giving up.")
                    continue
                    
                # 再試行間隔を徐々に増やす（バックオフ）
                retry_delay = min(config.RETRY_DELAY * (retry_count + 1), config.MAX_RETRY_DELAY)
                
                logging.info(f"Retrying recording for camera {camera_id} (attempt {retry_count + 1}/{config.RETRY_ATTEMPTS})")
                
                try:
                    # 既存のプロセスを確認して終了
                    if camera_id in recording_processes:
                        stop_recording(camera_id)
                        
                    # 録画再開
                    start_new_recording(camera_id, rtsp_url)
                    logging.info(f"Successfully restarted recording for camera {camera_id} after {retry_count + 1} attempts")
                    
                except Exception as e:
                    logging.error(f"Failed to restart recording for camera {camera_id}: {e}")
                    
                    # 次の再試行をスケジュール
                    recording_task_queue.put({
                        'action': 'retry_recording',
                        'camera_id': camera_id,
                        'rtsp_url': rtsp_url,
                        'retry_count': retry_count + 1,
                        'next_retry': time.time() + retry_delay
                    })
                    
            elif task['action'] == 'rotate_recording':
                camera_id = task['camera_id']
                rtsp_url = task['rtsp_url']
                
                logging.info(f"Rotating recording for camera {camera_id}")
                
                try:
                    # 既存の録画を停止
                    stop_recording(camera_id)
                    time.sleep(2)
                    
                    # 新しい録画を開始
                    start_new_recording(camera_id, rtsp_url)
                    logging.info(f"Successfully rotated recording for camera {camera_id}")
                    
                except Exception as e:
                    logging.error(f"Failed to rotate recording for camera {camera_id}: {e}")
                    
                    # 再試行タスクを追加
                    recording_task_queue.put({
                        'action': 'retry_recording',
                        'camera_id': camera_id,
                        'rtsp_url': rtsp_url,
                        'retry_count': 0,
                        'next_retry': time.time() + config.RETRY_DELAY
                    })
                    
        except Exception as e:
            logging.error(f"Error in process_recording_tasks: {e}")
            time.sleep(5)  # エラー発生時は少し待機
            
def check_recording_health():
    """
    すべての録画プロセスのヘルスを定期的にチェックする
    """
    while True:
        try:
            time.sleep(config.HEALTH_CHECK_INTERVAL)
            
            current_time = time.time()
            cameras_to_check = list(recording_health_checks.keys())
            
            for camera_id in cameras_to_check:
                health_info = recording_health_checks.get(camera_id)
                
                if not health_info:
                    continue
                    
                # 最後のチェックから一定時間経過したプロセスを確認
                if current_time - health_info['last_check'] > config.HEALTH_CHECK_INTERVAL * 2:
                    logging.warning(f"Recording health check timeout for camera {camera_id}")
                    
                    if camera_id in recording_processes:
                        recording_info = recording_processes[camera_id]
                        
                        # プロセスが終了していないかチェック
                        if recording_info['process'].poll() is None:
                            # まだ実行中であれば、ヘルスチェック情報を更新
                            recording_health_checks[camera_id]['last_check'] = current_time
                        else:
                            # プロセスが終了していれば再起動タスクを追加
                            logging.error(f"Recording process for camera {camera_id} has died. Scheduling restart.")
                            recording_task_queue.put({
                                'action': 'retry_recording',
                                'camera_id': camera_id,
                                'rtsp_url': recording_info['rtsp_url'],
                                'retry_count': 0,
                                'next_retry': time.time() + 5  # 5秒後に再試行
                            })
                            
                            # メモリリークを防ぐため、古いプロセス情報をクリーンアップ
                            stop_recording(camera_id)
                
        except Exception as e:
            logging.error(f"Error in check_recording_health: {e}")

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

                    # プロセスの状態を確認
                    if process.poll() is not None:  # プロセスが終了している場合
                        logging.warning(f"Recording process for camera {camera_id} has died. Restarting...")

                        # 録画を再開するタスクをキューに追加
                        recording_task_queue.put({
                            'action': 'retry_recording',
                            'camera_id': camera_id,
                            'rtsp_url': camera['rtsp_url'],
                            'retry_count': 0,
                            'next_retry': time.time() + 5  # 5秒後に再試行
                        })
                        
                        # 古いプロセス情報をクリーンアップ
                        stop_recording(camera_id)
            
            # システムリソースをチェック
            check_system_resources()

        except Exception as e:
            logging.error(f"Error in monitor_recording_processes: {e}")

        time.sleep(30)  # 30秒ごとにチェック

def check_system_resources():
    """
    システムリソースをチェックし、必要に応じて録画を一時停止
    """
    import psutil
    
    try:
        # CPU使用率をチェック
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # メモリ使用率をチェック
        mem_percent = psutil.virtual_memory().percent
        
        logging.info(f"System resources: CPU {cpu_percent}%, Memory {mem_percent}%")
        
        # リソース使用量が閾値を超えた場合
        if cpu_percent > config.MAX_CPU_PERCENT or mem_percent > config.MAX_MEM_PERCENT:
            logging.warning(f"System resources critical: CPU {cpu_percent}%, Memory {mem_percent}%")
            
            # アクティブな録画プロセスが多い場合、一部を一時停止
            if len(recording_processes) > 10:  # 最低10台は維持
                # 録画時間が長いものから一時停止
                cameras_by_duration = sorted(
                    recording_processes.keys(),
                    key=lambda c: (datetime.now() - recording_start_times.get(c, datetime.now())).total_seconds(),
                    reverse=True
                )
                
                # 最大3台を一時停止
                pause_count = min(3, len(cameras_by_duration) // 3)
                for i in range(pause_count):
                    camera_id = cameras_by_duration[i]
                    logging.warning(f"Temporarily pausing camera {camera_id} due to system resource constraints")
                    
                    # 録画情報を保存
                    recording_info = recording_processes[camera_id]
                    rtsp_url = recording_info['rtsp_url']
                    
                    # 録画を停止
                    stop_recording(camera_id)
                    
                    # 5分後に再開するタスクをキューに追加
                    recording_task_queue.put({
                        'action': 'retry_recording',
                        'camera_id': camera_id,
                        'rtsp_url': rtsp_url,
                        'retry_count': 0,
                        'next_retry': time.time() + 300  # 5分後
                    })
    
    except Exception as e:
        logging.error(f"Error checking system resources: {e}")

def initialize_recording():
    """
    録画システムの初期化
    """
    # 録画タスク処理スレッドの起動
    task_thread = threading.Thread(target=process_recording_tasks, daemon=True)
    task_thread.start()
    logging.info("Started recording task processor thread")
    
    # ヘルスチェックスレッドの起動
    health_thread = threading.Thread(target=check_recording_health, daemon=True)
    health_thread.start()
    logging.info("Started recording health check thread")
    
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
    
    # 録画開始のディレイをランダム化（一度に大量のFFmpegプロセスが起動するのを防ぐ）
    cameras_to_start = []
    for camera in cameras:
        if camera['rtsp_url']:
            cameras_to_start.append(camera)
    
    # 録画開始と録画状況の初期化
    for i, camera in enumerate(cameras_to_start):
        try:
            # 同時起動数の制限と、起動タイミングをずらす
            if i > 0 and i % 5 == 0:  # 5カメラずつ起動
                time.sleep(5)
                
            if camera['rtsp_url']:
                # 録画を開始するタスクをキューに追加
                recording_task_queue.put({
                    'action': 'retry_recording',
                    'camera_id': camera['id'],
                    'rtsp_url': camera['rtsp_url'],
                    'retry_count': 0,
                    'next_retry': time.time() + (i * 2)  # カメラごとに起動タイミングをずらす
                })

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