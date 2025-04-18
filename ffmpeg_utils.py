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
import platform
import signal
import shlex

def check_rtsp_connection(rtsp_url, timeout=10):
    """
    RTSP接続の可否をチェックする関数

    Args:
        rtsp_url (str): チェックするRTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        bool: 接続が成功したかどうか
    """
    try:
        # 接続試行回数の設定
        max_retries = 3  # 2から3に増加
        for retry in range(max_retries):
            try:
                # 長い待機時間に対処するため、リトライごとに異なるタイプの接続テストを試みる
                if retry == 0:
                    # 最初の試行: 短いリード間隔で速く確認
                    ffprobe_command = [
                        'ffprobe',
                        '-v', 'error',
                        '-rtsp_transport', 'tcp',  # より信頼性が高いTCPを使用
                        '-i', rtsp_url,
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        '-read_intervals', '%+2'  # より短く、速く
                    ]
                elif retry == 1:
                    # 2回目の試行: UDPで試みる（一部のカメラはUDPの方が良いパフォーマンス）
                    ffprobe_command = [
                        'ffprobe',
                        '-v', 'error',
                        '-rtsp_transport', 'udp',
                        '-i', rtsp_url,
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        '-read_intervals', '%+3'
                    ]
                else:
                    # 最後の試行: より多くのオプションを追加
                    ffprobe_command = [
                        'ffprobe',
                        '-v', 'error',
                        '-rtsp_transport', 'tcp',
                        '-stimeout', '5000000',  # マイクロ秒単位のソケットタイムアウト
                        '-i', rtsp_url,
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        '-read_intervals', '%+4'
                    ]

                # オペレーティングシステムに応じてcreationflagsを設定
                creation_flags = 0
                if os.name == 'nt':
                    creation_flags = subprocess.CREATE_NO_WINDOW
                
                # タイムアウトを設定
                result = subprocess.run(
                    ffprobe_command, 
                    timeout=timeout,
                    capture_output=True,
                    text=True,
                    creationflags=creation_flags
                )

                # 終了コードが0なら接続成功
                if result.returncode == 0:
                    logging.info(f"RTSP connection successful: {rtsp_url} (attempt {retry+1}/{max_retries})")
                    return True
                else:
                    # エラー出力にRTSP関連のエラーが含まれるか確認
                    error_output = result.stderr
                    if "RTSP" in error_output or "Timeout" in error_output:
                        logging.warning(f"RTSP connection specific error: {rtsp_url}, Error: {error_output}")
                    else:
                        logging.warning(f"RTSP connection failed: {rtsp_url}, Error: {error_output}")
                    
                    if retry < max_retries - 1:
                        logging.info(f"Retrying RTSP connection ({retry+1}/{max_retries})...")
                        time.sleep(2)  # 再試行前に少し待機
                    else:
                        return False

            except subprocess.TimeoutExpired:
                logging.error(f"RTSP connection timeout: {rtsp_url} (attempt {retry+1}/{max_retries})")
                
                # タイムアウトした場合、残っている可能性のあるffprobeプロセスを強制終了
                kill_ffprobe_processes()
                
                if retry < max_retries - 1:
                    logging.info(f"Retrying after timeout ({retry+1}/{max_retries})...")
                    time.sleep(2)  # 再試行前に少し待機
                else:
                    return False

        return False  # すべての再試行が失敗

    except Exception as e:
        logging.error(f"Error checking RTSP connection: {rtsp_url}, Error: {e}")
        return False

