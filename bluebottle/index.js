/**
 * Blue Bottle Coffee Japan → Shopify 爬蟲
 * Zeabur 部署版 — Express Server + node-cron 排程 + 內建控制台
 * 
 * 來源: https://store.bluebottlecoffee.jp/ (Shopify)
 * 
 * 打開網址就是控制台，API Key 從環境變數讀取，不用手動輸入
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
const CRON_SCHEDULE = process.env.CRON_SCHEDULE || '0 6 * * *';

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
// HTML 控制台
// ============================================================
const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Blue Bottle Coffee — 爬蟲控制台</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0a0a0a; color: #fff; min-height: 100vh; }
  .container { max-width: 1000px; margin: 0 auto; padding: 20px; }
  .header { display: flex; align-items: center; gap: 14px; margin-bottom: 8px; }
  .header .logo { width: 36px; height: 36px; background: #4c8bf5; border-radius: 8px;
                  display: flex; align-items: center; justify-content: center; font-size: 16px; }
  h1 { font-size: 26px; } h1 span { color: #4c8bf5; }
  .subtitle { color: #666; margin-bottom: 28px; font-size: 14px; }
  .config-check { background: #111; border: 1px solid #222; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
  .config-item { display: flex; align-items: center; gap: 8px; margin: 6px 0; font-size: 14px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot.ok { background: #22c55e; } .dot.miss { background: #ef4444; }
  .status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }
  .sc { background: #111; border: 1px solid #222; border-radius: 8px; padding: 16px; }
  .sc .lb { color: #666; font-size: 12px; text-transform: uppercase; }
  .sc .vl { font-size: 24px; font-weight: 600; margin-top: 4px; }
  .sc .sb { color: #666; font-size: 12px; margin-top: 2px; }
  .grn { color: #22c55e; } .ylw { color: #eab308; } .red { color: #ef4444; } .blu { color: #4c8bf5; }
  .ctrls { background: #111; border: 1px solid #222; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
  .ctrls-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  button { padding: 10px 22px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600;
           cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; gap: 6px; }
  .bp { background: #4c8bf5; color: #fff; } .bp:hover { background: #3a76e0; }
  .bs { background: #6b4f3a; color: #fff; } .bs:hover { background: #5a4230; }
  .bo { background: transparent; color: #fff; border: 1px solid #333; } .bo:hover { border-color: #666; }
  button:disabled { background: #333!important; color: #666!important; border-color: #333!important; cursor: not-allowed; }
  .sp { width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.2); border-top-color: #fff;
        border-radius: 50%; animation: spin 0.6s linear infinite; display: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .rp { background: #111; border: 1px solid #222; border-radius: 8px; overflow: hidden; margin-bottom: 24px; display: none; }
  .rp-h { padding: 14px 16px; border-bottom: 1px solid #222; display: flex; justify-content: space-between; align-items: center; }
  .rp-h h3 { font-size: 14px; }
  .rp-badge { font-size: 12px; padding: 2px 10px; border-radius: 10px; background: #1a1a1a; color: #888; }
  .rp-b { padding: 16px; max-height: 400px; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11px; color: #666; text-transform: uppercase; padding: 8px; border-bottom: 1px solid #222; }
  td { padding: 8px; border-bottom: 1px solid #1a1a1a; }
  tr:hover td { background: #151515; }
  .bd { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; }
  .bd.i { background: #052e16; color: #22c55e; } .bd.o { background: #2c0b0e; color: #ef4444; }
  .ls { background: #111; border: 1px solid #222; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
  .lc { font-family: 'SF Mono','Menlo',monospace; font-size: 12px; color: #888; max-height: 250px;
        overflow-y: auto; background: #0a0a0a; padding: 12px; border-radius: 4px; margin-top: 8px; line-height: 1.8; }
  .lc .e { color: #ef4444; } .lc .g { color: #22c55e; } .lc .w { color: #eab308; }
  @media (max-width: 768px) {
    .status-grid { grid-template-columns: repeat(2, 1fr); }
    .ctrls-row { flex-direction: column; }
    .ctrls-row button { width: 100%; justify-content: center; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header"><div class="logo">☕</div><h1>Blue Bottle Coffee <span>爬蟲控制台</span></h1></div>
  <p class="subtitle">Shopify JSON API 爬蟲 → OpenAI 翻譯 → 自動上架 | 排程: %%SCHEDULE%% (%%TZ%%)</p>

  <div class="config-check">
    <h3 style="margin-bottom:10px;font-size:14px;">⚙️ 環境設定</h3>
    <div class="config-item"><span class="dot %%C_SHOP%%"></span> Shopify %%S_SHOP%%</div>
    <div class="config-item"><span class="dot %%C_OAI%%"></span> OpenAI 翻譯 %%S_OAI%%</div>
    <div class="config-item"><span class="dot %%C_KEY%%"></span> API Key %%S_KEY%%</div>
  </div>

  <div class="status-grid">
    <div class="sc"><div class="lb">狀態</div><div class="vl blu" id="v-st">—</div><div class="sb" id="v-sch"></div></div>
    <div class="sc"><div class="lb">累計同步</div><div class="vl grn" id="v-cnt">—</div><div class="sb" id="v-ls">尚未同步</div></div>
    <div class="sc"><div class="lb">上次結果</div><div class="vl" id="v-res">—</div><div class="sb" id="v-rd"></div></div>
    <div class="sc"><div class="lb">運行時間</div><div class="vl" id="v-up">—</div><div class="sb" id="v-sa"></div></div>
  </div>

  <div class="ctrls"><div class="ctrls-row">
    <button class="bp" id="b-sync" onclick="doSync()"><span class="sp" id="sp1"></span> 🔄 完整同步</button>
    <button class="bs" id="b-fetch" onclick="doFetch()"><span class="sp" id="sp2"></span> 🔍 測試抓取</button>
    <button class="bo" id="b-price" onclick="doPrice()"><span class="sp" id="sp3"></span> 💰 更新價格</button>
    <button class="bo" onclick="loadLogs()">📋 重整日誌</button>
  </div></div>

  <div class="rp" id="rp">
    <div class="rp-h"><h3 id="rp-t">結果</h3><span class="rp-badge" id="rp-bg"></span></div>
    <div class="rp-b" id="rp-bd"></div>
  </div>

  <div class="ls">
    <h3 style="font-size:14px;">📋 服務日誌</h3>
    <div class="lc" id="lc">載入中...</div>
  </div>
</div>

<script>
async function api(m,p,b){const o={method:m,headers:{'Content-Type':'application/json'}};if(b)o.body=JSON.stringify(b);const r=await fetch(p,o);return r.json();}

async function refreshStatus(){try{const d=await api('GET','/api/status');const s=d.status==='syncing';
$('v-st').textContent=s?'同步中...':'待命';$('v-st').className='vl '+(s?'ylw':'blu');
$('v-sch').textContent=d.schedule+' ('+d.timezone+')';
$('v-cnt').textContent=d.totalSyncs;$('v-ls').textContent=d.lastSync?ft(d.lastSync):'尚未同步';
if(d.lastResult){const r=d.lastResult;if(r.created!==undefined){$('v-res').textContent=(r.created+r.updated);$('v-res').className='vl grn';
$('v-rd').textContent='新建'+r.created+' / 更新'+r.updated+' / 跳過'+r.skipped+' / 錯誤'+r.errors;}
else if(r.error){$('v-res').textContent='失敗';$('v-res').className='vl red';$('v-rd').textContent=r.error;}}
const u=Math.floor(d.uptime||0),h=Math.floor(u/3600),m=Math.floor((u%3600)/60);
$('v-up').textContent=h>0?h+'h '+m+'m':m+'m';$('v-sa').textContent=d.startedAt?'啟動: '+ft(d.startedAt):'';
$('b-sync').disabled=s;$('sp1').style.display=s?'inline-block':'none';}catch(e){}}

function $(id){return document.getElementById(id);}
function ft(iso){return new Date(iso).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false});}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

async function doSync(){const b=$('b-sync'),s=$('sp1');b.disabled=true;s.style.display='inline-block';
alog('🔧 手動觸發完整同步...');
try{await api('POST','/api/sync');alog('✅ 同步已啟動，等待完成...');
const p=setInterval(async()=>{await refreshStatus();const d=await api('GET','/api/status');
if(d.status!=='syncing'){clearInterval(p);b.disabled=false;s.style.display='none';await loadLogs();alog('✅ 同步完成');}},5000);
}catch(e){alog('❌ 觸發失敗: '+e.message,1);b.disabled=false;s.style.display='none';}}

async function doFetch(){const b=$('b-fetch'),s=$('sp2');b.disabled=true;s.style.display='inline-block';
alog('🔍 測試抓取中...');
try{const d=await api('POST','/api/fetch-only');showRP('抓取結果',d.count+' 個商品',mkTbl(d.products));
alog('✅ 成功抓取 '+d.count+' 個商品');}catch(e){alog('❌ 抓取失敗: '+e.message,1);}
finally{b.disabled=false;s.style.display='none';}}

async function doPrice(){const r=prompt('請輸入日圓匯率 (1 JPY = ? TWD)','0.22');if(r===null)return;
const b=$('b-price'),s=$('sp3');b.disabled=true;s.style.display='inline-block';
alog('💰 更新價格中 (匯率: '+r+')...');
try{const d=await api('POST','/api/price-update',{rate:parseFloat(r)});alog('✅ 價格更新完成，'+(d.updated||0)+' 個 variant');
}catch(e){alog('❌ 更新失敗: '+e.message,1);}finally{b.disabled=false;s.style.display='none';}}

async function loadLogs(){try{const d=await api('GET','/api/logs');const el=$('lc');
el.innerHTML=(d.logs||[]).map(l=>{let c='';if(l.includes('✅')||l.includes('成功'))c='g';
else if(l.includes('⚠')||l.includes('跳過'))c='w';else if(l.includes('❌')||l.includes('失敗'))c='e';
return '<div class="'+c+'">'+esc(l)+'</div>';}).join('');el.scrollTop=el.scrollHeight;}catch(e){}}

function alog(m,e){const el=$('lc');const c=e?'e':(m.includes('✅')?'g':'');
const t=new Date().toLocaleTimeString('zh-TW',{hour12:false});
el.innerHTML+='<div class="'+c+'">['+t+'] '+esc(m)+'</div>';el.scrollTop=el.scrollHeight;}

function showRP(t,bg,h){const p=$('rp');p.style.display='';$('rp-t').textContent=t;$('rp-bg').textContent=bg;$('rp-bd').innerHTML=h;}

function mkTbl(ps){if(!ps||!ps.length)return '<div style="text-align:center;color:#666;padding:20px;">📦 沒有商品</div>';
let h='<table><thead><tr><th>#</th><th>Handle</th><th>名稱</th><th>價格</th><th>V</th><th>圖</th><th>狀態</th></tr></thead><tbody>';
ps.forEach((p,i)=>{h+='<tr><td style="color:#666">'+(i+1)+'</td>'
+'<td><code style="font-size:12px;background:#1a1a1a;padding:2px 6px;border-radius:3px;">'+esc(p.handle)+'</code></td>'
+'<td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+esc(p.title)+'</td>'
+'<td style="font-family:monospace;">¥'+Number(p.price).toLocaleString()+'</td>'
+'<td style="text-align:center">'+p.variants+'</td><td style="text-align:center">'+p.images+'</td>'
+'<td><span class="bd '+(p.available?'i':'o')+'">'+(p.available?'有庫存':'售罄')+'</span></td></tr>';});
h+='</tbody></table>';return h;}

refreshStatus();loadLogs();setInterval(refreshStatus,15000);
</script>
</body>
</html>`;

// ============================================================
// Routes
// ============================================================

// 控制台首頁 (HTML)
app.get('/', (req, res) => {
  const shopOk = !!(process.env.SHOPIFY_SHOP && process.env.SHOPIFY_ACCESS_TOKEN);
  const oaiOk = !!process.env.OPENAI_API_KEY;
  const keyOk = !!process.env.API_KEY;

  const html = DASHBOARD_HTML
    .replace('%%SCHEDULE%%', CRON_SCHEDULE)
    .replace('%%TZ%%', process.env.TZ || 'Asia/Taipei')
    .replace('%%C_SHOP%%', shopOk ? 'ok' : 'miss')
    .replace('%%S_SHOP%%', shopOk ? '✓ ' + process.env.SHOPIFY_SHOP : '✗ 未設定')
    .replace('%%C_OAI%%', oaiOk ? 'ok' : 'miss')
    .replace('%%S_OAI%%', oaiOk ? '✓ 已設定' : '✗ 未設定（將跳過翻譯）')
    .replace('%%C_KEY%%', keyOk ? 'ok' : 'miss')
    .replace('%%S_KEY%%', keyOk ? '✓ 已設定' : '⚠ 未設定（API 無保護）');

  res.type('html').send(html);
});

// 健康檢查
app.get('/health', (req, res) => {
  res.status(200).json({ status: 'ok' });
});

// JSON 狀態
app.get('/api/status', (req, res) => {
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

// 觸發同步
app.post('/api/sync', (req, res) => {
  if (state.isRunning) {
    return res.status(409).json({ error: '同步正在進行中' });
  }
  log('🔧 手動觸發同步');
  res.json({ message: '同步已開始', startedAt: new Date().toISOString() });
  runSync();
});

// 只抓取
app.post('/api/fetch-only', async (req, res) => {
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
app.post('/api/price-update', async (req, res) => {
  const rate = parseFloat(req.body?.rate) || undefined;
  try {
    log(`🔧 手動觸發價格更新${rate ? ` (匯率: ${rate})` : ''}`);
    const result = await updateAllPrices(rate);
    res.json({ success: true, ...result });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// 日誌
app.get('/api/logs', (req, res) => {
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
