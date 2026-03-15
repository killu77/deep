# 锁定 bookworm，避免 trixie 包名不兼容问题
FROM python:3.11-slim-bookworm

# 1. 系统依赖（合并 Playwright Firefox 所需的依赖，跳过 install-deps）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    # Firefox / Camoufox 运行时依赖
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxshmfence1 libxfixes3 libxext6 \
    # Playwright Firefox 额外需要的
    libdbus-1-3 libnspr4 libnss3 \
    libgdk-pixbuf-2.0-0 libx11-6 libxcb1 \
    libxtst6 libxss1 \
    # 字体（中文支持）
    fonts-noto-cjk fonts-unifont fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Python 依赖（只要 requirements.txt 不变就缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. 预装浏览器（重量级，单独一层缓存）
#    注意：只 install，不 install-deps（系统依赖已在上面手动装好）
RUN python -m camoufox fetch \
    && python -m playwright install firefox

# 4. 最后才拷贝代码（改代码不会触发前面的层重建）
COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["python", "app.py"]
