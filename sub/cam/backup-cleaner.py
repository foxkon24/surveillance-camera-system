import os
from datetime import datetime, timedelta
import re

def create_directory_if_not_exists(directory):
    """指定されたディレクトリが存在しない場合、作成する"""
    if not os.path.exists(directory):
        os.makedirs(directory)

def get_camera_numbers():
    """cam_config.txtから項番を取得する"""
    camera_numbers = []
    config_path = r'D:\laragon\www\system\cam\cam_config.txt'
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            number = line.split(',')[0]
            camera_numbers.append(number)

    return camera_numbers

def delete_old_backup_files():
    """バックアップディレクトリ内の6ヶ月以上経過したMP4ファイルを削除する"""
    # バックアップベースディレクトリのパス
    backup_base_dir = r'D:\laragon\www\system\cam\backup'

    # 現在の日付を取得
    current_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # 6ヶ月前の日付を計算
    six_months_ago = current_date - timedelta(days=180)

    print(f"現在日: {current_date.strftime('%Y-%m-%d')}")
    print(f"6ヶ月前の日付: {six_months_ago.strftime('%Y-%m-%d')}")
    print("この日付より前のファイルを削除します")

    # カメラ番号のリストを取得
    camera_numbers = get_camera_numbers()

    total_deleted = 0
    total_size_deleted = 0

    for camera_num in camera_numbers:
        # バックアップディレクトリのパス
        backup_dir = os.path.join(backup_base_dir, f"{camera_num}")

        # バックアップディレクトリが存在しない場合はスキップ
        if not os.path.exists(backup_dir):
            print(f"カメラID {camera_num} のバックアップディレクトリが存在しません")
            continue

        camera_deleted = 0
        camera_size_deleted = 0

        # バックアップディレクトリ内のMP4ファイルを処理
        for filename in os.listdir(backup_dir):
            if filename.endswith('.mp4'):
                # ファイル名から日時を抽出 (例: 1_20241212000000.mp4)
                match = re.search(r'_(\d{14})\.mp4$', filename)
                if match:
                    date_str = match.group(1)

                    try:
                        file_date = datetime.strptime(date_str, '%Y%m%d%H%M%S')
                        # ファイルの日付部分のみを比較するため、時分秒を0にセット
                        file_date = file_date.replace(hour=0, minute=0, second=0, microsecond=0)

                        # ファイルが6ヶ月以上前の場合
                        if file_date <= six_months_ago:
                            file_path = os.path.join(backup_dir, filename)

                            try:
                                # ファイルサイズを取得 (バイト単位)
                                file_size = os.path.getsize(file_path)
                                # ファイルを削除
                                os.remove(file_path)
                                print(f"削除完了: カメラID {camera_num} - {filename} (作成日: {file_date.strftime('%Y-%m-%d')})")
                                total_deleted += 1
                                total_size_deleted += file_size
                                camera_deleted += 1
                                camera_size_deleted += file_size

                            except Exception as e:
                                print(f"エラー - {filename}の削除中: {str(e)}")

                    except ValueError as e:
                        print(f"ファイル名の日付解析エラー - {filename}: {str(e)}")

        # カメラごとの統計
        if camera_deleted > 0:
            camera_size_mb = camera_size_deleted / (1024 * 1024)
            print(f"カメラID {camera_num}: {camera_deleted} ファイルを削除 (約 {camera_size_mb:.2f} MB)")

    # 削除されたファイルの合計サイズをMB単位で表示
    if total_deleted > 0:
        total_size_mb = total_size_deleted / (1024 * 1024)
        print(f"\n合計統計:")
        print(f"合計 {total_deleted} ファイルを削除しました (約 {total_size_mb:.2f} MB)")
    else:
        print("\n削除対象のファイルはありませんでした")

if __name__ == "__main__":
    try:
        print("6ヶ月以上経過したMP4ファイルの削除を開始します...")
        delete_old_backup_files()
        print("処理が正常に完了しました。")

    except Exception as e:
        print(f"プログラムの実行中にエラーが発生しました: {str(e)}")
