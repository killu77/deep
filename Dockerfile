FROM python:3.10-slim

# 安装系统依赖（Playwright Firefox 所需的全部运行时库）
# 注意：Debian Bookworm 中部分包已改名，这里使用正确的包名
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
    # Firefox 核心依赖
    libdbus-glib-1-2 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libasound2 \
    libxss1 \
    libxtst6 \
    libxi6 \
    libnss3 \
    libnspr4 \
    libxcursor1 \
    libxfixes3 \
    libxrender1 \
    libfontconfig1 \
    libfreetype6 \
    libharfbuzz0b \
    # Bookworm 中改名的包（替代 libgdk-pixbuf2.0-0, ttf-unifont, ttf-ubuntu-font-family）
    libgdk-pixbuf-2.0-0 \
    fonts-unifont \
    fonts-ubuntu \
    fonts-noto-color-emoji \
    fonts-liberation \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    # 其他可能需要的
    libdrm2 \
    libxshmfence1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN useradd -m -u 1000 app_user

WORKDIR /app

# 复制 requirements.txt 并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# ===== 核心：预安装 Playwright Firefox（默认引擎）=====
# 只运行 install 下载浏览器二进制，不运行 install-deps（依赖已在上面手动装好）
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN python -m playwright install firefox \
    && chown -R app_user:app_user /opt/browsers

# ===== 可选：尝试预下载 Camoufox（失败不影响启动）=====
RUN mkdir -p /home/app_user/.cache && chown -R app_user:app_user /home/app_user/.cache
USER app_user
ENV CAMOUFOX_NO_UPDATE_CHECK=1
RUN python -c "import camoufox; camoufox.fetch()" 2>/dev/null \
    || echo "⚠️ Camoufox 预下载跳过，运行时将使用 Playwright Firefox"

# 切换回 root 复制应用代码
USER root
COPY --chown=app_user:app_user . .

# 设置默认环境变量（API_SECRET_KEY 建议通过 Secrets 覆盖，不要硬编码敏感值）
ENV HEADLESS=true

# 最终以 app_user 运行
USER app_user

EXPOSE 7860
CMD ["python", "app.py"]
