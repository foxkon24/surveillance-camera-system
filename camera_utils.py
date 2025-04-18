"""
カメラ管理モジュール
カメラ設定の読み込みと管理機能を提供します
"""
import os
import logging
from datetime import datetime
import time
import json
import threading
import config

# カメラ設定の最終読み込み時刻
last_config_read_time = 0
# 設定ファイルの最終更新時刻
last_config_mtime = 0
# 読み込んだカメラ設定
cached_cameras = []
# カメラ名のキャッシュ
cached_camera_names = {}
# 設定読み込みのロック
config_lock = threading.Lock()
# 設定ファイルの自動更新チェック間隔（秒）
CONFIG_CHECK_INTERVAL = 60

def read_config(force_reload=False):
    """
    カメラ設定を読み込む

    Args:
        force_reload (bool): 強制的に再読み込みするか

    Returns:
        list: カメラ情報のリスト。各カメラは辞書形式。
    """
    global last_config_read_time, last_config_mtime, cached_cameras
    
    # ロックを取得して設定の読み込みを排他制御
    with config_lock:
        current_time = time.time()
        
        # 現在の設定ファイルの更新時刻を取得
        try:
            current_mtime = os.path.getmtime(config.CONFIG_PATH) if os.path.exists(config.CONFIG_PATH) else 0
        except Exception as e:
            logging.error(f"設定ファイル更新時刻取得エラー: {e}")
            current_mtime = 0
        
        # キャッシュの有効期限チェック（60秒以内の再読み込みはスキップ）
        if (not force_reload and 
            cached_cameras and 
            current_time - last_config_read_time < CONFIG_CHECK_INTERVAL and
            current_mtime == last_config_mtime):
            return cached_cameras

        try:
            if os.path.exists(config.CONFIG_PATH):
                with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
                    cameras = []
                    line_number = 0

                    for line in file:
                        line_number += 1
                        line = line.strip()
                        
                        # 空行またはコメント行はスキップ
                        if not line or line.startswith('#'):
                            continue
                            
                        parts = line.split(',')

                        # フォーマットを確認 (最低でもID、名前、URLの3つが必要)
                        if len(parts) < 3:
                            logging.warning(f"Invalid format at line {line_number}: {line}")
                            continue

                        # RTSPURLが空の場合はスキップ
                        if not parts[2].strip():
                            logging.warning(f"Empty RTSP URL at line {line_number}: {line}")
                            continue

                        # カメラ情報を追加
                        camera = {
                            'id': parts[0].strip(),
                            'name': parts[1].strip(),
                            'rtsp_url': parts[2].strip()
                        }
                        
                        # オプションのパラメータがあれば追加
                        if len(parts) > 3:
                            # 追加設定（例: 有効/無効、自動録画など）
                            camera['enabled'] = parts[3].strip().lower() in ('1', 'true', 'yes', 'on')
                            
                            # その他の設定
                            if len(parts) > 4:
                                camera['auto_record'] = parts[4].strip().lower() in ('1', 'true', 'yes', 'on')

                        cameras.append(camera)

                # キャッシュを更新
                cached_cameras = cameras
                last_config_read_time = current_time
                last_config_mtime = current_mtime
                
                logging.info(f"カメラ設定を読み込みました: {len(cameras)}台")
                return cameras
            else:
                logging.error(f"設定ファイルが見つかりません: {config.CONFIG_PATH}")
                return []

        except Exception as e:
            logging.error(f"設定ファイル読み込みエラー: {e}")
            return []

def read_config_names():
    """
    カメラID/名前マッピングを読み込む

    Returns:
        dict: カメラIDをキー、カメラ名を値とする辞書
    """
    global cached_camera_names
    
    # キャッシュが存在する場合はそれを使用
    if cached_camera_names:
        return cached_camera_names.copy()
        
    camera_names = {}
    
    # カメラ設定を読み込み
    cameras = read_config()
    
    # カメラ名をマッピング
    for camera in cameras:
        camera_names[camera['id']] = camera['name']
    
    # キャッシュを更新
    cached_camera_names = camera_names.copy()
    
    return camera_names

