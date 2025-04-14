"""
ファイルシステムユーティリティ
ディレクトリ作成や空き容量チェックなどの機能を提供します
"""
import os
import logging
import psutil
import time
import shutil
from datetime import datetime

def ensure_directory_exists(path):
    """ディレクトリが存在しない場合は作成"""
    if not os.path.exists(path):
        try:
            os.makedirs(path)
            logging.info(f"Created directory: {path}")
            
            # ディレクトリに対して全権限を付与
            try:
                os.chmod(path, 0o777)
            except OSError as e:
                logging.warning(f"Could not set directory permissions for {path}: {e}")
                
        except Exception as e:
            logging.error(f"Error creating directory {path}: {e}")
            raise
    else:
        # ディレクトリが既に存在する場合でも書き込み権限をチェック
        if not os.access(path, os.W_OK):
            logging.warning(f"Directory {path} exists but may not be writable")

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
                if drive:
                    free_bytes = psutil.disk_usage(drive).free
                else:
                    free_bytes = psutil.disk_usage(path).free
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
    try:
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        filename = f"{camera_id}_{timestamp}.mp4"
        camera_dir = os.path.join(base_path, str(camera_id))
        ensure_directory_exists(camera_dir)
        full_path = os.path.normpath(os.path.join(camera_dir, filename))
        logging.info(f"Generated file path: {full_path}")
        return full_path
    except Exception as e:
        logging.error(f"Error generating file path for camera {camera_id}: {e}")
        # デフォルトパスを返す
        default_path = os.path.normpath(os.path.join(base_path, str(camera_id), f"{camera_id}_error.mp4"))
        return default_path

def cleanup_directory(directory, file_pattern='', max_age_seconds=None):
    """
    ディレクトリ内のファイルをクリーンアップする

    Args:
        directory (str): クリーンアップするディレクトリ
        file_pattern (str): 対象ファイルのパターン（例: '.ts'）
        max_age_seconds (int, optional): この秒数より古いファイルを削除
    """
    if not os.path.exists(directory):
        logging.warning(f"Cleanup directory does not exist: {directory}")
        return

    try:
        current_time = time.time()
        cleaned_count = 0
        failed_count = 0

        for filename in os.listdir(directory):
            if file_pattern and not filename.endswith(file_pattern):
                continue

            file_path = os.path.join(directory, filename)
            if not os.path.isfile(file_path):
                continue

            if max_age_seconds and (current_time - os.path.getctime(file_path)) > max_age_seconds:
                try:
                    os.remove(file_path)
                    cleaned_count += 1
                except OSError as e:
                    failed_count += 1
                    logging.error(f"Error removing file {file_path}: {e}")

        if cleaned_count > 0:
            logging.info(f"Cleaned up {cleaned_count} files from {directory}")
        if failed_count > 0:
            logging.warning(f"Failed to remove {failed_count} files from {directory}")

    except Exception as e:
        logging.error(f"Error cleaning up directory {directory}: {e}")

def move_file(src, dst, overwrite=False):
    """
    ファイルを移動する

    Args:
        src (str): 移動元のファイルパス
        dst (str): 移動先のファイルパス
        overwrite (bool): 移動先に同名のファイルが存在する場合に上書きするかどうか

    Returns:
        bool: 移動が成功したかどうか
    """
    try:
        # 移動元ファイルの存在確認
        if not os.path.exists(src):
            logging.error(f"Source file does not exist: {src}")
            return False
            
        # 移動先ディレクトリの存在確認
        dst_dir = os.path.dirname(dst)
        if not os.path.exists(dst_dir):
            ensure_directory_exists(dst_dir)
            
        # 移動先ファイルの存在確認
        if os.path.exists(dst):
            if overwrite:
                try:
                    os.remove(dst)
                except OSError as e:
                    logging.error(f"Error removing existing destination file {dst}: {e}")
                    return False
            else:
                logging.warning(f"Destination file already exists: {dst}")
                return False
                
        # ファイル移動
        shutil.move(src, dst)
        logging.info(f"Moved file from {src} to {dst}")
        return True
        
    except Exception as e:
        logging.error(f"Error moving file from {src} to {dst}: {e}")
        return False

def copy_file(src, dst, overwrite=False):
    """
    ファイルをコピーする

    Args:
        src (str): コピー元のファイルパス
        dst (str): コピー先のファイルパス
        overwrite (bool): コピー先に同名のファイルが存在する場合に上書きするかどうか

    Returns:
        bool: コピーが成功したかどうか
    """
    try:
        # コピー元ファイルの存在確認
        if not os.path.exists(src):
            logging.error(f"Source file does not exist: {src}")
            return False
            
        # コピー先ディレクトリの存在確認
        dst_dir = os.path.dirname(dst)
        if not os.path.exists(dst_dir):
            ensure_directory_exists(dst_dir)
            
        # コピー先ファイルの存在確認
        if os.path.exists(dst):
            if overwrite:
                try:
                    os.remove(dst)
                except OSError as e:
                    logging.error(f"Error removing existing destination file {dst}: {e}")
                    return False
            else:
                logging.warning(f"Destination file already exists: {dst}")
                return False
                
        # ファイルコピー
        shutil.copy2(src, dst)
        logging.info(f"Copied file from {src} to {dst}")
        return True
        
    except Exception as e:
        logging.error(f"Error copying file from {src} to {dst}: {e}")
        return False
