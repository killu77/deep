FROM python:3.10-slim-bookworm

# 安装系统依赖（合并之前能跑的 + 修复包名）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
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
    libdrm2 \
    libxshmfence1 \
    libxfixes3 \
    libxext6 \
    libdbus-1-3 \
    libnspr4 \
    libx11-6 \
    libxcb1 \
    libcairo2 \
    fonts-noto-cjk \
    fonts-unifont \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 创建非 root 用户
RUN useradd -m -u 1000 app_user
RUN mkdir -p /home/app_user/.cache && chown -R app_user:app_user /home/app_user/.cache

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Playwright 浏览器安装到共享路径
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN mkdir -p /opt/browsers && chmod 777 /opt/browsers

# 安装 Playwright Firefox
RUN python -m playwright install firefox
RUN chown -R app_user:app_user /opt/browsers

# 预安装 Camoufox
RUN su app_user -c "python -c \"from camoufox.sync_api import CamoufoxSync; print('Camoufox pre-installed')\"" 2>/dev/null \
    || echo "⚠️ Camoufox pre-install skipped, will retry at runtime"

COPY . .
RUN chown -R app_user:app_user /app

USER app_user

ENV PORT=10000
EXPOSE 10000

# 用 xvfb-run 包裹启动命令
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x720x24", "python", "app.py"]
