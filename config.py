"""
設定管理モジュール
共通の設定値やパスを管理します
"""
import os
import logging
import sys
import subprocess
import platform
import shutil
import re

# 基本パス設定
BASE_PATH = os.path.join('D:\\', 'laragon', 'www', 'system', 'cam')
CONFIG_PATH = os.path.join(BASE_PATH, 'cam_config.txt')
TMP_PATH = os.path.join(BASE_PATH, 'tmp')
RECORD_PATH = os.path.join(BASE_PATH, 'record')
BACKUP_PATH = os.path.join(BASE_PATH, 'backup')
LOG_PATH = os.path.join(BASE_PATH, 'streaming.log')
LOG_BACKUP_PATH = os.path.join(BASE_PATH, 'logs')  # ログバックアップディレクトリ

# バージョン情報
VERSION = "1.2.1"  # バージョン番号（メジャー.マイナー.パッチ）

# 録画設定
MAX_RECORDING_HOURS = 1  # 最大録画時間（時間）
MIN_DISK_SPACE_GB = 2    # 最小必要ディスク容量（GB）- 1GBから2GBに増加
MAX_RECORD_FILES = 100   # カメラあたりの最大録画ファイル数

# ストリーミング設定
RETRY_ATTEMPTS = 10       # 再試行回数 (5→10に増加)
RETRY_DELAY = 15         # 再試行遅延（秒）(30→15に削減)
MAX_RETRY_DELAY = 120    # 最大再試行遅延（秒）(120は維持)
STREAM_HEALTH_CHECK_INTERVAL = 15  # ストリーム健全性チェック間隔（秒）

# HLSストリーミング設定
HLS_SEGMENT_TIME = 1.5   # HLSセグメント時間（秒）(2→1.5に短縮)
HLS_LIST_SIZE = 15       # HLSリストサイズ (10→15に増加)
HLS_UPDATE_TIMEOUT = 15  # HLSファイル更新タイムアウト（秒）

# FFmpeg設定
FFMPEG_BUFFER_SIZE = 32768  # FFmpegバッファサイズ (KB)
FFMPEG_THREAD_QUEUE_SIZE = 32768  # FFmpegスレッドキューサイズ

# ログファイル設定
MAX_LOG_SIZE = 10 * 1024 * 1024  # 最大ログファイルサイズ (10MB)
MAX_LOG_BACKUPS = 5             # 保持するログバックアップの数

# ウェブインターフェース設定
WEB_REFRESH_INTERVAL = 300  # ウェブ画面の自動更新間隔（秒）

# システム設定
CHECK_INTERVAL = 5  # プロセス監視間隔（秒）

# FFmpegバージョン情報
FFMPEG_VERSION = None
FFMPEG_SUPPORTS_STIMEOUT = False

# ログローテーション設定
def rotate_log_file():
    """ログファイルのローテーションを行う"""
    if os.path.exists(LOG_PATH):
        try:
            # ログファイルのサイズを確認
            log_size = os.path.getsize(LOG_PATH)
            
            if log_size > MAX_LOG_SIZE:
                # バックアップディレクトリの確認
                if not os.path.exists(LOG_BACKUP_PATH):
                    os.makedirs(LOG_BACKUP_PATH)
                
                # 現在の日時を取得
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                
                # バックアップファイル名
                backup_file = os.path.join(LOG_BACKUP_PATH, f"streaming_{timestamp}.log")
                
                # ファイルをコピー
                shutil.copy2(LOG_PATH, backup_file)
                
                # ログファイルをリセット
                with open(LOG_PATH, 'w') as f:
                    f.write(f"=== Log rotated at {timestamp} ===\n")
                
                print(f"Log file rotated: {LOG_PATH} -> {backup_file}")
                
                # 古いバックアップを削除
                log_files = [os.path.join(LOG_BACKUP_PATH, f) for f in os.listdir(LOG_BACKUP_PATH) 
                          if f.startswith("streaming_") and f.endswith(".log")]
                
                # 作成日時でソート（古い順）
                log_files.sort(key=lambda x: os.path.getctime(x))
                
                # 最大数を超える場合は古いものから削除
                while len(log_files) > MAX_LOG_BACKUPS:
                    old_file = log_files.pop(0)  # 最も古いファイル
                    try:
                        os.remove(old_file)
                        print(f"Deleted old log file: {old_file}")
                    except Exception as e:
                        print(f"Error deleting old log file {old_file}: {e}")
        
        except Exception as e:
            print(f"Error rotating log file: {e}")

# ロギング設定
def setup_logging():
    """ロギングの設定を行う"""
    # ログファイルのローテーション
    rotate_log_file()
    
    # ログフォーマットの設定
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # バックアップディレクトリの確認
    if not os.path.exists(LOG_BACKUP_PATH):
        os.makedirs(LOG_BACKUP_PATH)
    
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        filename=LOG_PATH,
        filemode='a'
    )

    # コンソールにもログを出力
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter(log_format, datefmt=date_format)
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

    logging.info("=== Logging initialized ===")
    logging.info(f"Application Version: {VERSION}")
    logging.info(f"Python Version: {sys.version}")
    logging.info(f"Platform: {platform.platform()}")
    logging.info(f"Base Path: {BASE_PATH}")
    
    # FFmpegのバージョンを確認
    check_ffmpeg()

# 設定ファイルの存在チェック
def check_config_file():
    """設定ファイルの存在チェック"""
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        return False

    # 設定ファイルの内容を確認
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                logging.error(f"設定ファイルが空です: {CONFIG_PATH}")
                return False
                
        # 設定内容を表示
        logging.info(f"設定ファイルの読み込み成功: {CONFIG_PATH}")
        return True
    except Exception as e:
        logging.error(f"設定ファイル読み込みエラー: {e}")
        return False

# FFmpegが利用可能かチェック
def check_ffmpeg():
    """FFmpegの利用可能性をチェック"""
    global FFMPEG_VERSION, FFMPEG_SUPPORTS_STIMEOUT
    
    try:
        # シェルを使用して実行（権限問題回避）
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, encoding='cp932', errors='ignore')
        if result.returncode == 0:
            logging.info("FFmpegが正常に検出されました")
            # FFmpegバージョンを出力
            version_text = result.stdout.splitlines()[0] if result.stdout else "Unknown"
            logging.info(f"FFmpeg version: {version_text}")
            FFMPEG_VERSION = version_text
            
            # バージョンから互換性のあるオプションを確認
            version_match = re.search(r'ffmpeg version ([0-9.]+)', version_text)
            if version_match:
                version_str = version_match.group(1)
                # 特定オプションのサポート状況を設定
                # -stimeoutオプションはバージョンによってサポートされないため
                # 正確なバージョンチェックを行う代わりに、機能テストを実施
                # 最新バージョンではrw_timeoutを使う
                FFMPEG_SUPPORTS_STIMEOUT = False
            
            return True
        else:
            logging.error("FFmpegが見つかりません")
            return False

    except Exception as e:
        logging.error(f"FFmpeg確認エラー: {e}")
        return False
