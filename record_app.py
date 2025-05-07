"""
録画専用アプリケーション
カメラ録画管理・録画ファイル配信のみを担当
"""
from flask import Flask, render_template, send_from_directory, request, jsonify
import os
import logging
import sys
import time

import config
import fs_utils
import camera_utils
import recording
import ffmpeg_utils

app = Flask(__name__)

@app.route('/system/cam/record/')
def list_recordings():
    """録画リスト表示"""
    recordings = {}
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

@app.route('/system/cam/record/<camera_id>/<filename>')
def serve_record_file(camera_id, filename):
    """録画ファイルを提供"""
    return send_from_directory(os.path.join(config.RECORD_PATH, camera_id), filename)

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

def initialize_record_app():
    """録画アプリ初期化"""
    try:
        config.setup_logging()
        logging.info("============= 録画アプリ起動 =============")
        for directory in [config.BASE_PATH, config.RECORD_PATH, config.BACKUP_PATH]:
            fs_utils.ensure_directory_exists(directory)
        if not config.check_config_file():
            logging.error("設定ファイルが見つかりません")
            return False
        cameras = camera_utils.read_config()
        if not cameras:
            logging.warning("有効なカメラ設定が見つかりません")
        recording.initialize_recording()
        if not config.check_ffmpeg():
            logging.error("FFmpegが見つかりません")
            return False
        return True
    except Exception as e:
        logging.error(f"初期化エラー: {e}")
        return False

if __name__ == '__main__':
    try:
        if not initialize_record_app():
            print("録画アプリの初期化に失敗しました。ログを確認してください。")
            sys.exit(1)
        print(f"Current working directory: {os.getcwd()}")
        print(f"Base path: {config.BASE_PATH}")
        print(f"Config file path: {config.CONFIG_PATH}")
        print(f"Config file exists: {os.path.exists(config.CONFIG_PATH)}")
        app.run(host='0.0.0.0', port=5001, debug=False)
    except Exception as e:
        logging.error(f"Startup error: {e}")
        print(f"Error: {e}")
        input("Press Enter to exit...") 