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

def check_rtsp_connection(rtsp_url, timeout=5):
    """
    RTSP接続の可否をチェックする関数

    Args:
        rtsp_url (str): チェックするRTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        bool: 接続が成功したかどうか
    """
    try:
        ffprobe_command = [
            'ffprobe',
            '-v', 'quiet',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            '-read_intervals', '%+2'  # 短い時間で読み取り
        ]

        # タイムアウトを設定
        result = subprocess.run(
            ffprobe_command, 
            timeout=timeout,
            capture_output=True,
            text=True
        )

        # 終了コードが0なら接続成功
        if result.returncode == 0:
            logging.info(f"RTSP connection successful: {rtsp_url}")
            return True
        else:
            logging.warning(f"RTSP connection failed: {rtsp_url}, Error: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        logging.error(f"RTSP connection timeout: {rtsp_url}")
        return False
    except Exception as e:
        logging.error(f"Error checking RTSP connection: {rtsp_url}, Error: {e}")
        return False

def kill_ffmpeg_processes(camera_id=None):
    """
    ffmpegプロセスを強制終了する関数

    Args:
        camera_id (str, optional): 特定のカメラID。指定されない場合は全てのffmpegプロセスを終了

    Returns:
        bool: 終了処理が成功したかどうか
    """
    try:
        # 先にpsutilを使って検索（より信頼性が高い）
        killed_pids = []
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # ffmpegプロセスを検索
                if proc.info['name'] == 'ffmpeg.exe' or proc.info['name'] == 'ffmpeg':
                    cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                    
                    # 特定のカメラIDが指定された場合、引数をチェック
                    if not camera_id or (camera_id and camera_id in cmdline):
                        try:
                            # SIGTERM信号でプロセスを終了（Windowsでは機能しない可能性あり）
                            proc.terminate()
                            # 短時間待機
                            gone, still_alive = psutil.wait_procs([proc], timeout=1)
                            
                            # まだ生きていれば強制終了
                            if proc in still_alive:
                                proc.kill()
                                
                            killed_pids.append(proc.pid)
                            logging.info(f'Killed ffmpeg process with PID: {proc.pid}')
                        except psutil.NoSuchProcess:
                            pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        # バックアップとしてtaskkillを使用
        if not killed_pids:
            try:
                # tasklist コマンドを実行してffmpegプロセスを検索
                result = subprocess.check_output('tasklist | findstr ffmpeg', shell=True).decode()

                # 各行からPIDを抽出
                pids = []
                for line in result.split('\n'):
                    if line.strip():
                        # スペースで分割し、2番目の要素（PID）を取得
                        pid = line.split()[1]

                        # 特定のカメラIDが指定された場合、プロセスの引数をチェック
                        if camera_id:
                            try:
                                process = psutil.Process(int(pid))
                                cmdline = ' '.join(process.cmdline())

                                # コマンドラインに特定のカメラIDが含まれているか確認
                                if camera_id in cmdline:
                                    pids.append(pid)

                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue
                        else:
                            pids.append(pid)

                # 見つかった各PIDに対してtaskkillを実行
                for pid in pids:
                    kill_command = f'taskkill /F /PID {pid}'
                    subprocess.run(kill_command, shell=True)
                    killed_pids.append(int(pid))
                    logging.info(f'Killed ffmpeg process with PID: {pid} using taskkill')
            except subprocess.CalledProcessError:
                # ffmpegプロセスが見つからない場合
                pass

        if not killed_pids:
            logging.info('No ffmpeg processes found to kill.')
            return False

        return True

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

        if log_path:
            # ログファイルパスが使用中の場合にバックアップファイル名を使用
            try:
                log_file = open(log_path, 'w')
            except PermissionError:
                base, ext = os.path.splitext(log_path)
                backup_log_path = f"{base}_backup{ext}"
                log_file = open(backup_log_path, 'w')
                logging.warning(f"Primary log file in use, using backup: {backup_log_path}")
                
            process = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=log_file,
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
        return
        
    while True:
        try:
            line = process.stderr.readline()
            if not line:
                break

            decoded_line = line.decode('utf-8', errors='replace').strip()
            if decoded_line:
                # エラーメッセージのみ記録
                if "Error" in decoded_line or "error" in decoded_line:
                    logging.error(f"FFmpeg error detected: {decoded_line}")
                # その他の出力は冗長なので記録しない

        except Exception as e:
            logging.error(f"Error in FFmpeg output monitoring: {e}")
            break

def terminate_process(process, timeout=3):
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
                # stdin書き込みエラーは無視
                pass

        # 短時間待ってからプロセスの状態を確認
        time.sleep(1)

        # プロセスがまだ実行中なら、taskkillを使用
        if process.poll() is None:
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                              check=True, capture_output=True, timeout=2)
                logging.info(f"Successfully killed process using taskkill")

            except Exception as e:
                # 最後の手段としてpsutil
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
                    logging.info("Killed process using psutil")

                except Exception as sub_e:
                    logging.error(f"Failed to kill process with psutil: {sub_e}")

        # プロセスの終了を待つ - 短いタイムアウト
        try:
            process.wait(timeout=timeout)
            logging.info("Process has terminated")

        except subprocess.TimeoutExpired:
            logging.warning("Process did not terminate within timeout")

        # ストリームのクローズ
        for stream in [process.stdin, process.stdout, process.stderr]:
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass

    except Exception as e:
        logging.error(f"Error terminating process: {e}")

