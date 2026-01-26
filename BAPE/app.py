"""
BAPE 商品爬蟲 + Shopify Bulk Operations 上架工具
來源：jp.bape.com
功能：
1. 按分類爬取 jp.bape.com 商品（メンズ、レディース、キッズ）
2. 翻譯並產生 JSONL 檔案
3. 使用 Shopify Bulk Operations API 批量上傳
4. 自動同步：相同商品覆蓋更新，下架商品設為草稿
"""

from flask import Flask, jsonify, request
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
import threading

app = Flask(__name__)

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""

SOURCE_URL = "https://jp.bape.com"

# 分類設定
CATEGORIES = {
    'mens': {
        'name': 'メンズ',
        'collection': "BAPE Men's",
        'base_url': '/collections/all',
        'filter': 'filter.p.m.bape_data.type=%E3%83%A1%E3%83%B3%E3%82%BA&filter.v.availability=1',
        'tags': ['BAPE', 'A BATHING APE', '日本', '潮流', '男裝'],
        'product_type': "BAPE 男裝"
    },
    'womens': {
        'name': 'レディース',
        'collection': "BAPE Women's",
        'base_url': '/collections/all',
        'filter': 'filter.p.m.bape_data.type=%E3%83%AC%E3%83%87%E3%82%A3%E3%83%BC%E3%82%B9&filter.v.availability=1',
        'tags': ['BAPE', 'A BATHING APE', '日本', '潮流', '女裝'],
        'product_type': "BAPE 女裝"
    },
    'kids': {
        'name': 'キッズ',
        'collection': "BAPE Kids",
        'base_url': '/collections/all',
        'filter': 'filter.p.m.bape_data.type=%E3%82%AD%E3%83%83%E3%82%BA&filter.v.availability=1',
        'tags': ['BAPE', 'A BATHING APE', '日本', '潮流', '童裝', '兒童'],
        'product_type': "BAPE 童裝"
    }
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
DEFAULT_WEIGHT = 0.5
JSONL_DIR = "/tmp/bape_jsonl"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/html',
    'Accept-Language': 'ja,en;q=0.9',
}

os.makedirs(JSONL_DIR, exist_ok=True)

scrape_status = {
    "running": False,
    "phase": "",
    "progress": 0,
    "total": 0,
    "current_product": "",
    "products": [],
    "errors": [],
    "jsonl_file": "",
    "bulk_operation_id": "",
    "bulk_status": "",
    "set_to_draft": 0,
}

# ========== 工具函數 ==========

def load_shopify_token():
    global SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN
    if not SHOPIFY_SHOP:
        SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP", "")
    if not SHOPIFY_ACCESS_TOKEN:
        SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")


def graphql_request(query, variables=None):
    load_shopify_token()
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
    
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    return response.json()


_collection_id_cache = {}


def get_or_create_collection(collection_name):
    global _collection_id_cache
    
    if collection_name in _collection_id_cache:
        return _collection_id_cache[collection_name]
    
    query = """
    query findCollection($title: String!) {
      collections(first: 1, query: $title) {
        edges { node { id title } }
      }
    }
    """
    result = graphql_request(query, {"title": f"title:{collection_name}"})
    edges = result.get('data', {}).get('collections', {}).get('edges', [])
    
    for edge in edges:
        if edge['node']['title'] == collection_name:
            collection_id = edge['node']['id']
            _collection_id_cache[collection_name] = collection_id
            print(f"[Collection] 找到: {collection_name}")
            return collection_id
    
    mutation = """
    mutation createCollection($input: CollectionInput!) {
      collectionCreate(input: $input) {
        collection { id title }
        userErrors { field message }
      }
    }
    """
    result = graphql_request(mutation, {
        "input": {
            "title": collection_name,
            "descriptionHtml": f"<p>{collection_name} - 日本 A BATHING APE 官方正品代購</p>"
        }
    })
    
    collection = result.get('data', {}).get('collectionCreate', {}).get('collection')
    if collection:
        collection_id = collection['id']
        _collection_id_cache[collection_name] = collection_id
        print(f"[Collection] 建立: {collection_name}")
        publish_collection_to_all_channels(collection_id)
        return collection_id
    
    return None


def publish_collection_to_all_channels(collection_id):
    publication_ids = get_all_publication_ids()
    if not publication_ids:
        return
    
    publication_inputs = [{"publicationId": pub_id} for pub_id in publication_ids]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable { availablePublicationsCount { count } }
        userErrors { field message }
      }
    }
    """
    graphql_request(mutation, {"id": collection_id, "input": publication_inputs})


def get_all_publication_ids():
    query = """{ publications(first: 20) { edges { node { id name } } } }"""
    result = graphql_request(query)
    return [edge['node']['id'] for edge in result.get('data', {}).get('publications', {}).get('edges', [])]


def calculate_selling_price(cost, weight):
    """(成本 + 重量*1250) / 0.7，無條件捨去"""
    shipping_cost = weight * 1250
    base_price = cost + shipping_cost
    return int(base_price / 0.7)


def contains_japanese(text):
    if not text:
        return False
    return bool(re.search(r'[\u3040-\u309F\u30A0-\u30FF]', text))


def remove_japanese(text):
    if not text:
        return text
    cleaned = re.sub(r'[\u3040-\u309F\u30A0-\u30FF]+', '', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


# ========== 翻譯 ==========

def translate_with_chatgpt(title, description, size_spec=''):
    size_spec_section = f"\n尺寸規格表：\n{size_spec}" if size_spec else ""
    
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本潮流品牌商品資訊翻譯成繁體中文。

商品名稱（日文/英文）：{title}
商品說明：{description[:1500] if description else ''}{size_spec_section}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，前面加上 BAPE）",
    "description": "翻譯後的商品說明（HTML格式，用<br>換行）",
    "size_spec_translated": "翻譯後的尺寸規格（格式：列1|列2|列3，每行換行分隔）"
}}

規則：
1. 絕對禁止日文（平假名、片假名）
2. 商品名稱開頭必須是「BAPE」
3. 尺寸欄位翻譯：サイズ→尺寸、着丈→衣長、身幅→身寬、肩幅→肩寬、袖丈→袖長
4. 完全忽略注意事項（ご注意、注意事項、ご了承、※記號開頭的警告文字等）
5. 完全忽略價格相關內容（円、日圓、OFF、割引、値下げ等）
6. 只回傳 JSON"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "你是翻譯專家。輸出禁止任何日文。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0,
                "max_tokens": 1500
            },
            timeout=60
        )
        
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content'].strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content.rsplit('```', 1)[0]
            
            translated = json.loads(content.strip())
            
            trans_title = translated.get('title', title)
            trans_desc = translated.get('description', description)
            trans_size = translated.get('size_spec_translated', '')
            
            if contains_japanese(trans_title):
                trans_title = remove_japanese(trans_title)
            if contains_japanese(trans_desc):
                trans_desc = remove_japanese(trans_desc)
            
            if not trans_title.startswith('BAPE'):
                trans_title = f"BAPE {trans_title}"
            
            size_html = build_size_table_html(trans_size) if trans_size else ''
            if size_html:
                trans_desc += '<br><br>' + size_html
            
            return {'success': True, 'title': trans_title, 'description': trans_desc}
        
        return {'success': False, 'title': f"BAPE {title}", 'description': description}
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {'success': False, 'title': f"BAPE {title}", 'description': description}


def build_size_table_html(size_spec_text):
    if not size_spec_text:
        return ''
    
    lines = [line.strip() for line in size_spec_text.strip().split('\n') if line.strip()]
    if not lines:
        return ''
    
    html = '<div class="size-spec"><h3>📏 尺寸規格</h3>'
    html += '<table style="border-collapse:collapse;width:100%;margin:10px 0;">'
    
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.split('|')]
        if i == 0:
            html += '<tr style="background:#f5f5f5;">'
            for cell in cells:
                html += f'<th style="border:1px solid #ddd;padding:8px;text-align:center;">{cell}</th>'
            html += '</tr>'
        else:
            html += '<tr>'
            for j, cell in enumerate(cells):
                style = 'border:1px solid #ddd;padding:8px;'
                style += 'font-weight:bold;background:#fafafa;' if j == 0 else 'text-align:center;'
                html += f'<td style="{style}">{cell}</td>'
            html += '</tr>'
    
    html += '</table><p style="font-size:12px;color:#666;">※ 尺寸可能有些許誤差</p></div>'
    return html


def clean_description(description):
    description = re.sub(r'<a[^>]*>.*?</a>', '', description)
    description = re.sub(r'[^<>]*\d+[,，]?\d*\s*日圓[^<>]*', '', description)
    description = re.sub(r'[^<>]*\d+[,，]?\d*\s*円[^<>]*', '', description)
    description = re.sub(r'[^<>]*\d+%\s*OFF[^<>]*', '', description, flags=re.IGNORECASE)
    description = re.sub(r'[^<>]*降價[^<>]*', '', description)
    description = re.sub(r'[^<>]*大幅[^<>]*', '', description)
    description = re.sub(r'[^<>]*注意事項[^<>]*', '', description)
    description = re.sub(r'[^<>]*請注意[^<>]*', '', description)
    description = re.sub(r'[^<>]*敬請諒解[^<>]*', '', description)
    description = re.sub(r'[^<>]*敬請見諒[^<>]*', '', description)
    description = re.sub(r'[^<>]*※[^<>]*', '', description)
    description = re.sub(r'<p>\s*</p>', '', description)
    description = re.sub(r'<br\s*/?>\s*<br\s*/?>', '<br>', description)
    description = re.sub(r'^\s*(<br\s*/?>)+', '', description)
    description = re.sub(r'(<br\s*/?>)+\s*$', '', description)
    description = description.strip()
    
    notice = """
