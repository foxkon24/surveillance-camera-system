"""
録画管理モジュール
録画プロセスの管理機能を提供します
"""
import os
import logging
import threading
import time
import traceback
from datetime import datetime
import subprocess
import psutil  # ← 追加

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
    録画を開始する関数

    Args:
        camera_id (str): カメラID
        rtsp_url (str): RTSP URL

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        logging.info(f"カメラ {camera_id} の録画開始処理を開始します")
        
        # 既存のプロセスが存在する場合は終了し、少し待機して次の録画の準備
        if camera_id in recording_processes:
            logging.info(f"カメラ {camera_id} の既存録画プロセスを停止します")
            stop_recording(camera_id)
            # 録画開始前に短い冷却時間を設ける（連続録画による問題を防止）
            time.sleep(3)

        # 録画用ディレクトリの確認と作成
        camera_dir = os.path.join(config.BASE_PATH, "record", camera_id)
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

        # 録画時間監視スレッドを必ず起動（重複起動防止）
        if camera_id not in recording_threads or not recording_threads[camera_id].is_alive():
            t = threading.Thread(target=check_recording_duration, args=(camera_id,), daemon=True)
            t.start()
            recording_threads[camera_id] = t
            logging.info(f"カメラ {camera_id} の録画時間監視スレッドを起動しました")
        else:
            logging.info(f"カメラ {camera_id} の録画時間監視スレッドは既に起動済みです")

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
        
    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        logging.info(f"Starting new recording for camera {camera_id} with URL {rtsp_url}")

        # もし同じカメラIDで録画中のプロセスがあれば確実に停止
        if camera_id in recording_processes:
            logging.warning(f"録画プロセスが既に存在します。既存の録画を停止します: カメラ {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)  # 停止処理の完了を待機

        # ディスク容量チェック
        logging.info(f"Free space check for camera {camera_id}")
        if not check_disk_space(camera_id):
            logging.error(f"ディスク容量不足のため録画を開始できません: カメラ {camera_id}")
            return False

        # 日時を含むファイルパスを生成
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        record_dir = os.path.join(config.BASE_PATH, "record", camera_id)
        file_path = os.path.join(record_dir, f"{camera_id}_{timestamp}.mp4")
        logging.info(f"Generated record file path: {file_path}")
        logging.info(f"Recording will be saved to: {file_path}")

        # HLSストリームの有無を確認
        hls_url = f"http://localhost:5000/system/cam/tmp/{camera_id}/{camera_id}.m3u8"
        use_hls = False
        try:
            import requests
            response = requests.head(hls_url, timeout=2)
            if response.status_code == 200:
                logging.info(f"カメラ{camera_id}はHLSソース（{hls_url}）を使用可能です")
                use_hls = True
            else:
                logging.info(f"カメラ{camera_id}のHLSソースは利用できません: ステータスコード {response.status_code}")
        except Exception as e:
            logging.warning(f"HLSストリーム確認中にエラーが発生しました({e})。カメラ{camera_id}はRTSPから直接録画します")

        # HLSが利用可能ならHLS経由で録画
        if use_hls:
            ffmpeg_cmd = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, file_path, camera_id)
            logging.info(f"HLS経由で録画を開始します: {hls_url}")
        else:
            # RTSP接続の確認（リトライ処理）
            rtsp_ok = False
            max_retries = 5
            last_error_message = ''
            for attempt in range(1, max_retries + 1):
                logging.info(f"RTSP connection check: {rtsp_url}")
                success, err_msg = ffmpeg_utils.check_rtsp_connection(rtsp_url)
                if success:
                    rtsp_ok = True
                    break
                else:
                    last_error_message = err_msg
                    logging.warning(f"RTSP接続失敗: {rtsp_url}（{attempt}回目） エラー: {err_msg}")
                    if attempt < max_retries:
                        backoff_time = 5 if "Operation not permitted" in err_msg else 2
                        logging.info(f"{backoff_time}秒間待機してリトライします...")
                        time.sleep(backoff_time)
            if not rtsp_ok:
                logging.error(f"カメラ{camera_id}はRTSPに接続できません。録画を開始できません。")
                return False
            ffmpeg_cmd = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, file_path, camera_id)

        cmd_str = ' '.join(str(x) for x in ffmpeg_cmd)
        logging.info(f"Executing FFmpeg command: {cmd_str}")
        logging.info(f"Starting FFmpeg process with command: {cmd_str}")

        my_env = os.environ.copy()
        process = subprocess.Popen(
            ffmpeg_cmd,
            env=my_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        time.sleep(1)
        if process.poll() is None:
            logging.info(f"FFmpeg process started with PID: {process.pid}")
            start_time = datetime.now()
            recording_processes[camera_id] = {
                'process': process,
                'url': rtsp_url,
                'file_path': file_path,
                'start_time': start_time,
                'hls': use_hls,
            }
            recording_start_times[camera_id] = start_time
            time.sleep(0.5)
            if process.poll() is None:
                logging.info(f"Recording process started with PID {process.pid}")
                if process.stderr:
                    threading.Thread(target=monitor_ffmpeg_output, args=(process, camera_id), daemon=True).start()
                return True
            else:
                # プロセスが即時終了した場合、stderr全文をログ出力
                try:
                    stderr_output = process.stderr.read().decode('utf-8', errors='replace')
                    logging.error(f"FFmpeg process failed to start for camera {camera_id}. STDERR:\n{stderr_output}")
                except Exception as e:
                    logging.error(f"FFmpeg stderr読み取り中に例外: {e}")
                return False
        else:
            # プロセスが即時終了した場合、stderr全文をログ出力
            try:
                stderr_output = process.stderr.read().decode('utf-8', errors='replace')
                logging.error(f"FFmpeg process failed to start for camera {camera_id}. STDERR:\n{stderr_output}")
            except Exception as e:
                logging.error(f"FFmpeg stderr読み取り中に例外: {e}")
            return False
    except Exception as e:
        logging.error(f"Error in start_new_recording for camera {camera_id}: {e}")
        return False

def monitor_ffmpeg_output(process, camera_id):
    """
    FFmpegプロセスのエラー出力を監視する
    
    Args:
        process: FFmpegプロセス
        camera_id: カメラID
    """
    try:
        for line in iter(process.stderr.readline, b''):
            line_text = line.decode('utf-8', errors='replace').strip()
            if line_text:
                if "error" in line_text.lower() or "warning" in line_text.lower():
                    logging.warning(f"FFmpeg {camera_id}: {line_text}")
                else:
                    logging.debug(f"FFmpeg {camera_id}: {line_text}")
    except Exception as e:
        logging.error(f"FFmpeg出力監視中にエラー: {str(e)}")

def stop_recording(camera_id):
    """
    録画を停止する関数

    Args:
        camera_id (str): カメラID

    Returns:
        bool: 操作が成功したかどうか
    """
    logging.info(f"カメラ {camera_id} の録画停止処理を開始します")

    recording_info = recording_processes.pop(camera_id, None)

    if camera_id in recording_start_times:
        del recording_start_times[camera_id]

    if recording_info:
        process = recording_info['process']
        file_path = recording_info['file_path']

        try:
            logging.info(f"録画プロセス (PID: {process.pid}) を停止します。ファイル: {file_path}")

            # プロセスを終了
            ffmpeg_utils.terminate_process(process)

            # ファイル存在確認
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                logging.info(f"録画ファイルのサイズ: {file_size / 1024:.2f} KB")

                # 1MB未満の小さなファイルは不完全と見なして削除
                if file_size < 1024 * 1024:  # 1MB
                    logging.warning(f"録画ファイルが小さすぎます ({file_size / 1024:.2f} KB)。不完全なファイルとして削除します: {file_path}")
                    try:
                        os.remove(file_path)
                        logging.info(f"不完全な録画ファイルを削除しました: {file_path}")
                    except Exception as del_err:
                        logging.error(f"ファイル削除エラー: {del_err}")
                elif file_size > 0:
                    # 十分なサイズのファイルは最終化する
                    logging.info(f"録画ファイルを最終化します: {file_path}")
                    ffmpeg_utils.finalize_recording(file_path)
                else:
                    logging.warning(f"録画ファイルが空です: {file_path}")
                    try:
                        os.remove(file_path)
                        logging.info(f"空の録画ファイルを削除しました: {file_path}")
                    except Exception as del_err:
                        logging.error(f"ファイル削除エラー: {del_err}")
            else:
                logging.error(f"録画ファイルが見つかりません: {file_path}")

            # 録画情報を削除
            if camera_id in recording_processes:
                del recording_processes[camera_id]
                
            # 録画開始時間情報も削除
            if camera_id in recording_start_times:
                del recording_start_times[camera_id]
                
            logging.info(f"カメラ {camera_id} の録画を正常に停止しました")
            return True

        except Exception as e:
            logging.error(f"録画停止中にエラーが発生しました: {e}")
            logging.exception("詳細なエラー情報:")
            return False
    else:
        logging.warning(f"カメラ {camera_id} の録画プロセスが見つかりません")
        return False

def check_recording_duration(camera_id):
    """
    録画の経過時間をチェックし、設定された時間（MAX_RECORDING_MINUTES）を超えた場合に新しい録画を開始する

    Args:
        camera_id (str): チェックするカメラID
    """
    logging.info(f"カメラ {camera_id} の録画時間監視を開始しました（最大録画時間: {config.MAX_RECORDING_MINUTES}分）")
    
    CHECK_INTERVAL = 0.2  # 監視間隔を0.2秒に短縮し、切り替え精度を向上
    
    while True:
        try:
            # カメラが録画中かチェック
            if camera_id not in recording_processes:
                logging.info(f"カメラ {camera_id} の録画が停止されたため、録画時間監視スレッドを終了します")
                break

            # 開始時間を取得
            start_time = recording_start_times.get(camera_id)
            if not start_time:
                logging.warning(f"カメラ {camera_id} の録画開始時間が記録されていません")
                time.sleep(1)  # 少し待機して再チェック
                continue

            # 現在の経過時間を計算
            current_time = datetime.now()
            duration = current_time - start_time
            duration_seconds = duration.total_seconds()
            duration_minutes = duration_seconds / 60

            # 設定された時間を超えているかチェック
            max_duration = config.MAX_RECORDING_MINUTES * 60  # 分を秒に変換
            
            # 残り時間をログに出力（30秒ごと）
            if int(duration_seconds) % 30 == 0 and abs(duration_seconds - round(duration_seconds)) < CHECK_INTERVAL:
                remaining_seconds = max_duration - duration_seconds
                if remaining_seconds > 0:
                    logging.info(f"カメラ {camera_id} の録画残り時間: {remaining_seconds:.1f}秒（{remaining_seconds/60:.1f}分）")
            
            # 最大録画時間を超えた場合、録画を再開
            if duration_seconds >= max_duration:
                logging.info(f"カメラ {camera_id} の録画時間が {config.MAX_RECORDING_MINUTES}分を超えました（実際: {duration_minutes:.2f}分）。新しいファイルで録画を再開します")
                
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config and camera_config.get('rtsp_url'):
                    recording_info = recording_processes.get(camera_id)
                    current_rtsp_url = recording_info.get('url') if recording_info else None
                    if stop_recording(camera_id):
                        # 録画停止直後に即時再開
                        rtsp_url_to_use = camera_config['rtsp_url']
                        if current_rtsp_url:
                            rtsp_url_to_use = current_rtsp_url
                        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                        record_dir = os.path.join(config.BASE_PATH, "record", camera_id)
                        file_path = os.path.join(record_dir, f"{camera_id}_{timestamp}.mp4")
                        logging.info(f"カメラ {camera_id} の録画を新しいファイル {file_path} で再開します")
                        logging.info(f"使用するソース: RTSP, URL: {rtsp_url_to_use}")
                        try:
                            has_audio = ffmpeg_utils.check_audio_stream(rtsp_url_to_use)
                            ffmpeg_cmd = [
                                config.FFMPEG_PATH,
                                '-loglevel', 'debug',
                                '-rtsp_transport', 'tcp',
                                '-buffer_size', '32768k',
                                '-use_wallclock_as_timestamps', '1',
                                '-i', rtsp_url_to_use,
                                '-r', '30',
                                '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p',
                                '-c:v', 'h264_nvenc',
                                '-gpu', '0',
                                '-preset', 'fast',
                                '-rc', 'vbr',
                                '-profile:v', 'high',
                                '-b:v', '4M'
                            ]
                            if has_audio:
                                ffmpeg_cmd.extend([
                                    '-c:a', 'aac',
                                    '-b:a', '128k',
                                    '-ar', '44100',
                                    '-ac', '2'
                                ])
                            else:
                                ffmpeg_cmd.extend(['-an'])
                            ffmpeg_cmd.extend([
                                '-max_muxing_queue_size', '2048',
                                '-fflags', '+genpts+discardcorrupt+igndts',
                                '-avoid_negative_ts', 'make_zero',
                                '-start_at_zero',
                                '-fps_mode', 'cfr',
                                '-async', '1',
                                '-movflags', '+faststart+frag_keyframe',
                                '-y', file_path
                            ])
                            cmd_str = ' '.join(ffmpeg_cmd)
                            logging.info(f"新規録画FFmpegコマンド: {cmd_str}")
                            my_env = os.environ.copy()
                            process = subprocess.Popen(
                                ffmpeg_cmd,
                                env=my_env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                            )
                            time.sleep(0.5)  # 起動確認の待機を短縮
                            if process.poll() is None:
                                logging.info(f"新規録画FFmpeg process started with PID: {process.pid}")
                                start_time = datetime.now()
                                recording_processes[camera_id] = {
                                    'process': process,
                                    'url': rtsp_url_to_use,
                                    'file_path': file_path,
                                    'start_time': start_time,
                                    'hls': False,
                                }
                                recording_start_times[camera_id] = start_time
                                if process.stderr:
                                    threading.Thread(
                                        target=monitor_ffmpeg_output,
                                        args=(process, camera_id),
                                        daemon=True
                                    ).start()
                                logging.info(f"カメラ {camera_id} の録画を新しいファイルで再開しました")
                                time.sleep(0.5)  # 開始直後の安定化待機を短縮
                                continue
                            else:
                                return_code = process.poll()
                                error_output = process.stderr.read().decode('utf-8', errors='replace') if process.stderr else ""
                                logging.error(f"新規録画プロセス起動失敗: 終了コード {return_code}, エラー: {error_output}")
                        except Exception as e:
                            logging.error(f"カメラ {camera_id} の録画再開中にエラーが発生しました: {e}")
                            logging.exception("詳細なエラー情報:")
                    else:
                        logging.error(f"カメラ {camera_id} の録画停止に失敗しました")
                    time.sleep(0.5)  # 再開失敗時も短めに待機
                else:
                    logging.error(f"カメラ {camera_id} の設定情報が見つかりません")
                    time.sleep(5)  # エラー後は短めに待機
        except Exception as e:
            logging.error(f"カメラ {camera_id} の録画時間監視でエラーが発生しました: {e}")
            logging.exception("詳細なエラー情報:")
            time.sleep(1)  # エラー後は短めに待機
        time.sleep(CHECK_INTERVAL)

def monitor_recording_processes():
    """
    すべての録画プロセスを監視し、必要に応じて再起動する
    ただし、自動的に新しい録画は開始しない（手動操作のみ）
    """
    # カメラごとの失敗回数・バックオフ管理用辞書
    retry_counts = {}
    backoff_times = {}
    min_backoff = 5
    max_backoff = 300

    while True:
        try:
            cameras = camera_utils.get_enabled_cameras()
            
            # 既に録画中のカメラのプロセスだけをチェック
            for camera in cameras:
                camera_id = camera['id']
                if camera_id in recording_processes:
                    recording_info = recording_processes[camera_id]
                    process = recording_info['process']

                    # プロセスの状態を確認
                    if process.poll() is not None:  # プロセスが終了している場合
                        logging.warning(f"カメラ {camera_id} の録画プロセスが終了しています。再起動します...")

                        # バックオフ管理
                        retry_counts.setdefault(camera_id, 0)
                        backoff_times.setdefault(camera_id, min_backoff)
                        retry_counts[camera_id] += 1
                        backoff = backoff_times[camera_id]

                        # バックオフ上限
                        if backoff > max_backoff:
                            backoff = max_backoff
                        logging.info(f"カメラ {camera_id} の録画再起動まで{backoff}秒待機（{retry_counts[camera_id]}回目の再起動）")
                        time.sleep(backoff)
                        backoff_times[camera_id] = min(backoff * 2, max_backoff)  # 指数バックオフ

                        # 録画を再開
                        try:
                            stop_recording(camera_id)  # 念のため停止処理を実行
                            time.sleep(2)  # 少し待機
                            start_recording(camera_id, camera['rtsp_url'])
                            logging.info(f"カメラ {camera_id} の録画を正常に再開しました")
                            # 成功したらリセット
                            retry_counts[camera_id] = 0
                            backoff_times[camera_id] = min_backoff
                        except Exception as e:
                            logging.error(f"カメラ {camera_id} の録画再開に失敗しました: {e}")
                            logging.exception("詳細なエラー情報:")
                else:
                    # プロセスが存在しない場合もリトライ（例：初回起動時や手動停止後）
                    pass
            # 注意：以前は5分ごとに未録画カメラを自動開始していましたが、
            # 手動で開始した録画のみを監視するため、この機能は無効化しました

        except Exception as e:
            logging.error(f"録画モニタリングプロセスでエラーが発生しました: {e}")
            logging.exception("詳細なエラー情報:")

        # 30秒ごとにチェック
        time.sleep(30)

def initialize_recording():
    """
    録画システムの初期化
    """
    # 監視スレッドの起動
    monitor_thread = threading.Thread(target=monitor_recording_processes, daemon=True)
    monitor_thread.start()
    logging.info("Started recording process monitor thread")
    # 自動録画開始は行わない（手動操作のみ）

def start_all_recordings():
    """
    すべてのカメラの録画を開始

    Returns:
        bool: 操作が成功したかどうか
    """
    success = True
    failed_cameras = []
    cameras = camera_utils.get_enabled_cameras()
    
    logging.info("====== 全カメラの録画開始処理を開始します ======")
    
    # すべての録画プロセスをまず停止して初期化
    logging.info("既存の録画プロセスを全て停止します")
    stop_all_recordings()
    
    # より長めに待機して確実にプロセスが終了するようにする
    logging.info("録画環境をクリーンにするために待機中...")
    time.sleep(8)  # 待機時間を延長して確実にプロセスが終了するようにする
    
    # 再度確認し、残っているプロセスがあれば強制終了
    if recording_processes:
        logging.warning(f"録画停止後も残っているプロセスを強制終了します: {list(recording_processes.keys())}")
        stop_all_recordings()
        time.sleep(3)
        
        # さらに残っているプロセスがあればFFmpegプロセスを直接キル
        if recording_processes:
            logging.warning("録画プロセスが残っています。FFmpegプロセスを直接強制終了します")
            ffmpeg_utils.kill_ffmpeg_processes()
            recording_processes.clear()
            recording_start_times.clear()
            time.sleep(2)
    
    logging.info("全カメラの録画を開始します...")
    
    # 各カメラについて録画を試行
    for camera in cameras:
        try:
            camera_id = camera['id']
            rtsp_url = camera['rtsp_url']
            
            # カメラIDがすでに録画中か確認（念のため）
            if camera_id in recording_processes:
                logging.warning(f"カメラ {camera_id} はまだ録画プロセスが残っています。既存のプロセスを停止します。")
                stop_recording(camera_id)
                time.sleep(2)  # プロセス終了を待機
                
            if rtsp_url:
                logging.info(f"カメラ {camera_id} の録画を開始します。URL: {rtsp_url}")
                
                # RTSP接続確認
                if not ffmpeg_utils.check_rtsp_connection(rtsp_url):
                    logging.warning(f"カメラ {camera_id} の接続確認に失敗しましたが、録画を試行します")
                
                # 録画ディレクトリが存在することを確認
                camera_dir = os.path.join(config.BASE_PATH, "record", camera_id)
                fs_utils.ensure_directory_exists(camera_dir)
                
                # 録画を開始
                start_recording(camera_id, rtsp_url)
                logging.info(f"カメラ {camera_id} の録画を開始しました")
            else:
                logging.error(f"カメラ {camera_id} のRTSP URLが空です")
                failed_cameras.append(camera_id)
                success = False

        except Exception as e:
            logging.error(f"カメラ {camera.get('id', 'unknown')} の録画開始に失敗しました: {e}")
            logging.exception("詳細なエラー情報:")
            failed_cameras.append(camera.get('id', 'unknown'))
            success = False
    
    # 結果ログ
    if failed_cameras:
        logging.warning(f"一部のカメラの録画開始に失敗しました: {', '.join(failed_cameras)}")
    else:
        logging.info("全カメラの録画を開始しました")
        
    # 2回目の試行 - 失敗したカメラについて再試行
    if failed_cameras:
        logging.info(f"失敗したカメラ {', '.join(failed_cameras)} の録画を再試行します")
        time.sleep(5)  # 再試行前により長く待機
        
        for camera_id in failed_cameras[:]:  # コピーを使用して反復中に変更を可能に
            try:
                # カメラ設定を取得
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config and camera_config.get('rtsp_url'):
                    logging.info(f"カメラ {camera_id} の録画を再試行します")
                    
                    # 既存のプロセスを再確認
                    if camera_id in recording_processes:
                        logging.warning(f"カメラ {camera_id} はすでに録画中です。録画を再開しません。")
                        failed_cameras.remove(camera_id)
                        continue
                        
                    start_recording(camera_id, camera_config['rtsp_url'])
                    logging.info(f"カメラ {camera_id} の録画再試行に成功しました")
                    failed_cameras.remove(camera_id)
                else:
                    logging.error(f"カメラ {camera_id} の設定が見つかりません")
            except Exception as e:
                logging.error(f"カメラ {camera_id} の録画再試行に失敗しました: {e}")
    
    # 最終結果
    if failed_cameras:
        logging.warning(f"最終的に録画開始に失敗したカメラ: {', '.join(failed_cameras)}")
        success = False
    else:
        logging.info("全カメラの録画を正常に開始しました")
    
    logging.info("====== 全カメラの録画開始処理が完了しました ======")
    return success

def stop_all_recordings():
    """
    すべてのカメラの録画を停止
    
    Returns:
        bool: 操作が成功したかどうか
    """
    success = True
    # 現在の録画プロセスのカメラIDリストを保存（反復中に変更されるため）
    camera_ids = list(recording_processes.keys())
    logging.info(f"停止対象のカメラ: {camera_ids}")
    
    if not camera_ids:
        logging.info("停止する録画プロセスがありません")
        return True
    
    # 各カメラの録画を停止試行
    failed_cameras = []
    for camera_id in camera_ids:
        try:
            logging.info(f"カメラ {camera_id} の録画を停止します...")
            if stop_recording(camera_id):
                logging.info(f"カメラ {camera_id} の録画を正常に停止しました")
            else:
                logging.warning(f"カメラ {camera_id} の録画停止メソッドは失敗を返しました")
                failed_cameras.append(camera_id)
                success = False
        except Exception as e:
            logging.error(f"カメラ {camera_id} の録画停止中にエラーが発生しました: {e}")
            logging.exception("詳細なエラー情報:")
            failed_cameras.append(camera_id)
            success = False
    
    # 停止に失敗したカメラに対して強制停止を試行
    if failed_cameras:
        logging.warning(f"以下のカメラの録画停止に失敗しました。強制停止を試みます: {failed_cameras}")
        for camera_id in failed_cameras[:]:  # コピーを使用
            try:
                # 録画プロセス情報を取得
                recording_info = recording_processes.get(camera_id)
                if recording_info and 'process' in recording_info:
                    process = recording_info['process']
                    logging.info(f"カメラ {camera_id} のプロセス(PID: {process.pid})を強制終了します")
                    
                    # 強制的にプロセスを終了
                    ffmpeg_utils.terminate_process(process, timeout=15)  # タイムアウト延長
                    
                    # recording_processesから削除
                    if camera_id in recording_processes:
                        del recording_processes[camera_id]
                    
                    # 開始時間も削除
                    if camera_id in recording_start_times:
                        del recording_start_times[camera_id]
                    
                    failed_cameras.remove(camera_id)
                    logging.info(f"カメラ {camera_id} のプロセスを強制終了しました")
                else:
                    logging.warning(f"カメラ {camera_id} の録画プロセス情報が見つかりません")
            except Exception as e:
                logging.error(f"カメラ {camera_id} の強制停止中にエラーが発生しました: {e}")
    
    # 最終確認：全プロセスが停止したか検証
    time.sleep(3)  # プロセスの終了を待機（時間を延長）
    remaining_processes = list(recording_processes.keys())
    if remaining_processes:
        logging.critical(f"停止操作後も以下のカメラの録画プロセスが残っています: {remaining_processes}")
        
        # 最後の手段として、直接プロセス削除とffmpeg_utils.kill_ffmpeg_processesを呼び出す
        try:
            logging.warning("すべてのFFmpegプロセスを強制終了します...")
            
            # 残っている全プロセスに対して個別に処理
            for camera_id in remaining_processes:
                try:
                    if camera_id in recording_processes:
                        info = recording_processes[camera_id]
                        if 'process' in info and info['process']:
                            try:
                                logging.warning(f"カメラ {camera_id} のプロセスを強制終了します (PID: {info['process'].pid})")
                                # プロセスが実行中かチェック
                                if info['process'].poll() is None:
                                    # まずは標準的な終了を試みる
                                    info['process'].terminate()
                                    time.sleep(1)
                                    # まだ実行中なら強制終了
                                    if info['process'].poll() is None:
                                        info['process'].kill()
                            except Exception as process_err:
                                logging.error(f"プロセス終了エラー: {process_err}")
                        
                        # データから削除
                        del recording_processes[camera_id]
                        if camera_id in recording_start_times:
                            del recording_start_times[camera_id]
                except Exception as proc_err:
                    logging.error(f"カメラ {camera_id} のプロセスクリーンアップエラー: {proc_err}")
            
            # すべてのFFmpegプロセスを強制終了
            ffmpeg_utils.kill_ffmpeg_processes()
            time.sleep(1)  # 終了を待機
            
            # recording_processesを完全にクリア
            recording_processes.clear()
            recording_start_times.clear()
            
            logging.info("すべての録画プロセスを強制終了しました")
        except Exception as e:
            logging.error(f"FFmpegプロセスの強制終了中にエラーが発生しました: {e}")
            success = False
    
    # Windows固有の対策：tasklist経由でFFmpegプロセスの有無を確認
    try:
        tasklist_output = subprocess.check_output("tasklist /FI \"IMAGENAME eq ffmpeg.exe\"", shell=True).decode('utf-8', errors='ignore')
        if "ffmpeg.exe" in tasklist_output:
            logging.warning("tasklist確認: FFmpegプロセスがまだ存在しています")
            logging.debug(f"tasklist出力: {tasklist_output}")
            
            # 強制終了コマンドを実行
            os.system("taskkill /F /IM ffmpeg.exe /T")
            time.sleep(1)
            logging.info("すべてのFFmpegプロセスを強制終了しました")
        else:
            logging.info("tasklist確認: すべてのFFmpegプロセスが終了しています")
    except Exception as e:
        logging.error(f"tasklist/taskkillコマンド実行中にエラーが発生しました: {e}")
    
    # 最終確認：データ構造が空かどうか
    if recording_processes:
        logging.warning(f"まだ録画プロセス情報が残っています: {list(recording_processes.keys())}。強制的にクリアします。")
        recording_processes.clear()
        recording_start_times.clear()
    
    logging.info(f"全カメラ録画停止処理が完了しました。結果: {'成功' if success else '一部失敗'}")
    return success

def get_recording_status(camera_id):
    """
    カメラの録画状態を取得する

    Args:
        camera_id (str): カメラID

    Returns:
        bool: 録画中かどうか
    """
    # カメラIDが録画プロセスリストに存在するかチェック
    is_recording = camera_id in recording_processes
    
    if is_recording:
        # プロセスが生きているか確認
        process = recording_processes[camera_id]['process']
        if process.poll() is not None:
            # プロセスが終了している場合は録画していないとみなす
            logging.warning(f"Recording process for camera {camera_id} exists but has terminated")
            return False
            
    return is_recording

def check_disk_space(camera_id):
    """
    録画に十分なディスク容量があるか確認する

    Args:
        camera_id (str): カメラID

    Returns:
        bool: 十分な容量がある場合はTrue
    """
    try:
        camera_dir = os.path.join(config.BASE_PATH, "record", camera_id)
        fs_utils.ensure_directory_exists(camera_dir)
        
        # ディスク空き容量を取得（GB単位）
        available_gb = fs_utils.get_free_space(camera_dir) / (1024 * 1024 * 1024)
        logging.info(f"Free space on drive: {available_gb:.2f} GB")
        logging.info(f"Free space in {camera_dir}: {available_gb:.2f} GB")
        
        # 最小必要容量と比較
        if available_gb < config.MIN_DISK_SPACE_GB:
            logging.error(f"Insufficient disk space: {available_gb:.2f} GB available, {config.MIN_DISK_SPACE_GB} GB required")
            return False
            
        return True
    except Exception as e:
        logging.error(f"ディスク容量チェック中にエラーが発生しました: {str(e)}")
        return False

def self_heal_recording_system():
    """
    録画分割・プロセス・ファイルの異常を自動検知し、恒久修正サイクルを回す自己修復監視関数
    """
    CHECK_INTERVAL = 60  # 監視間隔（秒）
    MAX_NO_UPDATE_MINUTES = config.MAX_RECORDING_MINUTES + 2  # 許容する最大ファイル無更新時間
    anomaly_counts = {}  # カメラごとの連続異常回数
    while True:
        try:
            for camera_id, rec_info in list(recording_processes.items()):
                proc = rec_info.get('process')
                file_path = rec_info.get('file_path')
                last_update = None
                if file_path and os.path.exists(file_path):
                    last_update = datetime.fromtimestamp(os.path.getmtime(file_path))
                now = datetime.now()
                # 1. プロセス生存・ゾンビ化監視
                if proc is not None:
                    if proc.poll() is not None or not psutil.pid_exists(proc.pid):
                        logging.error(f"[SELF-HEAL] カメラ{camera_id}の録画プロセスが異常終了/ゾンビ化。自動修復を試みます")
                        anomaly_counts[camera_id] = anomaly_counts.get(camera_id, 0) + 1
                        _dump_anomaly(camera_id, 'process_zombie', file_path)
                        stop_recording(camera_id)
                        time.sleep(2)
                        cam = camera_utils.get_camera_by_id(camera_id)
                        if cam and cam.get('rtsp_url'):
                            start_recording(camera_id, cam['rtsp_url'])
                        continue
                # 2. ファイルサイズ・生成間隔監視
                if file_path and os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    if file_size < 1024 * 1024:  # 1MB未満
                        logging.warning(f"[SELF-HEAL] カメラ{camera_id}の録画ファイルが小さすぎます。不完全ファイルとして削除・再録画")
                        anomaly_counts[camera_id] = anomaly_counts.get(camera_id, 0) + 1
                        _dump_anomaly(camera_id, 'file_too_small', file_path)
                        stop_recording(camera_id)
                        time.sleep(2)
                        cam = camera_utils.get_camera_by_id(camera_id)
                        if cam and cam.get('rtsp_url'):
                            start_recording(camera_id, cam['rtsp_url'])
                        continue
                    # ファイル更新間隔監視
                    if last_update and (now - last_update).total_seconds() > MAX_NO_UPDATE_MINUTES * 60:
                        logging.error(f"[SELF-HEAL] カメラ{camera_id}の録画ファイルが{MAX_NO_UPDATE_MINUTES}分以上更新されていません。自動修復を試みます")
                        anomaly_counts[camera_id] = anomaly_counts.get(camera_id, 0) + 1
                        _dump_anomaly(camera_id, 'file_no_update', file_path)
                        stop_recording(camera_id)
                        time.sleep(2)
                        cam = camera_utils.get_camera_by_id(camera_id)
                        if cam and cam.get('rtsp_url'):
                            start_recording(camera_id, cam['rtsp_url'])
                        continue
                # 2.5. ディレクトリ内最新ファイルの生成間隔監視
                record_dir = os.path.dirname(file_path) if file_path else None
                if record_dir and os.path.exists(record_dir):
                    mp4_files = [f for f in os.listdir(record_dir) if f.endswith('.mp4') and not f.endswith('.temp.mp4')]
                    if mp4_files:
                        latest_mp4 = max(mp4_files, key=lambda f: os.path.getmtime(os.path.join(record_dir, f)))
                        latest_mp4_path = os.path.join(record_dir, latest_mp4)
                        latest_mp4_time = datetime.fromtimestamp(os.path.getmtime(latest_mp4_path))
                        if (now - latest_mp4_time).total_seconds() > MAX_NO_UPDATE_MINUTES * 60:
                            logging.error(f"[SELF-HEAL] カメラ{camera_id}の録画ディレクトリ内で最新mp4ファイルが{MAX_NO_UPDATE_MINUTES}分以上生成・更新されていません。自動修復を試みます")
                            anomaly_counts[camera_id] = anomaly_counts.get(camera_id, 0) + 1
                            _dump_anomaly(camera_id, 'dir_no_new_mp4', latest_mp4_path)
                            stop_recording(camera_id)
                            time.sleep(2)
                            cam = camera_utils.get_camera_by_id(camera_id)
                            if cam and cam.get('rtsp_url'):
                                start_recording(camera_id, cam['rtsp_url'])
                            continue
                # 3. 一時ファイル残存監視
                if record_dir and os.path.exists(record_dir):
                    for fname in os.listdir(record_dir):
                        if fname.endswith('.temp.mp4'):
                            temp_path = os.path.join(record_dir, fname)
                            try:
                                os.remove(temp_path)
                                logging.info(f"[SELF-HEAL] 残存一時ファイルを自動削除: {temp_path}")
                            except Exception as e:
                                logging.error(f"[SELF-HEAL] 一時ファイル削除失敗: {temp_path}, {e}")
                # 4. 連続異常発生時のバックオフ・アラート
                if anomaly_counts.get(camera_id, 0) >= 3:
                    logging.critical(f"[SELF-HEAL] カメラ{camera_id}で連続異常が3回以上発生。バックオフし管理者に通知してください")
                    # ここで通知や外部連携処理を追加可能
                    time.sleep(120)  # 2分バックオフ
                    anomaly_counts[camera_id] = 0
        except Exception as e:
            logging.error(f"[SELF-HEAL] 自己修復監視サイクルで例外: {e}")
        time.sleep(CHECK_INTERVAL)

def _dump_anomaly(camera_id, anomaly_type, file_path):
    """
    異常発生時の詳細ダンプを自動保存
    """
    try:
        dump_dir = os.path.join(config.BASE_PATH, 'log', 'self_heal')
        os.makedirs(dump_dir, exist_ok=True)
        now = datetime.now().strftime('%Y%m%d_%H%M%S')
        dump_file = os.path.join(dump_dir, f"{camera_id}_{anomaly_type}_{now}.log")
        with open(dump_file, 'w', encoding='utf-8') as f:
            f.write(f"camera_id: {camera_id}\n")
            f.write(f"anomaly_type: {anomaly_type}\n")
            f.write(f"file_path: {file_path}\n")
            f.write(f"datetime: {now}\n")
            if file_path and os.path.exists(file_path):
                f.write(f"file_size: {os.path.getsize(file_path)}\n")
                f.write(f"last_update: {datetime.fromtimestamp(os.path.getmtime(file_path))}\n")
            f.write(f"process_list: {psutil.pids()}\n")
    except Exception as e:
        logging.error(f"[SELF-HEAL] 異常ダンプ保存失敗: {e}")

# 監視スレッドの起動部の直後に追加
try:
    self_heal_thread = threading.Thread(target=self_heal_recording_system, daemon=True)
    self_heal_thread.start()
    logging.info("Started self-heal recording system thread")
except Exception as e:
    logging.error(f"Failed to start self-heal thread: {e}")