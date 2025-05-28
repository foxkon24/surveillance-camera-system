"""
カメラ管理モジュール
カメラ設定の読み込みと管理機能を提供します
"""
import os
import logging
from datetime import datetime
import config
import time
import requests

# グローバル変数としてカメラ設定をキャッシュ
_camera_cache = None
_camera_names_cache = None
# カメラの再起動試行回数を記録
_camera_restart_attempts = {}
# カメラ再起動の連続試行最大回数
MAX_CAMERA_RESTART_ATTEMPTS = 3
# カメラ再起動の間隔（秒）
CAMERA_RESTART_INTERVAL = 60

def read_config():
    """
    カメラ設定を読み込む
    キャッシュがある場合はキャッシュから返す

    Returns:
        list: カメラ情報のリスト。各カメラは辞書形式。
    """
    global _camera_cache
    
    # キャッシュがあればそれを返す
    if _camera_cache is not None:
        return _camera_cache.copy()
        
    try:
        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            cameras = []

            for line in file:
                parts = line.strip().split(',')

                # RTSPURLが空の場合はスキップ
                if len(parts) >= 3 and parts[2].strip():
                    enabled = 1
                    if len(parts) >= 4 and parts[3].strip() != '':
                        try:
                            enabled = int(parts[3])
                        except Exception:
                            enabled = 1
                    cameras.append({
                        'id': parts[0],
                        'name': parts[1],
                        'rtsp_url': parts[2],
                        'enabled': enabled
                    })
            
            # キャッシュを更新
            _camera_cache = cameras
            return cameras

    except Exception as e:
        logging.error(f"設定ファイル読み込みエラー: {e}")
        return []

def reload_config():
    """
    設定ファイルを強制的に再読み込みする
    キャッシュをクリアして最新のcam_config.txtを必ず読み込む
    Returns:
        list: 最新のカメラ情報リスト
    """
    global _camera_cache, _camera_names_cache
    
    # キャッシュをクリア
    _camera_cache = None
    _camera_names_cache = None
    
    # 設定を再読み込み
    return read_config()

def read_config_names():
    """
    カメラID/名前マッピングを読み込む
    キャッシュがある場合はキャッシュから返す

    Returns:
        dict: カメラIDをキー、カメラ名を値とする辞書
    """
    global _camera_names_cache
    
    # キャッシュがあればそれを返す
    if _camera_names_cache is not None:
        return _camera_names_cache.copy()
        
    camera_names = {}
    try:
        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            for line in file:
                parts = line.strip().split(',')
                if len(parts) >= 2:
                    camera_names[parts[0]] = parts[1]  # カメラIDと名前をマッピング
        
        # キャッシュを更新
        _camera_names_cache = camera_names
        return camera_names

    except Exception as e:
        logging.error(f"設定ファイル読み込みエラー: {e}")
        return {}

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

                try:
                    for file in os.listdir(camera_path):
                        if file.endswith('.mp4'):
                            try:
                                # ファイル情報を取得
                                file_path = os.path.join(camera_path, file)
                                file_size = os.path.getsize(file_path)
                                file_mtime = os.path.getmtime(file_path)

                                # ファイル名から日時を解析
                                try:
                                    # ファイル名のフォーマット: <カメラID>_YYYYMMDDHHmmSS.mp4
                                    date_str = file.split('_')[1].split('.')[0]
                                    date = datetime.strptime(date_str, '%Y%m%d%H%M%S')

                                except:
                                    date = datetime.fromtimestamp(file_mtime)

                                mp4_files.append({
                                    'filename': file,
                                    'size': file_size,
                                    'date': date,
                                    'mtime': file_mtime
                                })
                            except Exception as file_e:
                                logging.error(f"Error processing file {file}: {file_e}")
                                continue
                                
                    # 日時でソート（新しい順）
                    mp4_files.sort(key=lambda x: x['date'], reverse=True)
                    recordings[camera_id] = mp4_files
                    
                except Exception as dir_e:
                    logging.error(f"Error reading directory {camera_path}: {dir_e}")
                    continue

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

def check_camera_availability(cameras=None):
    """
    カメラの可用性を確認する

    Args:
        cameras (list, optional): 確認するカメラのリスト。指定なしの場合は全カメラ

    Returns:
        dict: カメラIDをキー、可用性を値とする辞書
    """
    import ffmpeg_utils  # 循環インポートを避けるため関数内でインポート
    
    if cameras is None:
        cameras = read_config()
        
    availability = {}
    
    for camera in cameras:
        camera_id = camera['id']
        rtsp_url = camera['rtsp_url']
        
        # RTSP接続をチェック
        available = ffmpeg_utils.check_rtsp_connection(rtsp_url)
        availability[camera_id] = available
        
    return availability

