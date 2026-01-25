"""
WORKMAN 商品爬蟲 + Shopify 上架工具
來源：workman.jp
功能：
1. 爬取 workman.jp 男裝(c52)和女裝(c53)所有商品
2. 解析顏色和尺寸，建立 Variants
3. 圖片下載並上傳
4. 價格同步：已存在商品若價格變動則自動更新
5. 無庫存商品設為草稿
6. Collection 建立後發布到所有 channels
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

SOURCE_URL = "https://workman.jp"
CATEGORIES = {
    'work': {'url': '/shop/c/c51/', 'collection': 'WORKMAN 作業服', 'tags': 'WORKMAN, 日本, 服飾, 作業服, 工作服'},
    'mens': {'url': '/shop/c/c52/', 'collection': 'WORKMAN 男裝', 'tags': 'WORKMAN, 日本, 服飾, 男裝'},
    'womens': {'url': '/shop/c/c53/', 'collection': 'WORKMAN 女裝', 'tags': 'WORKMAN, 日本, 服飾, 女裝'},
    'kids': {'url': '/shop/c/c54/', 'collection': 'WORKMAN 兒童服', 'tags': 'WORKMAN, 日本, 服飾, 兒童服, 童裝'}
}

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DEFAULT_WEIGHT = 0.5  # 預設重量 kg

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en;q=0.9',
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
    japanese_pattern = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
    return bool(japanese_pattern.search(text))


def remove_japanese(text):
    """移除文字中的日文字元"""
    if not text:
        return text
    cleaned = re.sub(r'[\u3040-\u309F\u30A0-\u30FF]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r'[（）\(\)]\s*[（）\(\)]', '', cleaned)
    cleaned = re.sub(r'\s*[/／]\s*$', '', cleaned)
    cleaned = re.sub(r'^\s*[/／]\s*', '', cleaned)
    return cleaned


def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明：{description[:1500] if description else ''}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，簡潔有力，前面加上 WORKMAN）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示，每個重點用 <br> 換行）",
    "page_title": "SEO 頁面標題（繁體中文，包含品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

【最重要規則 - 絕對禁止日文】：
- 禁止出現任何平假名（あいうえお等）
- 禁止出現任何片假名（アイウエオ等）
- 如果原文有日文，必須翻譯成繁體中文
- 如果無法翻譯，直接省略該部分

其他規則：
1. 這是日本平價服飾品牌 WORKMAN 的商品
2. 商品名稱開頭必須是「WORKMAN」
3. 翻譯要自然流暢，不要生硬
4. SEO 內容要包含：WORKMAN、日本、平價、機能服飾等關鍵字
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
            trans_desc = translated.get('description', description)
            trans_page_title = translated.get('page_title', '')
            trans_meta_desc = translated.get('meta_description', '')
            
            # 檢查並移除日文
            if contains_japanese(trans_title):
                print(f"[警告] 標題包含日文，正在移除")
                trans_title = remove_japanese(trans_title)
            if contains_japanese(trans_desc):
                print(f"[警告] 描述包含日文，正在移除")
                trans_desc = remove_japanese(trans_desc)
            if contains_japanese(trans_page_title):
                trans_page_title = remove_japanese(trans_page_title)
            if contains_japanese(trans_meta_desc):
                trans_meta_desc = remove_japanese(trans_meta_desc)
            
            if not trans_title.startswith('WORKMAN'):
                trans_title = f"WORKMAN {trans_title}"
            
            return {
                'success': True,
                'title': trans_title,
                'description': trans_desc,
                'page_title': trans_page_title,
                'meta_description': trans_meta_desc
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"WORKMAN {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"WORKMAN {title}",
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
    """取得 Collection 內的商品（包含 variants 詳細資訊）"""
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
                    
                    variant_response = requests.get(
                        shopify_api_url(f"variants/{variant_id}.json"),
                        headers=get_shopify_headers()
                    )
                    if variant_response.status_code == 200:
                        variant_data = variant_response.json().get('variant', {})
                        cost = variant_data.get('cost')
                    time.sleep(0.1)
                    
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
        "id": f"gid://shopify/Collection/{collection_id}",
        "input": publication_inputs
    }
    
    pub_response = requests.post(graphql_url, headers=headers, json={
        'query': mutation,
        'variables': variables
    })
    
    return pub_response.status_code == 200


def get_or_create_collection(collection_title):
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
    
    return None


def add_product_to_collection(product_id, collection_id):
    response = requests.post(
        shopify_api_url('collects.json'),
        headers=get_shopify_headers(),
        json={'collect': {'product_id': product_id, 'collection_id': collection_id}}
    )
    return response.status_code == 201


def publish_to_all_channels(product_id):
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


def get_total_pages(category_url):
    """取得分類的總頁數"""
    url = SOURCE_URL + category_url
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # 找 "最後" 連結
            last_link = soup.find('a', string='最後')
            if last_link and last_link.get('href'):
                # /shop/c/c52_p14/ -> 14
                match = re.search(r'_p(\d+)', last_link['href'])
                if match:
                    return int(match.group(1))
            return 1
    except Exception as e:
        print(f"[ERROR] 取得總頁數失敗: {e}")
    return 1


def fetch_product_links_from_page(page_url):
    """從列表頁取得商品連結"""
    product_links = []
    try:
        response = requests.get(page_url, headers=HEADERS, timeout=30)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # 找所有商品連結 /shop/g/gXXXX/
            links = soup.find_all('a', href=re.compile(r'/shop/g/g\d+/'))
            seen = set()
            for link in links:
                href = link.get('href')
                if href and href not in seen:
                    seen.add(href)
                    product_links.append(SOURCE_URL + href)
    except Exception as e:
        print(f"[ERROR] 取得商品連結失敗: {e}")
    return product_links


def fetch_all_product_links(category_key):
    """取得分類下所有商品連結"""
    category = CATEGORIES[category_key]
    base_url = category['url']
    
    total_pages = get_total_pages(base_url)
    print(f"[INFO] {category['collection']} 共 {total_pages} 頁")
    
    all_links = []
    
    for page in range(1, total_pages + 1):
        if page == 1:
            page_url = SOURCE_URL + base_url
        else:
            # /shop/c/c52/ -> /shop/c/c52_p2/
            page_url = SOURCE_URL + base_url.rstrip('/') + f'_p{page}/'
        
        print(f"[INFO] 正在載入第 {page}/{total_pages} 頁...")
        links = fetch_product_links_from_page(page_url)
        all_links.extend(links)
        print(f"[INFO] 第 {page} 頁取得 {len(links)} 個商品")
        time.sleep(0.5)
    
    # 去重
    all_links = list(dict.fromkeys(all_links))
    print(f"[INFO] {category['collection']} 共 {len(all_links)} 個商品")
    return all_links


def parse_product_page(url):
    """解析商品頁面"""
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 商品名稱
        title_elem = soup.find('h1')
        title = title_elem.get_text(strip=True) if title_elem else ''
        
        # 價格
        price = 0
        price_elem = soup.find('h2', string=re.compile(r'[\d,]+円'))
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            price_match = re.search(r'([\d,]+)円', price_text)
            if price_match:
                price = int(price_match.group(1).replace(',', ''))
        
        # 商品番號
        product_code = ''
        code_dt = soup.find('dt', string='商品番号')
        if code_dt:
            code_dd = code_dt.find_next_sibling('dd')
            if code_dd:
                product_code = code_dd.get_text(strip=True)
        
        # 管理番號（用於圖片和 handle）
        manage_code = ''
        manage_dt = soup.find('dt', string='管理番号')
        if manage_dt:
            manage_dd = manage_dt.find_next_sibling('dd')
            if manage_dd:
                manage_code = manage_dd.get_text(strip=True)
        
        # 商品說明
        description = ''
        desc_dt = soup.find('dt', string='商品説明')
        if desc_dt:
            desc_dd = desc_dt.find_next_sibling('dd')
            if desc_dd:
                description = str(desc_dd)
        
        # 顏色選項（從圖片 alt 取得）
        colors = []
        color_imgs = soup.find_all('img', src=re.compile(r'/img/goods/\d+/\d+_c\d+\.jpg'))
        for img in color_imgs:
            alt = img.get('alt', '')
            if alt and alt not in colors:
                colors.append(alt)
        
        # 如果沒有顏色圖，檢查是否有單一顏色
        if not colors:
            main_img = soup.find('img', src=re.compile(r'/img/goods/L/\d+_t1\.jpg'))
            if main_img:
                alt = main_img.get('alt', '')
                if alt:
                    colors.append(alt)
        
        if not colors:
            colors = ['標準']
        
        # 尺寸選項（從規格表取得）
        sizes = []
        size_dt = soup.find('dt', string='サイズ・スペック')
        if size_dt:
            size_dd = size_dt.find_next_sibling('dd')
            if size_dd:
                table = size_dd.find('table')
                if table:
                    # 找第一行的 th（尺寸標題）
                    first_row = table.find('tr')
                    if first_row:
                        ths = first_row.find_all('th')
                        for th in ths[1:]:  # 跳過第一個（通常是 "サイズ"）
                            size = th.get_text(strip=True)
                            if size and size not in sizes:
                                sizes.append(size)
        
        if not sizes:
            sizes = ['FREE']
        
        # 圖片 - 收集所有圖片並轉換為大圖
        images = []
        color_images = {}  # {顏色索引: 圖片URL}
        
        # 找所有圖片 (包含 _t1, _c1, _c2, _d1, _d2 等)
        all_imgs = soup.find_all('img', src=re.compile(r'/img/goods/'))
        seen_imgs = set()
        
        for img in all_imgs:
            img_src = img.get('src', '')
            if not img_src or img_src in seen_imgs:
                continue
            
            # 跳過 icon 圖片
            if '/icon/' in img_src or 'logo' in img_src:
                continue
            
            # 提取檔名
            filename_match = re.search(r'/(\d+_[a-z]\d+\.jpg)$', img_src)
            if not filename_match:
                filename_match = re.search(r'/(\d+_t\d+\.jpg)$', img_src)
            
            if filename_match:
                filename = filename_match.group(1)
                # 轉換為大圖 URL
                large_url = f"{SOURCE_URL}/img/goods/L/{filename}"
                
                if large_url not in seen_imgs:
                    seen_imgs.add(large_url)
                    
                    # 主圖 (_t1) 放最前面
                    if '_t1.' in filename:
                        images.insert(0, large_url)
                    # 顏色圖 (_c1, _c2...) 記錄對應關係
                    elif '_c' in filename:
                        images.append(large_url)
                        # 提取顏色索引
                        c_match = re.search(r'_c(\d+)\.', filename)
                        if c_match:
                            color_idx = int(c_match.group(1)) - 1  # 轉為 0-based
                            color_images[color_idx] = large_url
                    # 詳細圖 (_d1, _d2...)
                    elif '_d' in filename:
                        images.append(large_url)
        
        # 如果沒找到圖片，嘗試直接用管理番號組合
        if not images and manage_code:
            images.append(f"{SOURCE_URL}/img/goods/L/{manage_code}_t1.jpg")
            for i in range(1, len(colors) + 1):
                color_img = f"{SOURCE_URL}/img/goods/L/{manage_code}_c{i}.jpg"
                images.append(color_img)
                color_images[i-1] = color_img
        
        return {
            'url': url,
            'title': title,
            'price': price,
            'product_code': product_code,
            'manage_code': manage_code,
            'description': description,
            'colors': colors,
            'sizes': sizes,
            'images': images,
            'color_images': color_images  # 顏色對應圖片
        }
        
    except Exception as e:
        print(f"[ERROR] 解析商品頁面失敗 {url}: {e}")
        return None


def update_product_prices(source_product, existing_product_info):
    """比對並更新商品價格（官網價格 vs Shopify 成本價）"""
    existing_variants = existing_product_info['variants']
    source_price = source_product['price']
    
    updated = False
    
    for ev in existing_variants:
        shopify_cost = float(ev.get('cost', 0)) if ev.get('cost') else 0
        
        if abs(source_price - shopify_cost) >= 1:
            variant_id = ev['variant_id']
            
            new_selling_price = calculate_selling_price(source_price, DEFAULT_WEIGHT)
            
            print(f"[價格更新] Variant {variant_id}: 成本 ¥{shopify_cost} -> ¥{source_price}, 售價更新為 ¥{new_selling_price}")
            
            response = requests.put(
                shopify_api_url(f"variants/{variant_id}.json"),
                headers=get_shopify_headers(),
                json={
                    'variant': {
                        'id': variant_id,
                        'price': f"{new_selling_price:.2f}",
                        'cost': f"{source_price:.2f}"
                    }
                }
            )
            
            if response.status_code == 200:
                updated = True
    
    return updated


def upload_to_shopify(product_data, collection_id, tags):
    """上傳商品到 Shopify"""
    
    original_title = product_data['title']
    description = product_data['description']
    manage_code = product_data['manage_code']
    cost = product_data['price']
    colors = product_data['colors']
    sizes = product_data['sizes']
    
    print(f"[翻譯] 正在翻譯: {original_title[:30]}...")
    translated = translate_with_chatgpt(original_title, description)
    
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)
    
    # 建立 Options
    options = []
    if len(colors) > 1 or (len(colors) == 1 and colors[0] != '標準'):
        options.append({'name': '顏色', 'values': colors})
    if len(sizes) > 1 or (len(sizes) == 1 and sizes[0] != 'FREE'):
        options.append({'name': '尺寸', 'values': sizes})
    
    if not options:
        options = [{'name': 'Title', 'values': ['Default Title']}]
    
    # 建立 Variants（顏色 × 尺寸）
    variants = []
    if len(options) == 1:
        if options[0]['name'] == '顏色':
            for color in colors:
                variants.append({
                    'option1': color,
                    'price': f"{selling_price:.2f}",
                    'sku': f"{manage_code}-{color}",
                    'weight': DEFAULT_WEIGHT,
                    'weight_unit': 'kg',
                    'inventory_management': None,
                    'inventory_policy': 'continue',
                    'requires_shipping': True,
                })
        elif options[0]['name'] == '尺寸':
            for size in sizes:
                variants.append({
                    'option1': size,
                    'price': f"{selling_price:.2f}",
                    'sku': f"{manage_code}-{size}",
                    'weight': DEFAULT_WEIGHT,
                    'weight_unit': 'kg',
                    'inventory_management': None,
                    'inventory_policy': 'continue',
                    'requires_shipping': True,
                })
        else:
            variants.append({
                'option1': 'Default Title',
                'price': f"{selling_price:.2f}",
                'sku': manage_code,
                'weight': DEFAULT_WEIGHT,
                'weight_unit': 'kg',
                'inventory_management': None,
                'inventory_policy': 'continue',
                'requires_shipping': True,
            })
    elif len(options) == 2:
        for color in colors:
            for size in sizes:
                variants.append({
                    'option1': color,
                    'option2': size,
                    'price': f"{selling_price:.2f}",
                    'sku': f"{manage_code}-{color}-{size}",
                    'weight': DEFAULT_WEIGHT,
                    'weight_unit': 'kg',
                    'inventory_management': None,
                    'inventory_policy': 'continue',
                    'requires_shipping': True,
                })
    else:
        variants.append({
            'option1': 'Default Title',
            'price': f"{selling_price:.2f}",
            'sku': manage_code,
            'weight': DEFAULT_WEIGHT,
            'weight_unit': 'kg',
            'inventory_management': None,
            'inventory_policy': 'continue',
            'requires_shipping': True,
        })
    
    # 處理圖片
    images_base64 = []
    color_images = product_data.get('color_images', {})  # {顏色索引: URL}
    image_url_to_position = {}  # {URL: position}
    
    print(f"[圖片] 開始下載 {len(product_data['images'])} 張圖片...")
    
    for idx, img_url in enumerate(product_data['images'][:15]):  # 最多 15 張
        print(f"[圖片] 下載中 ({idx+1}/{min(len(product_data['images']), 15)}): {img_url[-30:]}")
        result = download_image_to_base64(img_url)
        
        if result['success']:
            position = idx + 1
            images_base64.append({
                'attachment': result['base64'],
                'position': position,
                'filename': f"workman_{manage_code}_{idx+1}.jpg"
            })
            image_url_to_position[img_url] = position
            print(f"[圖片] ✓ 下載成功")
        else:
            print(f"[圖片] ✗ 下載失敗")
        
        time.sleep(0.3)
    
    print(f"[圖片] 成功下載 {len(images_base64)} 張圖片")
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': 'WORKMAN',
            'product_type': '',
            'status': 'active',
            'published': True,
            'handle': f"workman-{manage_code}",
            'options': options,
            'variants': variants,
            'images': images_base64,
            'tags': tags,
            'metafields_global_title_tag': translated['page_title'],
            'metafields_global_description_tag': translated['meta_description'],
            'metafields': [
                {
                    'namespace': 'custom',
                    'key': 'link',
                    'value': product_data['url'],
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
    
    if response.status_code == 201:
        created_product = response.json()['product']
        product_id = created_product['id']
        created_variants = created_product.get('variants', [])
        created_images = created_product.get('images', [])
        
        # 更新每個 variant 的 cost
        for cv in created_variants:
            requests.put(
                shopify_api_url(f"variants/{cv['id']}.json"),
                headers=get_shopify_headers(),
                json={'variant': {'id': cv['id'], 'cost': f"{cost:.2f}"}}
            )
        
        # 圖片與 Variant 對應
        # 建立顏色到 variant IDs 的對應
        color_to_variant_ids = {}
        for cv in created_variants:
            color = cv.get('option1', '')
            if color:
                if color not in color_to_variant_ids:
                    color_to_variant_ids[color] = []
                color_to_variant_ids[color].append(cv['id'])
        
        # 把顏色圖和對應的 variants 關聯
        for color_idx, color_img_url in color_images.items():
            if color_idx < len(colors):
                color_name = colors[color_idx]
                variant_ids = color_to_variant_ids.get(color_name, [])
                
                if variant_ids and color_img_url in image_url_to_position:
                    position = image_url_to_position[color_img_url]
                    # 找到對應的 Shopify 圖片
                    for created_img in created_images:
                        if created_img.get('position') == position:
                            # 更新圖片的 variant_ids
                            requests.put(
                                shopify_api_url(f"products/{product_id}/images/{created_img['id']}.json"),
                                headers=get_shopify_headers(),
                                json={'image': {'id': created_img['id'], 'variant_ids': variant_ids}}
                            )
                            print(f"[圖片對應] 顏色 {color_name} 圖片已關聯 {len(variant_ids)} 個 variants")
                            break
        
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
    <title>WORKMAN 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #FF6600; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #FF6600; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; margin-bottom: 10px; }}
        .btn:hover {{ background: #E55A00; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-work {{ background: #795548; }}
        .btn-mens {{ background: #2980b9; }}
        .btn-womens {{ background: #e91e63; }}
        .btn-kids {{ background: #4caf50; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #FF6600, #FFA500); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 80px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #FF6600; }}
        .stat-label {{ font-size: 11px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🔧 WORKMAN 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 workman.jp 所有商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">※ 已存在商品會自動同步價格</p>
        <button class="btn btn-work" id="startWorkBtn" onclick="startScrape('work')">🔧 爬取作業服</button>
        <button class="btn btn-mens" id="startMensBtn" onclick="startScrape('mens')">👔 爬取男裝</button>
        <button class="btn btn-womens" id="startWomensBtn" onclick="startScrape('womens')">👗 爬取女裝</button>
        <button class="btn btn-kids" id="startKidsBtn" onclick="startScrape('kids')">👶 爬取兒童服</button>
        <button class="btn" id="startAllBtn" onclick="startScrape('all')">🚀 全部爬取</button>
        
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
        function disableButtons(disabled) {{
            document.getElementById('startWorkBtn').disabled = disabled;
            document.getElementById('startMensBtn').disabled = disabled;
            document.getElementById('startWomensBtn').disabled = disabled;
            document.getElementById('startKidsBtn').disabled = disabled;
            document.getElementById('startAllBtn').disabled = disabled;
        }}
        async function testShopify() {{
            log('測試 Shopify 連線...');
            try {{
                const res = await fetch('/api/test-shopify');
                const data = await res.json();
                if (data.success) log('✓ 連線成功！', 'success');
                else log('✗ 連線失敗: ' + data.error, 'error');
            }} catch (e) {{ log('✗ 請求失敗: ' + e.message, 'error'); }}
        }}
        async function startScrape(category) {{
            clearLog(); log('開始爬取流程 (' + category + ')...');
            disableButtons(true);
            document.getElementById('progressSection').style.display = 'block';
            try {{
                const res = await fetch('/api/start?category=' + category, {{ method: 'POST' }});
                const data = await res.json();
                if (!data.success) {{ log('✗ ' + data.error, 'error'); disableButtons(false); return; }}
                log('✓ 爬取任務已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            }} catch (e) {{ log('✗ ' + e.message, 'error'); disableButtons(false); }}
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
                document.getElementById('draftCount').textContent = data.set_to_draft || 0;
                document.getElementById('errorCount').textContent = data.errors.length;
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    disableButtons(false);
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
    
    category = request.args.get('category', 'all')
    
    thread = threading.Thread(target=run_scrape, args=(category,))
    thread.start()
    
    return jsonify({'success': True, 'message': f'WORKMAN 爬蟲已啟動 ({category})'})


from flask import request


def run_scrape(category='all'):
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
            "out_of_stock": 0,
            "set_to_draft": 0,
            "price_updated": 0
        }
        
        categories_to_scrape = []
        if category == 'all':
            categories_to_scrape = ['work', 'mens', 'womens', 'kids']
        elif category in CATEGORIES:
            categories_to_scrape = [category]
        else:
            scrape_status['errors'].append({'error': f'未知分類: {category}'})
            scrape_status['running'] = False
            return
        
        for cat_key in categories_to_scrape:
            cat_info = CATEGORIES[cat_key]
            collection_name = cat_info['collection']
            tags = cat_info['tags']
            
            scrape_status['current_product'] = f"正在設定 Collection: {collection_name}..."
            collection_id = get_or_create_collection(collection_name)
            print(f"[INFO] Collection ID: {collection_id}")
            
            scrape_status['current_product'] = f"正在取得 {collection_name} 商品列表..."
            collection_products_map = get_collection_products_with_details(collection_id)
            existing_handles = set(collection_products_map.keys())
            
            scrape_status['current_product'] = f"正在從 workman.jp 取得 {collection_name} 商品連結..."
            product_links = fetch_all_product_links(cat_key)
            scrape_status['total'] += len(product_links)
            
            # 記錄官網有的商品 handle
            website_handles = set()
            
            for idx, link in enumerate(product_links):
                scrape_status['progress'] += 1
                scrape_status['current_product'] = f"處理中: {link[-20:]}"
                
                product_data = parse_product_page(link)
                
                if not product_data:
                    scrape_status['errors'].append({'url': link, 'error': '解析失敗'})
                    continue
                
                my_handle = f"workman-{product_data['manage_code']}"
                website_handles.add(my_handle)
                
                # 檢查是否已存在
                if my_handle in existing_handles:
                    existing_info = collection_products_map[my_handle]
                    
                    scrape_status['current_product'] = f"檢查價格: {product_data['title'][:20]}"
                    if update_product_prices(product_data, existing_info):
                        print(f"[價格同步] {product_data['title']}")
                        scrape_status['price_updated'] += 1
                    else:
                        print(f"[跳過] 已存在，價格無變動: {my_handle}")
                    
                    scrape_status['skipped_exists'] += 1
                    scrape_status['skipped'] += 1
                    continue
                
                result = upload_to_shopify(product_data, collection_id, tags)
                
                if result['success']:
                    translated_title = result.get('translated', {}).get('title', product_data['title'])
                    variants_count = result.get('variants_count', 0)
                    print(f"[成功] {translated_title} ({variants_count} variants)")
                    scrape_status['uploaded'] += 1
                    scrape_status['products'].append({
                        'handle': my_handle,
                        'title': translated_title,
                        'status': 'success'
                    })
                else:
                    print(f"[失敗] {product_data['title']}: {result['error']}")
                    scrape_status['errors'].append({
                        'handle': my_handle,
                        'title': product_data['title'],
                        'error': result['error']
                    })
                
                time.sleep(1)
            
            # 設為草稿：已上架但官網已下架的商品
            scrape_status['current_product'] = f"正在檢查 {collection_name} 需要設為草稿的商品..."
            
            for my_handle, product_info in collection_products_map.items():
                if my_handle not in website_handles:
                    scrape_status['current_product'] = f"設為草稿: {my_handle}"
                    print(f"[設為草稿] {my_handle} - 官網已下架")
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
    """測試爬取"""
    # 測試取得一個商品
    test_url = "https://workman.jp/shop/g/g2300044989124/"
    product = parse_product_page(test_url)
    
    if product:
        return jsonify({
            'success': True,
            'product': {
                'title': product['title'],
                'price': product['price'],
                'product_code': product['product_code'],
                'manage_code': product['manage_code'],
                'colors': product['colors'],
                'sizes': product['sizes'],
                'images_count': len(product['images'])
            }
        })
    else:
        return jsonify({'success': False, 'error': '解析失敗'})


if __name__ == '__main__':
    print("=" * 50)
    print("WORKMAN 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)
