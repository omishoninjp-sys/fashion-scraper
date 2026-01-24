"""
Human Made 商品爬蟲 + Shopify 上架工具
功能：
1. 從 humanmade.jp Shopify JSON API 爬取所有商品
2. 完整複製 Variants（顏色、尺寸等選項）
3. 圖片對應 Variant
4. 每個 Variant 獨立計算售價
5. 上架到 Shopify
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

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""

SOURCE_URL = "https://humanmade.jp"
PRODUCTS_JSON_URL = "https://humanmade.jp/collections/all/products.json"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
DEFAULT_WEIGHT = 0.5  # 預設重量 kg

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
}

scrape_status = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_product": "",
    "products": [],
    "errors": [],
    "uploaded": 0,
    "skipped": 0,
    "skipped_exists": 0,
    "filtered_by_price": 0,
    "deleted": 0
}


def load_shopify_token():
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP
    
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
        print(f"[設定] 從環境變數載入 - 商店: {SHOPIFY_SHOP}")
        return True
    
    token_file = "shopify_token.json"
    if os.path.exists(token_file):
        with open(token_file, 'r') as f:
            data = json.load(f)
            SHOPIFY_ACCESS_TOKEN = data.get('access_token', '')
            shop = data.get('shop', '')
            if shop:
                SHOPIFY_SHOP = shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
            print(f"[設定] 從檔案載入 - 商店: {SHOPIFY_SHOP}")
            return True
    return False


def get_shopify_headers():
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


def calculate_selling_price(cost, weight):
    """售價 = [進貨價 + (重量 * 1250)] / 0.7"""
    if not cost or cost <= 0:
        return 0
    weight = weight if weight and weight > 0 else DEFAULT_WEIGHT
    shipping_cost = weight * 1250
    price = (cost + shipping_cost) / 0.7
    return round(price)


def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文/英文）：{title}
商品說明：{description[:1500] if description else ''}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文或英文，簡潔有力，前面加上 Human Made）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示，每個重點用 <br> 換行）",
    "page_title": "SEO 頁面標題（繁體中文，包含品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

重要規則：
1. 這是日本潮流品牌 Human Made 的商品
2. 商品名稱如果是英文可以保留英文，但開頭必須是「Human Made」
3. 翻譯要自然流暢，不要生硬
4. 【禁止使用任何日文】所有內容必須是繁體中文或英文
5. SEO 內容要包含：Human Made、日本、潮流、服飾等關鍵字
6. description 中每個重點用 <br> 換行，方便閱讀
7. 只回傳 JSON，不要其他文字"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。你的輸出必須完全使用繁體中文和英文，絕對禁止出現任何日文字元。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0,
                "max_tokens": 1000
            },
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            
            content = content.strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content.rsplit('```', 1)[0]
            content = content.strip()
            
            translated = json.loads(content)
            
            trans_title = translated.get('title', title)
            if not trans_title.startswith('Human Made'):
                trans_title = f"Human Made {trans_title}"
            
            return {
                'success': True,
                'title': trans_title,
                'description': translated.get('description', description),
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"Human Made {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"Human Made {title}",
            'description': description,
            'page_title': '',
            'meta_description': ''
        }


def download_image_to_base64(img_url, max_retries=3):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': SOURCE_URL + '/',
    }
    
    # 確保使用較大尺寸的圖片
    if '_small' in img_url or '_thumbnail' in img_url:
        img_url = re.sub(r'_\d+x\d*\.', '.', img_url)
        img_url = re.sub(r'_(small|thumbnail|medium)\.', '.', img_url)
    
    for attempt in range(max_retries):
        try:
            response = requests.get(img_url, headers=headers, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', 'image/jpeg')
                if 'jpeg' in content_type or 'jpg' in content_type:
                    img_format = 'image/jpeg'
                elif 'png' in content_type:
                    img_format = 'image/png'
                elif 'webp' in content_type:
                    img_format = 'image/webp'
                else:
                    img_format = 'image/jpeg'
                
                img_base64 = base64.b64encode(response.content).decode('utf-8')
                return {'success': True, 'base64': img_base64, 'content_type': img_format}
            else:
                print(f"[圖片下載] 第 {attempt+1} 次嘗試失敗: HTTP {response.status_code}")
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次嘗試異常: {e}")
        time.sleep(1)
    
    return {'success': False}


def get_existing_products_map():
    products_map = {}
    url = shopify_api_url("products.json?limit=250")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            print(f"Error fetching products: {response.status_code}")
            break
        
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            # 用 handle 作為唯一識別（因為 variants 可能有多個 SKU）
            handle = product.get('handle')
            if handle and product_id:
                products_map[handle] = product_id
            # 也記錄 SKU
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    products_map[f"sku:{sku}"] = product_id
        
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    return products_map


def get_collection_products_map(collection_id):
    products_map = {}
    if not collection_id:
        return products_map
    
    url = shopify_api_url(f"collections/{collection_id}/products.json?limit=250")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            break
        
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            handle = product.get('handle')
            if handle and product_id:
                products_map[handle] = product_id
        
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    print(f"[INFO] Collection 內有 {len(products_map)} 個商品")
    return products_map


def set_product_to_draft(product_id):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {"id": product_id, "status": "draft"}
    })
    if response.status_code == 200:
        print(f"[設為草稿] Product ID: {product_id}")
        return True
    return False


def get_or_create_collection(collection_title="Human Made"):
    response = requests.get(
        shopify_api_url(f'custom_collections.json?title={collection_title}'),
        headers=get_shopify_headers()
    )
    
    if response.status_code == 200:
        collections = response.json().get('custom_collections', [])
        for col in collections:
            if col['title'] == collection_title:
                print(f"[INFO] 找到現有 Collection: {collection_title} (ID: {col['id']})")
                return col['id']
    
    response = requests.post(
        shopify_api_url('custom_collections.json'),
        headers=get_shopify_headers(),
        json={'custom_collection': {'title': collection_title, 'published': True}}
    )
    
    if response.status_code == 201:
        collection_id = response.json()['custom_collection']['id']
        print(f"[INFO] 建立新 Collection: {collection_title} (ID: {collection_id})")
        return collection_id
    
    print(f"[ERROR] 無法建立 Collection: {response.text}")
    return None


def add_product_to_collection(product_id, collection_id):
    response = requests.post(
        shopify_api_url('collects.json'),
        headers=get_shopify_headers(),
        json={'collect': {'product_id': product_id, 'collection_id': collection_id}}
    )
    return response.status_code == 201


def publish_to_all_channels(product_id):
    print(f"[發布] 正在發布商品 {product_id} 到所有渠道...")
    
    graphql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }
    
    query = """
    {
      publications(first: 20) {
        edges {
          node {
            id
            name
          }
        }
      }
    }
    """
    
    response = requests.post(graphql_url, headers=headers, json={'query': query})
    
    if response.status_code != 200:
        return False
    
    result = response.json()
    publications = result.get('data', {}).get('publications', {}).get('edges', [])
    
    seen_names = set()
    unique_publications = []
    for pub in publications:
        name = pub['node']['name']
        if name not in seen_names:
            seen_names.add(name)
            unique_publications.append(pub['node'])
    
    publication_inputs = [{"publicationId": pub['id']} for pub in unique_publications]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount { count }
        }
        userErrors { field message }
      }
    }
    """
    
    variables = {
        "id": f"gid://shopify/Product/{product_id}",
        "input": publication_inputs
    }
    
    pub_response = requests.post(graphql_url, headers=headers, json={
        'query': mutation,
        'variables': variables
    })
    
    return pub_response.status_code == 200


