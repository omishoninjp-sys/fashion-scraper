"""
WORKMAN 商品爬蟲 + Shopify Bulk Operations 上架工具 + 庫存同步
來源：workman.jp
功能：
1. 爬取 workman.jp 各分類商品
2. 翻譯並產生 JSONL 檔案
3. 使用 Shopify Bulk Operations API 批量上傳
4. 庫存同步：檢查官網庫存狀態，缺貨商品自動下架
"""

from flask import Flask, jsonify, send_file
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

SOURCE_URL = "https://workman.jp"
CATEGORIES = {
    'work': {'url': '/shop/c/c51/', 'collection': 'WORKMAN 作業服', 'tags': ['WORKMAN', '日本', '服飾', '作業服', '工作服']},
    'mens': {'url': '/shop/c/c52/', 'collection': 'WORKMAN 男裝', 'tags': ['WORKMAN', '日本', '服飾', '男裝']},
    'womens': {'url': '/shop/c/c53/', 'collection': 'WORKMAN 女裝', 'tags': ['WORKMAN', '日本', '服飾', '女裝']},
    'kids': {'url': '/shop/c/c54/', 'collection': 'WORKMAN 兒童', 'tags': ['WORKMAN', '日本', '服飾', '兒童', '童裝']}
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DEFAULT_WEIGHT = 0.5
JSONL_DIR = "/tmp/workman_jsonl"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en;q=0.9',
}

# 缺貨關鍵字
OUT_OF_STOCK_KEYWORDS = [
    '店舗のみのお取り扱い',
    'オンラインストア販売終了',
    '店舗在庫を確認する',
    '予約受付は終了',
    '受付終了',
    '取り扱いを終了',
]

os.makedirs(JSONL_DIR, exist_ok=True)

scrape_status = {
    "running": False, "phase": "", "progress": 0, "total": 0,
    "current_product": "", "products": [], "errors": [],
    "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "",
}

inventory_sync_status = {
    "running": False, "phase": "", "progress": 0, "total": 0,
    "current_product": "",
    "results": {"checked": 0, "in_stock": 0, "out_of_stock": 0, "draft_set": 0, "inventory_zeroed": 0, "errors": 0, "page_gone": 0},
    "details": [], "errors": [],
}

def reset_inventory_sync_status():
    global inventory_sync_status
    inventory_sync_status = {
        "running": False, "phase": "", "progress": 0, "total": 0,
        "current_product": "",
        "results": {"checked": 0, "in_stock": 0, "out_of_stock": 0, "draft_set": 0, "inventory_zeroed": 0, "errors": 0, "page_gone": 0},
        "details": [], "errors": [],
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
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
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
      collections(first: 1, query: $title) { edges { node { id title } } }
    }
    """
    result = graphql_request(query, {"title": f"title:{collection_name}"})
    edges = result.get('data', {}).get('collections', {}).get('edges', [])
    for edge in edges:
        if edge['node']['title'] == collection_name:
            collection_id = edge['node']['id']
            _collection_id_cache[collection_name] = collection_id
            return collection_id
    mutation = """
    mutation createCollection($input: CollectionInput!) {
      collectionCreate(input: $input) { collection { id title } userErrors { field message } }
    }
    """
    result = graphql_request(mutation, {"input": {"title": collection_name, "descriptionHtml": f"<p>{collection_name} 商品系列</p>"}})
    collection = result.get('data', {}).get('collectionCreate', {}).get('collection')
    if collection:
        collection_id = collection['id']
        _collection_id_cache[collection_name] = collection_id
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
      publishablePublish(id: $id, input: $input) { publishable { availablePublicationsCount { count } } userErrors { field message } }
    }
    """
    graphql_request(mutation, {"id": collection_id, "input": publication_inputs})

def get_all_publication_ids():
    query = '{ publications(first: 20) { edges { node { id name } } } }'
    result = graphql_request(query)
    return [edge['node']['id'] for edge in result.get('data', {}).get('publications', {}).get('edges', [])]

def calculate_selling_price(cost, weight):
    shipping_cost = weight * 1250
    base_price = cost + shipping_cost
    selling_price = base_price / 0.7
    return int(selling_price)

def contains_japanese(text):
    if not text:
        return False
    return bool(re.search(r'[\u3040-\u309F]', text) or re.search(r'[\u30A0-\u30FF]', text))

def remove_japanese(text):
    if not text:
        return text
    cleaned = re.sub(r'[\u3040-\u309F\u30A0-\u30FF]+', '', text)
    return re.sub(r'\s+', ' ', cleaned).strip()

# ========== 翻譯 ==========

def translate_with_chatgpt(title, description, size_spec=''):
    size_spec_section = f"\n尺寸規格表：\n{size_spec}" if size_spec else ""
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文。

商品名稱（日文）：{title}
商品說明：{description[:1500] if description else ''}{size_spec_section}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，前面加上 WORKMAN）",
    "description": "翻譯後的商品說明（HTML格式，用<br>換行）",
    "size_spec_translated": "翻譯後的尺寸規格（格式：列1|列2|列3，每行換行分隔）"
}}

