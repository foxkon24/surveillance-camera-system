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
import queue
from datetime import datetime

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
# ストリーミングキューを追加
streaming_queue = queue.Queue()
# ストリーミング処理のロック
streaming_lock = threading.Lock()
# 同時ストリーミング数
active_streams_count = 0
# ストリーミングワーカーの実行フラグ
streaming_workers_running = False
# リソース使用状況
system_resources = {'cpu': 0, 'memory': 0}
# 健全性チェックの間隔（秒）
HEALTH_CHECK_INTERVAL = 15
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 30
# ストリーミング再起動回数を記録
restart_counts = {}
# ストリーミング再起動の最大回数（これを超えるとより長い時間待機する）
MAX_RESTART_COUNT = 5
# ストリーミング再起動後の待機時間（秒）
RESTART_COOLDOWN = 30

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    global active_streams_count
    
    if camera['id'] in streaming_processes:
        # すでにストリーミング中の場合は成功を返す
        return True
    
    # キューに追加して非同期で処理
    streaming_queue.put(camera)
    
    # ワーカースレッドがまだ起動していなければ起動
    if not streaming_workers_running:
        start_streaming_workers()
    
    # キューに入れたことを成功として返す
    return True

def start_streaming_workers():
    """
    ストリーミングワーカースレッドを開始する
    """
    global streaming_workers_running
    
    if streaming_workers_running:
        return
    
    streaming_workers_running = True
    
    # ワーカースレッドを作成
    for i in range(3):  # 複数のワーカースレッドを作成
        worker = threading.Thread(
            target=streaming_worker,
            daemon=True,
            name=f"streaming-worker-{i}"
        )
        worker.start()
    
    # リソース監視スレッドを開始
    resource_monitor = threading.Thread(
        target=monitor_system_resources,
        daemon=True,
        name="resource-monitor"
    )
    resource_monitor.start()
    
    # 定期的なクリーンアップスレッドを開始
    cleanup_thread = threading.Thread(
        target=cleanup_scheduler,
        daemon=True,
        name="cleanup-scheduler"
    )
    cleanup_thread.start()
    
    # 全体的な健全性監視スレッドを開始
    health_monitor = threading.Thread(
        target=global_health_monitor,
        daemon=True,
        name="health-monitor"
    )
    health_monitor.start()
    
    logging.info("Streaming workers and monitors started")

def streaming_worker():
    """
    ストリーミングリクエストを処理するワーカー
    """
    global active_streams_count
    
    while True:
        try:
            # キューからカメラ情報を取得
            camera = streaming_queue.get(timeout=1)
            
            # 既にストリーミング中ならスキップ
            if camera['id'] in streaming_processes:
                streaming_queue.task_done()
                continue
            
            # リソース使用状況をチェック
            cpu_usage = system_resources['cpu']
            mem_usage = system_resources['memory']
            
            with streaming_lock:
                current_streams = active_streams_count
            
            # リソース制限チェック
            if current_streams >= config.MAX_CONCURRENT_STREAMS:
                logging.warning(f"Maximum concurrent streams limit reached ({current_streams}/{config.MAX_CONCURRENT_STREAMS}). Delaying stream for camera {camera['id']}")
                # キューに戻して後で再試行
                streaming_queue.put(camera)
                streaming_queue.task_done()
                time.sleep(5)
                continue
            
            if cpu_usage > config.MAX_CPU_PERCENT or mem_usage > config.MAX_MEM_PERCENT:
                logging.warning(f"System resources critical: CPU {cpu_usage}%, Memory {mem_usage}%. Delaying stream for camera {camera['id']}")
                # キューに戻して後で再試行
                streaming_queue.put(camera)
                streaming_queue.task_done()
                time.sleep(10)
                continue
            
            # ストリーミングを開始
            success = start_streaming_process(camera)
            
            if success:
                with streaming_lock:
                    active_streams_count += 1
                logging.info(f"Successfully started streaming for camera {camera['id']}. Active streams: {active_streams_count}")
            else:
                logging.error(f"Failed to start streaming for camera {camera['id']}")
                # 少し待ってから再試行
                time.sleep(5)
                streaming_queue.put(camera)
            
            streaming_queue.task_done()
            
        except queue.Empty:
            # キューが空の場合は待機
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Error in streaming worker: {e}")
            time.sleep(1)

