// single.html用JavaScript
// cameraDataはwindow.cameraDataとしてグローバルにセットされている前提

let hls = null;
let retryAttempts = 0;
let lastDataReceived = Date.now();  // データ受信の最終時刻
let errorCount = 0;                 // エラー発生回数
let lastPlayTime = 0;               // 最後の再生位置
let pageActive = true;              // ページがアクティブかどうか
const MAX_RETRY_ATTEMPTS = 15;      // 最大再試行回数を増加
const RETRY_DELAY = 2000;           // 再試行間隔を短縮
const HEALTH_CHECK_INTERVAL = 3000; // 健全性チェック間隔を短縮
const STREAM_STALL_TIMEOUT = 15000; // ストリーム停止判定時間を短縮
const RECOVERY_ATTEMPTS = 3;        // 回復試行回数を増加

// ストリーミングの健全性チェック用フラグ
let healthCheckActive = true;

function updateStreamStatus(status, isError = false) {
    const statusElement = document.getElementById('status-main');
    const errorOverlay = document.getElementById('error-main');
    const spinner = document.getElementById('spinner-main');
    
    if (statusElement) {
        statusElement.textContent = status;
        
        // ステータスに応じて表示スタイルを変更
        if (isError) {
            statusElement.style.color = 'red';
            statusElement.style.fontWeight = 'bold';
            
            // エラーオーバーレイを表示
            if (errorOverlay) {
                errorOverlay.style.display = 'block';
                errorOverlay.textContent = status;
            }
        } else if (status === '接続済') {
            statusElement.style.color = 'green';
            
            // エラーオーバーレイを非表示
            if (errorOverlay) errorOverlay.style.display = 'none';
            
            // グローバルステータスを更新
            if (statusDisplay) {
                statusDisplay.textContent = 'ストリーム状態: 接続済み';
                statusDisplay.className = 'global-status status-ok';
            }
        } else if (status.includes('再接続') || status.includes('バッファリング')) {
            statusElement.style.color = 'orange';
            
            // エラーオーバーレイを非表示
            if (errorOverlay) errorOverlay.style.display = 'none';
            
            // グローバルステータスを更新
            if (statusDisplay) {
                statusDisplay.textContent = `ストリーム状態: ${status}`;
                statusDisplay.className = 'global-status status-warning';
            }
        } else {
            statusElement.style.color = 'gray';
            
            // エラーオーバーレイを非表示
            if (errorOverlay) errorOverlay.style.display = 'none';
        }
    }
    
    // ローディングスピナーの制御
    if (spinner) {
        if (status.includes('接続中') || status.includes('バッファリング') || status.includes('再読み込み中') || status.includes('再接続')) {
            spinner.style.display = 'block';
        } else {
            spinner.style.display = 'none';
        }
    }
}

function reloadStream() {
    console.log("Reloading stream");
    updateStreamStatus('再読み込み中...', false);
    
    // ローディングスピナーを表示
    const spinner = document.getElementById('spinner-main');
    if (spinner) spinner.style.display = 'block';
    
    if (hls) {
        try {
            hls.destroy();
        } catch (e) {
            console.error("Error destroying HLS instance:", e);
        }
        hls = null;
    }
    
    // リトライカウントをリセット
    retryAttempts = 0;
    
    // 少し待ってから再初期化
    setTimeout(() => initializePlayer(), RETRY_DELAY);
}

// サーバーサイドでカメラストリームを再起動（コールバック対応）
function restartStream(callback) {
    updateStreamStatus('ストリーム再起動中...', false);
    
    // ローディングスピナーを表示
    const spinner = document.getElementById('spinner-main');
    if (spinner) spinner.style.display = 'block';
    
    // 既存のプレイヤーをクリーンアップ
    if (hls) {
        try {
            hls.destroy();
        } catch (e) {}
        hls = null;
    }
    
    const cameraId = window.cameraData.id;
    
    // サーバーにリクエストを送信
    fetch(`/system/cam/restart_stream/${cameraId}`, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        console.log(`Stream restart response:`, data);
        if (data.status === 'success') {
            // 5秒待ってからプレイヤーを再初期化（サーバー側の処理完了を待つ）
            setTimeout(() => {
                initializePlayer();
                // コールバックがある場合は実行
                if (typeof callback === 'function') {
                    setTimeout(callback, 3000); // 3秒後にコールバックを実行
                }
            }, 5000);
        } else {
            updateStreamStatus(`再起動失敗: ${data.message || '不明なエラー'}`, true);
            // エラーがあっても再初期化を試みる
            setTimeout(() => {
                initializePlayer();
                // コールバックがある場合は実行
                if (typeof callback === 'function') {
                    setTimeout(callback, 3000);
                }
            }, 3000);
        }
    })
    .catch(error => {
        console.error(`Error restarting stream:`, error);
        updateStreamStatus('再起動リクエスト失敗', true);
        // エラーがあっても再初期化を試みる
        setTimeout(() => {
            initializePlayer();
            // コールバックがある場合は実行
            if (typeof callback === 'function') {
                setTimeout(callback, 3000);
            }
        }, 3000);
    });
}