規則：
1. 絕對禁止日文（平假名、片假名）
2. 商品名稱開頭必須是「WORKMAN」
3. 尺寸欄位翻譯：サイズ→尺寸、着丈→衣長、身幅→身寬、肩幅→肩寬、袖丈→袖長
4. 完全忽略注意事項（ご注意、注意事項、ご了承、※記號開頭的警告文字等）
5. 完全忽略價格相關內容（円、日圓、OFF、割引、値下げ等）
6. 只回傳 JSON"""

    try:
        response = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是翻譯專家。輸出禁止任何日文。"},
                {"role": "user", "content": prompt}
            ], "temperature": 0, "max_tokens": 1500}, timeout=60)
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
            if not trans_title.startswith('WORKMAN'):
                trans_title = f"WORKMAN {trans_title}"
            size_html = build_size_table_html(trans_size) if trans_size else ''
            if size_html:
                trans_desc += '<br><br>' + size_html
            return {'success': True, 'title': trans_title, 'description': trans_desc}
        else:
            return {'success': False, 'title': f"WORKMAN {title}", 'description': description}
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {'success': False, 'title': f"WORKMAN {title}", 'description': description}

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
                if j == 0:
                    style += 'font-weight:bold;background:#fafafa;'
                else:
                    style += 'text-align:center;'
                html += f'<td style="{style}">{cell}</td>'
            html += '</tr>'
    html += '</table><p style="font-size:12px;color:#666;">※ 尺寸可能有些許誤差</p></div>'
    return html

# ========== 爬取函數 ==========

def get_total_pages(category_url):
    url = SOURCE_URL + category_url
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            last_link = soup.find('a', string='最後')
            if last_link and last_link.get('href'):
                match = re.search(r'_p(\d+)', last_link['href'])
                if match:
                    return int(match.group(1))
            pagination = soup.find_all('a', href=re.compile(r'_p\d+'))
            max_page = 1
            for link in pagination:
                match = re.search(r'_p(\d+)', link.get('href', ''))
                if match:
                    max_page = max(max_page, int(match.group(1)))
            if max_page > 1:
                return max_page
            pager = soup.find('div', class_=re.compile(r'pager|pagination'))
            if pager:
                for link in pager.find_all('a'):
                    text = link.get_text(strip=True)
                    if text.isdigit():
                        max_page = max(max_page, int(text))
                return max_page
            return 1
    except Exception as e:
        print(f"[ERROR] 取得總頁數失敗: {e}")
    return 1

def fetch_all_product_links(category_key):
    category = CATEGORIES[category_key]
    base_url = category['url']
    total_pages = get_total_pages(base_url)
    print(f"[INFO] {category['collection']} 共 {total_pages} 頁")
    all_links = []
    for page in range(1, total_pages + 1):
        if page == 1:
            page_url = SOURCE_URL + base_url
        else:
            page_url = SOURCE_URL + base_url.rstrip('/') + f'_p{page}/'
        try:
            response = requests.get(page_url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                for link in soup.find_all('a', href=True):
                    href = link.get('href', '')
                    if '/shop/g/' in href:
                        full_url = SOURCE_URL + href if href.startswith('/') else href
                        full_url = full_url.split('?')[0]
                        if full_url not in all_links:
                            all_links.append(full_url)
            elif response.status_code == 404:
                break
        except Exception as e:
            print(f"[ERROR] 頁面 {page} 載入失敗: {e}")
        time.sleep(0.5)
    print(f"[INFO] {category['collection']} 共 {len(all_links)} 個商品")
    return all_links

def parse_product_page(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        # === 缺貨檢查：發現缺貨關鍵字直接跳過不上架 ===
        page_text = soup.get_text()
        for keyword in OUT_OF_STOCK_KEYWORDS:
            if keyword in page_text:
                print(f"[跳過] 缺貨（{keyword}）: {url}")
                return None
        if '売り切れ' in page_text or '品切れ' in page_text:
            print(f"[跳過] 缺貨（売り切れ/品切れ/予約受付は終了）: {url}")
            return None
        # === 缺貨檢查結束 ===
        title = ''
        title_elem = soup.find('h1', class_='block-goods-name')
        if title_elem:
            title = title_elem.get_text(strip=True)
        else:
            title_elem = soup.find('h1')
            if title_elem:
                title = title_elem.get_text(strip=True)
        price = 0
        price_elem = soup.find('p', class_='block-goods-price')
        if not price_elem:
            price_elem = soup.find(class_=re.compile(r'price'))
        if price_elem:
            match = re.search(r'[\d,]+', price_elem.get_text(strip=True))
            if match:
                price = int(match.group().replace(',', ''))
        manage_code = ''
        code_dt = soup.find('dt', string='管理番号')
        if code_dt:
            code_dd = code_dt.find_next_sibling('dd')
            if code_dd:
                manage_code = code_dd.get_text(strip=True)
        if not manage_code:
            match = re.search(r'/g/g(\d+)/', url)
            if match:
                manage_code = match.group(1)
        if not manage_code:
            return None
        if price == 0:
            price = 1500
        description = ''
        size_spec = ''
        comment1 = soup.find('dl', class_='block-goods-comment1')
        if comment1:
            desc_dd = comment1.find('dd', class_='js-goods-tabContents')
            if desc_dd:
                for tag in desc_dd.find_all(['script', 'style']):
                    tag.decompose()
                desc_content = []
                for elem in desc_dd.children:
                    if hasattr(elem, 'name') and elem.name in ['p', 'div']:
                        text = elem.get_text(strip=True)
                        if text:
                            desc_content.append(str(elem))
                description = '\n'.join(desc_content)
        comment2 = soup.find('dl', class_='block-goods-comment2')
        if comment2:
            spec_dd = comment2.find('dd', class_='js-goods-tabContents')
            if spec_dd:
                table = spec_dd.find('table')
                if table:
                    for row in table.find_all('tr'):
                        cells = row.find_all(['th', 'td'])
                        size_spec += ' | '.join([c.get_text(strip=True) for c in cells]) + '\n'
        colors = []
        images = []
        slider = soup.find('div', class_='js-goods-detail-goods-slider')
        if slider:
            for img in slider.find_all('img', class_='js-zoom'):
                img_src = img.get('src', '')
                if img_src:
                    full_url = SOURCE_URL + img_src
                    if '_t1.' in img_src:
                        images.insert(0, full_url)
                    elif full_url not in images:
                        images.append(full_url)
        gallery = soup.find('ul', class_='js-goods-detail-gallery-slider')
        if gallery:
            for item in gallery.find_all('li', class_='block-goods-gallery--color-variation-src'):
                color_elem = item.find('p', class_='block-goods-detail--color-variation-goods-color-name')
                if color_elem:
                    color = color_elem.get_text(strip=True)
                    if color and color not in colors:
                        colors.append(color)
        if not colors:
            colors = ['標準']
        sizes = []
        size_dt = soup.find('dt', string='サイズ・スペック')
        if size_dt:
            size_dd = size_dt.find_next_sibling('dd')
            if size_dd:
                table = size_dd.find('table')
                if table:
                    first_row = table.find('tr')
                    if first_row:
                        for th in first_row.find_all('th')[1:]:
                            size = th.get_text(strip=True)
                            if size and size not in sizes:
                                sizes.append(size)
        if not sizes:
            sizes = ['FREE']
        images = list(dict.fromkeys(images))[:10]
        if not images and manage_code:
            images.append(f"{SOURCE_URL}/img/goods/L/{manage_code}_t1.jpg")
        return {'url': url, 'title': title, 'price': price, 'manage_code': manage_code,
                'description': description, 'size_spec': size_spec, 'colors': colors, 'sizes': sizes, 'images': images}
    except Exception as e:
        print(f"[ERROR] 解析失敗 {url}: {e}")
        return None

def product_to_jsonl_entry(product_data, tags, category_key, collection_id, existing_product_id=None):
    PRODUCT_TYPES = {'work': 'WORKMAN 作業服', 'mens': 'WORKMAN 男裝', 'womens': 'WORKMAN 女裝', 'kids': 'WORKMAN 兒童'}
    product_type = PRODUCT_TYPES.get(category_key, 'WORKMAN')
    translated = translate_with_chatgpt(product_data['title'], product_data['description'], product_data.get('size_spec', ''))
    title = translated['title']
    description = translated['description']
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
    description = re.sub(r'\n\s*\n', '\n', description).strip()
    notice = """
