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
import signal

def kill_ffmpeg_processes(camera_id=None):
    """
    ffmpegプロセスを強制終了する関数

    Args:
        camera_id (str, optional): 特定のカメラID。指定されない場合は全てのffmpegプロセスを終了

    Returns:
        bool: 終了処理が成功したかどうか
    """
    try:
        # psutilを使ってffmpegプロセスを検索（より信頼性の高い方法）
        killed = False
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'ffmpeg' in proc.info['name'].lower():
                    # 特定のカメラIDが指定された場合、プロセスの引数をチェック
                    if camera_id:
                        cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                        if camera_id in cmdline:
                            logging.info(f"Killing ffmpeg process with PID {proc.pid} for camera {camera_id}")
                            try:
                                proc.kill()
                                killed = True
                            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                                logging.warning(f"Failed to kill process {proc.pid}: {e}")
                    else:
                        logging.info(f"Killing ffmpeg process with PID {proc.pid}")
                        try:
                            proc.kill()
                            killed = True
                        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                            logging.warning(f"Failed to kill process {proc.pid}: {e}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # 補助的にtaskkillも使用（Windows環境のため）
        try:
            if camera_id:
                result = subprocess.run(f'tasklist /fi "imagename eq ffmpeg.exe" /fo csv /nh', 
                                      shell=True, capture_output=True, text=True)
                if 'ffmpeg.exe' in result.stdout:
                    subprocess.run(f'taskkill /f /im ffmpeg.exe', shell=True, 
                                  capture_output=True, check=False)
                    killed = True
            else:
                subprocess.run(f'taskkill /f /im ffmpeg.exe', shell=True, 
                              capture_output=True, check=False)
                killed = True
        except Exception as e:
            logging.warning(f"Error using taskkill: {e}")

        if not killed:
            logging.info('No ffmpeg processes found to kill.')
            return False

        return True

    except Exception as e:
        logging.error(f'An error occurred during killing ffmpeg processes: {str(e)}')
        return False

def check_rtsp_connection(rtsp_url, timeout=5):
    """
    RTSPストリームに接続できるかどうかを確認

    Args:
        rtsp_url (str): チェックするRTSP URL
        timeout (int): 接続タイムアウト（秒）

    Returns:
        bool: 接続可能かどうか
        str: エラーメッセージ（成功した場合は空文字）
    """
    try:
        # FFprobeを使用してRTSPストリームをチェック
        ffprobe_command = [
            'ffprobe',
            '-v', 'error',
            '-rtsp_transport', 'tcp',
            '-stimeout', f'{timeout * 1000000}',  # マイクロ秒単位
            '-i', rtsp_url,
            '-show_entries', 'stream=codec_type',
            '-of', 'json',
            '-timeout', f'{timeout}'
        ]

        # タイムアウト付きでプロセスを実行
        process = subprocess.run(
            ffprobe_command, 
            capture_output=True, 
            text=True,
            timeout=timeout + 2  # 少し余裕を持たせる
        )

        # エラー出力を確認
        if process.returncode != 0:
            return False, process.stderr.strip()

        # 出力をJSON形式で解析
        try:
            result = json.loads(process.stdout)
            if 'streams' in result and len(result['streams']) > 0:
                logging.info(f"RTSP connection successful: {rtsp_url}")
                return True, ""
            else:
                return False, "No streams found in RTSP source"
        except json.JSONDecodeError:
            return False, "Invalid JSON response from ffprobe"

    except subprocess.TimeoutExpired:
        return False, f"Connection timeout after {timeout} seconds"

    except Exception as e:
        return False, f"Error checking RTSP connection: {str(e)}"

def check_audio_stream(rtsp_url):
    """
    RTSPストリームに音声が含まれているかチェック

    Args:
        rtsp_url (str): チェックするRTSP URL

    Returns:
        bool: 音声ストリームが含まれているかどうか
    """
    try:
        # FFprobeを使用してストリーム情報を取得
        ffprobe_command = [
            'ffprobe',
            '-v', 'quiet',
            '-rtsp_transport', 'tcp',  # TCPトランスポート使用
            '-stimeout', '5000000',    # 5秒タイムアウト
            '-print_format', 'json',
            '-show_streams',
            '-i', rtsp_url
        ]

        result = subprocess.run(ffprobe_command, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logging.warning(f"Failed to check audio stream: {result.stderr}")
            return False
            
        stream_info = json.loads(result.stdout)

        # 音声ストリームの確認
        has_audio = any(stream.get('codec_type') == 'audio' for stream in stream_info.get('streams', []))
        if not has_audio:
            logging.warning(f"No audio stream detected in RTSP URL: {rtsp_url}")

        return has_audio

    except Exception as e:
        logging.error(f"Error checking audio stream: {e}")
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
                '-c', 'copy',
                '-movflags', '+faststart',
                '-y',
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

def start_ffmpeg_process(command, log_path=None, high_priority=True):
    """
    FFmpegプロセスを開始する

    Args:
        command (list): FFmpegコマンドと引数のリスト
        log_path (str, optional): ログ出力先のパス
        high_priority (bool): 高優先度で実行するかどうか

    Returns:
        subprocess.Popen: 生成されたプロセスオブジェクト
    """
    try:
        creation_flags = subprocess.CREATE_NO_WINDOW
        if high_priority:
            creation_flags |= subprocess.HIGH_PRIORITY_CLASS

        logging.info(f"Starting FFmpeg with command: {' '.join(command)}")

        if log_path:
            with open(log_path, 'w', encoding='utf-8') as log_file:
                process = subprocess.Popen(
                    command,
                    stdout=log_file,
                    stderr=subprocess.PIPE,  # エラー出力はPIPEに変更して監視できるようにする
                    creationflags=creation_flags
                )
        else:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags
            )

        logging.info(f"Started FFmpeg process with PID: {process.pid}")

        # エラー出力を監視するスレッド
        monitor_thread = threading.Thread(
            target=monitor_ffmpeg_output,
            args=(process,),
            daemon=True
        )
        monitor_thread.start()

        return process

    except Exception as e:
        logging.error(f"Failed to start FFmpeg process: {e}")
        raise

def monitor_ffmpeg_output(process):
    """
    FFmpegプロセスの出力を監視する

    Args:
        process (subprocess.Popen): 監視するFFmpegプロセス
    """
    if not process or not process.stderr:
        logging.warning("Cannot monitor FFmpeg output: invalid process or stderr stream")
        return

    while True:
        try:
            # プロセスが終了していないか確認
            if process.poll() is not None:
                break

            # ノンブロッキングでエラー出力を取得
            line = process.stderr.readline()
            if not line:
                time.sleep(0.1)  # 短時間の待機後にチェック継続
                continue

            decoded_line = line.decode('utf-8', errors='replace').strip()
            if decoded_line:
                # 重大度に応じてログレベルを変更
                if "Error" in decoded_line or "error" in decoded_line:
                    logging.error(f"FFmpeg error: {decoded_line}")
                elif "Warning" in decoded_line or "warning" in decoded_line:
                    logging.warning(f"FFmpeg warning: {decoded_line}")
                else:
                    logging.debug(f"FFmpeg output: {decoded_line}")

        except Exception as e:
            logging.error(f"Error in FFmpeg output monitoring: {e}")
            break

    # プロセスが終了した場合のエラーコード確認
    exit_code = process.poll()
    if exit_code is not None and exit_code != 0:
        logging.error(f"FFmpeg process exited with code: {exit_code}")

        # 残りのエラー出力をすべて読み出す
        try:
            remaining_output = process.stderr.read()
            if remaining_output:
                remaining_output = remaining_output.decode('utf-8', errors='replace')
                logging.error(f"Final FFmpeg error output: {remaining_output}")
        except Exception as e:
            logging.warning(f"Failed to read remaining stderr: {e}")

def terminate_process(process, timeout=5):
    """
    プロセスを適切に終了させる

    Args:
        process (subprocess.Popen): 終了させるプロセス
        timeout (int): 終了を待つ最大秒数
    """
    if process is None or process.poll() is not None:
        return

    try:
        # まず、qコマンドを送信
        if process.stdin:
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                logging.info("Sent 'q' command to FFmpeg process")
            except Exception as e:
                logging.warning(f"Error sending q command: {e}")

        # 少し待ってからプロセスの状態を確認
        for _ in range(3):  # 最大3秒待機
            time.sleep(1)
            if process.poll() is not None:
                logging.info(f"Process terminated gracefully with code {process.poll()}")
                break

        # プロセスがまだ実行中なら、SIGTERMで終了を試みる
        if process.poll() is None:
            try:
                process.terminate()
                logging.info("Sent SIGTERM to process")
                
                # 終了を待つ
                try:
                    process.wait(timeout=2)
                    logging.info("Process terminated with SIGTERM")
                except subprocess.TimeoutExpired:
                    logging.warning("Process did not respond to SIGTERM")
            except Exception as e:
                logging.warning(f"Error terminating process: {e}")

        # それでも実行中なら、SIGKILLを送信
        if process.poll() is None:
            try:
                process.kill()
                logging.info("Sent SIGKILL to process")
                
                # 終了を待つ
                try:
                    process.wait(timeout=2)
                    logging.info("Process killed with SIGKILL")
                except subprocess.TimeoutExpired:
                    logging.warning("Process did not respond to SIGKILL")
            except Exception as e:
                logging.warning(f"Error killing process: {e}")

        # 最後の手段として、Windows固有の方法でプロセスを強制終了
        if process.poll() is None:
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                              check=True, capture_output=True)
                logging.info(f"Successfully killed process using taskkill")
            except Exception as e:
                logging.error(f"Error using taskkill: {e}")

                # psutilでの最後の試み
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
                    logging.info("Killed process using psutil")
                except Exception as sub_e:
                    logging.error(f"Failed to kill process with psutil: {sub_e}")

        # ストリームのクローズ
        for stream in [process.stdin, process.stdout, process.stderr]:
            if stream:
                try:
                    stream.close()
                except Exception as e:
                    logging.debug(f"Error closing stream: {e}")

    except Exception as e:
        logging.error(f"Error terminating process: {e}")

