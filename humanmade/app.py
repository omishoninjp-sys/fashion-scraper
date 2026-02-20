"""
Human Made 商品爬蟲 + Shopify 上架工具 v2.2
功能：
1. 從 humanmade.jp Shopify JSON API 爬取所有商品
2. 完整複製 Variants（顏色、尺寸等選項）
3. 每個 Variant 獨立計算售價
4. 價格同步：已存在商品若價格變動則自動更新
5. v2.2: 無庫存/下架商品直接刪除（不設草稿）
"""

from flask import Flask, jsonify
import requests
import re
import json
import os
import time
import threading
import base64

app = Flask(__name__)

SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
SOURCE_URL = "https://humanmade.jp"
PRODUCTS_JSON_URL = "https://humanmade.jp/collections/all/products.json"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
DEFAULT_WEIGHT = 0.5

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Accept': 'application/json'}

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "skipped_exists": 0, "filtered_by_price": 0, "out_of_stock": 0,
    "deleted": 0, "price_updated": 0
}


def load_shopify_token():
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://','').replace('http://','').replace('.myshopify.com','').strip('/')
        return True
    tf = "shopify_token.json"
    if os.path.exists(tf):
        with open(tf, 'r') as f:
            d = json.load(f)
            SHOPIFY_ACCESS_TOKEN = d.get('access_token', '')
            s = d.get('shop', '')
            if s: SHOPIFY_SHOP = s.replace('https://','').replace('http://','').replace('.myshopify.com','').strip('/')
            return True
    return False