<br><br>
<p><strong>【請注意以下事項】</strong></p>
<p>※不接受退換貨</p>
<p>※開箱請全程錄影</p>
<p>※因庫存有限，訂購時間不同可能會出現缺貨情況。</p>
"""
    description = description + notice
    manage_code = product_data['manage_code']
    cost = product_data['price']
    colors = product_data['colors']
    sizes = product_data['sizes']
    images = product_data['images']
    source_url = product_data['url']
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)
    product_options = []
    has_color_option = len(colors) > 1 or (len(colors) == 1 and colors[0] != '標準')
    has_size_option = len(sizes) > 1 or (len(sizes) == 1 and sizes[0] != 'FREE')
    if has_color_option:
        product_options.append({"name": "顏色", "values": [{"name": c} for c in colors]})
    if has_size_option:
        product_options.append({"name": "尺寸", "values": [{"name": s} for s in sizes]})
    image_list = images[:10] if images else []
    first_image = image_list[0] if image_list else None
    files = [{"originalSource": img_url, "contentType": "IMAGE"} for img_url in image_list]
    variant_file = {"originalSource": first_image, "contentType": "IMAGE"} if first_image else None
    variants = []
    if has_color_option and has_size_option:
        for color in colors:
            for size in sizes:
                v = {"price": selling_price, "sku": f"{manage_code}-{color}-{size}", "inventoryPolicy": "CONTINUE", "taxable": False, "inventoryItem": {"cost": cost}, "optionValues": [{"optionName": "顏色", "name": color}, {"optionName": "尺寸", "name": size}]}
                if variant_file: v["file"] = variant_file
                variants.append(v)
    elif has_color_option:
        for color in colors:
            v = {"price": selling_price, "sku": f"{manage_code}-{color}", "inventoryPolicy": "CONTINUE", "taxable": False, "inventoryItem": {"cost": cost}, "optionValues": [{"optionName": "顏色", "name": color}]}
            if variant_file: v["file"] = variant_file
            variants.append(v)
    elif has_size_option:
        for size in sizes:
            v = {"price": selling_price, "sku": f"{manage_code}-{size}", "inventoryPolicy": "CONTINUE", "taxable": False, "inventoryItem": {"cost": cost}, "optionValues": [{"optionName": "尺寸", "name": size}]}
            if variant_file: v["file"] = variant_file
            variants.append(v)
    else:
        v = {"price": selling_price, "sku": manage_code, "inventoryPolicy": "CONTINUE", "taxable": False, "inventoryItem": {"cost": cost}}
        if variant_file: v["file"] = variant_file
        variants.append(v)
    seo_title = f"{title} | WORKMAN 日本代購"
    seo_description = f"日本 WORKMAN 官方正品代購。{title}，台灣現貨或日本直送，品質保證。GOYOUTATI 御用達日本伴手禮專門店。"
    product_input = {
        "title": title, "descriptionHtml": description, "vendor": "WORKMAN",
        "productType": product_type, "status": "ACTIVE", "handle": f"workman-{manage_code}", "tags": tags,
        "seo": {"title": seo_title, "description": seo_description},
        "metafields": [{"namespace": "custom", "key": "link", "value": source_url, "type": "url"}]
    }
    if existing_product_id: product_input["id"] = existing_product_id
    if collection_id: product_input["collections"] = [collection_id]
    if product_options: product_input["productOptions"] = product_options
    if variants: product_input["variants"] = variants
    if files: product_input["files"] = files
    return {"productSet": product_input, "synchronous": True}

# ========== Bulk Operations ==========

def create_staged_upload():
    query = """mutation stagedUploadsCreate($input: [StagedUploadInput!]!) { stagedUploadsCreate(input: $input) { stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }"""
    variables = {"input": [{"resource": "BULK_MUTATION_VARIABLES", "filename": "products.jsonl", "mimeType": "text/jsonl", "httpMethod": "POST"}]}
    result = graphql_request(query, variables)
    if 'errors' in result: return None
    targets = result.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    return targets[0] if targets else None

def upload_jsonl_to_staged(staged_target, jsonl_path):
    url = staged_target['url']
    params = {p['name']: p['value'] for p in staged_target['parameters']}
    with open(jsonl_path, 'rb') as f:
        files = {'file': ('products.jsonl', f, 'text/jsonl')}
        response = requests.post(url, data=params, files=files, timeout=300)
    return response.status_code in [200, 201, 204]

def run_bulk_mutation(staged_upload_path):
    query = """mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) { bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) { bulkOperation { id status } userErrors { field message } } }"""
    mutation = """mutation call($productSet: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $productSet) { product { id title } userErrors { field message } } }"""
    return graphql_request(query, {"mutation": mutation, "stagedUploadPath": staged_upload_path})

def check_bulk_operation_status(operation_id=None):
    if operation_id:
        query = """query($id: ID!) { node(id: $id) { ... on BulkOperation { id status errorCode createdAt completedAt objectCount fileSize url partialDataUrl } } }"""
        result = graphql_request(query, {"id": operation_id})
        return result.get('data', {}).get('node', {})
    else:
        query = '{ currentBulkOperation(type: MUTATION) { id status errorCode createdAt completedAt objectCount fileSize url } }'
        result = graphql_request(query)
        return result.get('data', {}).get('currentBulkOperation', {})

def get_bulk_operation_results():
    status = check_bulk_operation_status()
    results = {'status': status.get('status'), 'objectCount': status.get('objectCount'), 'errorCode': status.get('errorCode'), 'url': status.get('url')}
    if status.get('url'):
        try:
            response = requests.get(status['url'], timeout=30)
            if response.status_code == 200:
                lines = response.text.strip().split('\n')
                results['total_results'] = len(lines)
                errors, successes = [], []
                for line in lines[:50]:
                    try:
                        data = json.loads(line)
                        if 'data' in data and 'productSet' in data.get('data', {}):
                            ps = data['data']['productSet']
                            ue = ps.get('userErrors', [])
                            if ue: errors.append({'errors': ue})
                            elif ps.get('product'): successes.append({'id': ps['product'].get('id'), 'title': ps['product'].get('title', '')[:50]})
                    except: pass
                results['errors'] = errors[:10]
                results['successes'] = successes[:10]
                results['error_count'] = len(errors)
                results['success_count'] = len(successes)
        except Exception as e:
            results['fetch_error'] = str(e)
    return results

# ========== 發布與刪除 ==========

def get_all_publications():
    query = '{ publications(first: 20) { edges { node { id name catalog { title } } } } }'
    result = graphql_request(query)
    pubs = []
    for edge in result.get('data', {}).get('publications', {}).get('edges', []):
        node = edge.get('node', {})
        pubs.append({'id': node.get('id'), 'name': node.get('name') or node.get('catalog', {}).get('title', 'Unknown')})
    return pubs

def publish_product_to_all_channels(product_id):
    publications = get_all_publications()
    if not publications: return {'success': False, 'error': 'No publications found'}
    publication_inputs = [{"publicationId": pub['id']} for pub in publications]
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { publishable { availablePublicationsCount { count } } userErrors { field message } } }"""
    result = graphql_request(mutation, {"id": product_id, "input": publication_inputs})
    user_errors = result.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
    if user_errors: return {'success': False, 'errors': user_errors}
    return {'success': True, 'publications': len(publications)}

