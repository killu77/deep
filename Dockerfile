# 使用 Python 3.10 轻量镜像
FROM python:3.10-slim

# 安装系统依赖（浏览器运行所需库和工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
    unzip \
    libdbus-glib-1-2 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libasound2 \
    libxss1 \
    libxtst6 \
    libxi6 \
    libnss3 \
    libxcursor1 \
    libgdk-pixbuf-2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN useradd -m -u 1000 app_user

WORKDIR /app

# 创建缓存目录并设置权限
RUN mkdir -p /home/app_user/.cache /opt/browsers \
    && chown -R app_user:app_user /home/app_user/.cache /opt/browsers

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

ENV CAMOUFOX_NO_UPDATE_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

# 预安装 Playwright Firefox
RUN python -m playwright install firefox \
    && chown -R app_user:app_user /opt/browsers

# ===== 修正后的手动下载 Camoufox =====
# 直接使用正确的下载链接（去掉 v 前缀）
RUN wget -O /tmp/camoufox.zip https://github.com/daijro/camoufox/releases/download/v135.0.1-beta.24/camoufox-135.0.1-beta.24-lin.x86_64.zip \
    && unzip /tmp/camoufox.zip -d /home/app_user/.cache/camoufox/ \
    && rm /tmp/camoufox.zip \
    && chown -R app_user:app_user /home/app_user/.cache/camoufox \
    && echo "Camoufox 预下载完成，缓存目录: /home/app_user/.cache/camoufox"

# 可选：验证文件
RUN ls -la /home/app_user/.cache/camoufox

COPY --chown=app_user:app_user . .

USER app_user

EXPOSE 7860

CMD ["python", "app.py"]
