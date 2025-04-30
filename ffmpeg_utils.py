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

def check_rtsp_connection(rtsp_url, timeout=15):
    """
    RTSP接続の可否をチェックする関数

    Args:
        rtsp_url (str): チェックするRTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        bool: 接続が成功したかどうか
    """
    try:
        # 通常のカメラに対する処理
        ffprobe_command = [
            'ffprobe',
            '-v', 'error',
            '-rtsp_transport', 'tcp',
            '-timeout', '10000000',  # 10秒のソケットタイムアウト（マイクロ秒）
            '-i', rtsp_url,
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            '-read_intervals', '%+3'
        ]

        # 接続試行を3回まで実施
        for attempt in range(3):
            try:
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
                    logging.warning(f"RTSP connection failed (attempt {attempt+1}/3): {rtsp_url}, Error: {result.stderr}")
                    time.sleep(2)  # 再試行前に待機
            except subprocess.TimeoutExpired:
                logging.error(f"RTSP connection timeout (attempt {attempt+1}/3): {rtsp_url}")
                time.sleep(2)  # 再試行前に待機

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
        # psutilを使用して全ffmpegプロセスを検索（より信頼性が高い）
        killed_count = 0
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # ffmpegプロセスを識別
                if 'ffmpeg' in proc.info['name'].lower():
                    cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                    
                    # 特定のカメラIDが指定された場合、コマンドラインをチェック
                    if camera_id and camera_id not in cmdline:
                        continue
                        
                    # プロセスを終了
                    proc.kill()
                    killed_count += 1
                    logging.info(f"Killed ffmpeg process with PID: {proc.info['pid']}")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # バックアップ方法としてtaskkillも実行
        if camera_id:
            os.system(f'taskkill /F /FI "IMAGENAME eq ffmpeg.exe" /FI "COMMANDLINE eq *{camera_id}*"')
        else:
            os.system('taskkill /F /IM ffmpeg.exe')

        if killed_count == 0:
            logging.info('No ffmpeg processes found to kill.')
            return False

        return True

    except Exception as e:
        logging.error(f'An error occurred during killing ffmpeg processes: {str(e)}')
        # エラー発生時でも最終手段としてtaskkillを試行
        try:
            if camera_id:
                os.system(f'taskkill /F /FI "IMAGENAME eq ffmpeg.exe" /FI "COMMANDLINE eq *{camera_id}*"')
            else:
                os.system('taskkill /F /IM ffmpeg.exe')
            return True
        except:
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

        result = subprocess.run(ffprobe_command, capture_output=True, text=True)
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
            with open(log_path, 'w') as log_file:
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
    error_count = 0
    hls_input_detected = False
    recording_started = False
    
    while True:
        try:
            if process.stderr is None:
                logging.warning("FFmpeg process stderr is None, cannot monitor output")
                break

            line = process.stderr.readline()
            if not line:
                break

            decoded_line = line.decode('utf-8', errors='replace').strip()
            if not decoded_line:
                continue

            # HLS入力を使用しているかを検出
            if '/system/cam/tmp/' in decoded_line and '.m3u8' in decoded_line:
                hls_input_detected = True
                logging.info(f"HLSストリームを入力として使用: {decoded_line}")
            
            # 録画開始を検出
            if 'Output #0' in decoded_line and '.mp4' in decoded_line:
                recording_started = True
                logging.info("録画プロセスが出力を開始しました")

            # エラーメッセージを検出
            if "Error" in decoded_line or "error" in decoded_line.lower():
                error_count += 1
                logging.error(f"FFmpeg error detected: {decoded_line}")
                
                # HLS入力を使用しているプロセスの一般的なエラーを特別処理
                if hls_input_detected and any(err in decoded_line for err in ["Operation not permitted", "Connection refused", "timeout"]):
                    logging.warning(f"HLS入力で一般的なエラーが発生しましたが、処理を継続します: {decoded_line}")
                    # エラーカウントをリセット（このエラーは無視）
                    error_count = max(0, error_count - 1)
                
                # 深刻な録画エラーの検出
                if recording_started and "Invalid data" in decoded_line:
                    logging.error("録画データの破損が検出されました")
                    error_count += 2  # 重大度を増加
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
            break

