"""
設定管理モジュール
共通の設定値やパスを管理します
"""
import os
import logging
import sys
import platform
import socket

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
RETRY_ATTEMPTS = 5       # 再試行回数を増加
RETRY_DELAY = 5          # 再試行遅延（秒）を短縮
MAX_RETRY_DELAY = 30     # 最大再試行遅延（秒）

# ロギング設定
def setup_logging():
    """ロギングの設定を行う"""
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

    logging.info("Logging initialized")
    logging.info(f"Process ID: {os.getpid()}")
    logging.info("============= アプリケーション起動 =============")
    logging.info(f"実行パス: {os.getcwd()}")
    logging.info(f"Pythonバージョン: {sys.version}")
    logging.info(f"OSバージョン: {os.name}")

# 設定ファイルの存在チェック
def check_config_file():
    """設定ファイルの存在チェック"""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"設定ファイルが見つかりません: {CONFIG_PATH}")
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

# サーバーのIPアドレスを取得
def get_server_ip():
    """サーバーのIPアドレスを取得"""
    try:
        # ローカルネットワーク内のIPアドレスを取得
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 外部に接続しようとする（実際には接続しない）
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logging.warning(f"IPアドレス取得エラー: {e}")
        return "localhost"
