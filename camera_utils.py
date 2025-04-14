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
        # 設定ファイルが存在するか確認
        if not os.path.exists(config.CONFIG_PATH):
            logging.error(f"カメラ設定ファイルが見つかりません: {config.CONFIG_PATH}")
            return []

        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            cameras = []
            line_number = 0

            for line in file:
                line_number += 1
                # コメント行や空行をスキップ
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                try:
                    parts = line.split(',')

                    # 最低限の要素数チェック
                    if len(parts) < 3:
                        logging.warning(f"設定ファイルの {line_number} 行目のフォーマットが不正です: {line}")
                        continue

                    # RTSPURLが空の場合はスキップ
                    if not parts[2].strip():
                        logging.warning(f"設定ファイルの {line_number} 行目のRTSP URLが空です: {line}")
                        continue

                    cameras.append({
                        'id': parts[0].strip(),
                        'name': parts[1].strip(),
                        'rtsp_url': parts[2].strip()
                    })

                except Exception as e:
                    logging.error(f"設定ファイルの {line_number} 行目の解析中にエラーが発生しました: {e}")
                    continue

            if not cameras:
                logging.warning("有効なカメラ設定が見つかりませんでした")
            else:
                logging.info(f"{len(cameras)} 台のカメラ設定を読み込みました")

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
        if not os.path.exists(config.CONFIG_PATH):
            logging.error(f"カメラ設定ファイルが見つかりません: {config.CONFIG_PATH}")
            return {}

        with open(config.CONFIG_PATH, 'r', encoding='utf-8') as file:
            for line in file:
                # コメント行や空行をスキップ
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                parts = line.split(',')
                if len(parts) >= 2:
                    camera_id = parts[0].strip()
                    camera_name = parts[1].strip()
                    camera_names[camera_id] = camera_name  # カメラIDと名前をマッピング

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

                try:
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
                                except Exception:
                                    date = datetime.fromtimestamp(file_mtime)

                                mp4_files.append({
                                    'filename': file,
                                    'size': file_size,
                                    'date': date,
                                    'mtime': file_mtime
                                })
                            except Exception as e:
                                logging.warning(f"ファイル情報取得エラー {file}: {e}")
                except Exception as e:
                    logging.error(f"ディレクトリ読み込みエラー {camera_path}: {e}")
                    continue

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

    logging.warning(f"Camera ID {camera_id} not found in configuration")
    return None

def validate_rtsp_url(rtsp_url):
    """
    RTSP URLの形式を検証する

    Args:
        rtsp_url (str): 検証するRTSP URL

    Returns:
        bool: URLが有効かどうか
        str: エラーメッセージ（有効な場合は空文字）
    """
    if not rtsp_url:
        return False, "RTSP URLが空です"
    
    # 基本的な形式チェック
    if not rtsp_url.startswith('rtsp://'):
        return False, "URLはrtsp://で始まる必要があります"

    # 最低限のコンポーネントを持っているか
    parts = rtsp_url.split('/')
    if len(parts) < 4:  # ['rtsp:', '', 'host:port', 'path']
        return False, "URLの形式が不正です。rtsp://ホスト[:ポート]/パスの形式である必要があります"

    # ホスト部分が存在するか
    host_part = parts[2]
    if not host_part:
        return False, "ホスト名が指定されていません"

    return True, ""

def test_camera_connection(camera_id):
    """
    指定されたカメラに接続テストを行う

    Args:
        camera_id (str): テストするカメラID

    Returns:
        bool: 接続成功したかどうか
        str: エラーメッセージ（成功した場合は空文字）
    """
    try:
        import ffmpeg_utils
        
        camera = get_camera_by_id(camera_id)
        if not camera:
            return False, f"カメラID {camera_id} の設定が見つかりません"
        
        rtsp_url = camera['rtsp_url']
        valid, error_msg = validate_rtsp_url(rtsp_url)
        if not valid:
            return False, error_msg
        
        # RTSPストリームに接続テスト
        success, error = ffmpeg_utils.check_rtsp_connection(rtsp_url)
        if not success:
            return False, f"接続テスト失敗: {error}"
        
        return True, ""
        
    except Exception as e:
        return False, f"接続テスト中にエラーが発生しました: {e}"
