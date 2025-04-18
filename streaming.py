"""
ストリーミング管理モジュール
HLSストリーミングプロセスの管理機能を提供します
"""
import os
import logging
import threading
import time
from datetime import datetime
import subprocess
import random
import json
import shutil
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
HEALTH_CHECK_INTERVAL = 5  # より頻繁に確認するために短縮
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 15  # タイムアウト値を最適化
# カメラの接続状態を追跡
camera_connection_status = {}  # 0=未接続, 1=接続中, 2=エラー
# カメラごとの最後の接続試行時間
last_connection_attempt = {}
# エラー回数を追跡
connection_error_counts = {}
# 最大エラー回数（これを超えるとより長い待機時間になる）
MAX_ERROR_COUNT = 5
# 再試行遅延設定
MIN_RETRY_DELAY = 10  # 最小再試行遅延（秒）を短縮して頻繁に試行
MAX_RETRY_DELAY = 60  # 最大再試行遅延（秒）を短縮
# 適応的バックオフのための基本係数
BACKOFF_FACTOR = 1.2  # 緩やかに増加させる
# HLSファイルチェックの最大試行回数
MAX_HLS_CHECK_ATTEMPTS = 20  # 増加
# ストリーミング開始時の初期化待機時間（秒）
STREAM_INIT_WAIT = 3  # 短縮
# プロセスの復元力を高めるためのリトライロック
retry_locks = {}
# 各カメラの最終再起動時間を追跡
last_restart_time = {}
# 最小再起動間隔（秒）- 頻繁すぎる再起動を防ぐ
MIN_RESTART_INTERVAL = 15  # 短縮
# クリーンアップログの間隔（秒）
CLEANUP_LOG_INTERVAL = 300  # 5分
last_cleanup_log_time = time.time()
# 一時ディレクトリの存在を確認済みかフラグ
tmp_dir_checked = {}

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    camera_id = camera['id']
    
    # カメラIDのリトライロックを作成
    if camera_id not in retry_locks:
        retry_locks[camera_id] = threading.Lock()
    
    # ロックを取得して重複処理を防止
    with retry_locks[camera_id]:
        # 最終接続試行からの経過時間をチェック
        current_time = time.time()
        if camera_id in last_connection_attempt:
            time_since_last_attempt = current_time - last_connection_attempt[camera_id]
            error_count = connection_error_counts.get(camera_id, 0)
            
            # エラー回数に応じた適応的バックオフを計算
            if error_count > 0:
                # 基本遅延 × (エラー回数 ^ BACKOFF_FACTOR)
                retry_delay = min(MIN_RETRY_DELAY * (error_count ** BACKOFF_FACTOR), MAX_RETRY_DELAY)
            else:
                retry_delay = 0
                
            # 前回の試行から十分な時間が経過していない場合はスキップ
            if time_since_last_attempt < retry_delay:
                logging.info(f"Skipping connection attempt for camera {camera_id} - waiting for retry delay ({int(retry_delay - time_since_last_attempt)}s remaining)")
                return False
            
            # 最後の再起動から十分な時間が経過していない場合はスキップ
            if camera_id in last_restart_time:
                time_since_restart = current_time - last_restart_time[camera_id]
                if time_since_restart < MIN_RESTART_INTERVAL:
                    logging.info(f"Skipping restart of camera {camera_id} - too frequent restart attempt ({int(MIN_RESTART_INTERVAL - time_since_restart)}s remaining)")
                    return False
        
        # 最終接続試行時間を記録
        last_connection_attempt[camera_id] = current_time
        
        try:
            # 既存のプロセスが存在する場合は確認
            if camera_id in streaming_processes and streaming_processes[camera_id]:
                process = streaming_processes[camera_id].get('process')
                if process and process.poll() is None:
                    # プロセスがまだ実行中なら正常性をチェック
                    if check_streaming_process_health(camera_id):
                        logging.info(f"Streaming process for camera {camera_id} is healthy and running")
                        return True
                    else:
                        # プロセスは実行中だが健全でない場合は再起動
                        logging.warning(f"Streaming process for camera {camera_id} is running but unhealthy, restarting")
                        stop_streaming(camera_id)
                        # プロセスが完全に終了するのを待つ
                        time.sleep(2)
                else:
                    # プロセスが終了している場合は再起動
                    logging.info(f"Streaming process for camera {camera_id} is not running, restarting")
                    stop_streaming(camera_id)
                    # プロセスが完全に終了するのを待つ
                    time.sleep(2)

            # 一時ディレクトリの確認と作成
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            fs_utils.ensure_directory_exists(camera_tmp_dir)
            # ディレクトリ存在確認フラグを設定
            tmp_dir_checked[camera_id] = True

            # ファイルパスの設定
            hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
            log_path = os.path.join(camera_tmp_dir, f"{camera_id}.log")
            
            # 既存のm3u8ファイルとtsファイルをクリーンアップ
            cleanup_streaming_files(camera_id)
            
            # 既存のffmpegプロセスが残っている場合、強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            # プロセス終了を待つ
            time.sleep(2)  

            # RTSPストリームの接続確認 - 重要な部分なので複数回試行
            logging.info(f"Checking RTSP connection for camera {camera_id}: {camera['rtsp_url']}")
            connection_successful = False
            connection_attempts = 0
            max_connection_attempts = 3  # 最大試行回数

            while not connection_successful and connection_attempts < max_connection_attempts:
                connection_successful = ffmpeg_utils.check_rtsp_connection(camera['rtsp_url'])
                connection_attempts += 1
                
                if not connection_successful and connection_attempts < max_connection_attempts:
                    logging.warning(f"RTSP connection attempt {connection_attempts} failed for camera {camera_id}, retrying...")
                    time.sleep(2)  # 再試行前に待機
            
            if not connection_successful:
                logging.warning(f"Failed to connect to RTSP stream for camera {camera_id}: {camera['rtsp_url']} after {max_connection_attempts} attempts")
                # 接続エラー回数を増加
                connection_error_counts[camera_id] = connection_error_counts.get(camera_id, 0) + 1
                camera_connection_status[camera_id] = 2  # エラー状態
                return False
            else:
                # 接続成功したらエラーカウントをリセット
                logging.info(f"Successfully connected to RTSP stream for camera {camera_id}")
                connection_error_counts[camera_id] = 0
                camera_connection_status[camera_id] = 1  # 接続中状態

            # ディレクトリと権限の確認
            logging.info(f"Ensuring tmp directory exists and has correct permissions: {camera_tmp_dir}")
            fs_utils.ensure_directory_exists(camera_tmp_dir)
            
            # セグメントパス - この場合はファイル名のみを指定し、相対パスにする
            segment_filename = f"{camera_id}_%03d.ts"
            
            # HLSストリーミング用FFmpegコマンド生成
            # セグメント時間を1秒に短縮、リスト長を10に設定
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
                camera['rtsp_url'], 
                hls_path,
                segment_filename,
                segment_time=1.0,  # セグメント長を短く
                list_size=10       # リスト長を調整
            )
            
            logging.info(f"Starting FFmpeg process with command: {' '.join(ffmpeg_command)}")

            # プロセス起動
            process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command, log_path=log_path)
            
            if process is None:
                logging.error(f"Failed to start FFmpeg process for camera {camera_id}")
                return False
                
            # プロセス情報を保存
            streaming_processes[camera_id] = {
                'process': process,
                'start_time': time.time(),
                'rtsp_url': camera['rtsp_url'],
                'log_path': log_path
            }
            
            # 最後の再起動時間を記録
            last_restart_time[camera_id] = time.time()
            
            # 初期化時点で更新情報を記録
            hls_last_update[camera_id] = time.time()
            if os.path.exists(hls_path):
                m3u8_last_size[camera_id] = os.path.getsize(hls_path)
                logging.info(f"Found initial m3u8 file with size: {m3u8_last_size[camera_id]}")
            else:
                m3u8_last_size[camera_id] = 0
                logging.info(f"m3u8 file not found initially, will be created by FFmpeg")

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

            # HLSファイルが作成されるまで待機
            wait_count = 0
            max_wait = STREAM_INIT_WAIT  # 3秒
            while wait_count < max_wait:
                if os.path.exists(hls_path) and os.path.getsize(hls_path) > 0:
                    logging.info(f"HLS file created successfully: {hls_path}")
                    return True
                wait_count += 1
                time.sleep(1)
                
            # HLSファイルがまだ作成されていなくても、プロセスが実行中なら成功と見なす
            if process.poll() is None:
                logging.warning(f"HLS file not created yet after {max_wait}s wait: {hls_path}, but process is running")
                return True
            else:
                # プロセスが終了していれば失敗
                logging.error(f"FFmpeg process terminated prematurely for camera {camera_id}")
                return False

        except Exception as e:
            logging.error(f"Error starting streaming for camera {camera_id}: {e}")
            # エラー回数を増加
            connection_error_counts[camera_id] = connection_error_counts.get(camera_id, 0) + 1
            camera_connection_status[camera_id] = 2  # エラー状態
            return False

