FROM python:3.10-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    procps \
    xvfb \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libxt6 \
    libasound2 \
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
    fonts-liberation \
    fonts-noto-cjk \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# 安装 Playwright 浏览器（Firefox）和 Camoufox
RUN python -m playwright install firefox --with-deps
RUN python -c "import camoufox; camoufox.sync_playwright()" 2>/dev/null || true
RUN python -c "from camoufox.sync_api import CamoufoxSync; print('Camoufox ready')" 2>/dev/null || echo "Will download on first run"

COPY . .

RUN useradd -m -u 1000 app_user && chown -R app_user:app_user /app
USER app_user

EXPOSE 7860

CMD ["python", "app.py"]
