"""
BAPE 商品爬蟲 + Shopify Bulk Operations 上架工具 v2.2
來源：jp.bape.com
功能：
1. 按分類爬取 jp.bape.com 商品（メンズ、レディース、キッズ）
2. 翻譯並產生 JSONL 檔案
3. 使用 Shopify Bulk Operations API 批量上傳
4. 智慧同步：新商品上架、已存在只更新價格
5. v2.2: 下架/缺貨商品直接刪除（不設草稿）
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

SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
SOURCE_URL = "https://jp.bape.com"

CATEGORIES = {
    'mens': {'name': 'メンズ', 'collection': "BAPE Men's", 'base_url': '/collections/all',
        'filter': 'filter.p.m.bape_data.type=%E3%83%A1%E3%83%B3%E3%82%BA&filter.v.availability=1',
        'tags': ['BAPE', 'A BATHING APE', '日本', '潮流', '男裝'], 'product_type': "BAPE 男裝"},
    'womens': {'name': 'レディース', 'collection': "BAPE Women's", 'base_url': '/collections/all',
        'filter': 'filter.p.m.bape_data.type=%E3%83%AC%E3%83%87%E3%82%A3%E3%83%BC%E3%82%B9&filter.v.availability=1',
        'tags': ['BAPE', 'A BATHING APE', '日本', '潮流', '女裝'], 'product_type': "BAPE 女裝"},
    'kids': {'name': 'キッズ', 'collection': "BAPE Kids", 'base_url': '/collections/all',
        'filter': 'filter.p.m.bape_data.type=%E3%82%AD%E3%83%83%E3%82%BA&filter.v.availability=1',
        'tags': ['BAPE', 'A BATHING APE', '日本', '潮流', '童裝', '兒童'], 'product_type': "BAPE 童裝"},
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
DEFAULT_WEIGHT = 0.5
JSONL_DIR = "/tmp/bape_jsonl"

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/html', 'Accept-Language': 'ja,en;q=0.9'}

os.makedirs(JSONL_DIR, exist_ok=True)

scrape_status = {
    "running": False, "phase": "", "progress": 0, "total": 0,
    "current_product": "", "products": [], "errors": [],
    "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "",
    "deleted": 0,
}


def load_shopify_token():
    global SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN
    if not SHOPIFY_SHOP: SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP", "")
    if not SHOPIFY_ACCESS_TOKEN: SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")


def graphql_request(query, variables=None):
    load_shopify_token()
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-10/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    payload = {'query': query}
    if variables: payload['variables'] = variables
    return requests.post(url, headers=headers, json=payload, timeout=60).json()


_collection_id_cache = {}


def get_or_create_collection(collection_name):
    global _collection_id_cache
    if collection_name in _collection_id_cache: return _collection_id_cache[collection_name]
    query = """query findCollection($title: String!) { collections(first: 1, query: $title) { edges { node { id title } } } }"""
    result = graphql_request(query, {"title": f"title:{collection_name}"})
    for edge in result.get('data', {}).get('collections', {}).get('edges', []):
        if edge['node']['title'] == collection_name:
            _collection_id_cache[collection_name] = edge['node']['id']
            return edge['node']['id']
    mutation = """mutation createCollection($input: CollectionInput!) { collectionCreate(input: $input) { collection { id title } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"title": collection_name, "descriptionHtml": f"<p>{collection_name} - 日本 A BATHING APE 官方正品代購</p>"}})
    collection = result.get('data', {}).get('collectionCreate', {}).get('collection')
    if collection:
        _collection_id_cache[collection_name] = collection['id']
        publish_collection_to_all_channels(collection['id'])
        return collection['id']
    return None


