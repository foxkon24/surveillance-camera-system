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
import socket
import signal

def check_rtsp_connection(rtsp_url, timeout=3):
    """
    RTSP接続の可否をチェックする関数

    Args:
        rtsp_url (str): チェックするRTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        bool: 接続が成功したかどうか
    """
    try:
        # URLからホストとポートを抽出
        # rtsp://username:password@host:port/path のような形式
        parts = rtsp_url.split('@')
        if len(parts) > 1:
            # 認証情報がある場合
            host_part = parts[1]
        else:
            # 認証情報がない場合
            host_part = parts[0].split('//')[1]
        
        # ホスト部分からホストとポートを取得
        host_port = host_part.split('/')[0]
        if ':' in host_port:
            host, port_str = host_port.split(':')
            port = int(port_str)
        else:
            host = host_port
            port = 554  # デフォルトRTSPポート
        
        # ソケット接続でホストの到達性を確認
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result == 0:
            logging.info(f"RTSP connection successful: {rtsp_url}")
            return True
        else:
            logging.warning(f"RTSP connection failed: {rtsp_url}")
            return False

    except Exception as e:
        logging.error(f"Error checking RTSP connection: {e}")
        return False

def kill_ffmpeg_processes(camera_id=None):
    """
    実行中のFFmpegプロセスを終了する
    
    Args:
        camera_id (int, optional): 特定のカメラIDに関連するプロセスのみを終了する場合に指定
                                 未指定の場合は特定のカメラに紐づかないFFmpegプロセスのみを終了
    """
    try:
        # psutilを使用して実行中のすべてのプロセスを取得
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # FFmpegプロセスを探す
                if proc.info['name'] and 'ffmpeg' in proc.info['name'].lower():
                    kill_process = False
                    cmd_line = ' '.join(proc.info['cmdline'] or [])
                    
                    # カメラIDが指定されている場合、そのカメラIDを含むコマンドラインのプロセスのみを終了
                    if camera_id is not None:
                        # カメラIDに関連するパターンを確認 (例: tmp/1/ や camera_1など)
                        camera_patterns = [
                            f"/tmp/{camera_id}/",
                            f"\\tmp\\{camera_id}\\",
                            f"camera_{camera_id}",
                            f"camera{camera_id}"
                        ]
                        
                        # カメラIDに関連するパターンがコマンドラインに含まれている場合
                        if any(pattern in cmd_line for pattern in camera_patterns):
                            kill_process = True
                    else:
                        # カメラID未指定の場合、カメラに明確に関連しないFFmpegプロセスのみを終了
                        # 注意: カメラIDが未指定の場合は、基本的に何も終了させないようにする
                        kill_process = False
                        
                    if kill_process:
                        # まずTERMシグナルでプロセスを終了
                        logging.info(f"Terminating FFmpeg process with PID {proc.pid}")
                        proc.terminate()
                        
                        # 少し待ってから終了確認
                        try:
                            proc.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            # 3秒待ってもプロセスが終了しない場合、強制終了
                            logging.warning(f"Force killing FFmpeg process with PID {proc.pid}")
                            proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # プロセス情報取得中にエラーが発生した場合はスキップ
                pass
    except Exception as e:
        logging.error(f"Error killing FFmpeg processes: {e}")
        # 例外が発生しても処理を続行する

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
            '-print_format', 'json',
            '-show_streams',
            '-i', rtsp_url
        ]

        result = subprocess.run(ffprobe_command, capture_output=True, text=True, timeout=5)
        stream_info = json.loads(result.stdout)

        # 音声ストリームの確認
        has_audio = any(stream['codec_type'] == 'audio' for stream in stream_info['streams'])
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

