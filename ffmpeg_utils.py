"""
FFmpeg関連ユーティリティ
FFmpegプロセスの操作と管理機能を提供します
"""
import subprocess
import logging
import psutil
import json
import time
import re
import os
import threading
import config
import requests
from datetime import datetime
import fractions
import signal
from urllib.parse import urlparse

# 接続に問題があるカメラのリストはconfig.pyから読み込む
# コメント化したまま削除

def check_rtsp_connection(rtsp_url, timeout=5):
    """
    RTSPのURLが有効かどうか確認する - FFmpeg 7.1.1対応版
    
    Args:
        rtsp_url (str): 確認するRTSP URL
        timeout (int): タイムアウト秒数
        
    Returns:
        tuple: (成功したかどうか, エラーメッセージ)
    """
    logging.info(f"RTSP connection check: {rtsp_url}")
    
    try:
        # FFmpeg 7.1.1対応 - rw_timeout削除
        cmd = [
            config.FFMPEG_PATH,
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-t', '1',  # 1秒だけキャプチャ
            '-f', 'null',
            '-'
        ]
        
        # サブプロセスを開始
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # タイムアウト付きで実行
        try:
            stdout, stderr = process.communicate(timeout=timeout+2)  # プロセス終了まで待機
            
            # 成功したかどうかを確認
            if process.returncode == 0:
                return True, ""
            else:
                return False, f"FFmpeg終了コード: {process.returncode}, エラー: {stderr}"
                
        except subprocess.TimeoutExpired:
            process.kill()
            return False, f"RTSPの接続確認がタイムアウトしました。URL: {rtsp_url}"
            
    except Exception as e:
        return False, f"RTSPの接続確認中に例外が発生しました: {str(e)}"

def check_stream_details(rtsp_url, timeout=10):
    """
    RTSPストリームの詳細情報（FPS、解像度）を取得する

    Args:
        rtsp_url (str): チェックするRTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        tuple: (fps, width, height) またはNone（失敗時）
    """
    try:
        ffprobe_command = [
            'ffprobe',
            '-v', 'error',
            '-rtsp_transport', 'tcp',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=r_frame_rate,width,height',
            '-of', 'csv=p=0:s=,',
            '-timeout', str(timeout * 1000000),
            '-i', rtsp_url
        ]

        result = subprocess.run(ffprobe_command, timeout=timeout, capture_output=True, text=True)
        
        if result.returncode == 0 and result.stdout.strip():
            # 出力フォーマット: "r_frame_rate,width,height" 例: "30/1,1920,1080"
            values = result.stdout.strip().split(',')
            if len(values) == 3:
                fps_str, width_str, height_str = values
                
                # FPS値を計算（分数形式の場合がある）
                try:
                    fps = float(fractions.Fraction(fps_str))
                    logging.info(f"FPS変換成功: {fps_str} → {fps}")
                except Exception as e:
                    logging.error(f"FPS変換エラー: fps_str={fps_str}, error={e}")
                    fps = 0
                
                return fps, int(float(width_str)), int(float(height_str))
        
        return None
    except Exception as e:
        logging.error(f"Error checking stream details: {e}")
        return None

