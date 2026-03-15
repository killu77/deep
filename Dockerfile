FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 \
    libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxshmfence1 libxfixes3 libxext6 \
    libdbus-1-3 libnspr4 libnss3 \
    libgdk-pixbuf-2.0-0 libx11-6 libxcb1 \
    libxtst6 libxss1 \
    fonts-noto-cjk fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只下载浏览器二进制，不要 install-deps
RUN python -m playwright install firefox

COPY . .

ENV PORT=10000
ENV HEADLESS=true
ENV USE_CAMOUFOX=false

EXPOSE 10000

CMD ["python", "app.py"]
