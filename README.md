# Fashion Scrapers Monorepo

服裝類商品爬蟲集合，自動爬取日本服裝品牌並上架到 Shopify。

## 品牌列表

| 品牌 | 目錄 | 狀態 |
|------|------|------|
| Human Made | `/humanmade` | ✅ |

## 功能特點

- 完整複製商品 Variants（顏色、尺寸等選項）
- 圖片對應 Variant
- 每個 Variant 獨立計算售價
- 自動翻譯成繁體中文
- 支援 cron-job 定時執行

## 售價公式

```
售價 = [進貨價 + (重量 × 1250)] / 0.7
```

- 重量預設：0.5kg（若無提供）
- 最低成本價門檻：¥1000

## 部署方式（Zeabur）

每個品牌獨立部署：

1. 在 Zeabur 建立新服務
2. 連接 GitHub repo
3. 設定 Root Directory 為對應品牌目錄（如 `humanmade`）
4. 設定環境變數：
   - `SHOPIFY_ACCESS_TOKEN`
   - `SHOPIFY_SHOP`
   - `OPENAI_API_KEY`

## API Endpoints

每個爬蟲都有以下 endpoints：

| Endpoint | Method | 說明 |
|----------|--------|------|
| `/` | GET | Web UI 控制面板 |
| `/api/start` | GET/POST | 啟動爬取（供 cron-job 使用）|
| `/api/status` | GET | 取得爬取狀態 |
| `/api/test-shopify` | GET | 測試 Shopify 連線 |
| `/api/test-scrape` | GET | 測試爬取（不上架）|

## cron-job.org 設定

```
URL: https://你的網域/api/start
Method: GET
執行頻率: 每日一次
```

## 目錄結構

```
fashion-scrapers/
├── README.md
├── .gitignore
└── humanmade/
    ├── app.py
    ├── requirements.txt
    └── Dockerfile
```