def fetch_all_products():
    """從 Human Made Shopify JSON API 取得所有商品"""
    products = []
    page = 1
    per_page = 250
    
    while True:
        url = f"{PRODUCTS_JSON_URL}?limit={per_page}&page={page}"
        print(f"[INFO] 正在載入第 {page} 頁...")
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            
            if response.status_code != 200:
                print(f"[ERROR] 載入失敗: HTTP {response.status_code}")
                break
            
            data = response.json()
            page_products = data.get('products', [])
            
            if not page_products:
                print(f"[INFO] 第 {page} 頁沒有商品，結束")
                break
            
            products.extend(page_products)
            print(f"[INFO] 第 {page} 頁取得 {len(page_products)} 個商品，累計 {len(products)} 個")
            
            if len(page_products) < per_page:
                break
            
            page += 1
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[ERROR] 載入失敗: {e}")
            break
    
    print(f"[INFO] 共取得 {len(products)} 個商品")
    return products


def upload_to_shopify(source_product, collection_id=None):
    """上傳商品到 Shopify（含 Variants）"""
    
    original_title = source_product.get('title', '')
    body_html = source_product.get('body_html', '')
    handle = source_product.get('handle', '')
    
    print(f"[翻譯] 正在翻譯: {original_title[:30]}...")
    translated = translate_with_chatgpt(original_title, body_html)
    
    if translated['success']:
        print(f"[翻譯成功] {translated['title'][:30]}...")
    else:
        print(f"[翻譯失敗] 使用原文")
    
    # 處理選項（Options）
    options = []
    for opt in source_product.get('options', []):
        options.append({
            'name': opt.get('name', 'Option'),
            'values': opt.get('values', [])
        })
    
    # 處理 Variants
    variants = []
    source_variants = source_product.get('variants', [])
    
    for sv in source_variants:
        cost = float(sv.get('price', 0))
        weight = float(sv.get('grams', 0)) / 1000 if sv.get('grams') else DEFAULT_WEIGHT
        selling_price = calculate_selling_price(cost, weight)
        
        variant_data = {
            'title': sv.get('title', 'Default'),
            'price': f"{selling_price:.2f}",
            'sku': sv.get('sku', ''),
            'weight': weight,
            'weight_unit': 'kg',
            'inventory_management': None,
            'inventory_policy': 'continue',
            'requires_shipping': True,
        }
        
        # 選項值
        if sv.get('option1'):
            variant_data['option1'] = sv.get('option1')
        if sv.get('option2'):
            variant_data['option2'] = sv.get('option2')
        if sv.get('option3'):
            variant_data['option3'] = sv.get('option3')
        
        variants.append({
            'variant_data': variant_data,
            'cost': cost,
            'source_id': sv.get('id'),
            'image_id': sv.get('image_id'),  # 原圖片 ID（稍後對應）
        })
    
    # 處理圖片
    source_images = source_product.get('images', [])
    images_base64 = []
    image_id_to_position = {}  # 原圖片 ID -> 新位置
    
    print(f"[圖片] 開始下載 {len(source_images)} 張圖片...")
    
    for idx, img in enumerate(source_images):
        img_url = img.get('src', '')
        if not img_url:
            continue
        
        # 確保 https
        if img_url.startswith('//'):
            img_url = 'https:' + img_url
        
        print(f"[圖片] 下載中 ({idx+1}/{len(source_images)})")
        result = download_image_to_base64(img_url)
        
        if result['success']:
            image_data = {
                'attachment': result['base64'],
                'position': idx + 1,
                'filename': f"humanmade_{handle}_{idx+1}.jpg"
            }
            
            # 記錄原圖片 ID 對應的 variant_ids
            source_variant_ids = img.get('variant_ids', [])
            if source_variant_ids:
                image_data['_source_variant_ids'] = source_variant_ids
            
            images_base64.append(image_data)
            image_id_to_position[img.get('id')] = idx + 1
            print(f"[圖片] ✓ 下載成功")
        else:
            print(f"[圖片] ✗ 下載失敗")
        
        time.sleep(0.3)
    
    print(f"[圖片] 成功下載 {len(images_base64)}/{len(source_images)} 張圖片")
    
    # 準備上傳資料（先不含 variant 圖片對應）
    images_for_upload = []
    for img in images_base64:
        upload_img = {
            'attachment': img['attachment'],
            'position': img['position'],
            'filename': img['filename']
        }
        images_for_upload.append(upload_img)
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': 'Human Made',
            'product_type': source_product.get('product_type', ''),
            'status': 'active',
            'published': True,
            'handle': f"humanmade-{handle}",
            'options': options if options else [{'name': 'Title', 'values': ['Default Title']}],
            'variants': [v['variant_data'] for v in variants],
            'images': images_for_upload,
            'tags': f"Human Made, 日本, 潮流, 服飾, {source_product.get('product_type', '')}",
            'metafields_global_title_tag': translated['page_title'],
            'metafields_global_description_tag': translated['meta_description'],
            'metafields': [
                {
                    'namespace': 'custom',
                    'key': 'link',
                    'value': f"{SOURCE_URL}/products/{handle}",
                    'type': 'url'
                }
            ]
        }
    }
    
    response = requests.post(
        shopify_api_url('products.json'),
        headers=get_shopify_headers(),
        json=shopify_product
    )
    
    print(f"[DEBUG] Shopify 回應: {response.status_code}")
    
    if response.status_code == 201:
        created_product = response.json()['product']
        product_id = created_product['id']
        created_variants = created_product.get('variants', [])
        created_images = created_product.get('images', [])
        
        print(f"[DEBUG] 商品建立成功: ID={product_id}, Variants={len(created_variants)}, Images={len(created_images)}")
        
        # 更新每個 variant 的 cost
        for idx, cv in enumerate(created_variants):
            if idx < len(variants):
                cost = variants[idx]['cost']
                requests.put(
                    shopify_api_url(f"variants/{cv['id']}.json"),
                    headers=get_shopify_headers(),
                    json={'variant': {'id': cv['id'], 'cost': f"{cost:.2f}"}}
                )
        
        # 圖片與 Variant 對應
        # 建立 source variant id -> created variant id 的映射
        source_to_created_variant = {}
        for idx, sv in enumerate(source_variants):
            if idx < len(created_variants):
                source_to_created_variant[sv.get('id')] = created_variants[idx]['id']
        
        # 更新圖片的 variant_ids
        for idx, created_img in enumerate(created_images):
            if idx < len(images_base64):
                source_variant_ids = images_base64[idx].get('_source_variant_ids', [])
                if source_variant_ids:
                    new_variant_ids = []
                    for svid in source_variant_ids:
                        if svid in source_to_created_variant:
                            new_variant_ids.append(source_to_created_variant[svid])
                    
                    if new_variant_ids:
                        requests.put(
                            shopify_api_url(f"products/{product_id}/images/{created_img['id']}.json"),
                            headers=get_shopify_headers(),
                            json={'image': {'id': created_img['id'], 'variant_ids': new_variant_ids}}
                        )
        
        if collection_id:
            add_product_to_collection(product_id, collection_id)
        
        publish_to_all_channels(product_id)
        
        return {
            'success': True,
            'product': created_product,
            'translated': translated,
            'variants_count': len(created_variants)
        }
    else:
        print(f"[ERROR] Shopify 錯誤: {response.text}")
        return {'success': False, 'error': response.text}