def publish_collection_to_all_channels(collection_id):
    pub_ids = get_all_publication_ids()
    if not pub_ids: return
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { publishable { availablePublicationsCount { count } } userErrors { field message } } }"""
    graphql_request(mutation, {"id": collection_id, "input": [{"publicationId": pid} for pid in pub_ids]})


def get_all_publication_ids():
    result = graphql_request("""{ publications(first: 20) { edges { node { id name } } } }""")
    return [edge['node']['id'] for edge in result.get('data', {}).get('publications', {}).get('edges', [])]


def get_all_publications():
    result = graphql_request("""{ publications(first: 20) { edges { node { id name } } } }""")
    return [{'id': edge['node']['id'], 'name': edge['node']['name']} for edge in result.get('data', {}).get('publications', {}).get('edges', [])]


def calculate_selling_price(cost, weight):
    return int((cost + weight * 1250) / 0.7)


def contains_japanese(text):
    if not text: return False
    return bool(re.search(r'[\u3040-\u309F\u30A0-\u30FF]', text))


def remove_japanese(text):
    if not text: return text
    return re.sub(r'\s+', ' ', re.sub(r'[\u3040-\u309F\u30A0-\u30FF]+', '', text)).strip()


# ========== 翻譯 ==========

def translate_with_chatgpt(title, description, size_spec=''):
    if not OPENAI_API_KEY:
        return {'success': False, 'title': f"BAPE {title}", 'description': description}
    size_spec_section = f"\n尺寸規格表：\n{size_spec}" if size_spec else ""
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本潮流品牌商品資訊翻譯成繁體中文。

商品名稱（日文/英文）：{title}
商品說明：{description[:1500] if description else ''}{size_spec_section}

請回傳 JSON 格式（不要加 markdown 標記）：
{{"title": "翻譯後的商品名稱（繁體中文，前面加上 BAPE）","description": "翻譯後的商品說明（HTML格式，用<br>換行）","size_spec_translated": "翻譯後的尺寸規格（格式：列1|列2|列3，每行換行分隔）"}}

規則：1. 絕對禁止日文 2. 開頭「BAPE」3. 尺寸：サイズ→尺寸、着丈→衣長、身幅→身寬、肩幅→肩寬、袖丈→袖長 4. 忽略注意事項和價格 5. 只回傳JSON"""
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是翻譯專家。輸出禁止任何日文。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1500}, timeout=60)
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content'].strip()
            if content.startswith('```'): content = content.split('\n', 1)[1]
            if content.endswith('```'): content = content.rsplit('```', 1)[0]
            translated = json.loads(content.strip())
            trans_title = translated.get('title', title)
            trans_desc = translated.get('description', description)
            trans_size = translated.get('size_spec_translated', '')
            if contains_japanese(trans_title): trans_title = remove_japanese(trans_title)
            if contains_japanese(trans_desc): trans_desc = remove_japanese(trans_desc)
            if not trans_title.startswith('BAPE'): trans_title = f"BAPE {trans_title}"
            size_html = build_size_table_html(trans_size) if trans_size else ''
            if size_html: trans_desc += '<br><br>' + size_html
            return {'success': True, 'title': trans_title, 'description': trans_desc}
        return {'success': False, 'title': f"BAPE {title}", 'description': description}
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {'success': False, 'title': f"BAPE {title}", 'description': description}


def build_size_table_html(size_spec_text):
    if not size_spec_text: return ''
    lines = [line.strip() for line in size_spec_text.strip().split('\n') if line.strip()]
    if not lines: return ''
    html = '<div class="size-spec"><h3>📏 尺寸規格</h3><table style="border-collapse:collapse;width:100%;margin:10px 0;">'
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.split('|')]
        if i == 0:
            html += '<tr style="background:#f5f5f5;">' + ''.join(f'<th style="border:1px solid #ddd;padding:8px;text-align:center;">{c}</th>' for c in cells) + '</tr>'
        else:
            html += '<tr>' + ''.join(f'<td style="border:1px solid #ddd;padding:8px;{"font-weight:bold;background:#fafafa;" if j==0 else "text-align:center;"}">{c}</td>' for j, c in enumerate(cells)) + '</tr>'
    html += '</table><p style="font-size:12px;color:#666;">※ 尺寸可能有些許誤差</p></div>'
    return html


def clean_description(description):
    description = re.sub(r'<a[^>]*>.*?</a>', '', description)
    for pat in [r'[^<>]*\d+[,，]?\d*\s*日圓[^<>]*', r'[^<>]*\d+[,，]?\d*\s*円[^<>]*',
                r'[^<>]*\d+%\s*OFF[^<>]*', r'[^<>]*降價[^<>]*', r'[^<>]*大幅[^<>]*',
                r'[^<>]*注意事項[^<>]*', r'[^<>]*請注意[^<>]*', r'[^<>]*敬請諒解[^<>]*',
                r'[^<>]*敬請見諒[^<>]*', r'[^<>]*※[^<>]*']:
        description = re.sub(pat, '', description, flags=re.IGNORECASE)
    description = re.sub(r'<p>\s*</p>', '', description)
    description = re.sub(r'<br\s*/?>\s*<br\s*/?>', '<br>', description)
    description = re.sub(r'^\s*(<br\s*/?>)+', '', description)
    description = re.sub(r'(<br\s*/?>)+\s*$', '', description).strip()
    return description + "\n<br><br>\n<p><strong>【請注意以下事項】</strong></p>\n<p>※不接受退換貨</p>\n<p>※開箱請全程錄影</p>\n<p>※因庫存有限，訂購時間不同可能會出現缺貨情況。</p>\n"


# ========== 爬取函數 ==========

def fetch_products_json(page=1):
    url = f"{SOURCE_URL}/collections/all/products.json?page={page}&limit=50"
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200: return []
        return response.json().get('products', [])
    except Exception as e:
        print(f"[錯誤] {e}"); return []