def get_ffmpeg_hls_command(rtsp_url, output_path, segment_path, segment_time=2, list_size=5):
    """
    HLSストリーミング用のFFmpegコマンドを生成

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): .m3u8ファイルの出力パス
        segment_path (str): セグメントファイルのパスパターン
        segment_time (int): セグメント長（秒）
        list_size (int): プレイリストのサイズ

    Returns:
        list: FFmpegコマンドのリスト
    """
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',         # TCPトランスポートを使用（より安定）
        '-use_wallclock_as_timestamps', '1',
        '-stimeout', '5000000',           # RTSP接続タイムアウト（5秒）
        '-i', rtsp_url,
        '-reset_timestamps', '1',
        '-buffer_size', '10240k',         # バッファサイズを大きく
        '-max_delay', '500000',           # 最大遅延時間を設定
        '-reconnect', '1',
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',      # 再接続最大遅延を増加
        '-timeout', '10000000',           # 操作タイムアウト（10秒）
        '-thread_queue_size', '8192',     # スレッドキューサイズを増加
        '-analyzeduration', '2147483647', # 入力ストリームの分析時間を延長
        '-probesize', '2147483647',       # プローブサイズを増やす
        '-c:v', 'copy',                   # ビデオはコピー
        '-c:a', 'aac',                    # 音声はAACに変換
        '-b:a', '128k',
        '-ar', '44100',
        '-ac', '2',
        '-f', 'hls',                      # フォーマット明示的に指定
        f'-hls_time', str(segment_time),
        f'-hls_list_size', str(list_size),
        '-hls_flags', 'delete_segments+append_list+program_date_time+independent_segments',
        '-hls_segment_type', 'mpegts',
        '-hls_allow_cache', '1',
        '-hls_segment_filename', segment_path,
        '-loglevel', 'warning',           # ログレベルをwarningに設定
        '-y',                             # 既存ファイルを上書き
        output_path
    ]