def restart_camera_hardware(camera_id):
    """
    カメラのハードウェア再起動を試みる
    
    Args:
        camera_id (str): 再起動するカメラID
        
    Returns:
        bool: 再起動リクエストが成功したかどうか
    """
    global _camera_restart_attempts
    
    # カメラ情報を取得
    camera = get_camera_by_id(camera_id)
    if not camera:
        logging.error(f"カメラID {camera_id} の情報が見つかりません")
        return False
    
    # 連続再起動試行回数をチェック
    current_time = time.time()
    if camera_id in _camera_restart_attempts:
        last_attempt, count = _camera_restart_attempts[camera_id]
        
        # 一定時間内に最大試行回数を超える場合は再起動しない
        if current_time - last_attempt < CAMERA_RESTART_INTERVAL and count >= MAX_CAMERA_RESTART_ATTEMPTS:
            logging.warning(f"カメラ {camera_id} の再起動試行回数が上限（{MAX_CAMERA_RESTART_ATTEMPTS}回）に達しました。次の再起動は {CAMERA_RESTART_INTERVAL - (current_time - last_attempt):.0f}秒後に可能になります。")
            return False
        
        # 一定時間経過したらカウントをリセット
        if current_time - last_attempt >= CAMERA_RESTART_INTERVAL:
            count = 0
            
        # カウントを更新
        _camera_restart_attempts[camera_id] = (current_time, count + 1)
    else:
        # 初回の試行
        _camera_restart_attempts[camera_id] = (current_time, 1)
    
    # カメラのURLを構築
    rtsp_url = camera['rtsp_url']
    
    # RTSPのURLからIPアドレスを抽出
    try:
        # rtsp://username:password@192.168.1.100:554/stream の形式を想定
        # または rtsp://192.168.1.100:554/stream の形式を想定
        parts = rtsp_url.split('@')
        if len(parts) > 1:
            # ユーザー名とパスワードがある場合
            ip_part = parts[1].split('/')[0]
        else:
            # ユーザー名とパスワードがない場合
            ip_part = parts[0].split('//')[1].split('/')[0]
        
        # ポート番号を除去
        ip_address = ip_part.split(':')[0]
        
        # 認証情報の抽出
        auth = None
        if len(parts) > 1 and '@' in rtsp_url:
            auth_part = rtsp_url.split('//')[1].split('@')[0]
            if ':' in auth_part:
                username, password = auth_part.split(':')
                auth = (username, password)
    except Exception as e:
        logging.error(f"カメラ {camera_id} のRTSP URLからIPアドレスとユーザー情報の抽出に失敗しました: {e}")
        return False
    
    # 再起動を試みる
    success = False
    
    try:
        # 一般的なIPカメラの再起動エンドポイントを試す
        endpoints = [
            # 一般的なHTTPベースの再起動エンドポイント
            f"http://{ip_address}/restart",
            f"http://{ip_address}/reboot",
            f"http://{ip_address}/cgi-bin/restart.cgi",
            f"http://{ip_address}/cgi-bin/reboot.cgi",
            f"http://{ip_address}/api/restart",
            f"http://{ip_address}/api/reboot"
        ]
        
        for endpoint in endpoints:
            try:
                logging.info(f"カメラ {camera_id} の再起動を試みます: {endpoint}")
                if auth:
                    response = requests.get(endpoint, auth=auth, timeout=5)
                else:
                    response = requests.get(endpoint, timeout=5)
                
                if response.status_code == 200:
                    logging.info(f"カメラ {camera_id} の再起動リクエスト成功: {endpoint}")
                    success = True
                    break
            except requests.RequestException:
                continue
        
        if not success:
            logging.warning(f"カメラ {camera_id} の標準的な再起動方法が失敗しました")
    except Exception as e:
        logging.error(f"カメラ {camera_id} の再起動処理中にエラーが発生しました: {e}")
    
    return success

def reset_camera_restart_attempts(camera_id=None):
    """
    カメラの再起動試行回数をリセットする
    
    Args:
        camera_id (str, optional): リセットするカメラID。指定なしの場合は全カメラ
    """
    global _camera_restart_attempts
    
    if camera_id:
        if camera_id in _camera_restart_attempts:
            del _camera_restart_attempts[camera_id]
            logging.info(f"カメラ {camera_id} の再起動試行回数をリセットしました")
    else:
        _camera_restart_attempts = {}
        logging.info("全カメラの再起動試行回数をリセットしました")

def get_enabled_cameras():
    """
    有効なカメラ（enabled=1）のみを返す
    Returns:
        list: 有効なカメラ情報のリスト
    """
    cameras = read_config()
    return [cam for cam in cameras if cam.get('enabled', 1) == 1]
