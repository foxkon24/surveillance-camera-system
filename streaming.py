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
# 監視スレッド状態追跡
monitoring_threads = {}
# 健全性チェックの間隔（秒）
HEALTH_CHECK_INTERVAL = 10
# ファイル更新タイムアウト（秒）- この時間以上更新がない場合は問題と判断
HLS_UPDATE_TIMEOUT = 15
# リカバリータイムアウト - この時間経過後はリカバリを試みる
RECOVERY_TIMEOUT = 15

def get_or_start_streaming(camera):
    """
    既存のストリーミングプロセスを取得するか、新しく開始する

    Args:
        camera (dict): カメラ情報

    Returns:
        bool: 操作が成功したかどうか
    """
    camera_id = camera['id']
    
    # すでに処理中かチェック
    if camera_id in streaming_processes and hasattr(streaming_processes[camera_id], 'processing_lock'):
        if streaming_processes[camera_id].processing_lock:
            logging.info(f"Streaming for camera {camera_id} is already being processed")
            return True
    
    try:
        # 処理中フラグを設定
        if camera_id in streaming_processes:
            streaming_processes[camera_id].processing_lock = True
    except:
        pass
        
    try:
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        fs_utils.ensure_directory_exists(camera_tmp_dir)

        hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8").replace('/', '\\')
        log_path = os.path.join(camera_tmp_dir, f"{camera_id}.log").replace('/', '\\')

        # 既存のffmpegプロセスが残っている場合、強制終了
        logging.info(f"Checking for existing ffmpeg processes for camera {camera_id} before starting.")
        ffmpeg_utils.kill_ffmpeg_processes(camera_id)
        time.sleep(0.5)  # プロセス終了を少し待つ

        # RTSPストリームの接続確認 - 短いタイムアウトでチェック
        if not ffmpeg_utils.check_rtsp_connection(camera['rtsp_url']):
            logging.warning(f"Failed to connect to RTSP stream for camera {camera_id}: {camera['rtsp_url']}")
            # 接続に失敗しても続行する - 後でリトライするため

        # Nginx用に最適化されたHLSセグメントパス
        segment_path = os.path.join(camera_tmp_dir, f"{camera_id}_%03d.ts").replace('/', '\\')
        
        # HLSストリーミング用FFmpegコマンド生成
        ffmpeg_command = ffmpeg_utils.get_ffmpeg_hls_command(
            camera['rtsp_url'], 
            hls_path,
            segment_path,
            segment_time=2,  # 2秒セグメント
            list_size=3      # プレイリストサイズを小さく
        )

        # プロセス起動
        process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command, log_path=log_path)
        streaming_processes[camera_id] = process
        
        # 処理中フラグを設定
        process.processing_lock = True
        
        # 初期化時点で更新情報を記録
        hls_last_update[camera_id] = time.time()
        if os.path.exists(hls_path):
            m3u8_last_size[camera_id] = os.path.getsize(hls_path)
        else:
            m3u8_last_size[camera_id] = 0

        # 統合監視スレッドを開始（既存のスレッドがあれば停止）
        if camera_id in monitoring_threads and monitoring_threads[camera_id].is_alive():
            logging.info(f"Stopping existing monitoring thread for camera {camera_id}")
            monitoring_threads[camera_id].stop_flag = True
            time.sleep(0.5)
            
        monitor_thread = StreamMonitorThread(camera_id, camera['rtsp_url'], process)
        monitor_thread.daemon = True
        monitor_thread.start()
        monitoring_threads[camera_id] = monitor_thread

        logging.info(f"Started streaming for camera {camera_id}")
        
        # プロセス起動後すぐに初期化完了を確認するために少し待機
        init_wait_count = 0
        init_success = False
        
        while init_wait_count < 10:  # 最大5秒間待機
            if os.path.exists(hls_path) and os.path.getsize(hls_path) > 0:
                # m3u8が生成された
                ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
                if len(ts_files) > 0:
                    # TSファイルも生成された
                    init_success = True
                    break
            
            time.sleep(0.5)
            init_wait_count += 1
            
        if init_success:
            logging.info(f"Streaming initialization successful for camera {camera_id}")
        else:
            logging.warning(f"Streaming may not have initialized correctly for camera {camera_id}")
            
        # 処理中フラグを解除
        process.processing_lock = False
        return True

    except Exception as e:
        logging.error(f"Error starting streaming for camera {camera_id}: {e}")
        # 処理中フラグを解除
        if camera_id in streaming_processes and hasattr(streaming_processes[camera_id], 'processing_lock'):
            streaming_processes[camera_id].processing_lock = False
        return False


