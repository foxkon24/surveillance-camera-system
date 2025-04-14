"""
ファイルシステムユーティリティ
ディレクトリ作成や空き容量チェックなどの機能を提供します
"""
import os
import logging
import psutil
from datetime import datetime
import time
import shutil

def ensure_directory_exists(path):
    """ディレクトリが存在しない場合は作成"""
    if not os.path.exists(path):
        os.makedirs(path)

        try:
            os.chmod(path, 0o777)  # ディレクトリに対して全権限を付与

        except OSError as e:
            logging.warning(f"Could not set directory permissions for {path}: {e}")

        logging.info(f"Created directory: {path}")

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

def secure_delete_file(file_path):
    """
    ファイルを安全に削除する（エラーハンドリング強化版）

    Args:
        file_path (str): 削除するファイルのパス
        
    Returns:
        bool: 操作が成功したかどうか
    """
    if not os.path.exists(file_path):
        return False
        
    try:
        # まずはシンプルに削除を試みる
        os.remove(file_path)
        return True
        
    except PermissionError:
        # ファイルがロックされている場合、少し待機してから再試行
        logging.warning(f"Permission error when deleting {file_path}, retrying...")
        try:
            time.sleep(1)
            os.remove(file_path)
            return True
        except Exception as e:
            logging.error(f"Failed to delete file after retry: {e}")
            return False
            
    except Exception as e:
        logging.error(f"Error deleting file {file_path}: {e}")
        return False

def copy_file_with_retries(src, dst, max_retries=3, retry_delay=1):
    """
    ファイルをコピーする関数（リトライ機能付き）

    Args:
        src (str): コピー元ファイルパス
        dst (str): コピー先ファイルパス
        max_retries (int): 最大再試行回数
        retry_delay (int): 再試行の間隔（秒）
        
    Returns:
        bool: 操作が成功したかどうか
    """
    for attempt in range(max_retries + 1):
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as e:
            if attempt < max_retries:
                logging.warning(f"Failed to copy {src} to {dst} (attempt {attempt+1}): {e}")
                time.sleep(retry_delay)
            else:
                logging.error(f"Failed to copy {src} to {dst} after {max_retries} attempts: {e}")
                return False
