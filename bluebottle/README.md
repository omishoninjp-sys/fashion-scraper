# Blue Bottle Coffee Japan 爬蟲 (Zeabur 版)

將 [Blue Bottle Coffee Japan](https://store.bluebottlecoffee.jp/) 商品自動同步至你的 Shopify 商店。

## 架構

```
store.bluebottlecoffee.jp (Shopify)
        ↓ /products.json
   [Express + node-cron on Zeabur]
        ↓ OpenAI 翻譯 (日文→繁中)
  your-store.myshopify.com
```

## 專案結構

```
bluebottle-scraper/
├── index.js              ← Express 入口 (Zeabur 啟動點)
├── lib/
│   ├── crawler.js        ← 爬蟲核心邏輯
│   ├── price-tool.js     ← 價格計算 JPY→TWD
│   └── logger.js         ← 記憶體日誌
├── package.json
└── .env.example
```

## API 端點

| Method | Path | 說明 |
|--------|------|------|
| GET | `/` | 服務狀態 (排程/上次同步/結果) |
| GET | `/health` | 健康檢查 (Zeabur 用) |
| POST | `/sync` | 手動觸發完整同步 |
| POST | `/fetch-only` | 只抓取來源商品 (測試用) |
| POST | `/price-update` | 更新所有商品價格 |
| GET | `/logs` | 查看最近日誌 |

POST 端點需帶 `x-api-key` header 或 `?key=` query。

## Zeabur 部署步驟

### 1. 推到 GitHub

```bash
cd bluebottle-scraper
git init
git add .
git commit -m "Blue Bottle Coffee scraper"
git remote add origin https://github.com/你的帳號/bluebottle-scraper.git
git push -u origin main
```

### 2. Zeabur 建立服務

1. Zeabur Dashboard → 建立 Project
2. Add Service → Deploy from GitHub → 選 bluebottle-scraper repo
3. Zeabur 會自動偵測 Node.js 並部署

### 3. 設定環境變數

在 Zeabur Dashboard → Service → **Variables** 加入：

| Variable | 值 |
|----------|---|
| `SHOPIFY_SHOP` | your-store.myshopify.com |
| `SHOPIFY_ACCESS_TOKEN` | shpat_xxx |
| `OPENAI_API_KEY` | sk-xxx |
| `CRON_SCHEDULE` | `0 6 * * *` |
| `TZ` | Asia/Taipei |
| `API_KEY` | 自訂密鑰 (保護 API) |
| `JPY_TO_TWD_RATE` | 0.22 |
| `SERVICE_FEE_RATE` | 0.10 |
| `SHIPPING_PER_ITEM` | 150 |
| `PROFIT_MARGIN` | 0.15 |

### 4. 綁定域名 (選用)

Zeabur → Networking → 加上 `bbc-scraper.zeabur.app` 或自訂域名

### 5. 測試

```bash
# 查看狀態
curl https://你的域名/

# 手動觸發同步
curl -X POST https://你的域名/sync -H "x-api-key: 你的密鑰"

# 只抓取測試
curl -X POST https://你的域名/fetch-only -H "x-api-key: 你的密鑰"

# 更新價格
curl -X POST https://你的域名/price-update \
  -H "x-api-key: 你的密鑰" \
  -H "Content-Type: application/json" \
  -d '{"rate": 0.22}'

# 查看日誌
curl https://你的域名/logs?key=你的密鑰
```

## 排程說明

- 預設 `0 6 * * *` = 每天台北時間早上 6:00 自動同步
- 可透過環境變數 `CRON_SCHEDULE` 調整
- 常用排程：
  - `0 6 * * *` — 每天 6:00
  - `0 6,18 * * *` — 每天 6:00 和 18:00
  - `0 */6 * * *` — 每 6 小時

## 注意事項

- 食品類（咖啡豆、羊羹）有保質期，注意出貨速度
- 酒類商品如不需要可在 `crawler.js` 的 `categoryMap` 過濾
- Human Made 聯名商品可交叉行銷
- 定期便(subscription)商品預設跳過