def start_streaming_process(camera):
    """
    実際にストリーミングプロセスを開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera['id'])
        fs_utils.ensure_directory_exists(camera_tmp_dir)

        # logディレクトリの存在確認（なければ作成）
        log_dir = os.path.join(config.BASE_PATH, 'log')
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        log_path = os.path.join(log_dir, f"hls_{camera['id']}_{timestamp}.log").replace('/', '\\')

        hls_path = os.path.join(camera_tmp_dir, f"{camera['id']}.m3u8").replace('/', '\\')

        # すべてのストリーミングプロセスを完全に終了
        ffmpeg_utils.kill_ffmpeg_processes()
        time.sleep(2)  # 確実にすべてのプロセスが終了するのを待つ

        # ディレクトリを完全に削除して再作成
        try:
            # ディレクトリを完全に削除
            if os.path.exists(camera_tmp_dir):
                logging.info(f"ディレクトリを削除します: {camera_tmp_dir}")
                fs_utils.remove_directory(camera_tmp_dir)
            
            # 新しくディレクトリを作成
            os.makedirs(camera_tmp_dir, exist_ok=True)
            logging.info(f"ディレクトリを作成しました: {camera_tmp_dir}")

        except Exception as e:
            logging.error(f"ディレクトリクリーンアップエラー: {e}")
            # エラーでも継続する

        # RTSP接続の確認 - タイムアウトを短くして速度優先
        rtsp_check_success = ffmpeg_utils.check_rtsp_connection(camera['rtsp_url'], timeout=config.RTSP_TIMEOUT)
        if not rtsp_check_success:
            logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
            # 接続に失敗しても続行する

        # FFmpegコマンドを統一関数で生成
        ffmpeg_cmd = ffmpeg_utils.get_hls_streaming_command(
            camera['rtsp_url'],
            hls_path,
            segment_time=2
        )

        # プロセス起動前に出力ファイルのパスが有効か確認
        try:
            test_dir = os.path.dirname(hls_path)
            if not os.path.exists(test_dir):
                os.makedirs(test_dir, exist_ok=True)
                logging.info(f"出力ディレクトリを確認しました: {test_dir}")
                
            # 書き込みテストを実施
            test_file = os.path.join(test_dir, 'write_test.txt')
            with open(test_file, 'w') as f:
                f.write('test')
            if os.path.exists(test_file):
                os.remove(test_file)
                logging.info("書き込みテスト成功")
        except Exception as e:
            logging.error(f"出力先への書き込みテストに失敗: {e}")
            # エラーは記録するが処理は継続

        # FFmpegプロセスを開始（独立ログファイルを使用）
        process = ffmpeg_utils.start_ffmpeg_process(
            ffmpeg_cmd, 
            log_path=log_path,  # カメラごとに独立したログファイル
            high_priority=True,
            show_error=True  # エラー出力を詳細に表示
        )
        streaming_processes[camera['id']] = process
        
        # 初期化時点で更新情報を記録
        hls_last_update[camera['id']] = time.time()
        if os.path.exists(hls_path):
            m3u8_last_size[camera['id']] = os.path.getsize(hls_path)
        else:
            m3u8_last_size[camera['id']] = 0

        # 再起動カウンターの初期化/リセット
        restart_counts[camera['id']] = 0

        # プロセスの状態を確認
        time.sleep(2)  # プロセスの起動を待つ
        if process.poll() is not None:
            return_code = process.poll()
            # エラー出力を詳細に取得
            try:
                error_output = ""
                if process.stderr:
                    error_output = process.stderr.read().decode('utf-8', errors='replace')
                if not error_output and os.path.exists(log_path):
                    with open(log_path, 'r') as f:
                        error_output = f.read()
            except Exception as err:
                logging.error(f"エラー出力の読み取りに失敗: {err}")
                error_output = "エラー出力の取得に失敗しました"

            logging.error(f"FFmpeg process failed to start. Return code: {return_code}")
            logging.error(f"FFmpeg error output: {error_output}")
            
            if camera['id'] in streaming_processes:
                del streaming_processes[camera['id']]
            return False

        # m3u8ファイルの生成を待機（最大30秒に延長）
        m3u8_created = False
        max_wait_time = 30  # 最大待機時間（秒）
        start_wait_time = time.time()
        wait_interval = 0.5  # 確認間隔を短く
        
        logging.info(f"Waiting for m3u8 file creation for camera {camera['id']}...")
        while time.time() - start_wait_time < max_wait_time:
            # プロセスが終了していないか確認
            if process.poll() is not None:
                return_code = process.poll()
                error_output = ""
                try:
                    if process.stderr:
                        error_output = process.stderr.read().decode('utf-8', errors='replace')
                    if not error_output and os.path.exists(log_path):
                        with open(log_path, 'r') as f:
                            error_output = f.read()
                except Exception as err:
                    logging.error(f"エラー出力の読み取りに失敗: {err}")
                    error_output = "エラー出力の取得に失敗しました"
                
                logging.error(f"FFmpeg process terminated during m3u8 wait with code {return_code}")
                logging.error(f"FFmpeg error output: {error_output}")
                if camera['id'] in streaming_processes:
                    del streaming_processes[camera['id']]
                return False
            
            # ファイル存在チェック
            if os.path.exists(hls_path):
                try:
                    m3u8_size = os.path.getsize(hls_path)
                    if m3u8_size > 0:
                        with open(hls_path, 'r') as f:
                            content = f.read()
                        
                        # m3u8の内容が有効か確認
                        if "#EXTM3U" in content:
                            # TSファイルが少なくとも1つ以上生成されているか確認
                            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                            if ts_files:
                                m3u8_created = True
                                logging.info(f"HLS playlist and TS files created for camera {camera['id']} after {time.time() - start_wait_time:.1f}s")
                                # ファイルを確実に更新するために一度コピーを作成
                                with open(hls_path, 'r') as src:
                                    m3u8_content = src.read()
                                # 一時的なバックアップファイルを作成（異常時の回復用）
                                backup_path = os.path.join(camera_tmp_dir, f"{camera['id']}_backup.m3u8")
                                with open(backup_path, 'w') as dst:
                                    dst.write(m3u8_content)
                                logging.info(f"Created backup m3u8 file for camera {camera['id']}")
                                break
                except Exception as e:
                    logging.warning(f"Error checking m3u8 file: {e}")
            
            time.sleep(wait_interval)
        
        if not m3u8_created:
            logging.warning(f"HLS playlist file was not created in time for camera {camera['id']}")
            # ファイルが作成されていない場合はプロセスを終了して失敗扱いに
            ffmpeg_utils.terminate_process(process)
            if camera['id'] in streaming_processes:
                del streaming_processes[camera['id']]
            return False
        
        # 監視スレッドを開始
        monitor_thread = threading.Thread(
            target=monitor_streaming_process,
            args=(camera['id'], process),
            daemon=True,
            name=f"monitor-stream-{camera['id']}"
        )
        monitor_thread.start()

        # ファイル更新監視スレッドを開始
        hls_monitor_thread = threading.Thread(
            target=monitor_hls_updates,
            args=(camera['id'],),
            daemon=True,
            name=f"monitor-hls-{camera['id']}"
        )
        hls_monitor_thread.start()

        logging.info(f"Successfully started streaming for camera {camera['id']}")
        return True

    except Exception as e:
        logging.error(f"Error starting streaming process for camera {camera['id']}: {e}")
        return False

def restart_streaming(camera_id):
    """
    ストリーミングプロセスを再起動

    Args:
        camera_id (str): 再起動するカメラID

    Returns:
        bool: 操作が成功したかどうか
    """
    global active_streams_count, restart_counts
    
    try:
        # 再起動回数のインクリメント
        if camera_id not in restart_counts:
            restart_counts[camera_id] = 0
        restart_counts[camera_id] += 1
        
        current_restart_count = restart_counts[camera_id]
        
        # 再起動回数に基づいて待機時間を計算
        if current_restart_count > MAX_RESTART_COUNT:
            cooldown = RESTART_COOLDOWN * (current_restart_count - MAX_RESTART_COUNT + 1)
            cooldown = min(cooldown, 300)  # 最大5分まで
            logging.warning(f"Camera {camera_id} has been restarted {current_restart_count} times. Waiting {cooldown} seconds before restart.")
            time.sleep(cooldown)
        
        logging.info(f"Restarting streaming for camera {camera_id} (restart #{current_restart_count})")
        
        # 既存のプロセスを強制終了
        if camera_id in streaming_processes:
            try:
                process = streaming_processes[camera_id]
                ffmpeg_utils.terminate_process(process)
                del streaming_processes[camera_id]
                
                with streaming_lock:
                    active_streams_count = max(0, active_streams_count - 1)
                
                logging.info(f"Terminated existing streaming process for camera {camera_id}")
            except Exception as term_error:
                logging.error(f"Error terminating process for camera {camera_id}: {term_error}")
        
        # 残っているプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        
        # カメラ情報を取得して再起動
        camera = camera_utils.get_camera_by_id(camera_id)
        if not camera:
            logging.error(f"Failed to restart streaming: camera {camera_id} not found in configuration")
            return False
        
        # ストリーミングを再開
        return get_or_start_streaming(camera)
        
    except Exception as e:
        logging.error(f"Error restarting streaming for camera {camera_id}: {e}")
        return False

def monitor_streaming_process(camera_id, process):
    """
    ストリーミングプロセスの監視を行う
    """
    global restart_counts
    
    try:
        consecutive_errors = 0
        last_error_time = time.time()
        
        while process.poll() is None:
            try:
                # プロセスのリソース使用状況を監視
                proc = psutil.Process(process.pid)
                if not proc.is_running():
                    logging.error(f"Camera {camera_id} process is not running")
                    break
                    
                # HLSファイルの健全性チェック
                if not check_hls_file_health(camera_id):
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logging.error(f"Camera {camera_id} HLS file health check failed 3 times")
                        break
                else:
                    consecutive_errors = 0
                
                # エラー間隔をチェック
                current_time = time.time()
                if current_time - last_error_time > 300:  # 5分以上エラーがない
                    restart_counts[camera_id] = 0  # エラーカウントをリセット
                
                time.sleep(5)  # 監視間隔
                
            except psutil.NoSuchProcess:
                logging.error(f"Camera {camera_id} process no longer exists")
                break
            except Exception as e:
                logging.error(f"Error monitoring camera {camera_id}: {e}")
                time.sleep(1)
                
    except Exception as e:
        logging.error(f"Monitor thread error for camera {camera_id}: {e}")
    finally:
        cleanup_camera_resources(camera_id)

def check_hls_file_health(camera_id):
    """
    HLSファイルの健全性をチェック
    """
    try:
        m3u8_path = os.path.join(config.TMP_PATH, str(camera_id), f"{camera_id}.m3u8")
        if not os.path.exists(m3u8_path):
            return False
            
        # ファイルの更新時刻をチェック
        current_time = time.time()
        mod_time = os.path.getmtime(m3u8_path)
        
        if current_time - mod_time > 10:  # 10秒以上更新がない
            return False
            
        # ファイルサイズをチェック
        size = os.path.getsize(m3u8_path)
        if size < 100:  # ファイルが小さすぎる
            return False
            
        # TSファイルの存在確認
        with open(m3u8_path, 'r') as f:
            content = f.read()
            if '.ts' not in content:
                return False
                
        return True
        
    except Exception as e:
        logging.error(f"Error checking HLS file health for camera {camera_id}: {e}")
        return False

def monitor_hls_updates(camera_id):
    """
    HLSファイルの更新を監視するスレッド

    Args:
        camera_id (str): 監視するカメラID
    """
    try:
        logging.info(f"Started HLS monitor thread for camera {camera_id}")
        
        # 初期化
        last_check_time = time.time()
        update_detected = False
        consecutive_no_updates = 0
        
        while camera_id in streaming_processes:
            try:
                current_time = time.time()
                # 15秒ごとにチェック（間隔を延長してオーバーヘッドを減らす）
                if current_time - last_check_time >= 15:
                    last_check_time = current_time
                    
                    # M3U8ファイルの確認
                    camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                    m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
                    
                    if not os.path.exists(m3u8_path):
                        logging.warning(f"M3U8 file missing for camera {camera_id} during update check")
                        consecutive_no_updates += 1
                    else:
                        try:
                            # ファイルサイズとタイムスタンプをチェック
                            current_size = os.path.getsize(m3u8_path)
                            current_mtime = os.path.getmtime(m3u8_path)
                            
                            # 前回のサイズと比較
                            if camera_id in m3u8_last_size:
                                previous_size = m3u8_last_size[camera_id]
                                
                                # 更新があったかどうかを判断
                                if current_size != previous_size:
                                    logging.info(f"HLS file for camera {camera_id} updated: {previous_size} -> {current_size} bytes")
                                    update_detected = True
                                    consecutive_no_updates = 0
                                    m3u8_last_size[camera_id] = current_size
                                else:
                                    # サイズが変わらない場合はmtimeをチェック
                                    if current_time - current_mtime > 30:  # 30秒以上更新がなければカウント
                                        consecutive_no_updates += 1
                                        logging.warning(f"No HLS file updates for camera {camera_id} in {current_time - current_mtime:.1f} seconds")
                            else:
                                # 初回の場合はサイズを記録
                                m3u8_last_size[camera_id] = current_size
                                
                        except Exception as e:
                            logging.error(f"Error checking HLS file for camera {camera_id}: {e}")
                            consecutive_no_updates += 1
                
                # 連続して更新がない場合は再起動（回数を増やして安定性を向上）
                if consecutive_no_updates >= 5:  # 5回連続で更新がない場合（約75秒間）
                    logging.warning(f"No HLS updates detected for camera {camera_id} after {consecutive_no_updates} checks - restarting stream")
                    restart_camera_stream(camera_id)
                    return
            
            except Exception as e:
                logging.error(f"Error in HLS monitoring loop for camera {camera_id}: {e}")
                consecutive_no_updates += 1
            
            # 監視間隔で待機
            time.sleep(5)  # 5秒間隔に延長（より安定した監視のため）
            
    except Exception as e:
        logging.error(f"Error in monitor_hls_updates for camera {camera_id}: {e}")
        restart_camera_stream(camera_id)

def restart_camera_stream(camera_id):
    """
    カメラストリームを安全に再起動する

    Args:
        camera_id (str): 再起動するカメラID
        
    Returns:
        bool: 再起動に成功したかどうか
    """
    try:
        logging.info(f"カメラ {camera_id} のストリームを再起動します")
        
        # カメラのプロセスとカウントをトラックするためのグローバル変数を取得
        global streaming_processes, restart_counts, active_streams_count
        
        # 1. 既存のプロセスを停止
        if camera_id in streaming_processes:
            try:
                process = streaming_processes[camera_id]
                logging.info(f"終了中のストリーミングプロセス: カメラ {camera_id}")
                ffmpeg_utils.terminate_process(process)
                time.sleep(2)  # プロセスの終了を待つ
            except Exception as e:
                logging.error(f"Error terminating process for camera {camera_id}: {e}")
            
            # プロセス情報を削除
            if camera_id in streaming_processes:
                del streaming_processes[camera_id]
        
            # アクティブカウントを減らす
            with streaming_lock:
                active_streams_count = max(0, active_streams_count - 1)
        
        # 2. 残っているすべてのFFmpegプロセスを強制終了（より強力なクリーンアップ）
        logging.info(f"カメラ {camera_id} の残っているプロセスを強制終了")
        # カメラIDに関連するプロセスのみ終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id=camera_id)
        
        # 3. カメラのディレクトリを再確認
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        
        # 4. ディレクトリとそのコンテンツのバックアップを作成
        try:
            backup_dir = os.path.join(config.BASE_PATH, 'backup', camera_id)
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir, exist_ok=True)
            
            # 既存のm3u8のバックアップを取得（バックアップがあれば）
            m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
            backup_m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}_backup.m3u8")
            if os.path.exists(backup_m3u8_path):
                backup_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup_file = os.path.join(backup_dir, f"{camera_id}_backup_{backup_timestamp}.m3u8")
                try:
                    with open(backup_m3u8_path, 'r') as src:
                        backup_content = src.read()
                    with open(backup_file, 'w') as dst:
                        dst.write(backup_content)
                    logging.info(f"バックアップm3u8を保存しました: {backup_file}")
                except Exception as e:
                    logging.error(f"バックアップm3u8の保存に失敗: {e}")
        except Exception as e:
            logging.error(f"バックアップディレクトリ作成エラー: {e}")
        
        # 5. ディレクトリを完全に削除して再作成
        if os.path.exists(camera_tmp_dir):
            try:
                # ディレクトリとそのすべての内容を削除
                logging.info(f"カメラ {camera_id} のディレクトリを削除: {camera_tmp_dir}")
                fs_utils.remove_directory(camera_tmp_dir)
            except Exception as e:
                logging.error(f"カメラ {camera_id} のディレクトリ削除エラー: {e}")
                # 削除に失敗した場合、強制的に再作成
                try:
                    # 強制的にファイル削除を試みる
                    if os.path.exists(camera_tmp_dir):
                        for filename in os.listdir(camera_tmp_dir):
                            file_path = os.path.join(camera_tmp_dir, filename)
                            try:
                                if os.path.isfile(file_path):
                                    os.remove(file_path)
                                    logging.info(f"ファイルを削除: {file_path}")
                            except Exception as file_err:
                                logging.error(f"ファイル削除エラー: {file_err}")
                except Exception:
                    pass
        
        # 6. 新しくディレクトリを作成
        try:
            os.makedirs(camera_tmp_dir, exist_ok=True)
            logging.info(f"カメラ {camera_id} の新しいディレクトリを作成: {camera_tmp_dir}")
        except Exception as e:
            logging.error(f"ディレクトリ作成エラー: {e}")
            return False

        # 7. キャッシュをクリア
        if camera_id in hls_last_update:
            del hls_last_update[camera_id]
        if camera_id in m3u8_last_size:
            del m3u8_last_size[camera_id]
        
        # 8. 少し待機してからストリーミングを再開（プロセス終了と競合しないように）
        time.sleep(3)
        
        # 9. カメラ情報を取得
        camera = camera_utils.get_camera_by_id(camera_id)
        if not camera:
            logging.error(f"Failed to find camera info for {camera_id}")
            return False
        
        # 10. 新しいプロセスを直接開始（キューなどを介さない）
        logging.info(f"カメラ {camera_id} の新しいストリーミングプロセスを開始")
        
        # 11. 再起動回数を確認し、制限を超えている場合はより長い待機
        current_restart_count = restart_counts.get(camera_id, 0) + 1
        restart_counts[camera_id] = current_restart_count
        
        if current_restart_count > 5:
            # 指数バックオフで待機時間を増加
            wait_time = min(5 * (2 ** (current_restart_count - 5)), 300)  # 最大5分
            logging.warning(f"カメラ {camera_id} の再起動回数が多いため {wait_time}秒待機します (再起動回数: {current_restart_count})")
            time.sleep(wait_time)
        
        # 12. プロセス開始
        success = start_streaming_process(camera)
        
        if success:
            logging.info(f"カメラ {camera_id} のストリーム再起動成功")
            # アクティブカウントを更新
            with streaming_lock:
                active_streams_count += 1
            return True
        else:
            logging.error(f"カメラ {camera_id} のストリーム再起動失敗")
            # 少し待機してから再試行（開始に失敗した場合のリトライ）
            time.sleep(5)
            logging.info(f"カメラ {camera_id} のストリーム再起動を再試行")
            
            # 最後の試行として、すべてのFFmpegプロセスを確実に強制終了
            ffmpeg_utils.kill_ffmpeg_processes()
            time.sleep(3)
            
            # 再度ディレクトリをクリーンアップして作成
            if os.path.exists(camera_tmp_dir):
                fs_utils.remove_directory(camera_tmp_dir)
            os.makedirs(camera_tmp_dir, exist_ok=True)
            
            success = start_streaming_process(camera)
            
            if success:
                logging.info(f"カメラ {camera_id} のストリーム再起動に2回目で成功")
                
                # アクティブカウントを更新
                with streaming_lock:
                    active_streams_count += 1
                    
                return True
            else:
                logging.error(f"カメラ {camera_id} のストリーム再起動に2回失敗")
                return False
        
    except Exception as e:
        logging.error(f"カメラ {camera_id} のストリーム再起動中にエラー発生: {e}")
        return False

def cleanup_camera_resources(camera_id):
    """
    指定されたカメラのリソースをクリーンアップ

    Args:
        camera_id (str): クリーンアップするカメラID
    """
    try:
        logging.info(f"Cleaning up resources for camera {camera_id}")
        
        # ストリーミングキャッシュから削除
        if camera_id in streaming_processes:
            process = streaming_processes[camera_id]
            
            try:
                # プロセスを停止
                ffmpeg_utils.terminate_process(process)
            except:
                pass
                
            del streaming_processes[camera_id]
        
        # ストリーミングの監視データを削除
        if camera_id in hls_last_update:
            del hls_last_update[camera_id]
            
        if camera_id in m3u8_last_size:
            del m3u8_last_size[camera_id]
            
        if camera_id in restart_counts:
            del restart_counts[camera_id]
        
        # 残っているffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        
        # 古いセグメントファイルを削除
        cleanup_old_segments(camera_id)
    
    except Exception as e:
        logging.error(f"Error cleaning up resources for camera {camera_id}: {e}")

def cleanup_old_segments(camera_id, force=False):
    """
    古いHLSセグメントファイルを削除

    Args:
        camera_id (str): クリーンアップするカメラID
        force (bool): 強制的に削除するかどうか
    """
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        
        if not os.path.exists(camera_tmp_dir):
            return
            
        # m3u8プレイリストに含まれているセグメントを確認
        m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
        active_segments = set()
        
        # ディレクトリ内のtsファイルをカウント
        ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
        
        # m3u8ファイルがないがtsファイルが存在する状態を検出（異常状態）
        if not os.path.exists(m3u8_path) and ts_files:
            logging.warning(f"異常状態検出: カメラ {camera_id} のm3u8ファイルがないのにtsファイルが {len(ts_files)} 個存在します")
            
            if force:
                # forceモードの場合、すべてのtsファイルを削除
                logging.info(f"カメラ {camera_id} の全てのtsファイルを強制削除します")
                for ts_file in ts_files:
                    try:
                        os.remove(os.path.join(camera_tmp_dir, ts_file))
                    except Exception as remove_err:
                        logging.error(f"tsファイルの削除に失敗しました: {ts_file}: {remove_err}")
                
                # プロセスが実行中なら、カメラの再起動をリクエスト
                if camera_id in streaming_processes:
                    logging.warning(f"カメラ {camera_id} のストリーミングを再起動します（m3u8なしtsファイルあり状態）")
                    
                    # この関数内から直接再起動せず、フラグを設定して次回のhealth_monitorで処理
                    # 再帰的な呼び出しを防ぐため、グローバルフラグやイベントで通知する代わりにログだけ残す
            return
        
        # 通常の処理：m3u8ファイルからアクティブなセグメントを読み取る
        if os.path.exists(m3u8_path):
            try:
                with open(m3u8_path, 'r') as f:
                    content = f.read()
                    
                # .tsファイルの行を検出
                for line in content.splitlines():
                    if line.endswith('.ts') and not line.startswith('#'):
                        active_segments.add(line.strip())
            except Exception as read_error:
                logging.error(f"Error reading m3u8 file for camera {camera_id}: {read_error}")
        
        # ディレクトリ内のすべてのtsファイルをチェック
        deleted_count = 0
        total_count = 0
        current_time = time.time()
        
        for filename in os.listdir(camera_tmp_dir):
            if filename.endswith('.ts'):
                total_count += 1
                file_path = os.path.join(camera_tmp_dir, filename)
                
                try:
                    # ファイルがm3u8に含まれているか確認
                    is_active = filename in active_segments
                    
                    # ファイルの経過時間を確認
                    file_age = current_time - os.path.getmtime(file_path)
                    
                    # アクティブなセグメントはより長く保持し、非アクティブなセグメントは早めに削除
                    max_age = config.HLS_SEGMENT_MAX_AGE
                    if is_active:
                        # アクティブなセグメントは2倍の時間保持
                        max_age = config.HLS_SEGMENT_MAX_AGE * 2
                    else:
                        # 非アクティブなセグメントは半分の時間で削除
                        max_age = config.HLS_SEGMENT_MAX_AGE / 2
                    
                    # ファイルが古すぎる場合またはforceフラグが立っている場合は削除
                    if file_age > max_age or force:
                        os.remove(file_path)
                        deleted_count += 1
                        logging.debug(f"Deleted old segment file: {file_path} (age: {file_age:.1f}s, active: {is_active})")
                except Exception as remove_error:
                    logging.error(f"Error removing segment file {file_path}: {remove_error}")
        
        if deleted_count > 0:
            logging.info(f"Cleaned up {deleted_count}/{total_count} segment files for camera {camera_id}")
    
    except Exception as e:
        logging.error(f"Error cleaning up segments for camera {camera_id}: {e}")

def global_health_monitor():
    """
    全体的なストリーミングの健全性を監視するスレッド
    """
    last_full_check = time.time()
    issue_counts = {}  # カメラIDごとの問題検出回数
    last_m3u8_check = {}  # 最後にm3u8ファイルを確認した時間
    m3u8_backup_status = {}  # バックアップm3u8の状態

    while True:
        try:
            current_time = time.time()
            # フルチェックは5分ごとに実行（それ以外は軽量チェックのみ）
            full_check = (current_time - last_full_check) > 300
            
            if full_check:
                last_full_check = current_time
                logging.info("グローバルヘルスモニター：完全システム状態チェック開始")
                
                # 残っているすべてのFFmpegプロセスを確認
                ffmpeg_processes = []
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    try:
                        if 'ffmpeg' in proc.name().lower():
                            cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                            ffmpeg_processes.append({
                                'pid': proc.info['pid'],
                                'cmdline': cmdline,
                                'create_time': proc.create_time() if hasattr(proc, 'create_time') else 0
                            })
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                
                logging.info(f"現在実行中のFFmpegプロセス: {len(ffmpeg_processes)}個")
                
                # システムリソースを確認
                cpu_percent = psutil.cpu_percent(interval=0.5)
                memory_info = psutil.virtual_memory()
                disk_usage = psutil.disk_usage('/')
                
                logging.info(f"システムリソース状態: CPU={cpu_percent}%, メモリ使用率={memory_info.percent}%, "
                            f"ディスク使用率={disk_usage.percent}%")
                
                # 異常なリソース使用率を検出
                if cpu_percent > 90 or memory_info.percent > 90 or disk_usage.percent > 90:
                    logging.warning("システムリソース不足が検出されました - パフォーマンスに影響を与える可能性があります")
                
                # すべてのカメラのm3u8ファイルと関連するFFmpegプロセスを検証
                for camera_id, process in streaming_processes.items():
                    try:
                        # 対応するFFmpegプロセスが存在するか確認
                        process_exists = process.poll() is None
                        
                        # m3u8ファイルが存在し、更新されているか確認
                        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                        m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
                        m3u8_backup_path = os.path.join(camera_tmp_dir, f"{camera_id}_backup.m3u8")
                        
                        m3u8_exists = os.path.exists(m3u8_path)
                        m3u8_backup_exists = os.path.exists(m3u8_backup_path)
                        
                        # m3u8の確認時間を記録
                        last_m3u8_check[camera_id] = current_time
                        m3u8_backup_status[camera_id] = m3u8_backup_exists
                        
                        # 問題のチェック
                        issues_detected = []
                        
                        if not process_exists:
                            issues_detected.append("プロセスが終了しています")
                        
                        if not m3u8_exists:
                            issues_detected.append("m3u8ファイルが存在しません")
                            # バックアップからの復元を試みる
                            if m3u8_backup_exists:
                                try:
                                    logging.info(f"バックアップm3u8を復元: {camera_id}")
                                    with open(m3u8_backup_path, 'r') as src:
                                        backup_content = src.read()
                                    with open(m3u8_path, 'w') as dst:
                                        dst.write(backup_content)
                                    logging.info(f"カメラ {camera_id} のm3u8を復元しました")
                                    # ファイルが復元されたことを確認
                                    if os.path.exists(m3u8_path):
                                        issues_detected.remove("m3u8ファイルが存在しません")
                                        issues_detected.append("m3u8ファイルをバックアップから復元しました")
                                except Exception as e:
                                    logging.error(f"バックアップからの復元エラー: {e}")
                        else:
                            # m3u8ファイルの更新時間を確認
                            try:
                                m3u8_mtime = os.path.getmtime(m3u8_path)
                                if current_time - m3u8_mtime > 60:  # 1分以上更新がない場合
                                    issues_detected.append(f"m3u8ファイルが {int(current_time - m3u8_mtime)}秒間更新されていません")
                                
                                # ファイルサイズが0の場合も問題
                                if os.path.getsize(m3u8_path) == 0:
                                    issues_detected.append("m3u8ファイルのサイズが0です")
                                    
                                # TSファイルの存在と更新時間を確認
                                ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                                if not ts_files:
                                    issues_detected.append("TSファイルが存在しません")
                                else:
                                    # 最新のTSファイルの更新時間をチェック
                                    newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                                    newest_ts_mtime = os.path.getmtime(os.path.join(camera_tmp_dir, newest_ts))
                                    if current_time - newest_ts_mtime > 60:  # 1分以上更新がない
                                        issues_detected.append(f"最新のTSファイルが {int(current_time - newest_ts_mtime)}秒間更新されていません")
                            except Exception as e:
                                logging.error(f"ファイル確認エラー: {e}")
                                issues_detected.append(f"ファイル確認中にエラー: {str(e)}")
                        
                        # バックアップファイルが存在しない場合、作成
                        if m3u8_exists and not m3u8_backup_exists:
                            try:
                                with open(m3u8_path, 'r') as src:
                                    m3u8_content = src.read()
                                with open(m3u8_backup_path, 'w') as dst:
                                    dst.write(m3u8_content)
                                logging.info(f"カメラ {camera_id} のm3u8バックアップを作成しました")
                                m3u8_backup_status[camera_id] = True
                            except Exception as e:
                                logging.error(f"バックアップ作成エラー: {e}")
                        
                        # 問題の合計数と詳細をログに出力
                        if issues_detected:
                            logging.warning(f"カメラ {camera_id} で {len(issues_detected)} 個の問題を検出: {', '.join(issues_detected)}")
                            
                            # 問題のカウンターを更新
                            if camera_id not in issue_counts:
                                issue_counts[camera_id] = 0
                            issue_counts[camera_id] += 1
                            
                            # 複数回連続で問題が検出された場合、ストリームを再起動
                            if issue_counts[camera_id] >= 2:
                                logging.warning(f"カメラ {camera_id} で複数回問題が検出されたため、ストリームを再起動します")
                                # 問題カウンターをリセット
                                issue_counts[camera_id] = 0
                                # ストリームを再起動
                                restart_camera_stream(camera_id)
                        else:
                            # 問題がなければカウンターをリセット
                            if camera_id in issue_counts:
                                issue_counts[camera_id] = 0
                            logging.info(f"カメラ {camera_id} のストリーミングは正常に動作しています")
                    
                    except Exception as e:
                        logging.error(f"カメラ {camera_id} の健全性チェック中にエラー: {e}")
            
            else:
                # 非フルチェック時の軽量監視
                # 各カメラのm3u8ファイルの存在だけを確認（30秒ごと）
                cameras_to_check = list(streaming_processes.keys())
                for camera_id in cameras_to_check:
                    # 最後のチェックから30秒以上経過している場合のみチェック
                    last_check = last_m3u8_check.get(camera_id, 0)
                    if current_time - last_check >= 30:
                        try:
                            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                            m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
                            m3u8_backup_path = os.path.join(camera_tmp_dir, f"{camera_id}_backup.m3u8")
                            
                            m3u8_exists = os.path.exists(m3u8_path)
                            last_m3u8_check[camera_id] = current_time
                            
                            # m3u8が存在しないが、バックアップが存在する場合
                            if not m3u8_exists and os.path.exists(m3u8_backup_path) and m3u8_backup_status.get(camera_id, False):
                                try:
                                    # バックアップからm3u8を復元
                                    logging.info(f"軽量チェック: カメラ {camera_id} のm3u8をバックアップから復元")
                                    with open(m3u8_backup_path, 'r') as src:
                                        backup_content = src.read()
                                    with open(m3u8_path, 'w') as dst:
                                        dst.write(backup_content)
                                    logging.info(f"カメラ {camera_id} のm3u8をバックアップから復元しました")
                                except Exception as e:
                                    logging.error(f"バックアップからの復元エラー: {e}")
                            
                            # m3u8が存在せず、他の問題が見つかった場合
                            if not m3u8_exists:
                                # カウンターを増加
                                if camera_id not in issue_counts:
                                    issue_counts[camera_id] = 0
                                issue_counts[camera_id] += 1
                                
                                # 複数回連続で問題が検出された場合
                                if issue_counts[camera_id] >= 2:
                                    logging.warning(f"カメラ {camera_id} でm3u8ファイルが複数回見つからないため、ストリームを再起動します")
                                    # 問題カウンターをリセット
                                    issue_counts[camera_id] = 0
                                    # ストリームを再起動
                                    restart_camera_stream(camera_id)
                            else:
                                # 問題がなければカウンターをリセット
                                if camera_id in issue_counts:
                                    issue_counts[camera_id] = 0
                        except Exception as e:
                            logging.error(f"カメラ {camera_id} の軽量チェック中にエラー: {e}")
            
            # システム全体の状態確認（30秒ごとに実行）
            try:
                # 未使用のtsファイルのクリーンアップ
                if (current_time - last_full_check) > 30:
                    logging.info("ストリーミングセグメントのクリーンアップを実行中")
                    scheduled_cleanup()
            except Exception as e:
                logging.error(f"定期クリーンアップ中にエラー: {e}")
            
            # 監視間隔
            time.sleep(15)
        
        except Exception as e:
            logging.error(f"グローバルヘルスモニターでエラーが発生: {e}")
            time.sleep(30)  # エラー発生時は少し長めに待機

def cleanup_scheduler():
    """
    定期的なクリーンアップタスクを実行
    """
    while True:
        try:
            logging.info("Running scheduled cleanup")
            
            # 各カメラについて古いセグメントファイルを削除
            for camera_id in list(streaming_processes.keys()):
                cleanup_old_segments(camera_id)
            
            # ディスク使用量の確認
            disk_ok = fs_utils.check_disk_space(config.TMP_PATH, min_free_space_gb=2)
            if not disk_ok:
                logging.warning("Low disk space detected. Performing thorough cleanup.")
                # より積極的なクリーンアップを実行
                for camera_id in list(streaming_processes.keys()):
                    cleanup_old_segments(camera_id)
            
            # 次の実行まで待機
            time.sleep(config.CLEANUP_INTERVAL)
            
        except Exception as e:
            logging.error(f"Error in cleanup scheduler: {e}")
            time.sleep(60)  # エラー時は1分待機

def monitor_system_resources():
    """
    システムリソースの使用状況を監視
    """
    global system_resources
    
    while True:
        try:
            # リソース情報を更新
            cpu_percent = psutil.cpu_percent(interval=1)
            memory_percent = psutil.virtual_memory().percent
            
            system_resources = {
                'cpu': cpu_percent,
                'memory': memory_percent
            }
            
            # CPUまたはメモリが危険なレベルの場合
            if cpu_percent > 90 or memory_percent > 90:
                logging.warning(f"Critical system resources: CPU {cpu_percent}%, Memory {memory_percent}%")
                
                # 一部のプロセスを停止して負荷を減らす
                if len(streaming_processes) > 5:
                    logging.warning("Temporarily stopping some streaming processes to reduce load")
                    
                    # プロセスの一部（最大5つ）を停止
                    count = 0
                    for camera_id in list(streaming_processes.keys()):
                        if count >= 5:
                            break
                            
                        logging.info(f"Temporarily stopping streaming for camera {camera_id} due to high system load")
                        cleanup_camera_resources(camera_id)
                        count += 1
                        
                        # 少し待ってリソース使用量の変化を確認
                        time.sleep(5)
                        
                        cpu_current = psutil.cpu_percent(interval=1)
                        if cpu_current < 70:
                            logging.info(f"System resources improved: CPU {cpu_current}%")
                            break
            
            # 次の確認まで待機
            time.sleep(config.RESOURCE_CHECK_INTERVAL)
            
        except Exception as e:
            logging.error(f"Error monitoring system resources: {e}")
            time.sleep(30)  # エラー時は30秒待機

def stop_all_streaming():
    """
    すべてのストリーミングプロセスを停止
    """
    global active_streams_count
    logging.info("Stopping all streaming processes")
    
    # 各プロセスを停止
    for camera_id, process in list(streaming_processes.items()):
        try:
            logging.info(f"Stopping streaming for camera {camera_id}")
            ffmpeg_utils.terminate_process(process)
            # cleanup_camera_resources(camera_id)  # リソースのクリーンアップ
        except Exception as e:
            logging.error(f"Error stopping streaming for camera {camera_id}: {e}")
    
    # 再初期化
    streaming_processes.clear()
    hls_last_update.clear()
    m3u8_last_size.clear()
    restart_counts.clear()
    
    # 残っているプロセスを強制終了
    try:
        ffmpeg_utils.kill_ffmpeg_processes()
    except Exception as e:
        logging.error(f"Error killing remaining ffmpeg processes: {e}")
    
    with streaming_lock:
        active_streams_count = 0
    
    logging.info("All streaming processes stopped")
    return True

def initialize_streaming():
    """
    ストリーミング機能を初期化して、すべてのカメラのストリーミングを自動的に開始する
    """
    logging.info("Initializing streaming module")
    
    # ディレクトリの存在を確認
    fs_utils.ensure_directory_exists(config.TMP_PATH)
    
    # 残っているffmpegプロセスをクリーンアップ
    ffmpeg_utils.kill_ffmpeg_processes()
    
    # 各カメラのディレクトリを準備
    cameras = camera_utils.read_config()
    for camera in cameras:
        camera_dir = os.path.join(config.TMP_PATH, camera['id'])
        fs_utils.ensure_directory_exists(camera_dir)
    
    logging.info("Streaming module initialized")
    
    # ストリーミングワーカースレッドを開始
    start_streaming_workers()
    
    # 少し待機してからすべてのカメラのストリーミングを開始
    time.sleep(2)
    
    # すべてのカメラのストリーミングを開始
    start_all_cameras_streaming(cameras)
    
    return True

def start_all_cameras_streaming(cameras=None):
    """
    すべてのカメラのストリーミングを開始する
    
    Args:
        cameras (list, optional): カメラ情報のリスト。指定されない場合は設定から読み込む
        
    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        if cameras is None:
            cameras = camera_utils.read_config()
        
        if not cameras:
            logging.warning("No cameras found in configuration")
            return False
        
        logging.info(f"Starting streaming for {len(cameras)} cameras")
        
        # 各カメラのストリーミングをスレッドで並列起動
        threads = []
        for camera in cameras:
            logging.info(f"Queueing streaming start for camera {camera['id']} ({camera['name']})")
            t = threading.Thread(target=get_or_start_streaming, args=(camera,))
            t.start()
            threads.append(t)
        # 全スレッドの完了を待つ（厳密な同時起動を目指す場合）
        for t in threads:
            t.join(timeout=2)
        return True
        
    except Exception as e:
        logging.error(f"Error starting all cameras streaming: {e}")
        return False

def scheduled_cleanup():
    """
    定期的なクリーンアップ処理を実行する関数
    
    Returns:
        bool: クリーンアップが成功したかどうか
    """
    try:
        logging.info("定期クリーンアップ処理を実行中...")
        
        # 各カメラディレクトリ内の古いtsファイルを削除
        for camera_id in streaming_processes.keys():
            try:
                camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                if os.path.exists(camera_tmp_dir):
                    cleanup_old_segments(camera_id)
            except Exception as e:
                logging.error(f"カメラ {camera_id} のクリーンアップエラー: {e}")
        
        return True
    except Exception as e:
        logging.error(f"定期クリーンアップ処理エラー: {e}")
        return False