def cleanup_streaming_files(camera_id):
    """
    特定カメラのストリーミングファイル（m3u8, ts）をクリーンアップする
    
    Args:
        camera_id (str): カメラID
    """
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        if not os.path.exists(camera_tmp_dir):
            return
            
        hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
            
        # 既存のm3u8ファイルがあれば削除
        if os.path.exists(hls_path):
            try:
                os.remove(hls_path)
                logging.info(f"Removed existing m3u8 file: {hls_path}")
            except Exception as e:
                logging.warning(f"Could not remove existing m3u8 file: {hls_path}, Error: {e}")
        
        # 古いTSファイルのクリーンアップ
        try:
            for file in os.listdir(camera_tmp_dir):
                if file.endswith('.ts'):
                    file_path = os.path.join(camera_tmp_dir, file)
                    try:
                        os.remove(file_path)
                        logging.debug(f"Removed old ts file: {file_path}")
                    except Exception as e:
                        logging.warning(f"Could not remove ts file: {file_path}, Error: {e}")
                        
            # クリーンアップログを一定間隔でのみ出力（ログが多すぎるのを防ぐ）
            global last_cleanup_log_time
            current_time = time.time()
            if current_time - last_cleanup_log_time > CLEANUP_LOG_INTERVAL:
                logging.info(f"Cleaned up streaming files for camera {camera_id}")
                last_cleanup_log_time = current_time
                
        except Exception as e:
            logging.warning(f"Error cleaning up ts files: {e}")
            
    except Exception as e:
        logging.warning(f"Error in cleanup_streaming_files for camera {camera_id}: {e}")

