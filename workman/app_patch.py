"""
====================================================================
  WORKMAN app.py 庫存同步整合補丁
  
  把以下程式碼加入 app.py 即可
====================================================================
"""

# ============================================================
# 1. 在 app.py 最上方的 import 區域加入：
# ============================================================

from inventory_sync import (
    run_inventory_sync,
    sync_status as inventory_sync_status,
    check_workman_stock
)


# ============================================================
# 2. 在 product_to_jsonl_entry 函數裡，把 inventoryPolicy 改掉：
#    搜尋所有的 "inventoryPolicy": "CONTINUE"
#    全部改成 "inventoryPolicy": "DENY"
# ============================================================

# 舊的（允許超賣）：
# "inventoryPolicy": "CONTINUE",

# 新的（庫存為0時禁止下單）：
# "inventoryPolicy": "DENY",


# ============================================================
# 3. 在 Flask 路由區塊加入以下 API endpoints：
# ============================================================

@app.route('/api/inventory_sync')
def api_inventory_sync():
    """
    庫存同步 API（背景執行）
    
    用法：GET /api/inventory_sync
    
    可用 cron-job.org 設定定時執行（建議每 6 小時）
    """
    if inventory_sync_status.get('running', False):
        return jsonify({
            'success': False,
            'error': '庫存同步正在執行中',
            'progress': inventory_sync_status.get('progress', 0),
            'total': inventory_sync_status.get('total', 0)
        })
    
    thread = threading.Thread(target=run_inventory_sync, daemon=False)
    thread.start()
    
    return jsonify({
        'success': True,
        'message': '已開始庫存同步',
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S')
    })


@app.route('/api/inventory_sync_status')
def api_inventory_sync_status():
    """查詢庫存同步進度"""
    return jsonify(inventory_sync_status)


@app.route('/api/check_stock')
def api_check_stock():
    """
    檢查單一商品的官網庫存狀態
    
    用法：GET /api/check_stock?url=https://workman.jp/shop/g/g2300068265020/
    """
    from flask import request
    url = request.args.get('url', '')
    
    if not url:
        return jsonify({'error': '請提供 url 參數'})
    
    result = check_workman_stock(url)
    return jsonify(result)


# ============================================================
# 4. 在首頁 HTML（index 函數的 return 字串中）加入庫存同步卡片：
#    放在「📢 發布到銷售管道」卡片之後
# ============================================================

INVENTORY_SYNC_HTML = '''
    <div class="card" style="border: 2px solid #ff9800; background: #fff8e1;">
        <h3>🔄 庫存同步</h3>
        <p>檢查 WORKMAN 官網庫存狀態，自動將缺貨商品設為草稿。</p>
        <p style="font-size:13px;color:#666;">
            判斷條件：「店舗のみのお取り扱い」「オンラインストア販売終了」「店舗在庫を確認する」
        </p>
        <button class="btn" style="background:#ff9800;color:white;font-size:18px;padding:15px 30px;" 
                onclick="startInventorySync()">🔄 開始庫存同步</button>
        <button class="btn btn-check" onclick="checkInventorySyncStatus()">📊 查看同步狀態</button>
        <button class="btn btn-check" onclick="checkSingleStock()">🔍 檢查單一商品</button>
        <div id="syncResult" style="margin-top:10px;padding:10px;background:#fff;border-radius:5px;display:none;"></div>
    </div>
'''

