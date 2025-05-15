"""
録画専用アプリケーション
カメラ録画管理・録画ファイル配信のみを担当
"""
from flask import Flask, render_template, send_from_directory, request, jsonify
import os
import logging
import sys
import time

import config
import fs_utils
import camera_utils
import recording
import ffmpeg_utils

app = Flask(__name__)

@app.route('/system/cam/admin/')
def admin_page():
    """管理者ページ表示"""
    try:
        logging.info(f"管理者ページ表示処理開始")
        # HTMLをシンプルにレンダリング（データはJavaScriptで取得）
        return render_template('admin.html')
    except Exception as e:
        logging.error(f"管理画面表示エラー: {e}", exc_info=True)
        error_html = f"""
        <!DOCTYPE html>
        <html><head><title>エラー</title></head>
        <body>
            <h1>エラーが発生しました</h1>
            <p>{str(e)}</p>
        </body></html>
        """
        return error_html, 500

@app.route('/system/cam/admin_data')
def admin_data():
    """管理者ページ用のデータをJSON形式で返す"""
    try:
        logging.info(f"管理者ページデータ取得処理開始")
        
        # ディスク使用量情報取得
        disk_usage = ""
        try:
            record_size = fs_utils.get_directory_size(config.RECORD_PATH)
            backup_size = fs_utils.get_directory_size(config.BACKUP_PATH)
            disk_usage = f"録画フォルダ: {fs_utils.format_size(record_size)} / バックアップフォルダ: {fs_utils.format_size(backup_size)}"
        except Exception as e:
            logging.error(f"ディスク使用量取得エラー: {e}")
            disk_usage = f"取得エラー: {str(e)}"
        
        # カメラ情報取得
        cameras_data = []
        try:
            cameras = camera_utils.read_config()
            if cameras:
                for camera in cameras:
                    cam_id = camera.get('id', 'unknown')
                    camera_info = {
                        'id': cam_id,
                        'name': camera.get('name', '不明'),
                        'rtsp_url': camera.get('rtsp_url', '不明'),
                        'status': recording.get_recording_status(cam_id)
                    }
                    cameras_data.append(camera_info)
        except Exception as e:
            logging.error(f"カメラ情報取得エラー: {e}")
        
        # JSON形式でデータを返す
        return jsonify({
            'disk_usage': disk_usage,
            'cameras': cameras_data
        })
    except Exception as e:
        logging.error(f"管理者ページデータ取得エラー: {e}", exc_info=True)
        return jsonify({
            'error': str(e)
        }), 500

