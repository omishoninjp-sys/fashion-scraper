FROM python:3.11-slim

# 安裝 Playwright/Chromium 系統依賴（手動安裝，避免 install-deps 套件名過時）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libxshmfence1 libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    libdrm2 libxkbcommon0 libatspi2.0-0 libwayland-client0 \
    fonts-noto-cjk fonts-unifont \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只安裝 Chromium（不用 install-deps，依賴已手動裝好）
RUN playwright install chromium

COPY . .

ENV PORT=8080
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "600", "--workers", "1", "--threads", "4", "app:app"]
