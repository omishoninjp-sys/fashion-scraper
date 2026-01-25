"""
WORKMAN 商品爬蟲 + Shopify Bulk Operations 上架工具
來源：workman.jp
功能：
1. 爬取 workman.jp 各分類商品
2. 翻譯並產生 JSONL 檔案
3. 使用 Shopify Bulk Operations API 批量上傳（數千商品/分鐘）
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
    'kids': {'url': '/shop/c/c54/', 'collection': 'WORKMAN 兒童服', 'tags': ['WORKMAN', '日本', '服飾', '兒童服', '童裝']}
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DEFAULT_WEIGHT = 0.5
JSONL_DIR = "/tmp/workman_jsonl"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en;q=0.9',
}

# 確保目錄存在
os.makedirs(JSONL_DIR, exist_ok=True)

scrape_status = {
    "running": False,
    "phase": "",  # "scraping" | "uploading"
    "progress": 0,
    "total": 0,
    "current_product": "",
    "products": [],
    "errors": [],
    "jsonl_file": "",
    "bulk_operation_id": "",
    "bulk_status": "",
}

# ========== 工具函數 ==========

def load_shopify_token():
    global SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN
    if not SHOPIFY_SHOP:
        SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP", "")
    if not SHOPIFY_ACCESS_TOKEN:
        SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")


def graphql_request(query, variables=None):
    """執行 GraphQL 請求"""
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


def calculate_selling_price(cost, weight):
    """計算售價"""
    shipping_cost = 800 + (weight * 400)
    base_price = cost + shipping_cost
    selling_price = base_price * 1.15
    return round(selling_price / 10) * 10


def contains_japanese(text):
    if not text:
        return False
    hiragana = re.search(r'[\u3040-\u309F]', text)
    katakana = re.search(r'[\u30A0-\u30FF]', text)
    return bool(hiragana or katakana)


def remove_japanese(text):
    if not text:
        return text
    cleaned = re.sub(r'[\u3040-\u309F\u30A0-\u30FF]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


# ========== 翻譯 ==========

def translate_with_chatgpt(title, description, size_spec=''):
    """翻譯商品資訊"""
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
4. 只回傳 JSON"""

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
                    {"role": "system", "content": "你是翻譯專家。輸出禁止任何日文。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0,
                "max_tokens": 1500
            },
            timeout=60
        )
        
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']
            content = content.strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content.rsplit('```', 1)[0]
            
            translated = json.loads(content.strip())
            
            trans_title = translated.get('title', title)
            trans_desc = translated.get('description', description)
            trans_size = translated.get('size_spec_translated', '')
            
            # 移除日文
            if contains_japanese(trans_title):
                trans_title = remove_japanese(trans_title)
            if contains_japanese(trans_desc):
                trans_desc = remove_japanese(trans_desc)
            
            if not trans_title.startswith('WORKMAN'):
                trans_title = f"WORKMAN {trans_title}"
            
            # 建立尺寸表 HTML
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
    """建立尺寸表 HTML"""
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
    """取得分類總頁數"""
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
            return 1
    except:
        pass
    return 1


def fetch_all_product_links(category_key):
    """取得分類內所有商品連結"""
    category = CATEGORIES[category_key]
    base_url = category['url']
    total_pages = get_total_pages(base_url)
    
    print(f"[INFO] {category['collection']} 共 {total_pages} 頁")
    
    all_links = []
    for page in range(1, total_pages + 1):
        if page == 1:
            page_url = SOURCE_URL + base_url
        else:
            page_url = SOURCE_URL + base_url.replace('/', f'_p{page}/', 1)
        
        try:
            response = requests.get(page_url, headers=HEADERS, timeout=30)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                product_links = soup.find_all('a', class_='block-link')
                for link in product_links:
                    href = link.get('href', '')
                    if '/shop/g/' in href:
                        full_url = SOURCE_URL + href if href.startswith('/') else href
                        if full_url not in all_links:
                            all_links.append(full_url)
        except Exception as e:
            print(f"[ERROR] 頁面 {page} 載入失敗: {e}")
        
        time.sleep(0.3)
    
    print(f"[INFO] {category['collection']} 共 {len(all_links)} 個商品")
    return all_links


