"""
BAPE 商品爬蟲 + Shopify 上架工具
來源：jp.bape.com
功能：
1. 從 jp.bape.com Shopify JSON API 爬取所有商品
2. 完整複製 Variants（顏色、尺寸等選項）
3. 圖片對應 Variant
4. 每個 Variant 獨立計算售價
5. 無庫存商品不上架，已上架但無庫存的設為草稿
6. 價格同步：已存在商品若價格變動則自動更新
7. Collection 建立後發布到所有 channels
"""

from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup
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

SOURCE_URL = "https://jp.bape.com"
PRODUCTS_JSON_URL = "https://jp.bape.com/collections/all/products.json"

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
    "out_of_stock": 0,
    "set_to_draft": 0,
    "price_updated": 0
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


def contains_japanese(text):
    """檢測文字是否包含日文（平假名、片假名）"""
    if not text:
        return False
    import re
    # 平假名: \u3040-\u309F, 片假名: \u30A0-\u30FF
    japanese_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
    return bool(japanese_pattern.search(text))


def remove_japanese(text):
    """移除文字中的日文字元"""
    if not text:
        return text
    import re
    # 移除平假名、片假名
    cleaned = re.sub(r'[\u3040-\u309F\u30A0-\u30FF]+', '', text)
    # 清理多餘空格
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # 清理多餘的標點
    cleaned = re.sub(r'[（）\(\)]\s*[（）\(\)]', '', cleaned)
    cleaned = re.sub(r'\s*[/／]\s*$', '', cleaned)
    cleaned = re.sub(r'^\s*[/／]\s*', '', cleaned)
    return cleaned


def translate_with_chatgpt(title, description, size_spec=''):
    # 準備尺寸規格文字
    size_spec_section = ''
    if size_spec:
        size_spec_section = f"\n尺寸規格表：\n{size_spec}"
    
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文/英文）：{title}
商品說明：{description[:1500] if description else ''}{size_spec_section}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文或英文，簡潔有力，前面加上 BAPE）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示，每個重點用 <br> 換行）",
    "size_spec_translated": "翻譯後的尺寸規格（如果有的話，把日文欄位名稱翻譯成中文，例如：サイズ→尺寸、着丈→衣長、身幅→身寬、肩幅→肩寬、袖丈→袖長，格式保持：列1|列2|列3...，每行用換行分隔）",
    "page_title": "SEO 頁面標題（繁體中文，包含品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

【最重要規則 - 絕對禁止日文】：
- 禁止出現任何平假名（あいうえお等）
- 禁止出現任何片假名（アイウエオ等）
- 如果原文有日文，必須翻譯成繁體中文
- 如果無法翻譯，直接省略該部分
- 違反此規則是嚴重錯誤