def get_product_category(product):
    tags = product.get('tags', [])
    title = product.get('title', '').upper()
    tags_str = ','.join(tags).lower() if isinstance(tags, list) else str(tags).lower()
    if 'キッズ' in tags_str or 'kids' in tags_str or 'KIDS' in title or 'キッズ' in title: return 'kids'
    if 'レディース' in tags_str or 'ladies' in tags_str or 'women' in tags_str or 'LADIES' in title or 'レディース' in title: return 'womens'
    return 'mens'


def fetch_all_products_by_category():
    all_products = {'mens': [], 'womens': [], 'kids': []}
    page = 1; seen_handles = set()
    while True:
        products = fetch_products_json(page)
        if not products: break
        for p in products:
            handle = p.get('handle', '')
            if handle in seen_handles: continue
            seen_handles.add(handle)
            if not any(v.get('available', False) for v in p.get('variants', [])): continue
            all_products[get_product_category(p)].append(p)
        if len(products) < 50: break
        page += 1; time.sleep(0.5)
    print(f"[爬取] 分類結果: 男裝 {len(all_products['mens'])}, 女裝 {len(all_products['womens'])}, 童裝 {len(all_products['kids'])}")
    return all_products


def fetch_size_table(handle):
    try:
        response = requests.get(f"{SOURCE_URL}/products/{handle}", headers={'User-Agent': HEADERS['User-Agent'], 'Accept': 'text/html'}, timeout=30)
        if response.status_code != 200: return None
        soup = BeautifulSoup(response.text, 'html.parser')
        def_list = soup.find('dl', class_='s-product-detail__def-list-description')
        if not def_list: return None
        size_dt = def_list.find('dt', string=re.compile(r'サイズ'))
        if not size_dt: return None
        size_dd = size_dt.find_next_sibling('dd')
        if not size_dd: return None
        table = size_dd.find('table')
        if not table: return None
        return '\n'.join([' | '.join([cell.get_text(strip=True) for cell in row.find_all(['th', 'td'])]) for row in table.find_all('tr')])
    except: return None


# ========== JSONL 生成 ==========

def product_to_jsonl_entry(product, category_key, collection_id, existing_product_id=None):
    cat_info = CATEGORIES[category_key]
    title = product.get('title', ''); body_html = product.get('body_html', ''); handle = product.get('handle', '')
    source_url = f"{SOURCE_URL}/products/{handle}"
    size_spec = fetch_size_table(handle)
    translated = translate_with_chatgpt(title, body_html, size_spec or '')
    trans_title = translated['title']; trans_desc = clean_description(translated['description'])
    options = product.get('options', []); source_variants = product.get('variants', []); images = product.get('images', [])
    has_options = len(options) > 0 and not (len(options) == 1 and options[0].get('name') == 'Title')
    product_options = []
    if has_options:
        for opt in options:
            opt_name = opt.get('name', ''); opt_values = opt.get('values', [])
            if opt_values and opt_name != 'Title':
                product_options.append({"name": opt_name, "values": [{"name": v} for v in opt_values]})
    image_list = [img['src'] for img in images[:10]] if images else []
    first_image = image_list[0] if image_list else None
    image_id_to_url = {img.get('id'): img.get('src', '') for img in images if img.get('id')}
    files = [{"originalSource": u, "contentType": "IMAGE"} for u in image_list]
    variants = []
    for sv in source_variants:
        if not sv.get('available', False): continue
        cost = float(sv.get('price', 0))
        if cost < MIN_PRICE: continue
        weight = float(sv.get('grams', 0)) / 1000 if sv.get('grams') else DEFAULT_WEIGHT
        selling_price = calculate_selling_price(cost, weight)
        sku_parts = [f"bape-{handle}"]; option_values = []
        if sv.get('option1') and len(options) > 0 and options[0].get('name') != 'Title':
            sku_parts.append(sv['option1']); option_values.append({"optionName": options[0]['name'], "name": sv['option1']})
        if sv.get('option2') and len(options) > 1:
            sku_parts.append(sv['option2']); option_values.append({"optionName": options[1]['name'], "name": sv['option2']})
        if sv.get('option3') and len(options) > 2:
            sku_parts.append(sv['option3']); option_values.append({"optionName": options[2]['name'], "name": sv['option3']})
        variant = {"price": selling_price, "sku": '-'.join(sku_parts), "inventoryPolicy": "CONTINUE", "taxable": False, "inventoryItem": {"cost": cost}}
        if option_values: variant["optionValues"] = option_values
        variant_image_id = sv.get('image_id') or sv.get('featured_image', {}).get('id')
        if variant_image_id and variant_image_id in image_id_to_url:
            variant["file"] = {"originalSource": image_id_to_url[variant_image_id], "contentType": "IMAGE"}
        elif first_image: variant["file"] = {"originalSource": first_image, "contentType": "IMAGE"}
        variants.append(variant)
    if not variants: return None
    product_input = {
        "title": trans_title, "descriptionHtml": trans_desc, "vendor": "BAPE",
        "productType": cat_info['product_type'], "status": "ACTIVE", "handle": f"bape-{handle}",
        "tags": cat_info['tags'],
        "seo": {"title": f"{trans_title} | BAPE 日本代購", "description": f"日本 A BATHING APE 官方正品代購。{trans_title}，台灣現貨或日本直送。GOYOUTATI 御用達日本伴手禮專門店。"},
        "metafields": [{"namespace": "custom", "key": "link", "value": source_url, "type": "url"}]}
    if existing_product_id: product_input["id"] = existing_product_id
    if collection_id: product_input["collections"] = [collection_id]
    if product_options: product_input["productOptions"] = product_options
    if variants: product_input["variants"] = variants
    if files: product_input["files"] = files
    return {"productSet": product_input, "synchronous": True}


