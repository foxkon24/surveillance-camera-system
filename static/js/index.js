// index.html用JavaScript
// カメラデータをスクリプトタグから取得
const camerasDataJson = document.getElementById('camera-data').textContent;
const camerasData = JSON.parse(camerasDataJson);

const players = {};
const retryAttempts = {};
const streamTimestamps = {};  // ストリームデータの最終受信時間を追跡
const MAX_RETRY_ATTEMPTS = 5;
const RETRY_DELAY = 2000;     // 再試行の遅延を短縮 (3000→2000ms)
const STREAM_CHECK_INTERVAL = 5000;  // ストリームチェック間隔を短縮 (10000→5000ms)
const STREAM_STALL_TIMEOUT = 10000;  // 停止と判断する時間を短縮 (20000→10000ms)

// カメラUIを動的に生成する関数
function createCameraElements() {
    const container = document.getElementById('camera-grid');
    camerasData.forEach(camera => {
        const cameraDiv = document.createElement('div');
        cameraDiv.className = 'camera';
        const cameraName = document.createElement('h4');
        cameraName.textContent = camera.name;
        cameraDiv.appendChild(cameraName);
        const videoWrapper = document.createElement('div');
        videoWrapper.className = 'video-wrapper';
        // enabled=0ならvideo生成せずステータスのみ
        if (camera.enabled === 0) {
            const statusDiv = document.createElement('div');
            statusDiv.className = 'stream-status';
            statusDiv.id = 'status' + camera.id;
            statusDiv.textContent = 'カメラ無効';
            videoWrapper.appendChild(statusDiv);
        } else {
            const video = document.createElement('video');
            video.id = 'video' + camera.id;
            video.autoplay = true;
            video.playsinline = true;
            video.muted = true;
            video.style.width = '320px';
            video.style.height = '240px';
            videoWrapper.appendChild(video);
            const statusDiv = document.createElement('div');
            statusDiv.className = 'stream-status';
            statusDiv.id = 'status' + camera.id;
            videoWrapper.appendChild(statusDiv);
        }
        cameraDiv.appendChild(videoWrapper);
        container.appendChild(cameraDiv);
    });
}

function updateStreamStatus(cameraId, status) {
    const statusElement = document.getElementById('status' + cameraId);
    if (statusElement) {
        statusElement.textContent = status;
    }
}

function reloadStream(cameraId) {
    const camera = camerasData.find(c => c.id == cameraId);
    if (camera && camera.enabled === 0) return;
    console.log(`Reloading stream for camera ${cameraId}`);
    updateStreamStatus(cameraId, '再読み込み中...');
    if (players[cameraId]) {
        players[cameraId].destroy();
    }
    setTimeout(() => initializePlayer(cameraId), 1000);
}

