# Human Made 爬蟲 v3.0 部署指南

## 改版原因
humanmade.jp 已在 2026/2/17 從 Shopify 遷移到自建平台：
- 舊 URL: `humanmade.jp/products/{handle}` → Shopify JSON API
- 新 URL: `www.humanmade.jp/all/{ITEM_ID}.html` → 自建平台 + WAF 防護

舊版爬蟲的 `products.json` API 已不存在，必須使用 Playwright headless browser。

## 新增依賴
- **Playwright** + Chromium（真實瀏覽器引擎）
- Docker image 較大（~800MB，含 Chromium）
- 記憶體建議 >= 1GB

## 部署步驟

### 方法一：Docker（推薦）
```bash
# 建構（第一次會比較久，需要下載 Chromium）
docker build -t humanmade-scraper-v3 .

# 執行
docker run -d \
  -p 8080:8080 \
  -e SHOPIFY_ACCESS_TOKEN=your_token \
  -e SHOPIFY_SHOP=your-shop.myshopify.com \
  -e OPENAI_API_KEY=your_key \
  humanmade-scraper-v3
```

### 方法二：VPS 直接部署
```bash
# 安裝系統依賴
sudo apt-get update
sudo apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
  libpango-1.0-0 libcairo2 libasound2 fonts-noto-cjk

# 安裝 Python 依賴
pip install -r requirements.txt

# 安裝 Playwright 瀏覽器
playwright install chromium
playwright install-deps chromium

# 設定環境變數
export SHOPIFY_ACCESS_TOKEN=your_token
export SHOPIFY_SHOP=your-shop.myshopify.com
export OPENAI_API_KEY=your_key

# 執行
python app.py
```

## 使用方式
1. 開啟 `http://your-server:8080`
2. 先按「測試連線」確認 Shopify 連線
3. 按「🔍 測試爬取（前 3 個）」確認爬蟲正常（會啟動 Chromium）
4. 確認沒問題後按「🚀 開始爬取」

## 安全機制
- 如果爬到的商品數量少於 10 個，會跳過刪除步驟（防止網站故障時誤刪）
- 這個門檻可在 `app.py` 的 `MIN_PRODUCTS_FOR_CLEANUP` 修改

## 注意事項
- Playwright 第一次啟動較慢（~10 秒）
- 每個商品頁面爬取間隔 1.5 秒，避免被封
- gunicorn timeout 設為 600 秒（爬蟲可能跑很久）
- 建議 VPS 至少 1GB RAM，2GB 更佳

## 已知限制
- 新網站可能使用 JS 動態載入，selector 可能需要依實際 HTML 調整
- 第一次部署後建議用「測試爬取」功能確認 selector 是否正確
- 如果網站再次改版，需要更新 `scrape_product_page()` 中的 selector