# ========== Bulk Operations ==========

def create_staged_upload():
    query = """mutation stagedUploadsCreate($input: [StagedUploadInput!]!) { stagedUploadsCreate(input: $input) { stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }"""
    result = graphql_request(query, {"input": [{"resource": "BULK_MUTATION_VARIABLES", "filename": "products.jsonl", "mimeType": "text/jsonl", "httpMethod": "POST"}]})
    targets = result.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    return targets[0] if targets else None


def upload_jsonl_to_staged(staged_target, jsonl_path):
    params = {p['name']: p['value'] for p in staged_target['parameters']}
    with open(jsonl_path, 'rb') as f:
        response = requests.post(staged_target['url'], data=params, files={'file': ('products.jsonl', f, 'text/jsonl')}, timeout=300)
    return response.status_code in [200, 201, 204]


def run_bulk_mutation(staged_upload_path):
    query = """mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) { bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) { bulkOperation { id status } userErrors { field message } } }"""
    mutation = """mutation call($productSet: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $productSet) { product { id title } userErrors { field message } } }"""
    return graphql_request(query, {"mutation": mutation, "stagedUploadPath": staged_upload_path})


def check_bulk_operation_status(operation_id=None):
    if operation_id:
        query = """query($id: ID!) { node(id: $id) { ... on BulkOperation { id status errorCode objectCount url } } }"""
        return graphql_request(query, {"id": operation_id}).get('data', {}).get('node', {})
    else:
        return graphql_request("""{ currentBulkOperation(type: MUTATION) { id status errorCode objectCount url } }""").get('data', {}).get('currentBulkOperation', {})


# ========== 商品管理 ==========

def fetch_bape_product_ids():
    all_products = []; cursor = None
    while True:
        if cursor:
            query = """query($cursor: String) { products(first: 250, after: $cursor, query: "vendor:BAPE") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }"""
            result = graphql_request(query, {"cursor": cursor})
        else:
            result = graphql_request("""{ products(first: 250, query: "vendor:BAPE") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }""")
        products = result.get('data', {}).get('products', {})
        for edge in products.get('edges', []):
            node = edge['node']
            all_products.append({'id': node['id'], 'title': node['title'], 'handle': node['handle'], 'status': node.get('status', '')})
            cursor = edge['cursor']
        if not products.get('pageInfo', {}).get('hasNextPage', False): break
        time.sleep(0.5)
    return all_products


def set_product_active(product_id):
    mutation = """mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id status } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id, "status": "ACTIVE"}})
    return not result.get('data', {}).get('productUpdate', {}).get('userErrors', [])


def update_existing_product_price(product_id, source_variants):
    query = f"""{{ product(id: "{product_id}") {{ variants(first: 100) {{ edges {{ node {{ id sku }} }} }} }} }}"""
    result = graphql_request(query)
    shopify_variants = result.get('data', {}).get('product', {}).get('variants', {}).get('edges', [])
    if not shopify_variants: return 0
    costs = [float(sv.get('price', 0)) for sv in source_variants if sv.get('available', False) and float(sv.get('price', 0)) >= MIN_PRICE]
    if not costs: return 0
    min_cost = min(costs)
    weight = float(source_variants[0].get('grams', 0)) / 1000 if source_variants[0].get('grams') else DEFAULT_WEIGHT
    selling_price = calculate_selling_price(min_cost, weight)
    updated = 0
    for v_edge in shopify_variants:
        mutation = """mutation productVariantUpdate($input: ProductVariantInput!) { productVariantUpdate(input: $input) { productVariant { id } userErrors { field message } } }"""
        graphql_request(mutation, {"input": {"id": v_edge['node']['id'], "price": str(selling_price)}})
        updated += 1; time.sleep(0.1)
    return updated


def delete_product(product_id):
    mutation = """mutation productDelete($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id}})
    return not result.get('data', {}).get('productDelete', {}).get('userErrors', [])