<br><br>
<p><strong>【請注意以下事項】</strong></p>
<p>※不接受退換貨</p>
<p>※開箱請全程錄影</p>
<p>※因庫存有限，訂購時間不同可能會出現缺貨情況。</p>
"""
    return description + notice


# ========== 爬取函數 ==========

def fetch_products_json(page=1):
    """從 BAPE 網站取得所有商品（JSON API）"""
    url = f"{SOURCE_URL}/collections/all/products.json?page={page}&limit=50"
    
    print(f"[爬取] 第 {page} 頁: {url}")
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        print(f"[爬取] HTTP 狀態: {response.status_code}")
        
        if response.status_code != 200:
            return []
        
        data = response.json()
        products = data.get('products', [])
        print(f"[爬取] 第 {page} 頁取得 {len(products)} 個商品")
        return products
        
    except Exception as e:
        print(f"[錯誤] {e}")
        import traceback
        traceback.print_exc()
        return []


def get_product_category(product):
    """根據商品資訊判斷分類"""
    tags = product.get('tags', [])
    title = product.get('title', '').upper()
    
    # 優先檢查 tags
    tags_str = ','.join(tags).lower() if isinstance(tags, list) else str(tags).lower()
    
    # 童裝判斷（優先，因為童裝商品名稱可能不含 KIDS）
    if 'キッズ' in tags_str or 'kids' in tags_str or 'KIDS' in title or 'キッズ' in title:
        return 'kids'
    
    # 女裝判斷
    if 'レディース' in tags_str or 'ladies' in tags_str or 'women' in tags_str or 'LADIES' in title or 'レディース' in title:
        return 'womens'
    
    # 男裝判斷（預設）
    if 'メンズ' in tags_str or 'mens' in tags_str or 'men' in tags_str or 'メンズ' in title:
        return 'mens'
    
    # 無法判斷時預設為男裝
    return 'mens'


def fetch_all_products_by_category():
    """取得所有商品並按分類整理"""
    all_products = {'mens': [], 'womens': [], 'kids': []}
    page = 1
    seen_handles = set()
    
    while True:
        products = fetch_products_json(page)
        
        if not products:
            break
        
        new_count = 0
        for p in products:
            handle = p.get('handle', '')
            if handle in seen_handles:
                continue
            seen_handles.add(handle)
            
            # 檢查庫存
            has_stock = any(v.get('available', False) for v in p.get('variants', []))
            if not has_stock:
                continue
            
            # 判斷分類
            category = get_product_category(p)
            all_products[category].append(p)
            new_count += 1
        
        print(f"[爬取] 第 {page} 頁新增 {new_count} 個有庫存商品")
        
        if len(products) < 50:
            break
        
        page += 1
        time.sleep(0.5)
    
    print(f"[爬取] 分類結果: 男裝 {len(all_products['mens'])}, 女裝 {len(all_products['womens'])}, 童裝 {len(all_products['kids'])}")
    return all_products


def fetch_category_products_html(category_key, page=1):
    """從 BAPE 網站 HTML 頁面取得商品列表（備用方法）"""
    cat_info = CATEGORIES[category_key]
    
    if page == 1:
        url = f"{SOURCE_URL}{cat_info['base_url']}?{cat_info['filter']}"
    else:
        url = f"{SOURCE_URL}{cat_info['base_url']}?{cat_info['filter']}&page={page}"
    
    print(f"[爬取 HTML] {cat_info['name']} 第 {page} 頁: {url}")
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        print(f"[爬取 HTML] HTTP 狀態: {response.status_code}")
        
        if response.status_code != 200:
            return [], False
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        product_handles = []
        all_links = soup.find_all('a', href=True)
        
        for link in all_links:
            href = link.get('href', '')
            if '/products/' in href:
                match = re.search(r'/products/([^/?#]+)', href)
                if match:
                    handle = match.group(1)
                    if handle not in product_handles and handle != 'products':
                        product_handles.append(handle)
        
        print(f"[爬取 HTML] 找到 {len(product_handles)} 個商品")
        
        has_next_page = False
        for link in all_links:
            if f'page={page + 1}' in link.get('href', ''):
                has_next_page = True
                break
        
        return product_handles, has_next_page
        
    except Exception as e:
        print(f"[錯誤] {e}")
        return [], False


def fetch_product_json(handle):
    """取得單一商品的 JSON 資料"""
    url = f"{SOURCE_URL}/products/{handle}.json"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code == 200:
            data = response.json()
            return data.get('product')
    except Exception as e:
        print(f"[錯誤] 取得商品 {handle} 失敗: {e}")
    
    return None


def fetch_all_category_products(category_key):
    """取得分類內所有商品（使用 JSON API + 程式過濾）"""
    all_by_category = fetch_all_products_by_category()
    products = all_by_category.get(category_key, [])
    print(f"[爬取] {CATEGORIES[category_key]['name']} 共 {len(products)} 個有庫存商品")
    return products


def fetch_size_table(handle):
    try:
        url = f"{SOURCE_URL}/products/{handle}"
        response = requests.get(url, headers={'User-Agent': HEADERS['User-Agent'], 'Accept': 'text/html'}, timeout=30)
        
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        def_list = soup.find('dl', class_='s-product-detail__def-list-description')
        if not def_list:
            return None
        
        size_dt = def_list.find('dt', string=re.compile(r'サイズ'))
        if not size_dt:
            return None
        
        size_dd = size_dt.find_next_sibling('dd')
        if not size_dd:
            return None
        
        table = size_dd.find('table')
        if not table:
            return None
        
        rows = table.find_all('tr')
        return '\n'.join([' | '.join([cell.get_text(strip=True) for cell in row.find_all(['th', 'td'])]) for row in rows])
    except:
        return None


# ========== JSONL 生成 ==========

def product_to_jsonl_entry(product, category_key, collection_id, existing_product_id=None):
    """將商品資料轉換為 JSONL 格式，支援更新現有商品"""
    cat_info = CATEGORIES[category_key]
    
    title = product.get('title', '')
    body_html = product.get('body_html', '')
    handle = product.get('handle', '')
    source_url = f"{SOURCE_URL}/products/{handle}"
    
    size_spec = fetch_size_table(handle)
    translated = translate_with_chatgpt(title, body_html, size_spec or '')
    trans_title = translated['title']
    trans_desc = clean_description(translated['description'])
    
    options = product.get('options', [])
    source_variants = product.get('variants', [])
    images = product.get('images', [])
    
    has_options = len(options) > 0 and not (len(options) == 1 and options[0].get('name') == 'Title')
    
    product_options = []
    if has_options:
        for opt in options:
            opt_name = opt.get('name', '')
            opt_values = opt.get('values', [])
            if opt_values and opt_name != 'Title':
                product_options.append({"name": opt_name, "values": [{"name": v} for v in opt_values]})
    
    image_list = [img['src'] for img in images[:10]] if images else []
    first_image = image_list[0] if image_list else None
    
    files = [{"originalSource": img_url, "contentType": "IMAGE"} for img_url in image_list]
    variant_file = {"originalSource": first_image, "contentType": "IMAGE"} if first_image else None
    
    variants = []
    for sv in source_variants:
        if not sv.get('available', False):
            continue
        
        cost = float(sv.get('price', 0))
        if cost < MIN_PRICE:
            continue
        
        weight = float(sv.get('grams', 0)) / 1000 if sv.get('grams') else DEFAULT_WEIGHT
        selling_price = calculate_selling_price(cost, weight)
        
        sku_parts = [f"bape-{handle}"]
        option_values = []
        
        if sv.get('option1') and len(options) > 0 and options[0].get('name') != 'Title':
            sku_parts.append(sv['option1'])
            option_values.append({"optionName": options[0]['name'], "name": sv['option1']})
        if sv.get('option2') and len(options) > 1:
            sku_parts.append(sv['option2'])
            option_values.append({"optionName": options[1]['name'], "name": sv['option2']})
        if sv.get('option3') and len(options) > 2:
            sku_parts.append(sv['option3'])
            option_values.append({"optionName": options[2]['name'], "name": sv['option3']})
        
        variant = {
            "price": selling_price,
            "sku": '-'.join(sku_parts),
            "inventoryPolicy": "CONTINUE",
            "taxable": False,
            "inventoryItem": {"cost": cost}
        }
        
        if option_values:
            variant["optionValues"] = option_values
        if variant_file:
            variant["file"] = variant_file
        
        variants.append(variant)
    
    if not variants:
        return None
    
    seo_title = f"{trans_title} | BAPE 日本代購"
    seo_description = f"日本 A BATHING APE 官方正品代購。{trans_title}，台灣現貨或日本直送，品質保證。GOYOUTATI 御用達日本伴手禮專門店。"
    
    product_input = {
        "title": trans_title,
        "descriptionHtml": trans_desc,
        "vendor": "BAPE",
        "productType": cat_info['product_type'],
        "status": "ACTIVE",
        "handle": f"bape-{handle}",
        "tags": cat_info['tags'],
        "seo": {"title": seo_title, "description": seo_description},
        "metafields": [{"namespace": "custom", "key": "link", "value": source_url, "type": "url"}]
    }
    
    # 如果商品已存在，加入 id 來更新
    if existing_product_id:
        product_input["id"] = existing_product_id
    
    if collection_id:
        product_input["collections"] = [collection_id]
    if product_options:
        product_input["productOptions"] = product_options
    if variants:
        product_input["variants"] = variants
    if files:
        product_input["files"] = files
    
    return {"productSet": product_input, "synchronous": True}


# ========== Bulk Operations ==========

def create_staged_upload():
    query = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets { url resourceUrl parameters { name value } }
        userErrors { field message }
      }
    }
    """
    result = graphql_request(query, {"input": [{"resource": "BULK_MUTATION_VARIABLES", "filename": "products.jsonl", "mimeType": "text/jsonl", "httpMethod": "POST"}]})
    targets = result.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    return targets[0] if targets else None


