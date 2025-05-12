"""
ファイルシステムユーティリティ
ディレクトリ作成や空き容量チェックなどの機能を提供します
"""
import os
import logging
import psutil
import time
import shutil
import random
import string
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

def generate_file_suffix():
    """
    ファイル名の重複を避けるためのランダムな接尾辞を生成

    Returns:
        str: 3文字のランダムな文字列
    """
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=3))

def get_record_file_path(base_path, camera_id):
    """
    録画ファイルのパスを生成する関数

    Args:
        base_path (str): 録画保存の基本パス
        camera_id (str): カメラID

    Returns:
        str: 録画ファイルの完全パス
    """
    # まずカメラIDに対応するディレクトリを確保
    camera_dir = os.path.join(base_path, camera_id)
    ensure_directory_exists(camera_dir)
    
    # 現在の日時をフォーマット
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M%S")
    
    # ランダムサフィックスは不要なので削除
    # ファイル名の形式: {カメラID}_{日付}{時間}.mp4
    file_name = f"{camera_id}_{date_str}{time_str}.mp4"
    file_path = os.path.join(camera_dir, file_name)
    
    logging.info(f"Generated record file path: {file_path}")
    return file_path

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

def get_directory_size(path):
    """
    ディレクトリの合計サイズをバイト単位で取得

    Args:
        path (str): サイズを取得するディレクトリパス

    Returns:
        int: ディレクトリサイズ（バイト）
    """
    try:
        if not os.path.exists(path):
            logging.warning(f"Directory does not exist: {path}")
            return 0
            
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total_size += os.path.getsize(fp)
                except OSError as e:
                    logging.warning(f"Error getting size of {fp}: {e}")
                    
        return total_size
        
    except Exception as e:
        logging.error(f"Error calculating directory size for {path}: {e}")
        return 0

def format_size(size_bytes):
    """
    バイト数を読みやすい単位にフォーマット

    Args:
        size_bytes (int): バイト単位のサイズ

    Returns:
        str: フォーマットされたサイズ文字列
    """
    try:
        # ゼロやマイナス値の処理
        if size_bytes <= 0:
            return "0 B"
            
        # 単位の定義
        units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
        
        # 適切な単位を計算
        i = 0
        while size_bytes >= 1024 and i < len(units) - 1:
            size_bytes /= 1024
            i += 1
            
        # 小数点以下2桁でフォーマット
        return f"{size_bytes:.2f} {units[i]}"
        
    except Exception as e:
        logging.error(f"Error formatting size: {e}")
        return f"{size_bytes} B"

