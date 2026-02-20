"""
WORKMAN 庫存同步模組 v2.2
功能：
1. 從 Shopify 取得所有 WORKMAN 商品的來源連結
2. 到 workman.jp 檢查庫存狀態
3. v2.2: 缺貨商品直接刪除（不設草稿）
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import time
import os

SOURCE_URL = "https://workman.jp"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8', 'Accept-Language': 'ja,en;q=0.9'}

OUT_OF_STOCK_KEYWORDS = ['店舗のみのお取り扱い', 'オンラインストア販売終了', '店舗在庫を確認する', '予約受付は終了', '受付終了', '販売を終了', '取り扱いを終了']

sync_status = {"running": False, "phase": "", "progress": 0, "total": 0, "current_product": "",
    "results": {"checked": 0, "in_stock": 0, "out_of_stock": 0, "deleted": 0, "errors": 0, "page_gone": 0},
    "details": [], "errors": []}


def reset_sync_status():
    global sync_status
    sync_status = {"running": False, "phase": "", "progress": 0, "total": 0, "current_product": "",
        "results": {"checked": 0, "in_stock": 0, "out_of_stock": 0, "deleted": 0, "errors": 0, "page_gone": 0},
        "details": [], "errors": []}


def graphql_request(query, variables=None):
    shop = os.environ.get("SHOPIFY_SHOP", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    url = f"https://{shop}.myshopify.com/admin/api/2024-01/graphql.json"
    payload = {'query': query}
    if variables: payload['variables'] = variables
    return requests.post(url, headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}, json=payload, timeout=60).json()


def fetch_workman_products_with_source():
    all_products = []; cursor = None
    while True:
        ac = f', after: "{cursor}"' if cursor else ''
        query = f'{{ products(first: 50, query: "vendor:WORKMAN"{ac}) {{ edges {{ node {{ id title handle status metafield(namespace: "custom", key: "link") {{ value }} variants(first: 100) {{ edges {{ node {{ id sku inventoryItem {{ id inventoryLevels(first: 5) {{ edges {{ node {{ id quantities(names: ["available"]) {{ name quantity }} location {{ id }} }} }} }} }} }} }} }} }} cursor }} pageInfo {{ hasNextPage }} }} }}'
        result = graphql_request(query)
        for edge in result.get('data', {}).get('products', {}).get('edges', []):
            n = edge['node']
            su = n.get('metafield', {}).get('value', '') if n.get('metafield') else ''
            vs = []
            for ve in n.get('variants', {}).get('edges', []):
                vn = ve['node']; ii = vn.get('inventoryItem', {}); ils = ii.get('inventoryLevels', {}).get('edges', [])
                vi = {'id': vn['id'], 'sku': vn.get('sku', ''), 'inventory_item_id': ii.get('id', ''), 'inventory_levels': []}
                for le in ils:
                    ln = le['node']; av = 0
                    for q in ln.get('quantities', []):
                        if q['name'] == 'available': av = q['quantity']
                    vi['inventory_levels'].append({'id': ln['id'], 'location_id': ln.get('location', {}).get('id', ''), 'available': av})
                vs.append(vi)
            all_products.append({'id': n['id'], 'title': n['title'], 'handle': n['handle'], 'status': n['status'], 'source_url': su, 'variants': vs})
            cursor = edge['cursor']
        if not result.get('data', {}).get('products', {}).get('pageInfo', {}).get('hasNextPage', False): break
        time.sleep(0.5)
    print(f"[Sync] 取得 {len(all_products)} 個 WORKMAN 商品")
    return all_products


def check_workman_stock(product_url):
    result = {'available': True, 'page_exists': True, 'out_of_stock_reason': ''}
    if not product_url: return {'available': False, 'page_exists': False, 'out_of_stock_reason': '無來源連結'}
    try:
        r = requests.get(product_url, headers=HEADERS, timeout=30)
        if r.status_code == 404: return {'available': False, 'page_exists': False, 'out_of_stock_reason': '頁面已不存在 (404)'}
        if r.status_code != 200: return {'available': False, 'page_exists': False, 'out_of_stock_reason': f'HTTP {r.status_code}'}
        pt = BeautifulSoup(r.text, 'html.parser').get_text()
        for kw in OUT_OF_STOCK_KEYWORDS:
            if kw in pt: return {'available': False, 'page_exists': True, 'out_of_stock_reason': kw}
        if '売り切れ' in pt or '品切れ' in pt:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '売り切れ / 品切れ'}
        if '予約受付は終了' in pt or '受付終了' in pt:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '予約受付終了'}
        cart_button = BeautifulSoup(r.text, 'html.parser').find('input', {'value': 'カートに入れる'})
        if not cart_button:
            cart_button = BeautifulSoup(r.text, 'html.parser').find('button', string=re.compile(r'カートに入れる'))
        if not cart_button:
            if '売り切れ' in pt or '品切れ' in pt:
                return {'available': False, 'page_exists': True, 'out_of_stock_reason': '売り切れ / 品切れ'}
        return result
    except requests.exceptions.Timeout:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': '連線超時'}
    except Exception as e:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': f'檢查錯誤: {str(e)}'}


def delete_product(product_id):
    """v2.2: 刪除商品"""
    mutation = """mutation productDelete($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id}})
    errors = result.get('data', {}).get('productDelete', {}).get('userErrors', [])
    if errors:
        print(f"[Sync] ❌ 刪除失敗 {product_id}: {errors}")
        return False
    print(f"[Sync] ✓ 已刪除: {product_id}")
    return True


