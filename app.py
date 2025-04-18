"""
監視カメラシステム メインアプリケーション
"""
from flask import Flask, render_template, send_from_directory, request, jsonify
import os
import logging
import sys
import time
import threading

# 自作モジュールのインポート
import config
import fs_utils
import camera_utils
import streaming
import recording
import ffmpeg_utils

app = Flask(__name__)

# システム状態のグローバル変数
system_status = {
    'last_check': time.time(),
    'disk_space': {},
    'cameras': {}
}

# ディスク容量確認の頻度 (秒)
DISK_CHECK_INTERVAL = 300  # 5分ごと

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
                    # 新しい順に並べ替え
                    mp4_files.sort(key=lambda f: os.path.getmtime(os.path.join(camera_path, f)), reverse=True)
                    recordings[camera_id] = mp4_files

        return render_template('recordings.html', recordings=recordings, camera_names=camera_names)

    except Exception as e:
        logging.error(f"Error listing recordings: {e}")
        return f"Error: {str(e)}", 500

@app.route('/system/cam/tmp/<camera_id>/<path:filename>')
def serve_tmp_files(camera_id, filename):
    """一時ファイル(HLS)を提供"""
    try:
        # 正しいパス形式を使用（置換なし）
        directory = os.path.join(config.TMP_PATH, camera_id)
        
        # ファイルが存在するか確認
        file_path = os.path.join(directory, filename)
        if not os.path.exists(file_path):
            logging.warning(f"HLS file not found: {file_path}")
            return "File not found", 404

        # MIME type の設定
        mimetype = None
        if filename.endswith('.m3u8'):
            mimetype = 'application/vnd.apple.mpegurl'
        elif filename.endswith('.ts'):
            mimetype = 'video/mp2t'

        # キャッシュを無効化するヘッダーを設定
        headers = {
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Pragma': 'no-cache',
            'Expires': '0'
        }

        return send_from_directory(
            directory,
            filename,
            as_attachment=False,
            mimetype=mimetype,
            add_etags=False,
            max_age=0
        ), 200, headers

    except Exception as e:
        logging.error(f"Error serving file {filename} for camera {camera_id}: {e}")
        return str(e), 500

@app.route('/system/cam/record/<camera_id>/<filename>')
def serve_record_file(camera_id, filename):
    """録画ファイルを提供"""
    try:
        # ファイルパスを構築
        file_path = os.path.join(config.RECORD_PATH, camera_id, filename)
        
        # ファイルの存在確認
        if not os.path.exists(file_path):
            logging.warning(f"Recording file not found: {file_path}")
            return "File not found", 404
            
        # ファイルサイズの確認（破損チェック）
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            logging.warning(f"Zero-size recording file: {file_path}")
            return "File is empty or corrupted", 500
        
        # ファイル拡張子確認
        if not filename.lower().endswith('.mp4'):
            logging.warning(f"Invalid file type requested: {filename}")
            return "Invalid file type", 400
            
        return send_from_directory(os.path.join(config.RECORD_PATH, camera_id), filename)
        
    except Exception as e:
        logging.error(f"Error serving recording file {filename} for camera {camera_id}: {e}")
        return str(e), 500

@app.route('/system/cam/backup/<camera_id>/<filename>')
def serve_backup_file(camera_id, filename):
    """バックアップファイルを提供"""
    try:
        # ファイルパスを構築
        file_path = os.path.join(config.BACKUP_PATH, camera_id, filename)
        
        # ファイルの存在確認
        if not os.path.exists(file_path):
            logging.warning(f"Backup file not found: {file_path}")
            return "File not found", 404
            
        # ファイルサイズの確認（破損チェック）
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            logging.warning(f"Zero-size backup file: {file_path}")
            return "File is empty or corrupted", 500
        
        return send_from_directory(os.path.join(config.BACKUP_PATH, camera_id), filename)
        
    except Exception as e:
        logging.error(f"Error serving backup file {filename} for camera {camera_id}: {e}")
        return str(e), 500