function initializePlayer(cameraId) {
    const camera = camerasData.find(c => c.id == cameraId);
    if (camera && camera.enabled === 0) return;
    const video = document.getElementById('video' + cameraId);
    // キャッシュバスターを複合化（より確実）
    const timestamp = new Date().getTime();
    // ページのグローバルタイムスタンプも利用（存在する場合）
    const pageTs = typeof pageTimestamp !== 'undefined' ? pageTimestamp : '';
    const filePath = `/system/cam/tmp/${cameraId}/${cameraId}.m3u8?t=${timestamp}&pt=${pageTs}`;
    
    if (players[cameraId]) {
        players[cameraId].destroy();
    }
    
    retryAttempts[cameraId] = 0;
    streamTimestamps[cameraId] = Date.now();
    
    if (Hls.isSupported()) {
        const hls = new Hls({
            debug: false,
            enableWorker: true,
            lowLatencyMode: true,
            backBufferLength: 0,           // バックバッファを無効化して最新データのみ表示
            maxBufferLength: 2,            // バッファ長をさらに短縮（リアルタイム性向上）
            maxMaxBufferLength: 4,         // 最大バッファ長も短縮
            liveBackBufferLength: 0,       // ライブストリームのバックバッファなし
            liveSyncDuration: 1,           // 同期持続時間を最小に（1秒）
            liveMaxLatencyDuration: 5,     // 最大レイテンシを5秒に制限
            manifestLoadingTimeOut: 5000,  // マニフェストロードタイムアウトを短く
            manifestLoadingMaxRetry: 4,    // マニフェストロード再試行回数
            levelLoadingTimeOut: 5000,     // レベルロードタイムアウトを短く
            fragLoadingTimeOut: 8000,      // フラグメントロードタイムアウト
            fragLoadingMaxRetry: 3,        // フラグメントロード再試行回数
            appendErrorMaxRetry: 3,        // 追加エラー再試行回数
            startFragPrefetch: true,      // フラグメントのプリフェッチを有効化
            testBandwidth: false          // 帯域テストを行わない（リアルタイム性優先）
        });

        // エラーイベントハンドラ
        hls.on(Hls.Events.ERROR, function (event, data) {
            // 深刻なエラーの場合のみ再試行
            if (data.fatal) {
                switch(data.type) {
                    case Hls.ErrorTypes.NETWORK_ERROR:
                        // ネットワークエラーは再試行
                        console.error(`カメラ ${cameraId} でネットワークエラーが発生: ${data.details}`);
                        handleStreamError(cameraId);
                        break;
                    case Hls.ErrorTypes.MEDIA_ERROR:
                        // メディアエラーは回復を試みる
                        console.error(`カメラ ${cameraId} でメディアエラーが発生: ${data.details}`);
                        hls.recoverMediaError();
                        break;
                    default:
                        // その他の致命的なエラーは再読み込み
                        console.error(`カメラ ${cameraId} で深刻なエラーが発生: ${data.type} - ${data.details}`);
                        handleStreamError(cameraId);
                        break;
                }
            } else {
                // 非致命的エラーはログのみ
                console.warn(`カメラ ${cameraId} で非致命的なエラーが発生: ${data.details}`);
            }
        });

        // マニフェストロードの成功を確認
        hls.on(Hls.Events.MANIFEST_PARSED, function() {
            console.info(`カメラ ${cameraId} のHLSマニフェストが解析されました`);
        });
        
        // フラグメントが読み込まれるたびにタイムスタンプを更新
        hls.on(Hls.Events.FRAG_LOADED, function() {
            streamTimestamps[cameraId] = Date.now();
        });

        // 定期的なチェックの実装
        let healthCheckInterval = setInterval(function() {
            const now = Date.now();
            const lastUpdate = streamTimestamps[cameraId] || 0;
            const elapsedTime = (now - lastUpdate) / 1000; // 秒単位
            
            // 一定時間以上更新がない場合はストリームを再読み込み
            if (lastUpdate > 0 && elapsedTime > 10) { // 10秒以上更新なし
                console.warn(`カメラ ${cameraId} のストリームが ${elapsedTime.toFixed(1)}秒間更新されていません。再読み込みします。`);
                clearInterval(healthCheckInterval);
                handleStreamError(cameraId);
            }
        }, 5000); // 5秒ごとにチェック

        hls.loadSource(filePath);
        hls.attachMedia(video);
        players[cameraId] = hls;
        
        video.addEventListener('loadedmetadata', function() {
            video.play().catch(e => console.warn('自動再生に失敗しました:', e));
        });
    } 
    else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // ネイティブHLS対応（Safari）
        video.src = filePath;
        video.addEventListener('loadedmetadata', function() {
            video.play().catch(e => console.warn('自動再生に失敗しました:', e));
        });
    }
}

function setupHealthChecks() {
    camerasData.forEach(camera => {
        if (camera.enabled === 0) return;
        setInterval(() => {
            checkCameraHealth(camera.id);
        }, STREAM_CHECK_INTERVAL);
    });
}