def batch_publish_workman_products():
    products = fetch_workman_product_ids()
    if not products: return {'success': False, 'error': 'No WORKMAN products found'}
    publications = get_all_publications()
    if not publications: return {'success': False, 'error': 'No publications found'}
    publication_inputs = [{"publicationId": pub['id']} for pub in publications]
    results = {'total': len(products), 'success': 0, 'failed': 0, 'errors': []}
    for product in products:
        mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
        result = graphql_request(mutation, {"id": product['id'], "input": publication_inputs})
        user_errors = result.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
        if user_errors: results['failed'] += 1
        else: results['success'] += 1
        time.sleep(0.1)
    return results

def fetch_workman_product_ids():
    all_ids = []
    cursor = None
    while True:
        if cursor:
            query = 'query($cursor: String) { products(first: 250, after: $cursor, query: "vendor:WORKMAN") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }'
            result = graphql_request(query, {"cursor": cursor})
        else:
            query = '{ products(first: 250, query: "vendor:WORKMAN") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }'
            result = graphql_request(query)
        edges = result.get('data', {}).get('products', {}).get('edges', [])
        for edge in edges:
            node = edge['node']
            all_ids.append({'id': node['id'], 'title': node['title'], 'handle': node['handle'], 'status': node.get('status', '')})
            cursor = edge['cursor']
        if not result.get('data', {}).get('products', {}).get('pageInfo', {}).get('hasNextPage', False):
            break
        time.sleep(0.5)
    return all_ids

def create_delete_jsonl(product_ids):
    jsonl_path = os.path.join(JSONL_DIR, f"delete_workman_{int(time.time())}.jsonl")
    with open(jsonl_path, 'w', encoding='utf-8') as f:
        for product in product_ids:
            f.write(json.dumps({"input": {"id": product['id']}}, ensure_ascii=False) + '\n')
    return jsonl_path

def run_bulk_delete_mutation(staged_upload_path):
    query = """mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) { bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) { bulkOperation { id status } userErrors { field message } } }"""
    mutation = """mutation call($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }"""
    return graphql_request(query, {"mutation": mutation, "stagedUploadPath": staged_upload_path})

def run_delete_workman_products():
    global scrape_status
    scrape_status = {"running": True, "phase": "deleting", "progress": 0, "total": 0, "current_product": "正在查詢 WORKMAN 商品...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}
    try:
        product_ids = fetch_workman_product_ids()
        if not product_ids:
            scrape_status['current_product'] = '沒有找到 WORKMAN 商品'
            scrape_status['running'] = False
            return
        scrape_status['total'] = len(product_ids)
        jsonl_path = create_delete_jsonl(product_ids)
        scrape_status['jsonl_file'] = jsonl_path
        staged = create_staged_upload()
        if not staged:
            scrape_status['errors'].append({'error': '建立 Staged Upload 失敗'})
            scrape_status['running'] = False
            return
        if not upload_jsonl_to_staged(staged, jsonl_path):
            scrape_status['errors'].append({'error': '上傳 JSONL 失敗'})
            scrape_status['running'] = False
            return
        staged_path = None
        for param in staged['parameters']:
            if param['name'] == 'key': staged_path = param['value']; break
        if not staged_path: staged_path = staged.get('resourceUrl', '')
        result = run_bulk_delete_mutation(staged_path)
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        if user_errors:
            scrape_status['errors'].append({'error': str(user_errors)})
            scrape_status['running'] = False
            return
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        scrape_status['bulk_status'] = bulk_op.get('status', '')
        scrape_status['current_product'] = f"批量刪除已啟動！正在刪除 {len(product_ids)} 個商品..."
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False

# ========== 庫存同步 ==========

def fetch_workman_products_with_source():
    all_products = []
    cursor = None
    while True:
        after_clause = f', after: "{cursor}"' if cursor else ''
        query = f'{{ products(first: 50, query: "vendor:WORKMAN"{after_clause}) {{ edges {{ node {{ id title handle status metafield(namespace: "custom", key: "link") {{ value }} variants(first: 100) {{ edges {{ node {{ id sku inventoryItem {{ id inventoryLevels(first: 5) {{ edges {{ node {{ id quantities(names: ["available"]) {{ name quantity }} location {{ id }} }} }} }} }} }} }} }} }} cursor }} pageInfo {{ hasNextPage }} }} }}'
        result = graphql_request(query)
        edges = result.get('data', {}).get('products', {}).get('edges', [])
        for edge in edges:
            node = edge['node']
            source_url = node.get('metafield', {}).get('value', '') if node.get('metafield') else ''
            variants = []
            for v_edge in node.get('variants', {}).get('edges', []):
                v_node = v_edge['node']
                inv_item = v_node.get('inventoryItem', {})
                inv_levels = inv_item.get('inventoryLevels', {}).get('edges', [])
                vi = {'id': v_node['id'], 'sku': v_node.get('sku', ''), 'inventory_item_id': inv_item.get('id', ''), 'inventory_levels': []}
                for le in inv_levels:
                    ln = le['node']
                    available = 0
                    for q in ln.get('quantities', []):
                        if q['name'] == 'available': available = q['quantity']
                    vi['inventory_levels'].append({'id': ln['id'], 'location_id': ln.get('location', {}).get('id', ''), 'available': available})
                variants.append(vi)
            all_products.append({'id': node['id'], 'title': node['title'], 'handle': node['handle'], 'status': node['status'], 'source_url': source_url, 'variants': variants})
            cursor = edge['cursor']
        if not result.get('data', {}).get('products', {}).get('pageInfo', {}).get('hasNextPage', False): break
        time.sleep(0.5)
    return all_products

def check_workman_stock(product_url):
    result = {'available': True, 'page_exists': True, 'out_of_stock_reason': ''}
    if not product_url:
        return {'available': False, 'page_exists': False, 'out_of_stock_reason': '無來源連結'}
    try:
        response = requests.get(product_url, headers=HEADERS, timeout=30)
        if response.status_code == 404:
            return {'available': False, 'page_exists': False, 'out_of_stock_reason': '頁面已不存在 (404)'}
        if response.status_code != 200:
            return {'available': False, 'page_exists': False, 'out_of_stock_reason': f'HTTP {response.status_code}'}
        page_text = BeautifulSoup(response.text, 'html.parser').get_text()
        for keyword in OUT_OF_STOCK_KEYWORDS:
            if keyword in page_text:
                return {'available': False, 'page_exists': True, 'out_of_stock_reason': keyword}
        if '売り切れ' in page_text or '品切れ' in page_text:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '売り切れ / 品切れ / 予約受付は終了'}
        return result
    except requests.exceptions.Timeout:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': '連線超時（暫不處理）'}
    except Exception as e:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': f'錯誤: {str(e)}（暫不處理）'}