@app.route('/system/cam/restart_stream/<camera_id>', methods=['POST'])
def restart_stream_endpoint(camera_id):
    """特定カメラのストリームを再起動するAPI"""
    try:
        # 特定カメラのストリームを再起動
        success = streaming.restart_streaming(camera_id)
        
        if success:
            logging.info(f"Successfully restarted stream for camera {camera_id}")
            return jsonify({"status": "success", "message": f"Stream for camera {camera_id} restarted"}), 200
        else:
            logging.error(f"Failed to restart stream for camera {camera_id}")
            return jsonify({"status": "error", "message": f"Failed to restart stream for camera {camera_id}"}), 500
            
    except Exception as e:
        logging.error(f"Error restarting stream for camera {camera_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/restart_all_streams', methods=['POST'])
def restart_all_streams_endpoint():
    """すべてのカメラのストリームを再起動するAPI"""
    try:
        # まず、すべてのストリーミングを停止
        streaming.stop_all_streaming()
        
        # 少し待つ
        time.sleep(2)
        
        # すべてのカメラのストリームを再開
        cameras = camera_utils.read_config()
        success = True
        failed_cameras = []
        
        for camera in cameras:
            try:
                result = streaming.get_or_start_streaming(camera)
                if not result:
                    success = False
                    failed_cameras.append(camera['id'])
                    logging.error(f"Failed to restart stream for camera {camera['id']}")
            except Exception as e:
                success = False
                failed_cameras.append(camera['id'])
                logging.error(f"Error restarting stream for camera {camera['id']}: {e}")
        
        if success:
            return jsonify({"status": "success", "message": "All streams restarted"}), 200
        else:
            return jsonify({
                "status": "partial", 
                "message": "Some streams failed to restart", 
                "failed_cameras": failed_cameras
            }), 207
            
    except Exception as e:
        logging.error(f"Error restarting all streams: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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

@app.route('/system/cam/stop_all_streaming', methods=['POST'])
def stop_all_streaming_route():
    """すべてのカメラのストリーミングを停止するAPI"""
    try:
        success = streaming.stop_all_streaming()
        return jsonify({"status": "all streaming stopped" if success else "some streaming processes failed to stop"})
    
    except Exception as e:
        logging.error(f"Failed to stop all streaming: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/status', methods=['GET'])
def status_route():
    """システム状態を取得するAPI"""
    try:
        # ディスク容量情報の更新（一定間隔ごと）
        current_time = time.time()
        if current_time - system_status['last_check'] > DISK_CHECK_INTERVAL:
            update_system_status()
        
        return jsonify(system_status)
        
    except Exception as e:
        logging.error(f"Failed to get system status: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/cleanup_old_recordings', methods=['POST'])
def cleanup_old_recordings_route():
    """古い録画ファイルを削除するAPI"""
    try:
        deleted_count = cleanup_old_recordings()
        return jsonify({
            "status": "success", 
            "message": f"Deleted {deleted_count} old recording files",
            "deleted_count": deleted_count
        })
    except Exception as e:
        logging.error(f"Failed to cleanup old recordings: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/system/cam/')
def index():
    """メインページ"""
    try:
        # ディスク容量チェック
        check_disk_space()
        
        cameras = camera_utils.read_config()
        
        # 必要なディレクトリを確保
        for camera in cameras:
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera['id'])
            fs_utils.ensure_directory_exists(camera_tmp_dir)
        
        # カメラがなければユーザーに通知
        if not cameras:
            logging.warning("No cameras found in configuration")
            return render_template('index.html', cameras=[], error="カメラが設定されていません。設定ファイルを確認してください。")

        # 各カメラの一時ディレクトリが存在することを確認
        for camera in cameras:
            camera_id = camera['id']
            camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
            if not os.path.exists(camera_tmp_dir):
                try:
                    os.makedirs(camera_tmp_dir, exist_ok=True)
                    logging.info(f"Created directory for camera {camera_id}: {camera_tmp_dir}")
                except Exception as e:
                    logging.error(f"Failed to create directory for camera {camera_id}: {e}")
        
        # ここでカメラの初期化を行わない（ページ表示後にJSで行う）
        # カメラの初期化処理は、Webページのロード後にJavaScriptから行うようにする
        
        return render_template('index.html', cameras=cameras)
        
    except Exception as e:
        logging.error(f"Error loading index page: {e}")
        return render_template('index.html', cameras=[], error=f"エラーが発生しました: {str(e)}")

@app.route('/system/cam/admin/')
def index_admin():
    """管理ページ"""
    try:
        cameras = camera_utils.read_config()
        
        # カメラ設定が存在するか確認
        if not cameras:
            logging.warning("No cameras found in configuration")
            return render_template('admin.html', cameras=[], error="カメラが設定されていません。設定ファイルを確認してください。")
            
        # 初期化の待機時間は削除
        return render_template('admin.html', cameras=cameras)
        
    except Exception as e:
        logging.error(f"Error loading admin page: {e}")
        return render_template('admin.html', cameras=[], error=f"エラーが発生しました: {str(e)}")

@app.route('/system/cam/single')
def index_single():
    """単一カメラ表示ページ"""
    try:
        camera_id = request.args.get('id')
        if not camera_id:
            return 'Camera ID not specified', 400

        cameras = camera_utils.read_config()
        if not cameras:
            return 'No cameras configured', 404

        target_camera = next((camera for camera in cameras if camera['id'] == camera_id), None)
        if target_camera is None:
            return 'Camera not found', 404

        # カメラの一時ディレクトリ確認
        camera_tmp_dir = os.path.join(config.TMP_PATH, camera_id)
        fs_utils.ensure_directory_exists(camera_tmp_dir)

        # カメラの初期化は行わない
        return render_template('single.html', camera=target_camera)
        
    except Exception as e:
        logging.error(f"Error loading single camera page: {e}")
        return f"Error: {str(e)}", 500

@app.route('/system/cam/backup/')
def backup_recordings():
    """バックアップ録画一覧を表示"""
    try:
        recordings = camera_utils.get_recordings(config.BACKUP_PATH)
        camera_names = camera_utils.read_config_names()

        return render_template('backup_recordings.html', recordings=recordings, camera_names=camera_names)
        
    except Exception as e:
        logging.error(f"Error loading backup recordings page: {e}")
        return f"Error: {str(e)}", 500

def check_disk_space():
    """ディスク容量を確認し、必要に応じて警告を表示"""
    try:
        base_paths = [config.RECORD_PATH, config.BACKUP_PATH, config.TMP_PATH]
        for path in base_paths:
            if os.path.exists(path):
                # ディスクの空き容量を取得 (GB単位)
                free_space_gb = fs_utils.get_free_space(path) / (1024 ** 3)
                
                if free_space_gb < config.MIN_DISK_SPACE_GB:
                    logging.warning(f"Low disk space for {path}: {free_space_gb:.2f} GB")
                    
                    # system_statusに記録
                    if 'disk_space' not in system_status:
                        system_status['disk_space'] = {}
                    
                    system_status['disk_space'][path] = {
                        'path': path,
                        'free_space_gb': round(free_space_gb, 2),
                        'status': 'warning' if free_space_gb < config.MIN_DISK_SPACE_GB else 'ok',
                        'timestamp': time.time()
                    }
                    
                    # 空き容量が極端に少ない場合は自動クリーンアップ
                    if free_space_gb < config.MIN_DISK_SPACE_GB / 2:
                        logging.warning(f"Critical low disk space for {path}: {free_space_gb:.2f} GB. Performing auto cleanup.")
                        # 古い録画ファイルを削除
                        cleanup_old_recordings()
                else:
                    # 容量が十分な場合も記録
                    if 'disk_space' not in system_status:
                        system_status['disk_space'] = {}
                    
                    system_status['disk_space'][path] = {
                        'path': path,
                        'free_space_gb': round(free_space_gb, 2),
                        'status': 'ok',
                        'timestamp': time.time()
                    }
    
    except Exception as e:
        logging.error(f"Error checking disk space: {e}")

def cleanup_old_recordings():
    """古い録画ファイルを自動クリーンアップする"""
    total_deleted = 0
    try:
        # 録画ディレクトリ内の全カメラのフォルダを確認
        if os.path.exists(config.RECORD_PATH):
            camera_dirs = os.listdir(config.RECORD_PATH)
            
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.RECORD_PATH, camera_id)
                
                if os.path.isdir(camera_path):
                    files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                    
                    if files:
                        # 作成日時でソート（最も古いファイルが先頭）
                        files.sort(key=lambda f: os.path.getctime(os.path.join(camera_path, f)))
                        
                        # 最も古いファイルから半分を削除
                        files_to_delete = files[:len(files) // 2]
                        
                        for file in files_to_delete:
                            file_path = os.path.join(camera_path, file)
                            try:
                                os.remove(file_path)
                                logging.info(f"Deleted old recording file: {file_path}")
                                total_deleted += 1
                            except Exception as e:
                                logging.error(f"Failed to delete file {file_path}: {e}")
        
        # 同様にバックアップディレクトリもクリーンアップ
        if os.path.exists(config.BACKUP_PATH):
            camera_dirs = os.listdir(config.BACKUP_PATH)
            
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.BACKUP_PATH, camera_id)
                
                if os.path.isdir(camera_path):
                    files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                    
                    if files:
                        # 作成日時でソート（最も古いファイルが先頭）
                        files.sort(key=lambda f: os.path.getctime(os.path.join(camera_path, f)))
                        
                        # 最も古いファイルから半分を削除
                        files_to_delete = files[:len(files) // 2]
                        
                        for file in files_to_delete:
                            file_path = os.path.join(camera_path, file)
                            try:
                                os.remove(file_path)
                                logging.info(f"Deleted old backup file: {file_path}")
                                total_deleted += 1
                            except Exception as e:
                                logging.error(f"Failed to delete backup file {file_path}: {e}")
        
        return total_deleted
        
    except Exception as e:
        logging.error(f"Error cleaning up old recordings: {e}")
        return total_deleted

def update_system_status():
    """システム状態情報を更新"""
    try:
        # ディスク容量情報の更新
        system_status['last_check'] = time.time()
        
        # ディスク容量チェック
        base_paths = [config.RECORD_PATH, config.BACKUP_PATH, config.TMP_PATH]
        for path in base_paths:
            if os.path.exists(path):
                free_space_gb = fs_utils.get_free_space(path) / (1024 ** 3)
                
                if 'disk_space' not in system_status:
                    system_status['disk_space'] = {}
                
                system_status['disk_space'][path] = {
                    'path': path,
                    'free_space_gb': round(free_space_gb, 2),
                    'status': 'warning' if free_space_gb < config.MIN_DISK_SPACE_GB else 'ok',
                    'timestamp': time.time()
                }
        
        # カメラ状態の確認
        cameras = camera_utils.read_config()
        for camera in cameras:
            camera_id = camera['id']
            
            if 'cameras' not in system_status:
                system_status['cameras'] = {}
            
            # ストリーミング状態
            stream_status = streaming.get_camera_status(camera_id)
            
            # 録画状態
            recording_active = camera_id in recording.recording_processes and recording.recording_processes[camera_id]
            
            system_status['cameras'][camera_id] = {
                'id': camera_id,
                'name': camera['name'],
                'streaming': stream_status,
                'recording': {
                    'active': recording_active,
                    'status_code': recording.recording_status.get(camera_id, 0),
                    'start_time': recording.recording_start_times.get(camera_id, None)
                },
                'timestamp': time.time()
            }
        
    except Exception as e:
        logging.error(f"Error updating system status: {e}")

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
            
        # システム状態監視スレッドを開始
        status_thread = threading.Thread(target=status_monitor_thread, daemon=True)
        status_thread.start()

        return True

    except Exception as e:
        logging.error(f"初期化エラー: {e}")
        return False

def status_monitor_thread():
    """システム状態を定期的に監視するスレッド"""
    while True:
        try:
            # システム状態を更新
            update_system_status()
            
            # ディスク容量が少ない場合は警告
            check_disk_space()
            
            # 録画ファイルの整合性を確認
            check_recording_integrity()
            
        except Exception as e:
            logging.error(f"Error in status monitor thread: {e}")
            
        # 5分ごとに実行
        time.sleep(300)

def check_recording_integrity():
    """録画ファイルの整合性をチェック"""
    try:
        if os.path.exists(config.RECORD_PATH):
            camera_dirs = os.listdir(config.RECORD_PATH)
            
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.RECORD_PATH, camera_id)
                
                if os.path.isdir(camera_path):
                    files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                    
                    for filename in files:
                        file_path = os.path.join(camera_path, filename)
                        
                        # ファイルサイズが0または非常に小さい場合はファイルが破損している可能性
                        file_size = os.path.getsize(file_path)
                        if file_size < 10240:  # 10KB未満
                            logging.warning(f"Possibly corrupted recording file detected: {file_path} (size: {file_size} bytes)")
                            
                            # 破損ファイルを削除
                            try:
                                os.remove(file_path)
                                logging.info(f"Deleted corrupted recording file: {file_path}")
                            except Exception as e:
                                logging.error(f"Failed to delete corrupted file {file_path}: {e}")
                                
        # バックアップフォルダも同様にチェック
        if os.path.exists(config.BACKUP_PATH):
            camera_dirs = os.listdir(config.BACKUP_PATH)
            
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.BACKUP_PATH, camera_id)
                
                if os.path.isdir(camera_path):
                    files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                    
                    for filename in files:
                        file_path = os.path.join(camera_path, filename)
                        
                        # ファイルサイズが0または非常に小さい場合はファイルが破損している可能性
                        file_size = os.path.getsize(file_path)
                        if file_size < 10240:  # 10KB未満
                            logging.warning(f"Possibly corrupted backup file detected: {file_path} (size: {file_size} bytes)")
                            
                            # 破損ファイルを削除
                            try:
                                os.remove(file_path)
                                logging.info(f"Deleted corrupted backup file: {file_path}")
                            except Exception as e:
                                logging.error(f"Failed to delete corrupted backup file {file_path}: {e}")
                        
    except Exception as e:
        logging.error(f"Error checking recording integrity: {e}")

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
