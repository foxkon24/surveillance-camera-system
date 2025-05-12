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

# 接続に問題があるカメラのリストはconfig.pyから読み込む
# UNSTABLE_CAMERAS = ["172.16.210.2"]  # カメラ3のIPアドレス

def check_rtsp_connection(rtsp_url, timeout=10):
    """
    RTSPストリームへの接続をテストする

    Args:
        rtsp_url (str): 検証するRTSPストリームURL
        timeout (int): タイムアウト秒数

    Returns:
        bool: 接続に成功したかどうか
    """
    logging.info(f"RTSP connection check: {rtsp_url}")
    
    try:
        # FFprobコマンドを構築 - シンプルな設定を使用
        ffprobe_cmd = [
            'ffprobe',
            '-v', 'error',
            '-rtsp_transport', 'tcp',  # TCPトランスポートを使用
            '-i', rtsp_url,
            '-show_streams',
            '-select_streams', 'v',  # ビデオストリームのみを選択
            '-of', 'json',
            '-count_frames', 'false',  # フレームカウントを無効化
            '-count_packets', 'false',  # パケットカウントを無効化
            '-read_intervals', '%+3',  # 最初の3秒のみ読み取り
            '-timeout', str(timeout * 1000000)  # タイムアウト（マイクロ秒単位）
        ]
        
        # クリエーションフラグを設定（Windows環境の場合）
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW
        
        # プロセスを実行、タイムアウト付き
        process = subprocess.Popen(
            ffprobe_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags
        )
        
        try:
            # タイムアウト付きで実行
            stdout, stderr = process.communicate(timeout=timeout+5)
            
            if process.returncode == 0:
                logging.info(f"RTSP connection successful: {rtsp_url}")
                return True
            else:
                error_msg = stderr.decode('utf-8', errors='replace')
                logging.warning(f"RTSP connection failed: {rtsp_url}, Error: {error_msg}")
                return False
                
        except subprocess.TimeoutExpired:
            # タイムアウトした場合、プロセスを強制終了
            process.kill()
            process.communicate()  # 残りの出力を読み取る
            logging.warning(f"RTSP connection timed out after {timeout} seconds: {rtsp_url}")
            return False
            
    except Exception as e:
        logging.error(f"Error checking RTSP connection: {e}")
        return False

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
    ffmpegプロセスを強制終了する関数

    Args:
        camera_id (str, optional): 特定のカメラID。指定されない場合は全てのffmpegプロセスを終了
        pid (int, optional): 特定のプロセスID。指定された場合はそのPIDのプロセスのみを終了

    Returns:
        bool: 終了処理が成功したかどうか
    """
    try:
        logging.info(f"kill_ffmpeg_processes: camera_id={camera_id}, pid={pid} の停止を開始")
        killed_count = 0
        ffmpeg_pids = []

        # 1. PIDが直接指定された場合：そのプロセスのみを終了
        if pid:
            try:
                proc = psutil.Process(pid)
                # プロセス名を確認して、ffmpegであることを確認
                if 'ffmpeg' in proc.name().lower():
                    logging.info(f"終了中のFFmpegプロセス PID: {pid}")
                    try:
                        proc.terminate()  # まずは穏やかに終了
                        time.sleep(1)
                        if proc.is_running():
                            proc.kill()  # 強制終了
                            time.sleep(0.5)
                            if proc.is_running():
                                logging.warning(f"プロセス PID: {pid} はまだ実行中、最後の手段を試行")
                                os.kill(pid, signal.SIGKILL)  # 最も強力な終了シグナル
                    except Exception as kill_err:
                        logging.error(f"プロセス終了エラー: {kill_err}")
                        try:
                            # 最後の手段としてOSコマンドを使用
                            os.system(f"taskkill /F /PID {pid} /T")
                        except Exception:
                            pass
                            
                    killed_count += 1
                    logging.info(f"指定されたPID {pid} のFFmpegプロセスを終了しました")
                    return True
                else:
                    logging.warning(f"PID {pid} のプロセスはFFmpegではありません: {proc.name()}")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
                logging.error(f"PID {pid} のプロセス終了失敗: {e}")
                return False

        # 2. psutilを使用してFFmpegプロセスを検索
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # FFmpegプロセスを識別
                if 'ffmpeg' in proc.name().lower():
                    proc_info = {}
                    proc_info['pid'] = proc.pid
                    
                    try:
                        proc_info['create_time'] = proc.create_time()
                        proc_info['cmdline'] = ' '.join(proc.cmdline())
                    except Exception:
                        proc_info['cmdline'] = ''
                        
                    ffmpeg_pids.append(proc_info)
                    
                    # 特定のカメラIDが指定された場合、コマンドラインをチェック
                    if camera_id and camera_id not in proc_info['cmdline']:
                        continue
                    
                    logging.info(f"FFmpegプロセス終了中 PID: {proc.pid}, コマンド: {proc_info['cmdline'][:100]}...")
                    
                    # まず穏やかに終了を試す
                    proc.terminate()
                    killed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
                logging.warning(f"プロセスアクセスエラー: {e}")
                continue

        # 少し待ってから残っているプロセスを強制終了
        if killed_count > 0:
            time.sleep(2)
            for proc_info in ffmpeg_pids:
                try:
                    pid = proc_info['pid']
                    proc = psutil.Process(pid)
                    if proc.is_running() and 'ffmpeg' in proc.name().lower():
                        # 特定のカメラIDが指定され、このプロセスがそのカメラに関連しない場合はスキップ
                        if camera_id and camera_id not in proc_info.get('cmdline', ''):
                            continue
                            
                        # まだ実行中の場合は強制終了
                        logging.warning(f"プロセス {pid} はまだ実行中、強制終了します")
                        proc.kill()
                        time.sleep(0.5)
                        
                        # さらに確実に終了させるため、SIGKILLも試行
                        if proc.is_running():
                            logging.warning(f"プロセス {pid} の強制終了に失敗、SIGKILLを試行")
                            os.kill(pid, signal.SIGKILL)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    # すでに終了しているか、アクセスできない
                    pass

        # 3. バックアップ方法としてtaskkillを実行（Windows対応）
        try:
            if camera_id:
                # 特定のカメラのプロセスだけを対象にする場合は、
                # 全体のtaskkillは実行せず、個別のプロセスを上記のpsutilで終了
                pass
            else:
                # 全てのFFmpegプロセスを対象にする場合
                logging.info("taskkillを使用して全てのFFmpegプロセスを終了します")
                # まず通常終了を試みる
                os.system('taskkill /IM ffmpeg.exe 2>nul')
                # 1秒待ってから強制終了
                time.sleep(1)
                # 強制終了オプションを使用
                os.system('taskkill /F /IM ffmpeg.exe 2>nul')
                # 子プロセスも含めて強制終了
                time.sleep(0.5)
                os.system('taskkill /F /T /IM ffmpeg.exe 2>nul')
        except Exception as taskkill_error:
            logging.error(f"taskkill実行エラー: {taskkill_error}")

        # 4. 最終確認：FFmpegプロセスが終了したか検証
        final_check_count = 0
        remaining_pids = []
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'ffmpeg' in proc.name().lower():
                    # カメラIDが指定された場合、そのカメラに関連するプロセスだけをカウント
                    if camera_id:
                        try:
                            cmdline = ' '.join(proc.cmdline())
                            if camera_id in cmdline:
                                final_check_count += 1
                                remaining_pids.append(proc.pid)
                                logging.warning(f"カメラ {camera_id} のFFmpegプロセスが終了していません: PID={proc.pid}")
                        except Exception:
                            # コマンドライン取得エラーの場合は、カメラIDが不明なので含めない
                            pass
                    else:
                        # 全プロセスをカウント
                        final_check_count += 1
                        remaining_pids.append(proc.pid)
                        logging.warning(f"FFmpegプロセスが終了していません: PID={proc.pid}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        if final_check_count == 0:
            logging.info("全てのFFmpegプロセスが正常に終了しました")
            return True
        else:
            logging.warning(f"終了処理後もまだ {final_check_count} 個のFFmpegプロセスが実行中: PID={remaining_pids}")
            
            # 最後の手段として、残っている各プロセスを個別に強制終了
            for pid in remaining_pids:
                try:
                    logging.warning(f"最後の手段: PID={pid} の強制終了を試みます")
                    os.kill(pid, signal.SIGKILL)
                except Exception as kill_err:
                    logging.error(f"最終的なプロセス終了エラー: {kill_err}")
                    try:
                        os.system(f"taskkill /F /PID {pid} /T")
                    except Exception:
                        pass
                        
            return killed_count > 0

        if killed_count == 0 and final_check_count == 0:
            logging.info('終了すべきFFmpegプロセスは見つかりませんでした。')
            return False

        return True

    except Exception as e:
        logging.error(f'FFmpegプロセス終了中にエラーが発生しました: {str(e)}')
        logging.exception("完全なエラー詳細:")
        
        # エラー発生時でも最終手段としてtaskkillを試行
        try:
            # すべてのffmpegプロセスを強制終了
            os.system('taskkill /F /IM ffmpeg.exe 2>nul')
        except:
            pass
        
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

def start_ffmpeg_process(command, log_path=None, high_priority=False, show_error=False):
    """
    FFmpegプロセスを開始する

    Args:
        command (list): FFmpegコマンドとその引数のリスト
        log_path (str, optional): ログ出力ファイルパス
        high_priority (bool): 優先度を高くするかどうか
        show_error (bool): エラー出力を詳細に表示するかどうか

    Returns:
        subprocess.Popen: 開始されたプロセスオブジェクト
    """
    try:
        # コマンドの概要を記録（長すぎる場合は省略）
        cmd_summary = ' '.join(command)
        if len(cmd_summary) > 500:
            cmd_summary = cmd_summary[:500] + "..."
        logging.info(f"Starting FFmpeg process with command: {cmd_summary}")
        
        # クリエーションフラグを設定（Windows環境の場合）
        creation_flags = 0
        if os.name == 'nt':
            # Windowsでは新しいコンソールウィンドウが表示されないようにする
            creation_flags = subprocess.CREATE_NO_WINDOW
        
        # 標準出力/エラー出力の設定
        if log_path:
            log_dir = os.path.dirname(log_path)
            if not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
                
            try:
                # ログファイルへの書き込みテスト
                with open(log_path, 'w', encoding='utf-8') as test_file:
                    test_file.write(f"FFmpeg log started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    test_file.write(f"Command: {cmd_summary}\n\n")
                logging.info(f"ログファイルへの書き込みテスト成功: {log_path}")
                log_file = open(log_path, 'a', encoding='utf-8')
                stderr = log_file
                stdout = log_file
            except Exception as e:
                logging.error(f"ログファイルの作成エラー: {e}")
                stderr = subprocess.PIPE
                stdout = subprocess.PIPE
        else:
            stderr = subprocess.PIPE
            stdout = subprocess.PIPE
        
        # プロセスの実行前にプロセス数の上限をチェック
        try:
            running_ffmpeg = 0
            for proc in psutil.process_iter(['name']):
                if 'ffmpeg' in proc.info['name'].lower():
                    running_ffmpeg += 1
            if running_ffmpeg > 10:  # 既に10個以上のFFmpegプロセスが実行中
                logging.warning(f"多数（{running_ffmpeg}個）のFFmpegプロセスが実行中です。新しいプロセスを開始する前に一部を終了します。")
                kill_ffmpeg_processes()  # 一部のプロセスを終了
                time.sleep(2)  # プロセスが終了するのを待つ
        except Exception as e:
            logging.error(f"プロセス数チェックエラー: {e}")
        
        # FFmpegプロセス用の環境変数を設定
        env = os.environ.copy()
        
        # Windows固有の設定
        if os.name == 'nt':
            # 実行パスを絶対パスに変換
            ffmpeg_path = 'ffmpeg'
            try:
                import shutil
                ffmpeg_full_path = shutil.which('ffmpeg')
                if ffmpeg_full_path:
                    ffmpeg_path = ffmpeg_full_path
                    command[0] = ffmpeg_full_path
                    logging.info(f"FFmpegの完全パス: {ffmpeg_full_path}")
            except Exception as e:
                logging.error(f"FFmpegパスの解決エラー: {e}")
        
        # プロセスを起動する前に一時停止（他のプロセスとの競合を避けるため）
        time.sleep(1)
            
        # プロセスを起動
        process = subprocess.Popen(
            command,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.PIPE,  # stdinも開いておく
            creationflags=creation_flags,
            close_fds=True,  # 未使用のファイルデスクリプタをクローズ
            shell=False,  # シェル経由で実行しない
            env=env  # 環境変数
        )
        
        # プロセスのPIDを記録
        pid = process.pid
        logging.info(f"FFmpeg process started with PID: {pid}")
        
        # 高優先度が要求された場合（Windows環境）
        if high_priority and os.name == 'nt':
            try:
                import psutil
                p = psutil.Process(pid)
                # 通常優先度よりも少し高くする（競合を避けるため最高優先度は使用しない）
                p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
                logging.info(f"Set high priority for FFmpeg process PID: {pid}")
            except Exception as e:
                logging.warning(f"Failed to set high priority for FFmpeg process: {e}")
        
        # 詳細なエラー表示が必要な場合、FFmpeg出力モニタリングスレッドを起動
        if show_error and process.stderr is not None:
            monitor_thread = threading.Thread(
                target=monitor_ffmpeg_output,
                args=(process,),
                daemon=True,
                name=f"ffmpeg-monitor-{pid}"
            )
            monitor_thread.start()
        
        # プロセス起動後少し待機して状態を確認
        time.sleep(0.5)
        if process.poll() is not None:
            logging.error(f"FFmpegプロセスが即座に終了しました（終了コード: {process.poll()}）")
            
            # ログファイルからエラー情報を取得
            if log_path and os.path.exists(log_path):
                try:
                    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                        log_content = f.read()
                        if log_content:
                            logging.error(f"ログファイルの内容: {log_content}")
                except Exception as log_err:
                    logging.error(f"ログファイル読み取りエラー: {log_err}")
        
        return process
    
    except Exception as e:
        logging.error(f"FFmpegプロセス起動エラー: {e}")
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

def get_hls_streaming_command(input_url, output_path, segment_time=2):
    """
    HLSストリーミング用のFFmpegコマンドを生成する - FFmpeg 7.1.1対応・時間軸同期
    
    Args:
        input_url (str): 入力URLまたはファイルパス
        output_path (str): 出力パス
        segment_time (int): セグメント長（秒）
        
    Returns:
        list: FFmpegコマンドのリスト
    """
    return [
        config.FFMPEG_PATH,
        '-rtsp_transport', 'tcp',
        '-buffer_size', '32768k',
        '-use_wallclock_as_timestamps', '1',  # 時間軸同期
        '-fflags', '+genpts',                 # タイムスタンプ生成
        '-i', input_url,
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-ar', '44100',
        '-hls_time', '2',
        '-hls_list_size', '10',
        '-hls_flags', 'delete_segments+append_list',
        '-hls_segment_type', 'mpegts',
        '-hls_init_time', '2',
        '-hls_allow_cache', '1',
        '-hls_segment_filename', os.path.join(os.path.dirname(output_path), '%d.ts'),
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
            '-protocol_whitelist', 'file,http,https,tcp,tls',  # 許可プロトコル
            '-i', hls_url,                                    # HLSストリーム入力
            '-r', '30',                                       # 30fpsを明示的に指定
            '-c:v', 'copy',                                   # ビデオコーデックをコピー
            '-c:a', 'aac',                                    # 音声コーデック
            '-b:a', '128k',                                   # 音声ビットレート
            '-ar', '44100',                                   # サンプリングレート
            '-ac', '2',                                       # ステレオ音声
            '-max_muxing_queue_size', '2048',                 # キューサイズを増加
            '-fflags', '+genpts+discardcorrupt+igndts',       # タイムスタンプ問題対策を追加
            '-avoid_negative_ts', 'make_zero',                # 負のタイムスタンプを回避
            '-start_at_zero',                                 # ゼロから開始
            '-fps_mode', 'cfr',                               # 一定フレームレート（-vsyncの代わり）
            '-async', '1',                                    # 音声同期
            '-movflags', '+faststart+frag_keyframe',          # MP4ファイル最適化
            '-y',                                             # 既存のファイルを上書き
            output_path
        ]
    
    # RTSPストリームを直接使用
    logging.info(f"カメラ{camera_id if camera_id else 'unknown'}はRTSPストリームから直接録画します: {rtsp_url}")
    
    return [
        'ffmpeg',
        '-rtsp_transport', 'tcp',                         # TCPトランスポートを使用
        '-analyzeduration', '10000000',                   # ストリーム解析時間を増加（10秒）
        '-probesize', '5000000',                          # プローブサイズを増加（5MB）
        '-buffer_size', '30720k',                         # バッファサイズを設定
        '-use_wallclock_as_timestamps', '1',              # 壁時計タイムスタンプを使用
        '-timeout', '10000000',                           # 接続タイムアウト（マイクロ秒）
        '-stimeout', '10000000',                          # ソケットタイムアウト
        '-xerror', '',                                    # 多くのエラーを致命的でないものとして扱う
        '-i', rtsp_url,                                   # RTSPストリーム入力
        '-r', '30',                                       # 30fpsを明示的に指定
        '-c:v', 'copy',                                   # ビデオコーデックをコピー
        '-c:a', 'aac',                                    # 音声コーデック
        '-b:a', '128k',                                   # 音声ビットレート
        '-ar', '44100',                                   # サンプリングレート
        '-ac', '2',                                       # ステレオ音声
        '-max_muxing_queue_size', '2048',                 # キューサイズを増加
        '-fflags', '+genpts+discardcorrupt+igndts',       # タイムスタンプ問題対策を追加
        '-avoid_negative_ts', 'make_zero',                # 負のタイムスタンプを回避
        '-async', '1',                                    # 音声同期
        '-fps_mode', 'cfr',                               # 一定フレームレート（-vsyncの代わり）
        '-start_at_zero',                                 # ゼロから開始
        '-movflags', '+faststart+frag_keyframe',          # MP4ファイル最適化
        '-y',                                             # 既存のファイルを上書き
        output_path
    ]
