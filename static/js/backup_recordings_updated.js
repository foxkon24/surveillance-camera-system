// backup_recordings_updated.js - Jinja2構文を使わないクライアントサイド実装
document.addEventListener('DOMContentLoaded', function() {
    // バックアップ録画データを取得
    fetchBackupRecordings();
    
    // 検索機能
    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.addEventListener('input', filterRecordings);
    }
    
    // カメラフィルター
    const cameraFilter = document.getElementById('camera-filter');
    if (cameraFilter) {
        cameraFilter.addEventListener('change', filterRecordings);
    }
    
    // 更新ボタン
    const refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function() {
            fetchBackupRecordings();
        });
    }
    
    // モーダルの閉じるボタン
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('close-modal')) {
            const videoModal = document.getElementById('video-player-modal');
            if (videoModal) {
                videoModal.style.display = 'none';
                const videoPlayer = document.getElementById('video-player');
                if (videoPlayer) {
                    videoPlayer.pause();
                    videoPlayer.src = '';
                }
            }
        }
    });
    
    // モーダル外のクリックで閉じる
    window.addEventListener('click', function(e) {
        const videoModal = document.getElementById('video-player-modal');
        if (videoModal && e.target === videoModal) {
            videoModal.style.display = 'none';
            const videoPlayer = document.getElementById('video-player');
            if (videoPlayer) {
                videoPlayer.pause();
                videoPlayer.src = '';
            }
        }
    });
    
    // ESCキーでモーダルを閉じる
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const videoModal = document.getElementById('video-player-modal');
            if (videoModal && videoModal.style.display === 'block') {
                videoModal.style.display = 'none';
                const videoPlayer = document.getElementById('video-player');
                if (videoPlayer) {
                    videoPlayer.pause();
                    videoPlayer.src = '';
                }
            }
        }
    });
});

// ビデオプレーヤーモーダルを作成
function createVideoPlayerModal() {
    // 既存のモーダルをチェック
    let videoModal = document.getElementById('video-player-modal');
    
    if (!videoModal) {
        videoModal = document.createElement('div');
        videoModal.id = 'video-player-modal';
        videoModal.className = 'modal';
        
        const modalContent = document.createElement('div');
        modalContent.className = 'modal-content';
        
        const closeButton = document.createElement('span');
        closeButton.className = 'close-modal';
        closeButton.innerHTML = '&times;';
        closeButton.title = '閉じる';
        
        const videoTitle = document.createElement('h3');
        videoTitle.id = 'video-title';
        videoTitle.className = 'video-title';
        
        const videoPlayer = document.createElement('video');
        videoPlayer.id = 'video-player';
        videoPlayer.className = 'video-player';
        videoPlayer.controls = true;
        videoPlayer.autoplay = true;
        
        modalContent.appendChild(closeButton);
        modalContent.appendChild(videoTitle);
        modalContent.appendChild(videoPlayer);
        videoModal.appendChild(modalContent);
        
        // スタイルを追加
        const style = document.createElement('style');
        style.textContent = `
            .modal {
                display: none;
                position: fixed;
                z-index: 1000;
                left: 0;
                top: 0;
                width: 100%;
                height: 100%;
                overflow: auto;
                background-color: rgba(0,0,0,0.7);
            }
            .modal-content {
                background-color: #fefefe;
                margin: 5% auto;
                padding: 20px;
                border: 1px solid #888;
                width: 80%;
                max-width: 1200px;
                border-radius: 5px;
                position: relative;
            }
            .close-modal {
                color: #aaa;
                float: right;
                font-size: 28px;
                font-weight: bold;
                cursor: pointer;
            }
            .close-modal:hover {
                color: black;
            }
            .video-title {
                margin-top: 0;
                margin-bottom: 15px;
                padding-bottom: 10px;
                border-bottom: 1px solid #eee;
                color: #333;
            }
            .video-player {
                width: 100%;
                max-height: 70vh;
                background-color: #000;
            }
        `;
        
        document.head.appendChild(style);
        document.body.appendChild(videoModal);
    }
    
    return videoModal;
}

