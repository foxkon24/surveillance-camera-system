"""
設定管理モジュール
共通の設定値やパスを管理します
"""
import os
import logging
import sys
import subprocess
from datetime import datetime
import logging.handlers

# 基本パス設定
BASE_PATH = os.path.join('D:\\', 'laragon', 'www', 'system', 'cam')
CONFIG_PATH = os.path.join(BASE_PATH, 'cam_config.txt')
TMP_PATH = os.path.join(BASE_PATH, 'tmp')
RECORD_PATH = os.path.join(BASE_PATH, 'record')
BACKUP_PATH = os.path.join(BASE_PATH, 'backup')

# FFmpeg設定
FFMPEG_PATH = 'ffmpeg'  # ffmpegコマンド（PATHに含まれている場合）
# または絶対パスを指定: FFMPEG_PATH = 'C:\\path\\to\\ffmpeg.exe'

# FFmpegバッファ設定
FFMPEG_BUFFER_SIZE = '16384k'  # FFmpegのバッファサイズ（8192kから増加）
FFMPEG_THREAD_QUEUE_SIZE = 2048  # FFmpegのスレッドキューサイズ（1024から増加）

# 実行ファイル名に基づいてログファイル名を設定
def get_log_path():
    """logディレクトリ配下にログファイルを作成する"""
    log_dir = os.path.join(BASE_PATH, 'log')
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')  # 年月日時分秒を含むタイムスタンプ形式
    if os.path.basename(sys.argv[0]) == 'record_app.py':
        return os.path.join(log_dir, f'recording_{timestamp}.log')
    else:
        return os.path.join(log_dir, f'streaming_{timestamp}.log')

LOG_PATH = get_log_path()

# 録画設定
MAX_RECORDING_HOURS = 1  # 最大録画時間（時間）
MIN_DISK_SPACE_GB = 1    # 最小必要ディスク容量（GB）

# ストリーミング設定
RETRY_ATTEMPTS = 3       # 再試行回数
RETRY_DELAY = 10         # 再試行遅延（秒）
MAX_RETRY_DELAY = 60     # 最大再試行遅延（秒）

# リソース制限設定
MAX_CONCURRENT_STREAMS = 10     # 最大同時ストリーミング数
RESOURCE_CHECK_INTERVAL = 30    # リソースチェック間隔（秒）
MAX_CPU_PERCENT = 80            # 最大CPU使用率（%）
MAX_MEM_PERCENT = 80            # 最大メモリ使用率（%）
CLEANUP_INTERVAL = 300           # クリーンアップ間隔（秒）
HLS_SEGMENT_MAX_AGE = 600       # 古いセグメントファイルの最大保持時間（秒）（300秒から600秒に増加）

# HLS設定
HLS_SEGMENT_TIME = 2            # HLSセグメント長（秒）
HLS_LIST_SIZE = 5               # HLSプレイリストサイズ

# HLSストリーミング設定
HLS_SEGMENT_DURATION = 2  # セグメント長（秒）
HLS_PLAYLIST_SIZE = 10    # プレイリストに保持するセグメント数
HLS_BUFFER_SIZE = 5242880 # バッファサイズ（5MB）
STREAM_RESTART_DELAY = 5  # ストリーミング再起動の遅延（秒）
MAX_RESTART_ATTEMPTS = 3  # 最大再起動試行回数

# ネットワーク設定
RTSP_TIMEOUT = 15               # RTSP接続タイムアウト（秒）
HEALTH_CHECK_INTERVAL = 10      # 健全性チェックの間隔（秒）
HLS_UPDATE_TIMEOUT = 15         # HLSファイル更新タイムアウト（秒）

# ロギング設定
def setup_logging():
    """ロギングの設定を行う（1日単位でローテート）"""
    log_dir = os.path.join(BASE_PATH, 'log')
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')  # 年月日時分秒を含むタイムスタンプ形式
    if os.path.basename(sys.argv[0]) == 'record_app.py':
        log_file = os.path.join(log_dir, f'recording_{timestamp}.log')
    else:
        log_file = os.path.join(log_dir, f'streaming_{timestamp}.log')

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers = []  # 既存のハンドラをクリア

    # ファイルへのログ出力ハンドラ
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # コンソールにも出力
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    logger.info(f"Logging initialized - Writing to {log_file}")

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