function forcePageReload() {
    console.log("Stream failed to connect. Forcing page reload.");
    location.reload();
}

// HLSファイルのチェック - キャッシュを回避
async function checkHLSFile() {
    try {
        const cameraId = window.cameraData.id;
        const timestamp = new Date().getTime();
        const filePath = `/system/cam/tmp/${cameraId}/${cameraId}.m3u8?_=${timestamp}`;
        
        const response = await fetch(filePath, { 
            method: 'HEAD',
            cache: 'no-store' // キャッシュを使用しない
        });
        
        return response.ok;
    } catch (error) {
        console.error(`Error checking HLS file:`, error);
        return false;
    }
}

async function initializePlayer() {
    const cameraId = window.CAMERA_ID;
    const video = document.getElementById('video' + cameraId);
    if (!video) return;
    
    // エラーオーバーレイ非表示
    const errorOverlay = document.getElementById('error-main');
    if (errorOverlay) errorOverlay.style.display = 'none';
    
    // ローディングスピナー表示
    const spinner = document.getElementById('spinner-main');
    if (spinner) spinner.style.display = 'block';
    
    // キャッシュバスター付きHLS URLを生成
    const timestamp = new Date().getTime();
    const pageTs = typeof pageTimestamp !== 'undefined' ? pageTimestamp : '';
    const hlsUrl = `/system/cam/tmp/${cameraId}/${cameraId}.m3u8?t=${timestamp}&pt=${pageTs}`;

    // 既存のhlsインスタンスを破棄
    if (hls) {
        try { hls.destroy(); } catch (e) {}
        hls = null;
    }
    retryAttempts = 0;
    lastDataReceived = Date.now();

    if (Hls.isSupported()) {
        hls = new Hls({
            debug: false,
            enableWorker: true,
            lowLatencyMode: true,
            backBufferLength: 0,
            maxBufferLength: 2,
            maxMaxBufferLength: 4,
            liveBackBufferLength: 0,
            liveSyncDuration: 1,
            liveMaxLatencyDuration: 5,
            manifestLoadingTimeOut: 5000,
            manifestLoadingMaxRetry: 4,
            levelLoadingTimeOut: 5000,
            fragLoadingTimeOut: 8000,
            fragLoadingMaxRetry: 3,
            appendErrorMaxRetry: 3,
            startFragPrefetch: true,
            testBandwidth: false
        });
        hls.on(Hls.Events.ERROR, function (event, data) {
            if (data.fatal) {
                switch(data.type) {
                    case Hls.ErrorTypes.NETWORK_ERROR:
                        console.error(`ネットワークエラー: ${data.details}`);
                        reloadStream();
                        break;
                    case Hls.ErrorTypes.MEDIA_ERROR:
                        console.error(`メディアエラー: ${data.details}`);
                        hls.recoverMediaError();
                        break;
                    default:
                        console.error(`深刻なエラー: ${data.type} - ${data.details}`);
                        reloadStream();
                        break;
                }
            } else {
                console.warn(`非致命的なエラー: ${data.details}`);
            }
        });
        hls.on(Hls.Events.MANIFEST_PARSED, function() {
            video.play().catch(e => console.warn('自動再生に失敗:', e));
            if (spinner) spinner.style.display = 'none';
            updateStreamStatus('接続済');
        });
        hls.on(Hls.Events.FRAG_LOADED, function() {
            lastDataReceived = Date.now();
        });
        hls.loadSource(hlsUrl);
        hls.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = hlsUrl;
        video.addEventListener('loadedmetadata', function() {
            video.play().catch(e => console.warn('自動再生に失敗:', e));
            if (spinner) spinner.style.display = 'none';
            updateStreamStatus('接続済');
        });
    } else {
        updateStreamStatus('HLS未対応ブラウザ', true);
    }
    video.addEventListener('error', function(e) {
        console.error('VIDEO ERROR:', e, video.error);
        updateStreamStatus('ビデオエラー', true);
    });
}