def delete_all_bape_products():
    global scrape_status
    products = fetch_bape_product_ids()
    total = len(products)
    if total == 0: return {'success': True, 'deleted': 0, 'message': '沒有 BAPE 商品'}
    deleted = 0; failed = 0
    for i, product in enumerate(products):
        scrape_status['current_product'] = f"刪除中 [{i+1}/{total}] {product.get('title', '')[:30]}"
        scrape_status['progress'] = i + 1
        if delete_product(product['id']): deleted += 1
        else: failed += 1
        time.sleep(0.2)
    return {'success': True, 'deleted': deleted, 'failed': failed, 'total': total}


def batch_publish_bape_products():
    products = fetch_bape_product_ids()
    if not products: return {'success': 0, 'failed': 0}
    publications = get_all_publications()
    if not publications: return {'success': 0, 'failed': 0}
    publication_inputs = [{"publicationId": pub['id']} for pub in publications]
    results = {'total': len(products), 'success': 0, 'failed': 0}
    pub_mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
    cat_mutation = """mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id } userErrors { field message } } }"""
    for product in products:
        result = graphql_request(pub_mutation, {"id": product['id'], "input": publication_inputs})
        if result.get('data', {}).get('publishablePublish', {}).get('userErrors', []): results['failed'] += 1
        else: results['success'] += 1
        graphql_request(cat_mutation, {"input": {"id": product['id'], "productCategory": {"productTaxonomyNodeId": "gid://shopify/ProductTaxonomyNode/1"}}})
        time.sleep(0.1)
    return results


# ========== 主流程 ==========

