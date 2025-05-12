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
import re

import config
import ffmpeg_utils
import fs_utils
import camera_utils

# グローバル変数
recording_processes = {}
recording_threads = {}
recording_start_times = {}  # 録画開始時刻を保持する辞書

# ログディレクトリを取得する補助関数
def get_log_dir():
    """ログディレクトリを取得"""
    return os.path.dirname(config.LOG_PATH)

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
        camera_dir = os.path.join(config.RECORD_PATH, camera_id)
        fs_utils.ensure_directory_exists(camera_dir)

        # ディスク容量チェック（最小1GB必要）
        required_space = 1024 * 1024 * 1024 * config.MIN_DISK_SPACE_GB
        available_space = fs_utils.get_free_space(camera_dir)

        if available_space < required_space:
            available_space_gb = available_space / (1024 * 1024 * 1024)
            error_msg = f"Insufficient disk space for camera {camera_id}. " \
                        f"Available: {available_space_gb:.2f} GB, " \
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

        # もし同じカメラIDで録画中のプロセスがあれば確実に停止
        if camera_id in recording_processes:
            logging.warning(f"録画プロセスが既に存在します。既存の録画を停止します: カメラ {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)  # 停止処理の完了を待機

        # RTSP接続の確認（接続できなくても録画は試行する）
        rtsp_ok = ffmpeg_utils.check_rtsp_connection(rtsp_url)
        if not rtsp_ok:
            logging.warning(f"RTSP接続の確認に失敗しましたが、カメラ {camera_id} の録画を試行します")

        # 音声ストリームの確認を追加（接続できなくても録画は試行する）
        try:
            has_audio = ffmpeg_utils.check_audio_stream(rtsp_url)
            if not has_audio:
                logging.warning(f"カメラ {camera_id} に音声ストリームが検出されませんでした。音声なしで録画します。")
        except Exception as audio_err:
            logging.warning(f"カメラ {camera_id} の音声ストリーム確認中にエラーが発生しましたが、録画を続行します: {audio_err}")

        # ディスク空き容量のチェック（バイト単位の結果をGB単位に変換）
        free_space = fs_utils.get_free_space(os.path.dirname(config.RECORD_PATH))
        free_space_gb = free_space / (1024 * 1024 * 1024)
        logging.info(f"Free space on drive {os.path.dirname(config.RECORD_PATH)[0]}: {free_space_gb:.2f} GB")
        
        # カメラ別のディレクトリパス
        camera_dir = os.path.join(config.RECORD_PATH, camera_id)
        
        # カメラ別のディレクトリがなければ作成
        if not os.path.exists(camera_dir):
            os.makedirs(camera_dir, exist_ok=True)
            logging.info(f"Created directory for camera {camera_id}: {camera_dir}")
        
        # カメラディレクトリの空き容量を再確認
        camera_free_space = fs_utils.get_free_space(camera_dir)
        camera_free_space_gb = camera_free_space / (1024 * 1024 * 1024)
        logging.info(f"Free space in {camera_dir}: {camera_free_space_gb:.2f} GB")
        
        if camera_free_space_gb < config.MIN_DISK_SPACE_GB:
            error_msg = f"Insufficient disk space for recording: {camera_free_space_gb:.2f} GB available, {config.MIN_DISK_SPACE_GB} GB required"
            logging.error(error_msg)
            raise Exception(error_msg)
        
        # 現在時刻を含むファイル名を生成
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        output_path = os.path.join(camera_dir, f"{camera_id}_{timestamp}.mp4")
        logging.info(f"Generated record file path: {output_path}")
        logging.info(f"Recording will be saved to: {output_path}")
        
        # ディレクトリに書き込み権限があるか確認
        try:
            test_file = os.path.join(camera_dir, f"test_{camera_id}.tmp")
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            logging.info(f"ディレクトリ {camera_dir} への書き込み権限を確認済み")
        except Exception as perm_err:
            logging.error(f"ディレクトリ {camera_dir} への書き込み権限がありません: {perm_err}")
            # 書き込み権限がなくても続行する（エラーをスローしない）
        
        # FFmpegコマンドを生成して実行
        if camera_id and camera_id != 'None' and camera_id != 'unknown':
            # カメラIDが有効な場合はカメラID付きのコマンドを使用
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, output_path, camera_id)
        else:
            # カメラIDが無効な場合は一般的なコマンドを使用
            ffmpeg_command = ffmpeg_utils.get_ffmpeg_record_command(rtsp_url, output_path)
        
        # FFmpegプロセスのログパスを設定
        log_dir = get_log_dir()
        log_path = os.path.join(log_dir, f"camera_{camera_id}.log")
        
        # FFmpegプロセス開始
        process = ffmpeg_utils.start_ffmpeg_process(ffmpeg_command, log_path=log_path)
        logging.info(f"Recording process started with PID {process.pid}")
        
        # プロセス情報を保存
        recording_processes[camera_id] = {
            'process': process,
            'output_path': output_path,
            'start_time': time.time(),
            'rtsp_url': rtsp_url
        }
        
        # ファイルが実際に作成されるまで待機（最大30秒）
        file_created = False
        max_wait_time = 30  # 30秒
        wait_interval = 1   # 1秒おきにチェック
        for _ in range(int(max_wait_time / wait_interval)):
            if os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                if file_size > 0:
                    logging.info(f"録画ファイルが正常に作成されました: {output_path} (サイズ: {file_size} バイト)")
                    file_created = True
                    break
            
            # プロセスが終了していないか確認
            if process.poll() is not None:
                logging.error(f"FFmpegプロセスが早期終了しました。終了コード: {process.poll()}")
                # プロセスは終了しているが、ファイルが作成されていない可能性
                break
                
            time.sleep(wait_interval)
        
        if not file_created:
            # ファイルが作成されなかった場合の警告
            logging.warning(f"カメラ {camera_id} の録画ファイルが {max_wait_time} 秒以内に作成されませんでした")
            
            # プロセスが実行中かどうかを確認
            if process.poll() is None:
                logging.info(f"プロセスは実行中です。録画を続行します")
            else:
                logging.error(f"プロセスは終了しています (終了コード: {process.poll()})。録画に失敗した可能性があります")
                # 失敗の理由がスタンダードエラーにあるかもしれないので、それを記録
                if process.stderr:
                    try:
                        error_output = process.stderr.read()
                        if error_output:
                            decoded_error = error_output.decode('utf-8', errors='replace')
                            logging.error(f"FFmpegエラー出力: {decoded_error}")
                    except Exception as stderr_err:
                        logging.error(f"標準エラー出力の読み取りに失敗: {stderr_err}")
        
        # 出力モニタリングスレッドを開始
        monitor_thread = threading.Thread(
            target=monitor_recording_process,
            args=(camera_id, process, output_path),
            daemon=True
        )
        monitor_thread.start()
        
        logging.info(f"カメラ {camera_id} の録画を開始しました")
        return process
    
    except Exception as e:
        error_msg = f"Error starting new recording for camera {camera_id}: {str(e)}"
        logging.error(error_msg)
        # プロセスの初期化中にエラーが発生した場合、クリーンアップ
        if camera_id in recording_processes:
            try:
                proc_info = recording_processes[camera_id]
                if 'process' in proc_info and proc_info['process']:
                    ffmpeg_utils.terminate_process(proc_info['process'])
            except Exception as cleanup_e:
                logging.error(f"Error cleaning up failed recording process: {cleanup_e}")
            # 登録情報を削除
            del recording_processes[camera_id]
        
        raise Exception(error_msg)

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
        file_path = recording_info['output_path']

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

            # 設定された時間経過で録画を再開
            max_duration = config.MAX_RECORDING_HOURS * 3600  # 時間を秒に変換
            if duration_seconds >= max_duration:
                camera_config = camera_utils.get_camera_by_id(camera_id)
                if camera_config:
                    logging.info(f"Restarting recording for camera {camera_id} due to duration limit")
                    stop_recording(camera_id)
                    time.sleep(2)
                    start_new_recording(camera_id, camera_config['rtsp_url'])
                else:
                    logging.error(f"Camera configuration not found for camera {camera_id}")

        except Exception as e:
            logging.error(f"Error in check_recording_duration for camera {camera_id}: {e}")

        time.sleep(10)  # より頻繁なチェック間隔