def check_streaming_process_health(camera_id):
    """
    ストリーミングプロセスの健全性をチェックする
    
    Args:
        camera_id (str): チェックするカメラID
        
    Returns:
        bool: プロセスが健全かどうか
    """
    try:
        # プロセス情報の確認
        if camera_id not in streaming_processes or not streaming_processes[camera_id]:
            return False
            
        process_info = streaming_processes[camera_id]
        process = process_info.get('process')
        
        if not process or process.poll() is not None:
            return False
            
        # HLSファイルの確認
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
        
        if not os.path.exists(hls_path) or os.path.getsize(hls_path) == 0:
            logging.warning(f"HLS file for camera {camera_id} does not exist or is empty")
            return False
            
        # 最終更新時間の確認
        current_time = time.time()
        last_update = hls_last_update.get(camera_id, 0)
        
        if current_time - last_update > HLS_UPDATE_TIMEOUT:
            logging.warning(f"HLS file for camera {camera_id} has not been updated for {current_time - last_update:.1f} seconds")
            return False
            
        # TSファイルの存在確認
        ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
        if not ts_files:
            logging.warning(f"No TS files found for camera {camera_id}")
            return False
            
        return True
        
    except Exception as e:
        logging.error(f"Error checking streaming process health for camera {camera_id}: {e}")
        return False

