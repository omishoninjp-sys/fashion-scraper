"""
BEAMS Scraper Web API — Zeabur 部署用
提供 Web 介面 + REST API 操作爬蟲
"""

import os
import json
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

from scraper import (
    CATEGORIES,
    BeamsScraper,
    ShopifyUploader,
    translate_ja_to_zhtw,
    calculate_proxy_price,
    run_scraper,
    logger,
)

app = Flask(__name__)

# 執行狀態追蹤
scrape_status = {
    "is_running": False,
    "last_run": None,
    "last_result": None,
}

# ============================================================
# Web 控制面板
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BEAMS 爬蟲控制台 | GOYOUTATI</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; color: #333; }
  .header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white; padding: 20px 30px; }
  .header h1 { font-size: 24px; margin-bottom: 5px; }
  .header p { opacity: 0.7; font-size: 14px; }
  .container { max-width: 1000px; margin: 20px auto; padding: 0 20px; }
  .card { background: white; border-radius: 12px; padding: 25px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .card h2 { font-size: 18px; margin-bottom: 15px; color: #1a1a2e; }
  .status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
  .status-idle { background: #e8f5e9; color: #2e7d32; }
  .status-running { background: #fff3e0; color: #e65100; animation: pulse 1.5s infinite; }
  @keyframes pulse { 50% { opacity: 0.6; } }
  .category-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; margin: 15px 0; }
  .cat-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border: 1px solid #e0e0e0; border-radius: 8px; cursor: pointer; transition: all 0.2s; font-size: 13px; }
  .cat-item:hover { border-color: #1a1a2e; background: #f8f8ff; }
  .cat-item input { accent-color: #1a1a2e; }
  .cat-item.checked { border-color: #1a1a2e; background: #f0f0ff; }
  .btn { padding: 10px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .btn-primary { background: #1a1a2e; color: white; }
  .btn-primary:hover { background: #2a2a4e; }
  .btn-primary:disabled { background: #ccc; cursor: not-allowed; }
  .btn-test { background: #e3f2fd; color: #1565c0; }
  .btn-test:hover { background: #bbdefb; }
  .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 15px; }
  .controls label { font-size: 13px; color: #666; }
  .controls input[type="number"] { width: 60px; padding: 6px 8px; border: 1px solid #ddd; border-radius: 6px; }
  .result-box { background: #f8f9fa; border-radius: 8px; padding: 15px; margin-top: 15px; font-family: monospace; font-size: 13px; white-space: pre-wrap; max-height: 400px; overflow-y: auto; display: none; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
  .stat-card { text-align: center; padding: 15px; background: #f8f9fa; border-radius: 8px; }
  .stat-card .number { font-size: 28px; font-weight: 700; color: #1a1a2e; }
  .stat-card .label { font-size: 12px; color: #888; margin-top: 4px; }
  .price-calc { display: grid; grid-template-columns: 1fr auto 1fr; gap: 15px; align-items: end; }
  .price-calc input { width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; }
  .price-calc .arrow { font-size: 24px; text-align: center; color: #888; padding-bottom: 8px; }
  .price-calc .result { font-size: 24px; font-weight: 700; color: #e74c3c; padding: 8px; text-align: center; }
  .section-label { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
</style>
</head>
<body>

<div class="header">
  <h1>🇯🇵 BEAMS 爬蟲控制台</h1>
  <p>GOYOUTATI 代購自動化系統 — 精選商品 → 翻譯 → 定價 → Shopify 上架</p>
</div>

<div class="container">

  <!-- 狀態卡 -->
  <div class="card">
    <h2>📊 系統狀態</h2>
    <div class="stats-grid">
      <div class="stat-card">
        <div id="status-text" class="status-badge status-idle">閒置中</div>
        <div class="label" style="margin-top:8px;">爬蟲狀態</div>
      </div>
      <div class="stat-card">
        <div class="number" id="stat-found">-</div>
        <div class="label">上次發現商品</div>
      </div>
      <div class="stat-card">
        <div class="number" id="stat-uploaded">-</div>
        <div class="label">成功上架</div>
      </div>
      <div class="stat-card">
        <div class="number" id="stat-skipped">-</div>
        <div class="label">跳過重複</div>
      </div>
    </div>
  </div>

  <!-- 快速價格計算 -->
  <div class="card">
    <h2>💰 代購價格試算（售價為日幣）</h2>
    <p style="font-size:13px;color:#666;margin-bottom:12px;">公式：(商品價格 + 重量×¥1,250/kg) ÷ 0.7</p>
    <div style="display:grid;grid-template-columns:1fr 1fr auto 1fr;gap:12px;align-items:end;">
      <div>
        <div class="section-label">日幣售價</div>
        <input type="number" id="jpy-input" placeholder="例: 18150" oninput="calcPrice()">
      </div>
      <div>
        <div class="section-label">預估重量(kg)</div>
        <select id="weight-input" onchange="calcPrice()" style="width:100%;padding:8px;border:1px solid #ddd;border-radius:8px;font-size:14px;">
          <option value="0.10">飾品 0.1kg</option>
          <option value="0.15">帽子/皮夾 0.15kg</option>
          <option value="0.20">T恤/手錶 0.2kg</option>
          <option value="0.25">襯衫 0.25kg</option>
          <option value="0.30" selected>裙子/雜貨 0.3kg</option>
          <option value="0.35">毛衣/針織 0.35kg</option>
          <option value="0.40">洋裝 0.4kg</option>
          <option value="0.50">長褲 0.5kg</option>
          <option value="0.60">包包 0.6kg</option>
          <option value="0.70">西裝外套 0.7kg</option>
          <option value="0.80">夾克/鞋子 0.8kg</option>
          <option value="1.50">大衣/西裝套裝 1.5kg</option>
        </select>
      </div>
      <div class="arrow" style="font-size:24px;text-align:center;color:#888;padding-bottom:8px;">→</div>
      <div>
        <div class="section-label">代購售價（日幣）</div>
        <div class="result" id="twd-result" style="font-size:24px;font-weight:700;color:#e74c3c;padding:8px;text-align:center;">-</div>
      </div>
    </div>
  </div>

  <!-- 分類選擇 -->
  <div class="card">
    <h2>📦 選擇爬取分類</h2>

    <h3 style="font-size:14px;color:#666;margin:10px 0 5px;">👔 男裝</h3>
    <div class="category-grid" id="cat-men"></div>

    <h3 style="font-size:14px;color:#666;margin:10px 0 5px;">👗 女裝</h3>
    <div class="category-grid" id="cat-women"></div>

    <h3 style="font-size:14px;color:#666;margin:10px 0 5px;">👶 童裝</h3>
    <div class="category-grid" id="cat-kids"></div>

    <div class="controls">
      <label>每分類頁數: <input type="number" id="max-pages" value="2" min="1" max="10"></label>
      <button class="btn btn-test" onclick="runScraper(true)">🧪 測試模式（不上架）</button>
      <button class="btn btn-primary" id="btn-run" onclick="runScraper(false)">🚀 開始爬取 + 上架</button>
    </div>
  </div>

  <!-- 結果 -->
  <div class="card">
    <h2>📋 執行結果</h2>
    <div class="result-box" id="result-box"></div>
    <p id="no-result" style="color:#999;font-size:14px;">尚未執行</p>
  </div>

</div>

<script>
const CATEGORIES = CATEGORIES_JSON;

// 渲染分類選項
function renderCategories() {
  const groups = { men: 'cat-men', women: 'cat-women', kids: 'cat-kids' };
  for (const [key, cat] of Object.entries(CATEGORIES)) {
    const prefix = key.split('_')[0];
    const container = document.getElementById(groups[prefix]);
    if (!container) continue;
    const label = cat.name.split('｜')[1] || cat.name;
    container.innerHTML += `
      <label class="cat-item" onclick="this.classList.toggle('checked')">
        <input type="checkbox" name="category" value="${key}">
        ${label}
      </label>`;
  }
}

// 價格計算
function calcPrice() {
  const jpy = parseInt(document.getElementById('jpy-input').value) || 0;
  const weight = parseFloat(document.getElementById('weight-input').value) || 0.3;
  if (jpy <= 0) { document.getElementById('twd-result').textContent = '-'; return; }
  fetch(`/api/calc-price?jpy=${jpy}&weight=${weight}`)
    .then(r => r.json())
    .then(d => {
      document.getElementById('twd-result').textContent = `¥${d.final_jpy.toLocaleString()}`;
    });
}

// 執行爬蟲
async function runScraper(dryRun) {
  const checked = [...document.querySelectorAll('input[name="category"]:checked')].map(c => c.value);
  if (checked.length === 0) { alert('請至少選擇一個分類！'); return; }

  const maxPages = parseInt(document.getElementById('max-pages').value) || 2;
  const btn = document.getElementById('btn-run');
  const statusEl = document.getElementById('status-text');
  const resultBox = document.getElementById('result-box');

  btn.disabled = true;
  statusEl.textContent = '執行中...';
  statusEl.className = 'status-badge status-running';
  resultBox.style.display = 'block';
  resultBox.textContent = '⏳ 爬蟲執行中，請稍候...\\n';
  document.getElementById('no-result').style.display = 'none';

  try {
    const resp = await fetch('/api/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories: checked, max_pages: maxPages, dry_run: dryRun }),
    });
    const data = await resp.json();

    statusEl.textContent = '閒置中';
    statusEl.className = 'status-badge status-idle';

    if (data.error) {
      resultBox.textContent = `❌ 錯誤: ${data.error}`;
    } else {
      document.getElementById('stat-found').textContent = data.total_found || 0;
      document.getElementById('stat-uploaded').textContent = data.total_uploaded || 0;
      document.getElementById('stat-skipped').textContent = data.total_skipped_duplicate || 0;

      let text = `✅ 執行完成！${dryRun ? '（測試模式）' : ''}\\n`;
      text += `━━━━━━━━━━━━━━━━━━━━━━━━━━\\n`;
      text += `發現商品: ${data.total_found}\\n`;
      text += `成功上架: ${data.total_uploaded}\\n`;
      text += `跳過重複: ${data.total_skipped_duplicate}\\n`;
      text += `上架失敗: ${data.total_failed}\\n`;
      text += `━━━━━━━━━━━━━━━━━━━━━━━━━━\\n\\n`;

      if (data.items && data.items.length > 0) {
        text += '商品明細:\\n';
        for (const item of data.items.slice(0, 20)) {
          const price = item.pricing ? `¥${item.price_jpy?.toLocaleString()} → ¥${item.pricing.final_jpy?.toLocaleString()} (${item.weight_kg}kg)` : '價格未知';
          text += `  ${item.item_code} | ${item.title_zh || item.title_ja || '?'} | ${price}\\n`;
        }
        if (data.items.length > 20) text += `  ... 還有 ${data.items.length - 20} 件\\n`;
      }

      // ========== DEBUG LOGS ==========
      if (data.debug_logs && data.debug_logs.length > 0) {
        text += '\\n━━━━━━━━ DEBUG LOGS ━━━━━━━━\\n';
        for (const line of data.debug_logs) {
          if (line.trim()) text += line + '\\n';
        }
      }

      resultBox.textContent = text;
    }
  } catch (e) {
    resultBox.textContent = `❌ 網路錯誤: ${e.message}`;
    statusEl.textContent = '錯誤';
    statusEl.className = 'status-badge status-idle';
  }

  btn.disabled = false;
}

renderCategories();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    """控制面板首頁"""
    # 注入分類資料到 HTML
    categories_json = json.dumps(
        {k: {"name": v["name"]} for k, v in CATEGORIES.items()},
        ensure_ascii=False,
    )
    html = DASHBOARD_HTML.replace("CATEGORIES_JSON", categories_json)
    return html


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """執行爬蟲 API"""
    global scrape_status

    if scrape_status["is_running"]:
        return jsonify({"error": "爬蟲正在執行中，請稍後再試"}), 409

    data = request.json or {}
    categories = data.get("categories", [])
    max_pages = data.get("max_pages", 2)
    dry_run = data.get("dry_run", True)

    if not categories:
        return jsonify({"error": "請選擇至少一個分類"}), 400

    scrape_status["is_running"] = True

    try:
        result = run_scraper(
            categories=categories,
            max_pages=max_pages,
            dry_run=dry_run,
        )
        scrape_status["last_run"] = datetime.now().isoformat()
        scrape_status["last_result"] = result
        return jsonify(result)
    except Exception as e:
        logger.error(f"爬蟲執行錯誤: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        scrape_status["is_running"] = False


@app.route("/api/calc-price")
def api_calc_price():
    """價格計算 API"""
    jpy = request.args.get("jpy", 0, type=int)
    weight = request.args.get("weight", 0.3, type=float)
    if jpy <= 0:
        return jsonify({"error": "請提供有效的日幣金額"}), 400
    result = calculate_proxy_price(jpy, weight)
    return jsonify(result)


@app.route("/api/categories")
def api_categories():
    """取得分類列表"""
    return jsonify({k: v["name"] for k, v in CATEGORIES.items()})


@app.route("/api/status")
def api_status():
    """取得爬蟲狀態"""
    return jsonify(scrape_status)


@app.route("/api/translate", methods=["POST"])
def api_translate():
    """翻譯測試 API"""
    data = request.json or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "請提供翻譯文字"}), 400
    result = translate_ja_to_zhtw(text)
    return jsonify({"original": text, "translated": result})


@app.route("/health")
def health():
    """Zeabur 健康檢查"""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# ============================================================
# 啟動
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("DEBUG", "false").lower() == "true")
