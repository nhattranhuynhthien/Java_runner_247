# ========================================================
# Dockerfile for Java 24/7 Cloud Runner on Hugging Face Spaces
# Base Image: Eclipse Temurin OpenJDK 21
# ========================================================

FROM eclipse-temurin:21-jdk-jammy

# Install Node.js LTS and essential tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    procps \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces standard: Non-root user with UID 1000
RUN useradd -m -u 1000 user

# Set environment variables
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860 \
    NODE_ENV=production

# Set working directory inside user home
WORKDIR /home/user/app

# Copy dependency definitions and install production packages
COPY --chown=user:user package*.json ./
RUN npm ci --only=production

# Copy application code with proper ownership
COPY --chown=user:user . .

# Ensure workspace directory exists and has full write permissions
RUN mkdir -p /home/user/app/workspace && chown -R user:user /home/user/app

# Switch to non-root user UID 1000
USER user

# Hugging Face Spaces default port
EXPOSE 7860

# Start Web Controller & Java Process Manager
CMD ["node", "server.js"]

