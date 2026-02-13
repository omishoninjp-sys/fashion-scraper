"""
adidas.jp 爬蟲 Web 控制面板
============================
Flask app，提供 Web UI 操作爬蟲
"""

import os
import asyncio
import threading
import time
import math
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string

from scraper import (
    AdidasScraper,
    ShopifyUploader,
    CATEGORIES,
    calculate_price,
    translate_ja_to_zhtw,
    logger,
    SHOPIFY_STORE,
    SHOPIFY_ACCESS_TOKEN,
    OPENAI_API_KEY,
)

app = Flask(__name__)

# ============================================================
# 全域狀態
# ============================================================
scrape_status = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_product": "",
    "uploaded": 0,
    "skipped": 0,
    "failed": 0,
    "errors": [],
    "products": [],
    "start_time": None,
    "end_time": None,
}


# ============================================================
# HTML 模板
# ============================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>adidas.jp 爬蟲控制台</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #000; color: #fff; min-height: 100vh; }
        .container { max-width: 1000px; margin: 0 auto; padding: 20px; }
        h1 { font-size: 28px; margin-bottom: 8px; }
        h1 span { color: #999; font-weight: 300; }
        .subtitle { color: #666; margin-bottom: 30px; font-size: 14px; }

        /* 狀態卡片 */
        .status-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }
        .status-card { background: #111; border: 1px solid #222; border-radius: 8px; padding: 16px; }
        .status-card .label { color: #666; font-size: 12px; text-transform: uppercase; }
        .status-card .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
        .status-card .value.green { color: #22c55e; }
        .status-card .value.yellow { color: #eab308; }
        .status-card .value.red { color: #ef4444; }

        /* 設定檢查 */
        .config-check { background: #111; border: 1px solid #222; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
        .config-item { display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 14px; }
        .config-item .dot { width: 8px; height: 8px; border-radius: 50%; }
        .config-item .dot.ok { background: #22c55e; }
        .config-item .dot.missing { background: #ef4444; }

        /* 控制面板 */
        .controls { background: #111; border: 1px solid #222; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
        .control-row { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
        .control-group { display: flex; flex-direction: column; gap: 4px; }
        .control-group label { font-size: 12px; color: #888; }
        select, input[type="number"] {
            background: #1a1a1a; border: 1px solid #333; color: #fff;
            padding: 8px 12px; border-radius: 6px; font-size: 14px;
        }
        button {
            padding: 8px 20px; border-radius: 6px; border: none;
            font-size: 14px; font-weight: 600; cursor: pointer; transition: 0.2s;
        }
        .btn-primary { background: #fff; color: #000; }
        .btn-primary:hover { background: #ddd; }
        .btn-primary:disabled { background: #444; color: #888; cursor: not-allowed; }
        .btn-danger { background: #dc2626; color: #fff; }
        .btn-danger:hover { background: #b91c1c; }
        .btn-test { background: #1a1a1a; color: #fff; border: 1px solid #333; }
        .btn-test:hover { background: #222; }

        /* 進度條 */
        .progress-section { background: #111; border: 1px solid #222; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
        .progress-bar-wrap { background: #1a1a1a; border-radius: 4px; height: 8px; margin: 12px 0; overflow: hidden; }
        .progress-bar { height: 100%; background: #fff; transition: width 0.3s; border-radius: 4px; }
        .progress-text { display: flex; justify-content: space-between; font-size: 13px; color: #888; }

        /* 價格計算器 */
        .calculator { background: #111; border: 1px solid #222; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
        .calc-row { display: flex; gap: 12px; align-items: end; }
        .calc-result { font-size: 20px; font-weight: 600; margin-top: 12px; }

        /* 商品列表 */
        .product-list { background: #111; border: 1px solid #222; border-radius: 8px; overflow: hidden; }
        .product-list h3 { padding: 16px; border-bottom: 1px solid #222; }
        .product-item { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
                         border-bottom: 1px solid #1a1a1a; font-size: 13px; }
        .product-item:last-child { border-bottom: none; }
        .product-item img { width: 48px; height: 48px; object-fit: cover; border-radius: 4px; background: #222; }
        .product-item .info { flex: 1; }
        .product-item .sku { color: #888; font-family: monospace; }
        .product-item .price { font-weight: 600; white-space: nowrap; }
        .product-item .price .original { color: #888; text-decoration: line-through; font-weight: 400; }
        .product-item .status-badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
        .product-item .status-badge.success { background: #052e16; color: #22c55e; }
        .product-item .status-badge.skip { background: #1c1917; color: #a3a3a3; }
        .product-item .status-badge.error { background: #2c0b0e; color: #ef4444; }

        /* Log */
        .log-section { background: #111; border: 1px solid #222; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
        .log-content { font-family: monospace; font-size: 12px; color: #888; max-height: 200px; overflow-y: auto;
                        background: #0a0a0a; padding: 12px; border-radius: 4px; margin-top: 8px; }
        .log-content .error { color: #ef4444; }
    </style>
</head>
<body>
<div class="container">
    <h1>adidas.jp <span>爬蟲控制台</span></h1>
    <p class="subtitle">Playwright 爬蟲 → Shopify 自動上架 | 定價: (售價+¥1,250) ÷ 0.7</p>

    <!-- 設定檢查 -->
    <div class="config-check">
        <h3 style="margin-bottom: 10px; font-size: 14px;">⚙️ 環境設定</h3>
        <div class="config-item">
            <span class="dot {{ 'ok' if shopify_ok else 'missing' }}"></span>
            Shopify {{ '✓ ' + store_name if shopify_ok else '✗ 未設定' }}
        </div>
        <div class="config-item">
            <span class="dot {{ 'ok' if openai_ok else 'missing' }}"></span>
            OpenAI 翻譯 {{ '✓ 已設定' if openai_ok else '✗ 未設定（將跳過翻譯）' }}
        </div>
    </div>

    <!-- 狀態卡片 -->
    <div class="status-grid">
        <div class="status-card">
            <div class="label">已上架</div>
            <div class="value green" id="stat-uploaded">0</div>
        </div>
        <div class="status-card">
            <div class="label">已跳過（重複）</div>
            <div class="value yellow" id="stat-skipped">0</div>
        </div>
        <div class="status-card">
            <div class="label">失敗</div>
            <div class="value red" id="stat-failed">0</div>
        </div>
    </div>

    <!-- 控制面板 -->
    <div class="controls">
        <div class="control-row">
            <div class="control-group">
                <label>分類</label>
                <select id="category">
                    <option value="men_originals">男鞋 Originals</option>
                    <option value="women_originals">女鞋 Originals</option>
                    <option value="all">全部</option>
                </select>
            </div>
            <div class="control-group">
                <label>最多頁數 (0=全部)</label>
                <input type="number" id="max-pages" value="0" min="0" max="50" style="width: 80px;">
            </div>
            <div class="control-group">
                <label>爬取模式</label>
                <select id="mode">
                    <option value="full">完整模式（含詳細頁）</option>
                    <option value="list-only">快速模式（僅列表頁）</option>
                </select>
            </div>
            <button class="btn-primary" id="btn-start" onclick="startScrape()">🚀 開始爬取</button>
            <button class="btn-test" onclick="startTest()" id="btn-test">🧪 測試（只上1個）</button>
            <button class="btn-test" onclick="testPrice()">🧮 定價計算</button>
        </div>
    </div>

    <!-- 進度 -->
    <div class="progress-section" id="progress-section" style="display:none;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong id="progress-label">準備中...</strong>
            <span id="progress-pct" style="color:#888; font-size:13px;">0%</span>
        </div>
        <div class="progress-bar-wrap">
            <div class="progress-bar" id="progress-bar" style="width:0%"></div>
        </div>
        <div class="progress-text">
            <span id="progress-detail">等待中</span>
            <span id="progress-time"></span>
        </div>
    </div>

    <!-- 價格計算器 -->
    <div class="calculator" id="calculator" style="display:none;">
        <h3 style="margin-bottom: 12px; font-size: 14px;">🧮 定價計算器</h3>
        <div class="calc-row">
            <div class="control-group">
                <label>adidas 售價（日幣）</label>
                <input type="number" id="calc-input" value="15950" style="width: 160px;">
            </div>
            <button class="btn-test" onclick="calcPrice()">計算</button>
        </div>
        <div class="calc-result" id="calc-result">
            Shopify 售價: ¥<span id="calc-output">24,572</span>
        </div>
        <p style="color:#666; font-size:12px; margin-top:6px;">公式: (售價 + ¥1,250) ÷ 0.7</p>
    </div>

    <!-- 商品列表 -->
    <div class="product-list" id="product-list" style="display:none;">
        <h3>📦 爬取結果 <span id="product-count" style="color:#888; font-weight:400;"></span></h3>
        <div id="product-items"></div>
    </div>

    <!-- Log -->
    <div class="log-section">
        <h3 style="font-size: 14px;">📋 執行記錄</h3>
        <div class="log-content" id="log-content">等待操作...</div>
    </div>
</div>

<script>
let pollInterval = null;

function log(msg, isError = false) {
    const el = document.getElementById('log-content');
    const cls = isError ? ' class="error"' : '';
    el.innerHTML += `<div${cls}>[${new Date().toLocaleTimeString()}] ${msg}</div>`;
    el.scrollTop = el.scrollHeight;
}

async function startTest() {
    const category = document.getElementById('category').value;
    document.getElementById('btn-start').disabled = true;
    document.getElementById('btn-test').disabled = true;
    document.getElementById('progress-section').style.display = 'block';
    document.getElementById('product-list').style.display = 'block';
    document.getElementById('product-items').innerHTML = '';
    log(`🧪 測試模式: ${category} (只上架1個商品)`);

    try {
        const resp = await fetch('/api/start-scrape', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ category, max_pages: 1, mode: 'full', test_mode: true })
        });
        const data = await resp.json();
        if (data.error) {
            log(data.error, true);
            document.getElementById('btn-start').disabled = false;
            document.getElementById('btn-test').disabled = false;
            return;
        }
        log(data.message);
        startPolling();
    } catch (e) {
        log('啟動失敗: ' + e, true);
        document.getElementById('btn-start').disabled = false;
        document.getElementById('btn-test').disabled = false;
    }
}

async function startScrape() {
    const category = document.getElementById('category').value;
    const maxPages = document.getElementById('max-pages').value;
    const mode = document.getElementById('mode').value;

    document.getElementById('btn-start').disabled = true;
    document.getElementById('progress-section').style.display = 'block';
    document.getElementById('product-list').style.display = 'block';
    document.getElementById('product-items').innerHTML = '';
    log(`開始爬取: ${category}, 最多 ${maxPages} 頁, 模式: ${mode}`);

    try {
        const resp = await fetch('/api/start-scrape', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ category, max_pages: parseInt(maxPages), mode })
        });
        const data = await resp.json();
        if (data.error) {
            log(data.error, true);
            document.getElementById('btn-start').disabled = false;
            return;
        }
        log(data.message);
        startPolling();
    } catch (e) {
        log('啟動失敗: ' + e, true);
        document.getElementById('btn-start').disabled = false;
    }
}

function startPolling() {
    pollInterval = setInterval(async () => {
        try {
            const resp = await fetch('/api/status');
            const s = await resp.json();

            document.getElementById('stat-uploaded').textContent = s.uploaded;
            document.getElementById('stat-skipped').textContent = s.skipped;
            document.getElementById('stat-failed').textContent = s.failed;

            if (s.total > 0) {
                const pct = Math.round((s.progress / s.total) * 100);
                document.getElementById('progress-bar').style.width = pct + '%';
                document.getElementById('progress-pct').textContent = pct + '%';
                document.getElementById('progress-detail').textContent =
                    `${s.progress} / ${s.total} 商品`;
            }
            document.getElementById('progress-label').textContent = s.current_product || '處理中...';
            document.getElementById('product-count').textContent = `(${s.products.length} 筆)`;

            // 更新商品列表
            const container = document.getElementById('product-items');
            container.innerHTML = s.products.map(p => `
                <div class="product-item">
                    <img src="${p.image || ''}" alt="">
                    <div class="info">
                        <div>${p.title}</div>
                        <div class="sku">${p.sku}</div>
                    </div>
                    <div class="price">
                        ¥${Number(p.selling_price).toLocaleString()}
                        <span class="original">¥${Number(p.price_jpy).toLocaleString()}</span>
                    </div>
                    <span class="status-badge ${p.status || ''}">${p.status_text || ''}</span>
                </div>
            `).join('');

            if (!s.running) {
                clearInterval(pollInterval);
                document.getElementById('btn-start').disabled = false;
                document.getElementById('btn-test').disabled = false;
                document.getElementById('progress-label').textContent = '✅ 完成';
                document.getElementById('progress-bar').style.width = '100%';
                log(`完成！上架: ${s.uploaded}, 跳過: ${s.skipped}, 失敗: ${s.failed}`);
            }
        } catch (e) { /* ignore */ }
    }, 2000);
}

function testPrice() {
    document.getElementById('calculator').style.display =
        document.getElementById('calculator').style.display === 'none' ? 'block' : 'none';
    calcPrice();
}

function calcPrice() {
    const input = parseInt(document.getElementById('calc-input').value) || 0;
    const result = Math.ceil((input + 1250) / 0.7);
    document.getElementById('calc-output').textContent = result.toLocaleString();
}
</script>
</body>
</html>
"""


# ============================================================
# API 路由
# ============================================================
@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        shopify_ok=bool(SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN),
        store_name=SHOPIFY_STORE,
        openai_ok=bool(OPENAI_API_KEY),
    )


@app.route("/api/status")
def api_status():
    return jsonify(scrape_status)


@app.route("/api/start-scrape", methods=["POST"])
def api_start_scrape():
    global scrape_status
    if scrape_status["running"]:
        return jsonify({"error": "爬蟲正在執行中，請等待完成"})

    data = request.get_json() or {}
    category = data.get("category", "men_originals")
    max_pages = data.get("max_pages", 0)
    mode = data.get("mode", "full")
    test_mode = data.get("test_mode", False)

    # 決定要爬的分類
    if category == "all":
        cats = list(CATEGORIES.keys())
    elif category in CATEGORIES:
        cats = [category]
    else:
        return jsonify({"error": f"無效分類: {category}"})

    # 重置狀態
    scrape_status = {
        "running": True,
        "progress": 0,
        "total": 0,
        "current_product": "初始化瀏覽器...",
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "products": [],
        "start_time": datetime.now().isoformat(),
        "end_time": None,
    }

    # 在背景線程中執行
    thread = threading.Thread(
        target=run_scrape_thread,
        args=(cats, max_pages, mode, test_mode),
        daemon=True,
    )
    thread.start()

    cat_names = ", ".join(CATEGORIES[c]["name"] for c in cats)
    test_label = " [🧪 測試模式：只上1個]" if test_mode else ""
    pages_label = "全部" if max_pages == 0 else f"最多 {max_pages}"
    return jsonify({"message": f"開始爬取: {cat_names} ({pages_label} 頁){test_label}"})


@app.route("/api/test-price")
def api_test_price():
    """測試定價"""
    price = request.args.get("price", 15950, type=int)
    return jsonify({
        "adidas_price": price,
        "selling_price": calculate_price(price),
        "formula": f"({price} + 1250) / 0.7 = {calculate_price(price)}",
    })


# ============================================================
# 背景爬蟲執行
# ============================================================
def run_scrape_thread(categories: list, max_pages: int, mode: str, test_mode: bool = False):
    """在背景線程中執行爬蟲"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_scrape_async(categories, max_pages, mode, test_mode))
    except Exception as e:
        logger.error(f"爬蟲執行錯誤: {e}")
        scrape_status["errors"].append(str(e))
    finally:
        scrape_status["running"] = False
        scrape_status["end_time"] = datetime.now().isoformat()
        loop.close()


async def run_scrape_async(categories: list, max_pages: int, mode: str, test_mode: bool = False):
    """非同步爬蟲主流程"""
    global scrape_status
    scraper = AdidasScraper()
    uploader = ShopifyUploader() if (SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN) else None

    try:
        # 啟動瀏覽器
        scrape_status["current_product"] = "啟動瀏覽器..."
        await scraper.init_browser()
        logger.info("瀏覽器啟動成功")

        all_products = []

        # 爬取每個分類的列表頁
        for cat_key in categories:
            cat = CATEGORIES[cat_key]
            scrape_status["current_product"] = f"爬取 {cat['name']} 列表頁..."
            logger.info(f"=== 開始爬取: {cat['name']} ===")

            # 測試模式只爬第1頁
            pages = 1 if test_mode else max_pages
            products = await scraper.scrape_listing_page(cat["url"], pages)
            for p in products:
                p["category"] = cat_key
                p["collection_name"] = cat["collection"]
            all_products.extend(products)
            logger.info(f"{cat['name']} 找到 {len(products)} 個商品")

        # 測試模式只處理第1個商品
        if test_mode and len(all_products) > 0:
            all_products = [all_products[0]]
            logger.info("🧪 測試模式：只處理第 1 個商品")

        scrape_status["total"] = len(all_products)
        logger.info(f"共找到 {len(all_products)} 個商品")

        # 處理每個商品
        for idx, product in enumerate(all_products):
            scrape_status["progress"] = idx + 1
            scrape_status["current_product"] = f"[{idx+1}/{len(all_products)}] {product['sku']} - {product['title']}"

            product_entry = {
                "sku": product["sku"],
                "title": product["title"],
                "price_jpy": product["price_jpy"],
                "selling_price": product["selling_price"],
                "image": product.get("image", ""),
                "status": "",
                "status_text": "",
            }

            # 重複檢查
            if uploader and uploader.is_duplicate(product["sku"]):
                product_entry["status"] = "skip"
                product_entry["status_text"] = "已存在"
                scrape_status["skipped"] += 1
                scrape_status["products"].append(product_entry)
                continue

            # 爬取詳細頁（完整模式）
            detail = None
            if mode == "full":
                scrape_status["current_product"] = (
                    f"[{idx+1}/{len(all_products)}] 爬取詳細頁: {product['sku']}"
                )
                detail = await scraper.scrape_product_detail(product["url"])
                time.sleep(1)  # 避免太快被封

            # 上架到 Shopify
            if uploader:
                collection_id = uploader.get_or_create_collection(
                    product["collection_name"]
                )
                result = uploader.upload_product(product, detail, collection_id)
                if result["success"]:
                    product_entry["status"] = "success"
                    product_entry["status_text"] = "已上架"
                    scrape_status["uploaded"] += 1
                else:
                    product_entry["status"] = "error"
                    product_entry["status_text"] = "失敗"
                    scrape_status["failed"] += 1
                    scrape_status["errors"].append(
                        f"{product['sku']}: {result.get('error', '')[:100]}"
                    )
            else:
                # 無 Shopify（測試模式）
                product_entry["status"] = "skip"
                product_entry["status_text"] = "測試模式"

            scrape_status["products"].append(product_entry)

            # 間隔避免被封
            time.sleep(0.5)

    except Exception as e:
        logger.error(f"爬蟲異常: {e}")
        scrape_status["errors"].append(str(e))
        raise
    finally:
        await scraper.close_browser()
        logger.info("瀏覽器已關閉")


# ============================================================
# 啟動
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
