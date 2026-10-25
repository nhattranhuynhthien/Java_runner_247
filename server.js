const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const cors = require('cors');
const path = require('path');
const fs = require('fs');
const { spawn, execSync } = require('child_process');
const os = require('os');
const multer = require('multer');
const pidusage = require('pidusage');
const crypto = require('crypto');

const PORT = parseInt(process.env.PORT || '7860', 10);
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD ? process.env.ADMIN_PASSWORD.trim() : '';
const WORKSPACE_DIR = path.resolve(__dirname, 'workspace');
const CONFIG_FILE = path.join(WORKSPACE_DIR, 'config.json');
const LOG_FILE = path.join(WORKSPACE_DIR, 'app.log');
const MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024; // 10MB limit per SRS requirement
const MAX_BUFFER_LINES = 1000; // 1,000 log lines ring buffer

if (!fs.existsSync(WORKSPACE_DIR)) {
    fs.mkdirSync(WORKSPACE_DIR, { recursive: true });
}

// Generate an application session secret for tokens
const SESSION_SECRET = crypto.randomBytes(32).toString('hex');
function generateToken(pass) {
    return crypto.createHmac('sha256', SESSION_SECRET).update(pass || 'default').digest('hex');
}
const VALID_TOKEN = generateToken(ADMIN_PASSWORD);

// Default configuration
const defaultConfig = {
    activeFile: 'Main.java',
    jvmArgs: '-Xms256m -Xmx4G',
    autoRestart: true,
    autoStart: true
};

function loadConfig() {
    try {
        if (fs.existsSync(CONFIG_FILE)) {
            const data = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
            return { ...defaultConfig, ...data };
        }
    } catch (e) {
        console.error('[CONFIG] Error loading config.json:', e.message);
    }
    return { ...defaultConfig };
}

function saveConfig(cfg) {
    try {
        fs.writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2), 'utf8');
    } catch (e) {
        console.error('[CONFIG] Error saving config.json:', e.message);
    }
}

let appConfig = loadConfig();

// App & Socket initialization
const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: { origin: '*' }
});

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Configure Multer for file uploads
const storage = multer.diskStorage({
    destination: (req, file, cb) => cb(null, WORKSPACE_DIR),
    filename: (req, file, cb) => {
        // Sanitize filename
        const safeName = path.basename(file.originalname).replace(/[^a-zA-Z0-9._-]/g, '_');
        cb(null, safeName);
    }
});
const upload = multer({
    storage,
    limits: { fileSize: 50 * 1024 * 1024 }, // 50MB max file
    fileFilter: (req, file, cb) => {
        const ext = path.extname(file.originalname).toLowerCase();
        if (['.java', '.jar', '.txt', '.json', '.properties', '.xml', '.yaml', '.yml'].includes(ext)) {
            cb(null, true);
        } else {
            cb(new Error('Chỉ chấp nhận file .java, .jar, .txt, .json, .properties, .xml'));
        }
    }
});

// Authentication Middleware
function checkAuth(req, res, next) {
    if (!ADMIN_PASSWORD) {
        return next(); // No password configured in environment
    }
    const authHeader = req.headers['authorization'];
    if (!authHeader) {
        return res.status(401).json({ success: false, error: 'Yêu cầu xác thực. Vui lòng đăng nhập.' });
    }
    const token = authHeader.replace(/^Bearer\s+/i, '').trim();
    if (token === VALID_TOKEN) {
        return next();
    }
    return res.status(401).json({ success: false, error: 'Mật khẩu hoặc Token không hợp lệ.' });
}

// Global Process State & Ring Buffer
let childProcess = null;
let processState = 'stopped'; // 'stopped' | 'compiling' | 'running' | 'error'
let processStartTime = null;
let isManualStop = false;
let restartCooldownTimeout = null;
let logLineId = 0;
const logRingBuffer = [];

function appendLog(type, text) {
    const timestamp = new Date().toISOString();
    const entry = {
        id: ++logLineId,
        type, // 'stdout' | 'stderr' | 'system'
        text,
        timestamp
    };

    logRingBuffer.push(entry);
    if (logRingBuffer.length > MAX_BUFFER_LINES) {
        logRingBuffer.shift();
    }

    // Broadcast in real-time to all connected web clients
    io.emit('log', entry);

    // Append to rotating log file
    try {
        if (fs.existsSync(LOG_FILE)) {
            const stats = fs.statSync(LOG_FILE);
            if (stats.size > MAX_LOG_SIZE_BYTES) {
                // Truncate / rotate log file
                const backupFile = LOG_FILE + '.1';
                if (fs.existsSync(backupFile)) fs.unlinkSync(backupFile);
                fs.renameSync(LOG_FILE, backupFile);
            }
        }
        fs.appendFileSync(LOG_FILE, `[${timestamp}] [${type.toUpperCase()}] ${text}\n`, 'utf8');
    } catch (e) {
        // Silently catch file log errors
    }
}