def upload_jsonl_to_staged(staged_target, jsonl_path):
    url = staged_target['url']
    params = {p['name']: p['value'] for p in staged_target['parameters']}
    with open(jsonl_path, 'rb') as f:
        response = requests.post(url, data=params, files={'file': ('products.jsonl', f, 'text/jsonl')}, timeout=300)
    return response.status_code in [200, 201, 204]


def run_bulk_mutation(staged_upload_path):
    query = """
    mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) {
      bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) {
        bulkOperation { id status }
        userErrors { field message }
      }
    }
    """
    mutation = """
    mutation call($productSet: ProductSetInput!, $synchronous: Boolean!) {
      productSet(synchronous: $synchronous, input: $productSet) {
        product { id title }
        userErrors { field message }
      }
    }
    """
    return graphql_request(query, {"mutation": mutation, "stagedUploadPath": staged_upload_path})


def check_bulk_operation_status(operation_id=None):
    if operation_id:
        query = """query($id: ID!) { node(id: $id) { ... on BulkOperation { id status errorCode objectCount url } } }"""
        result = graphql_request(query, {"id": operation_id})
        return result.get('data', {}).get('node', {})
    else:
        query = """{ currentBulkOperation(type: MUTATION) { id status errorCode objectCount url } }"""
        result = graphql_request(query)
        return result.get('data', {}).get('currentBulkOperation', {})


# ========== 商品管理 ==========

def get_all_publications():
    query = """{ publications(first: 20) { edges { node { id name } } } }"""
    result = graphql_request(query)
    return [{'id': edge['node']['id'], 'name': edge['node']['name']} for edge in result.get('data', {}).get('publications', {}).get('edges', [])]


def get_product_id_by_handle(handle):
    """根據 handle 查詢商品 ID"""
    query = """
    query getProductByHandle($handle: String!) {
      productByHandle(handle: $handle) {
        id
        title
      }
    }
    """
    result = graphql_request(query, {"handle": handle})
    product = result.get('data', {}).get('productByHandle')
    if product:
        return product['id']
    return None


