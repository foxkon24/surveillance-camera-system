"""
監視カメラシステム メインアプリケーション
"""
from flask import Flask, render_template, send_from_directory, request, jsonify, make_response
import os
import logging
import sys
import time

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
            logging.warning(f"File not found: {file_path}, requested by client")
            return "File not found", 404

        # CORS対応を追加（クロスドメインアクセスを許可）
        response = make_response(send_from_directory(
            directory,
            os.path.basename(file_path),
            as_attachment=False,
            mimetype='application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else None))
        
        # キャッシュを無効化
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        # CORS対応
        response.headers['Access-Control-Allow-Origin'] = '*'
        
        return response

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
    # ストリーミングの初期化
    for camera in cameras:
        streaming.get_or_start_streaming(camera)

    # ストリームの初期化を待つ
    time.sleep(1)

    # 各カメラのストリーミング状態を取得
    stream_status = streaming.get_camera_streaming_status()
    
    return render_template('index.html', cameras=cameras, stream_status=stream_status)

@app.route('/system/cam/admin/')
def index_admin():
    """管理ページ"""
    cameras = camera_utils.read_config()
    for camera in cameras:
        streaming.get_or_start_streaming(camera)

    # ストリームの初期化を待つ
    time.sleep(1)

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
    time.sleep(1)

    return render_template('single.html', camera=target_camera)

@app.route('/system/cam/backup/')
def backup_recordings():
    """バックアップ録画一覧を表示"""
    recordings = camera_utils.get_recordings(config.BACKUP_PATH)
    camera_names = camera_utils.read_config_names()

    return render_template('backup_recordings.html', recordings=recordings, camera_names=camera_names)

@app.route('/api/status')
def get_status():
    """システム状態を取得するAPI"""
    try:
        # カメラ情報を取得
        cameras = camera_utils.read_config()
        # ストリーミング状態を取得
        stream_status = streaming.get_camera_streaming_status()
        
        # レスポンス形式の構築
        status_info = {
            "cameras": [],
            "system": {
                "uptime": get_uptime(),
                "ffmpeg_version": get_ffmpeg_version()
            }
        }
        
        # カメラごとの状態情報
        for camera in cameras:
            camera_id = camera['id']
            camera_info = {
                "id": camera_id,
                "name": camera['name'],
                "status": stream_status.get(camera_id, "unknown")
            }
            status_info["cameras"].append(camera_info)
        
        return jsonify(status_info)
        
    except Exception as e:
        logging.error(f"Error getting system status: {e}")
        return jsonify({"error": str(e)}), 500

def get_uptime():
    """システムの稼働時間を取得"""
    try:
        import psutil
        uptime_seconds = time.time() - psutil.boot_time()
        return int(uptime_seconds)
    except:
        return 0

def get_ffmpeg_version():
    """FFmpegのバージョン情報を取得"""
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            # 最初の行だけ返す
            return result.stdout.split('\n')[0]
        return "Unknown"
    except:
        return "Error getting FFmpeg version"

def initialize_app():
    """アプリケーション初期化"""
    try:
        # ロギングの設定
        config.setup_logging()

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

        # ストリーミングシステムの初期化
        streaming.initialize_streaming()

        # 録画システムの初期化
        recording.initialize_recording()

        # FFmpegの確認
        if not config.check_ffmpeg():
            logging.error("FFmpegが見つかりません")
            return False

        # サーバーIPアドレスの表示
        server_ip = config.get_server_ip()
        logging.info(f"Server IP: http://{server_ip}:5000/system/cam/")

        return True

    except Exception as e:
        logging.error(f"初期化エラー: {e}")
        return False

if __name__ == '__main__':
    try:
        if not initialize_app():
            print("アプリケーションの初期化に失敗しました。ログを確認してください。")
            sys.exit(1)

        # 環境情報を出力
        print(f"Current working directory: {os.getcwd()}")
        print(f"Base path: {config.BASE_PATH}")
        print(f"Config file path: {config.CONFIG_PATH}")
        print(f"Config file exists: {os.path.exists(config.CONFIG_PATH)}")
        print(f"Server IP: http://{config.get_server_ip()}:5000/system/cam/")

        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

    except Exception as e:
        logging.error(f"Startup error: {e}")
        print(f"Error: {e}")
        input("Press Enter to exit...")