def set_product_to_draft(product_id):
    mutation = """mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id status } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id, "status": "DRAFT"}})
    errors = result.get('data', {}).get('productUpdate', {}).get('userErrors', [])
    if errors: return False
    return True

def zero_variant_inventory(inventory_item_id, location_id):
    mutation = """mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) { inventorySetQuantities(input: $input) { inventoryAdjustmentGroup { reason } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"reason": "correction", "name": "available", "quantities": [{"inventoryItemId": inventory_item_id, "locationId": location_id, "quantity": 0}]}})
    errors = result.get('data', {}).get('inventorySetQuantities', {}).get('userErrors', [])
    return len(errors) == 0

def run_inventory_sync():
    global inventory_sync_status
    reset_inventory_sync_status()
    inventory_sync_status['running'] = True
    inventory_sync_status['phase'] = 'fetching'
    inventory_sync_status['current_product'] = '正在取得 Shopify 商品清單...'
    print(f"[Sync] ========== 開始庫存同步 ==========")
    try:
        products = fetch_workman_products_with_source()
        inventory_sync_status['total'] = len(products)
        if not products:
            inventory_sync_status['current_product'] = '沒有找到 WORKMAN 商品'
            inventory_sync_status['running'] = False
            return
        inventory_sync_status['phase'] = 'checking'
        for idx, product in enumerate(products):
            inventory_sync_status['progress'] = idx + 1
            inventory_sync_status['current_product'] = f"[{idx+1}/{len(products)}] {product['title'][:30]}"
            if product['status'] == 'DRAFT':
                inventory_sync_status['results']['checked'] += 1
                continue
            source_url = product['source_url']
            if not source_url:
                match = re.search(r'workman-(\d+)', product.get('handle', ''))
                if match: source_url = f"{SOURCE_URL}/shop/g/g{match.group(1)}/"
                else:
                    inventory_sync_status['results']['checked'] += 1
                    inventory_sync_status['results']['errors'] += 1
                    continue
            stock = check_workman_stock(source_url)
            inventory_sync_status['results']['checked'] += 1
            if stock['available']:
                inventory_sync_status['results']['in_stock'] += 1
                inventory_sync_status['details'].append({'title': product['title'][:40], 'status': 'in_stock', 'source_url': source_url})
            else:
                inventory_sync_status['results']['out_of_stock'] += 1
                if not stock['page_exists']: inventory_sync_status['results']['page_gone'] += 1
                for variant in product['variants']:
                    for level in variant['inventory_levels']:
                        if level['available'] > 0:
                            zero_variant_inventory(variant['inventory_item_id'], level['location_id'])
                            inventory_sync_status['results']['inventory_zeroed'] += 1
                if set_product_to_draft(product['id']):
                    inventory_sync_status['results']['draft_set'] += 1
                inventory_sync_status['details'].append({'title': product['title'][:40], 'status': 'out_of_stock', 'reason': stock['out_of_stock_reason'], 'source_url': source_url})
            time.sleep(1)
        inventory_sync_status['phase'] = 'completed'
        r = inventory_sync_status['results']
        inventory_sync_status['current_product'] = f"✅ 完成！檢查:{r['checked']} 有貨:{r['in_stock']} 缺貨:{r['out_of_stock']} 草稿:{r['draft_set']}"
        print(f"[Sync] ========== 庫存同步完成 ==========")
    except Exception as e:
        inventory_sync_status['errors'].append({'error': str(e)})
        inventory_sync_status['phase'] = 'error'
        print(f"[Sync] ❌ {e}")
    finally:
        inventory_sync_status['running'] = False

# ========== 主流程 ==========

