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
import random

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
# ストリーミング情報を保持する辞書
streaming_info = {}
# 健全性チェックの間隔（秒）
HEALTH_CHECK_INTERVAL = 15
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 120  # Default timeout for HLS stream update in seconds
# カメラ起動の間隔（秒）- カメラごとの起動間隔
CAMERA_START_INTERVAL = 2  # 1秒から2秒に変更
# 同時ストリーミングの最大数
MAX_CONCURRENT_STREAMS = 4  # 6から4に戻す
# プロセス起動前の待機時間
PROCESS_START_DELAY = 2  # 1秒から2秒に変更
# 現在アクティブなストリーミングの数
active_streams_lock = threading.Lock()
active_streams_count = 0

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    global active_streams_count
    
    if camera['id'] not in streaming_processes:
        try:
            # 現在のアクティブストリーム数を確認
            with active_streams_lock:
                current_active = active_streams_count
                
            # カメラの準備
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera['id'])
            fs_utils.ensure_directory_exists(camera_tmp_dir)

            hls_path = os.path.join(camera_tmp_dir, f"{camera['id']}.m3u8").replace('/', '\\')
            log_path = os.path.join(camera_tmp_dir, f"{camera['id']}.log").replace('/', '\\')

            # 古いHLSファイルを削除
            if os.path.exists(hls_path):
                try:
                    os.remove(hls_path)
                    logging.info(f"Removed old HLS file: {hls_path}")
                except Exception as e:
                    logging.error(f"Error removing old HLS file: {e}")
                    
            # 古いTSセグメントも削除
            ts_files_removed = 0
            for file in os.listdir(camera_tmp_dir):
                if file.endswith('.ts'):
                    try:
                        os.remove(os.path.join(camera_tmp_dir, file))
                        ts_files_removed += 1
                    except Exception as e:
                        logging.error(f"Error removing old TS file {file}: {e}")
            
            if ts_files_removed > 0:
                logging.info(f"Removed {ts_files_removed} old TS files from {camera_tmp_dir}")

            # 既存のffmpegプロセスが残っている場合、強制終了
            ffmpeg_utils.kill_ffmpeg_processes(camera['id'])
            
            # RTSPストリームの接続確認
            connection_success = ffmpeg_utils.check_rtsp_connection(camera['rtsp_url'])
            if not connection_success:
                logging.warning(f"Failed to connect to RTSP stream for camera {camera['id']}: {camera['rtsp_url']}")
                # 接続に失敗した場合は一定時間待機してから再試行
                time.sleep(1)
                connection_success = ffmpeg_utils.check_rtsp_connection(camera['rtsp_url'])
                
                if not connection_success:
                    logging.error(f"Failed to connect to RTSP stream after retry for camera {camera['id']}")
                    return False

            # Nginx用に最適化されたHLSセグメントパス
            segment_path = os.path.join(camera_tmp_dir, f"{camera['id']}_%03d.ts").replace('/', '\\')
            
            # アクティブストリーム数をインクリメント
            with active_streams_lock:
                active_streams_count += 1
                logging.info(f"Active streams: {active_streams_count}")
                
            # Windowsでのパス問題を避けるため、パスを引用符で囲む
            safe_hls_path = f'"{hls_path}"'
            safe_segment_path = f'"{segment_path}"'
                
            # ffmpegコマンドを簡略化
            command = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-i', camera['rtsp_url'],
                '-c:v', 'copy',
                '-c:a', 'copy',
                '-f', 'hls',
                '-hls_time', '2',
                '-hls_list_size', '6',
                '-hls_flags', 'delete_segments',
                '-hls_segment_filename', segment_path,
                hls_path
            ]

            # プロセス起動
            process = ffmpeg_utils.start_ffmpeg_process(command, log_path=log_path, high_priority=False)
            streaming_processes[camera['id']] = process
            
            # ストリーミング情報を更新
            streaming_info[camera['id']] = {
                'playlist_path': hls_path,
                'camera_tmp_dir': camera_tmp_dir,
                'rtsp_url': camera['rtsp_url']
            }
            
            # プロセスが起動していることを確認（起動直後に終了しないように）
            time.sleep(0.5)
            
            if process.poll() is not None:
                logging.error(f"FFmpeg process for camera {camera['id']} terminated immediately after start with code {process.returncode}")
                with active_streams_lock:
                    active_streams_count -= 1
                return False
            
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
            
            # HLSファイルが生成されるのを少し待つ（最大10秒）
            max_wait = 10
            for i in range(max_wait * 2):
                if os.path.exists(hls_path) and os.path.getsize(hls_path) > 0:
                    logging.info(f"HLS file successfully created for camera {camera['id']} with size {os.path.getsize(hls_path)} bytes")
                    break
                time.sleep(0.5)
            else:
                logging.warning(f"HLS file not created for camera {camera['id']} after {max_wait} seconds")
            
            return True

        except Exception as e:
            # エラー時にアクティブストリーム数を減らす
            with active_streams_lock:
                if active_streams_count > 0:
                    active_streams_count -= 1
            
            logging.error(f"Error starting streaming for camera {camera['id']}: {e}")
            return False

    return True

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
        
        # アクティブストリーム数を減らす
        with active_streams_lock:
            if camera_id in streaming_processes and active_streams_count > 0:
                active_streams_count -= 1
                logging.info(f"Decreased active streams: {active_streams_count}")
        
        # ストリーミングプロセスを削除
        if camera_id in streaming_processes:
            del streaming_processes[camera_id]
        
        # リソースをクリーンアップするために少し待機
        time.sleep(3)  # 2から3秒に増加
        
        # カメラ設定を読み込んでストリーミングを再開
        camera = camera_utils.get_camera_by_id(camera_id)
        if camera:
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
    hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8").replace('/', '\\')
    
    failures = 0
    max_failures = 3  # 連続でこの回数分問題が検出されたら再起動
    
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
                try:
                    # ファイルサイズをチェック
                    current_size = os.path.getsize(hls_path)
                    last_size = m3u8_last_size.get(camera_id, 0)
                    
                    if current_size != last_size:
                        # ファイルサイズが変わっていれば更新されている
                        m3u8_last_size[camera_id] = current_size
                        hls_last_update[camera_id] = current_time
                        file_updated = True
                        failures = 0  # 正常更新を検出したらカウンタをリセット
                except Exception as e:
                    logging.error(f"Error checking m3u8 file size for camera {camera_id}: {e}")
            
            # TSファイルの更新も確認
            try:
                ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                if ts_files:
                    newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                    newest_ts_path = os.path.join(camera_tmp_dir, newest_ts)
                    ts_mtime = os.path.getmtime(newest_ts_path)
                    
                    if ts_mtime > hls_last_update.get(camera_id, 0):
                        hls_last_update[camera_id] = current_time
                        file_updated = True
                        failures = 0  # 正常更新を検出したらカウンタをリセット
            except Exception as e:
                logging.error(f"Error checking ts files for camera {camera_id}: {e}")
            
            # ファイル更新が停止しているかチェック
            last_update = hls_last_update.get(camera_id, 0)
            if not file_updated and (current_time - last_update) > HLS_UPDATE_TIMEOUT:
                logging.warning(f"HLS files for camera {camera_id} have not been updated for {current_time - last_update:.2f} seconds")
                failures += 1
                
                if failures >= max_failures:
                    logging.error(f"HLS update timeout detected for camera {camera_id}. Restarting streaming.")
                    # streamingモジュールのrestart_streaming関数を呼び出し
                    try:
                        restart_streaming(camera_id)
                    except Exception as restart_err:
                        logging.error(f"Failed to restart streaming for camera {camera_id}: {restart_err}")
                    failures = 0
                    
                    # 監視を終了（新しいスレッドが開始されるため）
                    break
            
        except Exception as e:
            logging.error(f"Error monitoring HLS updates for camera {camera_id}: {e}")
        
        time.sleep(HEALTH_CHECK_INTERVAL)

