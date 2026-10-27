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

# Background watcher to ensure window focus
(
    sleep 3
    WID=$(xdotool search --onlyvisible --class "java" 2>/dev/null | head -n 1 || true)
    if [ -n "$WID" ]; then
        xdotool windowfocus "$WID" 2>/dev/null || true
        echo "[EMU] Đã kích hoạt focus cửa sổ game: $WID"
    fi
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

    # Vòng lặp giữ game chạy 24/7 (tự hồi sinh nếu game ngắt kết nối hoặc văng)
    while true; do
        if [ -n "$MIDLET_CLASS" ]; then
            echo "[EMU] Khởi chạy trực tiếp MIDlet: $MIDLET_CLASS"
            java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --appclasspath "$JAR_FILE" "$MIDLET_CLASS" || true
        else
            echo "[EMU] Khởi chạy file JAR mặc định..."
            java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar "$JAR_FILE" || true
        fi
        echo "[EMU] Game đã dừng. Tự động khởi động lại sau 2 giây..."
        sleep 2
    done
else
    echo "[EMU] Chưa có file NRO.jar trong /app/roms. Chạy ở chế độ chờ..."
    exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar
fi
