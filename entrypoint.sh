#!/bin/bash
set -e

PORT=${PORT:-7860}
export DISPLAY=:1

echo "=========================================================="
echo "🚀 Khởi động Cloud Gaming Ngọc Rồng Online 24/24..."
echo "📡 Cổng phục vụ: $PORT"
echo "=========================================================="

# 1. Setup Fluxbox auto-maximize
mkdir -p /root/.fluxbox
cat << 'EOF' > /root/.fluxbox/apps
[app] (name=.*)
  [Maximized] {yes}
  [Deco] {NONE}
[end]
EOF

# 2. Start Virtual Display (Xvfb :1) - 480x640 optimal for mobile & PC
echo "[1/4] Khởi động Màn hình ảo (Xvfb :1)..."
Xvfb :1 -screen 0 480x640x24 -nolisten tcp &
sleep 1

# 3. Start Lightweight Window Manager (Fluxbox)
echo "[2/4] Khởi động Window Manager (Fluxbox)..."
fluxbox &
sleep 1

# 4. Start VNC Server (x11vnc) with zero latency flags (-nowf -noxdamage -repeat)
echo "[3/4] Khởi động x11vnc với độ trễ siêu thấp..."
x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -nowf -noxdamage -repeat -wait 10 -q &
sleep 1

# 5. Start Game Watchdog Loop (Keeps game running in background 24/7)
echo "[4/4] Bắt đầu phiên treo game Ngọc Rồng 24/24..."
(
    export DISPLAY=:1
    while true; do
        TARGET_JAR=$(ls /app/workspace/*.jar 2>/dev/null | head -n 1)
        if [ -n "$TARGET_JAR" ]; then
            echo "[GAME] Bắt đầu chạy game: $TARGET_JAR"
            java -Dfile.encoding=ISO_8859_1 -Xms128m -Xmx1G -jar /app/emulator/freej2me.jar "$TARGET_JAR" || true
        fi
        echo "[GAME] Game đã tắt hoặc thoát. Tự động khởi chạy lại sau 3 giây..."
        sleep 3
    done
) &

# Prepare noVNC directory and UptimeRobot health-check
mkdir -p /usr/share/novnc/health
echo '{"status":"alive","game":"Ngoc Rong Online 24/24"}' > /usr/share/novnc/health/index.html

# Deploy modern UI
cp /app/public/index.html /usr/share/novnc/index.html
cp /app/public/index.html /usr/share/novnc/vnc.html

# Start Websockify with heartbeat to prevent any disconnection
echo "[READY] Hệ thống đã sẵn sàng phục vụ tại cổng $PORT!"
exec websockify --web /usr/share/novnc --heartbeat 20 0.0.0.0:"$PORT" 127.0.0.1:5900
