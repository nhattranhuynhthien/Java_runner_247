# ========================================================
# NRO Cloud 24/7 Ultra-Light Web Controller
# Headless Xvfb + MicroEmulator + Flask + Supervisor
# ========================================================

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    DISPLAY=:1

# Install OpenJDK 8 JRE, Xvfb, xdotool, xclip, xsel, Python3, Supervisor, and X11 graphics libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-8-jre \
    xvfb \
    xdotool \
    xclip \
    xsel \
    x11-xkb-utils \
    python3 \
    python3-pip \
    python3-pil \
    procps \
    curl \
    supervisor \
    libxext6 \
    libxrender1 \
    libxtst6 \
    libxi6 \
    libfreetype6 \
    fontconfig \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /data/roms /data/.microemulator

# Download MicroEmulator 2.0.4
RUN curl -L https://repo1.maven.org/maven2/org/microemu/microemulator/2.0.4/microemulator-2.0.4.jar -o /app/microemulator.jar

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Pre-configure MicroEmulator borderless 240x360
RUN mkdir -p /root/.microemulator
COPY config2.xml /root/.microemulator/config2.xml

# Configure Supervisord
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Copy project files
COPY . .

# Ensure bridge.jar is present in both /app/bridge.jar and /app/scripts/bridge.jar
RUN cp -f /app/bridge.jar /app/scripts/bridge.jar 2>/dev/null || cp -f /app/scripts/bridge.jar /app/bridge.jar 2>/dev/null || true

# Set execution permission for scripts
RUN chmod +x /app/scripts/start_emu.sh

EXPOSE 8080

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