def monitor_recording_process(camera_id, process, output_path):
    """
    録画プロセスの出力を監視する

    Args:
        camera_id (str): カメラID
        process (subprocess.Popen): 監視するFFmpegプロセス
        output_path (str): 録画ファイルのパス
    """
    try:
        logging.info(f"カメラ {camera_id} の録画プロセスの監視を開始します")
        # FFmpeg出力を監視
        ffmpeg_utils.monitor_ffmpeg_output(process)
    except Exception as e:
        logging.error(f"監視スレッドでエラーが発生しました (カメラ {camera_id}): {e}")
    finally:
        logging.info(f"カメラ {camera_id} の録画監視スレッドが終了します")

def monitor_recording_processes():
    """
    すべての録画プロセスを監視し、必要に応じて再起動する
    ただし、自動的に新しい録画は開始しない（手動操作のみ）
    """
    while True:
        try:
            cameras = camera_utils.read_config()
            
            # 既に録画中のカメラのプロセスだけをチェック
            for camera in cameras:
                camera_id = camera['id']
                if camera_id in recording_processes:
                    recording_info = recording_processes[camera_id]
                    process = recording_info['process']

                    # プロセスの状態を確認
                    if process.poll() is not None:  # プロセスが終了している場合
                        logging.warning(f"カメラ {camera_id} の録画プロセスが終了しています。再起動します...")

                        # 録画を再開
                        try:
                            stop_recording(camera_id)  # 念のため停止処理を実行
                            time.sleep(2)  # 少し待機
                            start_recording(camera_id, camera['rtsp_url'])
                            logging.info(f"カメラ {camera_id} の録画を正常に再開しました")

                        except Exception as e:
                            logging.error(f"カメラ {camera_id} の録画再開に失敗しました: {e}")
                            logging.exception("詳細なエラー情報:")
            
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
    cameras = camera_utils.read_config()
    
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
                
                # RTSP接続確認（接続に失敗しても録画を試行）
                rtsp_ok = ffmpeg_utils.check_rtsp_connection(rtsp_url)
                if not rtsp_ok:
                    logging.warning(f"カメラ {camera_id} のRTSP接続確認に失敗しましたが、録画を試行します")
                
                # 録画ディレクトリが存在することを確認
                camera_dir = os.path.join(config.RECORD_PATH, camera_id)
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
                        
                    # 録画を強制的に開始する
                    try:
                        start_recording(camera_id, camera_config['rtsp_url'])
                        logging.info(f"カメラ {camera_id} の録画再試行に成功しました")
                        failed_cameras.remove(camera_id)
                    except Exception as retry_err:
                        logging.error(f"カメラ {camera_id} の録画再試行中にエラーが発生しました: {retry_err}")
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