def run_test_single(category='mens'):
    global scrape_status
    scrape_status = {"running": True, "phase": "testing", "progress": 0, "total": 1, "current_product": "測試單品...",
        "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "deleted": 0}
    try:
        cat_info = CATEGORIES[category]
        collection_id = get_or_create_collection(cat_info['collection'])
        if not collection_id:
            scrape_status['errors'].append({'error': '無法建立 Collection'}); return
        all_by_category = fetch_all_products_by_category()
        products = all_by_category.get(category, [])
        if not products:
            scrape_status['errors'].append({'error': f'沒有找到 {cat_info["name"]} 的商品'}); return
        test_product = None
        for p in products:
            if min((float(v.get('price', 0)) for v in p.get('variants', [])), default=0) >= MIN_PRICE:
                test_product = p; break
        if not test_product:
            scrape_status['errors'].append({'error': '沒有符合條件的商品'}); return
        scrape_status['current_product'] = f"翻譯: {test_product['title'][:30]}..."
        entry = product_to_jsonl_entry(test_product, category, collection_id)
        if not entry:
            scrape_status['errors'].append({'error': '商品轉換失敗'}); return
        product_input = entry['productSet']
        scrape_status['products'].append({'title': product_input['title'], 'handle': product_input['handle'], 'variants': len(product_input.get('variants', []))})
        scrape_status['current_product'] = "上傳到 Shopify..."
        mutation = """mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $input) { product { id title handle } userErrors { field code message } } }"""
        result = graphql_request(mutation, {"input": product_input, "synchronous": True})
        product_set = result.get('data', {}).get('productSet', {})
        user_errors = product_set.get('userErrors', [])
        if user_errors:
            scrape_status['errors'].append({'error': '; '.join([e.get('message', str(e)) for e in user_errors])})
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
    finally:
        scrape_status['running'] = False


def run_full_sync(category='all'):
    """v2.2 智慧同步：新商品→Bulk Upload / 已存在→更新價格 / 下架/缺貨→刪除"""
    global scrape_status
    print(f"[SYNC] ========== 開始智慧同步 v2.2 ==========")
    scrape_status = {"running": True, "phase": "cron_sync", "progress": 0, "total": 0,
        "current_product": "開始智慧同步...", "products": [], "errors": [],
        "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "deleted": 0}
    try:
        categories_to_scrape = ['mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not categories_to_scrape: raise Exception(f'未知分類: {category}')

        # 1. 取得 Shopify 現有商品
        scrape_status['current_product'] = '取得 Shopify 現有商品...'
        existing_products = fetch_bape_product_ids()
        existing_handles = {p['handle']: p for p in existing_products}
        print(f"[SYNC] Shopify 現有 {len(existing_handles)} 個 BAPE 商品")

        # 2. 爬取 BAPE 所有商品
        scrape_status['current_product'] = '爬取 BAPE 商品...'
        all_by_category = fetch_all_products_by_category()

        # 3. 比對 + 處理
        new_entries = []; scraped_handles = set()
        updated_count = 0; price_updated_count = 0

        for cat_key in categories_to_scrape:
            cat_info = CATEGORIES[cat_key]
            collection_id = get_or_create_collection(cat_info['collection'])
            if not collection_id: continue
            products = all_by_category.get(cat_key, [])
            print(f"[SYNC] {cat_info['collection']} 共 {len(products)} 個有庫存商品")
            scrape_status['total'] += len(products)

            for product in products:
                scrape_status['progress'] += 1
                handle = product.get('handle', ''); title = product.get('title', '')[:30]
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {title}"
                my_handle = f"bape-{handle}"; scraped_handles.add(my_handle)
                existing_info = existing_handles.get(my_handle)

                if existing_info:
                    try:
                        cnt = update_existing_product_price(existing_info['id'], product.get('variants', []))
                        if cnt > 0: price_updated_count += 1
                        if existing_info.get('status') == 'DRAFT':
                            set_product_active(existing_info['id'])
                            pub_ids = get_all_publication_ids()
                            if pub_ids:
                                graphql_request("""mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }""",
                                    {"id": existing_info['id'], "input": [{"publicationId": pid} for pid in pub_ids]})
                        updated_count += 1
                    except Exception as e:
                        scrape_status['errors'].append({'error': f'更新失敗 {title}: {str(e)}'})
                    time.sleep(0.2)
                else:
                    try:
                        entry = product_to_jsonl_entry(product, cat_key, collection_id)
                        if entry:
                            new_entries.append(entry)
                            scrape_status['products'].append({'title': entry['productSet']['title'], 'handle': entry['productSet']['handle'], 'variants': len(entry['productSet'].get('variants', []))})
                    except Exception as e:
                        scrape_status['errors'].append({'error': f'轉換失敗 {title}: {str(e)}'})
                    time.sleep(0.3)

        # 4. 新商品批量上傳
        if new_entries:
            jsonl_path = os.path.join(JSONL_DIR, f"bape_{category}_{int(time.time())}.jsonl")
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                for entry in new_entries: f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            scrape_status['jsonl_file'] = jsonl_path
            scrape_status['phase'] = 'uploading'
            scrape_status['current_product'] = f'批量上傳 {len(new_entries)} 個新商品...'
            staged = create_staged_upload()
            if not staged: raise Exception('建立 Staged Upload 失敗')
            if not upload_jsonl_to_staged(staged, jsonl_path): raise Exception('上傳 JSONL 失敗')
            staged_path = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), '')
            result = run_bulk_mutation(staged_path)
            user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
            if user_errors: raise Exception(f'Bulk Mutation 錯誤: {user_errors}')
            scrape_status['bulk_operation_id'] = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {}).get('id', '')
            scrape_status['current_product'] = '等待上傳完成...'
            for _ in range(120):
                status = check_bulk_operation_status()
                scrape_status['bulk_status'] = status.get('status', '')
                if status.get('status') == 'COMPLETED': break
                elif status.get('status') in ['FAILED', 'CANCELED']: raise Exception(f'Bulk 失敗: {status.get("status")}')
                time.sleep(5)
            scrape_status['phase'] = 'publishing'
            scrape_status['current_product'] = '發布新商品...'
            batch_publish_bape_products()

        # === v2.2: 下架/缺貨商品直接刪除（不設草稿）===
        scrape_status['phase'] = 'deleting'
        scrape_status['current_product'] = '清理下架/缺貨商品...'
        delete_count = 0
        for handle, product_info in existing_handles.items():
            if handle not in scraped_handles:
                print(f"[SYNC] 🗑 刪除: {handle} - {product_info.get('title', '')[:30]}")
                scrape_status['current_product'] = f"刪除: {product_info.get('title', '')[:30]}"
                if delete_product(product_info['id']):
                    delete_count += 1
                time.sleep(0.2)

        scrape_status['deleted'] = delete_count
        scrape_status['current_product'] = f"✅ 完成！新商品 {len(new_entries)} 個，更新 {updated_count} 個，刪除 {delete_count} 個"
        scrape_status['phase'] = 'completed'
        print(f"[SYNC] ✅ 新商品: {len(new_entries)}, 更新價格: {price_updated_count}, 刪除: {delete_count}")
        return {'success': True, 'new_products': len(new_entries), 'updated': updated_count, 'deleted': delete_count}

    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        scrape_status['current_product'] = f"❌ 錯誤: {str(e)}"
        scrape_status['phase'] = 'error'
        import traceback; traceback.print_exc()
        return {'success': False, 'error': str(e)}
    finally:
        scrape_status['running'] = False


# ========== Flask Routes + Frontend ==========