INVENTORY_SYNC_JS = '''
        function startInventorySync() {
            if (!confirm('確定要開始庫存同步？\\n\\n將會檢查所有 WORKMAN 商品的官網庫存狀態，缺貨的商品會被設為草稿。')) return;
            
            log('🔄 開始庫存同步...');
            document.getElementById('status').textContent = '庫存同步中...';
            
            fetch('/api/inventory_sync')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log('❌ ' + data.error);
                    } else {
                        log('🔄 庫存同步已啟動，請等待...');
                        pollInventorySyncStatus();
                    }
                })
                .catch(err => log('❌ 啟動失敗: ' + err));
        }
        
        function pollInventorySyncStatus() {
            fetch('/api/inventory_sync_status')
                .then(r => r.json())
                .then(data => {
                    let statusText = data.current_product || '處理中...';
                    if (data.total > 0) {
                        statusText += ` (${data.progress}/${data.total})`;
                    }
                    document.getElementById('status').textContent = statusText;
                    
                    // 更新進度條
                    if (data.total > 0) {
                        let pct = (data.progress / data.total * 100).toFixed(1);
                        document.getElementById('progressBar').style.width = pct + '%';
                    }
                    
                    if (data.running) {
                        setTimeout(pollInventorySyncStatus, 2000);
                    } else if (data.phase === 'completed') {
                        let r = data.results;
                        log(`✅ 庫存同步完成！`);
                        log(`   檢查: ${r.checked} 個商品`);
                        log(`   有貨: ${r.in_stock}`);
                        log(`   缺貨: ${r.out_of_stock}`);
                        log(`   設為草稿: ${r.draft_set}`);
                        log(`   庫存歸零: ${r.inventory_zeroed}`);
                        log(`   頁面消失: ${r.page_gone}`);
                        if (r.errors > 0) log(`   錯誤: ${r.errors}`);
                        
                        showSyncDetails(data);
                    } else if (data.phase === 'error') {
                        log('❌ 庫存同步失敗');
                        if (data.errors.length > 0) {
                            data.errors.forEach(e => log('   ' + (e.error || JSON.stringify(e))));
                        }
                    }
                })
                .catch(err => log('❌ 查詢狀態失敗: ' + err));
        }
        
        function checkInventorySyncStatus() {
            fetch('/api/inventory_sync_status')
                .then(r => r.json())
                .then(data => {
                    if (data.phase === 'completed') {
                        let r = data.results;
                        log(`📊 上次同步結果: 檢查 ${r.checked}, 有貨 ${r.in_stock}, 缺貨 ${r.out_of_stock}, 草稿 ${r.draft_set}`);
                        showSyncDetails(data);
                    } else if (data.running) {
                        log(`🔄 同步進行中: ${data.progress}/${data.total} - ${data.current_product}`);
                    } else {
                        log('📊 尚未執行庫存同步');
                    }
                });
        }
        
        function showSyncDetails(data) {
            const div = document.getElementById('syncResult');
            if (!data.details || data.details.length === 0) {
                div.style.display = 'none';
                return;
            }
            
            let html = '<h4>同步詳情</h4><table>';
            html += '<tr><th>商品</th><th>狀態</th><th>原因</th></tr>';
            
            // 只顯示缺貨的
            let outOfStock = data.details.filter(d => d.status !== 'in_stock');
            if (outOfStock.length === 0) {
                html += '<tr><td colspan="3">🎉 所有商品都有貨！</td></tr>';
            } else {
                outOfStock.forEach(d => {
                    let statusEmoji = d.status === 'page_gone' ? '🚫' : '❌';
                    html += `<tr><td>${d.title}</td><td>${statusEmoji} ${d.status}</td><td>${d.reason || ''}</td></tr>`;
                });
            }
            
            html += '</table>';
            html += `<p style="font-size:12px;color:#666;">顯示 ${outOfStock.length} 個缺貨商品（共 ${data.details.length} 個已檢查）</p>`;
            
            div.innerHTML = html;
            div.style.display = 'block';
        }
        
        function checkSingleStock() {
            let url = prompt('請輸入 WORKMAN 商品 URL：\\n例如: https://workman.jp/shop/g/g2300068265020/');
            if (!url) return;
            
            log('🔍 檢查: ' + url);
            fetch('/api/check_stock?url=' + encodeURIComponent(url))
                .then(r => r.json())
                .then(data => {
                    if (data.available) {
                        log('✅ 有貨！可線上購買');
                    } else {
                        log('❌ 缺貨: ' + (data.out_of_stock_reason || '未知原因'));
                    }
                    log('   頁面存在: ' + (data.page_exists ? '是' : '否'));
                })
                .catch(err => log('❌ 檢查失敗: ' + err));
        }
'''
