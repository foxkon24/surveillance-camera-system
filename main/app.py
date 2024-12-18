from flask import Flask, render_template, send_from_directory, request, jsonify
import subprocess
import os
import threading
import time
from datetime import datetime, timedelta
import signal
import psutil
import logging

app = Flask(__name__)

# Configuration
CONFIG_PATH = 'D:\\xampp\\htdocs\\system\\cam\\cam_config.txt'
TMP_PATH = 'D:\\xampp\\htdocs\\system\\cam\\tmp'
RECORD_PATH = 'D:\\xampp\\htdocs\\system\\cam\\record'

MAX_RECORDING_HOURS = 1  # 最大録画時間（時間）

recording_processes = {}
recording_threads = {}

recording_start_times = {}  # 録画開始時刻を保持する辞書

# ロギングの設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='recorder.log'
)

@app.route('/system/cam/record/')
def list_recordings():
    recordings = {}
    camera_names = {}  # カメラ名を保存する辞書を追加

    # カメラ設定を読み込む
    with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
        for line in file:
            parts = line.strip().split(',')
            camera_names[parts[0]] = parts[1]  # カメラIDと名前をマッピング

    try:
        camera_dirs = os.listdir(RECORD_PATH)
        for camera_id in camera_dirs:
            camera_path = os.path.join(RECORD_PATH, camera_id)
            if os.path.isdir(camera_path):
                mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                if mp4_files:
                    recordings[camera_id] = mp4_files

        return render_template('recordings.html', recordings=recordings, camera_names=camera_names)  # カメラ名も渡す
    except Exception as e:
        print(f"Error listing recordings: {e}")
        return f"Error: {str(e)}", 500

@app.route('/system/cam/record/<camera_id>/<filename>')
def serve_record_file(camera_id, filename):
    return send_from_directory(os.path.join(RECORD_PATH, camera_id), filename)

@app.route('/system/cam/tmp/<camera_id>/<filename>')
def serve_tmp_files(camera_id, filename):
    try:
        # パスを正規化
        file_path = os.path.join(TMP_PATH, camera_id, filename).replace('/', '\\')
        directory = os.path.dirname(file_path)

        if not os.path.exists(file_path):
            return "File not found", 404

        return send_from_directory(
            directory,
            os.path.basename(file_path),
            as_attachment=False,
            mimetype='application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else None
        )
    except Exception as e:
        logging.error(f"Error serving file {filename} for camera {camera_id}: {e}")
        return str(e), 500

def read_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
        cameras = []
        for line in file:
            parts = line.strip().split(',')
            cameras.append({
                'id': parts[0],
                'name': parts[1],
                'rtsp_url': parts[2]
            })

        return cameras

def ensure_directory_exists(path):
    if not os.path.exists(path):
        os.makedirs(path)
        try:
            os.chmod(path, 0o777)  # ディレクトリに対して全権限を付与
        except OSError as e:
            logging.warning(f"Could not set directory permissions for {path}: {e}")

def get_record_file_path(camera_id):
    """録画ファイルのパスを生成する関数"""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    filename = f"{camera_id}_{timestamp}.mp4"
    full_path = os.path.join(RECORD_PATH, str(camera_id), filename)
    logging.info(f"Generated file path: {full_path}")
    return full_path

def start_recording(camera_id, rtsp_url):
    ensure_directory_exists(os.path.join(RECORD_PATH, camera_id))
    start_new_recording(camera_id, rtsp_url)

def finalize_recording(file_path):
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

def check_recording_duration(camera_id):
    """録画時間をチェックし、必要に応じて録画を再開する"""
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

            # 1時間経過で録画を再開（バッファ時間を確保）
            if duration_seconds >= 3600:  # 1時間 = 3600秒
                camera_config = next((cam for cam in read_config() if cam['id'] == camera_id), None)
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