def fetch_bape_product_ids():
    all_products = []
    cursor = None
    
    while True:
        if cursor:
            query = """query($cursor: String) { products(first: 250, after: $cursor, query: "vendor:BAPE") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }"""
            result = graphql_request(query, {"cursor": cursor})
        else:
            query = """{ products(first: 250, query: "vendor:BAPE") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }"""
            result = graphql_request(query)
        
        products = result.get('data', {}).get('products', {})
        edges = products.get('edges', [])
        
        for edge in edges:
            node = edge['node']
            all_products.append({'id': node['id'], 'title': node['title'], 'handle': node['handle'], 'status': node['status']})
            cursor = edge['cursor']
        
        if not products.get('pageInfo', {}).get('hasNextPage', False):
            break
        time.sleep(0.5)
    
    return all_products


def set_product_to_draft(product_id):
    mutation = """mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id status } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id, "status": "DRAFT"}})
    return not result.get('data', {}).get('productUpdate', {}).get('userErrors', [])


def delete_product(product_id):
    """刪除單一商品"""
    mutation = """
    mutation productDelete($input: ProductDeleteInput!) {
      productDelete(input: $input) {
        deletedProductId
        userErrors { field message }
      }
    }
    """
    result = graphql_request(mutation, {"input": {"id": product_id}})
    user_errors = result.get('data', {}).get('productDelete', {}).get('userErrors', [])
    return not user_errors


def delete_all_bape_products():
    """刪除所有 BAPE 商品"""
    global scrape_status
    
    print("[DELETE] 開始刪除所有 BAPE 商品...")
    
    products = fetch_bape_product_ids()
    total = len(products)
    
    if total == 0:
        return {'success': True, 'deleted': 0, 'message': '沒有 BAPE 商品'}
    
    print(f"[DELETE] 找到 {total} 個 BAPE 商品")
    
    deleted = 0
    failed = 0
    
    for i, product in enumerate(products):
        scrape_status['current_product'] = f"刪除中 [{i+1}/{total}] {product.get('title', '')[:30]}"
        scrape_status['progress'] = i + 1
        
        if delete_product(product['id']):
            deleted += 1
            print(f"[DELETE] 已刪除: {product.get('title', '')[:30]}")
        else:
            failed += 1
            print(f"[DELETE] 刪除失敗: {product.get('title', '')[:30]}")
        
        time.sleep(0.2)
    
    return {'success': True, 'deleted': deleted, 'failed': failed, 'total': total}


def batch_publish_bape_products():
    products = fetch_bape_product_ids()
    if not products:
        return {'success': False, 'error': 'No products'}
    
    publications = get_all_publications()
    if not publications:
        return {'success': False, 'error': 'No publications'}
    
    publication_inputs = [{"publicationId": pub['id']} for pub in publications]
    results = {'total': len(products), 'success': 0, 'failed': 0, 'errors': []}
    
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
    
    for product in products:
        result = graphql_request(mutation, {"id": product['id'], "input": publication_inputs})
        if result.get('data', {}).get('publishablePublish', {}).get('userErrors', []):
            results['failed'] += 1
        else:
            results['success'] += 1
        time.sleep(0.1)
    
    return results


# ========== 主流程 ==========

def run_test_single(category='mens'):
    global scrape_status
    
    scrape_status = {"running": True, "phase": "testing", "progress": 0, "total": 1, "current_product": "測試單品...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "set_to_draft": 0}
    
    try:
        cat_info = CATEGORIES[category]
        print(f"[TEST] 分類: {category}, Collection: {cat_info['collection']}")
        
        scrape_status['current_product'] = f"取得 Collection..."
        collection_id = get_or_create_collection(cat_info['collection'])
        print(f"[TEST] Collection ID: {collection_id}")
        
        if not collection_id:
            scrape_status['errors'].append({'error': '無法建立 Collection'})
            scrape_status['current_product'] = '❌ 無法建立 Collection'
            return
        
        scrape_status['current_product'] = f"爬取商品..."
        
        # 取得所有商品並分類
        all_by_category = fetch_all_products_by_category()
        products = all_by_category.get(category, [])
        
        print(f"[TEST] {category} 有 {len(products)} 個商品")
        
        if not products:
            scrape_status['errors'].append({'error': f'沒有找到 {cat_info["name"]} 的商品'})
            scrape_status['current_product'] = f'❌ 沒有找到 {cat_info["name"]} 的商品'
            return
        
        # 取第一個符合條件的商品
        test_product = None
        for p in products:
            min_price = min((float(v.get('price', 0)) for v in p.get('variants', [])), default=0)
            if min_price >= MIN_PRICE:
                test_product = p
                break
        
        if not test_product:
            scrape_status['errors'].append({'error': f'沒有符合條件的商品（價格 >= {MIN_PRICE}）'})
            scrape_status['current_product'] = '❌ 沒有符合條件的商品'
            return
        
        print(f"[TEST] 測試商品: {test_product.get('title', '')[:50]}")
        
        # 檢查商品是否已存在
        my_handle = f"bape-{test_product.get('handle', '')}"
        existing_id = get_product_id_by_handle(my_handle)
        if existing_id:
            print(f"[TEST] 商品已存在，將進行更新: {existing_id}")
        else:
            print(f"[TEST] 商品不存在，將建立新商品")
        
        scrape_status['current_product'] = f"翻譯: {test_product['title'][:30]}..."
        entry = product_to_jsonl_entry(test_product, category, collection_id, existing_id)
        
        if not entry:
            scrape_status['errors'].append({'error': '商品轉換失敗（可能沒有有效的 variant）'})
            scrape_status['current_product'] = '❌ 商品轉換失敗'
            return
        
        product_input = entry['productSet']
        print(f"[TEST] 轉換成功: {product_input['title']}")
        print(f"[TEST] Variants: {len(product_input.get('variants', []))}")
        
        scrape_status['products'].append({'title': product_input['title'], 'handle': product_input['handle'], 'variants': len(product_input.get('variants', []))})
        
        scrape_status['current_product'] = "上傳到 Shopify..."
        mutation = """mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $input) { product { id title handle } userErrors { field code message } } }"""
        
        # 除錯：打印完整的 product_input
        print(f"[TEST] ===== ProductSet Input =====")
        print(f"[TEST] title: {product_input.get('title')}")
        print(f"[TEST] handle: {product_input.get('handle')}")
        print(f"[TEST] vendor: {product_input.get('vendor')}")
        print(f"[TEST] productType: {product_input.get('productType')}")
        print(f"[TEST] productOptions: {product_input.get('productOptions')}")
        print(f"[TEST] variants count: {len(product_input.get('variants', []))}")
        if product_input.get('variants'):
            print(f"[TEST] first variant: {product_input['variants'][0]}")
        print(f"[TEST] collections: {product_input.get('collections')}")
        print(f"[TEST] ================================")
        
        result = graphql_request(mutation, {"input": product_input, "synchronous": True})
        
        # 除錯：打印完整回應
        print(f"[TEST] ===== GraphQL Response =====")
        print(f"[TEST] {json.dumps(result, ensure_ascii=False, indent=2)[:2000]}")
        print(f"[TEST] ================================")
        
        product_set = result.get('data', {}).get('productSet', {})
        user_errors = product_set.get('userErrors', [])
        
        if user_errors:
            error_msg = '; '.join([e.get('message', str(e)) for e in user_errors])
            scrape_status['errors'].append({'error': error_msg})
            scrape_status['current_product'] = f"❌ 失敗: {error_msg}"
            print(f"[ERROR] productSet 失敗: {error_msg}")
        else:
            product = product_set.get('product', {})
            publications = get_all_publications()
            if publications:
                pub_mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
                graphql_request(pub_mutation, {"id": product['id'], "input": [{"publicationId": pub['id']} for pub in publications]})
            scrape_status['current_product'] = f"✅ 成功！{product.get('title', '')}"
        
        scrape_status['progress'] = 1
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        import traceback
        traceback.print_exc()
    finally:
        scrape_status['running'] = False


def run_scrape(category):
    global scrape_status
    
    scrape_status = {"running": True, "phase": "scraping", "progress": 0, "total": 0, "current_product": "", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "set_to_draft": 0}
    
    try:
        categories_to_scrape = ['mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not categories_to_scrape:
            scrape_status['errors'].append({'error': f'未知分類: {category}'})
            return
        
        # 先取得所有現有商品的 handle -> id 映射
        scrape_status['current_product'] = '取得現有商品...'
        existing_products = fetch_bape_product_ids()
        existing_handles = {p['handle']: p['id'] for p in existing_products}
        print(f"[SCRAPE] 現有 {len(existing_handles)} 個 BAPE 商品")
        
        all_jsonl_entries = []
        
        for cat_key in categories_to_scrape:
            cat_info = CATEGORIES[cat_key]
            collection_id = get_or_create_collection(cat_info['collection'])
            if not collection_id:
                continue
            
            products = fetch_all_category_products(cat_key)
            if not products:
                continue
            
            scrape_status['total'] += len(products)
            
            for product in products:
                scrape_status['progress'] += 1
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {product.get('title', '')[:30]}"
                
                try:
                    my_handle = f"bape-{product.get('handle', '')}"
                    existing_id = existing_handles.get(my_handle)
                    
                    entry = product_to_jsonl_entry(product, cat_key, collection_id, existing_id)
                    if entry:
                        all_jsonl_entries.append(entry)
                        scrape_status['products'].append({'title': entry['productSet']['title'], 'handle': entry['productSet']['handle'], 'variants': len(entry['productSet'].get('variants', []))})
                except Exception as e:
                    scrape_status['errors'].append({'error': str(e)})
                
                time.sleep(0.5)
        
        if all_jsonl_entries:
            jsonl_path = os.path.join(JSONL_DIR, f"bape_{category}_{int(time.time())}.jsonl")
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                for entry in all_jsonl_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            scrape_status['jsonl_file'] = jsonl_path
        
        scrape_status['current_product'] = f"完成！共 {len(all_jsonl_entries)} 個商品"
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False
        scrape_status['phase'] = "completed"


def run_bulk_upload(jsonl_path):
    global scrape_status
    
    scrape_status['phase'] = 'uploading'
    scrape_status['running'] = True
    
    try:
        staged = create_staged_upload()
        if not staged or not upload_jsonl_to_staged(staged, jsonl_path):
            scrape_status['errors'].append({'error': '上傳失敗'})
            return
        
        staged_path = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), staged.get('resourceUrl', ''))
        result = run_bulk_mutation(staged_path)
        
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        scrape_status['bulk_status'] = bulk_op.get('status', '')
        scrape_status['current_product'] = f"批量操作已啟動: {bulk_op.get('status', '')}"
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False


def run_test_sync(category='all', limit=10):
    """測試上架（每個分類只抓 limit 個商品）"""
    global scrape_status
    
    print(f"[TEST SYNC] ========== 測試上架 (每分類 {limit} 個) ==========")
    
    scrape_status = {"running": True, "phase": "test_sync", "progress": 0, "total": 0, "current_product": "開始測試上架...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "set_to_draft": 0}
    
    try:
        # 取得現有商品
        scrape_status['current_product'] = '取得現有商品...'
        existing_products = fetch_bape_product_ids()
        existing_handles = {p['handle']: p for p in existing_products}
        print(f"[TEST SYNC] 現有 {len(existing_handles)} 個商品")
        
        # 取得所有商品並分類
        scrape_status['current_product'] = '爬取商品...'
        all_by_category = fetch_all_products_by_category()
        
        categories_to_scrape = ['mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not categories_to_scrape:
            raise Exception(f'未知分類: {category}')
        
        all_jsonl_entries = []
        
        for cat_key in categories_to_scrape:
            cat_info = CATEGORIES[cat_key]
            scrape_status['current_product'] = f"處理 {cat_info['collection']} (限 {limit} 個)..."
            
            collection_id = get_or_create_collection(cat_info['collection'])
            if not collection_id:
                scrape_status['errors'].append({'error': f"無法取得 Collection: {cat_info['collection']}"})
                continue
            
            # 取得該分類的商品，限制數量
            products = all_by_category.get(cat_key, [])[:limit]
            print(f"[TEST SYNC] {cat_info['collection']} 取 {len(products)} 個商品")
            
            if not products:
                scrape_status['errors'].append({'error': f"{cat_info['collection']} 沒有找到商品"})
                continue
            
            scrape_status['total'] += len(products)
            
            for product in products:
                scrape_status['progress'] += 1
                handle = product.get('handle', '')
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {product.get('title', '')[:30]}"
                
                my_handle = f"bape-{handle}"
                existing_info = existing_handles.get(my_handle)
                existing_id = existing_info['id'] if existing_info else None
                
                try:
                    entry = product_to_jsonl_entry(product, cat_key, collection_id, existing_id)
                    if entry:
                        all_jsonl_entries.append(entry)
                        scrape_status['products'].append({
                            'title': entry['productSet']['title'],
                            'handle': entry['productSet']['handle'],
                            'variants': len(entry['productSet'].get('variants', []))
                        })
                        print(f"[TEST SYNC] 已加入: {entry['productSet']['title'][:30]}")
                except Exception as e:
                    print(f"[TEST SYNC] 轉換失敗: {e}")
                    scrape_status['errors'].append({'error': str(e)})
                
                time.sleep(0.3)
        
        print(f"[TEST SYNC] 總共 {len(all_jsonl_entries)} 個商品")
        
        if not all_jsonl_entries:
            raise Exception('沒有爬取到商品')
        
        # 寫入 JSONL
        jsonl_path = os.path.join(JSONL_DIR, f"bape_test_{category}_{int(time.time())}.jsonl")
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            for entry in all_jsonl_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        scrape_status['jsonl_file'] = jsonl_path
        
        # 批量上傳
        print(f"[TEST SYNC] 批量上傳 {len(all_jsonl_entries)} 個商品...")
        scrape_status['current_product'] = '批量上傳...'
        scrape_status['phase'] = 'uploading'
        
        staged = create_staged_upload()
        if not staged or not upload_jsonl_to_staged(staged, jsonl_path):
            raise Exception('上傳失敗')
        
        staged_path = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), '')
        result = run_bulk_mutation(staged_path)
        
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        if user_errors:
            raise Exception(f'Bulk Mutation 錯誤: {user_errors}')
        
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        
        # 等待完成
        scrape_status['current_product'] = '等待完成...'
        for _ in range(60):
            status = check_bulk_operation_status()
            scrape_status['bulk_status'] = status.get('status', '')
            if status.get('status') == 'COMPLETED':
                break
            elif status.get('status') in ['FAILED', 'CANCELED']:
                raise Exception(f'Bulk 失敗: {status.get("status")}')
            time.sleep(5)
        
        # 發布
        scrape_status['current_product'] = '發布...'
        scrape_status['phase'] = 'publishing'
        batch_publish_bape_products()
        
        scrape_status['current_product'] = f"✅ 測試完成！上傳 {len(all_jsonl_entries)} 個商品"
        scrape_status['phase'] = 'completed'
        
        return {'success': True, 'total_products': len(all_jsonl_entries)}
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        scrape_status['current_product'] = f"❌ 錯誤: {str(e)}"
        scrape_status['phase'] = 'error'
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}
    finally:
        scrape_status['running'] = False


def run_full_sync(category='all'):
    global scrape_status
    
    print(f"[CRON] ========== 開始同步 ==========")
    
    scrape_status = {"running": True, "phase": "cron_sync", "progress": 0, "total": 0, "current_product": "開始...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "set_to_draft": 0}
    
    try:
        # 取得 Shopify 現有商品（包含 id）
        scrape_status['current_product'] = '取得 Shopify 現有商品...'
        existing_products = fetch_bape_product_ids()
        existing_handles = {p['handle']: p for p in existing_products}
        print(f"[CRON] Shopify 現有 {len(existing_handles)} 個商品")
        
        # 取得所有商品並分類
        scrape_status['current_product'] = '爬取商品...'
        all_by_category = fetch_all_products_by_category()
        
        categories_to_scrape = ['mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not categories_to_scrape:
            raise Exception(f'未知分類: {category}')
        
        all_jsonl_entries = []
        scraped_handles = set()
        
        for cat_key in categories_to_scrape:
            cat_info = CATEGORIES[cat_key]
            scrape_status['current_product'] = f"處理 {cat_info['collection']}..."
            
            collection_id = get_or_create_collection(cat_info['collection'])
            if not collection_id:
                continue
            
            products = all_by_category.get(cat_key, [])
            print(f"[CRON] {cat_info['collection']} 共 {len(products)} 個商品")
            
            scrape_status['total'] += len(products)
            
            for product in products:
                scrape_status['progress'] += 1
                handle = product.get('handle', '')
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {product.get('title', '')[:30]}"
                
                my_handle = f"bape-{handle}"
                scraped_handles.add(my_handle)
                
                existing_info = existing_handles.get(my_handle)
                existing_id = existing_info['id'] if existing_info else None
                
                try:
                    entry = product_to_jsonl_entry(product, cat_key, collection_id, existing_id)
                    if entry:
                        all_jsonl_entries.append(entry)
                        scrape_status['products'].append({
                            'title': entry['productSet']['title'],
                            'handle': entry['productSet']['handle'],
                            'variants': len(entry['productSet'].get('variants', []))
                        })
                except Exception as e:
                    scrape_status['errors'].append({'error': str(e)})
                
                time.sleep(0.3)
        
        if not all_jsonl_entries:
            raise Exception('沒有爬取到商品')
        
        # 寫入 JSONL
        jsonl_path = os.path.join(JSONL_DIR, f"bape_{category}_{int(time.time())}.jsonl")
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            for entry in all_jsonl_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        scrape_status['jsonl_file'] = jsonl_path
        
        # 批量上傳
        print(f"[CRON] 批量上傳...")
        scrape_status['current_product'] = '批量上傳...'
        scrape_status['phase'] = 'uploading'
        
        staged = create_staged_upload()
        if not staged or not upload_jsonl_to_staged(staged, jsonl_path):
            raise Exception('上傳失敗')
        
        staged_path = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), '')
        result = run_bulk_mutation(staged_path)
        
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        if user_errors:
            raise Exception(f'Bulk Mutation 錯誤: {user_errors}')
        
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        
        # 等待完成
        print(f"[CRON] 等待完成...")
        scrape_status['current_product'] = '等待完成...'
        
        for _ in range(120):
            status = check_bulk_operation_status()
            scrape_status['bulk_status'] = status.get('status', '')
            if status.get('status') == 'COMPLETED':
                break
            elif status.get('status') in ['FAILED', 'CANCELED']:
                raise Exception(f'Bulk 失敗: {status.get("status")}')
            time.sleep(5)
        
        # 發布
        print(f"[CRON] 發布...")
        scrape_status['current_product'] = '發布...'
        scrape_status['phase'] = 'publishing'
        batch_publish_bape_products()
        
        # 處理下架
        print(f"[CRON] 處理下架...")
        scrape_status['current_product'] = '處理下架...'
        scrape_status['phase'] = 'drafting'
        
        draft_count = 0
        for handle, product_info in existing_handles.items():
            if handle not in scraped_handles and product_info.get('status') == 'ACTIVE':
                print(f"[CRON] 設為草稿: {handle}")
                if set_product_to_draft(product_info['id']):
                    draft_count += 1
                time.sleep(0.2)
        
        scrape_status['set_to_draft'] = draft_count
        scrape_status['current_product'] = f"✅ 完成！上傳 {len(all_jsonl_entries)} 個，下架 {draft_count} 個"
        scrape_status['phase'] = 'completed'
        
        return {'success': True, 'total_products': len(all_jsonl_entries), 'set_to_draft': draft_count}
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        scrape_status['current_product'] = f"❌ 錯誤: {str(e)}"
        scrape_status['phase'] = 'error'
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}
    finally:
        scrape_status['running'] = False


# ========== API Routes ==========

@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BAPE 爬蟲工具</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; }
        h1 { color: #333; margin-bottom: 20px; }
        .card { background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .card h2 { color: #444; margin-bottom: 15px; font-size: 18px; }
        .btn { display: inline-block; padding: 12px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; margin: 5px; }
        .btn-primary { background: #0066ff; color: white; }
        .btn-success { background: #00c853; color: white; }
        .btn-warning { background: #ff9800; color: white; }
        .btn-danger { background: #f44336; color: white; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .status { background: #f8f9fa; border-radius: 8px; padding: 15px; margin-top: 15px; }
        .progress-bar { height: 20px; background: #e0e0e0; border-radius: 10px; overflow: hidden; margin: 10px 0; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #0066ff, #00c853); transition: width 0.3s; }
        .log { background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 8px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 12px; }
        select { padding: 10px; border-radius: 6px; border: 1px solid #ddd; font-size: 14px; margin-right: 10px; }
        .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-top: 15px; }
        .stat-box { background: #f0f4f8; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 24px; font-weight: bold; }
        .stat-label { font-size: 12px; color: #666; }
        .danger-zone { border: 2px solid #f44336; background: #fff5f5; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🦍 BAPE 爬蟲工具</h1>
        
        <div class="card">
            <h2>⚡ 測試單品</h2>
            <p style="color:#666;margin-bottom:10px;">測試單一商品上傳</p>
            <select id="testCat"><option value="mens">男裝</option><option value="womens">女裝</option><option value="kids">童裝</option></select>
            <button class="btn btn-warning" onclick="startTest()">🧪 測試單品</button>
        </div>
        
        <div class="card">
            <h2>🧪 測試上架（每分類 10 個）</h2>
            <p style="color:#666;margin-bottom:10px;">快速測試，每個分類只抓 10 個商品</p>
            <select id="testSyncCat"><option value="all">全部</option><option value="mens">男裝</option><option value="womens">女裝</option><option value="kids">童裝</option></select>
            <button class="btn btn-warning" onclick="startTestSync()">🧪 測試上架</button>
        </div>
        
        <div class="card">
            <h2>🔄 完整同步</h2>
            <p style="color:#666;margin-bottom:10px;">爬取所有商品並同步到 Shopify</p>
            <select id="syncCat"><option value="all">全部</option><option value="mens">男裝</option><option value="womens">女裝</option><option value="kids">童裝</option></select>
            <button class="btn btn-success" onclick="startSync()">🔄 開始同步</button>
        </div>
        
        <div class="card">
            <h2>📊 執行狀態</h2>
            <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
            <div class="status">
                <div>階段：<span id="phase">-</span></div>
                <div>進度：<span id="progress">0/0</span></div>
                <div>目前：<span id="current">-</span></div>
            </div>
            <div class="stats">
                <div class="stat-box"><div class="stat-value" id="productCount">0</div><div class="stat-label">已處理</div></div>
                <div class="stat-box"><div class="stat-value" id="draftCount">0</div><div class="stat-label">已下架</div></div>
                <div class="stat-box"><div class="stat-value" id="errorCount">0</div><div class="stat-label">錯誤</div></div>
            </div>
        </div>
        
        <div class="card">
            <h2>📝 日誌</h2>
            <div class="log" id="log"></div>
        </div>
        
        <div class="card">
            <h2>🔧 工具</h2>
            <button class="btn btn-primary" onclick="testShopify()">測試 Shopify</button>
            <button class="btn btn-primary" onclick="testBape()">測試 BAPE</button>
            <button class="btn btn-primary" onclick="countProducts()">商品數量</button>
            <button class="btn btn-success" onclick="publishAll()">發布所有</button>
        </div>
        
        <div class="card danger-zone">
            <h2>⚠️ 危險區域</h2>
            <p style="color:#666;margin-bottom:10px;">刪除操作無法復原，請謹慎使用</p>
            <button class="btn btn-danger" onclick="deleteAll()">🗑️ 刪除所有 BAPE 商品</button>
        </div>
    </div>
    
    <script>
        let pollInterval;
        
        function log(msg, type='info') {
            const logDiv = document.getElementById('log');
            const time = new Date().toLocaleTimeString();
            const color = type === 'success' ? '#4ec9b0' : type === 'error' ? '#f14c4c' : '#d4d4d4';
            logDiv.innerHTML += `<div style="color:${color}">[${time}] ${msg}</div>`;
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        
        function updateStatus(data) {
            document.getElementById('phase').textContent = data.phase || '-';
            document.getElementById('progress').textContent = `${data.progress||0}/${data.total||0}`;
            document.getElementById('current').textContent = data.current_product || '-';
            document.getElementById('productCount').textContent = data.products?.length || 0;
            document.getElementById('draftCount').textContent = data.set_to_draft || 0;
            document.getElementById('errorCount').textContent = data.errors?.length || 0;
            document.getElementById('progressFill').style.width = data.total > 0 ? (data.progress/data.total*100)+'%' : '0%';
        }
        
        async function pollStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                updateStatus(data);
                if (!data.running) {
                    clearInterval(pollInterval);
                    if (data.phase === 'completed') log('✅ 完成！', 'success');
                    if (data.errors && data.errors.length > 0) {
                        data.errors.forEach(e => log('❌ ' + (e.error || JSON.stringify(e)), 'error'));
                    }
                }
            } catch (e) { console.error(e); }
        }
        
        async function startTest() {
            log('🧪 開始測試單品...');
            const res = await fetch('/api/test_single?category=' + document.getElementById('testCat').value);
            const data = await res.json();
            if (data.success) {
                log('測試已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            } else log('❌ ' + (data.error || '啟動失敗'), 'error');
        }
        
        async function startTestSync() {
            log('🧪 開始測試上架（每分類 10 個）...');
            const res = await fetch('/api/test_sync?category=' + document.getElementById('testSyncCat').value);
            const data = await res.json();
            if (data.success) {
                log('測試上架已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            } else log('❌ ' + (data.error || '啟動失敗'), 'error');
        }
        
        async function startSync() {
            log('🔄 開始完整同步...');
            const res = await fetch('/api/auto_sync?category=' + document.getElementById('syncCat').value);
            const data = await res.json();
            if (data.success) {
                log('同步已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            } else log('❌ ' + (data.error || '啟動失敗'), 'error');
        }
        
        async function deleteAll() {
            if (!confirm('確定要刪除所有 BAPE 商品嗎？此操作無法復原！')) return;
            if (!confirm('真的確定嗎？所有商品都會被刪除！')) return;
            
            log('🗑️ 開始刪除所有 BAPE 商品...', 'error');
            const res = await fetch('/api/delete_all');
            const data = await res.json();
            if (data.success) {
                log('刪除已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            } else log('❌ ' + (data.error || '啟動失敗'), 'error');
        }
        
        async function testShopify() {
            log('測試 Shopify...');
            const res = await fetch('/api/test');
            const data = await res.json();
            if (data.data?.shop) log('✅ ' + data.data.shop.name, 'success');
            else log('❌ 連線失敗', 'error');
        }
        
        async function testBape() {
            log('測試 BAPE...');
            const res = await fetch('/api/test_bape');
            const data = await res.json();
            for (const [k, v] of Object.entries(data)) {
                if (v.ok) log(`✅ ${k}: ${v.products_found} 個`, 'success');
                else log(`❌ ${k}: ${v.error}`, 'error');
            }
        }
        
        async function countProducts() {
            const res = await fetch('/api/count');
            const data = await res.json();
            log('商品數量: ' + data.count, 'success');
        }
        
        async function publishAll() {
            log('📢 發布所有商品...');
            const res = await fetch('/api/publish_all');
            const data = await res.json();
            if (data.success) {
                log('發布已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            } else log('❌ ' + (data.error || '啟動失敗'), 'error');
        }
    </script>
</body>
</html>'''


@app.route('/api/status')
def api_status():
    return jsonify(scrape_status)


@app.route('/api/test')
def api_test():
    load_shopify_token()
    return jsonify(graphql_request("{ shop { name } }"))


@app.route('/api/test_single')
def api_test_single():
    category = request.args.get('category', 'mens')
    if scrape_status.get('running'):
        return jsonify({'success': False, 'error': '正在執行中'})
    if category not in CATEGORIES:
        return jsonify({'success': False, 'error': '無效分類'})
    threading.Thread(target=run_test_single, args=(category,)).start()
    return jsonify({'success': True})


@app.route('/api/scrape')
def api_scrape():
    category = request.args.get('category', 'all')
    if scrape_status.get('running'):
        return jsonify({'success': False, 'error': '正在執行中'})
    threading.Thread(target=run_scrape, args=(category,)).start()
    return jsonify({'success': True})


@app.route('/api/upload')
def api_upload():
    jsonl_file = request.args.get('file', '')
    if not jsonl_file or not os.path.exists(jsonl_file):
        return jsonify({'error': 'JSONL 不存在'})
    if scrape_status['running']:
        return jsonify({'error': '正在執行中'})
    threading.Thread(target=run_bulk_upload, args=(jsonl_file,)).start()
    return jsonify({'started': True})


@app.route('/api/auto_sync')
def api_auto_sync():
    category = request.args.get('category', 'all')
    if scrape_status.get('running'):
        return jsonify({'success': False, 'error': '正在執行中'})
    threading.Thread(target=run_full_sync, args=(category,)).start()
    return jsonify({'success': True})


@app.route('/api/cron_sync')
def api_cron_sync():
    category = request.args.get('category', 'all')
    if scrape_status.get('running'):
        return jsonify({'success': False, 'error': '正在執行中'})
    return jsonify(run_full_sync(category))


@app.route('/api/bulk_status')
def api_bulk_status():
    return jsonify(check_bulk_operation_status(scrape_status.get('bulk_operation_id') or None))


@app.route('/api/publish_all')
def api_publish_all():
    if scrape_status.get('running'):
        return jsonify({'error': '正在執行中'})
    
    def run_publish():
        global scrape_status
        scrape_status['running'] = True
        scrape_status['phase'] = 'publishing'
        try:
            results = batch_publish_bape_products()
            scrape_status['current_product'] = f"完成！成功: {results.get('success', 0)}"
        except Exception as e:
            scrape_status['errors'].append({'error': str(e)})
        finally:
            scrape_status['running'] = False
            scrape_status['phase'] = 'idle'
    
    threading.Thread(target=run_publish).start()
    return jsonify({'success': True})


@app.route('/api/delete_all')
def api_delete_all():
    """刪除所有 BAPE 商品"""
    if scrape_status.get('running'):
        return jsonify({'error': '正在執行中'})
    
    def run_delete():
        global scrape_status
        scrape_status['running'] = True
        scrape_status['phase'] = 'deleting'
        scrape_status['progress'] = 0
        scrape_status['total'] = 0
        scrape_status['current_product'] = '取得商品列表...'
        scrape_status['errors'] = []
        
        try:
            products = fetch_bape_product_ids()
            scrape_status['total'] = len(products)
            
            results = delete_all_bape_products()
            scrape_status['current_product'] = f"✅ 刪除完成！已刪除 {results.get('deleted', 0)} 個商品"
        except Exception as e:
            scrape_status['errors'].append({'error': str(e)})
            scrape_status['current_product'] = f"❌ 錯誤: {str(e)}"
        finally:
            scrape_status['running'] = False
            scrape_status['phase'] = 'completed'
    
    threading.Thread(target=run_delete).start()
    return jsonify({'success': True, 'message': '已開始刪除'})


@app.route('/api/test_sync')
def api_test_sync():
    """測試上架（每個分類只抓 10 個）"""
    category = request.args.get('category', 'all')
    limit = int(request.args.get('limit', 10))
    
    if scrape_status.get('running'):
        return jsonify({'success': False, 'error': '正在執行中'})
    
    threading.Thread(target=run_test_sync, args=(category, limit)).start()
    return jsonify({'success': True, 'message': f'測試上架已啟動（每分類 {limit} 個）'})


@app.route('/api/count')
def api_count():
    load_shopify_token()
    result = graphql_request("{ productsCount(query: \"vendor:BAPE\") { count } }")
    return jsonify({'count': result.get('data', {}).get('productsCount', {}).get('count', 0)})


@app.route('/api/test_bape')
def api_test_bape():
    """測試連線到 jp.bape.com 並顯示分類統計"""
    results = {}
    
    try:
        # 先測試基本連線
        url = f"{SOURCE_URL}/collections/all/products.json?page=1&limit=10"
        print(f"[TEST BAPE] 測試連線: {url}")
        
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        results['connection'] = {
            'url': url,
            'status': response.status_code,
            'ok': response.status_code == 200
        }
        
        if response.status_code == 200:
            # 取得所有商品並分類
            all_by_category = fetch_all_products_by_category()
            
            results['categories'] = {
                'mens': {
                    'name': 'メンズ（男裝）',
                    'count': len(all_by_category.get('mens', []))
                },
                'womens': {
                    'name': 'レディース（女裝）',
                    'count': len(all_by_category.get('womens', []))
                },
                'kids': {
                    'name': 'キッズ（童裝）',
                    'count': len(all_by_category.get('kids', []))
                }
            }
            
            total = sum(len(v) for v in all_by_category.values())
            results['total_products'] = total
            
            # 顯示每個分類的前 3 個商品
            for cat_key in ['mens', 'womens', 'kids']:
                products = all_by_category.get(cat_key, [])[:3]
                results['categories'][cat_key]['samples'] = [
                    {'title': p.get('title', '')[:50], 'handle': p.get('handle', '')}
                    for p in products
                ]
        
    except requests.exceptions.Timeout:
        results['error'] = '連線超時'
    except requests.exceptions.ConnectionError as e:
        results['error'] = f'連線失敗: {str(e)[:100]}'
    except Exception as e:
        results['error'] = str(e)
        import traceback
        results['traceback'] = traceback.format_exc()
    
    return jsonify(results)


@app.route('/api/test_html')
def api_test_html():
    """直接測試 HTML 爬取"""
    category = request.args.get('category', 'mens')
    
    if category not in CATEGORIES:
        return jsonify({'error': f'無效分類: {category}'})
    
    cat_info = CATEGORIES[category]
    url = f"{SOURCE_URL}{cat_info['base_url']}?{cat_info['filter']}"
    
    result = {
        'category': category,
        'url': url
    }
    
    try:
        print(f"[TEST HTML] 請求: {url}")
        response = requests.get(url, headers=HEADERS, timeout=30)
        
        result['status'] = response.status_code
        result['content_length'] = len(response.text)
        result['headers'] = dict(response.headers)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 頁面標題
            title = soup.find('title')
            result['page_title'] = title.get_text()[:100] if title else None
            
            # 所有連結
            all_links = soup.find_all('a', href=True)
            result['total_links'] = len(all_links)
            
            # 商品連結
            product_handles = []
            for link in all_links:
                href = link.get('href', '')
                if '/products/' in href:
                    match = re.search(r'/products/([^/?#]+)', href)
                    if match and match.group(1) != 'products':
                        if match.group(1) not in product_handles:
                            product_handles.append(match.group(1))
            
            result['products_found'] = len(product_handles)
            result['product_handles'] = product_handles[:20]
            
            # 連結樣本
            result['sample_links'] = [a.get('href', '')[:80] for a in all_links[:30]]
            
            # HTML 片段（前 2000 字元）
            result['html_preview'] = response.text[:2000]
        else:
            result['response_text'] = response.text[:1000]
            
    except requests.exceptions.Timeout:
        result['error'] = '連線超時 (30秒)'
    except requests.exceptions.ConnectionError as e:
        result['error'] = f'連線失敗: {str(e)}'
    except Exception as e:
        result['error'] = str(e)
        import traceback
        result['traceback'] = traceback.format_exc()
    
    return jsonify(result)


if __name__ == '__main__':
    print("BAPE 爬蟲工具")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