@app.route('/system/cam/record/')
def list_recordings():
    """録画リスト表示（簡易版）"""
    try:
        # より詳細なログ
        logging.info(f"録画リスト表示処理開始")
        logging.info(f"RECORD_PATH: {config.RECORD_PATH}")
        
        # シンプルなHTML生成（テンプレートを使わない）
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>録画データ</title>
        </head>
        <body>
            <h1>録画データ（簡易表示）</h1>
            <p><a href="/system/cam/admin/">管理画面に戻る</a></p>
        """
        
        # ディレクトリ存在チェック
        if not os.path.exists(config.RECORD_PATH):
            logging.error(f"録画フォルダが存在しません: {config.RECORD_PATH}")
            html += f"<p>エラー: 録画フォルダが存在しません: {config.RECORD_PATH}</p>"
            html += "</body></html>"
            return html
        
        html += "<ul>"
        
        # 録画ファイルの一覧
        try:
            camera_dirs = os.listdir(config.RECORD_PATH)
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.RECORD_PATH, camera_id)
                if os.path.isdir(camera_path):
                    try:
                        mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                        mp4_files.sort(reverse=True)
                        
                        html += f"<li>カメラID {camera_id}: {len(mp4_files)}ファイル<ul>"
                        for file in mp4_files[:5]:  # 最初の5件のみ表示
                            html += f'<li><a href="/system/cam/record/{camera_id}/{file}">{file}</a></li>'
                        if len(mp4_files) > 5:
                            html += f"<li>...ほか {len(mp4_files)-5} ファイル</li>"
                        html += "</ul></li>"
                    except Exception as dir_e:
                        logging.error(f"カメラディレクトリ処理エラー({camera_id}): {dir_e}")
                        html += f"<li>カメラID {camera_id}: エラー - {str(dir_e)}</li>"
        except Exception as list_e:
            logging.error(f"カメラディレクトリ一覧取得エラー: {list_e}")
            html += f"<li>エラー: カメラディレクトリ一覧取得エラー - {str(list_e)}</li>"
        
        html += """
            </ul>
        </body>
        </html>
        """
        return html
    except Exception as e:
        logging.error(f"録画リスト表示エラー: {e}", exc_info=True)
        error_html = f"""
        <!DOCTYPE html>
        <html><head><title>エラー</title></head>
        <body>
            <h1>エラーが発生しました</h1>
            <p>{str(e)}</p>
            <p><a href="/system/cam/admin/">管理画面に戻る</a></p>
        </body></html>
        """
        return error_html, 500

@app.route('/system/cam/backup/')
def backup_recordings():
    """バックアップ録画一覧表示（簡易版）"""
    try:
        # より詳細なログ
        logging.info(f"バックアップ録画一覧表示処理開始")
        logging.info(f"BACKUP_PATH: {config.BACKUP_PATH}")
        
        # シンプルなHTML生成（テンプレートを使わない）
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>バックアップ録画一覧</title>
        </head>
        <body>
            <h1>バックアップ録画一覧（簡易表示）</h1>
            <p><a href="/system/cam/admin/">管理画面に戻る</a></p>
        """
        
        # ディレクトリ存在チェック
        if not os.path.exists(config.BACKUP_PATH):
            logging.error(f"バックアップフォルダが存在しません: {config.BACKUP_PATH}")
            html += f"<p>エラー: バックアップフォルダが存在しません: {config.BACKUP_PATH}</p>"
            html += "</body></html>"
            return html
            
        html += "<ul>"
        
        # バックアップファイルの一覧
        try:
            camera_dirs = os.listdir(config.BACKUP_PATH)
            for camera_id in camera_dirs:
                camera_path = os.path.join(config.BACKUP_PATH, camera_id)
                if os.path.isdir(camera_path):
                    try:
                        mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                        mp4_files.sort(reverse=True)
                        
                        html += f"<li>カメラID {camera_id}: {len(mp4_files)}ファイル<ul>"
                        for file in mp4_files[:5]:  # 最初の5件のみ表示
                            html += f'<li><a href="/system/cam/backup/{camera_id}/{file}">{file}</a></li>'
                        if len(mp4_files) > 5:
                            html += f"<li>...ほか {len(mp4_files)-5} ファイル</li>"
                        html += "</ul></li>"
                    except Exception as dir_e:
                        logging.error(f"バックアップディレクトリ処理エラー({camera_id}): {dir_e}")
                        html += f"<li>カメラID {camera_id}: エラー - {str(dir_e)}</li>"
        except Exception as list_e:
            logging.error(f"バックアップディレクトリ一覧取得エラー: {list_e}")
            html += f"<li>エラー: バックアップディレクトリ一覧取得エラー - {str(list_e)}</li>"
        
        html += """
            </ul>
        </body>
        </html>
        """
        return html
    except Exception as e:
        logging.error(f"バックアップ録画一覧表示エラー: {e}", exc_info=True)
        error_html = f"""
        <!DOCTYPE html>
        <html><head><title>エラー</title></head>
        <body>
            <h1>エラーが発生しました</h1>
            <p>{str(e)}</p>
            <p><a href="/system/cam/admin/">管理画面に戻る</a></p>
        </body></html>
        """
        return error_html, 500

@app.route('/system/cam/record/<camera_id>/<filename>')
def serve_record_file(camera_id, filename):
    """録画ファイルを提供"""
    return send_from_directory(os.path.join(config.RECORD_PATH, camera_id), filename)

@app.route('/system/cam/backup/<camera_id>/<filename>')
def serve_backup_file(camera_id, filename):
    """バックアップファイルを提供"""
    return send_from_directory(os.path.join(config.BACKUP_PATH, camera_id), filename)

