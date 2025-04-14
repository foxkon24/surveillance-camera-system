"""
カメラ管理モジュール
カメラ設定の読み込みと管理機能を提供します
"""
import os
import logging
from datetime import datetime
import config
import subprocess
import json

def read_config():
    """
    カメラ設定を読み込む

    Returns:
        list: カメラ情報のリスト。各カメラは辞書形式。
    """
    try:
        # 設定ファイルが存在するか確認
        if not os.path.exists(config.CONFIG_PATH):
            logging.error(f"設定ファイルが見つかりません: {config.CONFIG_PATH}")
            return []
            
        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            cameras = []

            for line in file:
                line = line.strip()
                # 空行をスキップ
                if not line or line.startswith('#'):
                    continue
                    
                parts = line.strip().split(',')

                # RTSPURLが空の場合はスキップ
                if len(parts) >= 3 and parts[2].strip():
                    camera = {
                        'id': parts[0],
                        'name': parts[1],
                        'rtsp_url': parts[2]
                    }
                    
                    # オプションの追加フィールドを処理
                    if len(parts) > 3:
                        for i in range(3, len(parts)):
                            if '=' in parts[i]:
                                key, value = parts[i].split('=', 1)
                                camera[key.strip()] = value.strip()
                    
                    cameras.append(camera)

            if not cameras:
                logging.warning("有効なカメラ設定が見つかりません")
                
            return cameras

    except Exception as e:
        logging.error(f"設定ファイル読み込みエラー: {e}")
        return []

def read_config_names():
    """
    カメラID/名前マッピングを読み込む

    Returns:
        dict: カメラIDをキー、カメラ名を値とする辞書
    """
    camera_names = {}
    try:
        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                # 空行をスキップ
                if not line or line.startswith('#'):
                    continue
                    
                parts = line.strip().split(',')
                if len(parts) >= 2:
                    camera_names[parts[0]] = parts[1]  # カメラIDと名前をマッピング

    except Exception as e:
        logging.error(f"設定ファイル読み込みエラー: {e}")
        return {}

    return camera_names

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

def test_rtsp_connection(rtsp_url, timeout=5):
    """
    RTSPストリームの接続をテストする

    Args:
        rtsp_url (str): テストするRTSP URL
        timeout (int): タイムアウト秒数

    Returns:
        tuple: (成功したかどうか, エラーメッセージ)
    """
    try:
        # FFprobeを使用してRTSPストリームをテスト
        command = [
            'ffprobe',
            '-rtsp_transport', 'tcp',
            '-v', 'error',
            '-timeout', str(timeout * 1000000),  # マイクロ秒単位
            '-i', rtsp_url,
            '-show_entries', 'stream=codec_type',
            '-of', 'json'
        ]
        
        result = subprocess.run(command, capture_output=True, timeout=timeout+2)
        
        if result.returncode == 0:
            # 成功
            return (True, "Connection successful")
        else:
            # 失敗
            error_output = result.stderr.decode('utf-8', errors='replace')
            if "401 Unauthorized" in error_output:
                return (False, "Authentication failed - incorrect username or password")
            elif "Connection timed out" in error_output or "Operation timed out" in error_output:
                return (False, "Connection timed out - check IP address and network")
            else:
                return (False, f"Connection failed: {error_output.strip()}")
                
    except subprocess.TimeoutExpired:
        return (False, "Process timed out")
    except Exception as e:
        return (False, f"Error: {str(e)}")

def get_rtsp_url_with_auth(camera):
    """
    認証情報を含むRTSP URLを安全に生成する

    Args:
        camera (dict): カメラ情報辞書

    Returns:
        str: 正規化されたRTSP URL
    """
    try:
        # RTSPのURLパターンを解析
        base_url = camera['rtsp_url']
        
        # すでに認証情報が含まれている場合はそのまま返す
        if '@' in base_url and '://' in base_url:
            return base_url
        
        # URLパターンが rtsp:// で始まることを確認
        if not base_url.startswith('rtsp://'):
            logging.warning(f"Invalid RTSP URL format for camera {camera['id']}: {base_url}")
            return base_url
        
        # 認証情報がある場合は追加
        if 'username' in camera and 'password' in camera:
            # rtsp:// の後に認証情報を挿入
            url_parts = base_url.split('://', 1)
            if len(url_parts) == 2:
                protocol, address = url_parts
                auth_url = f"{protocol}://{camera['username']}:{camera['password']}@{address}"
                return auth_url
        
        return base_url
    
    except Exception as e:
        logging.error(f"Error formatting RTSP URL for camera {camera['id']}: {e}")
        return camera['rtsp_url']  # エラー時は元のURLを返す