def get_shopify_headers():
    return {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


def calculate_selling_price(cost, weight):
    if not cost or cost <= 0: return 0
    weight = weight if weight and weight > 0 else DEFAULT_WEIGHT
    return round((cost + weight * 1250) / 0.7)


def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文/英文）：{title}
商品說明：{description[:1500] if description else ''}

請回傳 JSON 格式（不要加 markdown 標記）：
{{"title":"翻譯後的商品名稱（前面加上 Human Made）","description":"翻譯後的商品說明（HTML，用<br>換行）","page_title":"SEO標題50-60字","meta_description":"SEO描述100字內"}}

規則：1. Human Made 潮流品牌 2. 開頭「Human Made」3. 禁日文 4. 自然流暢 5. 只回傳JSON"""
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。輸出禁止任何日文字元。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1000}, timeout=60)
        if r.status_code == 200:
            c = r.json()['choices'][0]['message']['content'].strip()
            if c.startswith('```'): c = c.split('\n', 1)[1]
            if c.endswith('```'): c = c.rsplit('```', 1)[0]
            t = json.loads(c.strip())
            tt = t.get('title', title)
            if not tt.startswith('Human Made'): tt = f"Human Made {tt}"
            return {'success': True, 'title': tt, 'description': t.get('description', description),
                    'page_title': t.get('page_title', ''), 'meta_description': t.get('meta_description', '')}
        return {'success': False, 'title': f"Human Made {title}", 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {'success': False, 'title': f"Human Made {title}", 'description': description, 'page_title': '', 'meta_description': ''}


def download_image_to_base64(img_url, max_retries=3):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
               'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8', 'Referer': SOURCE_URL + '/'}
    if '_small' in img_url or '_thumbnail' in img_url:
        img_url = re.sub(r'_\d+x\d*\.', '.', img_url)
        img_url = re.sub(r'_(small|thumbnail|medium)\.', '.', img_url)
    for attempt in range(max_retries):
        try:
            r = requests.get(img_url, headers=headers, timeout=30)
            if r.status_code == 200:
                ct = r.headers.get('Content-Type', 'image/jpeg')
                fmt = 'image/png' if 'png' in ct else 'image/webp' if 'webp' in ct else 'image/jpeg'
                return {'success': True, 'base64': base64.b64encode(r.content).decode('utf-8'), 'content_type': fmt}
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次異常: {e}")
        time.sleep(1)
    return {'success': False}


# ========== Shopify 工具函數 ==========

def get_collection_products_with_details(collection_id):
    """取得 Collection 內的商品（包含 variants 詳細資訊，用於價格比對）"""
    products_map = {}
    if not collection_id: return products_map
    url = shopify_api_url(f"collections/{collection_id}/products.json?limit=250")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200: break
        for product in response.json().get('products', []):
            product_id = product.get('id'); handle = product.get('handle')
            if handle and product_id:
                variants_info = []
                for v in product.get('variants', []):
                    variant_id = v.get('id'); cost = None
                    vr = requests.get(shopify_api_url(f"variants/{variant_id}.json"), headers=get_shopify_headers())
                    if vr.status_code == 200: cost = vr.json().get('variant', {}).get('cost')
                    time.sleep(0.1)
                    variants_info.append({'variant_id': variant_id, 'price': v.get('price'), 'cost': cost,
                        'sku': v.get('sku'), 'option1': v.get('option1'), 'option2': v.get('option2'), 'option3': v.get('option3')})
                products_map[handle] = {'product_id': product_id, 'variants': variants_info}
        lh = response.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    print(f"[INFO] Collection 內有 {len(products_map)} 個商品")
    return products_map


def delete_product(product_id):
    """v2.2: 刪除商品"""
    r = requests.delete(shopify_api_url(f"products/{product_id}.json"), headers=get_shopify_headers())
    if r.status_code == 200:
        print(f"[已刪除] Product ID: {product_id}")
        return True
    return False


def publish_collection_to_all_channels(collection_id):
    gu = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    hd = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    r = requests.post(gu, headers=hd, json={'query': '{ publications(first:20){ edges{ node{ id name }}}}'})
    if r.status_code != 200: return False
    pubs = r.json().get('data', {}).get('publications', {}).get('edges', [])
    seen = set(); uq = []
    for p in pubs:
        if p['node']['name'] not in seen: seen.add(p['node']['name']); uq.append(p['node'])
    mut = """mutation publishablePublish($id:ID!,$input:[PublicationInput!]!){publishablePublish(id:$id,input:$input){userErrors{field message}}}"""
    requests.post(gu, headers=hd, json={'query': mut, 'variables': {"id": f"gid://shopify/Collection/{collection_id}", "input": [{"publicationId": p['id']} for p in uq]}})
    return True


def get_or_create_collection(ct="Human Made"):
    r = requests.get(shopify_api_url(f'custom_collections.json?title={ct}'), headers=get_shopify_headers())
    if r.status_code == 200:
        for c in r.json().get('custom_collections', []):
            if c['title'] == ct:
                publish_collection_to_all_channels(c['id']); return c['id']
    r = requests.post(shopify_api_url('custom_collections.json'), headers=get_shopify_headers(),
        json={'custom_collection': {'title': ct, 'published': True}})
    if r.status_code == 201:
        cid = r.json()['custom_collection']['id']
        publish_collection_to_all_channels(cid); return cid
    return None


def add_product_to_collection(pid, cid):
    return requests.post(shopify_api_url('collects.json'), headers=get_shopify_headers(),
        json={'collect': {'product_id': pid, 'collection_id': cid}}).status_code == 201


def publish_to_all_channels(pid):
    gu = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    hd = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    r = requests.post(gu, headers=hd, json={'query': '{ publications(first:20){ edges{ node{ id name }}}}'})
    if r.status_code != 200: return False
    pubs = r.json().get('data', {}).get('publications', {}).get('edges', [])
    seen = set(); uq = []
    for p in pubs:
        if p['node']['name'] not in seen: seen.add(p['node']['name']); uq.append(p['node'])
    mut = """mutation publishablePublish($id:ID!,$input:[PublicationInput!]!){publishablePublish(id:$id,input:$input){userErrors{field message}}}"""
    requests.post(gu, headers=hd, json={'query': mut, 'variables': {"id": f"gid://shopify/Product/{pid}", "input": [{"publicationId": p['id']} for p in uq]}})
    return True


def fetch_all_products():
    products = []; page = 1
    while True:
        url = f"{PRODUCTS_JSON_URL}?limit=250&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200: break
            pp = r.json().get('products', [])
            if not pp: break
            products.extend(pp)
            if len(pp) < 250: break
            page += 1; time.sleep(0.5)
        except Exception as e:
            print(f"[ERROR] {e}"); break
    print(f"[INFO] 共取得 {len(products)} 個商品")
    return products


def check_product_stock(product):
    return any(v.get('available', False) for v in product.get('variants', []))


def update_product_prices(source_product, existing_product_info):
    product_id = existing_product_info['product_id']
    existing_variants = existing_product_info['variants']
    source_variants = source_product.get('variants', [])
    updated = False
    evm = {}
    for ev in existing_variants:
        evm[f"{ev.get('option1','')}|{ev.get('option2','')}|{ev.get('option3','')}"] = ev
    for sv in source_variants:
        key = f"{sv.get('option1','')}|{sv.get('option2','')}|{sv.get('option3','')}"
        if key in evm:
            ev = evm[key]
            source_cost = float(sv.get('price', 0))
            shopify_cost = float(ev.get('cost', 0)) if ev.get('cost') else 0
            if abs(source_cost - shopify_cost) >= 1:
                vid = ev['variant_id']
                weight = float(sv.get('grams', 0)) / 1000 if sv.get('grams') else DEFAULT_WEIGHT
                new_price = calculate_selling_price(source_cost, weight)
                r = requests.put(shopify_api_url(f"variants/{vid}.json"), headers=get_shopify_headers(),
                    json={'variant': {'id': vid, 'price': f"{new_price:.2f}", 'cost': f"{source_cost:.2f}"}})
                if r.status_code == 200: updated = True
    return updated


def upload_to_shopify(source_product, collection_id=None):
    original_title = source_product.get('title', '')
    body_html = source_product.get('body_html', '')
    handle = source_product.get('handle', '')
    translated = translate_with_chatgpt(original_title, body_html)

    options = [{'name': opt.get('name', 'Option'), 'values': opt.get('values', [])} for opt in source_product.get('options', [])]

    variants = []
    source_variants = source_product.get('variants', [])
    for sv in source_variants:
        cost = float(sv.get('price', 0))
        weight = float(sv.get('grams', 0)) / 1000 if sv.get('grams') else DEFAULT_WEIGHT
        selling_price = calculate_selling_price(cost, weight)
        vd = {'title': sv.get('title', 'Default'), 'price': f"{selling_price:.2f}", 'sku': sv.get('sku', ''),
              'weight': weight, 'weight_unit': 'kg', 'inventory_management': None,
              'inventory_policy': 'continue', 'requires_shipping': True}
        if sv.get('option1'): vd['option1'] = sv['option1']
        if sv.get('option2'): vd['option2'] = sv['option2']
        if sv.get('option3'): vd['option3'] = sv['option3']
        variants.append({'variant_data': vd, 'cost': cost, 'source_id': sv.get('id'), 'image_id': sv.get('image_id')})

    source_images = source_product.get('images', [])
    images_base64 = []; image_id_to_position = {}
    for idx, img in enumerate(source_images):
        img_url = img.get('src', '')
        if not img_url: continue
        if img_url.startswith('//'): img_url = 'https:' + img_url
        result = download_image_to_base64(img_url)
        if result['success']:
            image_data = {'attachment': result['base64'], 'position': idx + 1, 'filename': f"humanmade_{handle}_{idx+1}.jpg"}
            svids = img.get('variant_ids', [])
            if svids: image_data['_source_variant_ids'] = svids
            images_base64.append(image_data)
            image_id_to_position[img.get('id')] = idx + 1
        time.sleep(0.3)

    images_for_upload = [{'attachment': i['attachment'], 'position': i['position'], 'filename': i['filename']} for i in images_base64]

    shopify_product = {'product': {
        'title': translated['title'], 'body_html': translated['description'], 'vendor': 'Human Made',
        'product_type': source_product.get('product_type', ''), 'status': 'active', 'published': True,
        'handle': f"humanmade-{handle}",
        'options': options if options else [{'name': 'Title', 'values': ['Default Title']}],
        'variants': [v['variant_data'] for v in variants], 'images': images_for_upload,
        'tags': f"Human Made, 日本, 潮流, 服飾, {source_product.get('product_type', '')}",
        'metafields_global_title_tag': translated['page_title'],
        'metafields_global_description_tag': translated['meta_description'],
        'metafields': [{'namespace': 'custom', 'key': 'link', 'value': f"{SOURCE_URL}/products/{handle}", 'type': 'url'}]}}

    response = requests.post(shopify_api_url('products.json'), headers=get_shopify_headers(), json=shopify_product)

    if response.status_code == 201:
        created_product = response.json()['product']
        product_id = created_product['id']
        created_variants = created_product.get('variants', [])
        created_images = created_product.get('images', [])

        for idx, cv in enumerate(created_variants):
            if idx < len(variants):
                requests.put(shopify_api_url(f"variants/{cv['id']}.json"), headers=get_shopify_headers(),
                    json={'variant': {'id': cv['id'], 'cost': f"{variants[idx]['cost']:.2f}"}})

        source_to_created_variant = {}
        for idx, sv in enumerate(source_variants):
            if idx < len(created_variants): source_to_created_variant[sv.get('id')] = created_variants[idx]['id']

        for idx, ci in enumerate(created_images):
            if idx < len(images_base64):
                svids = images_base64[idx].get('_source_variant_ids', [])
                if svids:
                    nvids = [source_to_created_variant[s] for s in svids if s in source_to_created_variant]
                    if nvids:
                        requests.put(shopify_api_url(f"products/{product_id}/images/{ci['id']}.json"),
                            headers=get_shopify_headers(), json={'image': {'id': ci['id'], 'variant_ids': nvids}})

        if collection_id: add_product_to_collection(product_id, collection_id)
        publish_to_all_channels(product_id)
        return {'success': True, 'product': created_product, 'translated': translated, 'variants_count': len(created_variants)}
    else:
        return {'success': False, 'error': response.text}


# ========== 主流程 ==========

def run_scrape():
    """v2.2: 下架/缺貨商品直接刪除"""
    global scrape_status
    try:
        scrape_status = {"running": True, "progress": 0, "total": 0, "current_product": "",
            "products": [], "errors": [], "uploaded": 0, "skipped": 0,
            "skipped_exists": 0, "filtered_by_price": 0, "out_of_stock": 0,
            "deleted": 0, "price_updated": 0}

        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("Human Made")

        scrape_status['current_product'] = "正在取得 Collection 內商品（含價格資訊）..."
        collection_products_map = get_collection_products_with_details(collection_id)
        existing_handles = set(collection_products_map.keys())

        scrape_status['current_product'] = "正在從 Human Made 取得商品列表..."
        product_list = fetch_all_products()
        scrape_status['total'] = len(product_list)

        in_stock_handles = set()

        for idx, product in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            handle = product.get('handle', '')
            title = product.get('title', '')
            my_handle = f"humanmade-{handle}"
            scrape_status['current_product'] = f"處理中: {title[:30]}"
            has_stock = check_product_stock(product)

            if has_stock:
                in_stock_handles.add(my_handle)

            if my_handle in existing_handles:
                existing_info = collection_products_map[my_handle]
                if has_stock:
                    scrape_status['current_product'] = f"檢查價格: {title[:30]}"
                    if update_product_prices(product, existing_info):
                        scrape_status['price_updated'] += 1
                    scrape_status['skipped_exists'] += 1
                    scrape_status['skipped'] += 1
                else:
                    scrape_status['skipped'] += 1
                continue

            variants = product.get('variants', [])
            min_price = min((float(v.get('price', 0)) for v in variants), default=0) if variants else 0
            if min_price < MIN_PRICE:
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1
                continue

            if not has_stock:
                scrape_status['out_of_stock'] += 1
                scrape_status['skipped'] += 1
                continue

            result = upload_to_shopify(product, collection_id)
            if result['success']:
                existing_handles.add(my_handle)
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'handle': handle, 'title': result.get('translated', {}).get('title', title),
                    'original_title': title, 'variants_count': result.get('variants_count', 0), 'status': 'success'})
            else:
                scrape_status['errors'].append({'handle': handle, 'title': title, 'error': result['error']})
            time.sleep(1)

        # === v2.2: 無庫存/下架商品直接刪除（不設草稿）===
        scrape_status['current_product'] = "正在清理下架/缺貨商品..."
        for my_handle, product_info in collection_products_map.items():
            if my_handle not in in_stock_handles:
                scrape_status['current_product'] = f"刪除: {my_handle}"
                print(f"[刪除] {my_handle} - 無庫存或已下架")
                if delete_product(product_info['product_id']):
                    scrape_status['deleted'] += 1
                time.sleep(0.5)

        scrape_status['current_product'] = "完成！"
    except Exception as e:
        import traceback; traceback.print_exc()
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False


# ========== Flask 路由 + 前端 ==========

@app.route('/')
def index():
    token_loaded = load_shopify_token()
    token_status = '<span style="color: green;">✓ 已載入</span>' if token_loaded else '<span style="color: red;">✗ 未設定</span>'
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Human Made 爬蟲工具</title>
<style>*{{box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}}h1{{color:#333;border-bottom:2px solid #E74C3C;padding-bottom:10px}}.card{{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}.btn{{background:#E74C3C;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px}}.btn:hover{{background:#C0392B}}.btn:disabled{{background:#ccc;cursor:not-allowed}}.btn-secondary{{background:#3498db}}.progress-bar{{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}}.progress-fill{{height:100%;background:linear-gradient(90deg,#E74C3C,#F39C12);transition:width 0.3s}}.status{{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}}.log{{max-height:300px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}}.stats{{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}}.stat{{flex:1;min-width:80px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}}.stat-number{{font-size:24px;font-weight:bold;color:#E74C3C}}.stat-label{{font-size:11px;color:#666;margin-top:5px}}</style></head>
<body>
<h1>❤️ Human Made 爬蟲工具 <small style="font-size:14px;color:#999;">v2.2</small></h1>
<div class="card"><h3>Shopify 連線狀態</h3><p>Token: {token_status}</p>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button></div>
<div class="card"><h3>開始爬取</h3>
<p>爬取 humanmade.jp 所有商品並上架到 Shopify（含 Variants）</p>
<p style="color:#666;font-size:14px;">※ 成本價低於 ¥1000 或無庫存的商品將自動跳過</p>
<p style="color:#666;font-size:14px;">※ 已存在商品會自動同步價格</p>
<p style="color:#e67e22;font-size:14px;font-weight:bold;">※ v2.2 缺貨/下架商品自動刪除</p>
<button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
<div id="progressSection" style="display:none;">
<div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
<div class="status" id="statusText">準備中...</div>
<div class="stats">
<div class="stat"><div class="stat-number" id="uploadedCount">0</div><div class="stat-label">已上架</div></div>
<div class="stat"><div class="stat-number" id="priceUpdatedCount" style="color:#3498db;">0</div><div class="stat-label">價格更新</div></div>
<div class="stat"><div class="stat-number" id="skippedCount">0</div><div class="stat-label">已跳過</div></div>
<div class="stat"><div class="stat-number" id="filteredCount">0</div><div class="stat-label">價格過濾</div></div>
<div class="stat"><div class="stat-number" id="outOfStockCount">0</div><div class="stat-label">無庫存</div></div>
<div class="stat"><div class="stat-number" id="deletedCount" style="color:#e67e22;">0</div><div class="stat-label">已刪除</div></div>
<div class="stat"><div class="stat-number" id="errorCount">0</div><div class="stat-label">錯誤</div></div>
</div></div></div>
<div class="card"><h3>執行日誌</h3><div class="log" id="logArea">等待開始...</div></div>
<script>
let pollInterval=null;
function log(msg,type=''){{const a=document.getElementById('logArea');const t=new Date().toLocaleTimeString();const c=type==='success'?'#4ec9b0':type==='error'?'#f14c4c':'#d4d4d4';a.innerHTML+='<div style="color:'+c+'">['+t+'] '+msg+'</div>';a.scrollTop=a.scrollHeight}}
function clearLog(){{document.getElementById('logArea').innerHTML=''}}
async function testShopify(){{log('測試 Shopify 連線...');try{{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ 連線成功！','success');else log('✗ 連線失敗: '+d.error,'error')}}catch(e){{log('✗ '+e.message,'error')}}}}
async function startScrape(){{clearLog();log('開始爬取流程...');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';try{{const r=await fetch('/api/start',{{method:'POST'}});const d=await r.json();if(!d.success){{log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}}log('✓ 爬取任務已啟動','success');pollInterval=setInterval(pollStatus,1000)}}catch(e){{log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}}}
async function pollStatus(){{try{{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('priceUpdatedCount').textContent=d.price_updated||0;document.getElementById('skippedCount').textContent=d.skipped;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('outOfStockCount').textContent=d.out_of_stock||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(!d.running&&d.progress>0){{clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;log('========== 爬取完成 ==========','success')}}}}catch(e){{}}}}
</script></body></html>'''


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/start', methods=['GET', 'POST'])
def api_start():
    global scrape_status
    if scrape_status['running']: return jsonify({'success': False, 'error': '爬取正在進行中'})
    if not load_shopify_token(): return jsonify({'success': False, 'error': '環境變數未設定'})
    threading.Thread(target=run_scrape).start()
    return jsonify({'success': True, 'message': 'Human Made 爬蟲已啟動'})


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token(): return jsonify({'success': False, 'error': '環境變數未設定'})
    r = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if r.status_code == 200: return jsonify({'success': True, 'shop': r.json()['shop']})
    return jsonify({'success': False, 'error': r.text}), 400


@app.route('/api/test-scrape')
def test_scrape():
    products = fetch_all_products()
    summaries = [{'handle': p.get('handle'), 'title': p.get('title'), 'variants_count': len(p.get('variants', [])),
        'images_count': len(p.get('images', [])), 'options': [o.get('name') for o in p.get('options', [])],
        'has_stock': check_product_stock(p),
        'min_price': min((float(v.get('price', 0)) for v in p.get('variants', [])), default=0)} for p in products[:3]]
    return jsonify({'total_count': len(products), 'samples': summaries})


if __name__ == '__main__':
    print("Human Made 爬蟲工具 v2.2")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