def kill_ffmpeg_processes(camera_id=None, pid=None):
    """
    実行中のFFmpegプロセスを強制終了する。
    camera_idまたはpidを指定すると特定のプロセスのみ終了。
    両方指定されていない場合はすべてのFFmpegプロセスを終了。
    
    Args:
        camera_id (str, optional): カメラID
        pid (int, optional): プロセスID
        
    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        logging.info(f"kill_ffmpeg_processes: camera_id={camera_id}, pid={pid} の停止を開始")

        # Windowsの場合はtaskkillコマンドを使用
        if os.name == 'nt':
            if pid:
                # 特定のプロセスIDのみ終了
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], 
                               stdout=subprocess.DEVNULL, 
                               stderr=subprocess.DEVNULL)
                logging.info(f"FFmpegプロセス(PID:{pid})を終了しました")
                return True
            
            # すべてのFFmpegプロセスを検索して終了
            logging.info("taskkillを使用して全てのFFmpegプロセスを終了します")
            try:
                if camera_id:
                    # camera_idに対応するプロセスを探して終了（未実装なので一旦すべて終了）
                    subprocess.run(['taskkill', '/F', '/IM', 'ffmpeg.exe'], 
                                stdout=subprocess.DEVNULL, 
                                stderr=subprocess.DEVNULL)
                else:
                    # すべてのffmpegプロセスを終了
                    subprocess.run(['taskkill', '/F', '/IM', 'ffmpeg.exe'], 
                                stdout=subprocess.DEVNULL, 
                                stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                # プロセスが見つからなかった場合などは無視
                pass
        else:
            # Linuxなどの場合はpsとkillコマンドを使用
            processes = []
            if pid:
                # 特定のプロセスIDのみ
                processes.append(pid)
            else:
                # すべてのFFmpegプロセスを検索
                try:
                    ps_output = subprocess.check_output(['ps', 'aux'], text=True)
                    for line in ps_output.splitlines():
                        if 'ffmpeg' in line and (not camera_id or camera_id in line):
                            parts = line.split()
                            if len(parts) > 1:
                                processes.append(parts[1])  # PID
                except subprocess.CalledProcessError:
                    pass
            
            # 見つかったプロセスを終了
            for proc_pid in processes:
                try:
                    os.kill(int(proc_pid), 9)  # SIGKILL
                    logging.info(f"FFmpegプロセス(PID:{proc_pid})を終了しました")
                except (ProcessLookupError, PermissionError) as e:
                    logging.warning(f"FFmpegプロセス(PID:{proc_pid})の終了に失敗しました: {e}")
        
        logging.info("全てのFFmpegプロセスが正常に終了しました")
        return True
        
    except Exception as e:
        logging.error(f"FFmpegプロセス終了中にエラーが発生しました: {e}")
        return False

def check_audio_stream(rtsp_url, timeout=10):
    """
    RTSPストリームに音声ストリームが含まれているかをチェックする
    
    Note:
        チェックに失敗しても例外をスローせず、Falseを返します

    Args:
        rtsp_url (str): RTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        bool: 音声ストリームが存在するかどうか
    """
    try:
        logging.info(f"音声ストリームの確認: {rtsp_url}")
        
        ffprobe_command = [
            'ffprobe',
            '-v', 'error',
            '-rtsp_transport', 'tcp',
            '-timeout', str(timeout * 1000000),  # マイクロ秒単位
            '-select_streams', 'a:0',  # 最初の音声ストリームを選択
            '-show_entries', 'stream=codec_type',
            '-of', 'json',
            '-i', rtsp_url
        ]
        
        # 音声ストリームの存在確認
        try:
            result = subprocess.run(ffprobe_command, timeout=timeout, capture_output=True, text=True)
            
            # 結果の解析
            if result.returncode == 0:
                # JSON形式の出力を解析
                output = json.loads(result.stdout)
                logging.debug(f"FFprobe streams output: {output}")
                
                # 音声ストリームの有無をチェック
                if output.get('streams') and len(output['streams']) > 0:
                    logging.info(f"Audio stream detected for {rtsp_url}")
                    return True
                else:
                    logging.warning(f"No audio stream found for {rtsp_url}")
                    return False
            else:
                # ffprobeコマンドが失敗した場合
                logging.warning(f"Failed to detect audio stream for {rtsp_url}: {result.stderr}")
                # 失敗しても録画は続けるべきなので、音声なしとみなして続行
                return False
                
        except subprocess.TimeoutExpired:
            logging.warning(f"Timeout detecting audio stream for {rtsp_url}")
            # タイムアウトでも録画は続けるべきなので、音声なしとみなして続行
            return False
            
        except json.JSONDecodeError as je:
            logging.error(f"JSON parsing error when checking audio stream: {je}")
            # 例外が発生しても録画は続けるべきなので、音声なしとみなして続行
            return False
            
    except Exception as e:
        logging.error(f"Error checking audio stream for {rtsp_url}: {e}")
        # 例外が発生しても録画は続けるべきなので、音声なしとみなして続行
        return False

def finalize_recording(file_path):
    """
    録画ファイルを最終化する（メタデータを追加して再生しやすくする）

    Args:
        file_path (str): 最終化する録画ファイルのパス
    """
    try:
        # ファイルが存在し、サイズが0より大きいか確認
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            # FFmpegを使用してファイルを再エンコード
            temp_path = file_path + '.temp.mp4'

            ffmpeg_command = [
                'ffmpeg',
                '-i', file_path,
                '-c:v', 'copy',                   # ビデオストリームをコピー
                '-c:a', 'copy',                   # 音声ストリームをコピー
                '-map_metadata', '0',             # メタデータを保持
                '-movflags', '+faststart',        # MP4ファイルを最適化（ストリーミング向け）
                '-write_tmcd', '0',               # タイムコードを書き込まない
                '-use_editlist', '0',             # 編集リストを無効化
                '-fflags', '+bitexact',           # ビット精度を維持
                '-flags:v', '+global_header',     # グローバルヘッダー設定
                '-ignore_unknown',                # 不明なデータを無視
                '-tag:v', 'avc1',                 # 標準的なH.264タグを使用
                '-y',                             # 出力ファイルを上書き
                temp_path
            ]

            subprocess.run(ffmpeg_command, check=True, capture_output=True)

            # 元のファイルを置き換え
            os.replace(temp_path, file_path)
            logging.info(f"Successfully finalized recording: {file_path}")
        else:
            logging.warning(f"Recording file is empty or does not exist: {file_path}")

    except Exception as e:
        logging.error(f"Error finalizing recording: {e}")

def start_ffmpeg_process(cmd, log_path=None, high_priority=False, show_error=True):
    """
    FFmpegプロセスを起動する
    
    Args:
        cmd (list): FFmpegコマンドのリスト
        log_path (str, optional): ログファイルパス
        high_priority (bool): 高優先度で実行するかどうか
        show_error (bool): エラー出力を表示するかどうか
        
    Returns:
        subprocess.Popen: 起動したプロセス
    """
    try:
        # 開始ログ
        cmd_str = ' '.join(cmd)
        logging.info(f"FFmpeg process starting with command: {cmd_str[:200]}...")
        
        # ログファイルが指定されている場合
        if log_path:
            log_dir = os.path.dirname(log_path)
            os.makedirs(log_dir, exist_ok=True)
            
            with open(log_path, 'w', encoding='utf-8') as log_file:
                # ログにコマンドを記録
                log_file.write(f"FFmpeg log started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                log_file.write(f"Command: {' '.join(cmd[:min(len(cmd), 200)])}")
                if len(cmd) > 200:
                    log_file.write("...\n")
                else:
                    log_file.write("\n")
                log_file.flush()
                
                # プロセス起動設定
                creation_flags = 0
                if os.name == 'nt':
                    creation_flags = subprocess.CREATE_NO_WINDOW
                    
                # プロセスを起動
                process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    creationflags=creation_flags,
                    universal_newlines=True
                )
        else:
            # ログファイルなしの場合
            stdout_target = subprocess.PIPE if show_error else subprocess.DEVNULL
            stderr_target = subprocess.PIPE if show_error else subprocess.DEVNULL
            
            # プロセス起動設定
            creation_flags = 0
            if os.name == 'nt':
                creation_flags = subprocess.CREATE_NO_WINDOW
                
            # プロセスを起動
            process = subprocess.Popen(
                cmd,
                stdout=stdout_target,
                stderr=stderr_target,
                creationflags=creation_flags,
                universal_newlines=True
            )
            
        # 高優先度が指定されている場合（Windowsのみ）
        if high_priority and os.name == 'nt':
            try:
                import psutil
                p = psutil.Process(process.pid)
                p.nice(psutil.HIGH_PRIORITY_CLASS)
                logging.info(f"FFmpeg process priority set to HIGH (PID: {process.pid})")
            except Exception as e:
                logging.warning(f"Failed to set FFmpeg process priority: {e}")
        
        logging.info(f"FFmpeg process started with PID: {process.pid}")
        return process
        
    except Exception as e:
        logging.error(f"Failed to start FFmpeg process: {e}")
        return None

def monitor_ffmpeg_output(process):
    """
    FFmpegプロセスの出力を監視する

    Args:
        process (subprocess.Popen): 監視するFFmpegプロセス
    """
    error_count = 0
    hls_input_detected = False
    recording_started = False
    last_progress_time = time.time()
    
    # stderr がない場合は監視を行わない
    if process.stderr is None:
        logging.warning("FFmpeg process stderr is None, cannot monitor output")
        return
    
    while True:
        try:
            line = process.stderr.readline()
            if not line:
                if process.poll() is not None:
                    logging.warning("FFmpegプロセスが終了しました。出力監視を停止します。")
                    break
                # 読み取りタイムアウトの場合、プロセスのステータスを確認して続行
                if time.time() - last_progress_time > 60:  # 1分以上進捗がない場合
                    logging.warning("FFmpegからの進捗情報が1分以上ありません。プロセスの状態を確認します。")
                    if process.poll() is not None:
                        logging.warning("FFmpegプロセスが終了しています。出力監視を停止します。")
                        break
                    else:
                        logging.info("FFmpegプロセスは実行中です。監視を続行します。")
                        last_progress_time = time.time()  # タイマーをリセット
                time.sleep(1)  # 短い待機時間を設けてCPU使用率を抑える
                continue

            decoded_line = line.decode('utf-8', errors='replace').strip()
            if not decoded_line:
                continue

            # 進捗情報更新
            if "frame=" in decoded_line and "time=" in decoded_line:
                last_progress_time = time.time()

            # HLS入力を使用しているかを検出
            if '/system/cam/tmp/' in decoded_line and '.m3u8' in decoded_line:
                hls_input_detected = True
                logging.info(f"HLSストリームを入力として使用: {decoded_line}")
            
            # 録画開始を検出
            if 'Output #0' in decoded_line and '.mp4' in decoded_line:
                recording_started = True
                logging.info("録画プロセスが出力を開始しました")
                error_count = 0  # 録画開始時点でエラーカウントをリセット

            # エラーメッセージを検出
            if "Error" in decoded_line or "error" in decoded_line.lower():
                error_count += 1
                logging.error(f"FFmpeg error detected: {decoded_line}")
                
                # HLS入力を使用しているプロセスの一般的なエラーを特別処理
                if hls_input_detected and any(err in decoded_line for err in ["Operation not permitted", "Connection refused", "timeout"]):
                    logging.warning(f"HLS入力で一般的なエラーが発生しましたが、処理を継続します: {decoded_line}")
                    # エラーカウントをリセット（このエラーは無視）
                    error_count = max(0, error_count - 1)
                
                # どのカメラでも一般的なネットワークエラーを許容
                if any(network_err in decoded_line for network_err in [
                    "Operation not permitted", 
                    "Connection refused", 
                    "timeout", 
                    "Network is unreachable",
                    "Invalid data",
                    "End of file",
                    "Connection reset by peer",
                    "Protocol error"
                ]):
                    logging.warning(f"一般的なネットワークエラーが発生しましたが、処理を継続します: {decoded_line}")
                    error_count = max(0, error_count - 1)  # エラーカウントを減少
                
                # 深刻な録画エラーの検出
                if recording_started and "Invalid data" in decoded_line:
                    logging.error("録画データの破損が検出されました")
                    error_count += 1  # カウントを増加（既に+1されているので+1追加で合計+2）
                    
                # 致命的なエラーを検出（終了するべきエラー）
                if "Conversion failed!" in decoded_line or "Invalid argument" in decoded_line:
                    logging.critical(f"致命的なFFmpegエラーが検出されました: {decoded_line}")
                    error_count += 5  # エラーカウントを大幅に増加
            else:
                # 通常のログメッセージ
                logging.info(f"FFmpeg output: {decoded_line}")
                
                # 録画の進行状況を示すメッセージを検出
                if "frame=" in decoded_line and "time=" in decoded_line:
                    # 正常に録画が進行中
                    error_count = max(0, error_count - 1)  # エラーカウントを徐々に減少
                    
                    # タイムコード情報を抽出して記録
                    if "time=" in decoded_line:
                        time_parts = decoded_line.split("time=")[1].split()[0]
                        logging.info(f"録画進行中: {time_parts}")

            # 短時間に多数のエラーが発生した場合、プロセスを再起動するべきと判断
            if error_count > 15:
                logging.error("多数のFFmpegエラーが検出されました。プロセスの再起動が必要な可能性があります。")
                break

        except Exception as e:
            logging.error(f"Error in FFmpeg output monitoring: {e}")
            # エラーが発生しても監視は続行
            time.sleep(1)
    
    # ループを抜けた場合、プロセスの最終状態を確認
    exit_code = process.poll()
    if exit_code is not None:
        logging.info(f"FFmpegプロセスが終了しました（終了コード: {exit_code}）")
    else:
        logging.warning("FFmpeg出力モニタリングが終了しましたが、プロセスはまだ実行中です")

def terminate_process(process, timeout=10):
    """
    プロセスを適切に終了させる

    Args:
        process (subprocess.Popen): 終了させるプロセス
        timeout (int): 終了を待つ最大秒数
    """
    if process is None or process.poll() is not None:
        return

    pid = process.pid
    logging.info(f"Terminating FFmpeg process (PID: {pid})...")

    try:
        # 1. まず、qコマンドを送信（標準的な終了シグナル）
        if process.stdin:
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                logging.info(f"Sent 'q' command to FFmpeg process PID: {pid}")
            except Exception as e:
                logging.error(f"Error sending q command to PID {pid}: {e}")

        # 少し待ってからプロセスの状態を確認
        for i in range(3):  # 3回試行
            time.sleep(1)
            if process.poll() is not None:
                logging.info(f"Process PID: {pid} terminated gracefully after 'q' command")
                return  # 正常に終了した場合は早期リターン

        # 2. プロセスがまだ実行中なら、terminateを試す（SIGTERM相当）
        if process.poll() is None:
            logging.info(f"Process PID: {pid} still running after 'q' command, sending terminate signal")
            process.terminate()
            
            # terminateの結果を待つ
            try:
                process.wait(timeout=3)
                if process.poll() is not None:
                    logging.info(f"Process PID: {pid} terminated after terminate signal")
                    return  # 正常に終了した場合は早期リターン
            except subprocess.TimeoutExpired:
                logging.warning(f"Process PID: {pid} did not respond to terminate signal")

        # 3. プロセスがまだ実行中なら、taskkillを使用（強制終了）
        if process.poll() is None:
            try:
                logging.info(f"Using taskkill /F /PID {pid} /T to forcefully terminate process")
                kill_result = subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], 
                                  capture_output=True, text=True)
                
                if kill_result.returncode == 0:
                    logging.info(f"Successfully killed process PID: {pid} using taskkill")
                else:
                    logging.error(f"Taskkill returned error code {kill_result.returncode}: {kill_result.stderr}")
                    raise Exception(f"Taskkill failed: {kill_result.stderr}")
            except Exception as e:
                logging.error(f"Error using taskkill on PID {pid}: {e}")
                
                # 4. 最後の手段としてpsutilを使用
                try:
                    logging.info(f"Attempting to kill PID: {pid} with psutil")
                    parent = psutil.Process(pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.terminate()
                            time.sleep(0.5)
                            if child.is_running():
                                child.kill()
                            logging.info(f"Killed child process with PID: {child.pid}")
                        except Exception as child_e:
                            logging.error(f"Failed to kill child process: {child_e}")
                    
                    parent.terminate()
                    time.sleep(1)
                    if parent.is_running():
                        parent.kill()
                    logging.info(f"Killed parent process with PID: {pid} using psutil")
                except Exception as psutil_e:
                    logging.error(f"Failed to kill process with psutil: {psutil_e}")
                    
                    # 5. 絶対に最後の手段：osのシステムコマンドを直接使用
                    try:
                        logging.warning(f"Executing OS command to kill PID: {pid}")
                        os.system(f"taskkill /F /PID {pid} /T")
                        time.sleep(1)
                        os.system(f"taskkill /F /PID {pid} /T")  # 念のため2回実行
                    except Exception as os_e:
                        logging.error(f"Failed with OS kill command: {os_e}")

        # プロセスの終了を待って確認
        try:
            process.wait(timeout=timeout)
            if process.poll() is not None:
                logging.info(f"Confirmed process PID: {pid} has terminated")
            else:
                logging.warning(f"Process PID: {pid} may still be running after all termination attempts")
        except subprocess.TimeoutExpired:
            logging.warning(f"Process PID: {pid} did not terminate within timeout")
            
        # 最終確認：プロセスがまだ存在するか
        try:
            if psutil.pid_exists(pid):
                logging.critical(f"WARNING: Process PID: {pid} still exists despite all termination attempts")
                # ログファイルにアラート情報を記録
                with open("process_kill_failure.log", "a") as f:
                    f.write(f"{datetime.now()}: Failed to kill process PID: {pid}\n")
            else:
                logging.info(f"Verified process PID: {pid} no longer exists")
        except:
            pass
    except Exception as e:
        logging.error(f"Unexpected error in terminate_process for PID {pid}: {e}")
        logging.exception("Complete error details:")

def get_hls_streaming_command(input_url, output_path, segment_time=1, buffer_size="32768k"):
    """
    HLSストリーミング用のFFmpegコマンドを生成する - FFmpeg 7.1.1対応・時間軸同期
    
    Args:
        input_url (str): 入力URLまたはファイルパス
        output_path (str): 出力パス
        segment_time (int): セグメント長（秒）
        buffer_size (str): バッファサイズ（デフォルト: "32768k"）
        
    Returns:
        list: FFmpegコマンドのリスト
    """
    # 出力ディレクトリのパスを取得
    output_dir = os.path.dirname(output_path)
    # ファイル名部分（拡張子なし）を取得
    filename = os.path.splitext(os.path.basename(output_path))[0]
    
    # 最小限の必須パラメータでコマンドを生成（安定性重視）
    return [
        config.FFMPEG_PATH,
        '-rtsp_transport', 'tcp',
        '-buffer_size', buffer_size,
        '-max_delay', '100000',
        '-analyzeduration', '1000000',
        '-probesize', '1000000',
        '-fflags', '+genpts+discardcorrupt+igndts+ignidx+flush_packets',
        '-err_detect', 'ignore_err',
        '-avoid_negative_ts', 'make_zero',
        '-use_wallclock_as_timestamps', '1',
        '-thread_queue_size', '512',
        '-flags', '+global_header',
        '-i', input_url,
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-tune', 'zerolatency',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '44100',
        '-ac', '2',
        '-async', '1',
        '-vsync', 'cfr',
        '-fps_mode', 'cfr',
        '-force_key_frames', f'expr:gte(t,n_forced*{segment_time})',
        '-sc_threshold', '0',
        '-g', str(segment_time * 30),
        '-movflags', 'empty_moov+omit_tfhd_offset+frag_keyframe+default_base_moof',
        '-hls_time', str(segment_time),
        '-hls_list_size', str(config.HLS_PLAYLIST_SIZE),
        '-hls_flags', 'delete_segments+independent_segments+split_by_time',
        '-hls_segment_type', 'mpegts',
        '-hls_segment_filename', os.path.join(output_dir, f"{filename}-%05d.ts"),
        '-hls_start_number_source', 'datetime',
        '-hls_allow_cache', '0',
        '-start_number', '1',
        '-muxdelay', '0',
        '-muxpreload', '0',
        '-max_muxing_queue_size', '4096',
        '-f', 'hls',
        '-y',
        output_path
    ]

def start_hls_streaming(camera_info):
    """
    HLSストリーミングを開始する
    
    Args:
        camera_info (dict): カメラ情報
        
    Returns:
        subprocess.Popen: 実行中のFFmpegプロセス
    """
    try:
        camera_id = camera_info['id']
        rtsp_url = camera_info['rtsp_url']
        
        # 出力ディレクトリの準備
        output_dir = os.path.join(config.TMP_PATH, str(camera_id))
        os.makedirs(output_dir, exist_ok=True)
        
        # 出力ファイルパス
        output_path = os.path.join(output_dir, f"{camera_id}.m3u8")
        
        # FFmpegコマンドの生成
        command = get_hls_streaming_command(
            rtsp_url,
            output_path,
            segment_time=config.HLS_SEGMENT_DURATION
        )
        
        # ログファイルの準備
        log_path = os.path.join(config.BASE_PATH, 'log', f'ffmpeg_{camera_id}.log')
        log_file = open(log_path, 'a')
        
        # プロセスの開始
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1
        )
        
        logging.info(f"Started HLS streaming for camera {camera_id}")
        return process
        
    except Exception as e:
        logging.error(f"Error starting HLS streaming for camera {camera_id}: {e}")
        return None

def get_ffmpeg_record_command(rtsp_url, output_path, camera_id=None):
    """
    録画用のFFmpegコマンドを生成 - Windows & FFmpeg 7.1.1互換

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): 録画ファイルの出力パス
        camera_id (str, optional): カメラID

    Returns:
        list: FFmpegコマンドのリスト
    """
    # HLSストリーミングを優先的に使用するかどうかをチェック
    use_hls = False
    
    # カメラIDが有効な場合のみHLSストリームを確認
    if camera_id and camera_id != 'None' and camera_id != 'unknown':
        try:
            # HLSストリームの存在を確認（app.pyが稼働中かどうか）
            hls_url = f"http://localhost:5000/system/cam/tmp/{camera_id}/{camera_id}.m3u8"
            logging.info(f"カメラ{camera_id}の録画: HLSストリーム確認を試みます")
            
            response = requests.head(hls_url, timeout=1)  # タイムアウトを短くして高速化
            
            if response.status_code == 200:
                logging.info(f"カメラ{camera_id}はHLSソース（{hls_url}）を使用可能です")
                use_hls = True
            else:
                logging.info(f"カメラ{camera_id}のHLSソースは利用できません: ステータスコード {response.status_code}")
                
        except Exception as e:
            logging.warning(f"HLSストリーム確認中にエラーが発生しました({e})。カメラ{camera_id}はRTSPから直接録画します")
    
    # HLSストリームが利用可能な場合
    if use_hls:
        hls_url = f"http://localhost:5000/system/cam/tmp/{camera_id}/{camera_id}.m3u8"
        logging.info(f"カメラ{camera_id}はHLSソース（{hls_url}）から録画します")
        return [
            'ffmpeg',
            '-protocol_whitelist', 'file,http,https,tcp,tls',
            '-hwaccel', 'cuda',
            '-c:v', 'h264_cuvid',
            '-i', hls_url,
            '-c:v', 'h264_nvenc',
            '-preset', 'fast',
            '-r', '30',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-ac', '2',
            '-max_muxing_queue_size', '2048',
            '-fflags', '+genpts+discardcorrupt+igndts',
            '-avoid_negative_ts', 'make_zero',
            '-start_at_zero',
            '-fps_mode', 'cfr',
            '-async', '1',
            '-movflags', '+faststart+frag_keyframe',
            '-y',
            output_path
        ]
    
    # RTSPストリームを直接使用
    logging.info(f"カメラ{camera_id if camera_id else 'unknown'}はRTSPストリームから直接録画します: {rtsp_url}")
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',
        '-hwaccel', 'cuda',
        '-c:v', 'h264_cuvid',
        '-analyzeduration', '10000000',
        '-probesize', '5000000',
        '-buffer_size', '30720k',
        '-use_wallclock_as_timestamps', '1',
        '-timeout', '10000000',
        '-rw_timeout', '10000000',
        '-xerror', '',
        '-i', rtsp_url,
        '-c:v', 'h264_nvenc',
        '-preset', 'fast',
        '-r', '30',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ar', '44100',
        '-ac', '2',
        '-max_muxing_queue_size', '2048',
        '-fflags', '+genpts+discardcorrupt+igndts',
        '-avoid_negative_ts', 'make_zero',
        '-start_at_zero',
        '-fps_mode', 'cfr',
        '-async', '1',
        '-movflags', '+faststart+frag_keyframe',
        '-y',
        output_path
    ]

def check_hls_stream_available(hls_url):
    """
    指定されたHLS URLが利用可能かチェックする
    
    Args:
        hls_url (str): 確認するHLS URL (m3u8)
        
    Returns:
        bool: 利用可能な場合True
    """
    try:
        # リクエストのタイムアウト設定
        timeout = 3
        
        # GETリクエスト送信
        response = requests.get(hls_url, timeout=timeout)
        
        # ステータスコード確認
        if response.status_code == 200:
            # コンテンツがm3u8形式か簡易チェック
            if "#EXTM3U" in response.text:
                # セグメントファイルが存在するか確認
                has_segments = False
                for line in response.text.splitlines():
                    if line.strip() and not line.startswith('#') and ('.ts' in line):
                        has_segments = True
                        break
                
                if has_segments:
                    logging.info(f"有効なHLSストリームを確認: {hls_url}")
                    return True
                else:
                    logging.warning(f"HLSファイルにセグメントがありません: {hls_url}")
            else:
                logging.warning(f"HLSファイルが正しい形式ではありません: {hls_url}")
        
        logging.warning(f"HLSストリームが利用できません: {hls_url} (ステータスコード: {response.status_code})")
        return False
    except Exception as e:
        logging.warning(f"HLSストリーム確認中にエラー: {hls_url}, {str(e)}")
        return False
