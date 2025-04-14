"""
設定管理モジュール
共通の設定値やパスを管理します
"""
import os
import logging
import sys
import time

# 基本パス設定
BASE_PATH = os.path.join('D:\\', 'laragon', 'www', 'system', 'cam')
CONFIG_PATH = os.path.join(BASE_PATH, 'cam_config.txt')
TMP_PATH = os.path.join(BASE_PATH, 'tmp')
RECORD_PATH = os.path.join(BASE_PATH, 'record')
BACKUP_PATH = os.path.join(BASE_PATH, 'backup')
LOG_PATH = os.path.join(BASE_PATH, 'streaming.log')

# 録画設定
MAX_RECORDING_HOURS = 1  # 最大録画時間（時間）
MIN_DISK_SPACE_GB = 1    # 最小必要ディスク容量（GB）

# ストリーミング設定
RETRY_ATTEMPTS = 5       # 再試行回数
RETRY_DELAY = 5          # 再試行遅延（秒）
MAX_RETRY_DELAY = 60     # 最大再試行遅延（秒）

# ロギング設定
def setup_logging():
    """ロギングの設定を行う"""
    # ログディレクトリが存在しない場合は作成
    log_dir = os.path.dirname(LOG_PATH)
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except Exception as e:
            print(f"Error creating log directory: {e}")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=LOG_PATH,
        filemode='a'
    )

    # コンソールにもログを出力
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

    # アプリケーション識別情報をログに記録
    logging.info(f"Process ID: {os.getpid()}")

    logging.info("Logging initialized")

# 設定ファイルの存在チェック
def check_config_file():
    """設定ファイルの存在チェック"""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        
        # サンプル設定ファイルをコピー（存在する場合）
        sample_path = os.path.join(os.path.dirname(CONFIG_PATH), 'cam_config - sample.txt')
        if os.path.exists(sample_path):
            try:
                import shutil
                shutil.copy(sample_path, CONFIG_PATH)
                logging.info(f"サンプル設定ファイルをコピーしました: {CONFIG_PATH}")
                return True
            except Exception as e:
                logging.error(f"サンプル設定ファイルのコピーに失敗しました: {e}")
        
        return False

    return True

# FFmpegが利用可能かチェック
def check_ffmpeg():
    """FFmpegの利用可能性をチェック"""
    try:
        import subprocess

        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            logging.info(f"FFmpegが正常に検出されました: {result.stdout.split('\\n')[0]}")
            return True
        else:
            logging.error("FFmpegが見つかりません")
            return False

    except Exception as e:
        logging.error(f"FFmpeg確認エラー: {e}")
        return False
