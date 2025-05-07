// index.html用JavaScript
// カメラデータをスクリプトタグから取得
const camerasDataJson = document.getElementById('camera-data').textContent;
const camerasData = JSON.parse(camerasDataJson);

const players = {};
const retryAttempts = {};
const streamTimestamps = {};  // ストリームデータの最終受信時間を追跡
const MAX_RETRY_ATTEMPTS = 5;
const RETRY_DELAY = 3000;
const STREAM_CHECK_INTERVAL = 10000;  // 10秒ごとにストリームをチェック
const STREAM_STALL_TIMEOUT = 20000;  // 20秒間データが来なければ停止とみなす

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
    console.log(`Reloading stream for camera ${cameraId}`);
    updateStreamStatus(cameraId, '再読み込み中...');
    if (players[cameraId]) {
        players[cameraId].destroy();
    }
    setTimeout(() => initializePlayer(cameraId), 1000);
}

function initializePlayer(cameraId) {
    const video = document.getElementById('video' + cameraId);
    const filePath = '/system/cam/tmp/' + cameraId + '/' + cameraId + '.m3u8';
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
            backBufferLength: 30,
            maxBufferLength: 10,
            maxMaxBufferLength: 20,
            manifestLoadingTimeOut: 10000,
            manifestLoadingMaxRetry: 3,
            levelLoadingTimeOut: 10000,
            levelLoadingMaxRetry: 3,
            fragLoadingTimeOut: 10000,
            fragLoadingMaxRetry: 3,
            capLevelToPlayerSize: true,
            startLevel: -1,
            defaultAudioCodec: 'mp4a.40.2',
            progressive: false,
            xhrSetup: function(xhr) {
                xhr.addEventListener('error', function() {
                    console.log('xhr error occurred');
                });
            }
        });
        players[cameraId] = hls;
        updateStreamStatus(cameraId, '接続中...');
        hls.loadSource(filePath);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, function() {
            video.play().catch(function(error) {
                console.log(`Camera ${cameraId} autoplay failed:`, error);
                video.muted = true;
                video.play();
            });
            updateStreamStatus(cameraId, '接続済');
        });
        hls.on(Hls.Events.FRAG_LOADED, function() {
            streamTimestamps[cameraId] = Date.now();
        });
        hls.on(Hls.Events.ERROR, function(event, data) {
            console.log(`Camera ${cameraId} HLS error:`, data);
            if (data.fatal) {
                updateStreamStatus(cameraId, 'エラー発生');
                switch(data.type) {
                    case Hls.ErrorTypes.NETWORK_ERROR:
                        console.log(`Camera ${cameraId} network error, attempting recovery...`);
                        if (retryAttempts[cameraId] < MAX_RETRY_ATTEMPTS) {
                            retryAttempts[cameraId]++;
                            updateStreamStatus(cameraId, '再接続中...');
                            setTimeout(() => {
                                hls.startLoad();
                            }, RETRY_DELAY);
                        } else {
                            reloadStream(cameraId);
                        }
                        break;
                    case Hls.ErrorTypes.MEDIA_ERROR:
                        console.log(`Camera ${cameraId} media error, attempting recovery...`);
                        hls.recoverMediaError();
                        break;
                    default:
                        if (retryAttempts[cameraId] < MAX_RETRY_ATTEMPTS) {
                            retryAttempts[cameraId]++;
                            setTimeout(() => initializePlayer(cameraId), RETRY_DELAY);
                        } else {
                            reloadStream(cameraId);
                        }
                        break;
                }
            }
        });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = filePath;
        video.addEventListener('loadedmetadata', function() {
            video.play().catch(function(error) {
                console.log(`Camera ${cameraId} autoplay failed:`, error);
                video.muted = true;
                video.play();
            });
        });
    }
}

function setupHealthChecks() {
    camerasData.forEach(camera => {
        setInterval(() => {
            checkCameraHealth(camera.id);
        }, STREAM_CHECK_INTERVAL);
    });
}

function checkCameraHealth(cameraId) {
    const video = document.getElementById('video' + cameraId);
    const currentTime = Date.now();
    const lastUpdateTime = streamTimestamps[cameraId] || 0;
    if (video && video.readyState === 0) {
        console.log(`Camera ${cameraId} stream not loaded, attempting recovery...`);
        updateStreamStatus(cameraId, '再接続中...');
        reloadStream(cameraId);
        return;
    }
    if (currentTime - lastUpdateTime > STREAM_STALL_TIMEOUT) {
        console.log(`Camera ${cameraId} stream stalled (no data for ${(currentTime - lastUpdateTime)/1000}s), reloading...`);
        updateStreamStatus(cameraId, 'データ停止 - 再読み込み中...');
        reloadStream(cameraId);
        return;
    }
    if (video && !video.paused && video.readyState > 1 && video.played.length > 0) {
        if (video.buffered.length > 0) {
            const bufferedEnd = video.buffered.end(video.buffered.length - 1);
            const bufferedTime = bufferedEnd - video.currentTime;
            if (bufferedTime < 0.5 && video.readyState < 4) {
                console.log(`Camera ${cameraId} insufficient buffer (${bufferedTime.toFixed(2)}s), may be stalling`);
                updateStreamStatus(cameraId, 'バッファリング中...');
            }
        }
    }
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