def monitor_streaming_process(camera_id, process):
    """監視スレッドでストリーミングプロセスを監視する。
    プロセスが終了していた場合は再起動を試みる。
    
    Args:
        camera_id (int or str): 監視するカメラのID
        process (subprocess.Popen): 監視するプロセスオブジェクト
    """
    camera_id = str(camera_id)
    logging.info(f"Started monitoring streaming process for camera {camera_id}")
    
    # 再起動試行回数を追跡するカウンター
    restart_attempts = 0
    max_restart_attempts = 5  # 最大再起動試行回数
    
    while camera_id in streaming_processes:
        # プロセスが終了したかチェック
        return_code = process.poll()
        
        if return_code is not None:
            # プロセスが終了している場合
            
            # 再起動試行回数が多すぎる場合はしばらく待機
            if restart_attempts >= max_restart_attempts:
                logging.warning(f"Too many restart attempts for camera {camera_id}. Waiting 60 seconds before next attempt.")
                time.sleep(60)
                restart_attempts = 0
            
            logging.warning(f"Streaming process for camera {camera_id} has died with return code {return_code}")
            
            # 最初にカメラのリソースをクリーンアップ
            cleanup_camera_resources(camera_id)
            
            # 既存のFFmpegプロセスを確実に終了
            ffmpeg_utils.kill_ffmpeg_processes(camera_id)
            
            # 少し待機してからストリーミングを再開
            time.sleep(5)
            
            try:
                logging.info(f"Attempting to restart streaming for camera {camera_id}")
                # カメラ設定を再取得
                camera = camera_utils.get_camera_by_id(camera_id)
                if not camera:
                    logging.error(f"Failed to get camera config for {camera_id} during restart")
                    time.sleep(30)  # 設定が取得できない場合は長めに待機
                    restart_attempts += 1
                    continue
                
                # ストリーミングを再開
                success = get_or_start_streaming(camera)
                if success:
                    logging.info(f"Successfully restarted streaming for camera {camera_id}")
                    restart_attempts = 0  # 成功したらカウンタをリセット
                    break  # 新しいモニタースレッドが作成されるので、このスレッドは終了
                else:
                    logging.error(f"Failed to restart streaming for camera {camera_id}")
                    restart_attempts += 1
            except Exception as e:
                logging.error(f"Error restarting streaming for camera {camera_id}: {e}")
                restart_attempts += 1
                time.sleep(10)  # エラー後も少し待機
        
        # 少し待ってから再チェック
        time.sleep(5)
    
    logging.info(f"Stopped monitoring streaming process for camera {camera_id}")

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