def run_test_single():
    global scrape_status
    scrape_status = {"running": True, "phase": "testing", "progress": 0, "total": 1, "current_product": "測試單品模式...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}
    try:
        cat_key = 'kids'
        cat_info = CATEGORIES[cat_key]
        collection_id = get_or_create_collection(cat_info['collection'])
        if not collection_id:
            scrape_status['errors'].append({'error': '無法建立 Collection'})
            scrape_status['running'] = False
            return
        product_links = fetch_all_product_links(cat_key)
        if not product_links:
            scrape_status['errors'].append({'error': '無法取得商品連結'})
            scrape_status['running'] = False
            return
        product_data = parse_product_page(product_links[0])
        if not product_data:
            scrape_status['errors'].append({'error': '解析商品失敗'})
            scrape_status['running'] = False
            return
        entry = product_to_jsonl_entry(product_data, cat_info['tags'], cat_key, collection_id)
        product_input = entry['productSet']
        scrape_status['products'].append({'title': product_input['title'], 'handle': product_input['handle'], 'variants': len(product_input.get('variants', []))})
        mutation = """mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $input) { product { id title handle status productType seo { title description } variants(first: 10) { edges { node { id sku price taxable inventoryItem { unitCost { amount currencyCode } } } } } } userErrors { field code message } } }"""
        load_shopify_token()
        result = graphql_request(mutation, {"input": product_input, "synchronous": True})
        product_set = result.get('data', {}).get('productSet', {})
        user_errors = product_set.get('userErrors', [])
        if user_errors:
            scrape_status['errors'].append({'error': '; '.join([e.get('message', '') for e in user_errors])})
        else:
            product = product_set.get('product', {})
            publish_result = publish_product_to_all_channels(product.get('id', ''))
            scrape_status['current_product'] = f"✅ 測試成功！{product.get('title', '')}"
            scrape_status['test_result'] = {'id': product.get('id'), 'title': product.get('title'), 'handle': product.get('handle'), 'productType': product.get('productType', ''), 'seo': product.get('seo', {}), 'variants': product.get('variants', {}), 'published': publish_result.get('publications', 0)}
        scrape_status['progress'] = 1
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False

def run_scrape(category):
    global scrape_status
    scrape_status = {"running": True, "phase": "scraping", "progress": 0, "total": 0, "current_product": "", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}
    try:
        cats = ['work', 'mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not cats:
            scrape_status['errors'].append({'error': f'未知分類: {category}'})
            scrape_status['running'] = False
            return
        all_entries = []
        for cat_key in cats:
            cat_info = CATEGORIES[cat_key]
            collection_id = get_or_create_collection(cat_info['collection'])
            if not collection_id: continue
            product_links = fetch_all_product_links(cat_key)
            if not product_links: continue
            scrape_status['total'] += len(product_links)
            for link in product_links:
                scrape_status['progress'] += 1
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {link.split('/')[-2]}"
                product_data = parse_product_page(link)
                if not product_data:
                    scrape_status['errors'].append({'url': link, 'error': '解析失敗'})
                    continue
                try:
                    entry = product_to_jsonl_entry(product_data, cat_info['tags'], cat_key, collection_id)
                    all_entries.append(entry)
                    scrape_status['products'].append({'title': entry['productSet']['title'], 'handle': entry['productSet']['handle'], 'variants': len(entry['productSet'].get('variants', []))})
                except Exception as e:
                    scrape_status['errors'].append({'url': link, 'error': str(e)})
                time.sleep(0.5)
        if all_entries:
            jsonl_path = os.path.join(JSONL_DIR, f"workman_{category}_{int(time.time())}.jsonl")
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                for entry in all_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            scrape_status['jsonl_file'] = jsonl_path
        scrape_status['current_product'] = f"完成！共 {len(all_entries)} 個商品"
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
        if not staged:
            scrape_status['errors'].append({'error': '建立 Staged Upload 失敗'})
            return
        if not upload_jsonl_to_staged(staged, jsonl_path):
            scrape_status['errors'].append({'error': '上傳 JSONL 失敗'})
            return
        staged_path = None
        for param in staged['parameters']:
            if param['name'] == 'key': staged_path = param['value']; break
        if not staged_path: staged_path = staged.get('resourceUrl', '')
        result = run_bulk_mutation(staged_path)
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        if user_errors:
            scrape_status['errors'].append({'error': str(user_errors)})
            return
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        scrape_status['bulk_status'] = bulk_op.get('status', '')
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False

def update_existing_product_price(product_id, product_data):
    """已存在的商品：只更新價格，不重新翻譯"""
    cost = product_data['price']
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)
    
    # 取得商品的所有 variants
    query = f"""
    {{
      product(id: "{product_id}") {{
        variants(first: 100) {{
          edges {{
            node {{
              id
              sku
              inventoryItem {{
                id
                inventoryLevels(first: 5) {{
                  edges {{
                    node {{
                      id
                      location {{ id }}
                      quantities(names: ["available"]) {{ name quantity }}
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    result = graphql_request(query)
    variants = result.get('data', {}).get('product', {}).get('variants', {}).get('edges', [])
    
    updated_variants = 0
    for v_edge in variants:
        v_node = v_edge['node']
        variant_id = v_node['id']
        
        # 更新價格
        mutation = """mutation productVariantUpdate($input: ProductVariantInput!) {
            productVariantUpdate(input: $input) {
                productVariant { id }
                userErrors { field message }
            }
        }"""
        graphql_request(mutation, {"input": {"id": variant_id, "price": str(selling_price)}})
        updated_variants += 1
        time.sleep(0.1)
    
    return updated_variants


def set_variant_inventory_available(inventory_item_id, location_id, quantity=10):
    """將 variant 庫存設為有貨（預設 10）"""
    mutation = """mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
        inventorySetQuantities(input: $input) {
            inventoryAdjustmentGroup { reason }
            userErrors { field message }
        }
    }"""
    result = graphql_request(mutation, {"input": {"reason": "correction", "name": "available", "quantities": [{"inventoryItemId": inventory_item_id, "locationId": location_id, "quantity": quantity}]}})
    errors = result.get('data', {}).get('inventorySetQuantities', {}).get('userErrors', [])
    return len(errors) == 0


def set_product_active(product_id):
    """將商品設為 ACTIVE"""
    mutation = """mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id status } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id, "status": "ACTIVE"}})
    errors = result.get('data', {}).get('productUpdate', {}).get('userErrors', [])
    return len(errors) == 0