def start_ffmpeg_process(command, log_path=None, high_priority=False):
    """
    FFmpegプロセスを起動する

    Args:
        command (list): FFmpegコマンドと引数のリスト
        log_path (str, optional): 出力ログファイルのパス
        high_priority (bool, optional): 高優先度で実行するかどうか

    Returns:
        subprocess.Popen: 起動したプロセスオブジェクト
    """
    try:
        # 環境に応じてstdout, stderrを設定
        stdout = subprocess.PIPE
        stderr = subprocess.PIPE
        
        if log_path:
            log_file = open(log_path, 'w', encoding='utf-8')
            stdout = log_file
            stderr = log_file
            
        # プロセスを起動（Windowsでは特別な考慮が必要）
        if os.name == 'nt':  # Windows環境
            # Windowsでは、shellをTrueにする必要がある場合がある
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.PIPE,
                shell=True,  # Windows環境ではshellをTrueに設定
                creationflags=subprocess.CREATE_NO_WINDOW  # コンソールウィンドウを表示しない
            )
        else:  # Linux/Unix環境
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.PIPE,
                shell=False
            )
        
        # プロセスIDをログに記録
        logging.info(f"Started FFmpeg process with PID: {process.pid}, Command: {' '.join(command)}")
        
        # 高優先度が指定されていればプロセスの優先度を上げる
        if high_priority:
            try:
                # Windowsでの優先度設定
                if os.name == 'nt':
                    p = psutil.Process(process.pid)
                    p.nice(psutil.HIGH_PRIORITY_CLASS)
                # Linux/Unixでの優先度設定
                else:
                    p = psutil.Process(process.pid)
                    p.nice(-5)  # -20から19の範囲で、低い値ほど優先度が高い
            except Exception as e:
                logging.warning(f"Failed to set high priority for FFmpeg process: {e}")
        
        # プロセスが正常に起動したか確認
        time.sleep(0.5)
        if process.poll() is not None:
            logging.error(f"FFmpeg process exited immediately with code {process.returncode}")
            
        return process
        
    except Exception as e:
        logging.error(f"Error starting FFmpeg process: {e}")
        raise

def monitor_ffmpeg_output(process):
    """
    FFmpegプロセスの出力を監視する

    Args:
        process (subprocess.Popen): 監視するFFmpegプロセス
    """
    while True:
        try:
            line = process.stderr.readline()
            if not line:
                break

            decoded_line = line.decode('utf-8', errors='replace').strip()
            if decoded_line:
                logging.info(f"FFmpeg output: {decoded_line}")
                # エラーメッセージを検出
                if "Error" in decoded_line:
                    logging.error(f"FFmpeg error detected: {decoded_line}")

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
        # まず、qコマンドを送信
        if process.stdin:
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                logging.info("Sent 'q' command to FFmpeg process")

            except Exception as e:
                logging.error(f"Error sending q command: {e}")

        # 少し待ってからプロセスの状態を確認
        time.sleep(2)

        # プロセスがまだ実行中なら、psutilを使用して優しく終了
        if process.poll() is None:
            try:
                p = psutil.Process(process.pid)
                p.terminate()
                
                # 終了を待つ
                gone, still_alive = psutil.wait_procs([p], timeout=timeout)
                
                # まだ終了していない場合は強制終了
                if p in still_alive:
                    p.kill()
                    
                logging.info(f"Successfully terminated process")
            except Exception as e:
                logging.error(f"Error terminating process: {e}")
                
                # 最後の手段としてtaskkill
                try:
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                                  check=True, capture_output=True)
                    logging.info(f"Successfully killed process using taskkill")
                except Exception as sub_e:
                    logging.error(f"Failed to kill process with taskkill: {sub_e}")

        # ストリームのクローズ
        for stream in [process.stdin, process.stdout, process.stderr]:
            if stream:
                try:
                    stream.close()
                except Exception as e:
                    logging.error(f"Error closing stream: {e}")

    except Exception as e:
        logging.error(f"Error terminating process: {e}")