@app.route('/start_recording', methods=['POST'])
def start_recording_route():
    """特定カメラの録画開始API"""
    data = request.json
    camera_id = data['camera_id']
    rtsp_url = data['rtsp_url']
    try:
        recording.start_recording(camera_id, rtsp_url)
        return jsonify({"status": "recording started"})
    except Exception as e:
        logging.error(f"Failed to start recording: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stop_recording', methods=['POST'])
def stop_recording_route():
    """特定カメラの録画停止API"""
    data = request.json
    camera_id = data['camera_id']
    try:
        recording.stop_recording(camera_id)
        return jsonify({"status": "recording stopped"})
    except Exception as e:
        logging.error(f"Failed to stop recording: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/start_all_recordings', methods=['POST'])
def start_all_recordings_handler():
    """
    全カメラの録画を開始するエンドポイント
    """
    try:
        # 既存の録画を確実に停止してクリーンアップ
        try:
            # まず全ての録画を停止
            recording.stop_all_recordings()
            # 少し待機して確実にプロセスが終了するようにする
            time.sleep(3)
            
            # 録画ディレクトリの小さなファイルをクリーンアップ
            if hasattr(fs_utils, 'clean_small_recordings'):
                fs_utils.clean_small_recordings(config.RECORD_PATH, min_size_kb=1024)
            else:
                # 代替のクリーンアップ処理
                cleanup_small_files(config.RECORD_PATH, min_size_kb=1024)
                
            logging.info("録画ディレクトリのクリーンアップが完了しました")
        except Exception as cleanup_err:
            logging.warning(f"クリーンアップ処理でエラーが発生しましたが、録画は継続します: {cleanup_err}")
        
        # 全カメラの録画を開始
        result = recording.start_all_recordings()
        
        # 処理完了後、フロントエンドのボタン状態更新のために少し遅延
        time.sleep(1)
        
        if result:
            # 成功した場合は通知
            return jsonify({'status': '全カメラの録画を開始しました'})
        else:
            # 一部失敗した場合
            return jsonify({'status': '一部のカメラの録画開始に失敗しました。詳細はログを確認してください。'}), 500
    except Exception as e:
        error_msg = f"全カメラ録画開始エラー: {str(e)}"
        logging.error(error_msg)
        logging.exception("詳細なエラー情報:")
        return jsonify({'status': error_msg}), 500

@app.route('/stop_all_recordings', methods=['POST'])
def stop_all_recordings_handler():
    """
    全カメラの録画を停止するエンドポイント
    """
    try:
        # 全カメラの録画を停止
        result = recording.stop_all_recordings()
        
        # 録画終了後にファイルを整理
        try:
            # 既存の関数があれば使用
            if hasattr(fs_utils, 'clean_small_recordings'):
                fs_utils.clean_small_recordings(config.RECORD_PATH, min_size_kb=1024)
            else:
                # 代替のクリーンアップ処理
                cleanup_small_files(config.RECORD_PATH, min_size_kb=1024)
                
            logging.info("録画終了後のファイルクリーンアップが完了しました")
        except Exception as cleanup_err:
            logging.warning(f"クリーンアップ処理でエラーが発生しました: {cleanup_err}")
        
        # 処理完了後、フロントエンドのボタン状態更新のために少し遅延
        time.sleep(1)
            
        if result:
            return jsonify({'status': '全カメラの録画を停止しました'})
        else:
            return jsonify({'status': '一部のカメラの録画停止に失敗しました'}), 500
    except Exception as e:
        error_msg = f"全カメラ録画停止エラー: {str(e)}"
        logging.error(error_msg)
        return jsonify({'status': error_msg}), 500

# 小さいファイルをクリーンアップする内部関数
def cleanup_small_files(base_path, min_size_kb=1024):
    """
    指定サイズより小さいファイルを削除する内部関数
    
    Args:
        base_path (str): クリーンアップするディレクトリ
        min_size_kb (int): 最小ファイルサイズ（KB）
    """
    min_size = min_size_kb * 1024  # KB → バイト
    deleted_count = 0
    
    try:
        logging.info(f"小さいファイルのクリーンアップを開始します: {base_path}")
        
        # ベースディレクトリが存在するか確認
        if not os.path.exists(base_path):
            logging.warning(f"ディレクトリが存在しません: {base_path}")
            return 0
            
        # カメラIDごとのディレクトリを処理
        for camera_id in os.listdir(base_path):
            camera_dir = os.path.join(base_path, camera_id)
            if not os.path.isdir(camera_dir):
                continue
                
            # ディレクトリ内のMP4ファイルを処理
            for filename in os.listdir(camera_dir):
                if not filename.endswith('.mp4'):
                    continue
                    
                file_path = os.path.join(camera_dir, filename)
                if not os.path.isfile(file_path):
                    continue
                    
                # ファイルサイズを確認
                try:
                    file_size = os.path.getsize(file_path)
                    if file_size < min_size:
                        try:
                            os.remove(file_path)
                            deleted_count += 1
                            logging.info(f"小さいファイルを削除しました: {file_path} ({file_size/1024:.1f} KB)")
                        except Exception as e:
                            logging.error(f"ファイル削除エラー: {file_path} - {e}")
                except Exception as size_err:
                    logging.error(f"ファイルサイズ取得エラー: {file_path} - {size_err}")
                    
        logging.info(f"クリーンアップ完了: {deleted_count}ファイルを削除しました")
        return deleted_count
        
    except Exception as e:
        logging.error(f"クリーンアップ処理でエラーが発生しました: {e}")
        return 0

@app.route('/system/cam/test_backup')
def test_backup():
    """バックアップ一覧のテスト（単純なテキスト返却）"""
    try:
        camera_dirs = os.listdir(config.BACKUP_PATH)
        result = "バックアップフォルダの内容:\n"
        for camera_id in camera_dirs:
            camera_path = os.path.join(config.BACKUP_PATH, camera_id)
            if os.path.isdir(camera_path):
                mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                result += f"カメラID {camera_id}: {len(mp4_files)}ファイル\n"
        return result
    except Exception as e:
        logging.error(f"テストエンドポイントエラー: {e}")
        return f"エラー: {str(e)}", 500

@app.route('/system/cam/test_record')
def test_record():
    """録画一覧のテスト（単純なテキスト返却）"""
    try:
        camera_dirs = os.listdir(config.RECORD_PATH)
        result = "録画フォルダの内容:\n"
        for camera_id in camera_dirs:
            camera_path = os.path.join(config.RECORD_PATH, camera_id)
            if os.path.isdir(camera_path):
                mp4_files = [f for f in os.listdir(camera_path) if f.endswith('.mp4')]
                result += f"カメラID {camera_id}: {len(mp4_files)}ファイル\n"
        return result
    except Exception as e:
        logging.error(f"テストエンドポイントエラー: {e}")
        return f"エラー: {str(e)}", 500

def initialize_record_app():
    """録画アプリ初期化"""
    try:
        config.setup_logging()
        logging.info("============= 録画アプリ起動 =============")
        for directory in [config.BASE_PATH, config.RECORD_PATH, config.BACKUP_PATH]:
            fs_utils.ensure_directory_exists(directory)
        if not config.check_config_file():
            logging.error("設定ファイルが見つかりません")
            return False
        cameras = camera_utils.read_config()
        if not cameras:
            logging.warning("有効なカメラ設定が見つかりません")
        recording.initialize_recording()
        if not config.check_ffmpeg():
            logging.error("FFmpegが見つかりません")
            return False
        return True
    except Exception as e:
        logging.error(f"初期化エラー: {e}")
        return False

if __name__ == '__main__':
    try:
        if not initialize_record_app():
            print("録画アプリの初期化に失敗しました。ログを確認してください。")
            sys.exit(1)
        print(f"Current working directory: {os.getcwd()}")
        print(f"Base path: {config.BASE_PATH}")
        print(f"Config file path: {config.CONFIG_PATH}")
        print(f"Config file exists: {os.path.exists(config.CONFIG_PATH)}")
        app.run(host='0.0.0.0', port=5100, debug=True)
    except Exception as e:
        logging.error(f"Startup error: {e}")
        print(f"Error: {e}")
        input("Press Enter to exit...") 