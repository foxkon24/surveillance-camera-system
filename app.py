"""
監視カメラシステム メインアプリケーション
"""
from flask import Flask, render_template, send_from_directory, request, jsonify
import os
import logging
import sys
import time
import json
from datetime import datetime
import fractions

# 自作モジュールのインポート
import config
import fs_utils
import camera_utils
import streaming
import recording
import ffmpeg_utils

app = Flask(__name__)

@app.route('/system/cam/tmp/<camera_id>/<filename>')
def serve_tmp_files(camera_id, filename):
    """一時ファイル(HLS)を提供"""
    try:
        # パスを正規化
        file_path = os.path.join(config.TMP_PATH, camera_id, filename).replace('/', '\\')
        directory = os.path.dirname(file_path)

        if not os.path.exists(file_path):
            return "File not found", 404

        # ファイルの最終更新時刻を取得
        last_modified = None
        try:
            last_modified = datetime.fromtimestamp(os.path.getmtime(file_path))
            last_modified_str = last_modified.strftime('%a, %d %b %Y %H:%M:%S GMT')
        except:
            last_modified_str = datetime.now().strftime('%a, %d %b %Y %H:%M:%S GMT')

        # コンテンツタイプを決定
        content_type = None
        if filename.endswith('.m3u8'):
            content_type = 'application/vnd.apple.mpegurl'
        elif filename.endswith('.ts'):
            content_type = 'video/mp2t'

        response = send_from_directory(
            directory,
            os.path.basename(file_path),
            as_attachment=False,
            mimetype=content_type)
            
        # キャッシュを完全に無効化（強化）
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, post-check=0, pre-check=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        # 追加のキャッシュ無効化対策
        response.headers['Last-Modified'] = last_modified_str
        response.headers['ETag'] = f'"{hash(str(os.path.getmtime(file_path)))}"'
        
        # CORS設定を追加
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Origin, X-Requested-With, Content-Type, Accept, Range'
        
        return response
    except Exception as e:
        logging.error(f"ファイル提供中にエラー発生: {str(e)}")
        return "Error", 500

@app.route('/system/cam/record/<camera_id>/<filename>')
def serve_record_file(camera_id, filename):
    """録画ファイルを提供"""
    return send_from_directory(os.path.join(config.RECORD_PATH, camera_id), filename)

@app.route('/system/cam/backup/<camera_id>/<filename>')
def serve_backup_file(camera_id, filename):
    """バックアップファイルを提供"""
    return send_from_directory(os.path.join(config.BACKUP_PATH, camera_id), filename)

@app.route('/system/cam/')
def index():
    """メインページ"""
    cameras = camera_utils.reload_config()
    
    # ストリームが開始されているか確認し、開始されていなければ開始する
    for camera in cameras:
        if camera['id'] not in streaming.streaming_processes:
            streaming.get_or_start_streaming(camera)

    # enabledフラグがなければ追加
    for camera in cameras:
        if 'enabled' not in camera:
            camera['enabled'] = 1

    # キャッシュバスターとしてのタイムスタンプを追加
    timestamp = int(time.time())
    
    return render_template('index.html', cameras=cameras, timestamp=timestamp)

@app.route('/system/cam/single')
def index_single():
    """単一カメラ表示ページ"""
    camera_id = request.args.get('id')
    if not camera_id:
        return 'Camera ID not specified', 400

    cameras = camera_utils.reload_config()
    target_camera = next((camera for camera in cameras if camera['id'] == camera_id), None)
    if target_camera is None:
        return 'Camera not found', 404

    # ワーカースレッドを必ず起動
    streaming.start_streaming_workers()
    streaming.get_or_start_streaming(target_camera)

    # HLSファイル（.m3u8）が生成されるまで最大10秒待機
    hls_path = os.path.join(config.TMP_PATH, camera_id, f"{camera_id}.m3u8")
    max_wait = 10
    waited = 0
    while not os.path.exists(hls_path) and waited < max_wait:
        time.sleep(1)
        waited += 1

    # キャッシュバスター用タイムスタンプ
    timestamp = int(time.time())
    # HLSストリームURL（キャッシュバスター付き）
    hls_url = f"/tmp/{camera_id}/{camera_id}.m3u8?ts={timestamp}"

    return render_template(
        'single.html',
        camera=target_camera,
        hls_url=hls_url,
        timestamp=timestamp
    )

@app.route('/system/cam/api/restart_stream/<camera_id>')
def restart_stream(camera_id):
    """カメラストリームを再起動するAPIエンドポイント"""
    try:
        logging.info(f"カメラID {camera_id} のストリーム再起動APIが呼び出されました")
        
        # 存在する有効なカメラIDかチェック
        cameras = camera_utils.reload_config()
        valid_camera = None
        for camera in cameras:
            if camera['id'] == camera_id:
                valid_camera = camera
                break
                
        if not valid_camera:
            logging.warning(f"カメラID {camera_id} が見つかりません")
            return jsonify({"success": False, "message": f"カメラID {camera_id} が見つかりません"}), 404
        
        # 現在のストリーミングプロセスを停止
        if camera_id in streaming.streaming_processes:
            logging.info(f"カメラID {camera_id} の既存ストリームを停止します")
            streaming.stop_streaming(camera_id)
            time.sleep(1)  # 処理完了を待機
        
        # 新しいストリーミングプロセスを開始
        success = streaming.get_or_start_streaming(valid_camera)
        
        if success:
            return jsonify({"success": True, "message": f"カメラID {camera_id} のストリームを再起動しました"}), 200
        else:
            return jsonify({"success": False, "message": f"カメラID {camera_id} のストリーム再起動に失敗しました"}), 500
            
    except Exception as e:
        logging.error(f"ストリーム再起動中にエラーが発生: {str(e)}")
        return jsonify({"success": False, "message": f"エラー: {str(e)}"}), 500

