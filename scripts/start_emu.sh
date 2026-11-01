#!/bin/bash
set -e

echo "[EMU] Waiting for Xvfb :1..."
for i in $(seq 1 30); do
    if [ -e "/tmp/.X11-unix/X1" ]; then
        break
    fi
    sleep 0.2
done

if [ ! -e "/tmp/.X11-unix/X1" ]; then
    echo "[EMU] Xvfb :1 did not become ready in time." >&2
    exit 1
fi

export DISPLAY=:1
JAR_FILE="${ROM_FILE:-/data/roms/NRO.jar}"
if [ ! -f "$JAR_FILE" ] && [ -f "/app/roms/NRO.jar" ]; then
    JAR_FILE="/app/roms/NRO.jar"
fi

setxkbmap -display :1 -layout us 2>/dev/null || true

if [ -f "$JAR_FILE" ]; then
    echo "[EMU] Starting game ROM: $JAR_FILE"
    exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 \
        -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false \
        -jar /app/microemulator.jar --quit "$JAR_FILE"
fi

echo "[EMU] No ROM found at $JAR_FILE; starting MicroEmulator launcher."
exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 \
    -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false \
    -jar /app/microemulator.jar
