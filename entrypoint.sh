#!/bin/bash
set -e

PORT=${PORT:-7860}
export DISPLAY=:1

echo "=========================================================="
echo "🚀 Khởi động Cloud Gaming Ngọc Rồng Online 24/24..."
echo "📡 Cổng phục vụ: $PORT"
echo "=========================================================="

# 1. Setup Fluxbox auto-maximize (makes game fill 100% of the screen)
mkdir -p /root/.fluxbox
cat << 'EOF' > /root/.fluxbox/apps
[app] (name=.*)
  [Maximized] {yes}
  [Deco] {NONE}
[end]
EOF

# 2. Start Virtual Display (Xvfb :1) with optimal J2ME resolution (480x640)
echo "[1/4] Khởi động Màn hình ảo (Xvfb :1)..."
Xvfb :1 -screen 0 480x640x24 -nolisten tcp &
sleep 2

# 3. Start Lightweight Window Manager (Fluxbox)
echo "[2/4] Khởi động Window Manager (Fluxbox)..."
fluxbox &
sleep 1

# 4. Start VNC Server (x11vnc)
echo "[3/4] Khởi động x11vnc (Port 5900)..."
x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -wait 50 -repeat -q &
sleep 1

# 5. Start Dynamic Game Watchdog Loop (Runs any .jar in workspace with ISO_8859_1 encoding)
echo "[4/4] Bắt đầu vòng lặp giám sát game (Watchdog)..."
(
    export DISPLAY=:1
    while true; do
        TARGET_JAR=$(ls /app/workspace/*.jar 2>/dev/null | head -n 1)
        if [ -n "$TARGET_JAR" ]; then
            echo "[GAME] Bắt đầu chạy game: $TARGET_JAR"
            java -Dfile.encoding=ISO_8859_1 -Xms128m -Xmx1G -jar /app/emulator/freej2me.jar "$TARGET_JAR" || true
        else
            echo "[GAME] Không tìm thấy file .jar nào trong /app/workspace. Đang chờ file..."
        fi
        echo "[GAME] Game đã tắt hoặc thoát. Tự động khởi chạy lại sau 3 giây..."
        sleep 3
    done
) &

# Prepare noVNC directory and UptimeRobot health-check
mkdir -p /usr/share/novnc/health
echo '{"status":"alive","game":"Ngoc Rong Online 24/24"}' > /usr/share/novnc/health/index.html

# Deploy modern UI to index.html and vnc.html
cp /app/public/index.html /usr/share/novnc/index.html
cp /app/public/index.html /usr/share/novnc/vnc.html

# Start Websockify directly on 0.0.0.0:$PORT
echo "[READY] Hệ thống đã sẵn sàng phục vụ tại cổng $PORT!"
exec websockify --web /usr/share/novnc 0.0.0.0:"$PORT" 127.0.0.1:5900
