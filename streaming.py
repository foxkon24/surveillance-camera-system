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
import traceback

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
HEALTH_CHECK_INTERVAL = 10  # 監視強化のため短縮
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 10  # 安定性のため延長
# ストリーミング再起動回数を記録
restart_counts = {}
# ストリーミング再起動の最大回数（これを超えるとより長い時間待機する）
MAX_RESTART_COUNT = 5
# ストリーミング再起動後の待機時間（秒）
RESTART_COOLDOWN = 30
# ストリーミング更新チェックの間隔（秒）
STREAMING_CHECK_INTERVAL = 3.0  # 安定性向上のため延長
# 新しいTSファイル形式に対応するための変数
MAX_NO_UPDATE_COUNT = 5  # 増加
# ファイル更新待機最大時間（秒）
MAX_UPDATE_WAIT_TIME = 10.0  # 延長
# TSファイル生成タイムアウト（秒）
TS_CREATION_TIMEOUT = 10.0  # 延長
# HLSファイル生成タイムアウト（秒）
HLS_CREATION_TIMEOUT = 30.0
# 連続で更新なしと判断される最大回数を増加（より許容的に）
MAX_CONSECUTIVE_NO_UPDATES = 3  # 増加
# RTSP接続タイムアウト（秒）
RTSP_CONNECTION_TIMEOUT = 15  # 延長
# カメラプロセスの辞書
camera_processes = {}

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    global active_streams_count
    
    # enabled=1以外は絶対にストリーミングしない
    if camera.get('enabled', 1) != 1:
        logging.info(f"カメラ {camera.get('id', 'unknown')} は無効設定のためストリーミングを開始しません")
        return False
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

        # カメラ固有のプロセスのみ終了（より高速な起動のため）
        ffmpeg_utils.kill_ffmpeg_processes(camera_id=camera['id'])
        time.sleep(0.5)  # プロセス終了を待つ時間を増加

        # カメラIDディレクトリが存在しない場合のみ作成
        if not os.path.exists(camera_tmp_dir):
            os.makedirs(camera_tmp_dir, exist_ok=True)
            logging.info(f"カメラディレクトリを作成しました: {camera_tmp_dir}")

        # RTSP URLのデバッグ出力 (認証情報は隠す)
        safe_rtsp_url = camera['rtsp_url']
        if "@" in safe_rtsp_url:
            # 認証情報を隠す
            parts = safe_rtsp_url.split("@", 1)
            protocol_auth = parts[0].split("://", 1)
            safe_rtsp_url = f"{protocol_auth[0]}://***:***@{parts[1]}"
        logging.info(f"カメラ {camera['id']} のRTSP URL: {safe_rtsp_url}")
        
        # 共通のセグメント時間とバッファサイズ（すべてのカメラに統一）
        hls_segment_time = 2
        ffmpeg_buffer_size = "32768k"  # 適切なバッファサイズ

        # FFmpegコマンドを統一関数で生成
        ffmpeg_cmd = ffmpeg_utils.get_hls_streaming_command(
            camera['rtsp_url'],
            hls_path,
            segment_time=hls_segment_time,
            buffer_size=ffmpeg_buffer_size
        )

        # FFmpegコマンドのデバッグ出力 (認証情報は隠す)
        debug_cmd = ffmpeg_cmd.copy()
        for i, arg in enumerate(debug_cmd):
            if arg == '-i' and i+1 < len(debug_cmd) and '@' in debug_cmd[i+1]:
                parts = debug_cmd[i+1].split('@')
                protocol_auth = parts[0].split('://')
                debug_cmd[i+1] = f"{protocol_auth[0]}://***:***@{parts[1]}"
                
        # コマンドの出力
        logging.info(f"FFmpeg command: {' '.join(debug_cmd)}")

        # 既存のTSファイルをクリアして新鮮な状態で開始
        try:
            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
            for ts_file in ts_files:
                try:
                    os.remove(os.path.join(camera_tmp_dir, ts_file))
                except:
                    pass  # 削除に失敗しても続行
        except Exception as e:
            logging.warning(f"TSファイルのクリーンアップに失敗: {e}")

        # FFmpegプロセスを最高優先度で開始（独立ログファイルを使用）
        process = ffmpeg_utils.start_ffmpeg_process(
            ffmpeg_cmd, 
            log_path=log_path,  # カメラごとに独立したログファイル
            high_priority=True,  # 高優先度で実行
            show_error=True  # エラー出力を詳細に表示
        )
        
        # プロセスの状態を確認（短時間で）
        time.sleep(1.0)  # プロセスの起動を待つ時間を延長
        if process is None or process.poll() is not None:
            return_code = process.poll() if process else "プロセス作成失敗"
            logging.error(f"FFmpegプロセスの起動に失敗しました。終了コード: {return_code}")
            
            # ログファイルから詳細情報を取得
            error_output = "ログ情報なし"
            if os.path.exists(log_path):
                try:
                    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                        error_output = f.read()
                        # エラーログ全体を記録
                        logging.error(f"FFmpegエラー出力: {error_output}")
                except Exception as e:
                    logging.error(f"ログファイル読み取りエラー: {e}")
            
            # RTSPストリームが接続できるか確認
            logging.info(f"RTSPストリームの接続確認を試みます: {safe_rtsp_url}")
            try:
                success, error_msg = ffmpeg_utils.check_rtsp_connection(camera['rtsp_url'], timeout=10)
                if success:
                    logging.info(f"RTSPストリームは接続可能です。別の問題が考えられます。")
                else:
                    logging.error(f"RTSPストリームに接続できません: {error_msg}")
            except Exception as e:
                logging.error(f"RTSP接続確認中にエラーが発生: {e}")
            
            cleanup_camera_resources(camera['id'])
            return False

        # プロセス情報を設定
        streaming_processes[camera['id']] = process
        
        # 初期化時点で更新情報を記録
        hls_last_update[camera['id']] = time.time()
        if os.path.exists(hls_path):
            m3u8_last_size[camera['id']] = os.path.getsize(hls_path)
        else:
            m3u8_last_size[camera['id']] = 0

        # 再起動カウンターの初期化/リセット
        restart_counts[camera['id']] = 0

        # m3u8ファイルの生成を待機（タイムアウト短縮）
        m3u8_created = False
        max_wait_time = 8  # 最大待機時間を短縮（秒）
        start_wait_time = time.time()
        wait_interval = 0.1  # 確認間隔をさらに短く
        
        logging.info(f"カメラ {camera['id']} のm3u8ファイル生成を待機中...")
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
                
                logging.error(f"m3u8待機中にFFmpegプロセスが終了しました。終了コード: {return_code}")
                logging.error(f"FFmpegエラー出力: {error_output[:500]}...")
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
                            # TSファイルが作成されるまで待機
                            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                            if ts_files:
                                m3u8_created = True
                                time_taken = time.time() - start_wait_time
                                logging.info(f"カメラ {camera['id']} のHLSプレイリストとTSファイルが {time_taken:.1f}秒後に作成されました")
                                # ファイルを確実に更新するために一度コピーを作成
                                with open(hls_path, 'r') as src:
                                    m3u8_content = src.read()
                                # 一時的なバックアップファイルを作成（異常時の回復用）
                                backup_path = os.path.join(camera_tmp_dir, f"{camera['id']}_backup.m3u8")
                                with open(backup_path, 'w') as dst:
                                    dst.write(m3u8_content)
                                logging.info(f"カメラ {camera['id']} のm3u8バックアップファイルを作成しました")
                                break
                except Exception as e:
                    logging.warning(f"m3u8ファイル確認中にエラー: {e}")
            
            time.sleep(wait_interval)
        
        # TSファイルがまだなくても短時間でプロセスを開始（監視スレッドが後で確認）
        if not m3u8_created and time.time() - start_wait_time >= 5:
            logging.warning(f"カメラ {camera['id']} のHLSプレイリストファイルが時間内に作成されませんでした。監視スレッドで確認を続けます")
        
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
            name=f"hls-monitor-{camera['id']}"
        )
        hls_monitor_thread.start()

        logging.info(f"カメラ {camera['id']} のストリーミングプロセスを正常に開始しました")
        return True

    except Exception as e:
        error_traceback = traceback.format_exc()
        logging.error(f"カメラ {camera['id']} のストリーミングプロセス開始中にエラーが発生: {e}")
        logging.error(f"詳細なエラー情報: {error_traceback}")
        cleanup_camera_resources(camera['id'])
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
    ストリーミングプロセスの健全性をモニタリングする関数

    Args:
        camera_id (str): カメラID
        process (subprocess.Popen): 監視するプロセス
    """
    global active_streams_count
    
    logging.info(f"カメラ {camera_id} のHLS監視スレッドを開始しました")
    
    try:
        while camera_id in streaming_processes:
            try:
                # プロセスがまだ実行中かチェック
                if process.poll() is not None:
                    logging.warning(f"カメラ {camera_id} のストリーミングプロセスが終了しました (返却コード: {process.returncode})")
                    # クリーンアップして再起動準備
                    cleanup_camera_resources(camera_id)
                    
                    # すべてのカメラに共通の再起動遅延を設定
                    restart_delay = RESTART_COOLDOWN
                    logging.info(f"カメラ {camera_id} に対して再起動遅延を適用します: {restart_delay}秒")
                    
                    # 少し待ってから再起動を試みる
                    time.sleep(restart_delay)
                    
                    # 再起動をキューに入れる
                    restart_camera_stream(camera_id)
                    break
                
                # HLSファイルの更新チェック
                if not check_hls_file_health(camera_id):
                    # ファイルが更新されていない場合、次のチェックまで待機
                    wait_time = STREAMING_CHECK_INTERVAL
                    
                    time.sleep(wait_time)
                    continue
                
                # 一定間隔待機
                time.sleep(STREAMING_CHECK_INTERVAL)
                
            except Exception as e:
                logging.error(f"カメラ {camera_id} の監視中にエラーが発生しました: {e}")
                time.sleep(STREAMING_CHECK_INTERVAL)
    
    except Exception as e:
        logging.error(f"カメラ {camera_id} の監視スレッドでエラーが発生しました: {e}")
    
    finally:
        logging.info(f"カメラ {camera_id} の監視スレッドを終了しました")
        # もしプロセスがまだ終了していなければ終了させる
        try:
            if camera_id in streaming_processes and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        except:
            pass
        # ストリーミングプロセスをクリーンアップして再ストリーミングができるようにする
        cleanup_camera_resources(camera_id)

def check_hls_file_health(camera_id):
    """
    HLSファイルの健全性をチェック
    """
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, str(camera_id))
        m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
        
        if not os.path.exists(m3u8_path):
            return False
            
        # ファイルの更新時刻をチェック
        current_time = time.time()
        mod_time = os.path.getmtime(m3u8_path)
        
        if current_time - mod_time > HLS_UPDATE_TIMEOUT:  # 3秒以上更新がない
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
        
        # カメラディレクトリ内のtsファイルをチェック
        ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
        if not ts_files:
            return False
            
        # 最新のTSファイルの更新時刻をチェック
        latest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
        latest_ts_path = os.path.join(camera_tmp_dir, latest_ts)
        latest_ts_mtime = os.path.getmtime(latest_ts_path)
        
        # 最新のTSファイルが3秒以上更新されていないかチェック
        if current_time - latest_ts_mtime > HLS_UPDATE_TIMEOUT:
            return False
            
        return True
        
    except Exception as e:
        logging.error(f"HLSファイルの健全性チェック中にエラーが発生しました: {e}")
        return False

def monitor_hls_updates(camera_id):
    """
    HLSファイルの更新を監視するスレッド

    Args:
        camera_id (str): 監視するカメラID
    """
    try:
        logging.info(f"カメラ {camera_id} のHLS監視スレッドを開始しました")
        
        # 初期化
        last_check_time = time.time()
        last_m3u8_size = 0
        last_ts_time = 0
        last_update_time = time.time()
        consecutive_no_updates = 0
        
        while camera_id in streaming_processes:
            try:
                current_time = time.time()
                # 監視間隔ごとにチェック
                if current_time - last_check_time >= STREAMING_CHECK_INTERVAL:
                    last_check_time = current_time
                    
                    # M3U8ファイルの確認
                    camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                    m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
                    
                    if os.path.exists(m3u8_path):
                        # M3U8ファイルのサイズをチェック
                        current_m3u8_size = os.path.getsize(m3u8_path)
                        
                        # TSファイルのチェック
                        ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                        ts_files_exist = len(ts_files) > 0
                        
                        if ts_files_exist:
                            # 最新のTSファイルの更新時間を取得
                            latest_ts = sorted(
                                [os.path.join(camera_tmp_dir, f) for f in ts_files],
                                key=os.path.getmtime, 
                                reverse=True
                            )[0]
                            current_ts_time = os.path.getmtime(latest_ts)
                            
                            # 更新ありと判断するケース
                            if current_m3u8_size != last_m3u8_size or current_ts_time > last_ts_time:
                                last_update_time = current_time
                                last_m3u8_size = current_m3u8_size
                                last_ts_time = current_ts_time
                                consecutive_no_updates = 0
                            else:
                                # 更新なしのカウント
                                consecutive_no_updates += 1
                                
                                # 3秒以上更新がない場合は即座にリセット
                                if current_time - last_update_time > MAX_UPDATE_WAIT_TIME:
                                    logging.warning(f"カメラ {camera_id} で問題を検出: TSファイルが {round(current_time - last_update_time, 1)}秒間更新されていません")
                                    logging.warning(f"カメラ {camera_id} の問題が検出されたため、ストリームを即座に再起動します")
                                    restart_camera_stream(camera_id)
                                    return  # 再起動したので監視スレッドを終了
                        else:
                            # TSファイルがない場合も問題とみなす
                            if current_time - last_update_time > MAX_UPDATE_WAIT_TIME:
                                logging.warning(f"カメラ {camera_id} で問題を検出: TSファイルが存在しません")
                                logging.warning(f"カメラ {camera_id} の問題が検出されたため、ストリームを即座に再起動します")
                                restart_camera_stream(camera_id)
                                return  # 再起動したので監視スレッドを終了
                            
                # スリープして負荷軽減
                time.sleep(0.5)
                
            except Exception as e:
                logging.error(f"カメラ {camera_id} の監視中にエラーが発生: {str(e)}")
                # 例外が発生しても監視を続行
                time.sleep(1)
        
        logging.info(f"カメラ {camera_id} のHLS監視スレッドを終了します")
    except Exception as e:
        logging.error(f"カメラ {camera_id} のHLS監視スレッドでエラーが発生: {str(e)}")

def restart_camera_stream(camera_id):
    """
    カメラストリームを再起動する

    Args:
        camera_id (str): カメラID
        
    Returns:
        bool: 再起動に成功したかどうか
    """
    global restart_counts
    
    try:
        # 再起動回数をインクリメント
        if camera_id in restart_counts:
            restart_counts[camera_id] += 1
        else:
            restart_counts[camera_id] = 1
        
        count = restart_counts[camera_id]
        logging.info(f"カメラ {camera_id} のストリーミングを再起動しています (試行 {count}/{MAX_RESTART_COUNT})")
        
        # 再起動回数が多すぎる場合は長めの冷却時間を設ける
        if count > MAX_RESTART_COUNT:
            cooling_time = RESTART_COOLDOWN * 2
            logging.warning(f"カメラ {camera_id} の再起動回数が多すぎます。{cooling_time}秒間待機します")
            time.sleep(cooling_time)
            # 再起動カウントをリセット
            restart_counts[camera_id] = 1
        
        # プロセスの終了を確認
        if camera_id in streaming_processes:
            process = streaming_processes[camera_id]
            try:
                if process and process.poll() is None:
                    # まだ実行中なら強制終了
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            except:
                pass
            
            # リソース解放
            cleanup_camera_resources(camera_id)
        
        # ストリーミングプロセスから削除
        with streaming_lock:
            if camera_id in streaming_processes:
                cleanup_camera_resources(camera_id)
        
        # 標準の待機時間
        time.sleep(1)
        
        # 対応するカメラ情報を取得
        camera_info = camera_utils.get_camera_by_id(camera_id)
        # enabled=1以外は絶対にストリーミングしない
        if not camera_info or camera_info.get('enabled', 1) != 1:
            logging.info(f"カメラ {camera_id} は無効設定のためストリーミングを再起動しません")
            return False
        
        # ストリーミングキューに追加して再起動
        streaming_queue.put(camera_info)
        logging.info(f"カメラ {camera_id} を再起動キューに入れました")
        return True
        
    except Exception as e:
        logging.error(f"カメラ {camera_id} の再起動中にエラーが発生しました: {e}")
        return False

def cleanup_camera_resources(camera_id):
    """
    指定されたカメラのリソースをクリーンアップ

    Args:
        camera_id (str): クリーンアップするカメラID
    """
    global active_streams_count
    try:
        logging.info(f"Cleaning up resources for camera {camera_id}")
        # ストリーミングキャッシュから削除
        with streaming_lock:
            if camera_id in streaming_processes:
                process = streaming_processes[camera_id]
                try:
                    # プロセスを停止
                    ffmpeg_utils.terminate_process(process)
                except:
                    pass
                del streaming_processes[camera_id]
                # アクティブストリーム数を必ず減算
                active_streams_count = max(0, active_streams_count - 1)
                logging.info(f"カメラ {camera_id} のストリーミングプロセスを削除しました。アクティブストリーム: {active_streams_count}")
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
                # 強制削除が指定されている場合、すべてのtsファイルを削除
                for ts_file in ts_files:
                    try:
                        os.remove(os.path.join(camera_tmp_dir, ts_file))
                        logging.info(f"強制削除: {ts_file}")
                    except Exception as del_err:
                        logging.error(f"ファイル削除エラー: {del_err}")
            return
        
        # m3u8ファイルが存在する場合、現在使用中のtsファイルを取得
        if os.path.exists(m3u8_path):
            try:
                with open(m3u8_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.endswith('.ts'):
                            # セグメントファイル名を抽出
                            segment_file = os.path.basename(line)
                            active_segments.add(segment_file)
            except Exception as e:
                logging.error(f"m3u8ファイル読み取りエラー: {e}")
                return  # エラーの場合は削除を中止
        
        # m3u8に含まれていない古いtsファイルを削除
        deleted_count = 0
        for ts_file in ts_files:
            if ts_file not in active_segments:
                try:
                    ts_file_path = os.path.join(camera_tmp_dir, ts_file)
                    
                    # 新しいTSファイル形式に対応（%Y%m%d%H%M%S-%d.ts）
                    # 1分以上経過したファイルのみ削除するように時間チェックを追加
                    file_mtime = os.path.getmtime(ts_file_path)
                    if time.time() - file_mtime > 60:  # 1分以上前のファイル
                        os.remove(ts_file_path)
                        deleted_count += 1
                except Exception as del_err:
                    logging.error(f"古いTSファイル削除エラー: {del_err}")
        
        if deleted_count > 0:
            logging.info(f"カメラ {camera_id} の古いTSファイル {deleted_count} 個を削除しました")
    
    except Exception as e:
        logging.error(f"古いセグメント削除エラー（カメラ {camera_id}）: {e}")

def global_health_monitor():
    """
    すべてのストリーミングカメラの健全性を監視する
    404エラーが多発する場合、自動的に再起動する
    """
    try:
        logging.info("Global health monitor started")
        
        # カメラごとの連続404エラーをカウントする辞書
        error_counts = {}
        # 最後のm3u8確認時刻を記録する辞書
        last_check_times = {}
        
        while True:
            try:
                # アクティブなストリーミングがない場合はスキップ
                if not streaming_processes:
                    time.sleep(HEALTH_CHECK_INTERVAL)
                    continue
                
                # 現在の時刻を記録
                current_time = time.time()
                
                # すべてのカメラプロセスをチェック
                for camera_id, process in list(streaming_processes.items()):
                    try:
                        # プロセスが終了しているか確認
                        if process is None or process.poll() is not None:
                            logging.warning(f"カメラ {camera_id} のプロセスが存在しないか終了しています (PID: {process.pid if process else 'None'})")
                            
                            # エラーカウントを増やす
                            error_counts[camera_id] = error_counts.get(camera_id, 0) + 1
                            
                            # 一定回数以上エラーが発生したら再起動
                            if error_counts[camera_id] >= 3:
                                logging.error(f"カメラ {camera_id} のプロセスが繰り返し終了しています。再起動します。")
                                restart_camera_stream(camera_id)
                                # カウントをリセット
                                error_counts[camera_id] = 0
                            
                            continue
                        
                        # HLSファイルの存在と更新時間を確認
                        m3u8_file = os.path.join(config.TMP_PATH, camera_id, f"{camera_id}.m3u8")
                        if not os.path.exists(m3u8_file):
                            logging.warning(f"カメラ {camera_id} のm3u8ファイルが見つかりません")
                            
                            # 前回のチェック時刻を取得
                            last_check = last_check_times.get(camera_id, 0)
                            
                            # 長時間ファイルが存在しない場合
                            if last_check > 0 and (current_time - last_check) > HLS_UPDATE_TIMEOUT * 2:
                                logging.error(f"カメラ {camera_id} のm3u8ファイルが {HLS_UPDATE_TIMEOUT * 2}秒以上存在しません。再起動します。")
                                restart_camera_stream(camera_id)
                                # 最終チェック時刻をリセット
                                last_check_times[camera_id] = current_time
                            else:
                                # 初回または短時間の場合は記録
                                last_check_times[camera_id] = last_check_times.get(camera_id, current_time)
                            
                            continue
                        
                        # 最終更新時刻を確認
                        try:
                            mtime = os.path.getmtime(m3u8_file)
                            if current_time - mtime > HLS_UPDATE_TIMEOUT:
                                logging.warning(f"カメラ {camera_id} のm3u8ファイルが {current_time - mtime:.1f}秒間更新されていません")
                                # 一定時間以上更新がなければ再起動
                                if current_time - mtime > HLS_UPDATE_TIMEOUT * 2:
                                    logging.error(f"カメラ {camera_id} のm3u8ファイルが {HLS_UPDATE_TIMEOUT * 2}秒以上更新されていません。再起動します。")
                                    restart_camera_stream(camera_id)
                            else:
                                # 正常な場合はエラーカウントをリセット
                                if camera_id in error_counts:
                                    error_counts[camera_id] = 0
                                if camera_id in last_check_times:
                                    last_check_times[camera_id] = current_time
                        
                        except Exception as e:
                            logging.error(f"カメラ {camera_id} のm3u8ファイル確認中にエラー: {e}")
                    
                    except Exception as e:
                        logging.error(f"カメラ {camera_id} の健全性確認中にエラー: {e}")
                
                # プロセスが終了しているが、まだ辞書に残っているものを検出
                for camera_id, process in list(streaming_processes.items()):
                    if process and process.poll() is not None:
                        logging.warning(f"カメラ {camera_id} のプロセスが終了しているのに記録が残っています。クリーンアップします。")
                        # すでに終了している場合はクリーンアップ
                        cleanup_camera_resources(camera_id)
                        # ストリーミングプロセス辞書から削除
                        with streaming_lock:
                            if camera_id in streaming_processes:
                                del streaming_processes[camera_id]
                                global active_streams_count
                                active_streams_count = max(0, active_streams_count - 1)
                
                # 長時間停止したカメラを検出して自動起動
                try:
                    all_cameras = camera_utils.get_enabled_cameras()
                    active_camera_ids = set(streaming_processes.keys())
                    
                    for camera in all_cameras:
                        camera_id = camera['id']
                        # アクティブでないカメラを見つけた場合
                        if camera_id not in active_camera_ids:
                            logging.info(f"カメラ {camera_id} が現在ストリーミングされていないため、起動を試みます")
                            # 非アクティブカメラをキューに追加
                            get_or_start_streaming(camera)
                except Exception as e:
                    logging.error(f"非アクティブカメラの検出中にエラー: {e}")
                
                # 間隔を空けて次の健全性チェック
                time.sleep(HEALTH_CHECK_INTERVAL)
                
            except Exception as e:
                logging.error(f"全体的な健全性監視中にエラー: {e}")
                time.sleep(HEALTH_CHECK_INTERVAL)
    
    except Exception as e:
        logging.error(f"健全性監視スレッドでエラーが発生: {e}")
        logging.error(traceback.format_exc())

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
    cameras = camera_utils.get_enabled_cameras()
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
            cameras = camera_utils.get_enabled_cameras()
        
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

def start_hls_streaming(camera_id):
    """
    HLSストリーミングを開始
    
    Args:
        camera_id (str): カメラID
        
    Returns:
        bool: 開始に成功したかどうか
    """
    global streaming_processes, active_streams_count
    
    try:
        # カメラIDが無効な場合はエラー
        if not camera_id:
            logging.error("無効なカメラID: カメラIDが指定されていません")
            return False
        
        # カメラ情報の取得
        camera_info = camera_utils.get_camera_by_id(camera_id)
        # enabled=1以外は絶対にストリーミングしない
        if not camera_info or camera_info.get('enabled', 1) != 1:
            logging.info(f"カメラ {camera_id} は無効設定のためHLSストリーミングを開始しません")
            return False
        if not camera_info:
            logging.error(f"カメラID {camera_id} の情報が見つかりません")
            return False
        
        rtsp_url = camera_info.get('rtsp_url')
        if not rtsp_url:
            logging.error(f"カメラID {camera_id} のRTSP URLが設定されていません")
            return False
        
        # 既存のストリーミングを停止
        if camera_id in streaming_processes:
            terminate_streaming(camera_id)
            time.sleep(1)  # 少し待ってから再開
        
        # 出力ディレクトリの準備
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        os.makedirs(camera_tmp_dir, exist_ok=True)
        
        # M3U8出力パス
        m3u8_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
        
        # 競合の防止のため、既存のm3u8ファイルを削除
        if os.path.exists(m3u8_path):
            try:
                os.remove(m3u8_path)
                logging.info(f"既存のm3u8ファイルを削除しました: {m3u8_path}")
            except Exception as e:
                logging.warning(f"m3u8ファイル削除エラー: {e}")
        
        # ログファイルパス
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        log_path = os.path.join(config.BASE_PATH, 'log', f'hls_{camera_id}_{timestamp}.log')
        
        # 書き込みテストを実行
        try:
            test_file_path = os.path.join(camera_tmp_dir, "write_test.txt")
            with open(test_file_path, 'w') as f:
                f.write("書き込みテスト")
            os.remove(test_file_path)
            logging.info("書き込みテスト成功")
        except Exception as e:
            logging.error(f"書き込みテストエラー: {e}")
            return False
        
        # FFmpegコマンドの生成
        command = ffmpeg_utils.get_hls_streaming_command(
            rtsp_url,
            m3u8_path,
            segment_time=config.HLS_SEGMENT_DURATION
        )
        
        # FFmpegプロセスを起動
        process = ffmpeg_utils.start_ffmpeg_process(
            command,
            log_path=log_path,
            high_priority=True
        )
        
        # プロセスの実行確認
        if process is None or process.poll() is not None:
            logging.error(f"FFmpegプロセスの起動に失敗しました（カメラ {camera_id}）")
            return False
        
        # グローバル変数に保存
        streaming_processes[camera_id] = process
        active_streams_count += 1
        
        # M3U8ファイルの生成を待つ
        m3u8_wait_count = 0
        while not os.path.exists(m3u8_path) and m3u8_wait_count < 30:  # 最大30秒待機
            logging.info(f"カメラ {camera_id} のm3u8ファイル生成を待機中...")
            time.sleep(1)
            m3u8_wait_count += 1
            
            # プロセスが途中で終了していないか確認
            if process.poll() is not None:
                exit_code = process.poll()
                logging.error(f"m3u8待機中にFFmpegプロセスが終了しました（終了コード: {exit_code}）")
                
                # エラー出力を確認
                try:
                    if os.path.exists(log_path):
                        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                            error_output = f.read()
                            logging.error(f"FFmpegエラー出力: {error_output}")
                except Exception as log_err:
                    logging.error(f"ログファイル読み取りエラー: {log_err}")
                
                # プロセスを無効化
                streaming_processes.pop(camera_id, None)
                active_streams_count = max(0, active_streams_count - 1)
                return False
        
        # M3U8ファイルが生成されたかを確認
        if not os.path.exists(m3u8_path):
            logging.error(f"m3u8ファイルが生成されませんでした: {m3u8_path}")
            # プロセスを終了
            ffmpeg_utils.terminate_process(process)
            streaming_processes.pop(camera_id, None)
            active_streams_count = max(0, active_streams_count - 1)
            return False
        
        # 監視スレッドを開始
        monitor_thread = threading.Thread(
            target=monitor_hls_updates,
            args=(camera_id,),
            daemon=True,
            name=f"hls-monitor-{camera_id}"
        )
        monitor_thread.start()
        
        logging.info(f"カメラ {camera_id} のHLSストリーミングを開始しました（PID: {process.pid}）")
        return True
        
    except Exception as e:
        logging.error(f"HLSストリーミング開始エラー（カメラ {camera_id}）: {e}")
        # 例外発生時は、プロセスがあれば終了
        if camera_id in streaming_processes:
            try:
                ffmpeg_utils.terminate_process(streaming_processes[camera_id])
                streaming_processes.pop(camera_id, None)
                active_streams_count = max(0, active_streams_count - 1)
            except Exception as term_err:
                logging.error(f"プロセス終了エラー: {term_err}")
        return False

def terminate_streaming(camera_id):
    """
    指定されたカメラのストリーミングプロセスを停止する
    
    Args:
        camera_id (str): 停止するカメラID
        
    Returns:
        bool: 停止に成功したかどうか
    """
    global streaming_processes, active_streams_count
    
    try:
        logging.info(f"カメラ {camera_id} のストリーミングを停止します")
        
        if camera_id not in streaming_processes:
            logging.warning(f"カメラ {camera_id} のストリーミングプロセスは存在しません")
            return True
        
        # プロセスを取得して停止
        process = streaming_processes[camera_id]
        ffmpeg_utils.terminate_process(process)
        
        # 残っているプロセスを強制終了
        ffmpeg_utils.kill_ffmpeg_processes(camera_id=camera_id)
        
        # カウンターを更新
        with streaming_lock:
            active_streams_count = max(0, active_streams_count - 1)
        
        # ディクショナリから削除
        del streaming_processes[camera_id]
        
        logging.info(f"カメラ {camera_id} のストリーミングを正常に停止しました")
        return True
        
    except Exception as e:
        logging.error(f"カメラ {camera_id} のストリーミング停止中にエラーが発生しました: {e}")
        return False

def start_streaming(camera_id, rtsp_url, output_dir):
    """
    RTSPストリームからHLSストリーミングを開始する
    
    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL
        output_dir (str): 出力ディレクトリ
        
    Returns:
        bool: 成功したかどうか
    """
    try:
        # 出力ディレクトリを設定
        camera_dir = os.path.join(output_dir, str(camera_id))

        # ディレクトリが存在しなければ作成
        if not os.path.exists(camera_dir):
            os.makedirs(camera_dir)

        # 出力ファイルのパスを設定
        output_path = os.path.join(camera_dir, f"{camera_id}.m3u8")

        # まず古いプロセスを強制終了
        try:
            ffmpeg_utils.kill_ffmpeg_processes(camera_id=camera_id)
            time.sleep(1)  # プロセス終了を待つ
        except Exception as e:
            logging.warning(f"古いFFmpegプロセスの終了中にエラー: {e}")

        # FFmpegコマンドを取得してストリーミング開始
        cmd = ffmpeg_utils.get_hls_streaming_command(rtsp_url, output_path, segment_time=1)
        
        # サブプロセスでFFmpegを起動
        logging.info(f"FFmpeg starting for camera {camera_id} with command: {' '.join(cmd)}")
        
        # 標準出力と標準エラー出力をパイプに接続
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1  # ラインバッファリング
        )

        # プロセスIDをグローバル辞書に保存
        camera_processes[camera_id] = {
            'process': process,
            'start_time': time.time(),
            'pid': process.pid,
            'status': 'running',
            'last_error': '',
            'rtsp_url': rtsp_url,
            'output_path': output_path,
            'restart_count': 0,
            'last_check': time.time()
        }

        # 非同期でログを読み込むスレッドを開始
        thread = threading.Thread(
            target=_process_ffmpeg_output,
            args=(process, camera_id, rtsp_url, output_dir),
            daemon=True
        )
        thread.start()

        # プロセスが起動したかの初期チェック（即時エラーをキャッチ）
        time.sleep(2)
        if process.poll() is not None:
            # プロセスが既に終了している場合
            returncode = process.poll()
            stdout, stderr = process.communicate()
            error_msg = f"FFmpegプロセスの起動に失敗しました。終了コード: {returncode}"
            logging.error(error_msg)
            logging.error(f"FFmpegエラー出力: {stderr}")
            
            # プロセス情報を更新
            camera_processes[camera_id].update({
                'status': 'failed',
                'last_error': error_msg
            })
            
            return False

        # 成功
        logging.info(f"Camera {camera_id} streaming started successfully")
        return True
        
    except Exception as e:
        logging.error(f"Failed to start streaming for camera {camera_id}: {str(e)}")
        traceback.print_exc()
        return False

def _process_ffmpeg_output(process, camera_id, rtsp_url, output_dir):
    """
    FFmpegプロセスの出力を非同期で処理するための関数
    
    Args:
        process (subprocess.Popen): 監視するFFmpegプロセス
        camera_id (str): カメラID
        rtsp_url (str): RTSPストリームURL
        output_dir (str): 出力ディレクトリ
    """
    try:
        logging.info(f"カメラ {camera_id} のFFmpeg出力モニタリングを開始")
        error_count = 0
        last_progress_time = time.time()
        
        # ループでプロセスの出力を読み込む
        while process.poll() is None:  # プロセスが実行中の間
            try:
                # 標準出力を読み込む
                stdout_line = process.stdout.readline()
                if stdout_line:
                    logging.debug(f"FFmpeg stdout [{camera_id}]: {stdout_line.strip()}")
                    last_progress_time = time.time()
                
                # 標準エラー出力を読み込む
                stderr_line = process.stderr.readline()
                if stderr_line:
                    stderr_text = stderr_line.strip()
                    # エラーメッセージを検出
                    if "Error" in stderr_text or "error" in stderr_text.lower():
                        error_count += 1
                        logging.error(f"FFmpeg error [{camera_id}]: {stderr_text}")
                        # エラーカウントが閾値を超えた場合
                        if error_count > 10:
                            logging.error(f"カメラ {camera_id} で多数のエラーが検出されました。プロセスを再起動します。")
                            break
                    else:
                        # 通常のログメッセージ
                        logging.debug(f"FFmpeg stderr [{camera_id}]: {stderr_text}")
                        # 進行状況のメッセージを検出して進捗を記録
                        if "frame=" in stderr_text and "time=" in stderr_text:
                            last_progress_time = time.time()
                
                # 進捗がない状態が長く続く場合
                if time.time() - last_progress_time > 30:  # 30秒以上進捗がない
                    logging.warning(f"カメラ {camera_id} の処理で30秒以上進捗がありません。")
                    error_count += 1
                    last_progress_time = time.time()  # リセット
                    if error_count > 5:
                        logging.error(f"カメラ {camera_id} での進捗がなさすぎるため、プロセスを再起動します。")
                        break
                
                # ストリーミングログの処理負荷を減らすために短い待機を入れる
                time.sleep(0.1)
                
            except Exception as read_err:
                logging.error(f"カメラ {camera_id} の出力読み取り中にエラー: {read_err}")
                time.sleep(1)
        
        # プロセスが終了した場合
        exit_code = process.poll()
        logging.info(f"カメラ {camera_id} のFFmpegプロセスが終了しました (終了コード: {exit_code})")
        
        # 必要に応じて自動再起動
        if exit_code != 0:
            logging.warning(f"カメラ {camera_id} のFFmpegプロセスが異常終了したため、再起動をキューに追加")
            # 再起動処理（必要に応じて実装）
            # カメラ情報を取得してストリーミングキューに追加する処理を呼び出す
            camera = camera_utils.get_camera_by_id(camera_id)
            # enabled=1以外は絶対にストリーミングしない
            if camera and camera.get('enabled', 1) == 1:
                streaming_queue.put(camera)
            else:
                logging.info(f"カメラ {camera_id} は無効設定のため再起動しません")
        
    except Exception as e:
        logging.error(f"カメラ {camera_id} のFFmpeg出力処理中に予期しないエラー: {e}")