def clean_small_recordings(base_path, min_size_kb=1024):
    """
    指定されたサイズより小さい録画ファイルを削除する

    Args:
        base_path (str): 録画ファイルの基本ディレクトリ
        min_size_kb (int): 最小ファイルサイズ（KB）

    Returns:
        int: 削除されたファイル数
    """
    min_size = min_size_kb * 1024  # KB -> バイトに変換
    deleted_count = 0

    try:
        logging.info(f"小さい録画ファイルのクリーンアップを開始します: {base_path}")
        
        # ベースディレクトリが存在しない場合は終了
        if not os.path.exists(base_path):
            logging.warning(f"ディレクトリが存在しません: {base_path}")
            return 0
            
        # カメラIDごとのディレクトリを処理
        for camera_id in os.listdir(base_path):
            camera_dir = os.path.join(base_path, camera_id)
            if not os.path.isdir(camera_dir):
                continue

            # ディレクトリ内のファイルを処理
            mp4_files = []
            
            # まずはすべてのMP4ファイルを収集
            for file_name in os.listdir(camera_dir):
                # MP4ファイルのみを対象
                if not file_name.endswith('.mp4'):
                    continue
                    
                file_path = os.path.join(camera_dir, file_name)
                if not os.path.isfile(file_path):
                    continue
                
                # ファイルサイズを取得
                try:
                    file_size = os.path.getsize(file_path)
                    mtime = os.path.getmtime(file_path)
                    
                    # ファイル情報を保存
                    mp4_files.append({
                        'path': file_path,
                        'name': file_name,
                        'size': file_size,
                        'mtime': mtime
                    })
                except Exception as size_err:
                    logging.error(f"ファイルサイズ取得エラー: {file_path} - {size_err}")
            
            # 似たタイムスタンプのファイルをグループ化して、小さいファイルを削除
            # 時間でソート
            mp4_files.sort(key=lambda x: x['mtime'])
            
            # 類似のタイムスタンプを持つファイルを検索（10秒以内の違い）
            for i in range(len(mp4_files)):
                current_file = mp4_files[i]
                
                # すでに処理済みなら次へ
                if current_file.get('processed', False):
                    continue
                
                # 同じ時間帯のファイルを探す
                similar_files = []
                for j in range(len(mp4_files)):
                    if i != j and not mp4_files[j].get('processed', False):
                        time_diff = abs(current_file['mtime'] - mp4_files[j]['mtime'])
                        # 10秒以内の時間差のファイルをグループ化
                        if time_diff < 10:
                            similar_files.append(mp4_files[j])
                
                # 類似ファイルが見つかった場合、サイズで比較
                if similar_files:
                    similar_files.append(current_file)  # 現在のファイルも含める
                    
                    # サイズで降順ソート
                    similar_files.sort(key=lambda x: x['size'], reverse=True)
                    
                    # 最大サイズのファイル以外を削除
                    for file_idx in range(1, len(similar_files)):
                        file_to_delete = similar_files[file_idx]
                        
                        # サイズが小さくて削除対象
                        try:
                            os.remove(file_to_delete['path'])
                            deleted_count += 1
                            logging.info(f"重複録画ファイルを削除しました: {file_to_delete['path']} ({file_to_delete['size']/1024:.2f} KB)")
                            # 処理済みとしてマーク
                            for k in range(len(mp4_files)):
                                if mp4_files[k]['path'] == file_to_delete['path']:
                                    mp4_files[k]['processed'] = True
                                    break
                        except Exception as e:
                            logging.error(f"ファイル削除エラー: {file_to_delete['path']} - {e}")
                    
                    # 現在のファイルも処理済みとしてマーク
                    current_file['processed'] = True
                
            # 残りの小さなファイルを処理
            for file_info in mp4_files:
                if not file_info.get('processed', False) and file_info['size'] < min_size:
                    try:
                        os.remove(file_info['path'])
                        deleted_count += 1
                        logging.info(f"小さい録画ファイルを削除しました: {file_info['path']} ({file_info['size']/1024:.2f} KB)")
                    except Exception as e:
                        logging.error(f"ファイル削除エラー: {file_info['path']} - {e}")

        logging.info(f"クリーンアップ完了: {deleted_count}ファイルを削除しました")
        return deleted_count

    except Exception as e:
        logging.error(f"録画ファイルのクリーンアップ中にエラーが発生しました: {e}")
        return 0

def remove_directory(dir_path):
    """
    ディレクトリとその中のすべてのファイルを完全に削除します
    
    Args:
        dir_path (str): 削除するディレクトリのパス
        
    Returns:
        bool: 操作が成功したかどうか
    """
    try:
        if not os.path.exists(dir_path):
            return True
            
        # ディレクトリ内のすべてのファイルを削除
        for filename in os.listdir(dir_path):
            file_path = os.path.join(dir_path, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    logging.debug(f"ファイルを削除しました: {file_path}")
                elif os.path.isdir(file_path):
                    # サブディレクトリも再帰的に削除
                    remove_directory(file_path)
            except Exception as e:
                logging.warning(f"ファイル {file_path} の削除に失敗: {e}")
                
        # ディレクトリ自体を削除
        os.rmdir(dir_path)
        logging.info(f"ディレクトリを削除しました: {dir_path}")
        return True
    
    except Exception as e:
        logging.error(f"ディレクトリ {dir_path} の削除中にエラーが発生しました: {e}")
        return False