def start_new_recording(camera_id, rtsp_url):
    try:
        logging.info(f"Starting new recording for camera {camera_id} with URL {rtsp_url}")

        if camera_id in recording_processes:
            logging.info(f"Stopping existing recording for camera {camera_id}")
            stop_recording(camera_id)
            time.sleep(2)

        # ディレクトリの存在確認と作成
        camera_dir = os.path.join(RECORD_PATH, camera_id)
        if not os.path.exists(camera_dir):
            os.makedirs(camera_dir)
            logging.info(f"Created directory: {camera_dir}")

        file_path = get_record_file_path(camera_id)
        logging.info(f"Recording will be saved to: {file_path}")

        # FFmpegコマンドを修正
        ffmpeg_command = [
            'ffmpeg',
            '-rtsp_transport', 'tcp',  # TCPトランスポートを使用
            '-use_wallclock_as_timestamps', '1',  # タイムスタンプの処理を改善
            '-i', rtsp_url,
            '-reset_timestamps', '1',  # タイムスタンプをリセット
            '-reconnect', '1',  # 接続が切れた場合に再接続を試みる
            '-reconnect_at_eof', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '2',  # 最大再接続遅延を2秒に設定
            '-c:v', 'copy',  # ビデオコーデックをそのままコピー
            '-c:a', 'aac',   # 音声コーデックをAACに設定
            '-movflags', '+faststart',  # ファストスタートフラグを設定
            '-y',  # 既存のファイルを上書き
            file_path
        ]

        logging.info(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

        # プロセスを管理者権限で実行
        process = subprocess.Popen(
            ffmpeg_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.HIGH_PRIORITY_CLASS
        )

        # プロセス情報を保存
        recording_processes[camera_id] = {
            'process': process,
            'file_path': file_path
        }
        recording_start_times[camera_id] = datetime.now()

        logging.info(f"Recording process started with PID {process.pid}")

        # エラー出力を監視するスレッド
        def monitor_ffmpeg_output(process):
            while True:
                try:
                    line = process.stderr.readline()
                    if not line:
                        break
                    decoded_line = line.decode('utf-8', errors='replace').strip()
                    if decoded_line:
                        logging.info(f"FFmpeg output: {decoded_line}")
                        # エラーメッセージを検出した場合の処理
                        if "Error opening input" in decoded_line:
                            logging.error(f"RTSP connection error detected: {decoded_line}")
                except Exception as e:
                    logging.error(f"Error in FFmpeg output monitoring: {e}")
                    break

        # エラー監視スレッドの開始
        error_thread = threading.Thread(target=monitor_ffmpeg_output, args=(process,), daemon=True)
        error_thread.start()

        # 録画時間監視スレッドの開始
        if camera_id not in recording_threads or not recording_threads[camera_id].is_alive():
            monitor_thread = threading.Thread(target=check_recording_duration, args=(camera_id,), daemon=True)
            monitor_thread.start()
            recording_threads[camera_id] = monitor_thread

        # プロセスの状態を確認
        time.sleep(2)  # プロセスの起動を待つ
        if process.poll() is not None:
            return_code = process.poll()
            error_output = process.stderr.read().decode('utf-8', errors='replace')
            logging.error(f"FFmpeg process failed to start. Return code: {return_code}")
            logging.error(f"FFmpeg error output: {error_output}")
            raise Exception(f"FFmpeg failed to start: {error_output}")

    except Exception as e:
        logging.error(f"Error starting new recording for camera {camera_id}: {e}")
        logging.exception("Full stack trace:")
        raise

def stop_recording(camera_id):
    logging.info(f"Attempting to stop recording for camera {camera_id}")

    recording_info = recording_processes.pop(camera_id, None)
    if camera_id in recording_start_times:
        del recording_start_times[camera_id]

    if recording_info:
        process = recording_info['process']
        file_path = recording_info['file_path']

        try:
            logging.info(f"Stopping recording process (PID: {process.pid}) for file: {file_path}")

            # プロセス終了処理
            try:
                # まず、qコマンドを送信してみる
                if process.poll() is None:  # プロセスが実行中の場合
                    try:
                        process.stdin.write(b'q\n')
                        process.stdin.flush()
                        logging.info("Sent 'q' command to FFmpeg")
                        time.sleep(2)  # qコマンドの処理を待つ
                    except Exception as e:
                        logging.error(f"Error sending q command: {e}")

                # qコマンドが効かない場合、taskkillを使用
                if process.poll() is None:
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=True, capture_output=True)
                    logging.info(f"Successfully killed process using taskkill")

            except Exception as e:
                logging.error(f"Error terminating process: {e}")
                # 最後の手段としてpsutilを使用
                try:
                    parent = psutil.Process(process.pid)
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
                    logging.info("Killed process using psutil")
                except Exception as sub_e:
                    logging.error(f"Error killing process with psutil: {sub_e}")

            # プロセスの終了を待つ
            try:
                process.wait(timeout=5)
                logging.info("Process has terminated")
            except subprocess.TimeoutExpired:
                logging.warning("Process did not terminate within timeout")

            # ストリームのクローズ
            for stream in [process.stdin, process.stdout, process.stderr]:
                if stream:
                    try:
                        stream.close()
                    except Exception as e:
                        logging.error(f"Error closing stream: {e}")

            # ファイル存在確認
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                logging.info(f"Recording file exists. Size: {file_size} bytes")
                if file_size > 0:
                    finalize_recording(file_path)
                else:
                    logging.warning("Recording file is empty")
            else:
                logging.error(f"Recording file not found: {file_path}")

        except Exception as e:
            logging.error(f"Error in stop_recording: {e}")
            logging.exception("Full stack trace:")
            raise
    else:
        logging.warning(f"No recording process found for camera {camera_id}")