// Heartbeat & Process Monitoring
let cachedProcessUsage = { cpu: 0, memoryMb: 0 };
setInterval(async () => {
    if (childProcess && childProcess.pid && processState === 'running') {
        try {
            const stats = await pidusage(childProcess.pid);
            cachedProcessUsage = {
                cpu: Math.round(stats.cpu * 10) / 10,
                memoryMb: Math.round(stats.memory / (1024 * 1024))
            };
        } catch (e) {
            // Process might have terminated
            cachedProcessUsage = { cpu: 0, memoryMb: 0 };
        }
    } else {
        cachedProcessUsage = { cpu: 0, memoryMb: 0 };
    }

    const currentUptime = processStartTime ? Math.floor((Date.now() - processStartTime) / 1000) : 0;
    io.emit('stats', {
        state: processState,
        uptime: currentUptime,
        pid: childProcess ? childProcess.pid : null,
        javaStats: cachedProcessUsage,
        systemStats: {
            freememMb: Math.round(os.freemem() / (1024 * 1024)),
            totalmemMb: Math.round(os.totalmem() / (1024 * 1024)),
            loadavg: os.loadavg()[0] || 0
        }
    });
}, 2000);

// Process Execution Manager
async function startProcess(targetFile) {
    if (childProcess) {
        appendLog('system', '[CẢNH BÁO] Tiến trình đang chạy. Vui lòng dừng tiến trình trước khi khởi động mới.');
        return false;
    }

    const fileToRun = targetFile || appConfig.activeFile || 'Main.java';
    const filePath = path.join(WORKSPACE_DIR, fileToRun);

    if (!fs.existsSync(filePath)) {
        appendLog('system', `[LỖI] Không tìm thấy file '${fileToRun}' trong thư mục workspace.`);
        processState = 'error';
        io.emit('status_change', { state: processState });
        return false;
    }

    isManualStop = false;
    const ext = path.extname(fileToRun).toLowerCase();

    // 1. If Java Source File (.java), Compile First
    if (ext === '.java') {
        processState = 'compiling';
        io.emit('status_change', { state: processState });
        appendLog('system', `[BIÊN DỊCH] Đang biên dịch file ${fileToRun} (javac)...`);

        const compileSuccess = await new Promise((resolve) => {
            const javac = spawn('javac', ['-encoding', 'UTF-8', fileToRun], {
                cwd: WORKSPACE_DIR
            });

            javac.stdout.on('data', (d) => appendLog('stdout', d.toString().trimEnd()));
            javac.stderr.on('data', (d) => appendLog('stderr', d.toString().trimEnd()));

            javac.on('error', (err) => {
                appendLog('stderr', `[LỖI JAVAC] Không thể gọi lệnh javac: ${err.message}. Đảm bảo JDK đã được cài đặt.`);
                resolve(false);
            });

            javac.on('close', (code) => {
                if (code === 0) {
                    appendLog('system', `[BIÊN DỊCH] Biên dịch thành công ${fileToRun}!`);
                    resolve(true);
                } else {
                    appendLog('stderr', `[BIÊN DỊCH THẤT BẠI] javac thoát với mã lỗi ${code}.`);
                    resolve(false);
                }
            });
        });

        if (!compileSuccess) {
            processState = 'error';
            io.emit('status_change', { state: processState });
            return false;
        }
    }

    // 2. Prepare Java command arguments
    const jvmArgList = (appConfig.jvmArgs || '-Xms256m -Xmx4G')
        .split(' ')
        .map(s => s.trim())
        .filter(Boolean);

    let runArgs = [...jvmArgList];
    if (ext === '.jar') {
        runArgs.push('-jar', fileToRun);
    } else {
        const className = path.basename(fileToRun, '.java');
        runArgs.push(className);
    }

    appendLog('system', `[KHỞI CHẠY] Lệnh: java ${runArgs.join(' ')}`);

    try {
        childProcess = spawn('java', runArgs, {
            cwd: WORKSPACE_DIR,
            env: process.env,
            stdio: ['pipe', 'pipe', 'pipe']
        });
    } catch (err) {
        appendLog('stderr', `[LỖI] Không thể khởi động tiến trình Java: ${err.message}`);
        processState = 'error';
        io.emit('status_change', { state: processState });
        return false;
    }

    processState = 'running';
    processStartTime = Date.now();
    io.emit('status_change', { state: processState, pid: childProcess.pid, uptime: 0 });

    childProcess.stdout.on('data', (data) => {
        const lines = data.toString().split(/\r?\n/);
        lines.forEach((line) => {
            if (line.length > 0) appendLog('stdout', line);
        });
    });

    childProcess.stderr.on('data', (data) => {
        const lines = data.toString().split(/\r?\n/);
        lines.forEach((line) => {
            if (line.length > 0) appendLog('stderr', line);
        });
    });

    childProcess.on('error', (err) => {
        appendLog('stderr', `[LỖI RUNTIME] Tiến trình gặp sự cố: ${err.message}`);
    });

    childProcess.on('close', (code, signal) => {
        const duration = processStartTime ? Math.floor((Date.now() - processStartTime) / 1000) : 0;
        appendLog('system', `[KẾT THÚC] Tiến trình Java đã thoát. Mã thoát: ${code}, Tín hiệu: ${signal || 'none'}, Thời gian chạy: ${duration}s.`);
        
        childProcess = null;
        processState = 'stopped';
        processStartTime = null;
        io.emit('status_change', { state: processState, pid: null, uptime: 0 });

        // Watchdog Auto-Restart check
        if (!isManualStop && appConfig.autoRestart) {
            appendLog('system', '[WATCHDOG] Phát hiện tiến trình dừng đột ngột. Tự động khởi chạy lại sau 3 giây...');
            if (restartCooldownTimeout) clearTimeout(restartCooldownTimeout);
            restartCooldownTimeout = setTimeout(() => {
                startProcess(fileToRun);
            }, 3000);
        }
    });

    return true;
}