def stop_streaming(camera_id):
    """
    特定カメラのストリーミングを停止する
    
    Args:
        camera_id (str): 停止するカメラID
    
    Returns:
        bool: 停止が成功したかどうか
    """
    try:
        logging.info(f"Stopping streaming for camera {camera_id}")
        
        # ロックを取得して重複処理を防止
        if camera_id not in retry_locks:
            retry_locks[camera_id] = threading.Lock()
            
        with retry_locks[camera_id]:
            # プロセス情報の取得
            process_info = streaming_processes.get(camera_id)
            if not process_info:
                logging.info(f"No streaming process found for camera {camera_id}")
                return True
                
            process = process_info.get('process')
            if process:
                # プロセスの終了
                ffmpeg_utils.terminate_process(process)
                logging.info(f"Terminated streaming process for camera {camera_id}")
                
            # カメラのストリーミング状態をクリア
            if camera_id in streaming_processes:
                streaming_processes[camera_id] = None
            
            # HLS更新時間をクリア
            if camera_id in hls_last_update:
                del hls_last_update[camera_id]
                
            # ファイルサイズをクリア
            if camera_id in m3u8_last_size:
                del m3u8_last_size[camera_id]
            
            # 念のため、残っているffmpegプロセスを強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            
            # ストリーミングファイルをクリーンアップ
            cleanup_streaming_files(camera_id)
            
            return True
            
    except Exception as e:
        logging.error(f"Error stopping streaming for camera {camera_id}: {e}")
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
        # 現在時刻を取得
        current_time = time.time()
        
        # 前回の再起動から十分な時間が経過していない場合はスキップ
        if camera_id in last_restart_time:
            time_since_restart = current_time - last_restart_time[camera_id]
            if time_since_restart < MIN_RESTART_INTERVAL:
                logging.warning(f"Too frequent restart attempt for camera {camera_id}, skipping. Last restart was {time_since_restart:.1f} seconds ago")
                return False
        
        logging.warning(f"Restarting streaming for camera {camera_id}")
        
        # ロックを取得して重複処理を防止
        if camera_id not in retry_locks:
            retry_locks[camera_id] = threading.Lock()
            
        with retry_locks[camera_id]:
            # 既存のストリーミングを停止
            stop_streaming(camera_id)
            
            # 短い待機時間を増加 - プロセスが完全に終了するのを待つ
            time.sleep(3)
            
            # 念のため最後にクリーンアップ
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            cleanup_streaming_files(camera_id)
            
            # カメラ設定を読み込んでストリーミングを再開
            camera = camera_utils.get_camera_by_id(camera_id)
            if camera:
                # 最終接続試行時間をリセットして強制的に接続
                if camera_id in last_connection_attempt:
                    del last_connection_attempt[camera_id]
                    
                # 最後の再起動時間を記録
                last_restart_time[camera_id] = current_time
                    
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
    hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
    
    failures = 0
    max_failures = 2  # 連続でこの回数分問題が検出されたら再起動
    check_interval = HEALTH_CHECK_INTERVAL
    
    # 最初のHLSファイル生成を待つ
    wait_count = 0
    max_wait = MAX_HLS_CHECK_ATTEMPTS
    
    while wait_count < max_wait:
        if os.path.exists(hls_path) and os.path.getsize(hls_path) > 0:
            logging.info(f"HLS file created for camera {camera_id} after {wait_count} seconds")
            break
        
        wait_count += 1
        time.sleep(1)
        
        # プロセスが終了していたら待機を終了
        if camera_id not in streaming_processes or not streaming_processes.get(camera_id):
            logging.warning(f"Streaming process for camera {camera_id} no longer exists during initial wait")
            return
    
    if wait_count >= max_wait and (not os.path.exists(hls_path) or os.path.getsize(hls_path) == 0):
        logging.error(f"HLS file not created for camera {camera_id} after {max_wait} seconds, restarting")
        restart_streaming(camera_id)
        return
    
    # 初期監視状態を設定
    hls_last_update[camera_id] = time.time()
    if os.path.exists(hls_path):
        m3u8_last_size[camera_id] = os.path.getsize(hls_path)
    
    while True:
        try:
            # プロセスが存在するか確認
            if camera_id not in streaming_processes:
                # ストリーミングが停止していたら監視も終了
                logging.info(f"Streaming process for camera {camera_id} no longer exists. Stopping HLS monitor.")
                break
            
            process_info = streaming_processes.get(camera_id)
            if not process_info or not process_info.get('process'):
                logging.info(f"No valid streaming process for camera {camera_id}. Stopping HLS monitor.")
                break
                
            # プロセスが終了していたら監視も終了
            process = process_info.get('process')
            if process and process.poll() is not None:
                logging.info(f"Streaming process for camera {camera_id} has terminated. Stopping HLS monitor.")
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
                    logging.warning(f"Error checking m3u8 file size: {e}")
            
            # TSファイルの更新も確認
            try:
                ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                if ts_files:
                    # 最も新しいTSファイルを取得
                    newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                    newest_ts_path = os.path.join(camera_tmp_dir, newest_ts)
                    ts_mtime = os.path.getmtime(newest_ts_path)
                    
                    # 最新のTS更新時間が記録されている最終更新時間より新しい場合
                    if ts_mtime > hls_last_update.get(camera_id, 0):
                        hls_last_update[camera_id] = current_time
                        file_updated = True
                        failures = 0  # 正常更新を検出したらカウンタをリセット
                else:
                    # TSファイルが一つもない場合は問題の可能性
                    logging.warning(f"No TS files found for camera {camera_id}")
            except Exception as e:
                logging.warning(f"Error checking TS files: {e}")
            
            # ファイル更新が停止しているかチェック
            last_update = hls_last_update.get(camera_id, 0)
            time_since_update = current_time - last_update
            
            if not file_updated and time_since_update > HLS_UPDATE_TIMEOUT:
                logging.warning(f"HLS files for camera {camera_id} have not been updated for {time_since_update:.2f} seconds")
                failures += 1
                
                if failures >= max_failures:
                    logging.error(f"HLS update timeout detected for camera {camera_id}. Restarting streaming.")
                    # より強力なクリーンアップを実施
                    cleanup_camera_resources(camera_id)
                    restart_streaming(camera_id)
                    failures = 0
                    
                    # 監視を終了（新しいスレッドが開始されるため）
                    break
            
            # 古いTSファイルを定期的にクリーンアップ
            if current_time % 30 < 1:  # 約30秒ごとに実行
                cleanup_old_segments(camera_id)
            
        except Exception as e:
            logging.error(f"Error monitoring HLS updates for camera {camera_id}: {e}")
        
        # 短い待機時間でループし、問題を早く検出できるようにする
        time.sleep(check_interval)

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
                return_code = process.poll()
                consecutive_failures += 1
                current_delay = min(retry_delay * (1.5 ** consecutive_failures), max_retry_delay)

                logging.warning(f"Streaming process for camera {camera_id} has died with code {return_code}. "
                                f"Attempt {consecutive_failures}/{max_failures}. "
                                f"Waiting {current_delay} seconds before retry.")

                # 既存のffmpegプロセスを強制終了
                ffmpeg_utils.kill_ffmpeg_processes(camera_id)
                time.sleep(3)  # 少し長めに待機

                if consecutive_failures >= max_failures:
                    logging.error(f"Too many consecutive failures for camera {camera_id}. Performing full restart.")
                    cleanup_camera_resources(camera_id)
                    consecutive_failures = 0

                # ストリーミングプロセスを削除
                if camera_id in streaming_processes:
                    streaming_processes[camera_id] = None

                # カメラ設定を読み込んでストリーミングを再開
                camera = camera_utils.get_camera_by_id(camera_id)
                if camera:
                    # 最終接続試行時間をリセットして強制的に接続
                    if camera_id in last_connection_attempt:
                        del last_connection_attempt[camera_id]
                        
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

                # プロセスの CPU 使用率をチェック
                try:
                    proc = psutil.Process(process.pid)
                    cpu_percent = proc.cpu_percent(interval=1)
                    
                    # CPU使用率が極端に高い場合は問題の可能性
                    if cpu_percent > 90:
                        logging.warning(f"High CPU usage detected for camera {camera_id}: {cpu_percent}%")
                        
                    # プロセスのステータスをチェック
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        logging.warning(f"Zombie process detected for camera {camera_id}")
                        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
                        break
                except Exception as e:
                    # psutilでのモニタリングエラーは無視
                    pass

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
        # ロックを取得して重複処理を防止
        if camera_id not in retry_locks:
            retry_locks[camera_id] = threading.Lock()
            
        with retry_locks[camera_id]:
            # まずプロセスを終了
            if camera_id in streaming_processes and streaming_processes[camera_id]:
                process = streaming_processes[camera_id].get('process')
                if process:
                    ffmpeg_utils.terminate_process(process)
                    logging.info(f"Terminated process for camera {camera_id} during cleanup")
            
            # 念のため強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            
            # 一時ディレクトリのファイルを削除
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            if os.path.exists(camera_tmp_dir):
                for file in os.listdir(camera_tmp_dir):
                    try:
                        file_path = os.path.join(camera_tmp_dir, file)
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            logging.debug(f"Removed file during cleanup: {file_path}")
                    except Exception as e:
                        logging.error(f"Error removing file {file}: {e}")

            # 状態変数をクリア
            if camera_id in streaming_processes:
                streaming_processes[camera_id] = None
            if camera_id in hls_last_update:
                del hls_last_update[camera_id]
            if camera_id in m3u8_last_size:
                del m3u8_last_size[camera_id]
                                
            logging.info(f"Completed cleanup for camera {camera_id}")

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
        try:
            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
            removed_count = 0
            
            for file in ts_files:
                if file.endswith('.ts'):
                    file_path = os.path.join(camera_tmp_dir, file)

                    try:
                        # ファイルが以下の条件を満たす場合に削除:
                        # 1. プレイリストに含まれていない
                        # 2. 作成から20秒以上経過している
                        if (file not in active_segments and 
                            os.path.exists(file_path) and 
                            current_time - os.path.getctime(file_path) > 20):
                            os.remove(file_path)
                            removed_count += 1
                    except Exception as e:
                        logging.error(f"Error removing file {file}: {e}")
                        
            # クリーンアップログを定期的にのみ出力（ログ量を減らす）
            if removed_count > 0:
                # 現在時刻が5分の倍数の場合にログ出力
                global last_cleanup_log_time
                if current_time - last_cleanup_log_time > CLEANUP_LOG_INTERVAL:
                    logging.info(f"Cleaned up {removed_count} old segment files for camera {camera_id}")
                    last_cleanup_log_time = current_time
                    
        except Exception as e:
            logging.error(f"Error listing files in directory {camera_tmp_dir}: {e}")

    except Exception as e:
        logging.error(f"Error in cleanup_old_segments for camera {camera_id}: {e}")

