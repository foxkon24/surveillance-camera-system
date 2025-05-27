// admin.html用JavaScript
// ...（ここにadmin.htmlの<script>タグ内のJSコードを移植）...

// window.camerasはHTML側でグローバルにセットされている前提

// ステータスメッセージを表示
function showStatusMessage(message, isError = false) {
    const msgElement = document.getElementById('status-message');
    if (msgElement) {
        if (message && message.trim() !== '') {
            msgElement.textContent = message;
            if (isError) {
                msgElement.style.color = 'red';
                msgElement.style.fontWeight = 'bold';
            } else {
                msgElement.style.color = '';
                msgElement.style.fontWeight = '';
            }
            msgElement.style.display = 'block';
            msgElement.scrollIntoView({ behavior: 'smooth' });
        } else {
            msgElement.style.display = 'none';
        }
    }
}

// ローディングオーバーレイの表示/非表示
function setLoading(show) {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.style.display = show ? 'flex' : 'none';
    }
}

// 管理画面の初期化 - ページ読み込み時に呼ばれる
function initializeAdminPage() {
    // ローディング表示
    setLoading(true);
    
    // システム状態を取得して表示
    fetchSystemStatus();
    
    // カメラ情報を取得して表示
    fetchCameraList()
        .then(() => {
            // すべての読み込みが完了したらローディングを非表示
            setLoading(false);
        })
        .catch(error => {
            console.error('初期化エラー:', error);
            setLoading(false);
        });
}

// システム状態を取得・表示する関数
function fetchSystemStatus() {
    return fetch('/system/cam/admin_data')
        .then(response => {
            if (!response.ok) {
                throw new Error('システム状態の取得に失敗しました');
            }
            return response.json();
        })
        .then(data => {
            // ディスク使用量を表示
            const diskUsageElement = document.getElementById('disk-usage');
            if (diskUsageElement && data.disk_usage) {
                diskUsageElement.innerHTML = data.disk_usage;
            }
        })
        .catch(error => {
            console.error('システム状態取得エラー:', error);
            // エラーメッセージを表示（必要に応じて）
        });
}

// カメラリストを取得・表示する関数
function fetchCameraList() {
    return fetch('/system/cam/admin_data')
        .then(response => {
            if (!response.ok) {
                throw new Error('カメラ情報の取得に失敗しました');
            }
            return response.json();
        })
        .then(data => {
            const cameraListElement = document.getElementById('camera-list-container');
            if (cameraListElement && data.cameras) {
                // 既存の内容をクリア
                cameraListElement.innerHTML = '';
                
                // 見出しを追加
                const heading = document.createElement('h2');
                heading.textContent = '登録カメラ一覧';
                cameraListElement.appendChild(heading);
                
                if (data.cameras.length > 0) {
                    // グリッドコンテナを作成
                    const gridContainer = document.createElement('div');
                    gridContainer.className = 'camera-grid';
                    cameraListElement.appendChild(gridContainer);
                    
                    // カメラ情報を表示
                    data.cameras.forEach(camera => {
                        const cameraDiv = document.createElement('div');
                        cameraDiv.className = 'camera-item';
                        
                        const cameraHeading = document.createElement('h3');
                        cameraHeading.textContent = camera.name || `カメラ ${camera.id}`;
                        cameraDiv.appendChild(cameraHeading);
                        
                        const idP = document.createElement('p');
                        idP.textContent = `ID: ${camera.id}`;
                        cameraDiv.appendChild(idP);
                        
                        const statusP = document.createElement('p');
                        statusP.textContent = `状態: ${camera.status ? '録画中' : '停止中'}`;
                        cameraDiv.appendChild(statusP);
                        
                        gridContainer.appendChild(cameraDiv);
                    });
                } else {
                    // カメラが登録されていない場合
                    const noCamera = document.createElement('p');
                    noCamera.textContent = '登録されたカメラがありません';
                    cameraListElement.appendChild(noCamera);
                }
            }
        })
        .catch(error => {
            console.error('カメラリスト取得エラー:', error);
            // エラーメッセージを表示（必要に応じて）
        });
}

