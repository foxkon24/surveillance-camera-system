import os
import glob

def delete_files():
    # ベースとなるtmpディレクトリのパス
    base_path = r"D:\xampp\htdocs\system\cam\tmp"

    try:
        # tmpディレクトリ内の全てのサブディレクトリを取得
        camera_dirs = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]

        for camera_dir in camera_dirs:
            camera_path = os.path.join(base_path, camera_dir)

            # 各ファイルタイプに対する削除処理
            file_patterns = [
                "*.log",
                "*.m3u8",
                "*.ts"
            ]

            for pattern in file_patterns:
                files = glob.glob(os.path.join(camera_path, pattern))
                for file_path in files:
                    try:
                        os.remove(file_path)
                        print(f"削除成功: {file_path}")
                    except Exception as e:
                        print(f"削除失敗: {file_path} - エラー: {str(e)}")

        print("ファイル削除処理が完了しました。")

    except Exception as e:
        print(f"エラーが発生しました: {str(e)}")

if __name__ == "__main__":
    # 確認メッセージを表示
    #print("tmpフォルダー内のlog、m3u8、tsファイルを削除します。")
    #confirmation = input("続行しますか？ (y/n): ")

    #if confirmation.lower() == 'y':
    #    delete_files()
    #else:
    #    print("処理を中止しました。")
    delete_files()

