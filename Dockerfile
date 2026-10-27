# ========================================================
# Dockerfile: Ngọc Rồng Online 24/24 Cloud Player (Nginx + noVNC + J2ME)
# ========================================================

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PORT=7860 \
    DISPLAY=:1

# Install OpenJDK, Xvfb virtual display, Fluxbox, x11vnc, noVNC, Nginx, and Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jre \
    xvfb \
    fluxbox \
    x11vnc \
    novnc \
    websockify \
    nginx \
    gettext-base \
    python3 \
    ca-certificates \
    procps \
    xdotool \
    wmctrl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY . .

# Set permissions
RUN chmod +x /app/entrypoint.sh

EXPOSE 7860

CMD ["/bin/bash", "/app/entrypoint.sh"]
