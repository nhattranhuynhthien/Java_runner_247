#!/bin/bash
set -e

# Keep MicroEmulator configuration and RMS data on the persistent /data volume.
EMU_HOME="${MICROEMULATOR_HOME:-/data/.microemulator}"
EMU_USER_HOME="$(dirname "$EMU_HOME")"
mkdir -p "$EMU_HOME"
if [ -d "/root/.microemulator" ] && [ ! -L "/root/.microemulator" ]; then
    cp -an /root/.microemulator/. "$EMU_HOME/"
fi
if [ ! -f "$EMU_HOME/config2.xml" ] && [ -f "/app/config2.xml" ]; then
    cp /app/config2.xml "$EMU_HOME/config2.xml"
fi
export HOME="$EMU_USER_HOME"

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

# Check if GameBridgeAgent is available
AGENT_OPT=""
if [ -f "/app/bridge.jar" ]; then
    echo "[EMU] Loading GameBridgeAgent from /app/bridge.jar"
    AGENT_OPT="-javaagent:/app/bridge.jar"
elif [ -f "./bridge.jar" ]; then
    echo "[EMU] Loading GameBridgeAgent from ./bridge.jar"
    AGENT_OPT="-javaagent:./bridge.jar"
fi

if [ -f "$JAR_FILE" ]; then
    echo "[EMU] Starting game ROM: $JAR_FILE"
    exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 \
        -Duser.home="$EMU_USER_HOME" \
        -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false \
        $AGENT_OPT \
        -jar /app/microemulator.jar --quit "$JAR_FILE"
fi

echo "[EMU] No ROM found at $JAR_FILE; starting MicroEmulator launcher."
exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 \
    -Duser.home="$EMU_USER_HOME" \
    -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false \
    $AGENT_OPT \
    -jar /app/microemulator.jar
