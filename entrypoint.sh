#!/bin/bash
set -e

PORT=${PORT:-7860}
echo "=========================================================="
echo "🚀 Khởi động Cloud Gaming Ngọc Rồng Online 24/24..."
echo "📡 Cổng phục vụ: $PORT"
echo "=========================================================="

# 1. Start Virtual Display (Xvfb)
echo "[1/5] Khởi động Màn hình ảo (Xvfb :1)..."
Xvfb :1 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
export DISPLAY=:1

# 2. Start Lightweight Window Manager (Fluxbox)
echo "[2/5] Khởi động Window Manager (Fluxbox)..."
fluxbox &
sleep 1

# 3. Start VNC Server (x11vnc)
echo "[3/5] Khởi động x11vnc (Port 5900)..."
x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -q &
sleep 1

# 4. Start Websockify (Port 6080 for noVNC WebSocket)
echo "[4/5] Kết nối Websockify WebSocket..."
websockify 6080 127.0.0.1:5900 &
sleep 1

# 5. Start Ngoc Rong Online Watchdog Loop
echo "[5/5] Khởi chạy Ngọc Rồng Online (DragonBoy250.jar)..."
(
    while true; do
        echo "[GAME] Bắt đầu phiên chạy Ngọc Rồng..."
        java -Xms128m -Xmx1G -jar /app/emulator/freej2me.jar /app/workspace/DragonBoy250.jar || true
        echo "[GAME] Game đã tắt hoặc mất kết nối. Tự động bật lại sau 3 giây..."
        sleep 3
    done
) &

# Link noVNC static files into public directory
mkdir -p /app/public/novnc
cp -r /usr/share/novnc/* /app/public/novnc/ 2>/dev/null || true

# Start Web Server & Health Check on main PORT
echo "[READY] Hệ thống đã sẵn sàng phục vụ tại cổng $PORT!"
exec python3 /app/web_server.py

