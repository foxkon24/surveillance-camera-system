"""
監視カメラシステム メインアプリケーション
"""
from flask import Flask, render_template, send_from_directory, request, jsonify
import os
import logging
import sys
import time
import json
from datetime import datetime

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
    return render_template('recordings.html')

@app.route('/system/cam/tmp/<camera_id>/<filename>')
def serve_tmp_files(camera_id, filename):
    """一時ファイル(HLS)を提供"""
    try:
        # パスを正規化
        file_path = os.path.join(config.TMP_PATH, camera_id, filename).replace('/', '\\')
        directory = os.path.dirname(file_path)

        if not os.path.exists(file_path):
            return "File not found", 404

        return send_from_directory(
            directory,
            os.path.basename(file_path),
            as_attachment=False,
            mimetype = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else None)

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

@app.route('/api/recordings')
def get_recordings_api():
    """録画ファイルリストをJSON形式で返すAPI"""
    recordings = {}
    camera_names = camera_utils.read_config_names()
    
    try:
        # 録画ファイルディレクトリが存在するか確認
        if os.path.exists(config.RECORD_PATH):
            camera_dirs = os.listdir(config.RECORD_PATH)
            
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.RECORD_PATH, camera_id)
                
                if os.path.isdir(camera_path):
                    mp4_files = []
                    try:
                        for file in os.listdir(camera_path):
                            if file.endswith('.mp4'):
                                file_path = os.path.join(camera_path, file)
                                file_size = os.path.getsize(file_path)
                                file_mtime = os.path.getmtime(file_path)
                                
                                # ファイル名から日時を抽出
                                date_str = ""
                                try:
                                    # ファイル名のフォーマット: <カメラID>_YYYYMMDDHHmmSS.mp4
                                    parts = file.split('_')
                                    if len(parts) > 1:
                                        date_part = parts[1].split('.')[0]
                                        if len(date_part) >= 14:
                                            date_str = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {date_part[8:10]}:{date_part[10:12]}:{date_part[12:14]}"
                                except:
                                    pass
                                
                                mp4_files.append({
                                    "filename": file,
                                    "size": file_size,
                                    "date": date_str or datetime.fromtimestamp(file_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                                    "url": f"/system/cam/record/{camera_id}/{file}"
                                })
                    except Exception as dir_e:
                        logging.error(f"Error reading directory {camera_path}: {dir_e}")
                        continue
                    
                    if mp4_files:
                        # 日時の新しい順にソート
                        mp4_files.sort(key=lambda x: x["date"], reverse=True)
                        recordings[camera_id] = {
                            "name": camera_names.get(camera_id, f"カメラ {camera_id}"),
                            "files": mp4_files
                        }
        
        return jsonify({
            "recordings": recordings,
            "camera_names": camera_names
        })
        
    except Exception as e:
        logging.error(f"Error listing recordings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/backup_recordings')
def get_backup_recordings_api():
    """バックアップ録画ファイルリストをJSON形式で返すAPI"""
    recordings = {}
    camera_names = camera_utils.read_config_names()
    
    try:
        # バックアップディレクトリが存在するか確認
        if os.path.exists(config.BACKUP_PATH):
            camera_dirs = os.listdir(config.BACKUP_PATH)
            
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.BACKUP_PATH, camera_id)
                
                if os.path.isdir(camera_path):
                    mp4_files = []
                    try:
                        for file in os.listdir(camera_path):
                            if file.endswith('.mp4'):
                                file_path = os.path.join(camera_path, file)
                                file_size = os.path.getsize(file_path)
                                file_mtime = os.path.getmtime(file_path)
                                
                                # ファイル名から日時を抽出
                                date_str = ""
                                try:
                                    # ファイル名のフォーマット: <カメラID>_YYYYMMDDHHmmSS.mp4
                                    parts = file.split('_')
                                    if len(parts) > 1:
                                        date_part = parts[1].split('.')[0]
                                        if len(date_part) >= 14:
                                            date_str = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {date_part[8:10]}:{date_part[10:12]}:{date_part[12:14]}"
                                except:
                                    pass
                                
                                mp4_files.append({
                                    "filename": file,
                                    "size": file_size,
                                    "date": date_str or datetime.fromtimestamp(file_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                                    "url": f"/system/cam/backup/{camera_id}/{file}"
                                })
                    except Exception as dir_e:
                        logging.error(f"Error reading directory {camera_path}: {dir_e}")
                        continue
                    
                    if mp4_files:
                        # 日時の新しい順にソート
                        mp4_files.sort(key=lambda x: x["date"], reverse=True)
                        recordings[camera_id] = {
                            "name": camera_names.get(camera_id, f"カメラ {camera_id}"),
                            "files": mp4_files
                        }
        
        return jsonify({
            "recordings": recordings,
            "camera_names": camera_names
        })
        
    except Exception as e:
        logging.error(f"Error listing backup recordings: {e}")
        return jsonify({"error": str(e)}), 500

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

    # ストリームの初期化を少し長めに待つ
    time.sleep(2)

    return render_template('index.html', cameras=cameras)

@app.route('/system/cam/admin/')
def index_admin():
    """管理ページ"""
    cameras = camera_utils.read_config()
    for camera in cameras:
        streaming.get_or_start_streaming(camera)

    # ストリームの初期化を少し長めに待つ
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

    # ストリームの初期化を少し長めに待つ
    time.sleep(2)

    # JSONとしてカメラデータを渡す
    camera_json = json.dumps(target_camera)

    return render_template('single.html', camera=target_camera, camera_json=camera_json)

@app.route('/system/cam/backup/')
def backup_recordings():
    """バックアップ録画一覧を表示"""
    return render_template('backup_recordings.html')

@app.route('/system/cam/restart_stream/<camera_id>', methods=['POST'])
def restart_stream(camera_id):
    """特定カメラのストリームを再起動するAPI"""
    try:
        if streaming.restart_streaming(camera_id):
            return jsonify({"status": "success", "message": f"Stream for camera {camera_id} restarted successfully"})
        else:
            return jsonify({"status": "error", "message": f"Failed to restart stream for camera {camera_id}"}), 500
    except Exception as e:
        logging.error(f"Error restarting stream for camera {camera_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/restart_all_streams', methods=['POST'])
def restart_all_streams():
    """全カメラのストリームを再起動するAPI"""
    try:
        cameras = camera_utils.read_config()
        success_count = 0
        failure_count = 0
        
        for camera in cameras:
            if streaming.restart_streaming(camera['id']):
                success_count += 1
            else:
                failure_count += 1
                
        if failure_count == 0:
            return jsonify({"status": "success", "message": f"All {success_count} streams restarted successfully"})
        else:
            return jsonify({"status": "partial", "message": f"{success_count} streams restarted, {failure_count} failed"})
    except Exception as e:
        logging.error(f"Error restarting all streams: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/status')
def get_system_status():
    """システムステータスを返すAPI"""
    try:
        # リソース状況を取得
        import psutil
        cpu_percent = psutil.cpu_percent()
        memory_percent = psutil.virtual_memory().percent
        
        # ディスク空き容量を取得
        disk_info = {}
        for path in [config.RECORD_PATH, config.BACKUP_PATH]:
            try:
                total, used, free = psutil.disk_usage(path)
                disk_info[path] = {
                    "total": total,
                    "used": used,
                    "free": free,
                    "percent": (used / total) * 100
                }
            except:
                disk_info[path] = {"error": "Unable to retrieve disk info"}
        
        # ストリーミング状況を取得
        streaming_status = {
            "active_count": streaming.active_streams_count,
            "processes": len(streaming.streaming_processes),
            "resources": streaming.system_resources
        }
        
        # 録画状況を取得
        recording_status = {
            "active_processes": len(recording.recording_processes),
            "start_times": {k: v.isoformat() if hasattr(v, 'isoformat') else str(v) 
                           for k, v in recording.recording_start_times.items()}
        }
        
        return jsonify({
            "timestamp": datetime.now().isoformat(),
            "system": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent
            },
            "disk": disk_info,
            "streaming": streaming_status,
            "recording": recording_status
        })
        
    except Exception as e:
        logging.error(f"Error getting system status: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/check_disk_space')
def check_disk_space():
    """ディスク空き容量を返すAPI"""
    try:
        # 録画ディレクトリの空き容量をチェック
        record_free = fs_utils.get_free_space(config.RECORD_PATH)
        record_free_gb = record_free / (1024 * 1024 * 1024)
        
        # バックアップディレクトリの空き容量をチェック
        backup_free = fs_utils.get_free_space(config.BACKUP_PATH)
        backup_free_gb = backup_free / (1024 * 1024 * 1024)
        
        return jsonify({
            "record_path": config.RECORD_PATH,
            "record_free_bytes": record_free,
            "record_free_gb": round(record_free_gb, 2),
            "backup_path": config.BACKUP_PATH,
            "backup_free_bytes": backup_free,
            "backup_free_gb": round(backup_free_gb, 2),
            "free_space": f"録画: {round(record_free_gb, 2)} GB, バックアップ: {round(backup_free_gb, 2)} GB"
        })
        
    except Exception as e:
        logging.error(f"Error checking disk space: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/cleanup_old_recordings', methods=['POST'])
def cleanup_old_recordings():
    """古い録画ファイルを削除するAPI"""
    try:
        # 録画ディレクトリ内の古いファイルを削除
        total_deleted = 0
        camera_dirs = os.listdir(config.RECORD_PATH)
        for camera_id in camera_dirs:
            camera_dir = os.path.join(config.RECORD_PATH, camera_id)
            if os.path.isdir(camera_dir):
                deleted = fs_utils.cleanup_directory(
                    camera_dir, 
                    file_pattern='.mp4', 
                    max_age_seconds=config.MAX_RECORDING_HOURS * 3600 * 24,  # 日数を時間に変換
                    max_files=100  # 最大ファイル数
                )
                if deleted:
                    total_deleted += deleted
        
        # バックアップディレクトリ内の古いファイルも削除
        if os.path.exists(config.BACKUP_PATH):
            backup_dirs = os.listdir(config.BACKUP_PATH)
            for camera_id in backup_dirs:
                backup_dir = os.path.join(config.BACKUP_PATH, camera_id)
                if os.path.isdir(backup_dir):
                    deleted = fs_utils.cleanup_directory(
                        backup_dir, 
                        file_pattern='.mp4', 
                        max_age_seconds=config.MAX_RECORDING_HOURS * 3600 * 7,  # バックアップはより長く保持（7倍）
                        max_files=50  # バックアップの最大ファイル数
                    )
                    if deleted:
                        total_deleted += deleted
        
        return jsonify({
            "status": "success",
            "files_deleted": total_deleted,
            "message": f"{total_deleted}件の古い録画ファイルを削除しました"
        })
        
    except Exception as e:
        logging.error(f"Error cleaning up old recordings: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def initialize_app():
    """アプリケーション初期化"""
    try:
        # ロギングの設定
        config.setup_logging()

        # ログの初期メッセージ
        logging.info("============= アプリケーション起動 =============")
        logging.info(f"実行パス: {os.getcwd()}")
        logging.info(f"Pythonバージョン: {sys.version}")
        logging.info(f"OSバージョン: {os.name}")

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

        app.run(host='0.0.0.0', port=5000, debug=False)

    except Exception as e:
        logging.error(f"Startup error: {e}")
        print(f"Error: {e}")
        input("Press Enter to exit...")
