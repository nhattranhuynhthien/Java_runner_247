#!/bin/bash
set -e

# Keep MicroEmulator configuration and RMS data on persistent /data volume,
# falling back cleanly to /tmp when /data is read-only or unavailable.
EMU_HOME="${MICROEMULATOR_HOME:-/data/.microemulator}"
if ! mkdir -p "$EMU_HOME" 2>/dev/null || [ ! -w "$EMU_HOME" ]; then
    echo "[EMU] WARNING: $EMU_HOME is not writable; falling back to /tmp/nro-data/.microemulator" >&2
    EMU_HOME="/tmp/nro-data/.microemulator"
    mkdir -p "$EMU_HOME"
fi
EMU_USER_HOME="$(dirname "$EMU_HOME")"

if [ -d "/root/.microemulator" ] && [ ! -L "/root/.microemulator" ]; then
    cp -rn /root/.microemulator/. "$EMU_HOME/" 2>/dev/null || true
fi
if [ -f "/app/config2.xml" ]; then
    cp -f /app/config2.xml "$EMU_HOME/config2.xml" 2>/dev/null || true
fi
export HOME="$EMU_USER_HOME"
export MICROEMULATOR_HOME="$EMU_HOME"

echo "[EMU] Waiting for Xvfb :1..."
for i in $(seq 1 50); do
    if [ -e "/tmp/.X11-unix/X1" ]; then
        break
    fi
    sleep 0.2
done

if [ ! -e "/tmp/.X11-unix/X1" ]; then
    if ! pgrep -f "Xvfb :1" >/dev/null 2>&1; then
        echo "[EMU] Cleaning stale X1 lock and starting fallback Xvfb :1..."
        rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
        /usr/bin/Xvfb :1 -screen 0 240x384x24 -ac -nolisten tcp >/dev/null 2>&1 &
    fi
    for i in $(seq 1 25); do
        if [ -e "/tmp/.X11-unix/X1" ]; then
            break
        fi
        sleep 0.2
    done
fi

if [ ! -e "/tmp/.X11-unix/X1" ]; then
    echo "[EMU] ERROR: Xvfb :1 socket /tmp/.X11-unix/X1 did not become ready in time." >&2
    exit 1
fi

export DISPLAY=:1
JAR_FILE="${ROM_FILE:-/data/roms/NRO.jar}"
if [ ! -f "$JAR_FILE" ] && [ -f "/app/roms/NRO.jar" ]; then
    JAR_FILE="/app/roms/NRO.jar"
fi

setxkbmap -display :1 -layout us 2>/dev/null || true

# Locate precompiled Java 8 GameBridgeAgent jar
AGENT_JAR=""
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
        AGENT_JAR="$bpath"
        break
    fi
done

if [ -z "$AGENT_JAR" ]; then
    echo "[EMU] WARNING: bridge.jar not found in any standard directory! GameBridge HTTP API will not start." >&2
fi

JAVA_XMX="${JAVA_XMX:-220m}"
JAVA_XMS="${JAVA_XMS:-64m}"
JAVA_OPTS=(
    "-Xmx${JAVA_XMX}"
    "-Xms${JAVA_XMS}"
    "-XX:+UseSerialGC"
    "-XX:TieredStopAtLevel=1"
    "-XX:MaxMetaspaceSize=96m"
    "-Xss512k"
    "-XX:MinHeapFreeRatio=15"
    "-XX:MaxHeapFreeRatio=35"
    "-Duser.home=${EMU_USER_HOME}"
    "-Dsun.java2d.opengl=false"
    "-Dsun.java2d.pmoffscreen=false"
    "-Dsun.java2d.xrender=false"
)
if [ -n "$AGENT_JAR" ]; then
    JAVA_OPTS+=("-javaagent:${AGENT_JAR}")
fi

if [ -f "$JAR_FILE" ]; then
    # Auto-confirm Launcher Start in case MicroEmulator stops at the MIDlet selection screen.
    # Tracks EMU_PID (which becomes the exec'd Java process) so the helper exits immediately if Java stops.
    EMU_PID="$$"
    (
        set +e
        sleep 2
        for i in $(seq 1 35); do
            kill -0 "$EMU_PID" 2>/dev/null || break
            sleep 1
            kill -0 "$EMU_PID" 2>/dev/null || break

            STATUS_JSON="$(curl -s -m 1 http://127.0.0.1:28888/api/game/status 2>/dev/null || true)"
            if printf '%s' "$STATUS_JSON" | grep -Eq '"midlet_loaded"[[:space:]]*:[[:space:]]*true'; then
                break
            fi

            LAUNCH_JSON="$(curl -s -m 1 http://127.0.0.1:28888/api/game/auto_launch 2>/dev/null || true)"
            if printf '%s' "$LAUNCH_JSON" | grep -Eq '"midlet_loaded"[[:space:]]*:[[:space:]]*true'; then
                break
            fi

            # Only fall back to X11 clicks after giving GameBridgeAgent's own watchdog time to auto-launch cleanly
            if [ "$i" -ge 5 ] && command -v xdotool >/dev/null 2>&1; then
                WID="$(DISPLAY=:1 xdotool search --onlyvisible --name "MicroEmulator" 2>/dev/null | tail -n 1)"
                if [ -z "$WID" ]; then
                    WID="$(DISPLAY=:1 xdotool search --onlyvisible --class "org-microemu-app-Main" 2>/dev/null | tail -n 1)"
                fi
                if [ -n "$WID" ]; then
                    DISPLAY=:1 xdotool windowfocus "$WID" 2>/dev/null || true
                    DISPLAY=:1 xdotool mousemove 160 232 click 1 2>/dev/null || true
                    DISPLAY=:1 xdotool key F2 Return 2>/dev/null || true
                fi
            fi
        done
    ) &

    echo "[EMU] Starting game ROM: $JAR_FILE"
    exec java "${JAVA_OPTS[@]}" -jar /app/microemulator.jar --rms file "$JAR_FILE"
fi

echo "[EMU] No ROM found at $JAR_FILE; starting MicroEmulator launcher."
exec java "${JAVA_OPTS[@]}" -jar /app/microemulator.jar --rms file
