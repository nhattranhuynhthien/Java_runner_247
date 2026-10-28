#!/bin/bash
set -e

echo "[EMU] Đang chờ Xvfb :1 sẵn sàng..."
for i in $(seq 1 30); do
    if [ -e "/tmp/.X11-unix/X1" ]; then
        echo "[EMU] Xvfb :1 đã sẵn sàng!"
        break
    fi
    sleep 0.2
done

export DISPLAY=:1

# Kill any existing orphan java processes
pkill -9 -f microemulator 2>/dev/null || true

# Background watchdog: ensures window focus & auto-kills if stuck on Launcher
(
    while true; do
        sleep 1
        WID=$(xdotool search --onlyvisible --class "java" 2>/dev/null | head -n 1 || true)
        if [ -n "$WID" ]; then
            xdotool windowfocus "$WID" 2>/dev/null || true
            WTITLE=$(xdotool getwindowname "$WID" 2>/dev/null || true)
            if [[ "$WTITLE" == *"Launcher"* ]]; then
                echo "[WATCHDOG] Cửa sổ game rơi vào màn hình Launcher. Đang tự động hồi sinh..."
                pkill -9 -f microemulator 2>/dev/null || true
            fi
        fi
    done
) &

JAR_FILE="/app/roms/NRO.jar"

if [ -f "$JAR_FILE" ]; then
    echo "[EMU] Tìm thấy game $JAR_FILE..."
    
    # Tìm MIDlet-1 main class từ MANIFEST.MF
    MIDLET_CLASS=$(python3 -c "
import zipfile
try:
    with zipfile.ZipFile('$JAR_FILE') as z:
        mf = z.read('META-INF/MANIFEST.MF').decode('utf-8', errors='ignore')
        for line in mf.splitlines():
            if line.startswith('MIDlet-1:'):
                parts = line.split(',')
                if len(parts) >= 3:
                    print(parts[2].strip())
                    break
except Exception:
    pass
" 2>/dev/null)

    # Vòng lặp giữ game chạy 24/7 (tự hồi sinh game nếu thoát ra đổi cấu hình hoặc mất mạng)
    while true; do
        echo "[EMU] Bắt đầu phiên chạy game (Tối ưu Java2D siêu mượt)..."
        echo "[EMU] Khởi chạy game từ file JAR: $JAR_FILE (--quit mode)..."
        java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false -jar /app/microemulator.jar --quit "$JAR_FILE" || true
        echo "[EMU] Game đã thoát (do đổi cấu hình hoặc tắt). Tự động khởi động lại sau 1 giây..."
        sleep 1
    done
else
    echo "[EMU] Chưa có file NRO.jar trong /app/roms. Chạy ở chế độ chờ..."
    exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false -jar /app/microemulator.jar
fi
