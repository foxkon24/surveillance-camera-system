// recordings.html用JavaScript

document.addEventListener('DOMContentLoaded', function() {
    // ファイルサイズを取得して表示
    document.querySelectorAll('.file-size').forEach(function(element) {
        const filePath = element.getAttribute('data-path');
        fetch(filePath, { method: 'HEAD' })
            .then(response => {
                if (response.ok) {
                    const size = response.headers.get('Content-Length');
                    if (size) {
                        const sizeInMB = (size / (1024 * 1024)).toFixed(2);
                        element.textContent = sizeInMB + ' MB';
                        if (parseFloat(sizeInMB) < 0.1) {
                            element.classList.add('small-file');
                            element.parentElement.classList.add('warning-row');
                        }
                    } else {
                        element.textContent = '不明';
                    }
                } else {
                    element.textContent = 'エラー';
                    element.parentElement.classList.add('error-row');
                }
            })
            .catch(error => {
                element.textContent = 'エラー';
                element.parentElement.classList.add('error-row');
            });
    });
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
            location.reload();
        });
    }
    // ダウンロードボタン
    document.querySelectorAll('.download-btn').forEach(function(button) {
        button.addEventListener('click', function() {
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
    });
    // 初回フィルター適用
    filterRecordings();
});

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