def cleanup_old_segments(camera_id, playlist_path):
    """
    古いHLSセグメントファイルをクリーンアップする

    Args:
        camera_id (str): カメラID
        playlist_path (str): m3u8プレイリストファイルのパス
    """
    try:
        if not os.path.exists(playlist_path):
            return False

        # プレイリストディレクトリ
        playlist_dir = os.path.dirname(playlist_path)
        
        # プレイリストファイルを読み込み
        now = time.time()
        try:
            with open(playlist_path, 'r') as f:
                playlist_content = f.read()
        except Exception as e:
            logging.error(f"Failed to read playlist file {playlist_path}: {e}")
            return False
        
        # プレイリストから.tsファイル名を抽出
        ts_files_in_playlist = []
        for line in playlist_content.split('\n'):
            if line.endswith('.ts') and not line.startswith('#'):
                ts_files_in_playlist.append(line.strip())
        
        # アクティブなストリーミングプロセスがない場合はクリーンアップしない
        if camera_id not in streaming_processes:
            logging.debug(f"Skipping cleanup for camera {camera_id} as streaming is not active")
            return True
        
        # ディレクトリ内のすべての.tsファイルをチェック
        all_ts_files = []
        for file in os.listdir(playlist_dir):
            if file.endswith(".ts"):
                file_path = os.path.join(playlist_dir, file)
                file_age = now - os.path.getmtime(file_path)
                
                # プレイリストに含まれていない.tsファイルは、300秒（5分）以上経過していれば削除
                if file not in ts_files_in_playlist and file_age > 300:  # 180秒から300秒に変更
                    try:
                        os.remove(file_path)
                        logging.debug(f"Deleted old segment file: {file}")
                    except Exception as e:
                        logging.error(f"Error deleting segment file {file}: {e}")
                else:
                    all_ts_files.append((file, file_age))
        
        # プレイリストに含まれるファイルは保持する（削除ロジックを変更）
        # 非常に古いファイルのみを削除対象にする
        if len(all_ts_files) > 10:  # 5から10個に増加
            all_ts_files.sort(key=lambda x: x[1], reverse=True)  # 最も古いファイルが先頭
            for file, age in all_ts_files[10:]:  # 10個以上の場合
                # 600秒（10分）以上経過したファイルのみ削除
                if age > 600:  # 5分 から 10分に増加
                    # プレイリストに含まれる場合はスキップ
                    if file in ts_files_in_playlist:
                        continue
                        
                    file_path = os.path.join(playlist_dir, file)
                    try:
                        os.remove(file_path)
                        logging.debug(f"Deleted excess segment file: {file}")
                    except Exception as e:
                        logging.error(f"Error deleting excess segment file {file}: {e}")
        
        return True
    except Exception as e:
        logging.error(f"Error cleaning up segments for camera {camera_id}: {e}")
        return False

