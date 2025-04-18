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
        # ドライブが見つからない場合に備えて多重チェック
        if not os.path.exists(path):
            # まず親ディレクトリを試行
            parent_path = os.path.dirname(path)
            if os.path.exists(parent_path):
                logging.warning(f"Path {path} does not exist, checking parent path {parent_path}")
                path = parent_path
            else:
                # 最後の手段としてカレントディレクトリを使用
                logging.warning(f"Parent path {parent_path} does not exist, using current directory")
                path = os.getcwd()
        
        if os.path.exists(path):
            # Windowsの場合はドライブのルートパスを取得
            if os.name == 'nt':
                drive = os.path.splitdrive(os.path.abspath(path))[0]
                if drive:
                    try:
                        free_bytes = psutil.disk_usage(drive).free
                        logging.info(f"Free space on drive {drive}: {free_bytes / (1024*1024*1024):.2f} GB")
                    except Exception as e:
                        logging.error(f"Error getting free space for drive {drive}: {e}")
                        # パス自体で再試行
                        free_bytes = psutil.disk_usage(path).free
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
        # エラーが発生した場合は最小限の容量を返す
        return 1024 * 1024 * 1024  # 1GB

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

def cleanup_directory(directory, file_pattern='', max_age_seconds=None, max_files=None):
    """
    ディレクトリ内のファイルをクリーンアップする

    Args:
        directory (str): クリーンアップするディレクトリ
        file_pattern (str): 対象ファイルのパターン（例: '.ts'）
        max_age_seconds (int, optional): この秒数より古いファイルを削除
        max_files (int, optional): 保持する最大ファイル数
    """
    if not os.path.exists(directory):
        return

    try:
        current_time = time.time()
        
        # ディレクトリ内のファイルを取得
        files = []
        for filename in os.listdir(directory):
            if file_pattern and not filename.endswith(file_pattern):
                continue

            file_path = os.path.join(directory, filename)
            if not os.path.isfile(file_path):
                continue

            # ファイル情報を取得
            try:
                file_stat = os.stat(file_path)
                file_mtime = file_stat.st_mtime
                file_size = file_stat.st_size
                
                # 非常に小さいファイルや空のファイルは破損している可能性があるので削除
                if file_size < 1024:  # 1KB未満
                    try:
                        os.remove(file_path)
                        logging.info(f"Removed very small file: {file_path} (size: {file_size} bytes)")
                        continue
                    except OSError as e:
                        logging.error(f"Error removing small file {file_path}: {e}")
                
                # ファイル情報を追加
                files.append({
                    'path': file_path,
                    'mtime': file_mtime,
                    'size': file_size
                })
            except OSError as e:
                logging.error(f"Error getting info for file {file_path}: {e}")

        # 削除するファイルを特定
        files_to_delete = []

        # 1. 古いファイルの削除
        if max_age_seconds:
            for file_info in files:
                if current_time - file_info['mtime'] > max_age_seconds:
                    files_to_delete.append(file_info['path'])
                    
        # 削除対象ではないファイルを取得
        remaining_files = [f for f in files if f['path'] not in files_to_delete]
                    
        # 2. ファイル数制限
        if max_files and len(remaining_files) > max_files:
            # 更新日時でソート（古い順）
            remaining_files.sort(key=lambda x: x['mtime'])
            
            # 古いファイルから削除
            excess_count = len(remaining_files) - max_files
            for i in range(excess_count):
                files_to_delete.append(remaining_files[i]['path'])

        # 削除を実行
        for file_path in files_to_delete:
            try:
                os.remove(file_path)
                logging.info(f"Removed old file: {file_path}")
            except OSError as e:
                logging.error(f"Error removing file {file_path}: {e}")

        # 削除したファイル数を返す
        return len(files_to_delete)

    except Exception as e:
        logging.error(f"Error cleaning up directory {directory}: {e}")
        return 0