def kill_ffprobe_processes():
    """ffprobeプロセスを強制終了する関数"""
    try:
        # OSに応じて異なる処理
        if os.name == 'nt':
            # Windows
            subprocess.run('taskkill /F /IM ffprobe.exe', shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            # Linux/Mac
            os.system("pkill -f ffprobe")
    except Exception as e:
        logging.error(f"Error killing ffprobe processes: {e}")

def kill_ffmpeg_processes(camera_id=None):
    """
    ffmpegプロセスを強制終了する関数

    Args:
        camera_id (str, optional): 特定のカメラID。指定されない場合は全てのffmpegプロセスを終了

    Returns:
        bool: 終了処理が成功したかどうか
    """
    try:
        # まずpsutilを使用してより詳細にプロセスを特定
        try:
            found_processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                # ffmpegプロセスを探す
                try:
                    if 'ffmpeg' in proc.info['name'].lower():
                        if camera_id:
                            # コマンドラインにカメラIDが含まれているか確認
                            cmdline = proc.info.get('cmdline', [])
                            cmdline_str = ' '.join(cmdline) if cmdline else ''
                            if camera_id in cmdline_str:
                                found_processes.append(proc)
                        else:
                            found_processes.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                    
            # 見つかったプロセスを終了
            for proc in found_processes:
                try:
                    proc.terminate()
                    logging.info(f"Terminated ffmpeg process with PID: {proc.pid}")
                    # プロセスが終了するのを少し待つ
                    gone, alive = psutil.wait_procs([proc], timeout=3)
                    
                    # 終了しない場合は強制終了
                    if proc in alive:
                        proc.kill()
                        logging.info(f"Killed ffmpeg process with PID: {proc.pid}")
                except Exception as e:
                    logging.error(f"Error terminating process with PID {proc.pid}: {e}")
                    
            if found_processes:
                logging.info(f"Terminated {len(found_processes)} ffmpeg processes")
                return True
                
        except Exception as psutil_error:
            logging.warning(f"Error using psutil to kill ffmpeg processes: {psutil_error}")
            
        # OS固有の方法にフォールバック
        if os.name == 'nt':
            # Windows
            try:
                # tasklist コマンドを実行してffmpegプロセスを検索
                task_cmd = 'tasklist /FI "IMAGENAME eq ffmpeg.exe" /FO CSV'
                result = subprocess.check_output(task_cmd, shell=True, creationflags=subprocess.CREATE_NO_WINDOW).decode()
                
                # CSVフォーマットをパース
                import csv
                from io import StringIO
                reader = csv.reader(StringIO(result))
                next(reader)  # ヘッダー行をスキップ
                
                pids = []
                for row in reader:
                    if row:  # 行が空でない場合
                        pid = row[1].strip('"')  # "1234" → 1234
                        
                        # 特定のカメラIDが指定された場合、プロセスの引数をチェック
                        if camera_id:
                            try:
                                # wmic コマンドでコマンドラインを取得
                                wmic_cmd = f'wmic process where processid="{pid}" get commandline'
                                cmdline = subprocess.check_output(wmic_cmd, shell=True, creationflags=subprocess.CREATE_NO_WINDOW).decode()
                                
                                # コマンドラインに特定のカメラIDが含まれているか確認
                                if camera_id in cmdline:
                                    pids.append(pid)
                            except:
                                continue
                        else:
                            pids.append(pid)

                # 見つかった各PIDに対してtaskkillを実行
                for pid in pids:
                    try:
                        kill_command = f'taskkill /F /PID {pid}'
                        subprocess.run(kill_command, shell=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                        logging.info(f'Killed ffmpeg process with PID: {pid}')
                    except subprocess.TimeoutExpired:
                        logging.warning(f'Timeout killing process with PID: {pid}')
                    except Exception as e:
                        logging.error(f'Error killing process with PID {pid}: {e}')

                if not pids:
                    logging.info('No ffmpeg processes found to kill.')
                    return False
                    
                return True

            except subprocess.CalledProcessError:
                logging.info('No ffmpeg processes found.')
                return False
                
        else:
            # Linux/Mac向け
            try:
                # カメラIDが指定されている場合はそのIDを含むプロセスのみを終了
                cmd = "ps aux | grep ffmpeg"
                if camera_id:
                    cmd += f" | grep {camera_id}"
                cmd += " | grep -v grep"
                
                result = subprocess.check_output(cmd, shell=True).decode('utf-8')
                
                # 各行からPIDを抽出
                pids = []
                for line in result.splitlines():
                    parts = line.split()
                    if len(parts) > 1:
                        pid = parts[1]
                        pids.append(pid)
                
                # プロセスを終了
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                        logging.info(f'Killed ffmpeg process with PID: {pid}')
                    except Exception as e:
                        logging.error(f'Error killing process with PID {pid}: {e}')
                
                return len(pids) > 0
                
            except Exception as e:
                logging.error(f'Error searching for ffmpeg processes: {e}')
                return False

        return True  # 処理が完了

    except Exception as e:
        logging.error(f'An error occurred during killing ffmpeg processes: {str(e)}')
        return False

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
            '-rtsp_transport', 'tcp',
            '-print_format', 'json',
            '-show_streams',
            '-i', rtsp_url
        ]

        # オペレーティングシステムに応じてcreationflagsを設定
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(ffprobe_command, capture_output=True, text=True, timeout=10,
                              creationflags=creation_flags)
        
        if result.returncode != 0:
            logging.warning(f"Failed to get stream info: {result.stderr}")
            return False
            
        try:
            stream_info = json.loads(result.stdout)
            
            # 音声ストリームの確認
            has_audio = any(stream.get('codec_type') == 'audio' for stream in stream_info.get('streams', []))
            if not has_audio:
                logging.warning(f"No audio stream detected in RTSP URL: {rtsp_url}")
                
            return has_audio
        except json.JSONDecodeError:
            logging.error(f"Error parsing JSON from ffprobe output: {result.stdout}")
            return False

    except subprocess.TimeoutExpired:
        logging.error(f"Timeout while checking audio stream: {rtsp_url}")
        # タイムアウトした場合、残っている可能性のあるffprobeプロセスを強制終了
        kill_ffprobe_processes()
        return False
    except Exception as e:
        logging.error(f"Error checking audio stream: {e}")
        return False

def finalize_recording(file_path):
    """
    録画ファイルを最終化する（メタデータを追加して再生しやすくする）

    Args:
        file_path (str): 最終化する録画ファイルのパス
        
    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        # ファイルが存在し、サイズが0より大きいか確認
        if not os.path.exists(file_path):
            logging.warning(f"Recording file does not exist: {file_path}")
            return False
            
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            logging.warning(f"Recording file is empty: {file_path}")
            return False
            
        # 小さすぎるファイルは破損している可能性
        if file_size < 10 * 1024:  # 10KB未満
            logging.warning(f"Recording file is too small, possibly corrupted: {file_path} ({file_size} bytes)")
            return False

        # 既存ファイルの整合性確認
        if not check_file_integrity(file_path):
            logging.warning(f"Recording file integrity check failed: {file_path}")
            return False

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

        # オペレーティングシステムに応じてcreationflagsを設定
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(ffmpeg_command, capture_output=True, text=True, creationflags=creation_flags)
        
        if result.returncode != 0:
            logging.error(f"Failed to finalize recording: {result.stderr}")
            return False

        # 元のファイルを置き換え
        try:
            # 処理完了を少し待つ
            time.sleep(1)
            
            # 生成されたファイルの存在確認
            if not os.path.exists(temp_path):
                logging.error(f"Temporary file was not created: {temp_path}")
                return False
                
            # 一時ファイルのサイズをチェック
            temp_size = os.path.getsize(temp_path)
            if temp_size < 1024:  # 1KB未満
                logging.error(f"Temporary file is too small: {temp_path} ({temp_size} bytes)")
                return False
            
            os.replace(temp_path, file_path)
            logging.info(f"Successfully finalized recording: {file_path}")
            return True
            
        except PermissionError:
            logging.error(f"Permission error replacing file: {file_path}")
            # 少し待ってからもう一度試みる
            time.sleep(1)
            try:
                os.replace(temp_path, file_path)
                logging.info(f"Successfully finalized recording after retry: {file_path}")
                return True
            except Exception as e:
                logging.error(f"Failed to replace file after retry: {e}")
                return False
        except Exception as e:
            logging.error(f"Failed to replace file: {e}")
            return False

    except Exception as e:
        logging.error(f"Error finalizing recording: {e}")
        return False

def check_file_integrity(file_path):
    """
    録画ファイルの整合性をチェック
    
    Args:
        file_path (str): チェックするファイルパス
        
    Returns:
        bool: ファイルが整合性チェックに合格したかどうか
    """
    try:
        # FFprobeを使用してファイルの整合性を確認
        ffprobe_command = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_type',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]

        # オペレーティングシステムに応じてcreationflagsを設定
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(ffprobe_command, capture_output=True, text=True, timeout=10, creationflags=creation_flags)
        
        # エラーが発生した場合は整合性チェック失敗
        if result.returncode != 0:
            logging.warning(f"File integrity check failed: {file_path}, Error: {result.stderr}")
            return False
            
        # ビデオストリームが存在するか確認
        if 'video' not in result.stdout:
            logging.warning(f"No video stream found in file: {file_path}")
            return False
            
        return True
        
    except subprocess.TimeoutExpired:
        logging.error(f"Timeout checking file integrity: {file_path}")
        return False
    except Exception as e:
        logging.error(f"Error checking file integrity: {file_path}, Error: {e}")
        return False

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
        # オペレーティングシステムに応じてcreationflagsを設定
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW
            if high_priority:
                creation_flags |= subprocess.HIGH_PRIORITY_CLASS

        # ログファイルが指定されている場合
        if log_path:
            try:
                with open(log_path, 'w', encoding='utf-8') as log_file:
                    process = subprocess.Popen(
                        command,
                        stdout=log_file,
                        stderr=log_file,
                        creationflags=creation_flags
                    )
            except Exception as e:
                logging.error(f"Error opening log file {log_path}: {e}")
                # ログファイルが開けなくても処理を続行
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
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

        # プロセスの状態を確認（数秒待って終了したか）
        time.sleep(1)
        if process.poll() is not None:
            return_code = process.poll()
            stderr_output = ""
            if process.stderr:
                try:
                    stderr_output = process.stderr.read().decode('utf-8', errors='replace')
                except:
                    stderr_output = "Could not read stderr"
            
            logging.error(f"FFmpeg process terminated immediately with code {return_code}: {stderr_output}")
            # プロセスが即座に終了した場合はNoneを返す
            return None
        
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
    if not process or not process.stderr:
        return
        
    while True:
        try:
            if process.poll() is not None:
                # プロセスが終了した場合は監視を終了
                logging.info(f"FFmpeg process with PID {process.pid} has exited with code {process.returncode}")
                break
                
            line = process.stderr.readline()
            if not line:
                break

            try:
                decoded_line = line.decode('utf-8', errors='replace').strip()
                if decoded_line:
                    # ログレベルを調整（デバッグ情報は省略）
                    if "Error" in decoded_line or "error" in decoded_line:
                        logging.error(f"FFmpeg error: {decoded_line}")
                    elif "Warning" in decoded_line or "warning" in decoded_line:
                        logging.warning(f"FFmpeg warning: {decoded_line}")
                    else:
                        # 頻繁なログを避けるため、重要な情報のみログ
                        important_keywords = ["Stream", "fps", "bitrate", "Opening", "Duration", "Output"]
                        if any(keyword in decoded_line for keyword in important_keywords):
                            logging.info(f"FFmpeg: {decoded_line}")
            except Exception as e:
                logging.error(f"Error decoding FFmpeg output: {e}")

        except Exception as e:
            logging.error(f"Error in FFmpeg output monitoring: {e}")
            break

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
        pid = process.pid
        logging.info(f"Terminating process with PID: {pid}")
        
        # まず、qコマンドを送信
        if process.stdin:
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                logging.info(f"Sent 'q' command to FFmpeg process with PID: {pid}")

            except Exception as e:
                logging.error(f"Error sending q command to process {pid}: {e}")

        # 少し待ってからプロセスの状態を確認
        start_time = time.time()
        while process.poll() is None and time.time() - start_time < 2:
            time.sleep(0.1)

        # プロセスがまだ実行中ならterminateを試す
        if process.poll() is None:
            try:
                process.terminate()
                logging.info(f"Terminated process {pid} using terminate()")
                
                # 少し待ってからプロセスの状態を確認
                start_time = time.time()
                while process.poll() is None and time.time() - start_time < 2:
                    time.sleep(0.1)
            except Exception as e:
                logging.error(f"Error terminating process {pid}: {e}")

        # プロセスがまだ実行中なら、最後の手段としてkill
        if process.poll() is None:
            try:
                # Windowsの場合taskkillを使用
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], 
                                  check=False, capture_output=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    # Linux/Macの場合はSIGKILL
                    os.kill(pid, signal.SIGKILL)
                
                logging.info(f"Killed process {pid} forcefully")
            except Exception as e:
                logging.error(f"Error killing process {pid}: {e}")
                
                # 最後の手段としてpsutil
                try:
                    parent = psutil.Process(pid)
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
                    logging.info(f"Killed process {pid} using psutil")
                except Exception as sub_e:
                    logging.error(f"Failed to kill process {pid} with psutil: {sub_e}")

        # ストリームのクローズ
        for stream in [process.stdin, process.stdout, process.stderr]:
            if stream:
                try:
                    stream.close()
                except Exception as e:
                    pass

    except Exception as e:
        logging.error(f"Error in terminate_process: {e}")

def get_ffmpeg_hls_command(rtsp_url, output_path, segment_filename, segment_time=1.5, list_size=15):
    """
    HLSストリーミング用のFFmpegコマンドを生成（安定性向上版）

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): .m3u8ファイルの出力パス
        segment_filename (str): セグメントファイルのファイル名パターン
        segment_time (float): セグメント長（秒）
        list_size (int): プレイリストのサイズ

    Returns:
        list: FFmpegコマンドのリスト
    """
    # 出力ディレクトリを取得 (m3u8ファイルがあるディレクトリ)
    output_dir = os.path.dirname(output_path)
    # セグメントファイルの完全パス
    segment_path = os.path.join(output_dir, segment_filename)
    
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',            # RTSPトランスポートにTCPを使用
        '-buffer_size', '32768k',            # バッファサイズを増加
        '-fflags', '+genpts+nobuffer+igndts+flush_packets',  # フラグを追加して安定性向上
        '-use_wallclock_as_timestamps', '1',
        '-re',                               # リアルタイムで読み込み
        '-stimeout', '5000000',              # RTSPタイムアウト5秒（マイクロ秒単位）
        '-i', rtsp_url,
        '-reset_timestamps', '1',
        '-vsync', 'passthrough',             # タイムスタンプを保持
        '-copyts',                           # タイムスタンプをコピー
        '-avoid_negative_ts', 'make_zero',   # 負のタイムスタンプを回避
        '-max_delay', '500000',              # 最大遅延500ms
        '-max_interleave_delta', '0',        # インターリーブデルタを無効に
        '-reconnect', '1',
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',         # 最大5秒の再接続遅延
        '-timeout', '5000000',               # タイムアウト5秒（マイクロ秒単位）
        '-err_detect', 'ignore_err',
        '-analyzeduration', '2000000',       # 分析時間2秒
        '-probesize', '10M',                 # プローブサイズを増加
        '-thread_queue_size', '32768',       # スレッドキューサイズを増加
        '-c:v', 'copy',                      # ビデオをそのままコピー
        '-c:a', 'aac',                       # 音声はAACに変換
        '-b:a', '128k',
        '-ar', '44100',
        '-ac', '2',
        '-f', 'hls',
        '-hls_time', str(segment_time),      # 短いセグメント時間で応答性向上
        '-hls_list_size', str(list_size),    # リストサイズを増加
        '-hls_flags', 'delete_segments+append_list+program_date_time+independent_segments+discont_start',  # フラグ追加
        '-hls_segment_type', 'mpegts',
        '-hls_init_time', '0',               # 初期セグメント時間
        '-hls_segment_filename', segment_path,
        output_path
    ]

def get_ffmpeg_record_command(rtsp_url, output_path):
    """
    録画用のFFmpegコマンドを生成（安定性向上版）

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): 録画ファイルの出力パス

    Returns:
        list: FFmpegコマンドのリスト
    """
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',             # TCPトランスポートを使用
        '-buffer_size', '32768k',             # バッファサイズを大幅に増加
        '-fflags', '+genpts+nobuffer+igndts', # フラグ追加
        '-use_wallclock_as_timestamps', '1',  # タイムスタンプの処理を改善
        '-stimeout', '5000000',               # RTSP接続タイムアウト（マイクロ秒）
        '-i', rtsp_url,
        '-reset_timestamps', '1',             # タイムスタンプをリセット
        '-vsync', 'passthrough',              # ビデオ同期調整
        '-avoid_negative_ts', 'make_zero',    # 負のタイムスタンプを回避
        '-reconnect', '1',                    # 接続が切れた場合に再接続を試みる
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',          # 最大再接続遅延を5秒に設定
        '-err_detect', 'ignore_err',          # エラー検出モードを設定
        '-thread_queue_size', '16384',        # 入力バッファサイズを調整
        '-analyzeduration', '2000000',        # 入力ストリームの分析時間を2秒に
        '-probesize', '32M',                  # プローブサイズを調整
        '-c:v', 'copy',                       # ビデオコーデックをそのままコピー
        '-c:a', 'aac',                        # 音声コーデックをAACに設定
        '-b:a', '128k',                       # 音声ビットレート
        '-ar', '44100',                       # サンプリングレート
        '-ac', '2',                           # ステレオ音声
        '-async', '1',                        # 音声の同期モード
        '-max_delay', '500000',               # 最大遅延時間（マイクロ秒）
        '-max_muxing_queue_size', '1024',     # 多重化キューサイズを増加
        '-movflags', '+faststart+write_colr', # ファストスタートフラグを設定
        '-y',                                 # 既存のファイルを上書き
        output_path
    ]
