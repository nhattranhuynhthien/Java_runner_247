#!/bin/bash
set -e

PORT=${PORT:-7860}
export DISPLAY=:1
export PORT

echo "=========================================================="
echo "🚀 Khởi động Cloud Gaming Ngọc Rồng Online 24/24..."
echo "📡 Cổng phục vụ Nginx: $PORT"
echo "=========================================================="

# 1. Setup External Input & Str Directory for Direct Input Ingestion
mkdir -p /str
touch /str/FreeJ2MEExternalKeyEvents.txt
chmod 777 /str/FreeJ2MEExternalKeyEvents.txt

# Setup FreeJ2ME Keycodes & High-Res Display Config
mkdir -p /app/freej2me_system
cat << 'EOF' > /app/freej2me_system/freej2me.conf
M3GUntextured:off
M3GWireframe:off
deleteTempKJXFiles:on
dumpAudioStreams:off
dumpGraphicsObjects:off
fpsCounterPosition:Off
input_ArrowDown:40
input_ArrowLeft:37
input_ArrowRight:39
input_ArrowUp:38
input_CLR:65
input_FastForward:32
input_Fire:10
input_LeftSoft:81
input_Num0:48
input_Num1:49
input_Num2:50
input_Num3:51
input_Num4:52
input_Num5:53
input_Num6:54
input_Num7:55
input_Num8:56
input_Num9:57
input_PauseResume:88
input_Pound:82
input_RightSoft:87
input_Screenshot:67
input_Star:69
logLevel:2
EOF

mkdir -p /app/config/DragonBoy
cat << 'EOF' > /app/config/DragonBoy/game.conf
backlightcolor:Disabled
compatfantasyzonefix:off
compatignorevolumechanges:off
compatimmediaterepaints:off
compatoverrideplatchecks:on
compatsiemensfriendlydrawing:off
compattranstooriginonreset:off
dojaversion:200
fontoffset:0
fps:0
fpshack:Disabled
phone:Standard
rotate:0
scrheight:320
scrwidth:240
sound:on
soundfont:Default
spdhackm3ghalfres:off
spdhacknoalpha:off
textfont:Default
EOF

# 2. Setup Fluxbox: Hide Taskbar, Remove Borders, Auto-Maximize
mkdir -p /root/.fluxbox
cat << 'EOF' > /root/.fluxbox/init
session.screen0.toolbar.visible: false
session.screen0.focusModel: AutoRaise
session.screen0.autoRaise: true
session.focusModel: ClickToFocus
session.appsFile: ~/.fluxbox/apps
EOF

cat << 'EOF' > /root/.fluxbox/apps
[app] (name=.*)
  [Deco] {NONE}
  [Maximized] {yes}
[end]
[app] (title=.*)
  [Deco] {NONE}
  [Maximized] {yes}
[end]
EOF

# 3. Start Virtual Display (Xvfb :1) - 480x640 optimal for mobile & PC
echo "[1/7] Khởi động Màn hình ảo (Xvfb :1)..."
Xvfb :1 -screen 0 480x640x24 -nolisten tcp &
sleep 1

# 4. Start Window Manager (Fluxbox)
echo "[2/7] Khởi động Window Manager (Fluxbox)..."
fluxbox &
sleep 1

# 5. Start VNC Server (x11vnc) with clean coordinates and instant repeats
echo "[3/7] Khởi động x11vnc (Port 5900)..."
x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -wait 5 -repeat -q &
sleep 1

# 6. Start Websockify internal bridge (Port 6080)
echo "[4/7] Khởi động Websockify (Internal Port 6080)..."
websockify 6080 127.0.0.1:5900 &
sleep 1

# 7. Start Web Input & File Uploader API (Internal Port 5000)
echo "[5/7] Khởi động Uploader & Input Ingestion API (Internal Port 5000)..."
python3 /app/uploader.py &
sleep 1

# 8. Start Focus & Fullscreen Watchdog (ensures game is permanently focused & fills screen)
echo "[6/7] Bật giám sát tự động khóa Focus & Toàn màn hình..."
(
    while true; do
        WID=$(xdotool search --name "FreeJ2ME" 2>/dev/null | head -n 1 || true)
        if [ -n "$WID" ]; then
            wmctrl -i -r "$WID" -b add,maximized_vert,maximized_horz 2>/dev/null || true
            xdotool windowactivate "$WID" 2>/dev/null || true
            xdotool windowfocus "$WID" 2>/dev/null || true
        fi
        sleep 2
    done
) &

# 9. Start Game Watchdog Loop (Keeps game running in background 24/7 at 480x640)
echo "[7/7] Bắt đầu phiên treo game Ngọc Rồng 24/24..."
(
    export DISPLAY=:1
    while true; do
        TARGET_JAR=$(ls /app/workspace/*.jar 2>/dev/null | head -n 1)
        if [ -n "$TARGET_JAR" ]; then
            echo "[GAME] Bắt đầu chạy game: $TARGET_JAR (Native 240x320 scaled 2x to 480x640)"
            java -Dfile.encoding=ISO_8859_1 -Xms128m -Xmx1G -jar /app/emulator/freej2me.jar "$TARGET_JAR" 240 320 2 0 || true
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
