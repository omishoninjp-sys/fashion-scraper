/**
 * Blue Bottle Coffee Japan → Shopify 爬蟲
 * Zeabur 部署版 — Express Server + node-cron 排程
 * 
 * 來源: https://store.bluebottlecoffee.jp/ (Shopify)
 * 
 * Endpoints:
 *   GET  /              → 狀態頁
 *   GET  /health        → 健康檢查
 *   POST /sync          → 手動觸發同步
 *   POST /fetch-only    → 只抓取不同步
 *   POST /price-update  → 更新價格
 *   GET  /logs          → 查看最近日誌
 */

require('dotenv').config();
const express = require('express');
const cron = require('node-cron');
const { syncProducts, fetchAllProducts, buildProductCategoryMap } = require('./lib/crawler');
const { updateAllPrices } = require('./lib/price-tool');
const { log, getLogs } = require('./lib/logger');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;

// ============================================================
// 狀態追蹤
// ============================================================
const state = {
  isRunning: false,
  lastSync: null,
  lastResult: null,
  startedAt: new Date().toISOString(),
  totalSyncs: 0,
};

// ============================================================
// 排程設定
// ============================================================
const CRON_SCHEDULE = process.env.CRON_SCHEDULE || '0 6 * * *'; // 預設每天早上 6:00 (UTC)

cron.schedule(CRON_SCHEDULE, async () => {
  log(`⏰ 排程觸發同步 (${CRON_SCHEDULE})`);
  await runSync();
}, {
  timezone: process.env.TZ || 'Asia/Taipei',
});

log(`📅 排程已設定: ${CRON_SCHEDULE} (${process.env.TZ || 'Asia/Taipei'})`);

// ============================================================
// 同步執行器
// ============================================================
async function runSync() {
  if (state.isRunning) {
    log('⚠️ 同步正在進行中，跳過');
    return { success: false, message: '同步正在進行中' };
  }

  state.isRunning = true;
  const startTime = Date.now();

  try {
    const result = await syncProducts();
    const elapsed = Math.round((Date.now() - startTime) / 1000);

    state.lastSync = new Date().toISOString();
    state.lastResult = { ...result, elapsed: `${elapsed}s` };
    state.totalSyncs++;

    log(`✅ 同步完成，耗時 ${elapsed}s`);
    return { success: true, ...state.lastResult };
  } catch (error) {
    log(`❌ 同步失敗: ${error.message}`);
    state.lastResult = { success: false, error: error.message };
    return { success: false, error: error.message };
  } finally {
    state.isRunning = false;
  }
}

// ============================================================
// Routes
// ============================================================

// 狀態頁
app.get('/', (req, res) => {
  res.json({
    service: 'Blue Bottle Coffee JP Scraper',
    source: 'https://store.bluebottlecoffee.jp/',
    status: state.isRunning ? 'syncing' : 'idle',
    schedule: CRON_SCHEDULE,
    timezone: process.env.TZ || 'Asia/Taipei',
    lastSync: state.lastSync,
    lastResult: state.lastResult,
    totalSyncs: state.totalSyncs,
    uptime: process.uptime(),
    startedAt: state.startedAt,
  });
});

// 健康檢查 (Zeabur 用)
app.get('/health', (req, res) => {
  res.status(200).json({ status: 'ok' });
});

// 手動觸發同步
app.post('/sync', async (req, res) => {
  // 簡單的 API Key 驗證
  const apiKey = req.headers['x-api-key'] || req.query.key;
  if (process.env.API_KEY && apiKey !== process.env.API_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  log('🔧 手動觸發同步');
  
  if (state.isRunning) {
    return res.status(409).json({ error: '同步正在進行中，請稍後再試' });
  }

  // 非同步執行，立即回應
  res.json({ message: '同步已開始', startedAt: new Date().toISOString() });
  runSync();
});

// 只抓取不同步（測試用）
app.post('/fetch-only', async (req, res) => {
  const apiKey = req.headers['x-api-key'] || req.query.key;
  if (process.env.API_KEY && apiKey !== process.env.API_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  try {
    log('🔧 手動觸發 fetch-only');
    const products = await fetchAllProducts();
    res.json({
      success: true,
      count: products.length,
      products: products.map(p => ({
        handle: p.handle,
        title: p.title,
        price: p.variants?.[0]?.price,
        available: p.variants?.some(v => v.available),
        variants: p.variants?.length,
        images: p.images?.length,
      })),
    });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// 更新價格
app.post('/price-update', async (req, res) => {
  const apiKey = req.headers['x-api-key'] || req.query.key;
  if (process.env.API_KEY && apiKey !== process.env.API_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  const rate = parseFloat(req.body?.rate) || undefined;

  try {
    log(`🔧 手動觸發價格更新${rate ? ` (匯率: ${rate})` : ''}`);
    const result = await updateAllPrices(rate);
    res.json({ success: true, ...result });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// 查看日誌
app.get('/logs', (req, res) => {
  const apiKey = req.headers['x-api-key'] || req.query.key;
  if (process.env.API_KEY && apiKey !== process.env.API_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  const count = parseInt(req.query.count) || 100;
  res.json({ logs: getLogs(count) });
});

// ============================================================
// 啟動
// ============================================================
app.listen(PORT, () => {
  log(`🚀 Blue Bottle Coffee 爬蟲啟動 port ${PORT}`);
  log(`📅 排程: ${CRON_SCHEDULE} (${process.env.TZ || 'Asia/Taipei'})`);
  log(`🔗 來源: https://store.bluebottlecoffee.jp/`);
});