def run_full_sync(category='all'):
    """
    智慧同步：
    1. 爬 workman.jp 取得所有商品連結
    2. 比對 Shopify 現有商品
    3. 新商品 → 翻譯 + 上架
    4. 已存在 + 有貨 → 只更新價格，庫存設有貨
    5. 已存在 + 缺貨（parse 回傳 None）→ 庫存歸零 + 設草稿
    6. workman 沒有、Shopify 有 → 設草稿
    """
    global scrape_status
    scrape_status = {"running": True, "phase": "cron_sync", "progress": 0, "total": 0, "current_product": "開始智慧同步...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "set_to_draft": 0}
    try:
        cats = ['work', 'mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not cats: raise Exception(f'未知分類: {category}')
        
        # 1. 取得 Shopify 現有商品（含 inventory 資料）
        scrape_status['current_product'] = '取得 Shopify 現有商品...'
        existing_products = fetch_workman_products_with_source()
        existing_handles = {p['handle']: p for p in existing_products}
        print(f"[SYNC] Shopify 現有 {len(existing_handles)} 個 WORKMAN 商品")
        
        # 2. 爬取 + 比對
        new_entries = []  # 新商品用 Bulk Upload
        scraped_handles = set()
        updated_count = 0
        price_updated_count = 0
        
        for cat_key in cats:
            cat_info = CATEGORIES[cat_key]
            collection_id = get_or_create_collection(cat_info['collection'])
            if not collection_id: continue
            product_links = fetch_all_product_links(cat_key)
            if not product_links: continue
            scrape_status['total'] += len(product_links)
            
            for link in product_links:
                scrape_status['progress'] += 1
                code = link.split('/')[-2] if link.endswith('/') else link.split('/')[-1]
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {code}"
                
                # 從 URL 取得 manage_code
                match = re.search(r'/g/g(\d+)/', link)
                manage_code = match.group(1) if match else ''
                my_handle = f"workman-{manage_code}" if manage_code else ''
                
                existing_info = existing_handles.get(my_handle) if my_handle else None
                
                if existing_info:
                    # ===== 已存在的商品：只檢查庫存 + 更新價格 =====
                    scraped_handles.add(my_handle)
                    
                    # 檢查官網庫存（用簡單的 HTTP GET，不需要完整 parse）
                    stock = check_workman_stock(link)
                    
                    if stock['available']:
                        # 有貨 → 只更新價格 + 確保 ACTIVE
                        try:
                            response = requests.get(link, headers=HEADERS, timeout=30)
                            if response.status_code == 200:
                                soup = BeautifulSoup(response.text, 'html.parser')
                                price_elem = soup.find('p', class_='block-goods-price')
                                if not price_elem:
                                    price_elem = soup.find(class_=re.compile(r'price'))
                                if price_elem:
                                    price_match = re.search(r'[\d,]+', price_elem.get_text(strip=True))
                                    if price_match:
                                        new_price = int(price_match.group().replace(',', ''))
                                        product_data_simple = {'price': new_price}
                                        update_existing_product_price(existing_info['id'], product_data_simple)
                                        price_updated_count += 1
                            
                            # 確保商品是 ACTIVE（可能之前被設為草稿）
                            if existing_info.get('status') == 'DRAFT':
                                set_product_active(existing_info['id'])
                                # 重新發布
                                publications = get_all_publication_ids()
                                if publications:
                                    pub_mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
                                    graphql_request(pub_mutation, {"id": existing_info['id'], "input": [{"publicationId": pid} for pid in publications]})
                            
                            updated_count += 1
                            print(f"[SYNC] ✓ 更新價格: {existing_info['title'][:30]}")
                        except Exception as e:
                            scrape_status['errors'].append({'url': link, 'error': f'更新失敗: {str(e)}'})
                    else:
                        # 缺貨 → 庫存歸零 + 設草稿
                        print(f"[SYNC] ❌ 缺貨，下架: {existing_info['title'][:30]} ({stock['out_of_stock_reason']})")
                        for variant in existing_info.get('variants', []):
                            for level in variant.get('inventory_levels', []):
                                if level['available'] > 0:
                                    zero_variant_inventory(variant['inventory_item_id'], level['location_id'])
                        if existing_info.get('status') != 'DRAFT':
                            set_product_to_draft(existing_info['id'])
                    
                    time.sleep(0.3)
                else:
                    # ===== 新商品：完整爬取 + 翻譯 + 加入 Bulk Upload =====
                    product_data = parse_product_page(link)
                    if not product_data: continue
                    
                    if manage_code:
                        scraped_handles.add(f"workman-{product_data['manage_code']}")
                    
                    try:
                        entry = product_to_jsonl_entry(product_data, cat_info['tags'], cat_key, collection_id)
                        new_entries.append(entry)
                        scrape_status['products'].append({'title': entry['productSet']['title'], 'handle': entry['productSet']['handle'], 'variants': len(entry['productSet'].get('variants', []))})
                        print(f"[SYNC] ✚ 新商品: {entry['productSet']['title'][:30]}")
                    except Exception as e:
                        scrape_status['errors'].append({'url': link, 'error': str(e)})
                    time.sleep(0.5)
        
        # 3. 新商品批量上傳
        if new_entries:
            jsonl_path = os.path.join(JSONL_DIR, f"workman_{category}_{int(time.time())}.jsonl")
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                for entry in new_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            scrape_status['jsonl_file'] = jsonl_path
            
            scrape_status['phase'] = 'uploading'
            scrape_status['current_product'] = f'批量上傳 {len(new_entries)} 個新商品...'
            staged = create_staged_upload()
            if not staged: raise Exception('建立 Staged Upload 失敗')
            if not upload_jsonl_to_staged(staged, jsonl_path): raise Exception('上傳 JSONL 失敗')
            staged_path = None
            for param in staged['parameters']:
                if param['name'] == 'key': staged_path = param['value']; break
            if not staged_path: staged_path = staged.get('resourceUrl', '')
            result = run_bulk_mutation(staged_path)
            if 'errors' in result: raise Exception(f'Bulk Mutation 錯誤: {result["errors"]}')
            bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
            user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
            if user_errors: raise Exception(f'userErrors: {user_errors}')
            scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
            
            # 等待完成
            scrape_status['current_product'] = '等待上傳完成...'
            max_wait, wait_time = 600, 0
            while wait_time < max_wait:
                status = check_bulk_operation_status()
                if status.get('status') == 'COMPLETED': break
                elif status.get('status') in ['FAILED', 'CANCELED']: raise Exception(f'失敗: {status.get("status")}')
                time.sleep(5); wait_time += 5
            if wait_time >= max_wait: raise Exception('超時')
            
            # 發布新商品
            scrape_status['phase'] = 'publishing'
            scrape_status['current_product'] = '發布新商品...'
            batch_publish_workman_products()
        
        # 4. 下架：workman 沒有的商品設為草稿
        scrape_status['phase'] = 'drafting'
        scrape_status['current_product'] = '處理下架...'
        draft_count = 0
        for handle, product_info in existing_handles.items():
            if handle not in scraped_handles and product_info.get('status', '') == 'ACTIVE':
                print(f"[SYNC] 🗑 下架: {handle} - {product_info.get('title', '')[:30]}")
                # 庫存歸零
                for variant in product_info.get('variants', []):
                    for level in variant.get('inventory_levels', []):
                        if level['available'] > 0:
                            zero_variant_inventory(variant['inventory_item_id'], level['location_id'])
                if set_product_to_draft(product_info['id']):
                    draft_count += 1
                time.sleep(0.2)
        
        scrape_status['set_to_draft'] = draft_count
        scrape_status['current_product'] = f"✅ 完成！新商品 {len(new_entries)} 個，更新 {updated_count} 個，下架 {draft_count} 個"
        scrape_status['phase'] = 'completed'
        print(f"[SYNC] ✅ 新商品: {len(new_entries)}, 更新價格: {price_updated_count}, 下架: {draft_count}")
        return {'success': True, 'new_products': len(new_entries), 'updated': updated_count, 'set_to_draft': draft_count}
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        scrape_status['phase'] = 'error'
        return {'success': False, 'error': str(e)}
    finally:
        scrape_status['running'] = False

# ========== Flask 路由 ==========

@app.route('/')
def index():
    return open(os.path.join(os.path.dirname(__file__), 'templates', 'index.html'), 'r', encoding='utf-8').read()

@app.route('/api/status')
def api_status():
    return jsonify(scrape_status)

@app.route('/api/start')
def api_start():
    from flask import request
    category = request.args.get('category', 'mens')
    if scrape_status['running']: return jsonify({'error': '正在執行中'})
    threading.Thread(target=run_scrape, args=(category,)).start()
    return jsonify({'started': True, 'category': category})

@app.route('/api/test_single')
def api_test_single():
    if scrape_status['running']: return jsonify({'error': '正在執行中'})
    threading.Thread(target=run_test_single).start()
    return jsonify({'started': True, 'mode': 'test_single'})

@app.route('/api/test_result')
def api_test_result():
    return jsonify({'running': scrape_status.get('running'), 'phase': scrape_status.get('phase'), 'current_product': scrape_status.get('current_product'), 'errors': scrape_status.get('errors', []), 'test_result': scrape_status.get('test_result', {})})

@app.route('/api/cron')
def api_cron():
    from flask import request
    category = request.args.get('category', 'all')
    if scrape_status.get('running'): return jsonify({'success': False, 'error': '正在執行中'})
    valid = ['work', 'mens', 'womens', 'kids', 'all']
    if category not in valid: return jsonify({'success': False, 'error': f'無效分類: {category}'})
    threading.Thread(target=run_full_sync, args=(category,), daemon=False).start()
    return jsonify({'success': True, 'message': f'已開始同步: {category}', 'started_at': time.strftime('%Y-%m-%d %H:%M:%S')})

@app.route('/api/cron_sync')
def api_cron_sync():
    from flask import request
    category = request.args.get('category', 'all')
    if scrape_status.get('running'): return jsonify({'success': False, 'error': '正在執行中'})
    return jsonify(run_full_sync(category))

@app.route('/api/upload')
def api_upload():
    from flask import request
    jsonl_file = request.args.get('file', '')
    if not jsonl_file or not os.path.exists(jsonl_file): return jsonify({'error': 'JSONL 檔案不存在'})
    if scrape_status['running']: return jsonify({'error': '正在執行中'})
    threading.Thread(target=run_bulk_upload, args=(jsonl_file,)).start()
    return jsonify({'started': True, 'file': jsonl_file})

@app.route('/api/bulk_status')
def api_bulk_status():
    op_id = scrape_status.get('bulk_operation_id', '')
    return jsonify(check_bulk_operation_status(op_id if op_id else None))

@app.route('/api/bulk_results')
def api_bulk_results():
    return jsonify(get_bulk_operation_results())

@app.route('/api/test')
def api_test():
    load_shopify_token()
    return jsonify(graphql_request("{ shop { name } }"))

@app.route('/api/delete')
def api_delete():
    if scrape_status['running']: return jsonify({'error': '正在執行中'})
    threading.Thread(target=run_delete_workman_products).start()
    return jsonify({'started': True})

@app.route('/api/publish_all')
def api_publish_all():
    if scrape_status.get('running'): return jsonify({'error': '正在執行中'})
    def run_publish():
        global scrape_status
        scrape_status['running'] = True
        scrape_status['phase'] = 'publishing'
        try:
            results = batch_publish_workman_products()
            scrape_status['current_product'] = f"發布完成！成功: {results.get('success', 0)}, 失敗: {results.get('failed', 0)}"
        except Exception as e:
            scrape_status['errors'].append({'error': str(e)})
        finally:
            scrape_status['running'] = False
    threading.Thread(target=run_publish, daemon=False).start()
    return jsonify({'success': True, 'message': '已開始發布'})

@app.route('/api/publications')
def api_publications():
    try: return jsonify({'publications': get_all_publications()})
    except Exception as e: return jsonify({'error': str(e)})

@app.route('/api/count')
def api_count():
    try:
        load_shopify_token()
        result = graphql_request('{ productsCount(query: "vendor:WORKMAN") { count } }')
        return jsonify({'count': result.get('data', {}).get('productsCount', {}).get('count', 0)})
    except Exception as e: return jsonify({'error': str(e)})

@app.route('/api/test_workman')
def api_test_workman():
    results = {}
    try:
        r = requests.get(SOURCE_URL, headers=HEADERS, timeout=10)
        results['homepage'] = {'status': r.status_code, 'ok': r.status_code == 200}
    except Exception as e:
        results['homepage'] = {'error': str(e), 'ok': False}
    try:
        r = requests.get(SOURCE_URL + '/shop/c/c54/', headers=HEADERS, timeout=10)
        results['kids_page'] = {'status': r.status_code, 'ok': r.status_code == 200}
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            goods_links = [l for l in soup.find_all('a', href=True) if '/shop/g/' in l.get('href', '')]
            results['kids_page']['goods_links_found'] = len(goods_links)
            if goods_links: results['kids_page']['first_link'] = goods_links[0].get('href', '')
    except Exception as e:
        results['kids_page'] = {'error': str(e), 'ok': False}
    return jsonify(results)

@app.route('/api/test_product')
def api_test_product():
    from flask import request
    product_url = request.args.get('url', SOURCE_URL + '/shop/g/g2300022383210/')
    if not product_url.startswith('http'): product_url = SOURCE_URL + product_url
    results = {'url': product_url}
    try:
        r = requests.get(product_url, headers=HEADERS, timeout=15)
        results['status'] = r.status_code
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            te = soup.find('h1', class_='block-goods-name')
            results['title_found'] = te is not None
            if te: results['title'] = te.get_text(strip=True)[:50]
            pe = soup.find('p', class_='block-goods-price')
            results['price_elem_found'] = pe is not None
            if pe: results['price_text'] = pe.get_text(strip=True)
            cd = soup.find('dt', string='管理番号')
            results['manage_code_dt_found'] = cd is not None
            if cd:
                dd = cd.find_next_sibling('dd')
                if dd: results['manage_code'] = dd.get_text(strip=True)
    except Exception as e:
        results['error'] = str(e)
    return jsonify(results)

# ========== 庫存同步 API ==========

@app.route('/api/inventory_sync')
def api_inventory_sync():
    if inventory_sync_status.get('running'): return jsonify({'success': False, 'error': '庫存同步正在執行中'})
    threading.Thread(target=run_inventory_sync, daemon=False).start()
    return jsonify({'success': True, 'message': '已開始庫存同步', 'started_at': time.strftime('%Y-%m-%d %H:%M:%S')})

@app.route('/api/inventory_sync_status')
def api_inventory_sync_status():
    return jsonify(inventory_sync_status)

@app.route('/api/check_stock')
def api_check_stock():
    from flask import request
    url = request.args.get('url', '')
    if not url: return jsonify({'error': '請提供 url 參數'})
    return jsonify(check_workman_stock(url))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