def start_streaming(camera):
    try:
        camera_tmp_dir = os.path.join(TMP_PATH, camera['id'])
        ensure_directory_exists(camera_tmp_dir)

        # m3u8ファイルのパスをバックスラッシュで統一
        hls_path = os.path.join(camera_tmp_dir, f"{camera['id']}.m3u8").replace('/', '\\')
        log_path = os.path.join(camera_tmp_dir, f"{camera['id']}.log").replace('/', '\\')

        # 既存のファイルが存在する場合は削除
        if os.path.exists(hls_path):
            os.remove(hls_path)

        ffmpeg_command = [
            'ffmpeg', '-i', camera['rtsp_url'], 
            '-c:v', 'copy', 
            '-c:a', 'aac',
            '-hls_time', '2', 
            '-hls_list_size', '3', 
            '-hls_flags', 'delete_segments',
            '-hls_allow_cache', '0',  # キャッシュを無効化
            hls_path
        ]

        with open(log_path, 'w') as log_file:
            process = subprocess.Popen(
                ffmpeg_command,
                stdout=log_file,
                stderr=log_file,
                creationflags=subprocess.CREATE_NO_WINDOW  # Windowsでコンソールを表示しない
            )

    except Exception as e:
        logging.error(f"Error in start_streaming for camera {camera['id']}: {e}")
        raise

@app.route('/start_recording', methods=['POST'])
def start_recording_route():
    data = request.json
    camera_id = data['camera_id']
    rtsp_url = data['rtsp_url']
    start_recording(camera_id, rtsp_url)
    return jsonify({"status": "recording started"})

@app.route('/stop_recording', methods=['POST'])
def stop_recording_route():
    data = request.json
    camera_id = data['camera_id']
    stop_recording(camera_id)
    return jsonify({"status": "recording stopped"})

@app.route('/start_all_recordings', methods=['POST'])
def start_all_recordings_route():
    cameras = read_config()
    for camera in cameras:
        start_recording(camera['id'], camera['rtsp_url'])
    return jsonify({"status": "all recordings started"})

@app.route('/stop_all_recordings', methods=['POST'])
def stop_all_recordings_route():
    cameras = read_config()
    for camera in cameras:
        stop_recording(camera['id'])
    return jsonify({"status": "all recordings stopped"})

@app.route('/system/cam/')
def index():
    cameras = read_config()
    for camera in cameras:
        threading.Thread(target=start_streaming, args=(camera,)).start()
    time.sleep(5)
    return render_template('index.html', cameras=cameras)

@app.route('/system/cam/admin/')
def index_admin():
    cameras = read_config()
    for camera in cameras:
        threading.Thread(target=start_streaming, args=(camera,)).start()
    time.sleep(5)
    return render_template('admin.html', cameras=cameras)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

