"""
監視カメラシステム メインアプリケーション
"""
from flask import Flask, render_template, send_from_directory, request, jsonify
import os
import logging
import sys
import time
import platform
import psutil

# 自作モジュールのインポート
import config
import fs_utils
import camera_utils
import streaming
import recording
import ffmpeg_utils

app = Flask(__name__)

@app.route('/system/cam/record/')
def list_recordings():
    """録画リスト表示"""
    recordings = {}
    camera_names = {}  # カメラ名を保存する辞書を追加

    # カメラ設定を読み込む
    camera_names = camera_utils.read_config_names()

    try:
        camera_dirs = os.listdir(config.RECORD_PATH)

        for camera_id in camera_dirs:
            camera_path = os.path.join(config.RECORD_PATH, camera_id)

            if os.path.isdir(camera_path):
                mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                if mp4_files:
                    recordings[camera_id] = mp4_files

        return render_template('recordings.html', recordings=recordings, camera_names=camera_names)

    except Exception as e:
        logging.error(f"Error listing recordings: {e}")
        return f"Error: {str(e)}", 500

@app.route('/system/cam/tmp/<camera_id>/<filename>')
def serve_tmp_files(camera_id, filename):
    """一時ファイル(HLS)を提供"""
    try:
        # パスを正規化
        file_path = os.path.join(config.TMP_PATH, camera_id, filename).replace('/', '\\')
        directory = os.path.dirname(file_path)

        if not os.path.exists(file_path):
            return "File not found", 404

        mime_type = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else None
        
        # 追加のデバッグログ
        logging.info(f"Serving file: {file_path}, Exists: {os.path.exists(file_path)}, Size: {os.path.getsize(file_path) if os.path.exists(file_path) else 'N/A'}")
        
        return send_from_directory(
            directory,
            os.path.basename(file_path),
            as_attachment=False,
            mimetype=mime_type)

    except Exception as e:
        logging.error(f"Error serving file {filename} for camera {camera_id}: {e}")
        return str(e), 500

@app.route('/system/cam/record/<camera_id>/<filename>')
def serve_record_file(camera_id, filename):
    """録画ファイルを提供"""
    return send_from_directory(os.path.join(config.RECORD_PATH, camera_id), filename)

@app.route('/system/cam/backup/<camera_id>/<filename>')
def serve_backup_file(camera_id, filename):
    """バックアップファイルを提供"""
    return send_from_directory(os.path.join(config.BACKUP_PATH, camera_id), filename)

