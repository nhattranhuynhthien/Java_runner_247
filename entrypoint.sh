#!/bin/bash
set -e

PORT=${PORT:-7860}
echo "=========================================================="
echo "🚀 Khởi động Cloud Gaming Ngọc Rồng Online 24/24..."
echo "📡 Cổng phục vụ: $PORT"
echo "=========================================================="

# 1. Start Virtual Display (Xvfb :1)
echo "[1/4] Khởi động Màn hình ảo (Xvfb :1)..."
Xvfb :1 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
export DISPLAY=:1

# 2. Start Lightweight Window Manager (Fluxbox)
echo "[2/4] Khởi động Window Manager (Fluxbox)..."
fluxbox &
sleep 1

# 3. Start VNC Server (x11vnc)
echo "[3/4] Khởi động x11vnc (Port 5900)..."
x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -q &
sleep 1

# 4. Start Ngoc Rong Online Watchdog Loop
echo "[4/4] Khởi chạy Ngọc Rồng Online (DragonBoy250.jar)..."
(
    while true; do
        echo "[GAME] Bắt đầu phiên chạy Ngọc Rồng..."
        java -Xms128m -Xmx1G -jar /app/emulator/freej2me.jar /app/workspace/DragonBoy250.jar || true
        echo "[GAME] Game đã tắt hoặc mất kết nối. Tự động bật lại sau 3 giây..."
        sleep 3
    done
) &

# Prepare noVNC directory and UptimeRobot health-check
mkdir -p /usr/share/novnc/health
echo '{"status":"alive","game":"Ngoc Rong Online 24/24"}' > /usr/share/novnc/health/index.html

# Set default index.html to auto-connect with auto-scaling to fit screen
cat << 'EOF' > /usr/share/novnc/index.html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0; url=/vnc.html?autoconnect=true&reconnect=true&resize=scale&quality=8">
    <title>Ngọc Rồng Online 24/24 Cloud Player</title>
</head>
<body style="background:#0b0f19;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:sans-serif;">
    <div style="text-align:center;">
        <h2 style="color:#fbbf24;margin-bottom:8px;">🐉 Ngọc Rồng Online 24/24</h2>
        <p style="color:#94a3b8;font-size:14px;">Đang kết nối vào màn hình game trên máy chủ đám mây...</p>
    </div>
</body>
</html>
EOF

# Start Websockify directly on $PORT (handles both HTTP static files, /health, and WebSocket)
echo "[READY] Hệ thống đã sẵn sàng phục vụ tại cổng $PORT!"
exec websockify --web /usr/share/novnc "$PORT" 127.0.0.1:5900
