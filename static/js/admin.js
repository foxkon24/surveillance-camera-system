// admin.html用JavaScript
// ...（ここにadmin.htmlの<script>タグ内のJSコードを移植）...

// window.camerasはHTML側でグローバルにセットされている前提

// ステータスメッセージを表示
function showStatusMessage(message, isError = false) {
    const msgElement = document.getElementById('status-message');
    if (msgElement) {
        msgElement.textContent = message;
        if (isError) {
            msgElement.style.color = 'red';
            msgElement.style.fontWeight = 'bold';
        } else {
            msgElement.style.color = '';
            msgElement.style.fontWeight = '';
        }
        msgElement.scrollIntoView({ behavior: 'smooth' });
    }
}

function startAllRecordings() {
    showStatusMessage('全カメラの録画を開始しています...');
    fetch('/stop_all_recordings', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
    .then(response => {
        if (!response.ok) throw new Error('録画停止に失敗しました');
        return response.json();
    })
    .then(data => {
        return fetch('/start_all_recordings', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    })
    .then(response => {
        if (!response.ok) throw new Error('録画開始に失敗しました');
        return response.json();
    })
    .then(data => {
        showStatusMessage('全カメラの録画を開始しました');
        setTimeout(() => { checkSystemStatus(false); }, 5000);
    })
    .catch(error => {
        showStatusMessage(`エラー: ${error.message}`, true);
    });
}

function stopAllRecordings() {
    showStatusMessage('全カメラの録画を停止しています...');
    fetch('/stop_all_recordings', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
    .then(response => {
        if (!response.ok) throw new Error('録画停止に失敗しました');
        return response.json();
    })
    .then(data => {
        showStatusMessage('全カメラの録画を停止しました');
        setTimeout(() => { checkSystemStatus(false); }, 3000);
    })
    .catch(error => {
        showStatusMessage(`エラー: ${error.message}`, true);
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

document.addEventListener('DOMContentLoaded', function() {
    checkSystemStatus(false);
});