def parse_product_page(url):
    """解析商品頁面"""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 標題
        title_elem = soup.find('h1', class_='block-goods-name')
        title = title_elem.get_text(strip=True) if title_elem else ''
        
        # 價格
        price = 0
        price_elem = soup.find('p', class_='block-goods-price')
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            match = re.search(r'[\d,]+', price_text)
            if match:
                price = int(match.group().replace(',', ''))
        
        # 管理番號
        manage_code = ''
        code_dt = soup.find('dt', string='管理番号')
        if code_dt:
            code_dd = code_dt.find_next_sibling('dd')
            if code_dd:
                manage_code = code_dd.get_text(strip=True)
        
        if not manage_code or price < 1000:
            return None
        
        # 商品說明
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
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        row_text = ' | '.join([c.get_text(strip=True) for c in cells])
                        size_spec += row_text + '\n'
        
        # 顏色和圖片
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
        
        # 尺寸
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
        
        return {
            'url': url,
            'title': title,
            'price': price,
            'manage_code': manage_code,
            'description': description,
            'size_spec': size_spec,
            'colors': colors,
            'sizes': sizes,
            'images': images
        }
        
    except Exception as e:
        print(f"[ERROR] 解析失敗 {url}: {e}")
        return None


def product_to_jsonl_entry(product_data, tags):
    """將商品資料轉換為 JSONL 格式（Shopify GraphQL ProductInput）"""
    
    # 翻譯
    translated = translate_with_chatgpt(
        product_data['title'],
        product_data['description'],
        product_data.get('size_spec', '')
    )
    
    title = translated['title']
    description = translated['description']
    manage_code = product_data['manage_code']
    cost = product_data['price']
    colors = product_data['colors']
    sizes = product_data['sizes']
    images = product_data['images']
    
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)
    
    # 建立 variants
    variants = []
    
    if len(colors) > 1 and len(sizes) > 1:
        # 顏色 × 尺寸
        for color in colors:
            for size in sizes:
                variants.append({
                    "price": str(selling_price),
                    "sku": f"{manage_code}-{color}-{size}",
                    "inventoryPolicy": "CONTINUE",
                    "inventoryManagement": None,
                    "weight": DEFAULT_WEIGHT,
                    "weightUnit": "KILOGRAMS",
                    "options": [color, size]
                })
    elif len(colors) > 1:
        for color in colors:
            variants.append({
                "price": str(selling_price),
                "sku": f"{manage_code}-{color}",
                "inventoryPolicy": "CONTINUE",
                "inventoryManagement": None,
                "weight": DEFAULT_WEIGHT,
                "weightUnit": "KILOGRAMS",
                "options": [color]
            })
    elif len(sizes) > 1:
        for size in sizes:
            variants.append({
                "price": str(selling_price),
                "sku": f"{manage_code}-{size}",
                "inventoryPolicy": "CONTINUE",
                "inventoryManagement": None,
                "weight": DEFAULT_WEIGHT,
                "weightUnit": "KILOGRAMS",
                "options": [size]
            })
    else:
        variants.append({
            "price": str(selling_price),
            "sku": manage_code,
            "inventoryPolicy": "CONTINUE",
            "inventoryManagement": None,
            "weight": DEFAULT_WEIGHT,
            "weightUnit": "KILOGRAMS",
        })
    
    # 建立 options
    options = []
    if len(colors) > 1 or (len(colors) == 1 and colors[0] != '標準'):
        options.append("顏色")
    if len(sizes) > 1 or (len(sizes) == 1 and sizes[0] != 'FREE'):
        options.append("尺寸")
    
    # 建立 images（使用原始 URL，Shopify 會自動抓取）
    image_inputs = []
    for img_url in images:
        image_inputs.append({"src": img_url})
    
    # ProductInput 結構
    product_input = {
        "title": title,
        "descriptionHtml": description,
        "vendor": "WORKMAN",
        "productType": "",
        "status": "ACTIVE",
        "handle": f"workman-{manage_code}",
        "tags": tags,
    }
    
    if options:
        product_input["options"] = options
    
    if variants:
        product_input["variants"] = variants
    
    if image_inputs:
        product_input["images"] = image_inputs
    
    # Metafield for source URL
    product_input["metafields"] = [
        {
            "namespace": "custom",
            "key": "link",
            "value": product_data['url'],
            "type": "url"
        }
    ]
    
    return {"input": product_input}