其他規則：
1. 這是日本潮流品牌 A BATHING APE (BAPE) 的商品
2. 商品名稱如果是英文可以保留英文，但開頭必須是「BAPE」
3. 翻譯要自然流暢，不要生硬
4. SEO 內容要包含：BAPE、A BATHING APE、日本、潮流、服飾等關鍵字
5. description 中每個重點用 <br> 換行，方便閱讀
6. 只回傳 JSON，不要其他文字"""

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
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。【最高優先規則】你的輸出絕對禁止出現任何日文字元（平假名、片假名）。所有內容必須是繁體中文或英文。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0,
                "max_tokens": 1500
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
            trans_desc = translated.get('description', description)
            trans_size_spec = translated.get('size_spec_translated', '')
            trans_page_title = translated.get('page_title', '')
            trans_meta_desc = translated.get('meta_description', '')
            
            # 檢查並移除日文
            if contains_japanese(trans_title):
                print(f"[警告] 標題包含日文，正在移除: {trans_title}")
                trans_title = remove_japanese(trans_title)
            if contains_japanese(trans_desc):
                print(f"[警告] 描述包含日文，正在移除")
                trans_desc = remove_japanese(trans_desc)
            if contains_japanese(trans_size_spec):
                print(f"[警告] 尺寸規格包含日文，正在移除")
                trans_size_spec = remove_japanese(trans_size_spec)
            if contains_japanese(trans_page_title):
                trans_page_title = remove_japanese(trans_page_title)
            if contains_japanese(trans_meta_desc):
                trans_meta_desc = remove_japanese(trans_meta_desc)
            
            if not trans_title.startswith('BAPE'):
                trans_title = f"BAPE {trans_title}"
            
            # 建立尺寸表 HTML
            size_spec_html = ''
            if trans_size_spec:
                size_spec_html = build_size_table_html(trans_size_spec)
            
            return {
                'success': True,
                'title': trans_title,
                'description': trans_desc,
                'size_spec_html': size_spec_html,
                'page_title': trans_page_title,
                'meta_description': trans_meta_desc
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"BAPE {title}",
                'description': description,
                'size_spec_html': '',
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"BAPE {title}",
            'description': description,
            'size_spec_html': '',
            'page_title': '',
            'meta_description': ''
        }


def build_size_table_html(size_spec_text):
    """將翻譯後的尺寸規格文字轉換成 HTML 表格"""
    if not size_spec_text:
        return ''
    
    lines = [line.strip() for line in size_spec_text.strip().split('\n') if line.strip()]
    if not lines:
        return ''
    
    html = '<div class="size-spec"><h3>📏 尺寸規格</h3>'
    html += '<table style="border-collapse: collapse; width: 100%; margin: 10px 0;">'
    
    for i, line in enumerate(lines):
        cells = [cell.strip() for cell in line.split('|')]
        if i == 0:
            # 第一行是標題
            html += '<tr style="background-color: #f5f5f5;">'
            for cell in cells:
                html += f'<th style="border: 1px solid #ddd; padding: 8px; text-align: center;">{cell}</th>'
            html += '</tr>'
        else:
            html += '<tr>'
            for j, cell in enumerate(cells):
                if j == 0:
                    # 第一列是標題
                    html += f'<td style="border: 1px solid #ddd; padding: 8px; font-weight: bold; background-color: #fafafa;">{cell}</td>'
                else:
                    html += f'<td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{cell}</td>'
            html += '</tr>'
    
    html += '</table>'
    html += '<p style="font-size: 12px; color: #666;">※ 單位為 cm，尺寸可能因商品而有些許誤差</p>'
    html += '</div>'
    
    return html


def download_image_to_base64(img_url, max_retries=3):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': SOURCE_URL + '/',
    }
    
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


def get_collection_products_with_details(collection_id):
    """取得 Collection 內的商品（包含 variants 詳細資訊，用於價格比對）"""
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
                variants_info = []
                for v in product.get('variants', []):
                    variant_id = v.get('id')
                    cost = None
                    
                    # 額外取得 variant 的 cost（collection products API 不含 cost）
                    variant_response = requests.get(
                        shopify_api_url(f"variants/{variant_id}.json"),
                        headers=get_shopify_headers()
                    )
                    if variant_response.status_code == 200:
                        variant_data = variant_response.json().get('variant', {})
                        cost = variant_data.get('cost')
                    time.sleep(0.1)  # 避免 API 限制
                    
                    variants_info.append({
                        'variant_id': variant_id,
                        'price': v.get('price'),
                        'cost': cost,
                        'sku': v.get('sku'),
                        'option1': v.get('option1'),
                        'option2': v.get('option2'),
                        'option3': v.get('option3'),
                    })
                products_map[handle] = {
                    'product_id': product_id,
                    'variants': variants_info
                }
        
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


def publish_collection_to_all_channels(collection_id):
    """發布 Collection 到所有銷售渠道"""
    print(f"[發布] 正在發布 Collection {collection_id} 到所有渠道...")
    
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
        print(f"[發布] 無法取得渠道列表: {response.status_code}")
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
    
    print(f"[發布] 找到 {len(unique_publications)} 個銷售渠道")
    
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
        "id": f"gid://shopify/Collection/{collection_id}",
        "input": publication_inputs
    }
    
    pub_response = requests.post(graphql_url, headers=headers, json={
        'query': mutation,
        'variables': variables
    })
    
    if pub_response.status_code == 200:
        print(f"[發布] Collection 已發布到所有渠道")
        return True
    else:
        print(f"[發布] 發布失敗: {pub_response.text}")
        return False


def get_or_create_collection(collection_title="BAPE"):
    response = requests.get(
        shopify_api_url(f'custom_collections.json?title={collection_title}'),
        headers=get_shopify_headers()
    )
    
    if response.status_code == 200:
        collections = response.json().get('custom_collections', [])
        for col in collections:
            if col['title'] == collection_title:
                print(f"[INFO] 找到現有 Collection: {collection_title} (ID: {col['id']})")
                publish_collection_to_all_channels(col['id'])
                return col['id']
    
    response = requests.post(
        shopify_api_url('custom_collections.json'),
        headers=get_shopify_headers(),
        json={'custom_collection': {'title': collection_title, 'published': True}}
    )
    
    if response.status_code == 201:
        collection_id = response.json()['custom_collection']['id']
        print(f"[INFO] 建立新 Collection: {collection_title} (ID: {collection_id})")
        publish_collection_to_all_channels(collection_id)
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
    """從 jp.bape.com Shopify JSON API 取得所有商品"""
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


def check_product_stock(product):
    """檢查商品是否有庫存（任一 variant 有庫存即可）"""
    variants = product.get('variants', [])
    for v in variants:
        if v.get('available', False):
            return True
    return False


def fetch_size_table(handle):
    """從商品頁面 HTML 取得尺寸表"""
    try:
        url = f"{SOURCE_URL}/products/{handle}"
        print(f"[尺寸表] 正在取得: {url}")
        
        response = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html',
        }, timeout=30)
        
        if response.status_code != 200:
            print(f"[尺寸表] HTTP {response.status_code}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找尺寸表 - 在 s-product-detail__def-list-description 裡面
        def_list = soup.find('dl', class_='s-product-detail__def-list-description')
        if not def_list:
            print(f"[尺寸表] 未找到 def-list")
            return None
        
        # 找 <dt>サイズ</dt> 後面的 <dd>
        size_dt = def_list.find('dt', string=re.compile(r'サイズ'))
        if not size_dt:
            print(f"[尺寸表] 未找到サイズ")
            return None
        
        size_dd = size_dt.find_next_sibling('dd')
        if not size_dd:
            print(f"[尺寸表] 未找到 dd")
            return None
        
        # 找表格
        table = size_dd.find('table')
        if not table:
            print(f"[尺寸表] 未找到 table")
            return None
        
        # 提取表格純文字（用於翻譯）
        rows = table.find_all('tr')
        size_spec_text = ''
        for row in rows:
            cells = row.find_all(['th', 'td'])
            row_text = ' | '.join([cell.get_text(strip=True) for cell in cells])
            size_spec_text += row_text + '\n'
        
        print(f"[尺寸表] 找到 {len(rows)} 行")
        return size_spec_text
        
    except Exception as e:
        print(f"[尺寸表] 錯誤: {e}")
        return None


def update_product_prices(source_product, existing_product_info):
    """比對並更新商品價格（官網價格 vs Shopify 成本價）"""
    product_id = existing_product_info['product_id']
    existing_variants = existing_product_info['variants']
    source_variants = source_product.get('variants', [])
    
    updated = False
    
    # 建立 existing variants 的查找表（用 option1+option2+option3 作為 key）
    existing_variant_map = {}
    for ev in existing_variants:
        key = f"{ev.get('option1', '')}|{ev.get('option2', '')}|{ev.get('option3', '')}"
        existing_variant_map[key] = ev
    
    for sv in source_variants:
        key = f"{sv.get('option1', '')}|{sv.get('option2', '')}|{sv.get('option3', '')}"
        
        if key in existing_variant_map:
            ev = existing_variant_map[key]
            
            # 官網價格（進貨成本）
            source_cost = float(sv.get('price', 0))
            
            # Shopify 現有成本價
            shopify_cost = float(ev.get('cost', 0)) if ev.get('cost') else 0
            
            # 比對：官網價格 vs Shopify 成本價
            if abs(source_cost - shopify_cost) >= 1:  # 成本價差異 >= 1 才更新
                variant_id = ev['variant_id']
                
                # 重新計算售價
                weight = float(sv.get('grams', 0)) / 1000 if sv.get('grams') else DEFAULT_WEIGHT
                new_selling_price = calculate_selling_price(source_cost, weight)
                
                print(f"[價格更新] Variant {variant_id}: 成本 ¥{shopify_cost} -> ¥{source_cost}, 售價更新為 ¥{new_selling_price}")
                
                # 更新價格和成本
                response = requests.put(
                    shopify_api_url(f"variants/{variant_id}.json"),
                    headers=get_shopify_headers(),
                    json={
                        'variant': {
                            'id': variant_id,
                            'price': f"{new_selling_price:.2f}",
                            'cost': f"{source_cost:.2f}"
                        }
                    }
                )
                
                if response.status_code == 200:
                    updated = True
                else:
                    print(f"[價格更新] 更新失敗: {response.text}")
    
    return updated


def upload_to_shopify(source_product, collection_id=None):
    """上傳商品到 Shopify（含 Variants）"""
    
    original_title = source_product.get('title', '')
    body_html = source_product.get('body_html', '')
    handle = source_product.get('handle', '')
    
    # 取得尺寸表
    size_spec = fetch_size_table(handle)
    
    print(f"[翻譯] 正在翻譯: {original_title[:30]}...")
    translated = translate_with_chatgpt(original_title, body_html, size_spec or '')
    
    if translated['success']:
        print(f"[翻譯成功] {translated['title'][:30]}...")
    else:
        print(f"[翻譯失敗] 使用原文")
    
    # 組合商品說明和尺寸表
    final_body_html = translated['description']
    if translated.get('size_spec_html'):
        final_body_html += '<br><br>' + translated['size_spec_html']
        print(f"[尺寸表] 已加入商品說明")
    
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
            'image_id': sv.get('image_id'),
        })
    
    # 處理圖片
    source_images = source_product.get('images', [])
    images_base64 = []
    image_id_to_position = {}
    
    print(f"[圖片] 開始下載 {len(source_images)} 張圖片...")
    
    for idx, img in enumerate(source_images):
        img_url = img.get('src', '')
        if not img_url:
            continue
        
        if img_url.startswith('//'):
            img_url = 'https:' + img_url
        
        print(f"[圖片] 下載中 ({idx+1}/{len(source_images)})")
        result = download_image_to_base64(img_url)
        
        if result['success']:
            image_data = {
                'attachment': result['base64'],
                'position': idx + 1,
                'filename': f"bape_{handle}_{idx+1}.jpg"
            }
            
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
            'body_html': final_body_html,  # 包含商品說明 + 尺寸表
            'vendor': 'BAPE',
            'product_type': source_product.get('product_type', ''),
            'status': 'active',
            'published': True,
            'handle': f"bape-{handle}",
            'options': options if options else [{'name': 'Title', 'values': ['Default Title']}],
            'variants': [v['variant_data'] for v in variants],
            'images': images_for_upload,
            'tags': f"BAPE, A BATHING APE, 日本, 潮流, 服飾, {source_product.get('product_type', '')}",
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
        source_to_created_variant = {}
        for idx, sv in enumerate(source_variants):
            if idx < len(created_variants):
                source_to_created_variant[sv.get('id')] = created_variants[idx]['id']
        
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
    <title>BAPE 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #8B4513; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #8B4513; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #A0522D; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #8B4513, #D2691E); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 80px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #8B4513; }}
        .stat-label {{ font-size: 11px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🦍 BAPE 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 jp.bape.com 所有商品並上架到 Shopify（含 Variants）</p>
        <p style="color: #666; font-size: 14px;">※ 成本價低於 ¥1000 或無庫存的商品將自動跳過</p>
        <p style="color: #666; font-size: 14px;">※ 已存在商品會自動同步價格</p>
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
                    <div class="stat-number" id="priceUpdatedCount" style="color: #3498db;">0</div>
                    <div class="stat-label">價格更新</div>
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
                    <div class="stat-number" id="outOfStockCount">0</div>
                    <div class="stat-label">無庫存</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="draftCount" style="color: #e67e22;">0</div>
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
                document.getElementById('priceUpdatedCount').textContent = data.price_updated || 0;
                document.getElementById('skippedCount').textContent = data.skipped;
                document.getElementById('filteredCount').textContent = data.filtered_by_price || 0;
                document.getElementById('outOfStockCount').textContent = data.out_of_stock || 0;
                document.getElementById('draftCount').textContent = data.set_to_draft || 0;
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
    
    return jsonify({'success': True, 'message': 'BAPE 爬蟲已啟動'})


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
            "out_of_stock": 0,
            "set_to_draft": 0,
            "price_updated": 0
        }
        
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("BAPE")
        print(f"[INFO] Collection ID: {collection_id}")
        
        scrape_status['current_product'] = "正在取得 Collection 內商品（含價格資訊）..."
        collection_products_map = get_collection_products_with_details(collection_id)
        existing_handles = set(collection_products_map.keys())
        print(f"[INFO] Collection 內有 {len(existing_handles)} 個商品")
        
        scrape_status['current_product'] = "正在從 jp.bape.com 取得商品列表..."
        product_list = fetch_all_products()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        # 記錄有庫存的商品 handle
        in_stock_handles = set()
        
        for idx, product in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            handle = product.get('handle', '')
            title = product.get('title', '')
            my_handle = f"bape-{handle}"
            scrape_status['current_product'] = f"處理中: {title[:30]}"
            
            # 檢查庫存
            has_stock = check_product_stock(product)
            
            if has_stock:
                in_stock_handles.add(my_handle)
            
            # 檢查是否已存在
            if my_handle in existing_handles:
                existing_info = collection_products_map[my_handle]
                
                if has_stock:
                    # 已存在且有庫存 -> 檢查並更新價格
                    scrape_status['current_product'] = f"檢查價格: {title[:30]}"
                    if update_product_prices(product, existing_info):
                        print(f"[價格同步] {title}")
                        scrape_status['price_updated'] += 1
                    else:
                        print(f"[跳過] 已存在，價格無變動: {handle}")
                    scrape_status['skipped_exists'] += 1
                    scrape_status['skipped'] += 1
                else:
                    print(f"[跳過] 已存在但無庫存（稍後設為草稿）: {handle}")
                    scrape_status['skipped'] += 1
                continue
            
            # 檢查最低價格
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
            
            # 檢查庫存（新商品）
            if not has_stock:
                print(f"[跳過] 無庫存: {title}")
                scrape_status['out_of_stock'] += 1
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
        
        # 設為草稿：已存在但現在無庫存或官網下架的商品
        scrape_status['current_product'] = "正在檢查需要設為草稿的商品..."
        
        for my_handle, product_info in collection_products_map.items():
            if my_handle not in in_stock_handles:
                scrape_status['current_product'] = f"設為草稿: {my_handle}"
                print(f"[設為草稿] {my_handle} - 無庫存或已下架")
                if set_product_to_draft(product_info['product_id']):
                    scrape_status['set_to_draft'] += 1
                time.sleep(0.5)
        
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
    
    summaries = []
    for p in products[:3]:
        summaries.append({
            'handle': p.get('handle'),
            'title': p.get('title'),
            'variants_count': len(p.get('variants', [])),
            'images_count': len(p.get('images', [])),
            'options': [o.get('name') for o in p.get('options', [])],
            'has_stock': check_product_stock(p),
            'min_price': min(float(v.get('price', 0)) for v in p.get('variants', [])) if p.get('variants') else 0
        })
    
    return jsonify({
        'total_count': len(products),
        'samples': summaries
    })


if __name__ == '__main__':
    print("=" * 50)
    print("BAPE 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)
