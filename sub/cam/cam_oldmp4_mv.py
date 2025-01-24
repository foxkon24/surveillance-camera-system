import os
import shutil
from datetime import datetime, timedelta
import re
from moviepy.editor import VideoFileClip

def create_directory_if_not_exists(directory):
    """指定されたディレクトリが存在しない場合、作成する"""
    if not os.path.exists(directory):
        os.makedirs(directory)

def get_camera_numbers():
    """cam_config.txtから項番を取得する"""
    camera_numbers = []
    config_path = r'D:\xampp\htdocs\system\cam\cam_config.txt'

    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            number = line.split(',')[0]
            camera_numbers.append(number)

    return camera_numbers

def check_video_duration(file_path):
    """動画ファイルの長さを確認する
    Returns:
        bool: 動画の長さが0秒でない場合はTrue、0秒の場合はFalse
    """
    try:
        with VideoFileClip(file_path) as clip:
            # 動画の長さが0秒より大きいかチェック
            return clip.duration > 0
    except Exception as e:
        print(f"動画ファイルの長さ確認中にエラー発生: {str(e)}")
        return False

def process_mp4_files():
    """MP4ファイルの処理を実行する"""
    # ベースディレクトリのパス
    record_base_dir = r'D:\xampp\htdocs\system\cam\record'
    backup_base_dir = r'D:\xampp\htdocs\system\cam\backup'

    # バックアップベースディレクトリの作成
    create_directory_if_not_exists(backup_base_dir)

    # 現在の日付を取得（時分秒を0にセット）
    current_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # 昨日の日付を計算
    yesterday = current_date - timedelta(days=1)

    # カメラ番号のリストを取得
    camera_numbers = get_camera_numbers()

    for camera_num in camera_numbers:
        # レコードディレクトリのパス
        record_dir = os.path.join(record_base_dir, f"{camera_num}")
        # バックアップディレクトリのパス
        backup_dir = os.path.join(backup_base_dir, f"{camera_num}")

        # バックアップディレクトリが存在しない場合は作成
        create_directory_if_not_exists(backup_dir)

        # レコードディレクトリが存在しない場合はスキップ
        if not os.path.exists(record_dir):
            continue

        # MP4ファイルを処理
        for filename in os.listdir(record_dir):
            if filename.endswith('.mp4'):
                # ファイル名から日時を抽出 (例: 1_20241212000000.mp4)
                match = re.search(r'_(\d{14})\.mp4$', filename)
                if match:
                    date_str = match.group(1)
                    file_date = datetime.strptime(date_str, '%Y%m%d%H%M%S')
                    # ファイルの日付部分のみを比較するため、時分秒を0にセット
                    file_date = file_date.replace(hour=0, minute=0, second=0, microsecond=0)

                    # ファイルが昨日以前の場合
                    if file_date <= yesterday:
                        source_path = os.path.join(record_dir, filename)
                        dest_path = os.path.join(backup_dir, filename)

                        try:
                            # ファイルを移動
                            shutil.move(source_path, dest_path)
                            print(f"移動完了: {filename}")

                            # 移動後のファイルの動画長をチェック
                            if not check_video_duration(dest_path):
                                # 動画長が0秒の場合、ファイルを削除
                                os.remove(dest_path)
                                print(f"動画長が0秒のため削除: {filename}")

                        except Exception as e:
                            print(f"エラー - {filename}の処理中: {str(e)}")

if __name__ == "__main__":
    try:
        process_mp4_files()
        print("処理が正常に完了しました。")
    except Exception as e:
        print(f"プログラムの実行中にエラーが発生しました: {str(e)}")

