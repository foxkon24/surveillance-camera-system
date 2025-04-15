"""
ファイルシステムユーティリティ
ディレクトリ作成や空き容量チェックなどの機能を提供します
"""
import os
import logging
import psutil
import time
from datetime import datetime

def ensure_directory_exists(path):
    """ディレクトリが存在しない場合は作成"""
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
            logging.info(f"Created directory: {path}")
        except Exception as e:
            logging.error(f"Error creating directory {path}: {e}")
            raise

        try:
            # Windows環境の場合は権限設定が異なる
            if os.name == 'nt':
                # Windowsでは特に権限設定は不要なことが多い
                pass
            else:
                os.chmod(path, 0o777)  # ディレクトリに対して全権限を付与
            
            logging.info(f"Set directory permissions for {path}")
        except OSError as e:
            logging.warning(f"Could not set directory permissions for {path}: {e}")
    elif not os.path.isdir(path):
        logging.error(f"Path exists but is not a directory: {path}")
        raise ValueError(f"Path exists but is not a directory: {path}")
    
    # ディレクトリの書き込み権限をチェック
    try:
        test_file_path = os.path.join(path, "_test_write_permission.tmp")
        with open(test_file_path, 'w') as f:
            f.write('test')
        os.remove(test_file_path)
        logging.debug(f"Verified write permissions for directory: {path}")
    except Exception as e:
        logging.error(f"Directory {path} does not have write permissions: {e}")
        raise

def get_free_space(path):
    """
    指定されたパスの空き容量をバイト単位で返す

    Args:
        path (str): チェックするディレクトリパス

    Returns:
        int: 空き容量（バイト）
    """
    try:
        if os.path.exists(path):
            # Windowsの場合はドライブのルートパスを取得
            if os.name == 'nt':
                drive = os.path.splitdrive(os.path.abspath(path))[0]
                free_bytes = psutil.disk_usage(drive).free
            else:
                free_bytes = psutil.disk_usage(path).free

            logging.info(f"Free space in {path}: {free_bytes / (1024*1024*1024):.2f} GB")

            return free_bytes
        else:
            logging.warning(f"Path does not exist: {path}")
            return 0

    except Exception as e:
        logging.error(f"Error getting free space for {path}: {e}")
        return 0

def get_record_file_path(base_path, camera_id):
    """
    録画ファイルのパスを生成する関数

    Args:
        base_path (str): 録画保存の基本パス
        camera_id (str): カメラID

    Returns:
        str: 録画ファイルの完全パス
    """
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    filename = f"{camera_id}_{timestamp}.mp4"
    camera_dir = os.path.join(base_path, str(camera_id))
    ensure_directory_exists(camera_dir)
    full_path = os.path.join(camera_dir, filename)
    logging.info(f"Generated file path: {full_path}")

    return full_path

def cleanup_directory(directory, file_pattern='', max_age_seconds=None):
    """
    ディレクトリ内のファイルをクリーンアップする

    Args:
        directory (str): クリーンアップするディレクトリ
        file_pattern (str): 対象ファイルのパターン（例: '.ts'）
        max_age_seconds (int, optional): この秒数より古いファイルを削除
    """
    if not os.path.exists(directory):
        return

    try:
        current_time = time.time()

        for filename in os.listdir(directory):
            if file_pattern and not filename.endswith(file_pattern):
                continue

            file_path = os.path.join(directory, filename)
            if not os.path.isfile(file_path):
                continue

            if max_age_seconds and (current_time - os.path.getctime(file_path)) > max_age_seconds:
                try:
                    os.remove(file_path)
                    logging.info(f"Removed old file: {file_path}")

                except OSError as e:
                    logging.error(f"Error removing file {file_path}: {e}")

    except Exception as e:
        logging.error(f"Error cleaning up directory {directory}: {e}")