// Stop Process Gracefully
async function stopProcess() {
    if (!childProcess) {
        processState = 'stopped';
        io.emit('status_change', { state: processState, pid: null, uptime: 0 });
        return true;
    }

    isManualStop = true;
    if (restartCooldownTimeout) {
        clearTimeout(restartCooldownTimeout);
        restartCooldownTimeout = null;
    }

    appendLog('system', '[DỪNG] Đang dừng tiến trình Java...');

    const pid = childProcess.pid;

    // Gracefully close stdin if open
    try {
        if (childProcess.stdin && !childProcess.stdin.destroyed) {
            childProcess.stdin.end();
        }
    } catch (e) {}

    return new Promise((resolve) => {
        let finished = false;

        const cleanupAndResolve = () => {
            if (!finished) {
                finished = true;
                childProcess = null;
                processState = 'stopped';
                processStartTime = null;
                io.emit('status_change', { state: processState, pid: null, uptime: 0 });
                resolve(true);
            }
        };

        if (process.platform === 'win32') {
            // On Windows, console child processes must be terminated with /T /F to kill child process trees
            try {
                execSync(`taskkill /pid ${pid} /T /F`);
            } catch (e) {
                // If already gone or errored, try process.kill
                try { childProcess.kill(); } catch (err) {}
            }
            cleanupAndResolve();
        } else {
            // On Linux / Docker: First send SIGTERM, then SIGKILL after timeout
            try {
                process.kill(-pid, 'SIGTERM'); // Send to process group if possible
            } catch (e) {
                try { childProcess.kill('SIGTERM'); } catch (err) {}
            }

            const forceTimeout = setTimeout(() => {
                if (!finished && childProcess) {
                    appendLog('system', '[DỪNG] Tiến trình chưa dừng sau 4s. Gửi SIGKILL cưỡng chế ngắt...');
                    try {
                        process.kill(-pid, 'SIGKILL');
                    } catch (e) {
                        try { childProcess.kill('SIGKILL'); } catch (err) {}
                    }
                    cleanupAndResolve();
                }
            }, 4000);

            const interval = setInterval(() => {
                if (!childProcess || finished) {
                    clearInterval(interval);
                    clearTimeout(forceTimeout);
                    cleanupAndResolve();
                }
            }, 200);
        }
    });
}

// Send standard input to Java process
function sendStdin(text) {
    if (!childProcess || processState !== 'running') {
        throw new Error('Tiến trình Java chưa khởi chạy.');
    }
    childProcess.stdin.write(text + '\n');
    appendLog('system', `>> [STDIN] ${text}`);
}

// ==================== REST API ENDPOINTS ====================

// FR-03: Health-check Endpoint for UptimeRobot (Public, No Auth)
app.get('/health', (req, res) => {
    res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
    const uptimeSec = processStartTime ? Math.floor((Date.now() - processStartTime) / 1000) : 0;
    res.status(200).json({
        status: 'alive',
        java_process: processState,
        uptime_seconds: uptimeSec,
        system_uptime: Math.floor(os.uptime()),
        memory_usage_mb: cachedProcessUsage.memoryMb,
        timestamp: new Date().toISOString()
    });
});