# ========== Bulk Operations ==========

def create_staged_upload():
    """建立 Staged Upload URL"""
    query = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters {
            name
            value
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    variables = {
        "input": [{
            "resource": "BULK_MUTATION_VARIABLES",
            "filename": "products.jsonl",
            "mimeType": "text/jsonl",
            "httpMethod": "POST"
        }]
    }
    
    result = graphql_request(query, variables)
    
    if 'errors' in result:
        print(f"[Staged Upload Error] {result['errors']}")
        return None
    
    targets = result.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    if targets:
        return targets[0]
    return None


def upload_jsonl_to_staged(staged_target, jsonl_path):
    """上傳 JSONL 到 Staged URL"""
    url = staged_target['url']
    params = {p['name']: p['value'] for p in staged_target['parameters']}
    
    with open(jsonl_path, 'rb') as f:
        files = {'file': ('products.jsonl', f, 'text/jsonl')}
        response = requests.post(url, data=params, files=files, timeout=300)
    
    return response.status_code in [200, 201, 204]


def run_bulk_mutation(staged_upload_path):
    """執行 Bulk Mutation"""
    query = """
    mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) {
      bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) {
        bulkOperation {
          id
          status
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    # productCreate mutation
    mutation = """
    mutation call($input: ProductInput!) {
      productCreate(input: $input) {
        product {
          id
          title
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    variables = {
        "mutation": mutation,
        "stagedUploadPath": staged_upload_path
    }
    
    result = graphql_request(query, variables)
    return result


def check_bulk_operation_status(operation_id=None):
    """檢查 Bulk Operation 狀態"""
    if operation_id:
        query = """
        query($id: ID!) {
          node(id: $id) {
            ... on BulkOperation {
              id
              status
              errorCode
              createdAt
              completedAt
              objectCount
              fileSize
              url
              partialDataUrl
            }
          }
        }
        """
        result = graphql_request(query, {"id": operation_id})
        return result.get('data', {}).get('node', {})
    else:
        # 取得最新的 bulk operation
        query = """
        {
          currentBulkOperation(type: MUTATION) {
            id
            status
            errorCode
            createdAt
            completedAt
            objectCount
            fileSize
            url
          }
        }
        """
        result = graphql_request(query)
        return result.get('data', {}).get('currentBulkOperation', {})


# ========== 批量刪除功能 ==========

def fetch_workman_product_ids():
    """取得所有 WORKMAN 商品的 ID（使用分頁查詢）"""
    all_ids = []
    cursor = None
    
    while True:
        if cursor:
            query = """
            query($cursor: String) {
              products(first: 250, after: $cursor, query: "vendor:WORKMAN") {
                edges {
                  node {
                    id
                    title
                    handle
                  }
                  cursor
                }
                pageInfo {
                  hasNextPage
                }
              }
            }
            """
            result = graphql_request(query, {"cursor": cursor})
        else:
            query = """
            {
              products(first: 250, query: "vendor:WORKMAN") {
                edges {
                  node {
                    id
                    title
                    handle
                  }
                  cursor
                }
                pageInfo {
                  hasNextPage
                }
              }
            }
            """
            result = graphql_request(query)
        
        products = result.get('data', {}).get('products', {})
        edges = products.get('edges', [])
        
        for edge in edges:
            node = edge['node']
            all_ids.append({
                'id': node['id'],
                'title': node['title'],
                'handle': node['handle']
            })
            cursor = edge['cursor']
        
        if not products.get('pageInfo', {}).get('hasNextPage', False):
            break
        
        time.sleep(0.5)  # 避免速率限制
    
    print(f"[INFO] 找到 {len(all_ids)} 個 WORKMAN 商品")
    return all_ids


def create_delete_jsonl(product_ids):
    """產生刪除用的 JSONL 檔案"""
    jsonl_filename = f"delete_workman_{int(time.time())}.jsonl"
    jsonl_path = os.path.join(JSONL_DIR, jsonl_filename)
    
    with open(jsonl_path, 'w', encoding='utf-8') as f:
        for product in product_ids:
            # productDelete 的 input 格式
            entry = {"input": {"id": product['id']}}
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    
    print(f"[INFO] 刪除 JSONL 已產生: {jsonl_path} ({len(product_ids)} 個商品)")
    return jsonl_path


def run_bulk_delete_mutation(staged_upload_path):
    """執行 Bulk Delete Mutation"""
    query = """
    mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) {
      bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) {
        bulkOperation {
          id
          status
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    # productDelete mutation
    mutation = """
    mutation call($input: ProductDeleteInput!) {
      productDelete(input: $input) {
        deletedProductId
        userErrors {
          field
          message
        }
      }
    }
    """
    
    variables = {
        "mutation": mutation,
        "stagedUploadPath": staged_upload_path
    }
    
    result = graphql_request(query, variables)
    return result


def run_delete_workman_products():
    """執行批量刪除 WORKMAN 商品"""
    global scrape_status
    
    scrape_status = {
        "running": True,
        "phase": "deleting",
        "progress": 0,
        "total": 0,
        "current_product": "正在查詢 WORKMAN 商品...",
        "products": [],
        "errors": [],
        "jsonl_file": "",
        "bulk_operation_id": "",
        "bulk_status": "",
    }
    
    try:
        # 1. 查詢所有 WORKMAN 商品
        print("[Delete] 查詢 WORKMAN 商品...")
        product_ids = fetch_workman_product_ids()
        
        if not product_ids:
            scrape_status['current_product'] = '沒有找到 WORKMAN 商品'
            scrape_status['running'] = False
            return
        
        scrape_status['total'] = len(product_ids)
        scrape_status['current_product'] = f'找到 {len(product_ids)} 個商品，準備刪除...'
        
        # 記錄要刪除的商品
        for p in product_ids[:20]:  # 只顯示前 20 個
            scrape_status['products'].append({
                'title': p['title'],
                'handle': p['handle'],
                'variants': 0
            })
        
        # 2. 產生刪除 JSONL
        print("[Delete] 產生刪除 JSONL...")
        jsonl_path = create_delete_jsonl(product_ids)
        scrape_status['jsonl_file'] = jsonl_path
        
        # 3. 建立 Staged Upload
        print("[Delete] 建立 Staged Upload...")
        scrape_status['current_product'] = '上傳刪除清單...'
        staged = create_staged_upload()
        
        if not staged:
            scrape_status['errors'].append({'error': '建立 Staged Upload 失敗'})
            scrape_status['running'] = False
            return
        
        # 4. 上傳 JSONL
        print("[Delete] 上傳 JSONL...")
        if not upload_jsonl_to_staged(staged, jsonl_path):
            scrape_status['errors'].append({'error': '上傳 JSONL 失敗'})
            scrape_status['running'] = False
            return
        
        # 5. 執行 Bulk Delete
        print("[Delete] 執行批量刪除...")
        scrape_status['current_product'] = '執行批量刪除...'
        
        staged_path = None
        for param in staged['parameters']:
            if param['name'] == 'key':
                staged_path = param['value']
                break
        
        if not staged_path:
            staged_path = staged.get('resourceUrl', '')
        
        result = run_bulk_delete_mutation(staged_path)
        
        if 'errors' in result:
            scrape_status['errors'].append({'error': str(result['errors'])})
            scrape_status['running'] = False
            return
        
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        
        if user_errors:
            scrape_status['errors'].append({'error': str(user_errors)})
            scrape_status['running'] = False
            return
        
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        scrape_status['bulk_status'] = bulk_op.get('status', '')
        scrape_status['current_product'] = f"批量刪除已啟動！正在刪除 {len(product_ids)} 個商品..."
        
        print(f"[Delete] 操作 ID: {bulk_op.get('id')}, 狀態: {bulk_op.get('status')}")
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        print(f"[ERROR] {e}")
    finally:
        scrape_status['running'] = False


# ========== 主流程 ==========

def run_scrape(category):
    """執行爬取，產生 JSONL 檔案"""
    global scrape_status
    
    scrape_status = {
        "running": True,
        "phase": "scraping",
        "progress": 0,
        "total": 0,
        "current_product": "",
        "products": [],
        "errors": [],
        "jsonl_file": "",
        "bulk_operation_id": "",
        "bulk_status": "",
    }
    
    try:
        categories_to_scrape = []
        if category == 'all':
            categories_to_scrape = ['work', 'mens', 'womens', 'kids']
        elif category in CATEGORIES:
            categories_to_scrape = [category]
        else:
            scrape_status['errors'].append({'error': f'未知分類: {category}'})
            scrape_status['running'] = False
            return
        
        all_jsonl_entries = []
        
        for cat_key in categories_to_scrape:
            cat_info = CATEGORIES[cat_key]
            tags = cat_info['tags']
            
            scrape_status['current_product'] = f"正在取得 {cat_info['collection']} 商品連結..."
            product_links = fetch_all_product_links(cat_key)
            scrape_status['total'] += len(product_links)
            
            for idx, link in enumerate(product_links):
                scrape_status['progress'] += 1
                scrape_status['current_product'] = f"處理中: {link[-30:]}"
                
                product_data = parse_product_page(link)
                
                if not product_data:
                    scrape_status['errors'].append({'url': link, 'error': '解析失敗'})
                    continue
                
                try:
                    print(f"[翻譯] {product_data['title'][:30]}...")
                    entry = product_to_jsonl_entry(product_data, tags)
                    all_jsonl_entries.append(entry)
                    
                    scrape_status['products'].append({
                        'title': entry['input']['title'],
                        'handle': entry['input']['handle'],
                        'variants': len(entry['input'].get('variants', []))
                    })
                    print(f"[OK] {entry['input']['title'][:30]}")
                except Exception as e:
                    print(f"[ERROR] {product_data['title'][:20]}: {e}")
                    scrape_status['errors'].append({'url': link, 'error': str(e)})
                
                time.sleep(0.5)  # 避免翻譯 API 過載
        
        # 寫入 JSONL 檔案
        if all_jsonl_entries:
            jsonl_filename = f"workman_{category}_{int(time.time())}.jsonl"
            jsonl_path = os.path.join(JSONL_DIR, jsonl_filename)
            
            with open(jsonl_path, 'w', encoding='utf-8') as f:
                for entry in all_jsonl_entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            
            scrape_status['jsonl_file'] = jsonl_path
            print(f"[完成] JSONL 檔案已產生: {jsonl_path} ({len(all_jsonl_entries)} 個商品)")
        
        scrape_status['current_product'] = f"完成！共 {len(all_jsonl_entries)} 個商品"
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        print(f"[ERROR] {e}")
    finally:
        scrape_status['running'] = False
        scrape_status['phase'] = "completed"


def run_bulk_upload(jsonl_path):
    """執行 Bulk Upload"""
    global scrape_status
    
    scrape_status['phase'] = 'uploading'
    scrape_status['running'] = True
    scrape_status['current_product'] = '正在準備上傳...'
    
    try:
        # 1. 建立 Staged Upload
        print("[Bulk] 建立 Staged Upload...")
        scrape_status['current_product'] = '建立上傳連結...'
        staged = create_staged_upload()
        
        if not staged:
            scrape_status['errors'].append({'error': '建立 Staged Upload 失敗'})
            return
        
        # 2. 上傳 JSONL
        print("[Bulk] 上傳 JSONL 檔案...")
        scrape_status['current_product'] = '上傳 JSONL 檔案...'
        
        if not upload_jsonl_to_staged(staged, jsonl_path):
            scrape_status['errors'].append({'error': '上傳 JSONL 失敗'})
            return
        
        # 3. 執行 Bulk Mutation
        print("[Bulk] 執行批量建立...")
        scrape_status['current_product'] = '執行批量建立...'
        
        # 找到 key 參數作為 stagedUploadPath
        staged_path = None
        for param in staged['parameters']:
            if param['name'] == 'key':
                staged_path = param['value']
                break
        
        if not staged_path:
            staged_path = staged.get('resourceUrl', '')
        
        print(f"[Bulk] Staged path: {staged_path}")
        result = run_bulk_mutation(staged_path)
        
        if 'errors' in result:
            scrape_status['errors'].append({'error': str(result['errors'])})
            return
        
        bulk_op = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        user_errors = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        
        if user_errors:
            scrape_status['errors'].append({'error': str(user_errors)})
            return
        
        scrape_status['bulk_operation_id'] = bulk_op.get('id', '')
        scrape_status['bulk_status'] = bulk_op.get('status', '')
        scrape_status['current_product'] = f"批量操作已啟動: {bulk_op.get('status', '')}"
        
        print(f"[Bulk] 操作 ID: {bulk_op.get('id')}, 狀態: {bulk_op.get('status')}")
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        print(f"[ERROR] {e}")
    finally:
        scrape_status['running'] = False


# ========== Flask 路由 ==========

@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>WORKMAN 爬蟲 (Bulk Operations)</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #d32f2f; }
        .card { background: white; border-radius: 12px; padding: 20px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .btn { padding: 12px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; margin: 5px; transition: all 0.2s; }
        .btn:hover { transform: translateY(-2px); }
        .btn-work { background: #1976d2; color: white; }
        .btn-mens { background: #388e3c; color: white; }
        .btn-womens { background: #d81b60; color: white; }
        .btn-kids { background: #f57c00; color: white; }
        .btn-all { background: #7b1fa2; color: white; }
        .btn-upload { background: #d32f2f; color: white; font-size: 18px; padding: 15px 30px; }
        .btn-check { background: #455a64; color: white; }
        .btn-delete { background: #b71c1c; color: white; }
        .btn:disabled { background: #ccc; cursor: not-allowed; transform: none; }
        #status { padding: 15px; background: #e3f2fd; border-radius: 8px; margin: 15px 0; }
        #log { height: 300px; overflow-y: auto; background: #263238; color: #aed581; padding: 15px; border-radius: 8px; font-family: monospace; font-size: 13px; }
        .progress { height: 8px; background: #e0e0e0; border-radius: 4px; margin: 10px 0; }
        .progress-bar { height: 100%; background: linear-gradient(90deg, #4caf50, #8bc34a); border-radius: 4px; transition: width 0.3s; }
        .progress-bar-delete { background: linear-gradient(90deg, #f44336, #ff5722); }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f5f5f5; }
        .phase { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }
        .phase-scraping { background: #fff3e0; color: #e65100; }
        .phase-uploading { background: #e3f2fd; color: #1565c0; }
        .phase-deleting { background: #ffebee; color: #c62828; }
        .phase-completed { background: #e8f5e9; color: #2e7d32; }
        .warning-box { background: #fff3e0; border: 2px solid #ff9800; border-radius: 8px; padding: 15px; margin: 10px 0; }
    </style>
</head>
<body>
    <h1>🏭 WORKMAN 爬蟲 (Bulk Operations 版)</h1>
    
    <div class="card">
        <h3>📥 第一步：爬取商品 → 產生 JSONL</h3>
        <p>選擇分類開始爬取，完成後會產生 JSONL 檔案</p>
        <button class="btn btn-work" onclick="startScrape('work')">🔧 作業服</button>
        <button class="btn btn-mens" onclick="startScrape('mens')">👔 男裝</button>
        <button class="btn btn-womens" onclick="startScrape('womens')">👗 女裝</button>
        <button class="btn btn-kids" onclick="startScrape('kids')">👶 兒童服</button>
        <button class="btn btn-all" onclick="startScrape('all')">🚀 全部</button>
    </div>
    
    <div class="card">
        <h3>📤 第二步：批量上傳到 Shopify</h3>
        <p>爬取完成後，點擊下方按鈕批量上傳（數千商品只需幾分鐘）</p>
        <button class="btn btn-upload" id="uploadBtn" onclick="startUpload()" disabled>📤 批量上傳到 Shopify</button>
        <button class="btn btn-check" onclick="checkStatus()">🔍 檢查上傳狀態</button>
    </div>
    
    <div class="card">
        <h3>🗑️ 批量刪除 WORKMAN 商品</h3>
        <div class="warning-box">
            ⚠️ <strong>警告：此操作會刪除 Shopify 中所有 vendor 為 "WORKMAN" 的商品！</strong>
        </div>
        <button class="btn btn-delete" onclick="startDelete()">🗑️ 刪除所有 WORKMAN 商品</button>
        <button class="btn btn-check" onclick="countProducts()">📊 查詢商品數量</button>
    </div>
    
    <div class="card">
        <h3>📊 執行狀態</h3>
        <div id="status">等待開始...</div>
        <div class="progress"><div class="progress-bar" id="progressBar" style="width:0%"></div></div>
    </div>
    
    <div class="card">
        <h3>📋 執行記錄</h3>
        <div id="log"></div>
    </div>
    
    <script>
        let currentJsonlFile = '';
        
        function startScrape(category) {
            if (!confirm(`確定要爬取 ${category} 分類？`)) return;
            
            // 重置狀態
            resetTracking();
            document.getElementById('uploadBtn').disabled = true;
            document.getElementById('log').innerHTML = '';
            
            fetch('/api/start?category=' + category)
                .then(r => r.json())
                .then(data => {
                    log('🚀 開始爬取: ' + category);
                    pollStatus();
                });
        }
        
        function startUpload() {
            if (!currentJsonlFile) {
                alert('請先完成爬取！');
                return;
            }
            if (!confirm('確定要批量上傳到 Shopify？')) return;
            
            resetTracking();
            
            fetch('/api/upload?file=' + encodeURIComponent(currentJsonlFile))
                .then(r => r.json())
                .then(data => {
                    log('🚀 開始批量上傳...');
                    pollStatus();
                });
        }
        
        function checkStatus() {
            fetch('/api/bulk_status')
                .then(r => r.json())
                .then(data => {
                    let status = data.status || 'UNKNOWN';
                    let count = data.objectCount || 0;
                    log(`📊 Bulk 狀態: ${status}, 處理數: ${count}`);
                    if (data.errorCode) {
                        log(`❌ 錯誤碼: ${data.errorCode}`);
                    }
                });
        }
        
        function startDelete() {
            if (!confirm('⚠️ 警告！\\n\\n此操作會刪除 Shopify 中所有 WORKMAN 商品！\\n\\n確定要繼續嗎？')) return;
            if (!confirm('再次確認：真的要刪除所有 WORKMAN 商品嗎？')) return;
            
            resetTracking();
            document.getElementById('log').innerHTML = '';
            
            fetch('/api/delete')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log('❌ 錯誤: ' + data.error);
                    } else {
                        log('🗑️ 開始批量刪除...');
                        pollStatus();
                    }
                });
        }
        
        function countProducts() {
            log('📊 正在查詢 WORKMAN 商品數量...');
            fetch('/api/count')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log('❌ 錯誤: ' + data.error);
                    } else {
                        log(`📊 目前有 ${data.count} 個 WORKMAN 商品`);
                    }
                });
        }
        
        function resetTracking() {
            lastProductCount = 0;
            lastProgress = 0;
            lastPhase = '';
        }
        
        function pollStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    updateUI(data);
                    if (data.running) {
                        setTimeout(pollStatus, 1000);  // 1 秒更新一次
                    }
                });
        }
        
        let lastProductCount = 0;
        let lastProgress = 0;
        let lastPhase = '';
        
        function updateUI(data) {
            let phaseClass = 'phase-' + data.phase;
            let phaseText = {scraping: '爬取中', uploading: '上傳中', deleting: '刪除中', completed: '完成'}[data.phase] || data.phase;
            
            // 階段變化時記錄
            if (data.phase !== lastPhase) {
                if (data.phase === 'scraping') log('📥 開始爬取商品...');
                else if (data.phase === 'uploading') log('📤 開始上傳到 Shopify...');
                else if (data.phase === 'deleting') log('🗑️ 開始刪除商品...');
                else if (data.phase === 'completed') log('✅ 作業完成！');
                lastPhase = data.phase;
            }
            
            let statusHtml = `<span class="phase ${phaseClass}">${phaseText}</span> `;
            statusHtml += data.current_product || '';
            
            if (data.total > 0) {
                statusHtml += `<br>進度: ${data.progress} / ${data.total}`;
                let pct = (data.progress / data.total * 100).toFixed(1);
                statusHtml += ` (${pct}%)`;
            }
            if (data.jsonl_file) {
                statusHtml += `<br>📄 JSONL: ${data.jsonl_file.split('/').pop()}`;
                currentJsonlFile = data.jsonl_file;
                document.getElementById('uploadBtn').disabled = false;
            }
            if (data.bulk_operation_id) {
                statusHtml += `<br>🔄 Bulk ID: ${data.bulk_operation_id.split('/').pop()}`;
                statusHtml += `<br>📊 狀態: ${data.bulk_status}`;
            }
            if (data.errors.length > 0) {
                statusHtml += `<br>⚠️ 錯誤: ${data.errors.length} 個`;
            }
            
            document.getElementById('status').innerHTML = statusHtml;
            
            let pct = data.total > 0 ? (data.progress / data.total * 100) : 0;
            document.getElementById('progressBar').style.width = pct + '%';
            
            // 進度變化時記錄
            if (data.progress > lastProgress && data.progress % 10 === 0) {
                log(`📊 進度: ${data.progress} / ${data.total}`);
            }
            lastProgress = data.progress;
            
            // 新商品時記錄
            if (data.products.length > lastProductCount) {
                let newProducts = data.products.slice(lastProductCount);
                for (let p of newProducts) {
                    log(`✓ ${p.title}`);
                }
                lastProductCount = data.products.length;
            }
            
            // 錯誤記錄
            if (data.errors.length > 0) {
                let lastError = data.errors[data.errors.length - 1];
                if (lastError.url) {
                    log(`❌ 失敗: ${lastError.url.split('/').pop()}`);
                }
            }
        }
        
        function log(msg) {
            let logDiv = document.getElementById('log');
            let time = new Date().toLocaleTimeString();
            // 避免重複訊息
            if (!logDiv.innerHTML.includes(msg)) {
                logDiv.innerHTML = `[${time}] ${msg}\n` + logDiv.innerHTML;
            }
        }
        
        // 初始載入狀態
        pollStatus();
    </script>
</body>
</html>'''


@app.route('/api/status')
def api_status():
    return jsonify(scrape_status)


@app.route('/api/start')
def api_start():
    from flask import request
    category = request.args.get('category', 'mens')
    
    if scrape_status['running']:
        return jsonify({'error': '正在執行中'})
    
    thread = threading.Thread(target=run_scrape, args=(category,))
    thread.start()
    
    return jsonify({'started': True, 'category': category})


@app.route('/api/upload')
def api_upload():
    from flask import request
    jsonl_file = request.args.get('file', '')
    
    if not jsonl_file or not os.path.exists(jsonl_file):
        return jsonify({'error': 'JSONL 檔案不存在'})
    
    if scrape_status['running']:
        return jsonify({'error': '正在執行中'})
    
    thread = threading.Thread(target=run_bulk_upload, args=(jsonl_file,))
    thread.start()
    
    return jsonify({'started': True, 'file': jsonl_file})


@app.route('/api/bulk_status')
def api_bulk_status():
    op_id = scrape_status.get('bulk_operation_id', '')
    status = check_bulk_operation_status(op_id if op_id else None)
    return jsonify(status)


@app.route('/api/test')
def api_test():
    """測試 Shopify 連線"""
    load_shopify_token()
    result = graphql_request("{ shop { name } }")
    return jsonify(result)


@app.route('/api/delete')
def api_delete():
    """批量刪除所有 WORKMAN 商品"""
    if scrape_status['running']:
        return jsonify({'error': '正在執行中'})
    
    thread = threading.Thread(target=run_delete_workman_products)
    thread.start()
    
    return jsonify({'started': True})


@app.route('/api/count')
def api_count():
    """查詢 WORKMAN 商品數量"""
    try:
        load_shopify_token()
        query = """
        {
          productsCount(query: "vendor:WORKMAN") {
            count
          }
        }
        """
        result = graphql_request(query)
        count = result.get('data', {}).get('productsCount', {}).get('count', 0)
        return jsonify({'count': count})
    except Exception as e:
        return jsonify({'error': str(e)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