@app.route('/system/cam/restart_all_streams', methods=['POST'])
def restart_all_streams():
    """全カメラのストリームを再起動するAPI"""
    try:
        cameras = camera_utils.reload_config()
        success_count = 0
        failure_count = 0
        
        for camera in cameras:
            if streaming.restart_streaming(camera['id']):
                success_count += 1
            else:
                failure_count += 1
                
        if failure_count == 0:
            return jsonify({"status": "success", "message": f"All {success_count} streams restarted successfully"})
        else:
            return jsonify({"status": "partial", "message": f"{success_count} streams restarted, {failure_count} failed"})
    except Exception as e:
        logging.error(f"Error restarting all streams: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def initialize_app():
    """アプリケーションの初期化"""
    # ロギングの設定
    config.setup_logging()

    # 設定ファイルの確認
    if not config.check_config_file():
        logging.error("設定ファイルが見つかりません。アプリケーションを終了します。")
        sys.exit(1)

    # FFmpegの確認
    if not config.check_ffmpeg():
        logging.error("FFmpegが見つかりません。アプリケーションを終了します。")
        sys.exit(1)
    
    # 古いログファイルの削除
    logging.info("古いログファイルを削除中...")
    log_dir = os.path.join(config.BASE_PATH, 'log')
    if os.path.exists(log_dir):
        try:
            deleted_logs = fs_utils.cleanup_directory(
                log_dir,
                file_pattern='.log',
                max_age_seconds=30 * 24 * 3600,  # 30日より古いログを削除
                max_files=50  # 最大50個のログファイルを保持
            )
            if deleted_logs:
                logging.info(f"{deleted_logs}件の古いログファイルを削除しました")
        except Exception as e:
            logging.error(f"古いログファイル削除中にエラー: {e}")
    
    # 古いFFmpegプロセスを確実に終了
    logging.info("古いFFmpegプロセスを終了しています...")
    try:
        ffmpeg_utils.kill_ffmpeg_processes()
        # プロセスが確実に終了するまで少し待つ
        time.sleep(3)
    except Exception as e:
        logging.error(f"古いFFmpegプロセス終了中にエラー: {e}")
        
    # 必要なディレクトリの作成
    logging.info("必要なディレクトリを準備中...")
    fs_utils.ensure_directory_exists(config.TMP_PATH)
    fs_utils.ensure_directory_exists(config.RECORD_PATH)
    fs_utils.ensure_directory_exists(config.BACKUP_PATH)
    
    # tmp以下のm3u8とtsファイルをクリア
    logging.info("一時ファイルをクリア中...")
    try:
        if os.path.exists(config.TMP_PATH):
            for camera_dir in os.listdir(config.TMP_PATH):
                camera_path = os.path.join(config.TMP_PATH, camera_dir)
                if os.path.isdir(camera_path):
                    for f in os.listdir(camera_path):
                        if f.endswith('.m3u8') or f.endswith('.ts'):
                            try:
                                os.remove(os.path.join(camera_path, f))
                            except Exception as e:
                                logging.error(f"一時ファイル削除エラー: {e}")
    except Exception as e:
        logging.error(f"一時ファイルクリア中にエラー: {e}")
    
    # ストリーミング機能の初期化（自動的にすべてのカメラのストリーミングを開始します）
    logging.info("ストリーミング機能を初期化中...")
    streaming.initialize_streaming()
    
    # ワーカーが起動するのを少し待つ
    logging.info("ストリーミングワーカー起動待機中...")
    time.sleep(2)
    
    # 録画機能の初期化
    logging.info("録画機能を初期化中...")
    recording.initialize_recording()
    
    logging.info("アプリケーションの初期化が完了しました")
    
    return True

if __name__ == '__main__':
    try:
        print("監視カメラシステムを起動中...")
        if not initialize_app():
            print("アプリケーションの初期化に失敗しました。ログを確認してください。")
            sys.exit(1)

        # 環境情報を出力
        print(f"カレントディレクトリ: {os.getcwd()}")
        print(f"ベースパス: {config.BASE_PATH}")
        print(f"設定ファイル: {config.CONFIG_PATH}")
        print(f"設定ファイルが存在: {os.path.exists(config.CONFIG_PATH)}")
        print(f"FFmpegバージョン: {config.FFMPEG_PATH}")

        print("Webサーバーを起動中...")
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        app.run(host='0.0.0.0', port=5000, debug=False)

    except Exception as e:
        logging.error(f"起動エラー: {e}")
        print(f"エラー: {e}")
        input("Enterキーを押して終了してください...")