def get_ffmpeg_hls_command(rtsp_url, output_path, segment_path, segment_time=2, list_size=3):
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
        '-rtsp_transport', 'tcp',           # TCPトランスポート指定（UDPより安定）
        '-buffer_size', '5242880',          # バッファサイズを5MBに設定
        '-use_wallclock_as_timestamps', '1', # タイムスタンプの処理を改善
        '-i', rtsp_url,
        '-reset_timestamps', '1',           # タイムスタンプのリセット
        '-reconnect', '1',                  # 接続が切れた場合の再接続
        '-reconnect_at_eof', '1',           # ファイル終端での再接続
        '-reconnect_streamed', '1',         # ストリーミング中の再接続
        '-reconnect_delay_max', '1',        # 再接続の最大遅延を1秒に設定
        '-thread_queue_size', '4096',       # スレッドキューサイズ
        '-r', '15',                         # フレームレートを15fpsに制限
        '-copyts',                          # タイムスタンプをコピー
        '-max_delay', '500000',             # 最大遅延を500msに設定
        '-fflags', '+nobuffer+flush_packets', # バッファリングを減らす
        '-flags', 'low_delay',              # 低遅延モード
        '-c:v', 'copy',                     # ビデオコーデックをコピー
        '-c:a', 'aac',                      # 音声コーデックをAAC
        '-b:a', '64k',                      # 音声ビットレートを下げる
        '-ar', '44100',                     # サンプリングレート
        '-ac', '2',                         # ステレオチャンネル
        '-f', 'hls',                        # HLS出力フォーマット
        f'-hls_time', str(segment_time),    # セグメント長
        f'-hls_list_size', str(list_size),  # プレイリストサイズを小さく
        '-hls_flags', 'delete_segments+append_list+program_date_time+discont_start',  # セグメントの効率的な管理
        '-hls_segment_type', 'mpegts',      # セグメントタイプ
        '-hls_allow_cache', '0',            # キャッシュを無効化
        '-hls_segment_filename', segment_path, # セグメントファイルパス
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
        '-i', rtsp_url,
        '-reset_timestamps', '1',             # タイムスタンプをリセット
        '-reconnect', '1',                    # 接続が切れた場合に再接続を試みる
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '2',          # 最大再接続遅延を2秒に設定
        '-thread_queue_size', '1024',         # 入力バッファサイズを増やす
        '-analyzeduration', '2147483647',     # 入力ストリームの分析時間を延長
        '-probesize', '2147483647',           # プローブサイズを増やす
        '-c:v', 'copy',                       # ビデオコーデックをそのままコピー
        '-c:a', 'aac',                        # 音声コーデックをAACに設定
        '-b:a', '128k',                       # 音声ビットレート
        '-ar', '44100',                       # サンプリングレート
        '-ac', '2',                           # ステレオ音声
        '-async', '1',                        # 音声の同期モード
        '-max_delay', '500000',               # 最大遅延時間（マイクロ秒）
        '-movflags', '+faststart',            # ファストスタートフラグを設定
        '-y',                                 # 既存のファイルを上書き
        output_path
    ]
