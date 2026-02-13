# adidas.jp 爬蟲 → Shopify 自動上架

## 功能
- 使用 Playwright 模擬瀏覽器爬取 adidas.jp
- 支援分類：男鞋、女鞋
- 自動定價：`(adidas售價 + ¥1,250) ÷ 0.7 = Shopify售價`
- ChatGPT 日文→繁體中文翻譯
- 自動上架到 Shopify + Collection 管理
- 重複商品自動跳過（SKU 比對）
- Web 控制面板即時監控

## 定價範例
| adidas 售價 | Shopify 售價 | 
|------------|-------------|
| ¥15,950    | ¥24,572     |
| ¥19,800    | ¥30,072     |
| ¥9,900     | ¥15,929     |
| ¥26,400    | ¥39,500     |

## 部署到 Zeabur

1. 推送到 GitHub
2. Zeabur 連接 GitHub repo
3. 設定環境變數：
   - `SHOPIFY_STORE` - 商店名稱
   - `SHOPIFY_ACCESS_TOKEN` - Admin API Token
   - `OPENAI_API_KEY` - 翻譯用（可選）
   - `PROXY_URL` - 代理（可選）

## 本機開發

```bash
pip install -r requirements.txt
playwright install chromium

export SHOPIFY_STORE=your-store
export SHOPIFY_ACCESS_TOKEN=your-token

python app.py
```

打開 http://localhost:5000