function checkCameraHealth(cameraId) {
    const camera = camerasData.find(c => c.id == cameraId);
    if (camera && camera.enabled === 0) return;
    const video = document.getElementById('video' + cameraId);
    const currentTime = Date.now();
    const lastUpdateTime = streamTimestamps[cameraId] || 0;
    
    // ビデオが読み込まれていない場合
    if (video && video.readyState === 0) {
        console.log(`Camera ${cameraId} stream not loaded, attempting recovery...`);
        updateStreamStatus(cameraId, '再接続中...');
        reloadStream(cameraId);
        return;
    }
    
    // データが一定時間更新されていない場合
    if (currentTime - lastUpdateTime > STREAM_STALL_TIMEOUT) {
        console.log(`Camera ${cameraId} stream stalled (no data for ${(currentTime - lastUpdateTime)/1000}s), reloading...`);
        updateStreamStatus(cameraId, 'データ停止 - 再読み込み中...');
        reloadStream(cameraId);
        return;
    }
    
    // バッファリング状態のチェック
    if (video && !video.paused && video.readyState > 1 && video.played.length > 0) {
        if (video.buffered.length > 0) {
            const bufferedEnd = video.buffered.end(video.buffered.length - 1);
            const bufferedTime = bufferedEnd - video.currentTime;
            
            // バッファが少なすぎる場合は警告
            if (bufferedTime < 0.5 && video.readyState < 4) {
                console.log(`Camera ${cameraId} insufficient buffer (${bufferedTime.toFixed(2)}s), may be stalling`);
                updateStreamStatus(cameraId, 'バッファリング中...');
                
                // バッファリングが続く場合はプレーヤーをリフレッシュ
                if (bufferedTime < 0.1) {
                    console.log(`Camera ${cameraId} critically low buffer, refreshing...`);
                    reloadStream(cameraId);
                }
            }
            else {
                updateStreamStatus(cameraId, '接続済');
            }
        }
    }
}

// サーバーサイドでカメラストリームを再起動
function restartStream(cameraId) {
    console.log(`カメラ ${cameraId} のストリームを再起動中...`);
    updateStreamStatus(cameraId, 'サーバー側で再起動中...');
    
    // クライアント側のプレーヤーを先にクリーンアップ
    if (players[cameraId]) {
        try {
            players[cameraId].destroy();
            players[cameraId] = null;
        } catch (e) {
            console.error(`Error destroying HLS instance for camera ${cameraId}:`, e);
        }
    }
    
    // サーバーにリクエストを送信
    fetch(`/system/cam/restart_stream/${cameraId}`, {
        method: 'POST',
        headers: {
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }
    })
    .then(response => response.json())
    .then(data => {
        console.log(`カメラ ${cameraId} 再起動レスポンス:`, data);
        
        // 少し早めに再初期化（3秒→2秒）
        setTimeout(() => {
            console.log(`カメラ ${cameraId} プレーヤーを再初期化中...`);
            initializePlayer(cameraId);
        }, 2000);
    })
    .catch(error => {
        console.error(`カメラ ${cameraId} 再起動リクエストエラー:`, error);
        updateStreamStatus(cameraId, '再起動リクエスト失敗');
        
        // エラーでも再初期化を試みる
        setTimeout(() => initializePlayer(cameraId), 2000);
    });
}

// すべてのカメラを再起動（順次実行）
function restartAllCameras(callback) {
    console.log("Restarting all cameras...");
    
    let index = 0;
    const restartNext = () => {
        if (index < camerasData.length) {
            const cameraId = camerasData[index].id;
            restartStream(cameraId);
            index++;
            // 各カメラの再起動間に2秒の遅延を設ける
            setTimeout(restartNext, 2000);
        } else {
            // すべてのカメラの再起動が完了したら、コールバックを実行
            if (typeof callback === 'function') {
                setTimeout(callback, 5000); // 5秒後にコールバックを実行
            }
        }
    };
    
    // 再起動処理を開始
    restartNext();
}

window.onload = function() {
    createCameraElements();
    camerasData.forEach(camera => {
        initializePlayer(camera.id);
    });
    setupHealthChecks();
};

