"""
カメラ管理モジュール
カメラ設定の読み込みと管理機能を提供します
"""
import os
import logging
from datetime import datetime
import config

def read_config():
    """
    カメラ設定を読み込む

    Returns:
        list: カメラ情報のリスト。各カメラは辞書形式。
    """
    try:
        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            cameras = []

            for line in file:
                parts = line.strip().split(',')

                # RTSPURLが空の場合はスキップ
                if len(parts) >= 3 and parts[2].strip():
                    cameras.append({
                        'id': parts[0],
                        'name': parts[1],
                        'rtsp_url': parts[2]
                    })

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