@app.route('/start_recording', methods=['POST'])
def start_recording_route():
    """特定カメラの録画開始API"""
    data = request.json
    camera_id = data['camera_id']
    rtsp_url = data['rtsp_url']

    try:
        recording.start_recording(camera_id, rtsp_url)
        return jsonify({"status": "recording started"})

    except Exception as e:
        logging.error(f"Failed to start recording: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stop_recording', methods=['POST'])
def stop_recording_route():
    """特定カメラの録画停止API"""
    data = request.json
    camera_id = data['camera_id']

    try:
        recording.stop_recording(camera_id)
        return jsonify({"status": "recording stopped"})

    except Exception as e:
        logging.error(f"Failed to stop recording: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/start_all_recordings', methods=['POST'])
def start_all_recordings_route():
    """全カメラの録画開始API"""
    try:
        success = recording.start_all_recordings()
        return jsonify({"status": "all recordings started" if success else "some recordings failed to start"})

    except Exception as e:
        logging.error(f"Failed to start all recordings: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stop_all_recordings', methods=['POST'])
def stop_all_recordings_route():
    """全カメラの録画停止API"""
    try:
        success = recording.stop_all_recordings()
        return jsonify({"status": "all recordings stopped" if success else "some recordings failed to stop"})

    except Exception as e:
        logging.error(f"Failed to stop all recordings: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/')
def index():
    """メインページ"""
    cameras = camera_utils.read_config()
    for camera in cameras:
        streaming.get_or_start_streaming(camera)

    # ストリームの初期化を待つ
    time.sleep(2)

    return render_template('index.html', cameras=cameras)

@app.route('/system/cam/admin/')
def index_admin():
    """管理ページ"""
    cameras = camera_utils.read_config()
    for camera in cameras:
        streaming.get_or_start_streaming(camera)

    # ストリームの初期化を待つ
    time.sleep(2)

    return render_template('admin.html', cameras=cameras)

@app.route('/system/cam/single')
def index_single():
    """単一カメラ表示ページ"""
    camera_id = request.args.get('id')
    if not camera_id:
        return 'Camera ID not specified', 400

    cameras = camera_utils.read_config()

    target_camera = next((camera for camera in cameras if camera['id'] == camera_id), None)
    if target_camera is None:
        return 'Camera not found', 404

    streaming.get_or_start_streaming(target_camera)

    # ストリームの初期化を待つ
    time.sleep(2)

    return render_template('single.html', camera=target_camera)

@app.route('/system/cam/backup/')
def backup_recordings():
    """バックアップ録画一覧を表示"""
    recordings = camera_utils.get_recordings(config.BACKUP_PATH)
    camera_names = camera_utils.read_config_names()

    return render_template('backup_recordings.html', recordings=recordings, camera_names=camera_names)

@app.route('/system/cam/status')
def system_status():
    """システムステータスを表示"""
    status = {
        "cameras": {},
        "system": {
            "cpu_usage": psutil.cpu_percent(),
            "memory_usage": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage('/').percent,
            "uptime": int(time.time() - psutil.boot_time())
        }
    }
    
    # カメラステータスを取得
    cameras = camera_utils.read_config()
    for camera in cameras:
        camera_id = camera['id']
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        hls_path = os.path.join(camera_tmp_dir, f"{camera_id}.m3u8")
        
        status["cameras"][camera_id] = {
            "name": camera["name"],
            "streaming": camera_id in streaming.streaming_processes and streaming.streaming_processes[camera_id].poll() is None,
            "hls_exists": os.path.exists(hls_path),
            "last_update": round(time.time() - streaming.hls_last_update.get(camera_id, 0)) if camera_id in streaming.hls_last_update else None
        }
    
    return jsonify(status)

@app.route('/system/cam/restart/<camera_id>')
def restart_camera(camera_id):
    """特定カメラのストリーミングを再起動"""
    try:
        success = streaming.restart_streaming(camera_id)
        if success:
            return jsonify({"status": "success", "message": f"Camera {camera_id} restarted successfully"})
        else:
            return jsonify({"status": "error", "message": f"Failed to restart camera {camera_id}"}), 500
    except Exception as e:
        logging.error(f"Error restarting camera {camera_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def initialize_app():
    """アプリケーション初期化"""
    try:
        # ロギングの設定
        config.setup_logging()
        
        # アプリケーション起動情報をログに記録
        logging.info("============= アプリケーション起動 =============")
        logging.info(f"実行パス: {os.getcwd()}")
        logging.info(f"Pythonバージョン: {sys.version}")
        logging.info(f"OSバージョン: {os.name}")
        logging.info(f"Process ID: {os.getpid()}")

        # 基本ディレクトリの確認
        for directory in [config.BASE_PATH, config.TMP_PATH, config.RECORD_PATH, config.BACKUP_PATH]:
            fs_utils.ensure_directory_exists(directory)

        # 設定ファイルの確認
        if not config.check_config_file():
            logging.error("設定ファイルが見つかりません")
            return False

        # カメラ設定の読み込み
        cameras = camera_utils.read_config()
        if not cameras:
            logging.warning("有効なカメラ設定が見つかりません")
        else:
            logging.info(f"{len(cameras)} 台のカメラ設定を読み込みました")

        # ストリーミングシステムの初期化
        streaming.initialize_streaming()

        # 録画システムの初期化
        recording.initialize_recording()

        # FFmpegの確認
        if not config.check_ffmpeg():
            logging.error("FFmpegが見つかりません")
            return False

        # IPアドレスの取得
        try:
            for interface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == 2:  # IPv4
                        ip = addr.address
                        if not ip.startswith("127."):
                            logging.info(f"Server IP: http://{ip}:5000/system/cam/")
                            print(f"Server IP: http://{ip}:5000/system/cam/")
        except Exception as e:
            logging.error(f"Error getting IP address: {e}")

        return True

    except Exception as e:
        logging.error(f"初期化エラー: {e}")
        return False

if __name__ == '__main__':
    try:
        # 管理者権限で実行されているか確認（Windowsの場合）
        if os.name == 'nt':
            try:
                import ctypes
                is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
                print(f"Running with {'administrative' if is_admin else 'standard user'} privileges.")
                logging.info(f"Running with {'administrative' if is_admin else 'standard user'} privileges.")
            except:
                pass

        if not initialize_app():
            print("アプリケーションの初期化に失敗しました。ログを確認してください。")
            sys.exit(1)

        # 環境情報を出力
        print(f"Current working directory: {os.getcwd()}")
        print(f"Base path: {config.BASE_PATH}")
        print(f"Config file path: {config.CONFIG_PATH}")
        print(f"Config file exists: {os.path.exists(config.CONFIG_PATH)}")

        app.run(host='0.0.0.0', port=5000, debug=False)

    except Exception as e:
        logging.error(f"Startup error: {e}")
        print(f"Error: {e}")
        input("Press Enter to exit...")