@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>BAPE 爬蟲工具</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f5f5;padding:20px}.container{max-width:1000px;margin:0 auto}h1{color:#333;margin-bottom:20px}.card{background:white;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,0.1)}.card h2{color:#444;margin-bottom:15px;font-size:18px}.btn{display:inline-block;padding:12px 24px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;margin:5px}.btn-primary{background:#0066ff;color:white}.btn-success{background:#00c853;color:white}.btn-warning{background:#ff9800;color:white}.btn-danger{background:#f44336;color:white}.btn:disabled{opacity:0.5;cursor:not-allowed}.status{background:#f8f9fa;border-radius:8px;padding:15px;margin-top:15px}.progress-bar{height:20px;background:#e0e0e0;border-radius:10px;overflow:hidden;margin:10px 0}.progress-fill{height:100%;background:linear-gradient(90deg,#0066ff,#00c853);transition:width 0.3s}.log{background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:8px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:12px}select{padding:10px;border-radius:6px;border:1px solid #ddd;font-size:14px;margin-right:10px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin-top:15px}.stat-box{background:#f0f4f8;padding:15px;border-radius:8px;text-align:center}.stat-value{font-size:24px;font-weight:bold}.stat-label{font-size:12px;color:#666}.danger-zone{border:2px solid #f44336;background:#fff5f5}</style></head>
<body><div class="container">
<h1>🦍 BAPE 爬蟲工具 <small style="font-size:14px;color:#999">v2.2</small></h1>
<div class="card"><h2>⚡ 測試單品</h2>
<select id="testCat"><option value="mens">男裝</option><option value="womens">女裝</option><option value="kids">童裝</option></select>
<button class="btn btn-warning" onclick="startTest()">🧪 測試單品</button></div>
<div class="card"><h2>🔄 智慧同步</h2>
<p style="color:#666;margin-bottom:10px;">新商品→翻譯上架 / 已存在→更新價格 / <b style="color:#e67e22">下架/缺貨→自動刪除</b></p>
<select id="syncCat"><option value="all">全部</option><option value="mens">男裝</option><option value="womens">女裝</option><option value="kids">童裝</option></select>
<button class="btn btn-success" onclick="startSync()">🔄 開始同步</button></div>
<div class="card"><h2>📊 執行狀態</h2>
<div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
<div class="status"><div>階段：<span id="phase">-</span></div><div>進度：<span id="progress">0/0</span></div><div>目前：<span id="current">-</span></div></div>
<div class="stats">
<div class="stat-box"><div class="stat-value" id="productCount">0</div><div class="stat-label">新商品</div></div>
<div class="stat-box"><div class="stat-value" id="deletedCount" style="color:#e67e22">0</div><div class="stat-label">已刪除</div></div>
<div class="stat-box"><div class="stat-value" id="errorCount" style="color:#e74c3c">0</div><div class="stat-label">錯誤</div></div>
</div></div>
<div class="card"><h2>📝 日誌</h2><div class="log" id="log"></div></div>
<div class="card"><h2>🔧 工具</h2>
<button class="btn btn-primary" onclick="testShopify()">測試 Shopify</button>
<button class="btn btn-primary" onclick="testBape()">測試 BAPE</button>
<button class="btn btn-primary" onclick="countProducts()">商品數量</button>
<button class="btn btn-success" onclick="publishAll()">發布所有</button></div>
<div class="card danger-zone"><h2>⚠️ 危險區域</h2><p style="color:#666;margin-bottom:10px;">刪除操作無法復原</p>
<button class="btn btn-danger" onclick="deleteAll()">🗑️ 刪除所有 BAPE 商品</button></div>
</div>
<script>
let pollInterval;
function log(msg,type='info'){const d=document.getElementById('log');const t=new Date().toLocaleTimeString();const c=type==='success'?'#4ec9b0':type==='error'?'#f14c4c':'#d4d4d4';d.innerHTML+=`<div style="color:${c}">[${t}] ${msg}</div>`;d.scrollTop=d.scrollHeight}
function updateStatus(d){document.getElementById('phase').textContent=d.phase||'-';document.getElementById('progress').textContent=`${d.progress||0}/${d.total||0}`;document.getElementById('current').textContent=d.current_product||'-';document.getElementById('productCount').textContent=d.products?.length||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors?.length||0;document.getElementById('progressFill').style.width=d.total>0?(d.progress/d.total*100)+'%':'0%'}
async function pollStatus(){try{const r=await fetch('/api/status');const d=await r.json();updateStatus(d);if(!d.running){clearInterval(pollInterval);if(d.phase==='completed')log('✅ 完成！','success');if(d.errors?.length>0)d.errors.forEach(e=>log('❌ '+(e.error||JSON.stringify(e)),'error'))}}catch(e){}}
async function startTest(){log('🧪 開始測試單品...');const r=await fetch('/api/test_single?category='+document.getElementById('testCat').value);const d=await r.json();if(d.success){log('測試已啟動','success');pollInterval=setInterval(pollStatus,1000)}else log('❌ '+(d.error||'啟動失敗'),'error')}
async function startSync(){log('🔄 開始智慧同步...');const r=await fetch('/api/auto_sync?category='+document.getElementById('syncCat').value);const d=await r.json();if(d.success){log('同步已啟動','success');pollInterval=setInterval(pollStatus,1000)}else log('❌ '+(d.error||'啟動失敗'),'error')}
async function deleteAll(){if(!confirm('確定要刪除所有 BAPE 商品嗎？'))return;if(!confirm('真的確定嗎？'))return;log('🗑️ 開始刪除...','error');const r=await fetch('/api/delete_all');const d=await r.json();if(d.success){log('刪除已啟動','success');pollInterval=setInterval(pollStatus,1000)}else log('❌ '+(d.error||'啟動失敗'),'error')}
async function testShopify(){log('測試 Shopify...');const r=await fetch('/api/test');const d=await r.json();if(d.data?.shop)log('✅ '+d.data.shop.name,'success');else log('❌ 連線失敗','error')}
async function testBape(){log('測試 BAPE...');const r=await fetch('/api/test_bape');const d=await r.json();log('總商品: '+(d.total_products||0),d.total_products?'success':'error')}
async function countProducts(){const r=await fetch('/api/count');const d=await r.json();log('Shopify 商品數量: '+d.count,'success')}
async function publishAll(){log('📢 發布所有商品...');const r=await fetch('/api/publish_all');const d=await r.json();if(d.success){log('發布已啟動','success');pollInterval=setInterval(pollStatus,1000)}else log('❌ '+(d.error||'啟動失敗'),'error')}
</script></body></html>'''


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
    if scrape_status.get('running'): return jsonify({'success': False, 'error': '正在執行中'})
    if category not in CATEGORIES: return jsonify({'success': False, 'error': '無效分類'})
    threading.Thread(target=run_test_single, args=(category,)).start()
    return jsonify({'success': True})

@app.route('/api/auto_sync')
def api_auto_sync():
    category = request.args.get('category', 'all')
    if scrape_status.get('running'): return jsonify({'success': False, 'error': '正在執行中'})
    threading.Thread(target=run_full_sync, args=(category,)).start()
    return jsonify({'success': True})

@app.route('/api/cron')
def api_cron():
    category = request.args.get('category', 'all')
    if scrape_status.get('running'): return jsonify({'success': False, 'error': '正在執行中'})
    threading.Thread(target=run_full_sync, args=(category,)).start()
    return jsonify({'success': True, 'message': '智慧同步已啟動'})

@app.route('/api/bulk_status')
def api_bulk_status():
    return jsonify(check_bulk_operation_status(scrape_status.get('bulk_operation_id') or None))

@app.route('/api/publish_all')
def api_publish_all():
    if scrape_status.get('running'): return jsonify({'error': '正在執行中'})
    def run_publish():
        global scrape_status
        scrape_status['running'] = True; scrape_status['phase'] = 'publishing'
        try:
            results = batch_publish_bape_products()
            scrape_status['current_product'] = f"完成！成功: {results.get('success', 0)}"
        except Exception as e: scrape_status['errors'].append({'error': str(e)})
        finally: scrape_status['running'] = False; scrape_status['phase'] = 'idle'
    threading.Thread(target=run_publish).start()
    return jsonify({'success': True})

@app.route('/api/delete_all')
def api_delete_all():
    if scrape_status.get('running'): return jsonify({'error': '正在執行中'})
    def run_delete():
        global scrape_status
        scrape_status['running'] = True; scrape_status['phase'] = 'deleting'
        scrape_status['progress'] = 0; scrape_status['total'] = 0; scrape_status['errors'] = []
        try:
            products = fetch_bape_product_ids(); scrape_status['total'] = len(products)
            results = delete_all_bape_products()
            scrape_status['current_product'] = f"✅ 已刪除 {results.get('deleted', 0)} 個商品"
        except Exception as e: scrape_status['errors'].append({'error': str(e)})
        finally: scrape_status['running'] = False; scrape_status['phase'] = 'completed'
    threading.Thread(target=run_delete).start()
    return jsonify({'success': True})

@app.route('/api/count')
def api_count():
    load_shopify_token()
    result = graphql_request("{ productsCount(query: \"vendor:BAPE\") { count } }")
    return jsonify({'count': result.get('data', {}).get('productsCount', {}).get('count', 0)})

@app.route('/api/test_bape')
def api_test_bape():
    results = {}
    try:
        all_by_category = fetch_all_products_by_category()
        results['total_products'] = sum(len(v) for v in all_by_category.values())
        results['categories'] = {k: len(v) for k, v in all_by_category.items()}
    except Exception as e: results['error'] = str(e)
    return jsonify(results)


if __name__ == '__main__':
    print("BAPE 爬蟲工具 v2.2")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