def get_ffmpeg_hls_command(rtsp_url, output_path, segment_path):
    """
    HLSストリーミング用のFFmpegコマンドを生成する

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): 出力プレイリストファイルのパス
        segment_path (str): セグメントファイルのパスパターン

    Returns:
        list: FFmpegコマンドと引数のリスト
    """
    # 基本コマンド - シンプルな形式に戻す
    command = [
        'ffmpeg',
        '-rtsp_transport', 'tcp',        # TCPを使用してRTSP接続
        '-i', rtsp_url,                  # 入力ソース
        '-c:v', 'copy',                  # ビデオコーデックはコピー
        '-c:a', 'copy',                  # オーディオコーデックはコピー
        '-f', 'hls',                     # HLS出力フォーマット
        '-hls_time', '2',                # セグメント長（秒）
        '-hls_list_size', '5',           # プレイリスト内のセグメント数
        '-hls_flags', 'delete_segments', # 古いセグメントを削除
        '-hls_segment_filename', segment_path, # セグメントファイルのパターン
        output_path                      # 出力パス
    ]
    
    return command

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
        '-i', rtsp_url,
        '-reset_timestamps', '1',             # タイムスタンプをリセット
        '-reconnect', '1',                    # 接続が切れた場合に再接続を試みる
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '2',          # 最大再接続遅延を2秒に設定
        '-thread_queue_size', '1024',         # 入力バッファサイズを調整
        '-c:v', 'copy',                       # ビデオコーデックをそのままコピー
        '-c:a', 'aac',                        # 音声コーデックをAACに設定
        '-b:a', '96k',                        # 音声ビットレートを下げる
        '-ar', '44100',                       # サンプリングレート
        '-ac', '2',                           # ステレオ音声
        '-async', '1',                        # 音声の同期モード
        '-max_delay', '500000',               # 最大遅延時間（マイクロ秒）
        '-movflags', '+faststart',            # ファストスタートフラグを設定
        '-y',                                 # 既存のファイルを上書き
        output_path
    ]

def terminate_ffmpeg_process(process):
    """
    FFmpegプロセスを安全に終了させる

    Args:
        process (subprocess.Popen): FFmpegプロセスオブジェクト

    Returns:
        bool: 処理成功ならTrue
    """
    if process is None:
        return True
        
    try:
        # プロセスが実行中か確認
        if process.poll() is None:
            # 'q'コマンドを送信して正常終了を試みる
            try:
                process.communicate(input=b'q', timeout=2)
            except subprocess.TimeoutExpired:
                # タイムアウトしたら強制終了
                process.kill()
                
            # プロセスが終了するのを待つ
            process.wait(timeout=3)
            
        return True
            
    except Exception as e:
        logging.error(f"Error terminating FFmpeg process: {e}")
        # エラーが発生した場合は強制終了を試みる
        try:
            process.kill()
        except:
            pass
        return False

# ストリーミングプロセスの健全性を確認
def check_ffmpeg_process_health(process):
    """
    FFmpegプロセスの健全性を確認する

    Args:
        process (subprocess.Popen): FFmpegプロセスオブジェクト

    Returns:
        bool: プロセスが正常に動作しているならTrue
    """
    if process is None:
        return False
        
    try:
        # プロセスの終了コードを確認（Noneなら実行中）
        return_code = process.poll()
        
        if return_code is None:
            # プロセスが実行中
            try:
                # psutilを使用してプロセスの詳細情報を取得
                p = psutil.Process(process.pid)
                cpu_percent = p.cpu_percent(interval=0.1)
                memory_percent = p.memory_percent()
                
                # 異常な値がないか確認
                if cpu_percent > 90:  # CPU使用率が90%を超えている
                    logging.warning(f"High CPU usage detected: {cpu_percent}%")
                    return False
                    
                if memory_percent > 90:  # メモリ使用率が90%を超えている
                    logging.warning(f"High memory usage detected: {memory_percent}%")
                    return False
                    
                return True
                
            except psutil.NoSuchProcess:
                logging.warning(f"Process {process.pid} no longer exists")
                return False
        else:
            # プロセスは終了している
            logging.warning(f"FFmpeg process exited with code {return_code}")
            return False
            
    except Exception as e:
        logging.error(f"Error checking FFmpeg process health: {e}")
        return False
        return False