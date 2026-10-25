// Java 24/7 Web Runner Client Application
(function () {
    let socket = null;
    let editor = null;
    let currentFile = 'Main.java';
    let isFileDirty = false;
    let autoScroll = true;
    let authRequired = false;
    let authToken = localStorage.getItem('java_runner_token') || '';
    let commandHistory = [];
    let historyIndex = -1;
    let logLineCount = 0;

    // DOM Elements
    const statusBadge = document.getElementById('status-badge');
    const statusText = document.getElementById('status-text');
    const metricPid = document.getElementById('metric-pid');
    const metricUptime = document.getElementById('metric-uptime');
    const metricRam = document.getElementById('metric-ram');
    const metricSysRam = document.getElementById('metric-sysram');

    const fileSelector = document.getElementById('file-selector');
    const activeFilename = document.getElementById('active-filename');
    const saveStatus = document.getElementById('save-status');
    const btnRefreshFiles = document.getElementById('btn-refresh-files');
    const btnUploadFile = document.getElementById('btn-upload-file');
    const fileInput = document.getElementById('file-input');
    const btnSaveCode = document.getElementById('btn-save-code');
    const btnRun = document.getElementById('btn-run');
    const btnStop = document.getElementById('btn-stop');
    const btnRestart = document.getElementById('btn-restart');
    const binaryPlaceholder = document.getElementById('binary-placeholder');

    const consoleOutput = document.getElementById('console-output');
    const logCount = document.getElementById('log-count');
    const btnToggleAutoscroll = document.getElementById('btn-toggle-autoscroll');
    const btnClearLogs = document.getElementById('btn-clear-logs');
    const btnDownloadLogs = document.getElementById('btn-download-logs');
    const stdinForm = document.getElementById('stdin-form');
    const stdinInput = document.getElementById('stdin-input');

    const btnAuthLock = document.getElementById('btn-auth-lock');
    const authStatusText = document.getElementById('auth-status-text');
    const authIcon = document.getElementById('auth-icon');
    const modalAuth = document.getElementById('modal-auth');
    const authForm = document.getElementById('auth-form');
    const authPasswordInput = document.getElementById('auth-password-input');
    const authErrorMsg = document.getElementById('auth-error-msg');

    const btnSettings = document.getElementById('btn-settings');
    const modalSettings = document.getElementById('modal-settings');
    const btnCloseSettings = document.getElementById('btn-close-settings');
    const btnCancelSettings = document.getElementById('btn-cancel-settings');
    const btnSaveSettings = document.getElementById('btn-save-settings');
    const cfgActiveFile = document.getElementById('cfg-active-file');
    const cfgJvmArgs = document.getElementById('cfg-jvm-args');
    const cfgAutoRestart = document.getElementById('cfg-auto-restart');
    const cfgAutoStart = document.getElementById('cfg-auto-start');

    const btnHealthCheck = document.getElementById('btn-health-check');
    const modalHealth = document.getElementById('modal-health');
    const btnCloseHealth = document.getElementById('btn-close-health');
    const btnDismissHealth = document.getElementById('btn-dismiss-health');
    const healthFullUrl = document.getElementById('health-full-url');
    const btnCopyHealth = document.getElementById('btn-copy-health');

    // Helper: Format Seconds to HH:MM:SS
    function formatUptime(seconds) {
        if (!seconds || seconds <= 0) return '00:00:00';
        const h = Math.floor(seconds / 3600).toString().padStart(2, '0');
        const m = Math.floor((seconds % 3600) / 60).toString().padStart(2, '0');
        const s = Math.floor(seconds % 60).toString().padStart(2, '0');
        return `${h}:${m}:${s}`;
    }

    // Helper: Update Status UI Badge
    function updateProcessStatus(state, pid) {
        statusBadge.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold flex items-center gap-1.5 transition-all';

        if (state === 'running') {
            statusBadge.classList.add('bg-emerald-950/80', 'text-emerald-300', 'border', 'border-emerald-500/40');
            statusBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-400 pulse-indicator"></span><span>Đang chạy</span>`;
            btnRun.disabled = true;
            btnStop.disabled = false;
            btnRestart.disabled = false;
            if (pid) metricPid.textContent = pid;
        } else if (state === 'compiling') {
            statusBadge.classList.add('bg-amber-950/80', 'text-amber-300', 'border', 'border-amber-500/40');
            statusBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-amber-400 animate-spin"></span><span>Đang biên dịch...</span>`;
            btnRun.disabled = true;
            btnStop.disabled = true;
            btnRestart.disabled = true;
        } else if (state === 'error') {
            statusBadge.classList.add('bg-rose-950/80', 'text-rose-300', 'border', 'border-rose-500/40');
            statusBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-400"></span><span>Lỗi thực thi</span>`;
            btnRun.disabled = false;
            btnStop.disabled = true;
            btnRestart.disabled = false;
            metricPid.textContent = '--';
        } else {
            statusBadge.classList.add('bg-slate-800', 'text-slate-300', 'border', 'border-slate-700');
            statusBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-slate-500"></span><span>Đã dừng</span>`;
            btnRun.disabled = false;
            btnStop.disabled = true;
            btnRestart.disabled = true;
            metricPid.textContent = '--';
            metricUptime.textContent = '00:00:00';
            metricRam.textContent = '0 MB';
        }
    }

    // API Request helper with Auth header
    async function apiRequest(endpoint, method = 'GET', body = null) {
        const headers = {
            'Content-Type': 'application/json'
        };
        if (authToken) {
            headers['Authorization'] = `Bearer ${authToken}`;
        }
        const options = { method, headers };
        if (body) {
            options.body = JSON.stringify(body);
        }

        const res = await fetch(endpoint, options);
        if (res.status === 401) {
            showAuthModal('Phiên làm việc hết hạn hoặc cần mật khẩu quản trị.');
            throw new Error('Yêu cầu xác thực.');
        }
        return res.json();
    }

    // Console Logging Append
    function appendLogEntry(entry) {
        const row = document.createElement('div');
        row.className = 'log-row';

        const time = document.createElement('span');
        time.className = 'log-time';
        const d = new Date(entry.timestamp);
        time.textContent = d.toTimeString().split(' ')[0];

        const content = document.createElement('span');
        content.className = 'log-content';

        if (entry.type === 'stderr') {
            content.classList.add('log-stderr');
        } else if (entry.type === 'system') {
            content.classList.add('log-system');
        } else {
            content.classList.add('log-stdout');
        }
        content.textContent = entry.text;

        row.appendChild(time);
        row.appendChild(content);
        consoleOutput.appendChild(row);

        logLineCount++;
        logCount.textContent = `${logLineCount} dòng`;

        if (autoScroll) {
            consoleOutput.scrollTop = consoleOutput.scrollHeight;
        }
    }

    // Load File list
    async function refreshFiles(selectFileName = null) {
        try {
            const data = await apiRequest('/api/files');
            if (data.success && data.files) {
                fileSelector.innerHTML = '';
                data.files.forEach(f => {
                    const opt = document.createElement('option');
                    opt.value = f.name;
                    opt.textContent = `${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
                    fileSelector.appendChild(opt);
                });

                const target = selectFileName || data.activeFile || 'Main.java';
                if ([...fileSelector.options].some(o => o.value === target)) {
                    fileSelector.value = target;
                    loadFile(target);
                } else if (fileSelector.options.length > 0) {
                    fileSelector.selectedIndex = 0;
                    loadFile(fileSelector.value);
                }
            }
        } catch (e) {
            console.error('Error refreshing files:', e);
        }
    }

    // Load Single File Content
    async function loadFile(name) {
        currentFile = name;
        activeFilename.textContent = name;
        if (cfgActiveFile) cfgActiveFile.value = name;

        if (name.endsWith('.jar')) {
            binaryPlaceholder.classList.remove('hidden');
            if (editor) editor.setValue('// File gói nhị phân .JAR');
            return;
        }

        binaryPlaceholder.classList.add('hidden');
        try {
            const data = await apiRequest(`/api/files/read?name=${encodeURIComponent(name)}`);
            if (data.success && editor) {
                editor.setValue(data.content);
                // Set language model
                const model = editor.getModel();
                if (name.endsWith('.java')) {
                    monaco.editor.setModelLanguage(model, 'java');
                } else if (name.endsWith('.json')) {
                    monaco.editor.setModelLanguage(model, 'json');
                } else if (name.endsWith('.xml')) {
                    monaco.editor.setModelLanguage(model, 'xml');
                } else {
                    monaco.editor.setModelLanguage(model, 'plaintext');
                }
                isFileDirty = false;
            }
        } catch (e) {
            console.error('Error loading file content:', e);
        }
    }

    // Save File
    async function saveCurrentFile() {
        if (!editor || currentFile.endsWith('.jar')) return;
        const content = editor.getValue();
        try {
            const res = await apiRequest('/api/files/save', 'POST', {
                name: currentFile,
                content
            });
            if (res.success) {
                isFileDirty = false;
                saveStatus.classList.remove('hidden');
                setTimeout(() => saveStatus.classList.add('hidden'), 2500);
            }
        } catch (e) {
            console.error('Save error:', e);
        }
    }

    // Initialize Monaco Editor
    function initMonaco() {
        require.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min/vs' } });
        require(['vs/editor/editor.main'], function () {
            editor = monaco.editor.create(document.getElementById('monaco-editor'), {
                value: '// Đang tải mã nguồn...',
                language: 'java',
                theme: 'vs-dark',
                automaticLayout: true,
                fontSize: 13,
                minimap: { enabled: false },
                scrollBeyondLastLine: false,
                tabSize: 4
            });

            // Ctrl+S or Cmd+S shortcut to save
            editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function () {
                saveCurrentFile();
            });

            editor.onDidChangeModelContent(() => {
                isFileDirty = true;
            });

            // Once editor is ready, fetch files
            refreshFiles();
        });
    }

    // Initialize Socket.io
    function initSocket() {
        socket = io({
            auth: { token: authToken }
        });

        socket.on('connect', () => {
            console.log('Socket.io connected');
        });

        socket.on('init', (data) => {
            updateProcessStatus(data.state, data.pid);
            authRequired = data.authRequired;
            updateAuthStatusUI();

            if (data.config) {
                if (cfgActiveFile) cfgActiveFile.value = data.config.activeFile || 'Main.java';
                if (cfgJvmArgs) cfgJvmArgs.value = data.config.jvmArgs || '-Xms256m -Xmx4G';
                if (cfgAutoRestart) cfgAutoRestart.checked = !!data.config.autoRestart;
                if (cfgAutoStart) cfgAutoStart.checked = !!data.config.autoStart;
            }

            // Render existing ring buffer logs
            consoleOutput.innerHTML = '';
            logLineCount = 0;
            if (data.logs && data.logs.length > 0) {
                data.logs.forEach(appendLogEntry);
            } else {
                consoleOutput.innerHTML = '<div class="text-slate-500 italic">[Chưa có log. Nhấn "Biên dịch & Chạy" để khởi động]</div>';
            }
        });

        socket.on('log', (entry) => {
            appendLogEntry(entry);
        });

        socket.on('status_change', (data) => {
            updateProcessStatus(data.state, data.pid);
        });

        socket.on('stats', (data) => {
            updateProcessStatus(data.state, data.pid);
            metricUptime.textContent = formatUptime(data.uptime);
            if (data.javaStats) {
                metricRam.textContent = `${data.javaStats.memoryMb} MB`;
            }
            if (data.systemStats) {
                metricSysRam.textContent = `${Math.round(data.systemStats.freememMb / 1024 * 10) / 10}GB / ${Math.round(data.systemStats.totalmemMb / 1024 * 10) / 10}GB`;
            }
        });

        socket.on('logs_cleared', () => {
            consoleOutput.innerHTML = '<div class="text-slate-500 italic">[Màn hình console đã được xóa]</div>';
            logLineCount = 0;
            logCount.textContent = '0 dòng';
        });
    }

    // Auth UI handling
    function updateAuthStatusUI() {
        if (!authRequired) {
            btnAuthLock.className = 'px-3 py-1.5 rounded-lg bg-emerald-600/20 text-emerald-300 border border-emerald-500/30 text-xs flex items-center gap-1.5';
            authStatusText.textContent = 'Công khai';
            authIcon.className = 'fa-solid fa-lock-open';
        } else if (authToken) {
            btnAuthLock.className = 'px-3 py-1.5 rounded-lg bg-emerald-600/20 text-emerald-300 border border-emerald-500/30 text-xs flex items-center gap-1.5';
            authStatusText.textContent = 'Đã đăng nhập';
            authIcon.className = 'fa-solid fa-shield-check';
        } else {
            btnAuthLock.className = 'px-3 py-1.5 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/40 text-xs flex items-center gap-1.5';
            authStatusText.textContent = 'Đăng nhập';
            authIcon.className = 'fa-solid fa-lock';
        }
    }

    function showAuthModal(error = '') {
        modalAuth.classList.remove('hidden');
        if (error) {
            authErrorMsg.textContent = error;
            authErrorMsg.classList.remove('hidden');
        } else {
            authErrorMsg.classList.add('hidden');
        }
        authPasswordInput.focus();
    }

    function hideAuthModal() {
        modalAuth.classList.add('hidden');
        authPasswordInput.value = '';
    }

    // Event Listeners
    function setupEventListeners() {
        // Run, Stop, Restart
        btnRun.addEventListener('click', async () => {
            await saveCurrentFile();
            try {
                await apiRequest('/api/process/start', 'POST', { file: currentFile });
            } catch (e) {}
        });

        btnStop.addEventListener('click', async () => {
            try {
                await apiRequest('/api/process/stop', 'POST');
            } catch (e) {}
        });

        btnRestart.addEventListener('click', async () => {
            await saveCurrentFile();
            try {
                await apiRequest('/api/process/restart', 'POST', { file: currentFile });
            } catch (e) {}
        });

        // Save Code Button
        btnSaveCode.addEventListener('click', () => {
            saveCurrentFile();
        });

        // File Selector change
        fileSelector.addEventListener('change', (e) => {
            loadFile(e.target.value);
        });

        btnRefreshFiles.addEventListener('click', () => {
            refreshFiles(currentFile);
        });

        // File Upload
        btnUploadFile.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', async (e) => {
            if (!fileInput.files || fileInput.files.length === 0) return;
            const file = fileInput.files[0];
            const formData = new FormData();
            formData.append('file', file);

            const headers = {};
            if (authToken) headers['Authorization'] = `Bearer ${authToken}`;

            try {
                const res = await fetch('/api/files/upload', {
                    method: 'POST',
                    headers,
                    body: formData
                });
                const data = await res.json();
                if (data.success) {
                    await refreshFiles(data.filename);
                } else {
                    alert('Lỗi tải file: ' + (data.error || 'Thất bại'));
                }
            } catch (err) {
                alert('Lỗi mạng khi tải file.');
            }
            fileInput.value = '';
        });

        // Console Controls
        btnToggleAutoscroll.addEventListener('click', () => {
            autoScroll = !autoScroll;
            btnToggleAutoscroll.classList.toggle('text-emerald-400', autoScroll);
            btnToggleAutoscroll.classList.toggle('text-slate-400', !autoScroll);
        });

        btnClearLogs.addEventListener('click', async () => {
            try {
                await apiRequest('/api/logs/clear', 'POST');
            } catch (e) {}
        });

        btnDownloadLogs.addEventListener('click', () => {
            window.open('/api/logs/download', '_blank');
        });

        // STDIN Form & History
        stdinForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const text = stdinInput.value.trim();
            if (!text) return;

            commandHistory.push(text);
            historyIndex = commandHistory.length;

            try {
                await apiRequest('/api/process/stdin', 'POST', { input: text });
                stdinInput.value = '';
            } catch (err) {
                console.error(err);
            }
        });

        // Up/Down Arrow for Stdin History
        stdinInput.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowUp') {
                if (historyIndex > 0) {
                    historyIndex--;
                    stdinInput.value = commandHistory[historyIndex] || '';
                }
                e.preventDefault();
            } else if (e.key === 'ArrowDown') {
                if (historyIndex < commandHistory.length - 1) {
                    historyIndex++;
                    stdinInput.value = commandHistory[historyIndex] || '';
                } else {
                    historyIndex = commandHistory.length;
                    stdinInput.value = '';
                }
                e.preventDefault();
            }
        });

        // Settings Modal
        btnSettings.addEventListener('click', () => {
            modalSettings.classList.remove('hidden');
        });
        btnCloseSettings.addEventListener('click', () => modalSettings.classList.add('hidden'));
        btnCancelSettings.addEventListener('click', () => modalSettings.classList.add('hidden'));
        btnSaveSettings.addEventListener('click', async () => {
            try {
                const res = await apiRequest('/api/config', 'POST', {
                    activeFile: cfgActiveFile.value.trim(),
                    jvmArgs: cfgJvmArgs.value.trim(),
                    autoRestart: cfgAutoRestart.checked,
                    autoStart: cfgAutoStart.checked
                });
                if (res.success) {
                    modalSettings.classList.add('hidden');
                }
            } catch (e) {}
        });

        // Health & UptimeRobot Modal
        btnHealthCheck.addEventListener('click', () => {
            const currentOrigin = window.location.origin;
            healthFullUrl.value = `${currentOrigin}/health`;
            modalHealth.classList.remove('hidden');
        });
        btnCloseHealth.addEventListener('click', () => modalHealth.classList.add('hidden'));
        btnDismissHealth.addEventListener('click', () => modalHealth.classList.add('hidden'));
        btnCopyHealth.addEventListener('click', () => {
            healthFullUrl.select();
            navigator.clipboard.writeText(healthFullUrl.value);
            btnCopyHealth.innerHTML = '<i class="fa-solid fa-check text-emerald-400"></i>';
            setTimeout(() => {
                btnCopyHealth.innerHTML = '<i class="fa-solid fa-copy"></i>';
            }, 2000);
        });

        // Auth Modal & Form
        btnAuthLock.addEventListener('click', () => {
            if (authRequired && authToken) {
                // Logout confirmation
                if (confirm('Bạn có muốn đăng xuất khỏi quyền Quản trị?')) {
                    authToken = '';
                    localStorage.removeItem('java_runner_token');
                    updateAuthStatusUI();
                }
            } else {
                showAuthModal();
            }
        });

        authForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const pass = authPasswordInput.value;
            try {
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: pass })
                });
                const data = await res.json();
                if (data.success) {
                    authToken = data.token;
                    localStorage.setItem('java_runner_token', authToken);
                    hideAuthModal();
                    updateAuthStatusUI();
                } else {
                    authErrorMsg.textContent = data.error || 'Sai mật khẩu!';
                    authErrorMsg.classList.remove('hidden');
                }
            } catch (err) {
                authErrorMsg.textContent = 'Lỗi kết nối máy chủ.';
                authErrorMsg.classList.remove('hidden');
            }
        });
    }

    // Startup
    document.addEventListener('DOMContentLoaded', () => {
        initSocket();
        initMonaco();
        setupEventListeners();
    });
})();

