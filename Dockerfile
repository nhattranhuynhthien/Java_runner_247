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
    procps \
    curl \
    supervisor \
    libxcb1 \
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

# Download MicroEmulator 2.0.4 and verify SHA-1 integrity
RUN curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 120 \
    https://repo1.maven.org/maven2/org/microemu/microemulator/2.0.4/microemulator-2.0.4.jar \
    -o /app/microemulator.jar && \
    echo "b4f230a8e7ff0fd3a79827c5528838bf0f0037fd  /app/microemulator.jar" | sha1sum -c -

# Install Python dependencies (Pillow 11.0.0 manylinux wheel bundles WebP/JPEG/XCB support for Python 3.10)
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Pre-configure MicroEmulator borderless 240x360
RUN mkdir -p /root/.microemulator
COPY config2.xml /root/.microemulator/config2.xml

# Configure Supervisord
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Copy project files
COPY . .

# Normalize line endings, verify bridge.jar in both standard locations, and set permissions
RUN sed -i 's/\r$//' /app/scripts/start_emu.sh /app/scripts/MANIFEST.MF /app/config2.xml /root/.microemulator/config2.xml /etc/supervisor/conf.d/supervisord.conf && \
    if [ -f /app/bridge.jar ]; then \
        cp -f /app/bridge.jar /app/scripts/bridge.jar; \
    elif [ -f /app/scripts/bridge.jar ]; then \
        cp -f /app/scripts/bridge.jar /app/bridge.jar; \
    else \
        echo "ERROR: bridge.jar missing from build context" >&2 && exit 1; \
    fi && \
    chmod +x /app/scripts/start_emu.sh && \
    chmod -R a+rX /app/roms

# Create a dedicated non-root user for future privilege drop while ensuring /data is writable
RUN groupadd -r nro && useradd -r -g nro -d /data -s /sbin/nologin nro && \
    chown -R nro:nro /data && chmod 755 /data /data/roms /data/.microemulator

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" >/dev/null || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
