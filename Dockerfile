# ========================================================
# NRO Cloud 24/7 Ultra-Light Web Controller
# Headless Xvfb + MicroEmulator + Flask + Supervisor
# ========================================================

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    DISPLAY=:1

# Install OpenJDK 8 JDK (for GameBridgeAgent compilation & JRE runtime), Xvfb, xdotool, xclip, xsel, Python3, Supervisor, and X11 graphics libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-8-jdk-headless \
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

# Normalize line endings and compile GameBridgeAgent.java for Java 8 so bridge.jar never misses classes
RUN sed -i 's/\r$//' /app/scripts/start_emu.sh /app/scripts/MANIFEST.MF /etc/supervisor/conf.d/supervisord.conf 2>/dev/null || true && \
    mkdir -p /tmp/bridge_build && \
    javac -encoding UTF-8 -source 1.8 -target 1.8 -d /tmp/bridge_build /app/scripts/GameBridgeAgent.java && \
    jar cfm /app/bridge.jar /app/scripts/MANIFEST.MF -C /tmp/bridge_build . && \
    cp -f /app/bridge.jar /app/scripts/bridge.jar && \
    rm -rf /tmp/bridge_build

# Set execution permission for scripts
RUN chmod +x /app/scripts/start_emu.sh

EXPOSE 8080

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