app.get('/ping', (req, res) => {
    res.status(200).send('pong');
});

// Authentication Endpoint
app.post('/api/auth/login', (req, res) => {
    const { password } = req.body;
    if (!ADMIN_PASSWORD) {
        return res.json({ success: true, token: VALID_TOKEN, authRequired: false, message: 'Chế độ không mật khẩu' });
    }
    if (password === ADMIN_PASSWORD) {
        return res.json({ success: true, token: VALID_TOKEN, authRequired: true });
    }
    return res.status(401).json({ success: false, error: 'Sai mật khẩu quản trị!' });
});

// Get System & Process Status
app.get('/api/status', (req, res) => {
    const uptimeSec = processStartTime ? Math.floor((Date.now() - processStartTime) / 1000) : 0;
    res.json({
        state: processState,
        pid: childProcess ? childProcess.pid : null,
        uptime: uptimeSec,
        stats: cachedProcessUsage,
        config: appConfig,
        authRequired: !!ADMIN_PASSWORD,
        system: {
            platform: os.platform(),
            arch: os.arch(),
            cpus: os.cpus().length,
            freememMb: Math.round(os.freemem() / (1024 * 1024)),
            totalmemMb: Math.round(os.totalmem() / (1024 * 1024))
        }
    });
});

// List Files in Workspace
app.get('/api/files', (req, res) => {
    try {
        const files = fs.readdirSync(WORKSPACE_DIR)
            .filter(f => !f.startsWith('.') && f !== 'app.log' && f !== 'app.log.1' && f !== 'config.json')
            .map(f => {
                const stat = fs.statSync(path.join(WORKSPACE_DIR, f));
                return {
                    name: f,
                    size: stat.size,
                    mtime: stat.mtime,
                    isJar: f.endsWith('.jar'),
                    isJava: f.endsWith('.java')
                };
            });
        res.json({ success: true, files, activeFile: appConfig.activeFile });
    } catch (e) {
        res.status(500).json({ success: false, error: e.message });
    }
});

// Read File Content
app.get('/api/files/read', (req, res) => {
    const filename = req.query.name || appConfig.activeFile || 'Main.java';
    const safeName = path.basename(filename);
    const filePath = path.join(WORKSPACE_DIR, safeName);

    if (!fs.existsSync(filePath)) {
        return res.status(404).json({ success: false, error: 'File không tồn tại' });
    }

    if (safeName.endsWith('.jar')) {
        return res.json({ success: true, name: safeName, isBinary: true, content: '[File nhị phân .JAR]' });
    }

    try {
        const content = fs.readFileSync(filePath, 'utf8');
        res.json({ success: true, name: safeName, isBinary: false, content });
    } catch (e) {
        res.status(500).json({ success: false, error: e.message });
    }
});

// Save File Content (Auth Required)
app.post('/api/files/save', checkAuth, (req, res) => {
    const { name, content } = req.body;
    if (!name || typeof content !== 'string') {
        return res.status(400).json({ success: false, error: 'Thiếu thông tin tên file hoặc nội dung' });
    }
    const safeName = path.basename(name);
    const filePath = path.join(WORKSPACE_DIR, safeName);

    try {
        fs.writeFileSync(filePath, content, 'utf8');
        appendLog('system', `[LƯU FILE] Đã lưu thành công file: ${safeName}`);
        res.json({ success: true, message: `Đã lưu file ${safeName}` });
    } catch (e) {
        res.status(500).json({ success: false, error: e.message });
    }
});

// Upload File (Auth Required)
app.post('/api/files/upload', checkAuth, upload.single('file'), (req, res) => {
    if (!req.file) {
        return res.status(400).json({ success: false, error: 'Không có file nào được tải lên.' });
    }
    appendLog('system', `[TẢI LÊN] Đã tải lên file mới: ${req.file.filename} (${(req.file.size / 1024).toFixed(1)} KB)`);
    res.json({
        success: true,
        filename: req.file.filename,
        size: req.file.size
    });
});

