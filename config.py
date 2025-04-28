"""
設定管理モジュール
共通の設定値やパスを管理します
"""
import os
import logging
import sys
import subprocess

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
RETRY_ATTEMPTS = 3       # 再試行回数
RETRY_DELAY = 10         # 再試行遅延（秒）
MAX_RETRY_DELAY = 60     # 最大再試行遅延（秒）

# リソース制限設定
MAX_CONCURRENT_STREAMS = 50     # 同時ストリーミング最大数
RESOURCE_CHECK_INTERVAL = 60    # リソースチェック間隔（秒）
MAX_CPU_PERCENT = 80            # 最大CPU使用率（%）
MAX_MEM_PERCENT = 80            # 最大メモリ使用率（%）
CLEANUP_INTERVAL = 300          # 古いセグメントファイル削除間隔（秒）
HLS_SEGMENT_MAX_AGE = 600       # 古いセグメントファイルの最大保持時間（秒）

# HLS設定
HLS_SEGMENT_TIME = 2            # HLSセグメント長（秒）
HLS_LIST_SIZE = 5               # HLSプレイリストサイズ

# ネットワーク設定
RTSP_TIMEOUT = 15               # RTSP接続タイムアウト（秒）
HEALTH_CHECK_INTERVAL = 15      # ヘルスチェック間隔（秒）
HLS_UPDATE_TIMEOUT = 20         # HLSファイル更新タイムアウト（秒）

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
        # シェルを使用して実行（権限問題回避）
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, shell=True)
        if result.returncode == 0:
            logging.info("FFmpegが正常に検出されました")
            # FFmpegバージョンを出力
            logging.info(f"FFmpeg version: {result.stdout.splitlines()[0]}")
            return True
        else:
            logging.error("FFmpegが見つかりません")
            return False

    except Exception as e:
        logging.error(f"FFmpeg確認エラー: {e}")
        return False
