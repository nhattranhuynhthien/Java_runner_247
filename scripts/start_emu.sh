#!/bin/bash
set -e

echo "[EMU] Đang chờ Xvfb :1 khởi động..."
sleep 2

export DISPLAY=:1

if [ -f "/app/roms/NRO.jar" ]; then
    echo "[EMU] Tìm thấy game /app/roms/NRO.jar. Đang khởi chạy MicroEmulator..."
    exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --impl org.microemu.device.j2se.J2SEDevice /app/roms/NRO.jar
else
    echo "[EMU] Không tìm thấy /app/roms/NRO.jar. Khởi chạy MicroEmulator ở chế độ chờ nạp game..."
    exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --impl org.microemu.device.j2se.J2SEDevice
fi