// Delete File (Auth Required)
app.post('/api/files/delete', checkAuth, (req, res) => {
    const { name } = req.body;
    if (!name) return res.status(400).json({ success: false, error: 'Thiếu tên file.' });
    const safeName = path.basename(name);
    if (safeName === 'Main.java' || safeName === 'config.json') {
        return res.status(400).json({ success: false, error: 'Không được xóa file cốt lõi của hệ thống.' });
    }
    const filePath = path.join(WORKSPACE_DIR, safeName);
    try {
        if (fs.existsSync(filePath)) {
            fs.unlinkSync(filePath);
            appendLog('system', `[XÓA FILE] Đã xóa file: ${safeName}`);
        }
        res.json({ success: true, message: 'Đã xóa file' });
    } catch (e) {
        res.status(500).json({ success: false, error: e.message });
    }
});

// Process Controls (Auth Required)
app.post('/api/process/start', checkAuth, async (req, res) => {
    const target = req.body.file || appConfig.activeFile;
    if (target) {
        appConfig.activeFile = target;
        saveConfig(appConfig);
    }
    const success = await startProcess(appConfig.activeFile);
    res.json({ success, state: processState });
});

app.post('/api/process/stop', checkAuth, async (req, res) => {
    await stopProcess();
    res.json({ success: true, state: processState });
});

app.post('/api/process/restart', checkAuth, async (req, res) => {
    await stopProcess();
    const success = await startProcess(appConfig.activeFile);
    res.json({ success, state: processState });
});

app.post('/api/process/stdin', checkAuth, (req, res) => {
    const { input } = req.body;
    if (!input) return res.status(400).json({ success: false, error: 'Nội dung rỗng' });
    try {
        sendStdin(input);
        res.json({ success: true });
    } catch (e) {
        res.status(400).json({ success: false, error: e.message });
    }
});

// Update Configuration (Auth Required)
app.post('/api/config', checkAuth, (req, res) => {
    const { activeFile, jvmArgs, autoRestart, autoStart } = req.body;
    if (activeFile !== undefined) appConfig.activeFile = activeFile;
    if (jvmArgs !== undefined) appConfig.jvmArgs = jvmArgs;
    if (autoRestart !== undefined) appConfig.autoRestart = !!autoRestart;
    if (autoStart !== undefined) appConfig.autoStart = !!autoStart;

    saveConfig(appConfig);
    appendLog('system', `[CẤU HÌNH] Đã cập nhật cấu hình hệ thống: File=${appConfig.activeFile}, JVM=${appConfig.jvmArgs}, Watchdog=${appConfig.autoRestart}, AutoStart=${appConfig.autoStart}`);
    res.json({ success: true, config: appConfig });
});

// Clear in-memory log buffer (Auth Required)
app.post('/api/logs/clear', checkAuth, (req, res) => {
    logRingBuffer.length = 0;
    io.emit('logs_cleared');
    res.json({ success: true });
});

// Download log file
app.get('/api/logs/download', (req, res) => {
    if (fs.existsSync(LOG_FILE)) {
        res.download(LOG_FILE, 'java-runner.log');
    } else {
        res.status(404).send('Chưa có file log.');
    }
});

// Socket.io Connection Handler
io.on('connection', (socket) => {
    // Send initial state & recent ring buffer history
    const currentUptime = processStartTime ? Math.floor((Date.now() - processStartTime) / 1000) : 0;
    socket.emit('init', {
        state: processState,
        pid: childProcess ? childProcess.pid : null,
        uptime: currentUptime,
        activeFile: appConfig.activeFile,
        config: appConfig,
        authRequired: !!ADMIN_PASSWORD,
        logs: logRingBuffer
    });

    socket.on('stdin', (text) => {
        try {
            sendStdin(text);
        } catch (e) {
            socket.emit('log', {
                id: ++logLineId,
                type: 'stderr',
                text: `[LỖI STDIN] ${e.message}`,
                timestamp: new Date().toISOString()
            });
        }
    });
});

// Server Initialization
server.listen(PORT, '0.0.0.0', () => {
    console.log(`====================================================`);
    console.log(`🚀 Java 24/7 Web Runner & Controller is online!`);
    console.log(`📡 Port: ${PORT}`);
    console.log(`🌐 Local URL: http://localhost:${PORT}`);
    console.log(`🩺 Health check: http://localhost:${PORT}/health`);
    console.log(`🔒 Admin Auth: ${ADMIN_PASSWORD ? 'ENABLED (Secret set)' : 'DISABLED (No password)'}`);
    console.log(`====================================================`);

    appendLog('system', `[HỆ THỐNG] Web Controller khởi chạy thành công trên cổng ${PORT}. Sẵn sàng nhận lệnh.`);

    // FR-04: Auto-start process on container boot if configured
    if (appConfig.autoStart) {
        console.log(`[AUTO-START] Tự động khởi chạy ${appConfig.activeFile}...`);
        setTimeout(() => {
            startProcess(appConfig.activeFile);
        }, 1500);
    }
});
