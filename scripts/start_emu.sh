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

# Background auto-trigger to bypass launcher menu or initial modal if needed
(
    sleep 3
    for k in 1 2 3 4 5; do
        if xdotool search --name "MicroEmulator" >/dev/null 2>&1; then
            echo "[AUTO-LAUNCH] Gửi tín hiệu F2 & Enter..."
            xdotool key F2 2>/dev/null || true
            xdotool mousemove 180 340 click 1 2>/dev/null || true
            xdotool key Return 2>/dev/null || true
        fi
        sleep 2
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

    if [ -n "$MIDLET_CLASS" ]; then
        echo "[EMU] Khởi chạy trực tiếp MIDlet: $MIDLET_CLASS"
        exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --impl org.microemu.device.j2se.J2SEDevice --appclasspath "$JAR_FILE" "$MIDLET_CLASS"
    else
        echo "[EMU] Khởi chạy file JAR mặc định..."
        exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --impl org.microemu.device.j2se.J2SEDevice "$JAR_FILE"
    fi
else
    echo "[EMU] Chưa có file NRO.jar trong /app/roms. Chạy ở chế độ chờ..."
    exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --impl org.microemu.device.j2se.J2SEDevice
fi
