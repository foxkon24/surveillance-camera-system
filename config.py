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
RETRY_ATTEMPTS = 5       # 再試行回数（増加）
RETRY_DELAY = 10         # 再試行遅延（秒）
MAX_RETRY_DELAY = 120    # 最大再試行遅延（秒）（増加）

# リソース制限設定
MAX_CONCURRENT_STREAMS = 50     # 同時ストリーミング最大数
RESOURCE_CHECK_INTERVAL = 60    # リソースチェック間隔（秒）
MAX_CPU_PERCENT = 85            # 最大CPU使用率（%）（少し緩和）
MAX_MEM_PERCENT = 85            # 最大メモリ使用率（%）（少し緩和）
CLEANUP_INTERVAL = 300          # 古いセグメントファイル削除間隔（秒）
HLS_SEGMENT_MAX_AGE = 600       # 古いセグメントファイルの最大保持時間（秒）

# HLS設定
HLS_SEGMENT_TIME = 2            # HLSセグメント長（秒）
HLS_LIST_SIZE = 5               # HLSプレイリストサイズ

# ネットワーク設定
RTSP_TIMEOUT = 20               # RTSP接続タイムアウト（秒）（増加）
HEALTH_CHECK_INTERVAL = 15      # ヘルスチェック間隔（秒）
HLS_UPDATE_TIMEOUT = 25         # HLSファイル更新タイムアウト（秒）（増加）

# FFmpeg設定
FFMPEG_THREAD_QUEUE_SIZE = 8192   # スレッドキューサイズを減らす
FFMPEG_BUFFER_SIZE = "20480k"     # バッファサイズを減らす
FFMPEG_MAX_DELAY = "500000"       # 最大遅延
FFMPEG_SOCKET_TIMEOUT = "20000000"  # ソケットタイムアウト（マイクロ秒）
FFMPEG_VIDEO_BITRATE = "1500k"    # ビデオビットレートを下げる
FFMPEG_MAX_BITRATE = "2000k"      # 最大ビットレートを下げる
FFMPEG_CRF = "28"                 # 画質設定を軽量化（値が大きいほど低画質）
FFMPEG_PRESET = "ultrafast"       # 最速のプリセット
FFMPEG_RECORD_PRESET = "ultrafast"  # 録画用も最速に
FFMPEG_GOP_SIZE = "30"            # GOPサイズ（フレーム数）

# プロセス管理設定
PROCESS_TERMINATION_TIMEOUT = 10  # プロセス終了タイムアウト（秒）
MAX_ERROR_COUNT = 5               # エラー発生最大回数（これを超えると一時停止）
ERROR_BACKOFF_TIME = 300          # エラー後のバックオフ時間（秒）

# ロギング設定
def setup_logging():
    """ロギングの設定を行う"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=LOG_PATH,
        filemode='a'
    )

    # ログファイルのサイズを確認し、大きすぎる場合はローテーション
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 10 * 1024 * 1024:  # 10MB以上
            # バックアップファイル名を作成
            backup_log = LOG_PATH + '.bak'
            # 既存のバックアップを削除
            if os.path.exists(backup_log):
                os.remove(backup_log)
            # 現在のログファイルをバックアップし、新しいファイルを作成
            os.rename(LOG_PATH, backup_log)
            logging.info("Rotated log file due to size")
    except Exception as e:
        # エラーが発生しても処理を続行
        pass

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
            version_line = result.stdout.splitlines()[0] if result.stdout else "Unknown"
            logging.info(f"FFmpeg version: {version_line}")
            return True
        else:
            logging.error("FFmpegが見つかりません")
            return False

    except Exception as e:
        logging.error(f"FFmpeg確認エラー: {e}")
        return False

# システムのリソース情報を取得
def get_system_info():
    """システムの基本情報を取得してログに出力"""
    try:
        import platform
        import psutil
        
        # 基本システム情報
        system_info = {
            "OS": f"{platform.system()} {platform.version()}",
            "Python": sys.version.split()[0],
            "CPU Cores": psutil.cpu_count(logical=True),
            "Physical Memory": f"{psutil.virtual_memory().total / (1024**3):.2f} GB",
            "Disk Space": f"{psutil.disk_usage('/').total / (1024**3):.2f} GB"
        }
        
        logging.info("===== System Information =====")
        for key, value in system_info.items():
            logging.info(f"{key}: {value}")
        logging.info("=============================")
        
    except Exception as e:
        logging.error(f"システム情報取得エラー: {e}")
