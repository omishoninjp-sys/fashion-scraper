# WORKMAN 爬蟲 (Zeabur 部署版)

自動爬取 WORKMAN 日本官網商品並批量上傳到 Shopify。

## 功能

- 🤖 **完全自動化** - Cron Job 一鍵執行全部流程
- 🧪 測試單品上傳（快速驗證格式）
- 📥 爬取商品（支援多頁分頁）
- 📤 批量上傳到 Shopify（Bulk Operations API）
- 📢 自動發布到所有銷售管道
- 🗑️ 批量刪除商品

## Zeabur 部署步驟

### 1. 建立 GitHub Repository

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/你的帳號/workman-scraper.git
git push -u origin main
```

### 2. 在 Zeabur 部署

1. 登入 [Zeabur](https://zeabur.com)
2. 建立新專案
3. 選擇「Deploy from GitHub」
4. 選擇你的 repository

### 3. 設定環境變數

在 Zeabur 專案的 **Variables** 頁面設定以下環境變數：

| 變數名稱 | 說明 | 範例 |
|---------|------|------|
| `SHOPIFY_STORE` | Shopify 商店名稱 | `goyoulink` |
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin API Token | `shpat_xxxxxxxx` |
| `OPENAI_API_KEY` | OpenAI API Key（用於翻譯） | `sk-xxxxxxxx` |

### 4. 取得 Shopify Access Token

1. 進入 Shopify 後台 → Settings → Apps and sales channels
2. 點擊 Develop apps → Create an app
3. 設定 Admin API scopes：
   - `read_products`
   - `write_products`
   - `read_publications`
   - `write_publications`
4. Install app → 複製 Admin API access token

---

## 🤖 Cron Job 自動化設定

### API Endpoints

| Endpoint | 說明 | 建議 |
|----------|------|------|
| `/api/cron?category=all` | 背景執行，立即回應 | ✅ 推薦使用 |
| `/api/cron_sync?category=all` | 同步執行，等待完成才回應 | 需要長 timeout |

### 可用分類參數

| 參數 | 說明 |
|------|------|
| `all` | 全部分類（預設） |
| `work` | 作業服 |
| `mens` | 男裝 |
| `womens` | 女裝 |
| `kids` | 兒童服 |

### cron-job.org 設定範例

1. 登入 [cron-job.org](https://cron-job.org)
2. 建立新的 Cron Job
3. 設定如下：

```
URL: https://你的zeabur網址/api/cron?category=all
Schedule: 每天凌晨 3 點（或你想要的時間）
Request Method: GET
Request Timeout: 30 seconds（背景執行不需要長 timeout）
```

### 執行流程

當呼叫 `/api/cron` 時，會自動執行：

1. **爬取商品** - 爬取所有分頁的商品
2. **翻譯** - 使用 GPT 翻譯成繁體中文
3. **批量上傳** - 使用 Shopify Bulk Operations API
4. **等待完成** - 輪詢檢查上傳狀態
5. **發布** - 發布到所有銷售管道

### 查看執行狀態

- 訪問網站首頁可以看到即時狀態
- 或呼叫 `/api/status` 取得 JSON 格式狀態

---

## 手動使用方式

部署完成後，訪問 Zeabur 提供的 URL 即可使用網頁介面。

### 建議流程

1. **測試連線** - 先測試 workman.jp 和 Shopify 連線
2. **測試單品** - 用「🧪 測試單品上傳」驗證格式
3. **批量爬取** - 選擇分類開始爬取
4. **批量上傳** - 爬取完成後上傳到 Shopify
5. **自動發布** - 上傳完成後自動發布到銷售管道

---

## 商品分類

| 分類 | WORKMAN URL | Shopify 商品類型 |
|-----|-------------|-----------------|
| 作業服 | /shop/c/c51/ | WORKMAN 作業服 |
| 男裝 | /shop/c/c52/ | WORKMAN 男裝 |
| 女裝 | /shop/c/c53/ | WORKMAN 女裝 |
| 兒童服 | /shop/c/c54/ | WORKMAN 兒童 |

## 注意事項

- 翻譯使用 OpenAI GPT-4o-mini，會產生 API 費用
- 批量上傳使用 Shopify Bulk Operations API，速度很快
- 每個商品說明文底部會自動加入統一注意事項
- SEO 標題/描述會自動生成
- 建議設定 cron 在凌晨執行，避免影響網站效能

## 技術架構

- **後端**: Flask + Gunicorn
- **爬蟲**: requests + BeautifulSoup4
- **翻譯**: OpenAI API
- **上傳**: Shopify GraphQL Admin API (Bulk Operations)