def terminate_process(process, timeout=10):
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
        for i in range(3):  # 3回試行（より確実に終了させる）
            time.sleep(1)
            if process.poll() is not None:
                logging.info("Process terminated gracefully")
                break

        # プロセスがまだ実行中なら、taskkillを使用
        if process.poll() is None:
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], 
                              check=True, capture_output=True)
                logging.info(f"Successfully killed process using taskkill")
            except Exception as e:
                logging.error(f"Error using taskkill: {e}")

                # 最後の手段としてpsutil
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except:
                            pass
                    parent.kill()
                    logging.info("Killed process using psutil")
                except Exception as sub_e:
                    logging.error(f"Failed to kill process with psutil: {sub_e}")

        # プロセスの終了を待つ
        try:
            process.wait(timeout=timeout)
            logging.info("Process has terminated")
        except subprocess.TimeoutExpired:
            logging.warning("Process did not terminate within timeout")
            # 最終手段：Windows固有のkill
            try:
                os.system(f"taskkill /F /PID {process.pid} /T")
                logging.info("Used os.system taskkill as last resort")
            except:
                pass

        # ストリームのクローズ
        for stream in [process.stdin, process.stdout, process.stderr]:
            if stream:
                try:
                    stream.close()
                except Exception as e:
                    pass  # エラーログ抑制

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
        list_size (int): プレイリストサイズ

    Returns:
        list: FFmpegコマンドのリスト
    """
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',                         # TCPトランスポートを明示的に使用
        '-buffer_size', config.FFMPEG_BUFFER_SIZE,        # 設定値を使用
        '-use_wallclock_as_timestamps', '1',
        '-i', rtsp_url,
        '-reset_timestamps', '1',
        '-reconnect', '1',
        '-reconnect_at_eof', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5',                      # 再接続遅延
        '-thread_queue_size', str(config.FFMPEG_THREAD_QUEUE_SIZE),  # 設定値を使用
        '-g', '30',                                       # GOPサイズを減らす
        '-sc_threshold', '0',                             # シーン変更検出しきい値を0に
        '-c:v', 'libx264',                                # ビデオを再エンコード
        '-preset', 'ultrafast',                           # 最も速いエンコードプリセット
        '-tune', 'zerolatency',                           # 低遅延用設定
        '-crf', '28',                                     # 画質設定を軽量化（数値が大きいほど低画質）
        '-b:v', '1500k',                                  # ビットレートを軽量化
        '-maxrate', '2000k',                              # 最大ビットレートを軽量化
        '-bufsize', '3000k',                              # バッファサイズ
        '-pix_fmt', 'yuv420p',                            # 互換性の高いピクセルフォーマット
        '-profile:v', 'baseline',                         # より互換性の高いプロファイル
        '-level', '3.0',                                  # 互換性を優先
        '-c:a', 'aac',
        '-b:a', '96k',                                    # 音声ビットレート
        '-ar', '44100',
        '-ac', '2',
        '-f', 'hls',                                      # HLS形式出力
        '-hls_time', str(segment_time),                   # セグメント長
        '-hls_list_size', str(list_size),                 # プレイリストサイズ
        '-hls_flags', 'delete_segments+append_list+program_date_time',  # HLSフラグ
        '-hls_segment_filename', segment_path,            # セグメントファイルパス
        '-hls_allow_cache', '0',                          # キャッシュ無効化
        '-loglevel', 'warning',                           # ログレベルを制限
        output_path
    ]

def get_ffmpeg_record_command(rtsp_url, output_path, camera_id=None):
    """
    録画用のFFmpegコマンドを生成

    Args:
        rtsp_url (str): RTSPストリームURL
        output_path (str): 録画ファイルの出力パス
        camera_id (str, optional): カメラID

    Returns:
        list: FFmpegコマンドのリスト
    """
    # 全てのカメラでHLSストリームを使用
    logging.info(f"カメラ{camera_id}にHLS録画コマンドを使用します")
    
    # HLSストリームをソースとして使用
    hls_url = f"http://localhost:5000/system/cam/tmp/{camera_id}/{camera_id}.m3u8"
    logging.info(f"カメラ{camera_id}はRTSPではなくHLSソース（{hls_url}）から録画します")
    
    return [
        'ffmpeg',
        '-protocol_whitelist', 'file,http,https,tcp,tls',  # 許可するプロトコル
        '-i', hls_url,                                    # HLSストリームを入力として使用
        '-c:v', 'copy',                                   # ビデオコーデックをそのままコピー
        '-c:a', 'aac',                                    # 音声コーデック
        '-b:a', '128k',                                   # 音声ビットレート
        '-ar', '44100',                                   # サンプリングレート
        '-ac', '2',                                       # ステレオ音声
        '-max_muxing_queue_size', '1024',                 # キューサイズ
        '-fflags', '+genpts+discardcorrupt',              # 破損フレームを破棄し、PTSを生成
        '-avoid_negative_ts', 'make_zero',                # 負のタイムスタンプを回避
        '-movflags', '+faststart+frag_keyframe',          # MP4ファイル最適化
        '-y',                                             # 既存のファイルを上書き
        output_path
    ]