def get_ffmpeg_record_command(rtsp_url, output_path):
    """
    録画用のFFmpegコマンドを生成

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): 録画ファイルの出力パス

    Returns:
        list: FFmpegコマンドのリスト
    """
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',             # TCPトランスポートを使用
        '-use_wallclock_as_timestamps', '1',  # タイムスタンプの処理を改善
        '-stimeout', '5000000',               # RTSP接続タイムアウト（5秒）
        '-i', rtsp_url,
        '-reset_timestamps', '1',             # タイムスタンプをリセット
        '-buffer_size', '10240k',             # バッファサイズを増加
        '-max_delay', '500000',               # 最大遅延時間を設定
        '-reconnect', '1',                    # 接続が切れた場合に再接続を試みる
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',          # 最大再接続遅延を5秒に設定
        '-thread_queue_size', '4096',         # 入力バッファサイズを増やす
        '-analyzeduration', '2147483647',     # 入力ストリームの分析時間を延長
        '-probesize', '2147483647',           # プローブサイズを増やす
        '-c:v', 'copy',                       # ビデオコーデックをそのままコピー
        '-c:a', 'aac',                        # 音声コーデックをAACに設定
        '-b:a', '128k',                       # 音声ビットレート
        '-ar', '44100',                       # サンプリングレート
        '-ac', '2',                           # ステレオ音声
        '-async', '1',                        # 音声の同期モード
        '-loglevel', 'warning',               # ログレベルをwarningに設定
        '-movflags', '+faststart',            # ファストスタートフラグを設定
        '-y',                                 # 既存のファイルを上書き
        output_path
    ]

def wait_for_hls_file(hls_path, timeout=10):
    """
    HLSファイルが作成されるのを待つ

    Args:
        hls_path (str): 待機するHLSファイルパス
        timeout (int): タイムアウト時間（秒）

    Returns:
        bool: ファイルが作成されたかどうか
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        if os.path.exists(hls_path):
            # ファイルが存在し、サイズが0でない場合
            if os.path.getsize(hls_path) > 0:
                logging.info(f"HLS file created successfully: {hls_path}")
                return True
        
        # 少し待機
        time.sleep(0.5)
    
    # タイムアウト
    logging.warning(f"HLS file not created for {hls_path} after {timeout} seconds")
    return False
