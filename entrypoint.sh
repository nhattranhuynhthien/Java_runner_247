#!/bin/bash
set -e

PORT=${PORT:-7860}
export DISPLAY=:1
export PORT

echo "=========================================================="
echo "🚀 Khởi động Cloud Gaming Ngọc Rồng Online 24/24..."
echo "📡 Cổng phục vụ Nginx: $PORT"
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
echo "[1/6] Khởi động Màn hình ảo (Xvfb :1)..."
Xvfb :1 -screen 0 480x640x24 -nolisten tcp &
sleep 1

# 3. Start Lightweight Window Manager (Fluxbox)
echo "[2/6] Khởi động Window Manager (Fluxbox)..."
fluxbox &
sleep 1

# 4. Start VNC Server (x11vnc) with zero latency
echo "[3/6] Khởi động x11vnc (Port 5900)..."
x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -nowf -noxdamage -repeat -wait 10 -q &
sleep 1

# 5. Start Websockify internal bridge (Port 6080)
echo "[4/6] Khởi động Websockify (Internal Port 6080)..."
websockify 6080 127.0.0.1:5900 &
sleep 1

# 6. Start Web File Uploader API (Internal Port 5000)
echo "[5/6] Khởi động Uploader API (Internal Port 5000)..."
python3 /app/uploader.py &
sleep 1

# 7. Start Game Watchdog Loop (Keeps game running in background 24/7)
echo "[6/6] Bắt đầu phiên treo game Ngọc Rồng 24/24..."
(
    export DISPLAY=:1
    while true; do
        TARGET_JAR=$(ls /app/workspace/*.jar 2>/dev/null | head -n 1)
        if [ -n "$TARGET_JAR" ]; then
            echo "[GAME] Bắt đầu chạy game: $TARGET_JAR"
            java -Dfile.encoding=ISO_8859_1 -Xms128m -Xmx1G -jar /app/emulator/freej2me.jar "$TARGET_JAR" || true
        fi
        echo "[GAME] Game đã tắt hoặc vừa được đổi file. Tự động khởi chạy lại sau 2 giây..."
        sleep 2
    done
) &

# Deploy modern UI directly to noVNC web directory
cp /app/public/index.html /usr/share/novnc/index.html
cp /app/public/index.html /usr/share/novnc/vnc.html

# Generate Nginx configuration from template replacing $PORT
envsubst '${PORT}' < /app/nginx.conf.template > /etc/nginx/nginx.conf

# Start Nginx in foreground on $PORT
echo "[READY] Nginx reverse proxy đã sẵn sàng phục vụ tại cổng $PORT!"
exec nginx -g "daemon off;"