def write_config(cameras):
    """
    カメラ設定をファイルに書き込む

    Args:
        cameras (list): カメラ情報のリスト

    Returns:
        bool: 操作が成功したかどうか
    """
    global last_config_read_time, last_config_mtime, cached_cameras, cached_camera_names
    
    # ロックを取得して設定の書き込みを排他制御
    with config_lock:
        try:
            # バックアップファイルを作成
            backup_path = f"{config.CONFIG_PATH}.bak"
            if os.path.exists(config.CONFIG_PATH):
                with open(config.CONFIG_PATH, 'r', encoding='utf-8') as src, open(backup_path, 'w', encoding='utf-8') as dst:
                    dst.write(src.read())
                
            # 新しい設定ファイルを書き込み
            with open(config.CONFIG_PATH, 'w', encoding='utf-8') as file:
                # ヘッダーを追加
                file.write("# カメラ設定ファイル\n")
                file.write("# 書式: カメラID,カメラ名,RTSP URL,有効/無効,自動録画\n")
                file.write("# 更新日時: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n\n")
                
                # 各カメラの設定を書き込み
                for camera in cameras:
                    # 必須パラメータ
                    line = f"{camera['id']},{camera['name']},{camera['rtsp_url']}"
                    
                    # オプションパラメータ
                    if 'enabled' in camera:
                        line += f",{'1' if camera['enabled'] else '0'}"
                        
                        if 'auto_record' in camera:
                            line += f",{'1' if camera['auto_record'] else '0'}"
                    
                    file.write(line + "\n")
            
            # キャッシュをクリア
            last_config_read_time = 0
            cached_cameras = []
            cached_camera_names = {}
            
            # 現在の更新時刻を取得
            last_config_mtime = os.path.getmtime(config.CONFIG_PATH)
            
            logging.info(f"カメラ設定を保存しました: {len(cameras)}台")
            return True
        
        except Exception as e:
            logging.error(f"設定ファイル書き込みエラー: {e}")
            return False

def get_recordings(base_path=None):
    """
    指定されたパスから録画ファイルを取得

    Args:
        base_path (str, optional): 録画ファイルを探すディレクトリ。指定なしの場合はconfig.BACKUP_PATH

    Returns:
        dict: カメラIDをキー、録画ファイルのリストを値とする辞書
    """
    if base_path is None:
        base_path = config.BACKUP_PATH

    recordings = {}

    try:
        # ベースフォルダの存在チェック
        if not os.path.exists(base_path):
            logging.warning(f"Recordings path does not exist: {base_path}")
            return {}

        # フォルダ内の全カメラディレクトリをチェック
        camera_dirs = os.listdir(base_path)
        for camera_id in camera_dirs:
            camera_path = os.path.join(base_path, camera_id)

            if os.path.isdir(camera_path):
                # MP4ファイルのリストを取得
                mp4_files = []

                for file in os.listdir(camera_path):
                    if file.endswith('.mp4'):
                        # ファイル情報を取得
                        file_path = os.path.join(camera_path, file)
                        
                        try:
                            file_size = os.path.getsize(file_path)
                            file_mtime = os.path.getmtime(file_path)

                            # ファイル名から日時を解析
                            try:
                                # ファイル名のフォーマット: <カメラID>_YYYYMMDDHHmmSS.mp4
                                date_str = file.split('_')[1].split('.')[0]
                                date = datetime.strptime(date_str, '%Y%m%d%H%M%S')
                            except:
                                date = datetime.fromtimestamp(file_mtime)

                            # 非常に小さいファイルはスキップ（破損の可能性）
                            if file_size > 1024:  # 1KB以上
                                mp4_files.append({
                                    'filename': file,
                                    'size': file_size,
                                    'date': date,
                                    'mtime': file_mtime
                                })
                            else:
                                logging.warning(f"Skipping small file (possibly corrupted): {file_path} ({file_size} bytes)")
                        except Exception as e:
                            logging.error(f"Error getting file info: {file_path}, Error: {e}")

                # 日時でソート（新しい順）
                mp4_files.sort(key=lambda x: x['date'], reverse=True)
                recordings[camera_id] = mp4_files

    except Exception as e:
        logging.error(f"録画ファイル取得エラー: {e}")
        return {}

    return recordings