document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
        for (const cameraId in players) {
            if (players[cameraId]) {
                players[cameraId].stopLoad();
            }
        }
    } else {
        for (const cameraId in players) {
            if (players[cameraId]) {
                players[cameraId].startLoad();
                streamTimestamps[cameraId] = Date.now();
            }
        }
    }
});

window.addEventListener('beforeunload', function() {
    for (const cameraId in players) {
        if (players[cameraId]) {
            players[cameraId].destroy();
        }
    }
});

document.addEventListener('DOMContentLoaded', function() {
    const cameraGrid = document.getElementById('camera-grid');
    if (!window.cameras || !Array.isArray(window.cameras)) return;
    window.cameras.forEach(function(camera) {
        // カメラごとに要素を生成
        const cameraDiv = document.createElement('div');
        cameraDiv.className = 'camera-item';
        cameraDiv.innerHTML = `
            <h4>${camera.name || 'カメラ'}</h4>
            <div class="video-container">
                <video id="video${camera.id}" autoplay playsinline muted></video>
                <div class="stream-status" id="status-${camera.id}">接続中...</div>
                <div class="loading-spinner" id="spinner-${camera.id}"></div>
                <div class="error-overlay" id="error-${camera.id}">エラーが発生しました<br>再読込してください</div>
            </div>
        `;
        cameraGrid.appendChild(cameraDiv);
        // HLS.jsでストリーム再生
        const video = cameraDiv.querySelector('video');
        const m3u8Url = camera.m3u8_url || `/system/cam/stream/${camera.id}/index.m3u8`;
        if (Hls.isSupported()) {
            const hls = new Hls();
            hls.loadSource(m3u8Url);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, function() {
                video.play();
            });
            hls.on(Hls.Events.ERROR, function(event, data) {
                document.getElementById(`status-${camera.id}`).textContent = 'ストリームエラー';
                document.getElementById(`error-${camera.id}`).style.display = 'block';
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = m3u8Url;
            video.addEventListener('loadedmetadata', function() {
                video.play();
            });
        } else {
            document.getElementById(`status-${camera.id}`).textContent = '未対応ブラウザ';
        }
    });
});

// ストリームエラーの統一処理関数
function handleStreamError(cameraId) {
    console.info(`カメラ ${cameraId} のストリームに問題が発生したため回復を試みます`);
    
    // 再試行回数をチェック
    if (retryAttempts[cameraId] >= MAX_RETRY_ATTEMPTS) {
        console.warn(`カメラ ${cameraId} の再試行回数が上限に達しました。サーバー側の再起動を要求します`);
        // サーバーサイドでのストリーム再起動をリクエスト
        fetch(`/system/cam/api/restart_stream/${cameraId}`)
            .then(response => response.json())
            .then(data => {
                console.info(`カメラ ${cameraId} のサーバーサイド再起動リクエスト結果:`, data);
                // サーバー側の処理を待って再初期化
                setTimeout(() => {
                    retryAttempts[cameraId] = 0;  // カウンターをリセット
                    initializePlayer(cameraId);
                }, 5000);  // 5秒待機
            })
            .catch(error => {
                console.error(`カメラ ${cameraId} の再起動リクエスト中にエラーが発生:`, error);
                // エラーが発生しても再試行
                setTimeout(() => {
                    retryAttempts[cameraId] = 0;
                    initializePlayer(cameraId);
                }, 5000);
            });
    } else {
        // クライアント側での再試行
        retryAttempts[cameraId]++;
        console.info(`カメラ ${cameraId} のストリームを再初期化します (試行: ${retryAttempts[cameraId]}/${MAX_RETRY_ATTEMPTS})`);
        
        // 既存のプレーヤーを破棄
        if (players[cameraId]) {
            players[cameraId].destroy();
            players[cameraId] = null;
        }
        
        // 少し待ってから再初期化
        setTimeout(() => {
            initializePlayer(cameraId);
        }, RETRY_DELAY);
    }
}
