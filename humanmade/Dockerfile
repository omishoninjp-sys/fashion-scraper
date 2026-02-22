FROM python:3.11-slim

# 安裝 Playwright/Chromium 系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libxshmfence1 libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    fonts-noto-cjk wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安裝 Playwright Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

ENV PORT=8080
# Playwright 需要更多記憶體和 timeout
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "600", "--workers", "1", "--threads", "4", "app:app"]