def get_camera_by_id(camera_id):
    """
    指定されたIDのカメラ情報を取得

    Args:
        camera_id (str): カメラID

    Returns:
        dict or None: カメラ情報。見つからない場合はNone
    """
    cameras = read_config()
    for camera in cameras:
        if camera['id'] == camera_id:
            return camera

    return None

def check_camera_connection(camera_id, rtsp_url=None):
    """
    カメラの接続状態を確認

    Args:
        camera_id (str): 確認するカメラID
        rtsp_url (str, optional): RTSPストリームURL。指定なしの場合は設定から取得

    Returns:
        bool: 接続が成功したかどうか
    """
    try:
        # RTSPURLが指定されていない場合は設定から取得
        if not rtsp_url:
            camera = get_camera_by_id(camera_id)
            if not camera:
                logging.error(f"カメラ情報が見つかりません: {camera_id}")
                return False
                
            rtsp_url = camera['rtsp_url']
            
        # RTSPURLが空の場合はエラー
        if not rtsp_url:
            logging.error(f"RTSPURLが空です: {camera_id}")
            return False
            
        # 接続チェック
        import ffmpeg_utils
        connection_result = ffmpeg_utils.check_rtsp_connection(rtsp_url)
        
        if connection_result:
            logging.info(f"カメラ接続成功: {camera_id}")
        else:
            logging.warning(f"カメラ接続失敗: {camera_id}")
            
        return connection_result
        
    except Exception as e:
        logging.error(f"カメラ接続確認エラー: {camera_id}, Error: {e}")
        return False

def monitor_config_changes():
    """
    設定ファイルの変更を監視し、更新があれば自動で再読み込みする
    """
    global last_config_mtime
    
    while True:
        try:
            # 現在の設定ファイルの更新時刻を取得
            if os.path.exists(config.CONFIG_PATH):
                current_mtime = os.path.getmtime(config.CONFIG_PATH)
                
                # 更新時刻が変わっていれば設定を再読み込み
                if current_mtime != last_config_mtime:
                    logging.info(f"設定ファイルの変更を検出しました: {config.CONFIG_PATH}")
                    read_config(force_reload=True)
            
        except Exception as e:
            logging.error(f"設定ファイル監視エラー: {e}")
            
        # 定期的にチェック
        time.sleep(CONFIG_CHECK_INTERVAL)

def start_config_monitor():
    """
    設定ファイル監視スレッドを開始
    """
    monitor_thread = threading.Thread(target=monitor_config_changes, daemon=True)
    monitor_thread.start()
    logging.info("設定ファイル監視スレッドを開始しました")

def export_camera_status(output_path):
    """
    全カメラの状態をJSONファイルにエクスポート

    Args:
        output_path (str): 出力ファイルパス

    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        # カメラ設定を読み込み
        cameras = read_config()
        
        # ストリーミング状態を取得
        import streaming
        
        # 録画状態を取得
        import recording
        
        # カメラごとのステータスを収集
        status = {}
        for camera in cameras:
            camera_id = camera['id']
            
            # ストリーミング状態
            stream_status = streaming.get_camera_status(camera_id)
            
            # 録画状態
            recording_active = camera_id in recording.recording_processes and recording.recording_processes[camera_id]
            recording_status = recording.recording_status.get(camera_id, 0)
            
            # 接続状態
            try:
                connection_ok = check_camera_connection(camera_id, camera['rtsp_url'])
            except:
                connection_ok = False
            
            # 統合ステータス
            status[camera_id] = {
                'id': camera_id,
                'name': camera['name'],
                'connection': {
                    'ok': connection_ok,
                    'rtsp_url': camera['rtsp_url']
                },
                'streaming': stream_status,
                'recording': {
                    'active': recording_active,
                    'status': recording_status
                },
                'timestamp': time.time()
            }
        
        # JSONファイルに書き込み
        with open(output_path, 'w', encoding='utf-8') as file:
            json.dump({
                'cameras': status,
                'timestamp': time.time(),
                'version': config.VERSION
            }, file, indent=2)
            
        logging.info(f"カメラステータスをエクスポートしました: {output_path}")
        return True
        
    except Exception as e:
        logging.error(f"カメラステータスエクスポートエラー: {e}")
        return False