def run_inventory_sync():
    """v2.2: 庫存同步 — 缺貨商品直接刪除"""
    global sync_status
    reset_sync_status()
    sync_status['running'] = True; sync_status['phase'] = 'fetching'
    sync_status['current_product'] = '正在取得 Shopify 商品清單...'
    print(f"[Sync] ========== 開始庫存同步 v2.2 ==========")
    try:
        products = fetch_workman_products_with_source()
        sync_status['total'] = len(products)
        if not products:
            sync_status['current_product'] = '沒有找到 WORKMAN 商品'
            sync_status['running'] = False
            return {'success': False, 'error': 'No WORKMAN products found'}

        sync_status['phase'] = 'checking'
        for idx, product in enumerate(products):
            sync_status['progress'] = idx + 1
            sync_status['current_product'] = f"[{idx+1}/{len(products)}] {product['title'][:30]}"
            if product['status'] == 'DRAFT':
                sync_status['results']['checked'] += 1; continue
            su = product['source_url']
            if not su:
                sync_status['results']['checked'] += 1; sync_status['results']['errors'] += 1
                sync_status['errors'].append({'product': product['title'][:30], 'error': '無來源連結'}); continue

            stock = check_workman_stock(su)
            sync_status['results']['checked'] += 1

            if stock['available']:
                sync_status['results']['in_stock'] += 1
                sync_status['details'].append({'title': product['title'][:40], 'status': 'in_stock', 'source_url': su})
            else:
                sync_status['results']['out_of_stock'] += 1
                if not stock['page_exists']: sync_status['results']['page_gone'] += 1
                # v2.2: 直接刪除
                if delete_product(product['id']):
                    sync_status['results']['deleted'] += 1
                sync_status['details'].append({'title': product['title'][:40], 'status': 'out_of_stock' if stock['page_exists'] else 'page_gone',
                    'reason': stock['out_of_stock_reason'], 'source_url': su})
            time.sleep(1)

        sync_status['phase'] = 'completed'
        r = sync_status['results']
        sync_status['current_product'] = f"✅ 完成！檢查:{r['checked']} 有貨:{r['in_stock']} 缺貨:{r['out_of_stock']} 已刪除:{r['deleted']}"
        print(f"[Sync] ✅ 檢查:{r['checked']} 有貨:{r['in_stock']} 缺貨:{r['out_of_stock']} 已刪除:{r['deleted']}")
        return {'success': True, 'results': sync_status['results']}
    except Exception as e:
        sync_status['errors'].append({'error': str(e)})
        sync_status['phase'] = 'error'
        print(f"[Sync] ❌ {e}")
        return {'success': False, 'error': str(e)}
    finally:
        sync_status['running'] = False