def cleanup_scheduler():
    """クリーンアップスケジューラー。定期的に古いセグメントを削除する"""
    logging.info("Started cleanup scheduler")
    while True:
        try:
            # すべてのアクティブなカメラに対してクリーンアップを実行
            for camera_id in list(streaming_processes.keys()):
                info = streaming_info.get(camera_id, {})
                playlist_path = info.get('playlist_path')
                if playlist_path and os.path.exists(playlist_path):
                    cleanup_old_segments(camera_id, playlist_path)
            
            # 一時ファイルのクリーンアップ
            cleanup_tmp_files()
            
        except Exception as e:
            logging.error(f"Error in cleanup scheduler: {e}")
        
        # 120秒（2分）ごとにクリーンアップを実行
        time.sleep(120)

def stop_all_streaming():
    """
    すべてのストリーミングプロセスを停止

    Returns:
        bool: 操作が成功したかどうか
    """
    global active_streams_count
    
    try:
        # アクティブストリーム数をリセット
        with active_streams_lock:
            active_streams_count = 0
            
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
    global active_streams_count
    
    # アクティブストリーム数を初期化
    with active_streams_lock:
        active_streams_count = 0
    
    # クリーンアップスレッドの起動
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    logging.info("Started segment cleanup scheduler thread")

def cleanup_tmp_files():
    """
    不要になった一時ファイルをクリーンアップする
    """
    try:
        # tmp ディレクトリをチェック
        if os.path.exists(config.TMP_PATH):
            for camera_dir in os.listdir(config.TMP_PATH):
                camera_path = os.path.join(config.TMP_PATH, camera_dir)
                if os.path.isdir(camera_path):
                    # カメラが現在ストリーミング中でない場合、古いファイルを削除
                    if camera_dir not in streaming_processes:
                        for file in os.listdir(camera_path):
                            file_path = os.path.join(camera_path, file)
                            if os.path.isfile(file_path):
                                # ファイルが10分（600秒）以上前に変更された場合に削除
                                file_age = time.time() - os.path.getmtime(file_path)
                                if file_age > 600:
                                    try:
                                        os.remove(file_path)
                                        logging.debug(f"Deleted old temporary file: {file_path}")
                                    except Exception as e:
                                        logging.error(f"Error deleting temporary file {file_path}: {e}")
        return True
    except Exception as e:
        logging.error(f"Error cleaning up temporary files: {e}")
        return False