# ========== Flask 路由 ==========

@app.route('/')
def index():
    token_loaded = load_shopify_token()
    token_status = '<span style="color: green;">✓ 已載入</span>' if token_loaded else '<span style="color: red;">✗ 未設定</span>'
    
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Human Made 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #E74C3C; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #E74C3C; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #C0392B; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #E74C3C, #F39C12); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 100px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #E74C3C; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>❤️ Human Made 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 humanmade.jp 所有商品並上架到 Shopify（含 Variants）</p>
        <p style="color: #666; font-size: 14px;">※ 成本價低於 ¥1000 的商品將自動跳過</p>
        <button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
        
        <div id="progressSection" style="display: none;">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill" style="width: 0%"></div>
            </div>
            <div class="status" id="statusText">準備中...</div>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-number" id="uploadedCount">0</div>
                    <div class="stat-label">已上架</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="skippedCount">0</div>
                    <div class="stat-label">已跳過</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="filteredCount">0</div>
                    <div class="stat-label">價格過濾</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="deletedCount" style="color: #e67e22;">0</div>
                    <div class="stat-label">設為草稿</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="errorCount">0</div>
                    <div class="stat-label">錯誤</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="card">
        <h3>執行日誌</h3>
        <div class="log" id="logArea">等待開始...</div>
    </div>

    <script>
        let pollInterval = null;
        function log(msg, type = '') {{
            const logArea = document.getElementById('logArea');
            const time = new Date().toLocaleTimeString();
            const color = type === 'success' ? '#4ec9b0' : type === 'error' ? '#f14c4c' : '#d4d4d4';
            logArea.innerHTML += '<div style="color:' + color + '">[' + time + '] ' + msg + '</div>';
            logArea.scrollTop = logArea.scrollHeight;
        }}
        function clearLog() {{ document.getElementById('logArea').innerHTML = ''; }}
        async function testShopify() {{
            log('測試 Shopify 連線...');
            try {{
                const res = await fetch('/api/test-shopify');
                const data = await res.json();
                if (data.success) log('✓ 連線成功！', 'success');
                else log('✗ 連線失敗: ' + data.error, 'error');
            }} catch (e) {{ log('✗ 請求失敗: ' + e.message, 'error'); }}
        }}
        async function startScrape() {{
            clearLog(); log('開始爬取流程...');
            document.getElementById('startBtn').disabled = true;
            document.getElementById('progressSection').style.display = 'block';
            try {{
                const res = await fetch('/api/start', {{ method: 'POST' }});
                const data = await res.json();
                if (!data.success) {{ log('✗ ' + data.error, 'error'); document.getElementById('startBtn').disabled = false; return; }}
                log('✓ 爬取任務已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            }} catch (e) {{ log('✗ ' + e.message, 'error'); document.getElementById('startBtn').disabled = false; }}
        }}
        async function pollStatus() {{
            try {{
                const res = await fetch('/api/status');
                const data = await res.json();
                const percent = data.total > 0 ? (data.progress / data.total * 100) : 0;
                document.getElementById('progressFill').style.width = percent + '%';
                document.getElementById('statusText').textContent = data.current_product + ' (' + data.progress + '/' + data.total + ')';
                document.getElementById('uploadedCount').textContent = data.uploaded;
                document.getElementById('skippedCount').textContent = data.skipped;
                document.getElementById('filteredCount').textContent = data.filtered_by_price || 0;
                document.getElementById('deletedCount').textContent = data.deleted || 0;
                document.getElementById('errorCount').textContent = data.errors.length;
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    document.getElementById('startBtn').disabled = false;
                    log('========== 爬取完成 ==========', 'success');
                }}
            }} catch (e) {{ console.error(e); }}
        }}
    </script>
</body>
</html>'''


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/start', methods=['GET', 'POST'])
def api_start():
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': 'Human Made 爬蟲已啟動'})


def run_scrape():
    global scrape_status
    
    try:
        scrape_status = {
            "running": True,
            "progress": 0,
            "total": 0,
            "current_product": "",
            "products": [],
            "errors": [],
            "uploaded": 0,
            "skipped": 0,
            "skipped_exists": 0,
            "filtered_by_price": 0,
            "deleted": 0
        }
        
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("Human Made")
        print(f"[INFO] Collection ID: {collection_id}")
        
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        existing_handles = set(collection_products_map.keys())
        print(f"[INFO] Collection 內有 {len(existing_handles)} 個商品")
        
        scrape_status['current_product'] = "正在從 Human Made 取得商品列表..."
        product_list = fetch_all_products()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        website_handles = set(f"humanmade-{p.get('handle', '')}" for p in product_list)
        
        for idx, product in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            handle = product.get('handle', '')
            title = product.get('title', '')
            scrape_status['current_product'] = f"處理中: {title[:30]}"
            
            # 檢查是否已存在
            if f"humanmade-{handle}" in existing_handles:
                print(f"[跳過] 已存在: {handle}")
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue
            
            # 檢查最低價格（取所有 variants 的最低價）
            variants = product.get('variants', [])
            if variants:
                min_price = min(float(v.get('price', 0)) for v in variants)
            else:
                min_price = 0
            
            if min_price < MIN_PRICE:
                print(f"[跳過] 價格低於{MIN_PRICE}円: {title} (¥{min_price})")
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1
                continue
            
            # 檢查庫存（至少有一個 variant 有庫存才上架）
            has_stock = False
            for v in variants:
                if v.get('available', False):
                    has_stock = True
                    break
            
            if not has_stock:
                print(f"[跳過] 無庫存: {title}")
                scrape_status['skipped'] += 1
                continue
            
            result = upload_to_shopify(product, collection_id)
            
            if result['success']:
                translated_title = result.get('translated', {}).get('title', title)
                variants_count = result.get('variants_count', 0)
                print(f"[成功] {translated_title} ({variants_count} variants)")
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'handle': handle,
                    'title': translated_title,
                    'original_title': title,
                    'variants_count': variants_count,
                    'status': 'success'
                })
            else:
                print(f"[失敗] {title}: {result['error']}")
                scrape_status['errors'].append({
                    'handle': handle,
                    'title': title,
                    'error': result['error']
                })
            
            time.sleep(1)
        
        # 設為草稿
        scrape_status['current_product'] = "正在檢查已下架商品..."
        handles_to_draft = existing_handles - website_handles
        
        if handles_to_draft:
            print(f"[INFO] 發現 {len(handles_to_draft)} 個商品需要設為草稿")
            for handle in handles_to_draft:
                scrape_status['current_product'] = f"設為草稿: {handle}"
                product_id = collection_products_map.get(handle)
                if product_id and set_product_to_draft(product_id):
                    scrape_status['deleted'] += 1
                time.sleep(0.5)
        else:
            print(f"[INFO] 沒有需要設為草稿的商品")
        
        scrape_status['current_product'] = "完成！"
        
    except Exception as e:
        print(f"[ERROR] 爬取過程發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    
    response = requests.get(
        shopify_api_url('shop.json'),
        headers=get_shopify_headers()
    )
    
    if response.status_code == 200:
        return jsonify({'success': True, 'shop': response.json()['shop']})
    else:
        return jsonify({'success': False, 'error': response.text}), 400


@app.route('/api/test-scrape')
def test_scrape():
    """測試取得商品資料"""
    products = fetch_all_products()
    
    # 回傳前 3 個商品的摘要
    summaries = []
    for p in products[:3]:
        summaries.append({
            'handle': p.get('handle'),
            'title': p.get('title'),
            'variants_count': len(p.get('variants', [])),
            'images_count': len(p.get('images', [])),
            'options': [o.get('name') for o in p.get('options', [])],
            'min_price': min(float(v.get('price', 0)) for v in p.get('variants', [])) if p.get('variants') else 0
        })
    
    return jsonify({
        'total_count': len(products),
        'samples': summaries
    })


if __name__ == '__main__':
    print("=" * 50)
    print("Human Made 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)