def check_disk_space(path, min_free_space_gb=2):
    """
    ディスク容量をチェックし、不足している場合は警告を表示

    Args:
        path (str): チェックするパス
        min_free_space_gb (float): 最小必要空き容量（GB）

    Returns:
        bool: 十分な空き容量があるかどうか
    """
    try:
        # 空き容量を取得
        free_space = get_free_space(path)
        free_space_gb = free_space / (1024 * 1024 * 1024)
        
        # 空き容量が最小値未満の場合
        if free_space_gb < min_free_space_gb:
            logging.warning(f"Low disk space on {path}: {free_space_gb:.2f}GB available, {min_free_space_gb}GB required")
            return False
        
        return True
        
    except Exception as e:
        logging.error(f"Error checking disk space for {path}: {e}")
        return False

def backup_file(source_path, dest_dir):
    """
    ファイルをバックアップディレクトリにコピー

    Args:
        source_path (str): コピー元ファイルパス
        dest_dir (str): コピー先ディレクトリ

    Returns:
        str or None: コピー先ファイルパスまたはNone（失敗時）
    """
    try:
        if not os.path.exists(source_path):
            logging.error(f"Source file does not exist: {source_path}")
            return None
            
        # コピー先ディレクトリの確認
        ensure_directory_exists(dest_dir)
        
        # ファイル名の取得
        filename = os.path.basename(source_path)
        dest_path = os.path.join(dest_dir, filename)
        
        # ファイルをコピー
        shutil.copy2(source_path, dest_path)
        logging.info(f"File backed up: {source_path} -> {dest_path}")
        
        return dest_path
        
    except Exception as e:
        logging.error(f"Error backing up file {source_path}: {e}")
        return None

def repair_mp4_file(file_path):
    """
    MP4ファイルの整合性をチェックし、可能であれば修復を試みる

    Args:
        file_path (str): チェック/修復するファイルパス

    Returns:
        bool: ファイルが有効かどうか
    """
    try:
        # ファイルが存在するか確認
        if not os.path.exists(file_path):
            logging.error(f"File does not exist: {file_path}")
            return False
            
        # ファイルサイズをチェック
        file_size = os.path.getsize(file_path)
        if file_size < 1024:  # 1KB未満
            logging.warning(f"File is too small: {file_path} ({file_size} bytes)")
            return False
            
        # ffmpegを使用して簡易チェック
        import subprocess
        
        # オペレーティングシステムに応じてcreationflagsを設定
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW
            
        # ファイルのヘッダー情報をチェック
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_type',
            '-of', 'csv=p=0',
            file_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=creation_flags)
        
        # ビデオストリームが存在するか確認
        if 'video' in result.stdout:
            return True
            
        # ファイルに問題がある場合は修復を試みる
        logging.warning(f"Attempting to repair file: {file_path}")
        
        # 一時ファイル名を生成
        temp_file = file_path + '.repaired.mp4'
        
        # ffmpegでファイルをリエンコード
        repair_cmd = [
            'ffmpeg',
            '-v', 'warning',
            '-err_detect', 'ignore_err',
            '-i', file_path,
            '-c', 'copy',
            '-y',
            temp_file
        ]
        
        repair_result = subprocess.run(repair_cmd, capture_output=True, text=True, creationflags=creation_flags)
        
        # 修復に成功した場合
        if repair_result.returncode == 0 and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1024:
            # 元のファイルを置き換え
            backup_file = file_path + '.bak'
            os.rename(file_path, backup_file)
            os.rename(temp_file, file_path)
            logging.info(f"File repaired successfully: {file_path}")
            
            # バックアップファイルを削除
            try:
                os.remove(backup_file)
            except:
                pass
                
            return True
        else:
            logging.error(f"Failed to repair file: {file_path}")
            
            # 一時ファイルがあれば削除
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
                    
            return False
        
    except Exception as e:
        logging.error(f"Error checking/repairing file {file_path}: {e}")
        return False
