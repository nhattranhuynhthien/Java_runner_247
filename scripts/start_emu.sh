#!/bin/bash
set -e

# Keep MicroEmulator configuration and RMS data on the persistent /data volume.
EMU_HOME="${MICROEMULATOR_HOME:-/data/.microemulator}"
EMU_USER_HOME="$(dirname "$EMU_HOME")"
mkdir -p "$EMU_HOME" || true
if [ -d "/root/.microemulator" ] && [ ! -L "/root/.microemulator" ]; then
    cp -rn /root/.microemulator/. "$EMU_HOME/" 2>/dev/null || true
fi
if [ -f "/app/config2.xml" ]; then
    cp -f /app/config2.xml "$EMU_HOME/config2.xml" 2>/dev/null || true
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
    echo "[EMU] Cleaning stale X1 lock and starting Xvfb :1..."
    rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
    /usr/bin/Xvfb :1 -screen 0 240x384x24 -ac -nolisten tcp >/dev/null 2>&1 &
    for i in $(seq 1 25); do
        if [ -e "/tmp/.X11-unix/X1" ]; then
            break
        fi
        sleep 0.2
    done
fi

export DISPLAY=:1
JAR_FILE="${ROM_FILE:-/data/roms/NRO.jar}"
if [ ! -f "$JAR_FILE" ] && [ -f "/app/roms/NRO.jar" ]; then
    JAR_FILE="/app/roms/NRO.jar"
fi

setxkbmap -display :1 -layout us 2>/dev/null || true

# Locate precompiled Java 8 GameBridgeAgent jar
AGENT_OPT=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for bpath in \
    "/app/bridge.jar" \
    "/app/scripts/bridge.jar" \
    "$SCRIPT_DIR/../bridge.jar" \
    "$SCRIPT_DIR/bridge.jar" \
    "./bridge.jar" \
    "./scripts/bridge.jar" \
    "/data/bridge.jar"; do
    if [ -f "$bpath" ]; then
        echo "[EMU] Loading GameBridgeAgent from $bpath"
        AGENT_OPT="-javaagent:$bpath"
        break
    fi
done

if [ -z "$AGENT_OPT" ]; then
    echo "[EMU] WARNING: bridge.jar not found in any standard directory! GameBridge HTTP API will not start." >&2
fi

# Auto-confirm Launcher Start in case MicroEmulator stops at the MIDlet selection screen
(
    sleep 2
    for i in $(seq 1 35); do
        sleep 1
        STATUS_JSON="$(curl -s -m 1 http://127.0.0.1:28888/api/game/status 2>/dev/null || true)"
        if echo "$STATUS_JSON" | grep -q '"midlet_loaded":true'; then
            break
        fi
        curl -s -m 1 http://127.0.0.1:28888/api/game/auto_launch >/dev/null 2>&1 || true
        if command -v xdotool >/dev/null 2>&1; then
            WID=$(DISPLAY=:1 xdotool search --onlyvisible --name "MicroEmulator" 2>/dev/null | tail -n 1)
            if [ -z "$WID" ]; then
                WID=$(DISPLAY=:1 xdotool search --onlyvisible --class "org-microemu-app-Main" 2>/dev/null | tail -n 1)
            fi
            if [ -n "$WID" ]; then
                DISPLAY=:1 xdotool windowfocus "$WID" 2>/dev/null || true
                DISPLAY=:1 xdotool mousemove 160 232 click 1 2>/dev/null || true
                DISPLAY=:1 xdotool key F2 Return 2>/dev/null || true
            fi
        fi
    done
) &

if [ -f "$JAR_FILE" ]; then
    echo "[EMU] Starting game ROM: $JAR_FILE"
    exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 \
        -Duser.home="$EMU_USER_HOME" \
        -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false \
        $AGENT_OPT \
        -jar /app/microemulator.jar --rms file "$JAR_FILE"
fi

echo "[EMU] No ROM found at $JAR_FILE; starting MicroEmulator launcher."
exec java -Xmx220m -Xms96m -XX:+UseSerialGC -XX:TieredStopAtLevel=1 \
    -Duser.home="$EMU_USER_HOME" \
    -Dsun.java2d.opengl=false -Dsun.java2d.pmoffscreen=false -Dsun.java2d.xrender=false \
    $AGENT_OPT \
    -jar /app/microemulator.jar --rms file
