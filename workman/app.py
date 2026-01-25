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


# 快取 Collection ID
_collection_id_cache = {}


def get_or_create_collection(collection_name):
    """取得或建立 Collection，回傳 Collection ID"""
    global _collection_id_cache
    
    # 檢查快取
    if collection_name in _collection_id_cache:
        return _collection_id_cache[collection_name]
    
    # 先查詢是否存在
    query = """
    query findCollection($title: String!) {
      collections(first: 1, query: $title) {
        edges {
          node {
            id
            title
          }
        }
      }
    }
    """
    result = graphql_request(query, {"title": f"title:{collection_name}"})
    edges = result.get('data', {}).get('collections', {}).get('edges', [])
    
    for edge in edges:
        if edge['node']['title'] == collection_name:
            collection_id = edge['node']['id']
            _collection_id_cache[collection_name] = collection_id
            print(f"[Collection] 找到: {collection_name} -> {collection_id}")
            return collection_id
    
    # 不存在，建立新的
    mutation = """
    mutation createCollection($input: CollectionInput!) {
      collectionCreate(input: $input) {
        collection {
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
    result = graphql_request(mutation, {
        "input": {
            "title": collection_name,
            "descriptionHtml": f"<p>{collection_name} 商品系列</p>"
        }
    })
    
    collection = result.get('data', {}).get('collectionCreate', {}).get('collection')
    if collection:
        collection_id = collection['id']
        _collection_id_cache[collection_name] = collection_id
        print(f"[Collection] 建立: {collection_name} -> {collection_id}")
        
        # 發布 Collection 到所有銷售管道
        publish_collection_to_all_channels(collection_id)
        
        return collection_id
    
    errors = result.get('data', {}).get('collectionCreate', {}).get('userErrors', [])
    print(f"[Collection] 建立失敗: {collection_name}, 錯誤: {errors}")
    return None


def publish_collection_to_all_channels(collection_id):
    """發布 Collection 到所有銷售管道"""
    publication_ids = get_all_publication_ids()
    
    if not publication_ids:
        print(f"[Collection] 沒有找到任何銷售管道")
        return
    
    publication_inputs = [{"publicationId": pub_id} for pub_id in publication_ids]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    result = graphql_request(mutation, {"id": collection_id, "input": publication_inputs})
    
    user_errors = result.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
    if user_errors:
        print(f"[Collection] 發布失敗: {user_errors}")
    else:
        count = result.get('data', {}).get('publishablePublish', {}).get('publishable', {}).get('availablePublicationsCount', {}).get('count', 0)
        print(f"[Collection] 已發布到 {count} 個銷售管道")


def get_all_publication_ids():
    """取得所有 Publication ID（用於發布商品）"""
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
    result = graphql_request(query)
    
    publication_ids = []
    edges = result.get('data', {}).get('publications', {}).get('edges', [])
    for edge in edges:
        publication_ids.append(edge['node']['id'])
    
    return publication_ids


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
4. 完全忽略注意事項（ご注意、注意事項、ご了承、※記號開頭的警告文字等），不要翻譯這些內容
5. 完全忽略價格相關內容（円、日圓、OFF、割引、値下げ等）
6. 只回傳 JSON"""

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
                
                # 找所有連結，篩選出商品連結 (/shop/g/)
                for link in soup.find_all('a', href=True):
                    href = link.get('href', '')
                    if '/shop/g/' in href:
                        full_url = SOURCE_URL + href if href.startswith('/') else href
                        if full_url not in all_links:
                            all_links.append(full_url)
                            
                print(f"[INFO] 第 {page} 頁找到 {len(all_links)} 個商品連結")
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
            print(f"[解析失敗] {url} - HTTP {response.status_code}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 標題 - 嘗試多種方式
        title = ''
        title_elem = soup.find('h1', class_='block-goods-name')
        if title_elem:
            title = title_elem.get_text(strip=True)
        else:
            # 備用：找任何 h1
            title_elem = soup.find('h1')
            if title_elem:
                title = title_elem.get_text(strip=True)
        
        print(f"[解析] 標題: {title[:30] if title else '(無)'}")
        
        # 價格 - 嘗試多種方式
        price = 0
        price_elem = soup.find('p', class_='block-goods-price')
        if not price_elem:
            # 備用：找 class 包含 price 的元素
            price_elem = soup.find(class_=re.compile(r'price'))
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            match = re.search(r'[\d,]+', price_text)
            if match:
                price = int(match.group().replace(',', ''))
        
        print(f"[解析] 價格: ¥{price}")
        
        # 管理番號 - 嘗試多種方式
        manage_code = ''
        code_dt = soup.find('dt', string='管理番号')
        if code_dt:
            code_dd = code_dt.find_next_sibling('dd')
            if code_dd:
                manage_code = code_dd.get_text(strip=True)
        
        if not manage_code:
            # 備用：從 URL 取得
            match = re.search(r'/g/g(\d+)/', url)
            if match:
                manage_code = match.group(1)
        
        print(f"[解析] 管理番號: {manage_code if manage_code else '(無)'}")
        
        # 放寬條件：只要有 manage_code 就繼續（不再要求 price >= 1000）
        if not manage_code:
            print(f"[解析失敗] {url} - 無法取得管理番號")
            return None
        
        # 如果價格為 0，設定預設值
        if price == 0:
            price = 1500  # 預設價格
            print(f"[解析] 價格為 0，使用預設值 ¥{price}")
        
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


def product_to_jsonl_entry(product_data, tags, category_key, collection_id):
    """將商品資料轉換為 JSONL 格式（Shopify GraphQL ProductSetInput）"""
    
    # 根據分類設定商品類型
    PRODUCT_TYPES = {
        'work': 'WORKMAN 作業服',
        'mens': 'WORKMAN 男裝',
        'womens': 'WORKMAN 女裝',
        'kids': 'WORKMAN 兒童'
    }
    product_type = PRODUCT_TYPES.get(category_key, 'WORKMAN')
    
    # 翻譯
    translated = translate_with_chatgpt(
        product_data['title'],
        product_data['description'],
        product_data.get('size_spec', '')
    )
    
    title = translated['title']
    description = translated['description']
    
    import re
    import html
    
    # 移除說明文中的超連結（包含 <a> 標籤和其中的文字）
    description = re.sub(r'<a[^>]*>.*?</a>', '', description)
    
    # 移除價格相關的句子（包含「日圓」「円」「OFF」「降價」等）
    description = re.sub(r'[^<>]*\d+[,，]?\d*\s*日圓[^<>]*', '', description)
    description = re.sub(r'[^<>]*\d+[,，]?\d*\s*円[^<>]*', '', description)
    description = re.sub(r'[^<>]*\d+%\s*OFF[^<>]*', '', description, flags=re.IGNORECASE)
    description = re.sub(r'[^<>]*降價[^<>]*', '', description)
    description = re.sub(r'[^<>]*大幅[^<>]*', '', description)
    
    # 移除注意事項相關內容（翻譯後可能殘留的）
    description = re.sub(r'[^<>]*注意事項[^<>]*', '', description)
    description = re.sub(r'[^<>]*請注意[^<>]*', '', description)
    description = re.sub(r'[^<>]*敬請諒解[^<>]*', '', description)
    description = re.sub(r'[^<>]*敬請見諒[^<>]*', '', description)
    description = re.sub(r'[^<>]*※[^<>]*', '', description)  # 移除 ※ 開頭的警告文字
    
    # 徹底清理空白和空標籤
    description = re.sub(r'<p>\s*</p>', '', description)  # 移除空的 <p> 標籤
    description = re.sub(r'<br\s*/?>\s*<br\s*/?>', '<br>', description)  # 連續 br 變單一
    description = re.sub(r'^\s*(<br\s*/?>)+', '', description)  # 移除開頭的 br
    description = re.sub(r'(<br\s*/?>)+\s*$', '', description)  # 移除結尾的 br
    description = re.sub(r'\n\s*\n', '\n', description)  # 移除連續空行
    description = description.strip()
    
    # 加入統一注意事項
    notice = """
<br><br>
<p><strong>【請注意以下事項】</strong></p>
<p>※不接受退換貨</p>
<p>※開箱請全程錄影</p>
<p>※因庫存有限，訂購時間不同可能會出現缺貨情況。</p>
"""
    description = description + notice
    
    manage_code = product_data['manage_code']
    cost = product_data['price']  # 日圓成本
    colors = product_data['colors']
    sizes = product_data['sizes']
    images = product_data['images']
    source_url = product_data['url']
    
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)
    
    # 建立 productOptions（ProductSetInput 格式）
    product_options = []
    has_color_option = len(colors) > 1 or (len(colors) == 1 and colors[0] != '標準')
    has_size_option = len(sizes) > 1 or (len(sizes) == 1 and sizes[0] != 'FREE')
    
    if has_color_option:
        product_options.append({
            "name": "顏色",
            "values": [{"name": c} for c in colors]
        })
    
    if has_size_option:
        product_options.append({
            "name": "尺寸",
            "values": [{"name": s} for s in sizes]
        })
    
    # 準備圖片（只取前10張）
    image_list = images[:10] if images else []
    first_image = image_list[0] if image_list else None
    
    # 建立 files 陣列
    files = []
    if image_list:
        for img_url in image_list:
            files.append({
                "originalSource": img_url,
                "contentType": "IMAGE"
            })
    
    # 建立 variant 的 file 物件（必須跟 files 陣列中的相同）
    variant_file = None
    if first_image:
        variant_file = {
            "originalSource": first_image,
            "contentType": "IMAGE"
        }
    
    # 建立 variants（ProductSetInput 格式）
    # 加入 cost（成本）和 taxable: false
    variants = []
    
    if has_color_option and has_size_option:
        # 顏色 × 尺寸
        for color in colors:
            for size in sizes:
                variant = {
                    "price": selling_price,
                    "sku": f"{manage_code}-{color}-{size}",
                    "inventoryPolicy": "CONTINUE",
                    "taxable": False,
                    "inventoryItem": {
                        "cost": cost  # 日圓成本
                    },
                    "optionValues": [
                        {"optionName": "顏色", "name": color},
                        {"optionName": "尺寸", "name": size}
                    ]
                }
                if variant_file:
                    variant["file"] = variant_file
                variants.append(variant)
    elif has_color_option:
        for color in colors:
            variant = {
                "price": selling_price,
                "sku": f"{manage_code}-{color}",
                "inventoryPolicy": "CONTINUE",
                "taxable": False,
                "inventoryItem": {
                    "cost": cost
                },
                "optionValues": [
                    {"optionName": "顏色", "name": color}
                ]
            }
            if variant_file:
                variant["file"] = variant_file
            variants.append(variant)
    elif has_size_option:
        for size in sizes:
            variant = {
                "price": selling_price,
                "sku": f"{manage_code}-{size}",
                "inventoryPolicy": "CONTINUE",
                "taxable": False,
                "inventoryItem": {
                    "cost": cost
                },
                "optionValues": [
                    {"optionName": "尺寸", "name": size}
                ]
            }
            if variant_file:
                variant["file"] = variant_file
            variants.append(variant)
    else:
        # 沒有選項
        variant = {
            "price": selling_price,
            "sku": manage_code,
            "inventoryPolicy": "CONTINUE",
            "taxable": False,
            "inventoryItem": {
                "cost": cost
            }
        }
        if variant_file:
            variant["file"] = variant_file
        variants.append(variant)
    
    # 建立 SEO 資訊（獨立撰寫，不使用說明文）
    seo_title = f"{title} | WORKMAN 日本代購"
    seo_description = f"日本 WORKMAN 官方正品代購。{title}，台灣現貨或日本直送，品質保證。GOYOUTATI 御用達日本伴手禮專門店。"
    
    # ProductSetInput 結構
    product_input = {
        "title": title,
        "descriptionHtml": description,
        "vendor": "WORKMAN",
        "productType": product_type,
        "status": "ACTIVE",
        "handle": f"workman-{manage_code}",
        "tags": tags,
        # SEO 資訊（獨立撰寫）
        "seo": {
            "title": seo_title,
            "description": seo_description
        },
        # 中繼欄位 - 來源連結
        "metafields": [
            {
                "namespace": "custom",
                "key": "link",
                "value": source_url,
                "type": "url"
            }
        ]
    }
    
    # 加入 Collection（使用 ID）
    if collection_id:
        product_input["collections"] = [collection_id]
    
    # 加入選項
    if product_options:
        product_input["productOptions"] = product_options
    
    # 加入 variants
    if variants:
        product_input["variants"] = variants
    
    # 加入圖片（使用 files）
    if files:
        product_input["files"] = files
    
    # 變數名稱是 productSet（不是 input）
    return {
        "productSet": product_input,
        "synchronous": True
    }


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
    
    # 使用 productSet mutation（2024 年後的新 API 格式）
    # 變數名稱必須是 $productSet（不是 $input）
    mutation = """
    mutation call($productSet: ProductSetInput!, $synchronous: Boolean!) {
      productSet(synchronous: $synchronous, input: $productSet) {
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


def get_bulk_operation_results():
    """取得 Bulk Operation 的詳細結果"""
    # 先取得最新的 bulk operation
    status = check_bulk_operation_status()
    
    results = {
        'status': status.get('status'),
        'objectCount': status.get('objectCount'),
        'errorCode': status.get('errorCode'),
        'url': status.get('url'),
    }
    
    # 如果有結果 URL，下載結果
    if status.get('url'):
        try:
            response = requests.get(status['url'], timeout=30)
            if response.status_code == 200:
                # 結果是 JSONL 格式
                lines = response.text.strip().split('\n')
                results['total_results'] = len(lines)
                results['sample_results'] = []
                
                errors = []
                successes = []
                
                for line in lines[:50]:  # 只檢查前 50 行
                    try:
                        data = json.loads(line)
                        
                        # 檢查 productSet 結果
                        if 'data' in data and 'productSet' in data.get('data', {}):
                            product_set = data['data']['productSet']
                            user_errors = product_set.get('userErrors', [])
                            
                            if user_errors:
                                errors.append({
                                    'errors': user_errors,
                                    'input': data.get('__parentId', '')
                                })
                            elif product_set.get('product'):
                                successes.append({
                                    'id': product_set['product'].get('id'),
                                    'title': product_set['product'].get('title', '')[:50]
                                })
                        # 相容舊的 productCreate 格式
                        elif 'data' in data and 'productCreate' in data.get('data', {}):
                            product_create = data['data']['productCreate']
                            user_errors = product_create.get('userErrors', [])
                            
                            if user_errors:
                                errors.append({
                                    'errors': user_errors,
                                    'input': data.get('__parentId', '')
                                })
                            elif product_create.get('product'):
                                successes.append({
                                    'id': product_create['product'].get('id'),
                                    'title': product_create['product'].get('title', '')[:50]
                                })
                        # 檢查是否有錯誤
                        elif 'errors' in data:
                            errors.append({
                                'errors': data['errors'],
                                'input': ''
                            })
                        
                        results['sample_results'].append(data)
                    except:
                        pass
                
                results['errors'] = errors[:10]
                results['successes'] = successes[:10]
                results['error_count'] = len(errors)
                results['success_count'] = len(successes)
        except Exception as e:
            results['fetch_error'] = str(e)
    
    return results


# ========== 批量發布到銷售管道 ==========

def get_all_publications():
    """取得所有銷售管道（Publications）"""
    query = """
    {
      publications(first: 20) {
        edges {
          node {
            id
            name
            catalog {
              title
            }
          }
        }
      }
    }
    """
    result = graphql_request(query)
    
    publications = []
    edges = result.get('data', {}).get('publications', {}).get('edges', [])
    for edge in edges:
        node = edge.get('node', {})
        publications.append({
            'id': node.get('id'),
            'name': node.get('name') or node.get('catalog', {}).get('title', 'Unknown')
        })
    
    return publications


def publish_product_to_all_channels(product_id):
    """發布商品到所有銷售管道"""
    publications = get_all_publications()
    
    if not publications:
        return {'success': False, 'error': 'No publications found'}
    
    # 建立 input 陣列
    publication_inputs = [{"publicationId": pub['id']} for pub in publications]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    result = graphql_request(mutation, {"id": product_id, "input": publication_inputs})
    
    user_errors = result.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
    if user_errors:
        return {'success': False, 'errors': user_errors}
    
    return {'success': True, 'publications': len(publications)}


def batch_publish_workman_products():
    """批量發布所有 WORKMAN 商品到所有銷售管道"""
    # 取得所有 WORKMAN 商品
    product_ids = fetch_workman_product_ids()
    
    if not product_ids:
        return {'success': False, 'error': 'No WORKMAN products found'}
    
    # 取得所有銷售管道
    publications = get_all_publications()
    
    if not publications:
        return {'success': False, 'error': 'No publications found'}
    
    publication_inputs = [{"publicationId": pub['id']} for pub in publications]
    
    results = {
        'total': len(product_ids),
        'success': 0,
        'failed': 0,
        'errors': []
    }
    
    for product_id in product_ids:
        mutation = """
        mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) {
            userErrors {
              field
              message
            }
          }
        }
        """
        
        result = graphql_request(mutation, {"id": product_id, "input": publication_inputs})
        
        user_errors = result.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
        if user_errors:
            results['failed'] += 1
            results['errors'].append({'id': product_id, 'errors': user_errors})
        else:
            results['success'] += 1
        
        time.sleep(0.1)  # 避免 rate limit
    
    return results


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

def run_test_single():
    """測試單品：爬取一個商品並直接上傳到 Shopify"""
    global scrape_status
    
    scrape_status = {
        "running": True,
        "phase": "testing",
        "progress": 0,
        "total": 1,
        "current_product": "測試單品模式...",
        "products": [],
        "errors": [],
        "jsonl_file": "",
        "bulk_operation_id": "",
        "bulk_status": "",
    }
    
    try:
        # 使用兒童服分類測試
        cat_key = 'kids'
        cat_info = CATEGORIES[cat_key]
        tags = cat_info['tags']
        collection_name = cat_info['collection']
        
        # 取得或建立 Collection
        scrape_status['current_product'] = f"取得/建立 {collection_name}..."
        print(f"[Test] 取得/建立 {collection_name}...")
        collection_id = get_or_create_collection(collection_name)
        
        if not collection_id:
            scrape_status['errors'].append({'error': '無法建立 Collection'})
            scrape_status['running'] = False
            return
        
        # 取得第一個商品連結
        scrape_status['current_product'] = "取得商品連結..."
        print("[Test] 取得第一個商品連結...")
        product_links = fetch_all_product_links(cat_key)
        
        if not product_links:
            scrape_status['errors'].append({'error': '無法取得商品連結'})
            scrape_status['running'] = False
            return
        
        # 只取第一個
        link = product_links[0]
        scrape_status['current_product'] = f"爬取: {link.split('/')[-2]}"
        print(f"[Test] 爬取: {link}")
        
        # 解析商品
        product_data = parse_product_page(link)
        
        if not product_data:
            scrape_status['errors'].append({'error': '解析商品失敗'})
            scrape_status['running'] = False
            return
        
        # 翻譯並建立資料
        scrape_status['current_product'] = f"翻譯: {product_data['title'][:20]}..."
        print(f"[Test] 翻譯: {product_data['title'][:30]}...")
        entry = product_to_jsonl_entry(product_data, tags, cat_key, collection_id)
        
        product_input = entry['productSet']
        
        scrape_status['products'].append({
            'title': product_input['title'],
            'handle': product_input['handle'],
            'variants': len(product_input.get('variants', []))
        })
        
        # 直接用 productSet mutation 上傳（不用 bulk operation）
        scrape_status['current_product'] = "上傳到 Shopify..."
        print("[Test] 直接上傳到 Shopify...")
        
        mutation = """
        mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) {
          productSet(synchronous: $synchronous, input: $input) {
            product {
              id
              title
              handle
              status
              productType
              onlineStoreUrl
              metafields(first: 5) {
                edges {
                  node {
                    namespace
                    key
                    value
                  }
                }
              }
              seo {
                title
                description
              }
              variants(first: 10) {
                edges {
                  node {
                    id
                    sku
                    price
                    taxable
                    inventoryItem {
                      unitCost {
                        amount
                        currencyCode
                      }
                    }
                  }
                }
              }
            }
            userErrors {
              field
              code
              message
            }
          }
        }
        """
        
        load_shopify_token()
        result = graphql_request(mutation, {
            "input": product_input,
            "synchronous": True
        })
        
        # 檢查結果
        product_set = result.get('data', {}).get('productSet', {})
        user_errors = product_set.get('userErrors', [])
        
        if user_errors:
            error_msg = '; '.join([e.get('message', str(e)) for e in user_errors])
            scrape_status['errors'].append({'error': f'上傳失敗: {error_msg}'})
            scrape_status['current_product'] = f"❌ 上傳失敗: {error_msg}"
            print(f"[Test] ❌ 上傳失敗: {user_errors}")
        else:
            product = product_set.get('product', {})
            product_id = product.get('id', '')
            product_title = product.get('title', '')
            product_handle = product.get('handle', '')
            
            # 發布到所有銷售管道
            scrape_status['current_product'] = "發布到銷售管道..."
            print("[Test] 發布到銷售管道...")
            publish_result = publish_product_to_all_channels(product_id)
            
            if publish_result.get('success'):
                scrape_status['current_product'] = f"✅ 測試成功！商品: {product_title}"
                print(f"[Test] ✅ 成功！ID: {product_id}")
                print(f"[Test] 標題: {product_title}")
                print(f"[Test] Handle: {product_handle}")
                print(f"[Test] 類型: {product.get('productType', '')}")
                print(f"[Test] SEO: {product.get('seo', {})}")
                print(f"[Test] 發布到 {publish_result.get('publications', 0)} 個銷售管道")
                
                # 記錄詳細結果
                scrape_status['test_result'] = {
                    'id': product_id,
                    'title': product_title,
                    'handle': product_handle,
                    'productType': product.get('productType', ''),
                    'seo': product.get('seo', {}),
                    'metafields': product.get('metafields', {}),
                    'variants': product.get('variants', {}),
                    'published': publish_result.get('publications', 0)
                }
            else:
                scrape_status['current_product'] = f"⚠️ 商品已建立但發布失敗"
                scrape_status['errors'].append({'error': f'發布失敗: {publish_result}'})
        
        scrape_status['progress'] = 1
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
        scrape_status['current_product'] = f"❌ 錯誤: {str(e)}"
        print(f"[Test] ❌ 錯誤: {e}")
        import traceback
        traceback.print_exc()
    finally:
        scrape_status['running'] = False


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
            collection_name = cat_info['collection']
            
            # 取得或建立 Collection
            scrape_status['current_product'] = f"正在取得/建立 {collection_name} 商品系列..."
            print(f"[Collection] 取得/建立 {collection_name}...")
            collection_id = get_or_create_collection(collection_name)
            
            if not collection_id:
                error_msg = f"無法取得/建立 {collection_name} 商品系列"
                print(f"[ERROR] {error_msg}")
                scrape_status['errors'].append({'error': error_msg})
                continue
            
            scrape_status['current_product'] = f"正在取得 {collection_name} 商品連結..."
            print(f"[DEBUG] 開始取得 {collection_name} 商品連結...")
            
            product_links = fetch_all_product_links(cat_key)
            
            print(f"[DEBUG] 取得 {len(product_links)} 個商品連結")
            
            if not product_links:
                error_msg = f"{collection_name} 取得 0 個商品連結，可能是網路問題或網站結構變更"
                print(f"[ERROR] {error_msg}")
                scrape_status['errors'].append({'error': error_msg})
                scrape_status['current_product'] = error_msg
                continue
            
            scrape_status['total'] += len(product_links)
            scrape_status['current_product'] = f"找到 {len(product_links)} 個商品，開始處理..."
            
            for idx, link in enumerate(product_links):
                scrape_status['progress'] += 1
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {link.split('/')[-2]}"
                
                product_data = parse_product_page(link)
                
                if not product_data:
                    scrape_status['errors'].append({'url': link, 'error': '解析失敗'})
                    continue
                
                try:
                    print(f"[翻譯] {product_data['title'][:30]}...")
                    entry = product_to_jsonl_entry(product_data, tags, cat_key, collection_id)
                    all_jsonl_entries.append(entry)
                    
                    scrape_status['products'].append({
                        'title': entry['productSet']['title'],
                        'handle': entry['productSet']['handle'],
                        'variants': len(entry['productSet'].get('variants', []))
                    })
                    print(f"[OK] {entry['productSet']['title'][:30]}")
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
        <h3>🔗 連線測試</h3>
        <p>爬取前先測試是否能連接到 workman.jp</p>
        <button class="btn btn-check" onclick="testConnection()">🔗 測試連線 workman.jp</button>
        <button class="btn btn-check" onclick="testProductParse()">🔍 測試商品頁面解析</button>
        <button class="btn btn-check" onclick="testShopify()">🔗 測試連線 Shopify</button>
    </div>
    
    <div class="card" style="border: 2px solid #28a745; background: #f0fff4;">
        <h3>🧪 測試單品（快速驗證）</h3>
        <p>只爬取<strong>一個商品</strong>並直接上傳到 Shopify，用於快速測試格式是否正確。</p>
        <button class="btn" style="background:#28a745;color:white;" onclick="testSingle()">🧪 測試單品上傳</button>
        <button class="btn btn-check" onclick="checkTestResult()">📋 查看測試結果</button>
        <div id="testResult" style="margin-top:10px;padding:10px;background:#fff;border-radius:5px;display:none;"></div>
    </div>
    
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
        <div style="margin: 10px 0; padding: 10px; background: #e8f5e9; border-radius: 8px;">
            <label style="cursor: pointer;">
                <input type="checkbox" id="autoPublish" checked style="margin-right: 8px;">
                <strong>上傳完成後自動發布到所有銷售管道</strong>
            </label>
        </div>
        <button class="btn btn-upload" id="uploadBtn" onclick="startUpload()" disabled>📤 批量上傳到 Shopify</button>
        <button class="btn btn-check" onclick="checkStatus()">🔍 檢查上傳狀態</button>
        <button class="btn btn-check" onclick="checkResults()">📋 查看詳細結果</button>
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
        <h3>📢 發布到銷售管道</h3>
        <p>商品建立後，需要發布到銷售管道才會在商店顯示。</p>
        <button class="btn btn-upload" onclick="publishAll()">📢 發布所有 WORKMAN 商品</button>
        <button class="btn btn-check" onclick="getPublications()">📋 查看銷售管道</button>
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
                    // 開始輪詢 bulk operation 狀態
                    setTimeout(pollBulkStatus, 5000);
                });
        }
        
        function pollBulkStatus() {
            fetch('/api/bulk_status')
                .then(r => r.json())
                .then(data => {
                    let status = data.status || 'UNKNOWN';
                    let count = data.objectCount || 0;
                    
                    document.getElementById('status').textContent = `Bulk Operation: ${status}, 處理數: ${count}`;
                    
                    if (status === 'COMPLETED') {
                        log(`✅ 批量上傳完成！共處理 ${count} 個商品`);
                        
                        // 自動發布
                        if (document.getElementById('autoPublish') && document.getElementById('autoPublish').checked) {
                            log('📢 自動發布到所有銷售管道...');
                            publishAll();
                        } else {
                            log('⚠️ 請點擊「📢 發布所有 WORKMAN 商品」按鈕來開啟銷售管道');
                        }
                    } else if (status === 'FAILED' || status === 'CANCELED') {
                        log(`❌ 批量上傳失敗: ${status}`);
                        if (data.errorCode) {
                            log(`錯誤碼: ${data.errorCode}`);
                        }
                    } else if (status === 'RUNNING' || status === 'CREATED') {
                        // 繼續輪詢
                        setTimeout(pollBulkStatus, 3000);
                    }
                })
                .catch(err => {
                    log('❌ 檢查狀態失敗: ' + err);
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
                    if (data.url) {
                        log(`📄 結果 URL: 有`);
                    }
                    
                    // 如果完成了，提示發布
                    if (status === 'COMPLETED') {
                        log('✅ 批量上傳已完成！');
                        if (document.getElementById('autoPublish') && document.getElementById('autoPublish').checked) {
                            log('📢 自動發布中...');
                            publishAll();
                        } else {
                            log('⚠️ 請點擊「📢 發布所有 WORKMAN 商品」按鈕來開啟銷售管道');
                        }
                    }
                });
        }
        
        function checkResults() {
            log('📋 正在取得詳細結果...');
            fetch('/api/bulk_results')
                .then(r => r.json())
                .then(data => {
                    log(`📊 狀態: ${data.status}`);
                    log(`📊 總數: ${data.objectCount}`);
                    
                    if (data.error_count !== undefined) {
                        log(`✅ 成功: ${data.success_count} 個`);
                        log(`❌ 失敗: ${data.error_count} 個`);
                    }
                    
                    if (data.successes && data.successes.length > 0) {
                        log('--- 成功的商品 ---');
                        for (let s of data.successes.slice(0, 5)) {
                            log(`   ✓ ${s.title}`);
                        }
                    }
                    
                    if (data.errors && data.errors.length > 0) {
                        log('--- 錯誤訊息 ---');
                        for (let e of data.errors.slice(0, 5)) {
                            log(`   ❌ ${JSON.stringify(e.errors)}`);
                        }
                    }
                    
                    if (data.fetch_error) {
                        log(`❌ 取得結果失敗: ${data.fetch_error}`);
                    }
                    
                    if (!data.url) {
                        log('⚠️ 沒有結果 URL，可能操作尚未完成');
                    }
                })
                .catch(err => {
                    log('❌ 取得結果失敗: ' + err);
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
        
        function publishAll() {
            if (!confirm('確定要發布所有 WORKMAN 商品到所有銷售管道？')) return;
            
            log('📢 正在發布商品到所有銷售管道...');
            document.getElementById('status').textContent = '正在發布商品...';
            
            fetch('/api/publish_all')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log('❌ 錯誤: ' + data.error);
                    } else {
                        log(`📢 發布完成！成功: ${data.success}, 失敗: ${data.failed}`);
                        if (data.errors && data.errors.length > 0) {
                            log('錯誤詳情: ' + JSON.stringify(data.errors.slice(0, 3)));
                        }
                    }
                    document.getElementById('status').textContent = '發布完成';
                })
                .catch(err => {
                    log('❌ 發布失敗: ' + err);
                });
        }
        
        function getPublications() {
            log('📋 正在查詢銷售管道...');
            fetch('/api/publications')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log('❌ 錯誤: ' + data.error);
                    } else if (data.publications) {
                        log(`📋 找到 ${data.publications.length} 個銷售管道:`);
                        data.publications.forEach(pub => {
                            log(`   - ${pub.name} (${pub.id})`);
                        });
                    }
                });
        }
        
        function testConnection() {
            log('🔗 測試連線 workman.jp...');
            fetch('/api/test_workman')
                .then(r => r.json())
                .then(data => {
                    if (data.homepage && data.homepage.ok) {
                        log('✅ workman.jp 主頁連線成功');
                    } else {
                        log('❌ workman.jp 主頁連線失敗: ' + JSON.stringify(data.homepage));
                    }
                    
                    if (data.kids_page && data.kids_page.ok) {
                        log(`✅ 兒童服分類頁連線成功，找到 ${data.kids_page.goods_links_found || 0} 個商品連結`);
                        if (data.kids_page.first_link) {
                            log(`   第一個連結: ${data.kids_page.first_link}`);
                        }
                    } else {
                        log('❌ 兒童服分類頁連線失敗: ' + JSON.stringify(data.kids_page));
                    }
                })
                .catch(err => {
                    log('❌ 測試失敗: ' + err);
                });
        }
        
        function testProductParse() {
            log('🔍 測試商品頁面解析...');
            fetch('/api/test_product')
                .then(r => r.json())
                .then(data => {
                    log('📄 測試 URL: ' + data.url);
                    log('   HTTP 狀態: ' + data.status);
                    
                    if (data.title_found) {
                        log('   ✅ 標題: ' + data.title);
                    } else {
                        log('   ❌ 找不到標題 (block-goods-name)');
                        if (data.h1_found) {
                            log('   📝 備用 h1: ' + data.h1_text);
                        }
                    }
                    
                    if (data.price_elem_found) {
                        log('   ✅ 價格: ' + data.price_text);
                    } else {
                        log('   ❌ 找不到價格 (block-goods-price)');
                        if (data.price_any_found) {
                            log('   📝 備用價格: ' + data.price_any_text);
                        }
                    }
                    
                    if (data.manage_code_dt_found) {
                        log('   ✅ 管理番號: ' + data.manage_code);
                    } else {
                        log('   ❌ 找不到管理番號 (dt 管理番号)');
                        if (data.manage_code_from_url) {
                            log('   📝 從 URL 取得: ' + data.manage_code_from_url);
                        }
                    }
                    
                    if (data.relevant_classes && data.relevant_classes.length > 0) {
                        log('   📋 相關 class: ' + data.relevant_classes.slice(0, 10).join(', '));
                    }
                })
                .catch(err => {
                    log('❌ 測試失敗: ' + err);
                });
        }
        
        function testShopify() {
            log('🔗 測試連線 Shopify...');
            fetch('/api/test')
                .then(r => r.json())
                .then(data => {
                    if (data.data && data.data.shop) {
                        log('✅ Shopify 連線成功: ' + data.data.shop.name);
                    } else if (data.errors) {
                        log('❌ Shopify 連線失敗: ' + JSON.stringify(data.errors));
                    } else {
                        log('⚠️ Shopify 回應: ' + JSON.stringify(data));
                    }
                })
                .catch(err => {
                    log('❌ 測試失敗: ' + err);
                });
        }
        
        function testSingle() {
            if (!confirm('將爬取一個兒童服商品並直接上傳到 Shopify，確定要測試？')) return;
            
            log('🧪 開始測試單品上傳...');
            document.getElementById('status').textContent = '測試單品模式...';
            document.getElementById('testResult').style.display = 'none';
            
            fetch('/api/test_single')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log('❌ ' + data.error);
                    } else {
                        log('🧪 測試已開始，請等待...');
                        pollTestStatus();
                    }
                })
                .catch(err => {
                    log('❌ 測試啟動失敗: ' + err);
                });
        }
        
        function pollTestStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('status').textContent = data.current_product || '處理中...';
                    
                    if (data.running) {
                        setTimeout(pollTestStatus, 1000);
                    } else {
                        // 測試完成，顯示結果
                        checkTestResult();
                    }
                });
        }
        
        function checkTestResult() {
            log('📋 查詢測試結果...');
            fetch('/api/test_result')
                .then(r => r.json())
                .then(data => {
                    const resultDiv = document.getElementById('testResult');
                    resultDiv.style.display = 'block';
                    
                    if (data.errors && data.errors.length > 0) {
                        resultDiv.innerHTML = '<strong style="color:red;">❌ 測試失敗:</strong><br>' + 
                            data.errors.map(e => e.error || JSON.stringify(e)).join('<br>');
                        log('❌ 測試失敗: ' + JSON.stringify(data.errors));
                    } else if (data.test_result && data.test_result.id) {
                        const r = data.test_result;
                        
                        // 取得第一個 variant 的資訊
                        let variantInfo = '(無)';
                        if (r.variants && r.variants.edges && r.variants.edges.length > 0) {
                            const v = r.variants.edges[0].node;
                            const cost = v.inventoryItem?.unitCost?.amount || '(空)';
                            const currency = v.inventoryItem?.unitCost?.currencyCode || '';
                            const taxable = v.taxable === false ? '❌ 不課稅' : '✅ 課稅';
                            variantInfo = `SKU: ${v.sku}, 價格: ${v.price}, 成本: ${cost} ${currency}, ${taxable}`;
                        }
                        
                        resultDiv.innerHTML = `
                            <strong style="color:green;">✅ 測試成功！</strong><br>
                            <table style="width:100%;border-collapse:collapse;margin-top:10px;">
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>ID</strong></td><td style="padding:5px;border:1px solid #ddd;">${r.id}</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>標題</strong></td><td style="padding:5px;border:1px solid #ddd;">${r.title}</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>Handle</strong></td><td style="padding:5px;border:1px solid #ddd;">${r.handle}</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>商品類型</strong></td><td style="padding:5px;border:1px solid #ddd;">${r.productType || '(空)'}</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>SEO 標題</strong></td><td style="padding:5px;border:1px solid #ddd;">${r.seo?.title || '(空)'}</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>SEO 描述</strong></td><td style="padding:5px;border:1px solid #ddd;">${(r.seo?.description || '(空)').substring(0, 80)}...</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>Variant (第1個)</strong></td><td style="padding:5px;border:1px solid #ddd;">${variantInfo}</td></tr>
                                <tr><td style="padding:5px;border:1px solid #ddd;"><strong>銷售管道</strong></td><td style="padding:5px;border:1px solid #ddd;">${r.published} 個</td></tr>
                            </table>
                            <p style="margin-top:10px;">👉 <a href="https://admin.shopify.com/store/goyoulink/products" target="_blank">前往 Shopify 後台查看</a></p>
                        `;
                        log('✅ 測試成功！商品: ' + r.title);
                    } else {
                        resultDiv.innerHTML = '<strong>⏳ 尚無測試結果</strong><br>狀態: ' + (data.current_product || '等待中');
                    }
                })
                .catch(err => {
                    log('❌ 查詢失敗: ' + err);
                });
        }
        
        function resetTracking() {
            lastProductCount = 0;
            lastProgress = 0;
            lastPhase = '';
            lastErrorCount = 0;
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
            if (data.errors.length > lastErrorCount) {
                let newErrors = data.errors.slice(lastErrorCount);
                for (let err of newErrors) {
                    if (err.error) {
                        log(`❌ ${err.error}`);
                    } else if (err.url) {
                        log(`❌ 失敗: ${err.url.split('/').pop()}`);
                    }
                }
                lastErrorCount = data.errors.length;
            }
        }
        
        let lastErrorCount = 0;
        
        function log(msg) {
            let logDiv = document.getElementById('log');
            let time = new Date().toLocaleTimeString();
            // 避免重複訊息（只檢查最近 50 行）
            let recentLog = logDiv.innerHTML.substring(0, 5000);
            if (!recentLog.includes(msg.substring(0, 50))) {
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


@app.route('/api/test_single')
def api_test_single():
    """測試單品：爬取一個商品並直接上傳"""
    if scrape_status['running']:
        return jsonify({'error': '正在執行中'})
    
    thread = threading.Thread(target=run_test_single)
    thread.start()
    
    return jsonify({'started': True, 'mode': 'test_single'})


@app.route('/api/test_result')
def api_test_result():
    """取得測試單品的詳細結果"""
    return jsonify({
        'running': scrape_status.get('running', False),
        'phase': scrape_status.get('phase', ''),
        'current_product': scrape_status.get('current_product', ''),
        'errors': scrape_status.get('errors', []),
        'test_result': scrape_status.get('test_result', {})
    })


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


@app.route('/api/bulk_results')
def api_bulk_results():
    """取得 Bulk Operation 的詳細結果"""
    results = get_bulk_operation_results()
    return jsonify(results)


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


@app.route('/api/publish_all')
def api_publish_all():
    """批量發布所有 WORKMAN 商品到所有銷售管道"""
    if scrape_status['running']:
        return jsonify({'error': '正在執行中'})
    
    try:
        results = batch_publish_workman_products()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/publications')
def api_publications():
    """取得所有銷售管道"""
    try:
        publications = get_all_publications()
        return jsonify({'publications': publications})
    except Exception as e:
        return jsonify({'error': str(e)})


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


@app.route('/api/test_workman')
def api_test_workman():
    """測試連線到 workman.jp"""
    results = {}
    
    # 測試主頁
    try:
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=10)
        results['homepage'] = {
            'status': response.status_code,
            'ok': response.status_code == 200
        }
    except Exception as e:
        results['homepage'] = {'error': str(e), 'ok': False}
    
    # 測試兒童服分類頁
    try:
        response = requests.get(SOURCE_URL + '/shop/c/c54/', headers=HEADERS, timeout=10)
        results['kids_page'] = {
            'status': response.status_code,
            'ok': response.status_code == 200
        }
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 找所有連結，篩選出商品連結 (/shop/g/)
            goods_links = []
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                if '/shop/g/' in href and href not in [l.get('href') for l in goods_links]:
                    goods_links.append(link)
            
            results['kids_page']['goods_links_found'] = len(goods_links)
            
            if goods_links:
                results['kids_page']['first_link'] = goods_links[0].get('href', '')
                results['kids_page']['sample_links'] = [l.get('href', '') for l in goods_links[:5]]
    except Exception as e:
        results['kids_page'] = {'error': str(e), 'ok': False}
    
    return jsonify(results)


@app.route('/api/test_product')
def api_test_product():
    """測試解析單一商品頁面"""
    from flask import request
    product_url = request.args.get('url', '')
    
    if not product_url:
        # 預設測試第一個兒童商品
        product_url = SOURCE_URL + '/shop/g/g2300022383210/'
    elif not product_url.startswith('http'):
        product_url = SOURCE_URL + product_url
    
    results = {'url': product_url}
    
    try:
        response = requests.get(product_url, headers=HEADERS, timeout=15)
        results['status'] = response.status_code
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 標題
            title_elem = soup.find('h1', class_='block-goods-name')
            results['title_found'] = title_elem is not None
            if title_elem:
                results['title'] = title_elem.get_text(strip=True)[:50]
            else:
                # 嘗試其他方式
                h1 = soup.find('h1')
                results['h1_found'] = h1 is not None
                if h1:
                    results['h1_text'] = h1.get_text(strip=True)[:50]
            
            # 價格
            price_elem = soup.find('p', class_='block-goods-price')
            results['price_elem_found'] = price_elem is not None
            if price_elem:
                results['price_text'] = price_elem.get_text(strip=True)
            else:
                # 嘗試其他方式
                price_any = soup.find(class_=re.compile(r'price'))
                results['price_any_found'] = price_any is not None
                if price_any:
                    results['price_any_text'] = price_any.get_text(strip=True)[:50]
            
            # 管理番號
            code_dt = soup.find('dt', string='管理番号')
            results['manage_code_dt_found'] = code_dt is not None
            if code_dt:
                code_dd = code_dt.find_next_sibling('dd')
                if code_dd:
                    results['manage_code'] = code_dd.get_text(strip=True)
            
            # 從 URL 取得備用
            match = re.search(r'/g/g(\d+)/', product_url)
            if match:
                results['manage_code_from_url'] = match.group(1)
            
            # 列出頁面上的一些 class
            all_classes = set()
            for tag in soup.find_all(class_=True):
                for c in tag.get('class', []):
                    if 'goods' in c.lower() or 'price' in c.lower() or 'product' in c.lower():
                        all_classes.add(c)
            results['relevant_classes'] = list(all_classes)[:20]
            
    except Exception as e:
        results['error'] = str(e)
    
    return jsonify(results)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
