FROM python:3.11-slim

# 1. 系统依赖（最不常变，放最上面利用缓存）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxshmfence1 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Python 依赖（只要 requirements.txt 不变就缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. 预装浏览器（重量级，单独一层缓存）
RUN python -m camoufox fetch \
    && python -m playwright install firefox \
    && python -m playwright install-deps firefox

# 4. 最后才拷贝代码（改代码不会触发前面的层重建）
COPY . .

# 5. 使用与 Render 兼容的端口
ENV PORT=10000
EXPOSE 10000

CMD ["python", "app.py"]