// 録画ファイルを再生する関数
function playRecordingFile(url, filename, cameraName) {
    // ビデオプレーヤーモーダルを作成または取得
    const videoModal = createVideoPlayerModal();
    const videoPlayer = document.getElementById('video-player');
    const videoTitle = document.getElementById('video-title');
    
    // ビデオプレーヤーを設定
    videoPlayer.src = url;
    videoTitle.textContent = (cameraName ? cameraName + ' - ' : '') + filename;
    
    // モーダルを表示
    videoModal.style.display = 'block';
    
    // 再生エラー処理
    videoPlayer.onerror = function() {
        alert('ビデオの再生中にエラーが発生しました。');
        videoModal.style.display = 'none';
    };
}

// バックアップ録画データを取得する関数
function fetchBackupRecordings() {
    fetch('/api/backup_recordings')
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.json();
        })
        .then(data => {
            renderBackupRecordings(data.recordings, data.camera_names);
        })
        .catch(error => {
            console.error('Error fetching backup recordings:', error);
            document.getElementById('no-recordings-message').textContent = 
                'エラー: バックアップファイルの取得に失敗しました。';
        });
}

// バックアップ録画データを描画する関数
function renderBackupRecordings(recordings, camera_names) {
    const container = document.getElementById('recordings-container');
    const noRecordingsMessage = document.getElementById('no-recordings-message');
    const cameraFilter = document.getElementById('camera-filter');
    
    // コンテナをクリア
    container.innerHTML = '';
    
    // カメラフィルターのオプションをクリア（「すべてのカメラ」以外）
    while (cameraFilter.options.length > 1) {
        cameraFilter.remove(1);
    }
    
    // 録画データがない場合
    if (Object.keys(recordings).length === 0) {
        noRecordingsMessage.textContent = 'バックアップフォルダにカメラの録画ファイルが見つかりません。';
        container.appendChild(noRecordingsMessage);
        return;
    }
    
    // 録画データがある場合は非表示に
    noRecordingsMessage.style.display = 'none';
    
    // 各カメラのバックアップ録画データを処理
    Object.keys(recordings).forEach(camera_id => {
        const camera = recordings[camera_id];
        const files = camera.files;
        const cameraName = camera_names[camera_id] || 'カメラ ' + camera_id;
        
        // カメラフィルターにオプションを追加
        const option = document.createElement('option');
        option.value = camera_id;
        option.textContent = cameraName;
        cameraFilter.appendChild(option);
        
        // カメラセクションを作成 - バックアップ用の縦方向リスト
        const cameraSection = document.createElement('div');
        cameraSection.className = 'camera-section';
        cameraSection.setAttribute('data-camera-id', camera_id);
        
        // カメラヘッダーを作成
        const cameraHeader = document.createElement('div');
        cameraHeader.className = 'camera-header';
        
        const cameraTitle = document.createElement('h2');
        cameraTitle.textContent = cameraName;
        cameraHeader.appendChild(cameraTitle);
        
        const fileCountDiv = document.createElement('div');
        fileCountDiv.className = 'file-count';
        fileCountDiv.textContent = files.length + 'ファイル';
        cameraHeader.appendChild(fileCountDiv);
        
        cameraSection.appendChild(cameraHeader);
        
        // ファイルリストを作成
        if (files.length > 0) {
            const table = document.createElement('table');
            table.className = 'recordings-table';
            
            // テーブルヘッダー
            const thead = document.createElement('thead');
            thead.innerHTML = `
                <tr>
                    <th>ファイル名</th>
                    <th>録画日時</th>
                    <th>サイズ</th>
                    <th>状態</th>
                    <th>アクション</th>
                </tr>
            `;
            table.appendChild(thead);
            
            // テーブルボディ
            const tbody = document.createElement('tbody');
            files.forEach(file => {
                const tr = document.createElement('tr');
                tr.className = 'recording-item';
                tr.setAttribute('data-filename', file.filename);
                
                // ファイル名
                const filenameTd = document.createElement('td');
                filenameTd.className = 'filename-cell';
                filenameTd.textContent = file.filename;
                tr.appendChild(filenameTd);
                
                // 録画日時
                const dateTd = document.createElement('td');
                dateTd.className = 'date-cell';
                dateTd.textContent = file.date;
                tr.appendChild(dateTd);
                
                // ファイルサイズ
                const sizeTd = document.createElement('td');
                sizeTd.className = 'size-cell';
                sizeTd.textContent = (file.size / (1024 * 1024)).toFixed(1) + ' MB';
                tr.appendChild(sizeTd);
                
                // ファイル状態
                const statusTd = document.createElement('td');
                statusTd.className = 'status-cell file-status';
                statusTd.setAttribute('data-path', `/system/cam/backup/${camera_id}/${file.filename}`);
                statusTd.textContent = '確認中...';
                
                // ファイル状態をチェック
                fetch(statusTd.getAttribute('data-path'), { method: 'HEAD' })
                    .then(response => {
                        if (response.ok) {
                            const size = response.headers.get('Content-Length');
                            if (size && parseInt(size) > 1024 * 10) {
                                statusTd.textContent = '正常';
                                statusTd.classList.add('status-ok');
                            } else {
                                statusTd.textContent = '小さいファイル';
                                statusTd.classList.add('status-warning');
                                tr.classList.add('warning-row');
                            }
                        } else {
                            statusTd.textContent = 'エラー';
                            statusTd.classList.add('status-error');
                            tr.classList.add('error-row');
                        }
                    })
                    .catch(error => {
                        statusTd.textContent = 'エラー';
                        statusTd.classList.add('status-error');
                        tr.classList.add('error-row');
                    });
                
                tr.appendChild(statusTd);
                
                // アクション
                const actionTd = document.createElement('td');
                actionTd.className = 'action-cell';
                
                // 新しいウィンドウで再生するボタン
                const playBtn = document.createElement('a');
                playBtn.href = `/system/cam/backup/${camera_id}/${file.filename}`;
                playBtn.className = 'play-btn';
                playBtn.target = '_blank';
                playBtn.title = '新しいウィンドウで再生';
                playBtn.textContent = '新規ウィンドウ';
                actionTd.appendChild(playBtn);
                
                // インラインで再生するボタン
                const inlinePlayBtn = document.createElement('button');
                inlinePlayBtn.className = 'play-btn inline-play-btn';
                inlinePlayBtn.title = 'この画面で再生';
                inlinePlayBtn.textContent = '再生';
                inlinePlayBtn.addEventListener('click', function() {
                    playRecordingFile(`/system/cam/backup/${camera_id}/${file.filename}`, file.filename, cameraName);
                });
                actionTd.appendChild(inlinePlayBtn);
                
                // ダウンロードボタン
                const downloadBtn = document.createElement('button');
                downloadBtn.className = 'download-btn';
                downloadBtn.setAttribute('data-path', `/system/cam/backup/${camera_id}/${file.filename}`);
                downloadBtn.title = 'ダウンロード';
                downloadBtn.textContent = '保存';
                downloadBtn.addEventListener('click', function() {
                    const filePath = this.getAttribute('data-path');
                    if (filePath) {
                        const fileName = filePath.split('/').pop();
                        const downloadLink = document.createElement('a');
                        downloadLink.href = filePath;
                        downloadLink.download = fileName;
                        downloadLink.style.display = 'none';
                        document.body.appendChild(downloadLink);
                        downloadLink.click();
                        setTimeout(function() {
                            document.body.removeChild(downloadLink);
                        }, 100);
                    }
                });
                actionTd.appendChild(downloadBtn);
                
                tr.appendChild(actionTd);
                
                tbody.appendChild(tr);
            });
            
            table.appendChild(tbody);
            cameraSection.appendChild(table);
        } else {
            const noRecordings = document.createElement('p');
            noRecordings.className = 'no-recordings';
            noRecordings.textContent = '録画ファイルが見つかりません';
            cameraSection.appendChild(noRecordings);
        }
        
        container.appendChild(cameraSection);
    });
    
    // 初回フィルター適用
    filterRecordings();
}

// 録画データをフィルターする関数
function filterRecordings() {
    const searchInput = document.getElementById('search-input');
    const cameraFilter = document.getElementById('camera-filter');
    if (!searchInput || !cameraFilter) return;
    
    const searchText = searchInput.value.toLowerCase();
    const selectedCamera = cameraFilter.value;
    
    document.querySelectorAll('.camera-section').forEach(function(section) {
        const cameraId = section.getAttribute('data-camera-id');
        if (selectedCamera !== 'all' && cameraId !== selectedCamera) {
            section.style.display = 'none';
            return;
        } else {
            section.style.display = '';
        }
        
        let visibleCount = 0;
        section.querySelectorAll('.recording-item').forEach(function(item) {
            const filename = item.getAttribute('data-filename').toLowerCase();
            if (searchText && !filename.includes(searchText)) {
                item.style.display = 'none';
            } else {
                item.style.display = '';
                visibleCount++;
            }
        });
        
        if (visibleCount === 0) {
            section.style.display = 'none';
        }
    });
}