def cleanup_scheduler():
    """
    すべてのカメラに対して定期的にクリーンアップを実行するスケジューラー
    """
    while True:
        try:
            # カメラ設定ファイルからカメラ一覧を読み込む
            cameras = camera_utils.read_config()
            
            for camera in cameras:
                try:
                    cleanup_old_segments(camera['id'])
                except Exception as e:
                    logging.error(f"Error cleaning up segments for camera {camera['id']}: {e}")
                    
            # 記録ファイルのローテーション
            rotate_log_files()

        except Exception as e:
            logging.error(f"Error in cleanup_scheduler: {e}")

        # 待機
        time.sleep(20)

def rotate_log_files():
    """FFmpegのログファイルをローテーションする"""
    try:
        # 各カメラのログファイルをチェック
        for camera_id in streaming_processes:
            if not streaming_processes[camera_id]:
                continue
                
            log_path = streaming_processes[camera_id].get('log_path')
            if not log_path or not os.path.exists(log_path):
                continue
                
            # ファイルサイズをチェック
            file_size = os.path.getsize(log_path)
            if file_size > 10 * 1024 * 1024:  # 10MB以上
                # バックアップファイル名を生成
                backup_path = log_path + f".{int(time.time())}.backup"
                
                try:
                    # ファイルをコピーして元のファイルを空にする
                    shutil.copy2(log_path, backup_path)
                    
                    # 元のファイルを空にする
                    with open(log_path, 'w') as f:
                        f.write(f"Log rotated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                        
                    logging.info(f"Rotated log file for camera {camera_id}: {log_path} -> {backup_path}")
                    
                    # 古いバックアップログを削除
                    cleanup_old_logs(os.path.dirname(log_path), camera_id)
                    
                except Exception as e:
                    logging.error(f"Error rotating log file {log_path}: {e}")
                
    except Exception as e:
        logging.error(f"Error rotating log files: {e}")

def cleanup_old_logs(directory, camera_id):
    """古いバックアップログを削除する"""
    try:
        if not os.path.exists(directory):
            return
            
        # バックアップログファイルを検索
        backup_logs = []
        for file in os.listdir(directory):
            if file.endswith('.backup') and camera_id in file:
                file_path = os.path.join(directory, file)
                backup_logs.append((file_path, os.path.getmtime(file_path)))
                
        # 作成日時でソート
        backup_logs.sort(key=lambda x: x[1])
        
        # 最新の3つを残して削除
        if len(backup_logs) > 3:
            for file_path, _ in backup_logs[:-3]:
                try:
                    os.remove(file_path)
                    logging.debug(f"Removed old backup log: {file_path}")
                except Exception as e:
                    logging.error(f"Error removing old backup log {file_path}: {e}")
                    
    except Exception as e:
        logging.error(f"Error cleaning up old logs: {e}")

def stop_all_streaming():
    """
    すべてのストリーミングプロセスを停止

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        success = True
        
        # カメラIDのリストを固定
        camera_ids = list(streaming_processes.keys())
        
        for camera_id in camera_ids:
            try:
                result = stop_streaming(camera_id)
                if not result:
                    success = False
                    logging.error(f"Failed to stop streaming for camera {camera_id}")
            except Exception as e:
                success = False
                logging.error(f"Error stopping streaming for camera {camera_id}: {e}")

        # 残っているffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes()
        
        # 少し待機してプロセスの終了を確認
        time.sleep(2)
        
        return success

    except Exception as e:
        logging.error(f"Error stopping all streaming processes: {e}")
        return False

def initialize_streaming():
    """
    ストリーミングシステムの初期化
    """
    # グローバル変数の初期化
    global streaming_processes, hls_last_update, m3u8_last_size
    global camera_connection_status, last_connection_attempt, connection_error_counts
    global retry_locks, last_restart_time, last_cleanup_log_time, tmp_dir_checked
    
    streaming_processes = {}
    hls_last_update = {}
    m3u8_last_size = {}
    camera_connection_status = {}
    last_connection_attempt = {}
    connection_error_counts = {}
    retry_locks = {}
    last_restart_time = {}
    last_cleanup_log_time = time.time()
    tmp_dir_checked = {}
    
    # 起動時に残っているffmpegプロセスをクリーンアップ
    try:
        ffmpeg_utils.kill_ffmpeg_processes()
    except Exception as e:
        logging.error(f"Error killing ffmpeg processes during initialization: {e}")
    
    # すべてのカメラの一時ディレクトリを確認
    cameras = camera_utils.read_config()
    for camera in cameras:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera['id'])
        try:
            fs_utils.ensure_directory_exists(camera_tmp_dir)
            cleanup_streaming_files(camera['id'])
            tmp_dir_checked[camera['id']] = True
        except Exception as e:
            logging.error(f"Error ensuring tmp directory for camera {camera['id']}: {e}")
            tmp_dir_checked[camera['id']] = False
    
    # クリーンアップスレッドの起動
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    logging.info("Started segment cleanup scheduler thread")

def get_camera_status(camera_id):
    """
    特定カメラの状態を取得

    Args:
        camera_id (str): カメラID

    Returns:
        dict: カメラの状態情報
    """
    status = {
        'connected': False,
        'status_code': 0,  # 0=未接続, 1=接続中, 2=エラー
        'last_update': 0,
        'error_count': 0,
        'uptime': 0
    }
    
    if camera_id in camera_connection_status:
        status['status_code'] = camera_connection_status[camera_id]
        status['connected'] = (camera_connection_status[camera_id] == 1)
    
    if camera_id in hls_last_update:
        status['last_update'] = hls_last_update[camera_id]
    
    if camera_id in connection_error_counts:
        status['error_count'] = connection_error_counts[camera_id]
        
    # 稼働時間を計算
    if camera_id in streaming_processes and streaming_processes[camera_id]:
        start_time = streaming_processes[camera_id].get('start_time', 0)
        if start_time > 0:
            status['uptime'] = int(time.time() - start_time)
    
    return status