function setRecordingButtonsEnabled(startEnabled, stopEnabled) {
    const startBtn = document.getElementById('start-all-recordings-btn');
    const stopBtn = document.getElementById('stop-all-recordings-btn');
    if (startBtn) startBtn.disabled = !startEnabled;
    if (stopBtn) stopBtn.disabled = !stopEnabled;
}

function startAllRecordings() {
    showStatusMessage('録画開始中...');
    setRecordingButtonsEnabled(false, false); // 開始中は両方無効化
    fetch('/start_all_recordings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        showStatusMessage('');
        setRecordingButtonsEnabled(true, true); // 完了後有効化
        alert(data.status);
        fetchSystemStatus();
        fetchCameraList();
    })
    .catch(error => {
        showStatusMessage('');
        setRecordingButtonsEnabled(true, true);
        console.error('Error:', error);
        alert('エラーが発生しました');
    });
}

function stopAllRecordings() {
    showStatusMessage('録画停止中...');
    setRecordingButtonsEnabled(false, false); // 停止中は両方無効化
    fetch('/stop_all_recordings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        showStatusMessage('');
        setRecordingButtonsEnabled(true, true); // 完了後有効化
        alert(data.status);
        fetchSystemStatus();
        fetchCameraList();
    })
    .catch(error => {
        showStatusMessage('');
        setRecordingButtonsEnabled(true, true);
        console.error('Error:', error);
        alert('エラーが発生しました');
    });
}

function restartAllStreams() {
    showStatusMessage('全カメラのストリームを再起動しています...');
    fetch('/system/cam/restart_all_streams', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
    .then(response => {
        if (!response.ok) throw new Error('ストリーム再起動に失敗しました');
        return response.json();
    })
    .then(data => {
        if (data.status === 'success') {
            showStatusMessage('全カメラのストリームを再起動しました');
        } else if (data.status === 'partial') {
            showStatusMessage('一部のカメラのストリーム再起動に失敗しました', true);
        } else {
            showStatusMessage(`ストリーム再起動エラー: ${data.message || '不明なエラー'}`, true);
        }
        setTimeout(() => { checkSystemStatus(false); }, 10000);
    })
    .catch(error => {
        showStatusMessage(`エラー: ${error.message}`, true);
    });
}

function checkSystemStatus(showMessage = true) {
    if (showMessage) showStatusMessage('システム状態を確認しています...');
    fetch('/system/cam/status')
    .then(response => {
        if (!response.ok) throw new Error('システム状態の取得に失敗しました');
        return response.json();
    })
    .then(data => {
        const container = document.getElementById('system-status-container');
        if (container) {
            container.textContent = JSON.stringify(data, null, 2);
        }
        if (showMessage) showStatusMessage('システム状態を取得しました');
    })
    .catch(error => {
        showStatusMessage(`エラー: ${error.message}`, true);
    });
}

function checkDiskSpace() {
    showStatusMessage('ディスク容量を確認しています...');
    fetch('/system/cam/check_disk_space')
    .then(response => {
        if (!response.ok) throw new Error('ディスク容量の取得に失敗しました');
        return response.json();
    })
    .then(data => {
        showStatusMessage(`空き容量: ${data.free_space || '不明'}`);
    })
    .catch(error => {
        showStatusMessage(`エラー: ${error.message}`, true);
    });
}

function cleanupOldRecordings() {
    showStatusMessage('古い録画を削除しています...');
    fetch('/system/cam/cleanup_old_recordings', { method: 'POST' })
    .then(response => {
        if (!response.ok) throw new Error('古い録画の削除に失敗しました');
        return response.json();
    })
    .then(data => {
        showStatusMessage('古い録画を削除しました');
    })
    .catch(error => {
        showStatusMessage(`エラー: ${error.message}`, true);
    });
}

// ページ読み込み時の処理
document.addEventListener('DOMContentLoaded', function() {
    // 管理画面の初期化
    initializeAdminPage();
});