class StreamMonitorThread(threading.Thread):
    """
    ストリーム監視用統合スレッド
    プロセス監視とHLSファイル監視を統合
    """
    def __init__(self, camera_id, rtsp_url, process):
        threading.Thread.__init__(self)
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.process = process
        self.stop_flag = False
        self.consecutive_failures = 0
        self.last_restart_time = time.time()
        self.max_failures = config.RETRY_ATTEMPTS
        self.retry_delay = config.RETRY_DELAY
        self.max_retry_delay = config.MAX_RETRY_DELAY
        
    def run(self):
        logging.info(f"Started monitoring thread for camera {self.camera_id}")
        
        while not self.stop_flag:
            try:
                current_time = time.time()
                
                # 1. プロセス状態チェック
                if self.process.poll() is not None:
                    if current_time - self.last_restart_time > RECOVERY_TIMEOUT:
                        self.restart_streaming()
                    else:
                        logging.info(f"Waiting before restart for camera {self.camera_id}")
                        
                # 2. HLSファイル更新チェック
                camera_tmp_dir = os.path.join(config.TMP_PATH, self.camera_id)
                hls_path = os.path.join(camera_tmp_dir, f"{self.camera_id}.m3u8")
                
                if os.path.exists(hls_path):
                    # ファイルの更新状況をチェック
                    file_updated = self.check_hls_updates(camera_tmp_dir, hls_path)
                    
                    if not file_updated:
                        last_update = hls_last_update.get(self.camera_id, 0)
                        if current_time - last_update > HLS_UPDATE_TIMEOUT:
                            if current_time - self.last_restart_time > RECOVERY_TIMEOUT:
                                logging.warning(f"HLS update timeout for camera {self.camera_id}. Last update: {current_time - last_update:.2f}s ago")
                                self.restart_streaming()
                else:
                    # m3u8ファイルが存在しない
                    if current_time - self.last_restart_time > RECOVERY_TIMEOUT:
                        logging.warning(f"HLS file does not exist for camera {self.camera_id}")
                        self.restart_streaming()
                
                # 3. 古いTSセグメントのクリーンアップ
                self.cleanup_segments(camera_tmp_dir, hls_path)
                
            except Exception as e:
                logging.error(f"Error in monitor thread for camera {self.camera_id}: {e}")
                
            # 短い間隔でチェック
            for _ in range(int(HEALTH_CHECK_INTERVAL * 2)):
                if self.stop_flag:
                    break
                time.sleep(0.5)
                
        logging.info(f"Monitoring thread for camera {self.camera_id} has stopped")
    
    def check_hls_updates(self, camera_tmp_dir, hls_path):
        """HLSファイルの更新を確認"""
        current_time = time.time()
        file_updated = False
        
        # m3u8ファイルサイズチェック
        if os.path.exists(hls_path):
            try:
                current_size = os.path.getsize(hls_path)
                last_size = m3u8_last_size.get(self.camera_id, 0)
                
                if current_size != last_size:
                    # サイズが変わった
                    m3u8_last_size[self.camera_id] = current_size
                    hls_last_update[self.camera_id] = current_time
                    file_updated = True
                    self.consecutive_failures = 0  # リセット
            except:
                pass
        
        # TSファイルのチェック
        try:
            ts_files = [f for f in os.listdir(camera_tmp_dir) if f.endswith('.ts')]
            if ts_files:
                try:
                    newest_ts = max(ts_files, key=lambda f: os.path.getmtime(os.path.join(camera_tmp_dir, f)))
                    newest_ts_path = os.path.join(camera_tmp_dir, newest_ts)
                    ts_mtime = os.path.getmtime(newest_ts_path)
                    
                    if ts_mtime > hls_last_update.get(self.camera_id, 0):
                        hls_last_update[self.camera_id] = current_time
                        file_updated = True
                        self.consecutive_failures = 0  # リセット
                except:
                    pass
        except:
            pass
            
        return file_updated
        
    def cleanup_segments(self, camera_tmp_dir, hls_path):
        """古いTSセグメントを削除"""
        try:
            if not os.path.exists(camera_tmp_dir):
                return
                
            current_time = time.time()
            active_segments = set()
            
            # m3u8からアクティブなセグメントを取得
            if os.path.exists(hls_path):
                try:
                    with open(hls_path, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line.endswith('.ts'):
                                active_segments.add(os.path.basename(line))
                except:
                    pass
            
            # 古いTSファイルを削除
            for file in os.listdir(camera_tmp_dir):
                if file.endswith('.ts'):
                    try:
                        file_path = os.path.join(camera_tmp_dir, file)
                        
                        # プレイリストに含まれていないかつ30秒以上経過したファイルを削除
                        if (file not in active_segments and 
                            current_time - os.path.getctime(file_path) > 30):
                            os.remove(file_path)
                    except:
                        pass
        except Exception as e:
            logging.error(f"Error cleaning up segments for camera {self.camera_id}: {e}")
    
    def restart_streaming(self):
        """ストリーミング再起動"""
        try:
            # 再起動中にプロセスが変わらないようにローカル変数に保存
            current_process = self.process
            
            self.consecutive_failures += 1
            current_delay = min(self.retry_delay * (2 ** (self.consecutive_failures - 1)), self.max_retry_delay)
            
            logging.warning(f"Restarting streaming for camera {self.camera_id}. "
                           f"Attempt {self.consecutive_failures}/{self.max_failures}. "
                           f"Delay: {current_delay:.1f}s")
            
            # 既存のプロセスを終了
            if hasattr(current_process, 'processing_lock') and current_process.processing_lock:
                logging.info(f"Stream {self.camera_id} is being processed, skipping restart")
                return False
                
            # 処理中フラグを設定
            current_process.processing_lock = True
            
            try:
                ffmpeg_utils.terminate_process(current_process)
                ffmpeg_utils.kill_ffmpeg_processes(self.camera_id)
            except:
                pass
                
            # 短時間待機
            time.sleep(current_delay / 2)  # 待機時間の半分だけ待つ
            
            # 再開前にストリーミングプロセスを削除
            if self.camera_id in streaming_processes and streaming_processes[self.camera_id] == current_process:
                del streaming_processes[self.camera_id]
            
            camera = camera_utils.get_camera_by_id(self.camera_id)
            if camera:
                # 処理中フラグを解除
                current_process.processing_lock = False
                
                # 新しいストリーミングを開始
                success = get_or_start_streaming(camera)
                if success:
                    self.last_restart_time = time.time()
                    logging.info(f"Successfully restarted streaming for camera {self.camera_id}")
                    
                    # スレッドを停止（新しいスレッドが開始されているはず）
                    self.stop_flag = True
                    return True
                else:
                    logging.error(f"Failed to restart streaming for camera {self.camera_id}")
            else:
                # 処理中フラグを解除
                current_process.processing_lock = False
                logging.error(f"Camera config not found for camera {self.camera_id}")
                
            return False
            
        except Exception as e:
            logging.error(f"Error restarting streaming for camera {self.camera_id}: {e}")
            if hasattr(self.process, 'processing_lock'):
                self.process.processing_lock = False
            return False


def cleanup_scheduler():
    """
    すべてのカメラに対して定期的にクリーンアップを実行するスケジューラー
    """
    while True:
        try:
            # カメラ設定を直接読み込み
            cameras = camera_utils.read_config()
            for camera in cameras:
                camera_id = camera['id']
                camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
                hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
                
                # モニタースレッドがない場合、古いセグメントをクリーンアップ
                if camera_id not in monitoring_threads or not monitoring_threads[camera_id].is_alive():
                    try:
                        if os.path.exists(camera_tmp_dir):
                            active_segments = set()
                            
                            # m3u8からアクティブなセグメントを取得
                            if os.path.exists(hls_path):
                                try:
                                    with open(hls_path, 'r') as f:
                                        for line in f:
                                            line = line.strip()
                                            if line.endswith('.ts'):
                                                active_segments.add(os.path.basename(line))
                                except:
                                    pass
                            
                            # 古いTSファイルを削除
                            current_time = time.time()
                            for file in os.listdir(camera_tmp_dir):
                                if file.endswith('.ts'):
                                    try:
                                        file_path = os.path.join(camera_tmp_dir, file)
                                        
                                        # プレイリストに含まれていないかつ30秒以上経過したファイルを削除
                                        if (file not in active_segments and 
                                            current_time - os.path.getctime(file_path) > 30):
                                            os.remove(file_path)
                                    except:
                                        pass
                    except Exception as e:
                        logging.error(f"Error in cleanup for camera {camera_id}: {e}")

        except Exception as e:
            logging.error(f"Error in cleanup_scheduler: {e}")

        time.sleep(30)  # 30秒ごとに実行

def stop_all_streaming():
    """
    すべてのストリーミングプロセスを停止

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        # 監視スレッドを停止
        for camera_id, thread in list(monitoring_threads.items()):
            try:
                thread.stop_flag = True
            except:
                pass
        
        # 少し待機してスレッドが停止することを確認
        time.sleep(1)
                
        # プロセスを停止
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
