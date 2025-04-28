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
    
    logging.info("Streaming workers and resource monitor started")

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

        hls_path = os.path.join(camera_tmp_dir, f"{camera['id']}.m3u8").replace('/', '\\')
        log_path = os.path.join(camera_tmp_dir, f"{camera['id']}.log").replace('/', '\\')

        if os.path.exists(hls_path):
            os.remove(hls_path)

        # 既存のffmpegプロセスが残っている場合、強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera['id'])
        time.sleep(1)  # プロセス終了待ち

        # RTSPストリームの接続確認
        if not ffmpeg_utils.check_rtsp_connection(camera['rtsp_url'], timeout=config.RTSP_TIMEOUT):
            logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
            # 接続に失敗しても続行する - 後でリトライするため

        # Nginx用に最適化されたHLSセグメントパス
        segment_path = os.path.join(camera_tmp_dir, f"{camera['id']}_%03d.ts").replace('/', '\\')
        
        # HLSストリーミング用FFmpegコマンド生成
        ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
            camera['rtsp_url'], 
            hls_path,
            segment_path,
            segment_time=config.HLS_SEGMENT_TIME,
            list_size=config.HLS_LIST_SIZE
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

        logging.info(f"Started streaming for camera {camera['id']}")
        return True

    except Exception as e:
        logging.error(f"Error starting streaming for camera {camera['id']}: {e}")
        return False

def monitor_system_resources():
    """
    システムリソースを監視する
    """
    global system_resources
    
    while True:
        try:
            # CPU使用率を取得
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # メモリ使用率を取得
            mem = psutil.virtual_memory()
            mem_percent = mem.percent
            
            # リソース情報を更新
            system_resources = {
                'cpu': cpu_percent,
                'memory': mem_percent
            }
            
            # ログに記録（情報レベル）
            if cpu_percent > 70 or mem_percent > 70:
                logging.warning(f"System resources high: CPU {cpu_percent}%, Memory {mem_percent}%, Active streams: {active_streams_count}")
            else:
                logging.info(f"System resources: CPU {cpu_percent}%, Memory {mem_percent}%, Active streams: {active_streams_count}")
            
            # 一定間隔で監視
            time.sleep(config.RESOURCE_CHECK_INTERVAL)
            
        except Exception as e:
            logging.error(f"Error monitoring system resources: {e}")
            time.sleep(60)  # エラー時は長めのインターバルを設ける

def restart_streaming(camera_id):
    """
    特定カメラのストリーミングを再起動する

    Args:
        camera_id (str): 再起動するカメラID
    
    Returns:
        bool: 操作が成功したかどうか
    """
    global active_streams_count
    
    try:
        logging.warning(f"Restarting streaming for camera {camera_id}")
        
        # 既存のffmpegプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        
        # ストリーミングプロセスを削除
        if camera_id in streaming_processes:
            del streaming_processes[camera_id]
            with streaming_lock:
                if active_streams_count > 0:
                    active_streams_count -= 1
        
        # カメラ設定を読み込んでストリーミングを再開
        camera = camera_utils.get_camera_by_id(camera_id)
        if camera:
            # キューに追加
            streaming_queue.put(camera)
            logging.info(f"Added camera {camera_id} to streaming queue for restart")
            return True
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

    # 全カメラ分のストリーミングをキューに投入
    cameras = camera_utils.read_config()
    for camera in cameras:
        get_or_start_streaming(camera)
