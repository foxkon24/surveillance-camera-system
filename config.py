"""
設定管理モジュール
共通の設定値やパスを管理します
"""
import os
import logging
import sys
import shutil
import subprocess
from datetime import datetime

# 基本パス設定
BASE_PATH = os.path.normpath(os.path.join('D:\\', 'laragon', 'www', 'system', 'cam'))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_PATH, 'cam_config.txt'))
TMP_PATH = os.path.normpath(os.path.join(BASE_PATH, 'tmp'))
RECORD_PATH = os.path.normpath(os.path.join(BASE_PATH, 'record'))
BACKUP_PATH = os.path.normpath(os.path.join(BASE_PATH, 'backup'))
LOG_PATH = os.path.normpath(os.path.join(BASE_PATH, 'logs'))
LOG_FILE = os.path.normpath(os.path.join(LOG_PATH, 'streaming.log'))

# 録画設定
MAX_RECORDING_HOURS = 1       # 最大録画時間（時間）
MIN_DISK_SPACE_GB = 1         # 最小必要ディスク容量（GB）
MAX_RECORDINGS_PER_CAMERA = 50 # 各カメラの保存する最大録画ファイル数

# ストリーミング設定
RETRY_ATTEMPTS = 3            # 再試行回数
RETRY_DELAY = 10              # 再試行遅延（秒）
MAX_RETRY_DELAY = 60          # 最大再試行遅延（秒）
RTSP_TIMEOUT = 10             # RTSPタイムアウト（秒）

# ロギング設定
def setup_logging():
    """ロギングの設定を行う（改善版）"""
    try:
        # ログディレクトリが存在するか確認
        if not os.path.exists(LOG_PATH):
            os.makedirs(LOG_PATH)
            
        # 日付ベースのログファイル名
        today = datetime.now().strftime('%Y%m%d')
        log_filename = os.path.join(LOG_PATH, f"streaming_{today}.log")
        
        # ログローテーション - 古いログファイルを管理（最大30日分保持）
        cleanup_old_logs(LOG_PATH, 30)
        
        # ロギングの設定
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            filename=log_filename,
            filemode='a'
        )

        # コンソールにもログを出力
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)

        logging.info("Logging initialized")
        
        # 実行中のPIDをログに記録
        logging.info(f"Process ID: {os.getpid()}")
        
    except Exception as e:
        print(f"Error setting up logging: {e}")
        # 最低限のロギング設定
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            stream=sys.stdout
        )
        logging.error(f"Error setting up logging: {e}")

def cleanup_old_logs(log_dir, max_days):
    """古いログファイルをクリーンアップする"""
    try:
        # ログファイルの一覧を取得
        log_files = []
        for filename in os.listdir(log_dir):
            if filename.startswith("streaming_") and filename.endswith(".log"):
                file_path = os.path.join(log_dir, filename)
                file_time = os.path.getmtime(file_path)
                log_files.append((file_path, file_time))
        
        # 日付でソート（古い順）
        log_files.sort(key=lambda x: x[1])
        
        # 最新の max_days 分を残して古いファイルを削除
        if len(log_files) > max_days:
            for file_path, _ in log_files[:-max_days]:
                try:
                    os.remove(file_path)
                    print(f"Removed old log file: {file_path}")
                except OSError as e:
                    print(f"Error removing old log file {file_path}: {e}")
    except Exception as e:
        print(f"Error cleaning up old logs: {e}")

# 設定ファイルの存在チェック
def check_config_file():
    """設定ファイルの存在チェック"""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        
        # サンプル設定ファイルがあればコピー
        sample_config = os.path.join(os.path.dirname(CONFIG_PATH), "cam_config - sample.txt")
        if os.path.exists(sample_config):
            try:
                shutil.copy2(sample_config, CONFIG_PATH)
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
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            # バージョン情報を抽出
            version_info = result.stdout.splitlines()[0] if result.stdout else "不明なバージョン"
            logging.info(f"FFmpegが正常に検出されました: {version_info}")
            return True
        else:
            logging.error("FFmpegが見つかりません")
            return False

    except Exception as e:
        logging.error(f"FFmpeg確認エラー: {e}")
        return False

# FFprobeが利用可能かチェック
def check_ffprobe():
    """FFprobeの利用可能性をチェック"""
    try:
        result = subprocess.run(['ffprobe', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            # バージョン情報を抽出
            version_info = result.stdout.splitlines()[0] if result.stdout else "不明なバージョン"
            logging.info(f"FFprobeが正常に検出されました: {version_info}")
            return True
        else:
            logging.error("FFprobeが見つかりません")
            return False

    except Exception as e:
        logging.error(f"FFprobe確認エラー: {e}")
        return False