// 健全性監視をindex.js流に強化
function setupHealthCheck() {
    setInterval(() => {
        if (!healthCheckActive) return;
        const cameraId = window.CAMERA_ID;
        const video = document.getElementById('video' + cameraId);
        if (!video) return;
        const now = Date.now();
        const elapsed = now - lastDataReceived;
        // readyState=0はストリーム未ロード
        if (video.readyState === 0) {
            console.log('ストリーム未ロード、回復試行');
            updateStreamStatus('再接続中...', false);
            reloadStream();
            return;
        }
        // データが一定時間更新されていない場合
        if (elapsed > 10000) {
            console.warn(`ストリームが${(elapsed/1000).toFixed(1)}秒間更新なし。再読み込み`);
            updateStreamStatus('データ停止 - 再読み込み中...', true);
            reloadStream();
            return;
        }
        // バッファリング状態の監視
        if (!video.paused && video.readyState > 1 && video.played.length > 0) {
            if (video.buffered.length > 0) {
                const bufferedEnd = video.buffered.end(video.buffered.length - 1);
                const bufferedTime = bufferedEnd - video.currentTime;
                if (bufferedTime < 0.5 && video.readyState < 4) {
                    console.log(`バッファ不足(${bufferedTime.toFixed(2)}s)`);
                    updateStreamStatus('バッファリング中...', false);
                    if (bufferedTime < 0.2 && hls) {
                        try {
                            video.pause();
                            setTimeout(() => {
                                video.play().catch(e => console.error('再開失敗:', e));
                            }, 500);
                        } catch (e) {
                            console.error('バッファ処理エラー:', e);
                        }
                    }
                }
            }
        }
        // 再生が止まっていないか
        if (!video.paused && video.played.length > 0) {
            if (Math.abs(video.currentTime - lastPlayTime) < 0.1 && elapsed > 5000) {
                console.log('再生停止、回復試行');
                updateStreamStatus('再生停止 - 回復中...', true);
                try {
                    video.currentTime = Math.max(0, video.currentTime - 2);
                    video.play().catch(e => {
                        console.error('回復再生失敗:', e);
                        reloadStream();
                    });
                } catch(e) {
                    console.error('回復エラー:', e);
                    reloadStream();
                }
            }
            lastPlayTime = video.currentTime;
        }
    }, 5000);
}

window.onload = function() {
    console.log("Page loaded, initializing player...");
    initializePlayer();
    setupHealthCheck();
};

// バックグラウンド切り替え時の処理
document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
        // バックグラウンドに移行した場合、健全性チェックを一時停止し、ストリームを一時停止
        healthCheckActive = false;
        if (hls) {
            try {
                hls.stopLoad();
                updateStreamStatus('一時停止中', false);
            } catch (e) {
                console.error("Error stopping HLS load:", e);
            }
        }
    } else {
        // フォアグラウンドに戻った場合、ストリームを再開
        if (hls) {
            try {
                hls.startLoad();
                lastDataReceived = Date.now(); // タイムスタンプを更新
                updateStreamStatus('再開中...', false);
                
                // ビデオ要素を取得
                const video = document.getElementById('video' + window.cameraData.id);
                if (video) {
                    video.play().catch(e => {
                        console.error("Resume play failed:", e);
                    });
                }
            } catch (e) {
                console.error("Error starting HLS load:", e);
                // プレイヤーが無効になっていたら再初期化
                initializePlayer();
            }
        } else {
            // プレイヤーが無効になっていたら再初期化
            initializePlayer();
        }
        
        // 健全性チェック再開（少し遅らせて）
        setTimeout(() => {
            healthCheckActive = true;
        }, 2000);
    }
});

// クリーンアップ
window.addEventListener('beforeunload', function() {
    if (hls) {
        try {
            hls.destroy();
        } catch (e) {
            console.error("Error destroying HLS instance:", e);
        }
    }
});
