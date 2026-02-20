"""
WORKMAN 商品爬蟲 + Shopify Bulk Operations 上架工具 v2.3
來源：workman.jp
功能：
1. 爬取 workman.jp 各分類商品
2. 翻譯並產生 JSONL 檔案
3. 使用 Shopify Bulk Operations API 批量上傳
4. v2.2: 缺貨/下架商品直接刪除（不設草稿）
5. v2.3: variant 級別同步 — 刪除缺貨的個別選項（顏色/尺寸）
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
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8', 'Accept-Language': 'ja,en;q=0.9'}

OUT_OF_STOCK_KEYWORDS = ['店舗のみのお取り扱い', 'オンラインストア販売終了', '店舗在庫を確認する', '予約受付は終了', '受付終了', '取り扱いを終了']

os.makedirs(JSONL_DIR, exist_ok=True)

scrape_status = {"running": False, "phase": "", "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}

inventory_sync_status = {"running": False, "phase": "", "progress": 0, "total": 0, "current_product": "",
    "results": {"checked": 0, "in_stock": 0, "out_of_stock": 0, "deleted": 0, "inventory_zeroed": 0, "errors": 0, "page_gone": 0, "variants_deleted": 0},
    "details": [], "errors": []}

def reset_inventory_sync_status():
    global inventory_sync_status
    inventory_sync_status = {"running": False, "phase": "", "progress": 0, "total": 0, "current_product": "",
        "results": {"checked": 0, "in_stock": 0, "out_of_stock": 0, "deleted": 0, "inventory_zeroed": 0, "errors": 0, "page_gone": 0, "variants_deleted": 0},
        "details": [], "errors": []}


def load_shopify_token():
    global SHOPIFY_SHOP, SHOPIFY_ACCESS_TOKEN
    if not SHOPIFY_SHOP: SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP", "")
    if not SHOPIFY_ACCESS_TOKEN: SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")

def graphql_request(query, variables=None):
    load_shopify_token()
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
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
            _collection_id_cache[collection_name] = edge['node']['id']; return edge['node']['id']
    mutation = """mutation createCollection($input: CollectionInput!) { collectionCreate(input: $input) { collection { id title } userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"title": collection_name, "descriptionHtml": f"<p>{collection_name} 商品系列</p>"}})
    c = result.get('data', {}).get('collectionCreate', {}).get('collection')
    if c:
        _collection_id_cache[collection_name] = c['id']; publish_collection_to_all_channels(c['id']); return c['id']
    return None

def publish_collection_to_all_channels(collection_id):
    pids = get_all_publication_ids()
    if not pids: return
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { publishable { availablePublicationsCount { count } } userErrors { field message } } }"""
    graphql_request(mutation, {"id": collection_id, "input": [{"publicationId": p} for p in pids]})

def get_all_publication_ids():
    result = graphql_request('{ publications(first: 20) { edges { node { id name } } } }')
    return [e['node']['id'] for e in result.get('data', {}).get('publications', {}).get('edges', [])]

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
    size_spec_section = f"\n尺寸規格表：\n{size_spec}" if size_spec else ""
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文。

商品名稱（日文）：{title}
商品說明：{description[:1500] if description else ''}{size_spec_section}

請回傳 JSON 格式（不要加 markdown 標記）：
{{"title":"翻譯後的商品名稱（前面加上 WORKMAN）","description":"翻譯後的商品說明（HTML，用<br>換行）","size_spec_translated":"翻譯後的尺寸規格（格式：列1|列2|列3，每行換行分隔）"}}

規則：1. 禁日文 2. 開頭「WORKMAN」3. 尺寸：サイズ→尺寸、着丈→衣長、身幅→身寬、肩幅→肩寬、袖丈→袖長 4. 忽略注意事項和價格 5. 只回傳JSON"""
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是翻譯專家。輸出禁止任何日文。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1500}, timeout=60)
        if r.status_code == 200:
            c = r.json()['choices'][0]['message']['content'].strip()
            if c.startswith('```'): c = c.split('\n', 1)[1]
            if c.endswith('```'): c = c.rsplit('```', 1)[0]
            t = json.loads(c.strip())
            tt = t.get('title', title); td = t.get('description', description); ts = t.get('size_spec_translated', '')
            if contains_japanese(tt): tt = remove_japanese(tt)
            if contains_japanese(td): td = remove_japanese(td)
            if not tt.startswith('WORKMAN'): tt = f"WORKMAN {tt}"
            sh = build_size_table_html(ts) if ts else ''
            if sh: td += '<br><br>' + sh
            return {'success': True, 'title': tt, 'description': td}
        return {'success': False, 'title': f"WORKMAN {title}", 'description': description}
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {'success': False, 'title': f"WORKMAN {title}", 'description': description}

def build_size_table_html(size_spec_text):
    if not size_spec_text: return ''
    lines = [l.strip() for l in size_spec_text.strip().split('\n') if l.strip()]
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


# ========== 爬取函數 ==========

def get_total_pages(category_url):
    url = SOURCE_URL + category_url
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            last_link = soup.find('a', string='最後')
            if last_link and last_link.get('href'):
                m = re.search(r'_p(\d+)', last_link['href'])
                if m: return int(m.group(1))
            pagination = soup.find_all('a', href=re.compile(r'_p\d+'))
            max_page = 1
            for link in pagination:
                m = re.search(r'_p(\d+)', link.get('href', ''))
                if m: max_page = max(max_page, int(m.group(1)))
            if max_page > 1: return max_page
            pager = soup.find('div', class_=re.compile(r'pager|pagination'))
            if pager:
                for link in pager.find_all('a'):
                    t = link.get_text(strip=True)
                    if t.isdigit(): max_page = max(max_page, int(t))
                return max_page
            return 1
    except Exception as e: print(f"[ERROR] 取得總頁數失敗: {e}")
    return 1

def fetch_all_product_links(category_key):
    category = CATEGORIES[category_key]
    base_url = category['url']
    total_pages = get_total_pages(base_url)
    all_links = []
    for page in range(1, total_pages + 1):
        page_url = SOURCE_URL + base_url if page == 1 else SOURCE_URL + base_url.rstrip('/') + f'_p{page}/'
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                for link in soup.find_all('a', href=True):
                    href = link.get('href', '')
                    if '/shop/g/' in href:
                        full_url = (SOURCE_URL + href if href.startswith('/') else href).split('?')[0]
                        if full_url not in all_links: all_links.append(full_url)
            elif r.status_code == 404: break
        except Exception as e: print(f"[ERROR] 頁面 {page}: {e}")
        time.sleep(0.5)
    print(f"[INFO] {category['collection']} 共 {len(all_links)} 個商品")
    return all_links


def parse_color_sizes_from_spec(soup):
    """
    v2.3: 從規格表解析每個顏色對應的尺寸
    回傳 dict: { '顏色名': ['S', 'M', 'L', ...], ... }
    如果無法解析，回傳 None
    """
    color_sizes = {}
    spec_dt = soup.find('dt', string='サイズ・スペック')
    if not spec_dt:
        return None

    spec_dd = spec_dt.find_next_sibling('dd')
    if not spec_dd:
        return None

    tables = spec_dd.find_all('table')
    if not tables:
        return None

    for table in tables:
        rows = table.find_all('tr')
        if not rows:
            continue

        # 第一行的 header 包含顏色名和尺寸
        first_row = rows[0]
        headers = [cell.get_text(strip=True) for cell in first_row.find_all(['th', 'td'])]

        if not headers:
            continue

        # 規格表的結構：
        # 1. 標題行可能是 "顏色名 | | S | M | L | LL | 3L" 
        #    其中第一個 cell 是顏色名（可能跨多行），後面是尺寸
        # 2. 也可能是 "サイズ | S | M | L | LL | 3L"

        # 找出尺寸名稱（通常在第一行 headers）
        # 尺寸格式：S, M, L, LL, 3L, 4L, 5L, 7L, FREE, フリー 等
        size_pattern = re.compile(r'^(S|M|L|LL|3L|4L|5L|7L|FREE|フリー|SS|XS|XL|XXL|\d+(?:\.\d+)?cm?)$', re.IGNORECASE)

        sizes_in_table = []
        size_start_idx = -1
        for idx, h in enumerate(headers):
            if size_pattern.match(h.strip()):
                if size_start_idx == -1:
                    size_start_idx = idx
                sizes_in_table.append(h.strip())

        if not sizes_in_table:
            continue

        # 嘗試從第一個 header cell 提取顏色名
        # 顏色名通常在第一個 cell，可能包含多個顏色（用空格或換行分隔）
        color_text = headers[0] if headers else ''

        # 如果第一個 cell 是像 "リミテッドブラック ライディングライムG プロブラック" 這樣
        # 也可能是 "サイズ" 或空
        if color_text in ('サイズ', '対応サイズ', '対応身長', '対応胸囲', '製品サイズ', ''):
            # 沒有顏色名，這是通用尺寸表
            # 把所有尺寸歸到 '__all__' key
            if '__all__' not in color_sizes:
                color_sizes['__all__'] = set()
            for s in sizes_in_table:
                color_sizes['__all__'].add(s)
        else:
            # 有顏色名 — 可能是多個顏色共用同一個表
            # 例如 "リミテッドブラック ライディングライムG プロブラック"
            # 或 "スノーブラウン スノーミント"
            # 把這些尺寸歸到每個顏色
            # 先嘗試用換行分割
            color_names = [c.strip() for c in re.split(r'\s+', color_text) if c.strip()]
            # 過濾掉可能混入的非顏色文字（如 "上着", "パンツ", 數字等）
            filtered = []
            for cn in color_names:
                # 跳過明顯不是顏色名的
                if cn in ('上着', 'パンツ', '単位（cm）', '※', '胸囲', '着丈', '肩幅', '袖丈', '裄丈', 'ヒップ', 'ワタリ幅', '股下'):
                    continue
                # 顏色名通常是片假名/漢字/英文混合，長度 > 1
                if len(cn) > 1:
                    filtered.append(cn)

            if not filtered:
                # 解析不出顏色名，歸到 __all__
                if '__all__' not in color_sizes:
                    color_sizes['__all__'] = set()
                for s in sizes_in_table:
                    color_sizes['__all__'].add(s)
            else:
                for cn in filtered:
                    if cn not in color_sizes:
                        color_sizes[cn] = set()
                    for s in sizes_in_table:
                        color_sizes[cn].add(s)

    # 轉換 set → list
    if color_sizes:
        return {k: sorted(list(v), key=lambda x: ['SS','XS','S','M','L','LL','XL','XXL','3L','4L','5L','7L','FREE'].index(x) if x in ['SS','XS','S','M','L','LL','XL','XXL','3L','4L','5L','7L','FREE'] else 99) for k, v in color_sizes.items()}

    return None


def parse_product_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.text, 'html.parser')
        page_text = soup.get_text()
        for kw in OUT_OF_STOCK_KEYWORDS:
            if kw in page_text: return None
        if '売り切れ' in page_text or '品切れ' in page_text: return None
        if '予約受付は終了' in page_text or '受付終了' in page_text: return None

        title = ''
        te = soup.find('h1', class_='block-goods-name')
        if te: title = te.get_text(strip=True)
        elif soup.find('h1'): title = soup.find('h1').get_text(strip=True)

        price = 0
        pe = soup.find('p', class_='block-goods-price') or soup.find(class_=re.compile(r'price'))
        if pe:
            m = re.search(r'[\d,]+', pe.get_text(strip=True))
            if m: price = int(m.group().replace(',', ''))

        manage_code = ''
        cd = soup.find('dt', string='管理番号')
        if cd:
            dd = cd.find_next_sibling('dd')
            if dd: manage_code = dd.get_text(strip=True)
        if not manage_code:
            m = re.search(r'/g/g(\d+)/', url)
            if m: manage_code = m.group(1)
        if not manage_code: return None
        if price == 0: price = 1500

        description = ''; size_spec = ''
        c1 = soup.find('dl', class_='block-goods-comment1')
        if c1:
            dd = c1.find('dd', class_='js-goods-tabContents')
            if dd:
                for tag in dd.find_all(['script', 'style']): tag.decompose()
                dc = [str(e) for e in dd.children if hasattr(e, 'name') and e.name in ['p', 'div'] and e.get_text(strip=True)]
                description = '\n'.join(dc)
        c2 = soup.find('dl', class_='block-goods-comment2')
        if c2:
            dd = c2.find('dd', class_='js-goods-tabContents')
            if dd:
                table = dd.find('table')
                if table:
                    for row in table.find_all('tr'):
                        size_spec += ' | '.join([c.get_text(strip=True) for c in row.find_all(['th', 'td'])]) + '\n'

        colors = []; images = []
        slider = soup.find('div', class_='js-goods-detail-goods-slider')
        if slider:
            for img in slider.find_all('img', class_='js-zoom'):
                src = img.get('src', '')
                if src:
                    fu = SOURCE_URL + src
                    if '_t1.' in src: images.insert(0, fu)
                    elif fu not in images: images.append(fu)
        gallery = soup.find('ul', class_='js-goods-detail-gallery-slider')
        if gallery:
            for item in gallery.find_all('li', class_='block-goods-gallery--color-variation-src'):
                ce = item.find('p', class_='block-goods-detail--color-variation-goods-color-name')
                if ce:
                    c = ce.get_text(strip=True)
                    if c and c not in colors: colors.append(c)
        if not colors: colors = ['標準']

        sizes = []
        sd = soup.find('dt', string='サイズ・スペック')
        if sd:
            sdd = sd.find_next_sibling('dd')
            if sdd:
                table = sdd.find('table')
                if table:
                    fr = table.find('tr')
                    if fr:
                        for th in fr.find_all('th')[1:]:
                            s = th.get_text(strip=True)
                            if s and s not in sizes: sizes.append(s)
        if not sizes: sizes = ['FREE']

        # v2.3: 解析每個顏色對應的可用尺寸
        color_sizes = parse_color_sizes_from_spec(soup)

        images = list(dict.fromkeys(images))[:10]
        if not images and manage_code: images.append(f"{SOURCE_URL}/img/goods/L/{manage_code}_t1.jpg")
        return {'url': url, 'title': title, 'price': price, 'manage_code': manage_code,
                'description': description, 'size_spec': size_spec, 'colors': colors, 'sizes': sizes, 'images': images,
                'color_sizes': color_sizes}
    except Exception as e:
        print(f"[ERROR] 解析失敗 {url}: {e}"); return None


# ========== v2.3: variant 可用性判斷 ==========

def get_available_variants(product_data):
    """
    v2.3: 根據 color_sizes 決定哪些 顏色+尺寸 組合應該建立
    回傳: list of (color, size) tuples
    如果 color_sizes 為 None（無法解析規格表），則回傳所有組合
    """
    colors = product_data['colors']
    sizes = product_data['sizes']
    color_sizes = product_data.get('color_sizes')

    has_color = len(colors) > 1 or (len(colors) == 1 and colors[0] != '標準')
    has_size = len(sizes) > 1 or (len(sizes) == 1 and sizes[0] != 'FREE')

    if not color_sizes:
        # 無法解析規格表 → 回傳所有組合
        if has_color and has_size:
            return [(c, s) for c in colors for s in sizes]
        elif has_color:
            return [(c, None) for c in colors]
        elif has_size:
            return [(None, s) for s in sizes]
        else:
            return [(None, None)]

    available = []

    if has_color and has_size:
        for c in colors:
            # 找出這個顏色對應的可用尺寸
            available_sizes_for_color = None

            # 精確匹配
            if c in color_sizes:
                available_sizes_for_color = color_sizes[c]
            else:
                # 模糊匹配：顏色名可能是子字串
                for cs_key, cs_sizes in color_sizes.items():
                    if cs_key == '__all__':
                        continue
                    if c in cs_key or cs_key in c:
                        available_sizes_for_color = cs_sizes
                        break

            # 如果找不到特定顏色的尺寸，使用 __all__
            if available_sizes_for_color is None and '__all__' in color_sizes:
                available_sizes_for_color = color_sizes['__all__']

            # 如果還是找不到，用所有尺寸（保守策略）
            if available_sizes_for_color is None:
                available_sizes_for_color = sizes

            for s in sizes:
                if s in available_sizes_for_color:
                    available.append((c, s))

    elif has_color:
        # 只有顏色，沒有尺寸選項
        for c in colors:
            available.append((c, None))
    elif has_size:
        # 只有尺寸，沒有顏色選項
        all_available = color_sizes.get('__all__', sizes)
        for s in sizes:
            if s in all_available:
                available.append((None, s))
    else:
        available.append((None, None))

    return available


# ========== JSONL 生成 ==========

def product_to_jsonl_entry(product_data, tags, category_key, collection_id, existing_product_id=None):
    PRODUCT_TYPES = {'work': 'WORKMAN 作業服', 'mens': 'WORKMAN 男裝', 'womens': 'WORKMAN 女裝', 'kids': 'WORKMAN 兒童'}
    product_type = PRODUCT_TYPES.get(category_key, 'WORKMAN')
    translated = translate_with_chatgpt(product_data['title'], product_data['description'], product_data.get('size_spec', ''))
    title = translated['title']; description = translated['description']
    for pat in [r'<a[^>]*>.*?</a>', r'[^<>]*\d+[,，]?\d*\s*日圓[^<>]*', r'[^<>]*\d+[,，]?\d*\s*円[^<>]*',
                r'[^<>]*\d+%\s*OFF[^<>]*', r'[^<>]*降價[^<>]*', r'[^<>]*大幅[^<>]*',
                r'[^<>]*注意事項[^<>]*', r'[^<>]*請注意[^<>]*', r'[^<>]*敬請諒解[^<>]*',
                r'[^<>]*敬請見諒[^<>]*', r'[^<>]*※[^<>]*']:
        description = re.sub(pat, '', description, flags=re.IGNORECASE)
    description = re.sub(r'<p>\s*</p>', '', description)
    description = re.sub(r'<br\s*/?>\s*<br\s*/?>', '<br>', description)
    description = re.sub(r'^\s*(<br\s*/?>)+', '', description)
    description = re.sub(r'(<br\s*/?>)+\s*$', '', description)
    description = re.sub(r'\n\s*\n', '\n', description).strip()
    description += "\n<br><br>\n<p><strong>【請注意以下事項】</strong></p>\n<p>※不接受退換貨</p>\n<p>※開箱請全程錄影</p>\n<p>※因庫存有限，訂購時間不同可能會出現缺貨情況。</p>\n"

    mc = product_data['manage_code']; cost = product_data['price']
    colors = product_data['colors']; sizes = product_data['sizes']
    images = product_data['images']; source_url = product_data['url']
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)

    # v2.3: 只建立有貨的 variant 組合
    available_variants = get_available_variants(product_data)
    if not available_variants:
        print(f"[跳過] 所有選項都缺貨: {mc}")
        return None

    # 從 available_variants 重新建立 colors/sizes 列表（只包含有貨的）
    available_colors = sorted(set(c for c, s in available_variants if c is not None), key=lambda x: colors.index(x) if x in colors else 99)
    available_sizes = sorted(set(s for c, s in available_variants if s is not None), key=lambda x: sizes.index(x) if x in sizes else 99)

    has_color = bool(available_colors)
    has_size = bool(available_sizes)

    product_options = []
    if has_color: product_options.append({"name": "顏色", "values": [{"name": c} for c in available_colors]})
    if has_size: product_options.append({"name": "尺寸", "values": [{"name": s} for s in available_sizes]})

    image_list = images[:10]; first_image = image_list[0] if image_list else None
    files = [{"originalSource": u, "contentType": "IMAGE"} for u in image_list]
    vf = {"originalSource": first_image, "contentType": "IMAGE"} if first_image else None

    variants = []
    for color, size in available_variants:
        sku_parts = [mc]
        option_values = []
        if color:
            sku_parts.append(color)
            option_values.append({"optionName": "顏色", "name": color})
        if size:
            sku_parts.append(size)
            option_values.append({"optionName": "尺寸", "name": size})

        v = {"price": selling_price, "sku": '-'.join(sku_parts), "inventoryPolicy": "CONTINUE", "taxable": False, "inventoryItem": {"cost": cost}}
        if option_values:
            v["optionValues"] = option_values
        if vf:
            v["file"] = vf
        variants.append(v)

    total_combos = len(colors) * len(sizes) if len(colors) > 1 or colors[0] != '標準' else len(sizes)
    if len(available_variants) < total_combos:
        print(f"[v2.3] {mc}: 只建立 {len(available_variants)}/{total_combos} 個 variant（過濾缺貨選項）")

    pi = {"title": title, "descriptionHtml": description, "vendor": "WORKMAN", "productType": product_type,
        "status": "ACTIVE", "handle": f"workman-{mc}", "tags": tags,
        "seo": {"title": f"{title} | WORKMAN 日本代購", "description": f"日本 WORKMAN 官方正品代購。{title}，台灣現貨或日本直送。GOYOUTATI 御用達日本伴手禮專門店。"},
        "metafields": [{"namespace": "custom", "key": "link", "value": source_url, "type": "url"}]}
    if existing_product_id: pi["id"] = existing_product_id
    if collection_id: pi["collections"] = [collection_id]
    if product_options: pi["productOptions"] = product_options
    if variants: pi["variants"] = variants
    if files: pi["files"] = files
    return {"productSet": pi, "synchronous": True}


# ========== Bulk Operations ==========

def create_staged_upload():
    query = """mutation stagedUploadsCreate($input: [StagedUploadInput!]!) { stagedUploadsCreate(input: $input) { stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }"""
    result = graphql_request(query, {"input": [{"resource": "BULK_MUTATION_VARIABLES", "filename": "products.jsonl", "mimeType": "text/jsonl", "httpMethod": "POST"}]})
    if 'errors' in result: return None
    targets = result.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    return targets[0] if targets else None

def upload_jsonl_to_staged(staged_target, jsonl_path):
    params = {p['name']: p['value'] for p in staged_target['parameters']}
    with open(jsonl_path, 'rb') as f:
        r = requests.post(staged_target['url'], data=params, files={'file': ('products.jsonl', f, 'text/jsonl')}, timeout=300)
    return r.status_code in [200, 201, 204]

def run_bulk_mutation(staged_upload_path):
    query = """mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) { bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) { bulkOperation { id status } userErrors { field message } } }"""
    mutation = """mutation call($productSet: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $productSet) { product { id title } userErrors { field message } } }"""
    return graphql_request(query, {"mutation": mutation, "stagedUploadPath": staged_upload_path})

def check_bulk_operation_status(operation_id=None):
    if operation_id:
        query = """query($id: ID!) { node(id: $id) { ... on BulkOperation { id status errorCode createdAt completedAt objectCount fileSize url partialDataUrl } } }"""
        return graphql_request(query, {"id": operation_id}).get('data', {}).get('node', {})
    return graphql_request('{ currentBulkOperation(type: MUTATION) { id status errorCode createdAt completedAt objectCount fileSize url } }').get('data', {}).get('currentBulkOperation', {})

def get_bulk_operation_results():
    status = check_bulk_operation_status()
    results = {'status': status.get('status'), 'objectCount': status.get('objectCount'), 'errorCode': status.get('errorCode'), 'url': status.get('url')}
    if status.get('url'):
        try:
            r = requests.get(status['url'], timeout=30)
            if r.status_code == 200:
                lines = r.text.strip().split('\n')
                results['total_results'] = len(lines)
                errors, successes = [], []
                for line in lines[:50]:
                    try:
                        d = json.loads(line)
                        if 'data' in d and 'productSet' in d.get('data', {}):
                            ps = d['data']['productSet']
                            ue = ps.get('userErrors', [])
                            if ue: errors.append({'errors': ue})
                            elif ps.get('product'): successes.append({'id': ps['product'].get('id'), 'title': ps['product'].get('title', '')[:50]})
                    except: pass
                results.update({'errors': errors[:10], 'successes': successes[:10], 'error_count': len(errors), 'success_count': len(successes)})
        except Exception as e: results['fetch_error'] = str(e)
    return results


# ========== 商品管理 ==========

def get_all_publications():
    result = graphql_request('{ publications(first: 20) { edges { node { id name catalog { title } } } } }')
    return [{'id': e['node'].get('id'), 'name': e['node'].get('name') or e['node'].get('catalog', {}).get('title', 'Unknown')} for e in result.get('data', {}).get('publications', {}).get('edges', [])]

def publish_product_to_all_channels(product_id):
    pubs = get_all_publications()
    if not pubs: return {'success': False}
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { publishable { availablePublicationsCount { count } } userErrors { field message } } }"""
    result = graphql_request(mutation, {"id": product_id, "input": [{"publicationId": p['id']} for p in pubs]})
    ue = result.get('data', {}).get('publishablePublish', {}).get('userErrors', [])
    return {'success': not ue, 'publications': len(pubs)}

def batch_publish_workman_products():
    products = fetch_workman_product_ids()
    if not products: return {'success': False}
    pubs = get_all_publications()
    if not pubs: return {'success': False}
    pi = [{"publicationId": p['id']} for p in pubs]
    results = {'total': len(products), 'success': 0, 'failed': 0}
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
    for p in products:
        r = graphql_request(mutation, {"id": p['id'], "input": pi})
        if r.get('data', {}).get('publishablePublish', {}).get('userErrors', []): results['failed'] += 1
        else: results['success'] += 1
        time.sleep(0.1)
    return results

def fetch_workman_product_ids():
    all_ids = []; cursor = None
    while True:
        if cursor:
            query = 'query($cursor: String) { products(first: 250, after: $cursor, query: "vendor:WORKMAN") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }'
            result = graphql_request(query, {"cursor": cursor})
        else:
            result = graphql_request('{ products(first: 250, query: "vendor:WORKMAN") { edges { node { id title handle status } cursor } pageInfo { hasNextPage } } }')
        for edge in result.get('data', {}).get('products', {}).get('edges', []):
            n = edge['node']
            all_ids.append({'id': n['id'], 'title': n['title'], 'handle': n['handle'], 'status': n.get('status', '')})
            cursor = edge['cursor']
        if not result.get('data', {}).get('products', {}).get('pageInfo', {}).get('hasNextPage', False): break
        time.sleep(0.5)
    return all_ids

def delete_product(product_id):
    """v2.2: 刪除商品"""
    mutation = """mutation productDelete($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }"""
    result = graphql_request(mutation, {"input": {"id": product_id}})
    errors = result.get('data', {}).get('productDelete', {}).get('userErrors', [])
    if errors:
        print(f"[刪除失敗] {product_id}: {errors}")
        return False
    print(f"[已刪除] {product_id}")
    return True

def set_product_active(product_id):
    mutation = """mutation productUpdate($input: ProductInput!) { productUpdate(input: $input) { product { id status } userErrors { field message } } }"""
    return not graphql_request(mutation, {"input": {"id": product_id, "status": "ACTIVE"}}).get('data', {}).get('productUpdate', {}).get('userErrors', [])

def zero_variant_inventory(inventory_item_id, location_id):
    mutation = """mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) { inventorySetQuantities(input: $input) { inventoryAdjustmentGroup { reason } userErrors { field message } } }"""
    return not graphql_request(mutation, {"input": {"reason": "correction", "name": "available", "quantities": [{"inventoryItemId": inventory_item_id, "locationId": location_id, "quantity": 0}]}}).get('data', {}).get('inventorySetQuantities', {}).get('userErrors', [])

def update_existing_product_price(product_id, product_data):
    cost = product_data['price']
    selling_price = calculate_selling_price(cost, DEFAULT_WEIGHT)
    result = graphql_request(f'{{ product(id: "{product_id}") {{ variants(first: 100) {{ edges {{ node {{ id sku }} }} }} }} }}')
    variants = result.get('data', {}).get('product', {}).get('variants', {}).get('edges', [])
    for v in variants:
        graphql_request("""mutation productVariantUpdate($input: ProductVariantInput!) { productVariantUpdate(input: $input) { productVariant { id } userErrors { field message } } }""",
            {"input": {"id": v['node']['id'], "price": str(selling_price)}})
        time.sleep(0.1)
    return len(variants)


# ========== v2.3: Variant 級別管理 ==========

def get_product_variants_graphql(product_id):
    """取得商品的所有 variants（GraphQL 版）"""
    result = graphql_request(f'{{ product(id: "{product_id}") {{ variants(first: 100) {{ edges {{ node {{ id title selectedOptions {{ name value }} sku }} }} }} }} }}')
    variants = []
    for edge in result.get('data', {}).get('product', {}).get('variants', {}).get('edges', []):
        n = edge['node']
        opts = {o['name']: o['value'] for o in n.get('selectedOptions', [])}
        variants.append({'id': n['id'], 'title': n.get('title', ''), 'sku': n.get('sku', ''), 'options': opts})
    return variants

def delete_variant_graphql(product_id, variant_id):
    """刪除商品的某個 variant（GraphQL 版）"""
    mutation = """mutation productVariantDelete($id: ID!) { productVariantDelete(id: $id) { deletedProductVariantId userErrors { field message } } }"""
    result = graphql_request(mutation, {"id": variant_id})
    errors = result.get('data', {}).get('productVariantDelete', {}).get('userErrors', [])
    if errors:
        print(f"[variant 刪除失敗] {variant_id}: {errors}")
        return False
    return True

def sync_product_variants(product_id, product_title, available_variant_keys):
    """
    v2.3: 比對商品的 variants，刪除沒貨的顏色/尺寸組合
    available_variant_keys: set of (color, size) 或 (color,) 或 (size,) 的 key 組合
        例如: {('リミテッドブラック', 'M'), ('ライディングライムG', 'L'), ...}
    回傳 {"kept": N, "deleted": N}
    """
    result = {"kept": 0, "deleted": 0}
    variants = get_product_variants_graphql(product_id)

    if not variants:
        return result

    if len(variants) <= 1:
        # 只剩一個 variant 不能刪（Shopify 規定至少要有一個）
        v = variants[0]
        v_key = _variant_to_key(v)
        if v_key not in available_variant_keys and available_variant_keys:
            # 唯一的 variant 也沒貨 → 刪整個商品
            print(f"[v2.3] 🗑 唯一選項也缺貨，刪除整個商品: {product_title[:30]}")
            delete_product(product_id)
            result["deleted"] = 1
        else:
            result["kept"] = 1
        return result

    for v in variants:
        v_key = _variant_to_key(v)
        if v_key in available_variant_keys or not available_variant_keys:
            result["kept"] += 1
        else:
            # 這個 variant 沒貨 → 刪掉
            if delete_variant_graphql(product_id, v['id']):
                color_val = v['options'].get('顏色', '')
                size_val = v['options'].get('尺寸', '')
                print(f"    ❌ 刪除缺貨選項: {color_val} {size_val}")
                result["deleted"] += 1
            else:
                result["kept"] += 1  # 刪失敗就保留
        time.sleep(0.1)

    # 刪完後如果沒有任何 variant 了，刪整個商品
    if result["kept"] == 0:
        remaining = get_product_variants_graphql(product_id)
        if not remaining:
            print(f"[v2.3] 🗑 所有選項都缺貨，刪除整個商品: {product_title[:30]}")
            delete_product(product_id)

    return result

def _variant_to_key(variant):
    """把 variant 的 options 轉成可比對的 key tuple"""
    color = variant['options'].get('顏色', '')
    size = variant['options'].get('尺寸', '')
    if color and size:
        return (color, size)
    elif color:
        return (color,)
    elif size:
        return (size,)
    else:
        return ('__default__',)

def _available_variants_to_keys(available_variants):
    """把 get_available_variants 的結果轉成 key set，用於比對"""
    keys = set()
    for color, size in available_variants:
        if color and size:
            keys.add((color, size))
        elif color:
            keys.add((color,))
        elif size:
            keys.add((size,))
        else:
            keys.add(('__default__',))
    return keys


def create_delete_jsonl(product_ids):
    jsonl_path = os.path.join(JSONL_DIR, f"delete_workman_{int(time.time())}.jsonl")
    with open(jsonl_path, 'w', encoding='utf-8') as f:
        for p in product_ids: f.write(json.dumps({"input": {"id": p['id']}}, ensure_ascii=False) + '\n')
    return jsonl_path

def run_bulk_delete_mutation(staged_upload_path):
    query = """mutation bulkOperationRunMutation($mutation: String!, $stagedUploadPath: String!) { bulkOperationRunMutation(mutation: $mutation, stagedUploadPath: $stagedUploadPath) { bulkOperation { id status } userErrors { field message } } }"""
    mutation = """mutation call($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }"""
    return graphql_request(query, {"mutation": mutation, "stagedUploadPath": staged_upload_path})

def run_delete_workman_products():
    global scrape_status
    scrape_status = {"running": True, "phase": "deleting", "progress": 0, "total": 0, "current_product": "正在查詢...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}
    try:
        pids = fetch_workman_product_ids()
        if not pids: scrape_status['current_product'] = '沒有商品'; scrape_status['running'] = False; return
        scrape_status['total'] = len(pids)
        jp = create_delete_jsonl(pids); scrape_status['jsonl_file'] = jp
        staged = create_staged_upload()
        if not staged: scrape_status['errors'].append({'error': 'Staged Upload 失敗'}); scrape_status['running'] = False; return
        if not upload_jsonl_to_staged(staged, jp): scrape_status['errors'].append({'error': 'JSONL 上傳失敗'}); scrape_status['running'] = False; return
        sp = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), staged.get('resourceUrl', ''))
        result = run_bulk_delete_mutation(sp)
        ue = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        if ue: scrape_status['errors'].append({'error': str(ue)}); scrape_status['running'] = False; return
        bo = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        scrape_status['bulk_operation_id'] = bo.get('id', ''); scrape_status['bulk_status'] = bo.get('status', '')
        scrape_status['current_product'] = f"批量刪除已啟動！正在刪除 {len(pids)} 個商品..."
    except Exception as e: scrape_status['errors'].append({'error': str(e)})
    finally: scrape_status['running'] = False


# ========== 庫存同步 ==========

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
        return result
    except requests.exceptions.Timeout:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': '連線超時（暫不處理）'}
    except Exception as e:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': f'錯誤: {str(e)}（暫不處理）'}

def check_workman_stock_with_variants(product_url):
    """
    v2.3: 一次爬取，同時檢查庫存 + 解析可用 variant
    回傳: {'available': bool, 'page_exists': bool, 'out_of_stock_reason': str, 'available_variants': list or None}
    """
    result = {'available': True, 'page_exists': True, 'out_of_stock_reason': '', 'available_variants': None}
    if not product_url:
        return {'available': False, 'page_exists': False, 'out_of_stock_reason': '無來源連結', 'available_variants': None}
    try:
        r = requests.get(product_url, headers=HEADERS, timeout=30)
        if r.status_code == 404:
            return {'available': False, 'page_exists': False, 'out_of_stock_reason': '頁面已不存在 (404)', 'available_variants': None}
        if r.status_code != 200:
            return {'available': False, 'page_exists': False, 'out_of_stock_reason': f'HTTP {r.status_code}', 'available_variants': None}

        soup = BeautifulSoup(r.text, 'html.parser')
        pt = soup.get_text()

        # --- 缺貨判斷（與 check_workman_stock 相同邏輯）---
        for kw in OUT_OF_STOCK_KEYWORDS:
            if kw in pt:
                return {'available': False, 'page_exists': True, 'out_of_stock_reason': kw, 'available_variants': None}
        if '売り切れ' in pt or '品切れ' in pt:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '売り切れ / 品切れ', 'available_variants': None}
        if '予約受付は終了' in pt or '受付終了' in pt:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '予約受付終了', 'available_variants': None}

        # --- 有貨：用同一個 soup 解析 variant ---
        colors = []
        gallery = soup.find('ul', class_='js-goods-detail-gallery-slider')
        if gallery:
            for item in gallery.find_all('li', class_='block-goods-gallery--color-variation-src'):
                ce = item.find('p', class_='block-goods-detail--color-variation-goods-color-name')
                if ce:
                    c = ce.get_text(strip=True)
                    if c and c not in colors: colors.append(c)
        if not colors: colors = ['標準']

        sizes = []
        sd = soup.find('dt', string='サイズ・スペック')
        if sd:
            sdd = sd.find_next_sibling('dd')
            if sdd:
                table = sdd.find('table')
                if table:
                    fr = table.find('tr')
                    if fr:
                        for th in fr.find_all('th')[1:]:
                            s = th.get_text(strip=True)
                            if s and s not in sizes: sizes.append(s)
        if not sizes: sizes = ['FREE']

        color_sizes = parse_color_sizes_from_spec(soup)
        product_data = {'colors': colors, 'sizes': sizes, 'color_sizes': color_sizes}
        result['available_variants'] = get_available_variants(product_data)
        return result

    except requests.exceptions.Timeout:
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': '連線超時（暫不處理）', 'available_variants': None}
    except Exception as e:
        print(f"[v2.3] 檢查失敗 {product_url}: {e}")
        return {'available': True, 'page_exists': True, 'out_of_stock_reason': f'錯誤: {str(e)}（暫不處理）', 'available_variants': None}


def run_inventory_sync():
    """v2.3: 庫存同步 — 缺貨商品刪除 + 缺貨 variant 刪除"""
    global inventory_sync_status
    reset_inventory_sync_status()
    inventory_sync_status['running'] = True; inventory_sync_status['phase'] = 'fetching'
    inventory_sync_status['current_product'] = '正在取得 Shopify 商品清單...'
    try:
        products = fetch_workman_products_with_source()
        inventory_sync_status['total'] = len(products)
        if not products: inventory_sync_status['current_product'] = '沒有找到商品'; inventory_sync_status['running'] = False; return
        inventory_sync_status['phase'] = 'checking'
        for idx, product in enumerate(products):
            inventory_sync_status['progress'] = idx + 1
            inventory_sync_status['current_product'] = f"[{idx+1}/{len(products)}] {product['title'][:30]}"
            if product['status'] == 'DRAFT':
                inventory_sync_status['results']['checked'] += 1; continue
            su = product['source_url']
            if not su:
                m = re.search(r'workman-(\d+)', product.get('handle', ''))
                if m: su = f"{SOURCE_URL}/shop/g/g{m.group(1)}/"
                else: inventory_sync_status['results']['checked'] += 1; inventory_sync_status['results']['errors'] += 1; continue

            # v2.3: 檢查庫存 + variant 可用性
            stock = check_workman_stock_with_variants(su)
            inventory_sync_status['results']['checked'] += 1

            if stock['available']:
                inventory_sync_status['results']['in_stock'] += 1

                # v2.3: 比對 variant
                if stock.get('available_variants') is not None:
                    available_keys = _available_variants_to_keys(stock['available_variants'])
                    vr = sync_product_variants(product['id'], product['title'], available_keys)
                    if vr['deleted'] > 0:
                        inventory_sync_status['results']['variants_deleted'] += vr['deleted']
                        print(f"[v2.3] 👕 {product['title'][:30]}: 保留 {vr['kept']} 選項, 刪除 {vr['deleted']} 選項")

                inventory_sync_status['details'].append({'title': product['title'][:40], 'status': 'in_stock', 'source_url': su})
            else:
                inventory_sync_status['results']['out_of_stock'] += 1
                if not stock['page_exists']: inventory_sync_status['results']['page_gone'] += 1
                # 整個商品缺貨 → 刪除
                if delete_product(product['id']):
                    inventory_sync_status['results']['deleted'] += 1
                inventory_sync_status['details'].append({'title': product['title'][:40], 'status': 'out_of_stock', 'reason': stock['out_of_stock_reason'], 'source_url': su})
            time.sleep(1)
        inventory_sync_status['phase'] = 'completed'
        r = inventory_sync_status['results']
        inventory_sync_status['current_product'] = f"✅ 完成！檢查:{r['checked']} 有貨:{r['in_stock']} 缺貨:{r['out_of_stock']} 已刪除商品:{r['deleted']} 已刪除選項:{r['variants_deleted']}"
    except Exception as e:
        inventory_sync_status['errors'].append({'error': str(e)})
        inventory_sync_status['phase'] = 'error'
    finally:
        inventory_sync_status['running'] = False


# ========== 主流程 ==========

def run_test_single():
    global scrape_status
    scrape_status = {"running": True, "phase": "testing", "progress": 0, "total": 1, "current_product": "測試單品...", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}
    try:
        cat_key = 'kids'; cat_info = CATEGORIES[cat_key]
        collection_id = get_or_create_collection(cat_info['collection'])
        if not collection_id: scrape_status['errors'].append({'error': '無法建立 Collection'}); return
        product_links = fetch_all_product_links(cat_key)
        if not product_links: scrape_status['errors'].append({'error': '無法取得商品連結'}); return
        product_data = parse_product_page(product_links[0])
        if not product_data: scrape_status['errors'].append({'error': '解析商品失敗'}); return
        entry = product_to_jsonl_entry(product_data, cat_info['tags'], cat_key, collection_id)
        if not entry: scrape_status['errors'].append({'error': '所有選項都缺貨'}); return
        pi = entry['productSet']
        scrape_status['products'].append({'title': pi['title'], 'handle': pi['handle'], 'variants': len(pi.get('variants', []))})
        mutation = """mutation productSet($input: ProductSetInput!, $synchronous: Boolean!) { productSet(synchronous: $synchronous, input: $input) { product { id title handle status productType seo { title description } variants(first: 10) { edges { node { id sku price taxable inventoryItem { unitCost { amount currencyCode } } } } } } userErrors { field code message } } }"""
        load_shopify_token()
        result = graphql_request(mutation, {"input": pi, "synchronous": True})
        ps = result.get('data', {}).get('productSet', {})
        ue = ps.get('userErrors', [])
        if ue: scrape_status['errors'].append({'error': '; '.join([e.get('message', '') for e in ue])})
        else:
            p = ps.get('product', {})
            pr = publish_product_to_all_channels(p.get('id', ''))
            scrape_status['current_product'] = f"✅ 測試成功！{p.get('title', '')}"
            scrape_status['test_result'] = {'id': p.get('id'), 'title': p.get('title'), 'handle': p.get('handle'), 'productType': p.get('productType', ''), 'seo': p.get('seo', {}), 'variants': p.get('variants', {}), 'published': pr.get('publications', 0)}
        scrape_status['progress'] = 1
    except Exception as e: scrape_status['errors'].append({'error': str(e)})
    finally: scrape_status['running'] = False

def run_scrape(category):
    global scrape_status
    scrape_status = {"running": True, "phase": "scraping", "progress": 0, "total": 0, "current_product": "", "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": ""}
    try:
        cats = ['work', 'mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not cats: scrape_status['errors'].append({'error': f'未知分類: {category}'}); return
        all_entries = []
        for ck in cats:
            ci = CATEGORIES[ck]; cid = get_or_create_collection(ci['collection'])
            if not cid: continue
            links = fetch_all_product_links(ck)
            if not links: continue
            scrape_status['total'] += len(links)
            for link in links:
                scrape_status['progress'] += 1
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {link.split('/')[-2]}"
                pd = parse_product_page(link)
                if not pd: scrape_status['errors'].append({'url': link, 'error': '解析失敗'}); continue
                try:
                    entry = product_to_jsonl_entry(pd, ci['tags'], ck, cid)
                    if entry:
                        all_entries.append(entry)
                        scrape_status['products'].append({'title': entry['productSet']['title'], 'handle': entry['productSet']['handle'], 'variants': len(entry['productSet'].get('variants', []))})
                    else:
                        scrape_status['errors'].append({'url': link, 'error': '所有選項缺貨，跳過'})
                except Exception as e: scrape_status['errors'].append({'url': link, 'error': str(e)})
                time.sleep(0.5)
        if all_entries:
            jp = os.path.join(JSONL_DIR, f"workman_{category}_{int(time.time())}.jsonl")
            with open(jp, 'w', encoding='utf-8') as f:
                for e in all_entries: f.write(json.dumps(e, ensure_ascii=False) + '\n')
            scrape_status['jsonl_file'] = jp
        scrape_status['current_product'] = f"完成！共 {len(all_entries)} 個商品"
    except Exception as e: scrape_status['errors'].append({'error': str(e)})
    finally: scrape_status['running'] = False; scrape_status['phase'] = "completed"

def run_bulk_upload(jsonl_path):
    global scrape_status
    scrape_status['phase'] = 'uploading'; scrape_status['running'] = True
    try:
        staged = create_staged_upload()
        if not staged: scrape_status['errors'].append({'error': 'Staged Upload 失敗'}); return
        if not upload_jsonl_to_staged(staged, jsonl_path): scrape_status['errors'].append({'error': 'JSONL 上傳失敗'}); return
        sp = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), staged.get('resourceUrl', ''))
        result = run_bulk_mutation(sp)
        ue = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
        if ue: scrape_status['errors'].append({'error': str(ue)}); return
        bo = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {})
        scrape_status['bulk_operation_id'] = bo.get('id', ''); scrape_status['bulk_status'] = bo.get('status', '')
    except Exception as e: scrape_status['errors'].append({'error': str(e)})
    finally: scrape_status['running'] = False


def run_full_sync(category='all'):
    """v2.3 智慧同步：新商品→只建有貨variant / 已存在+有貨→更新價格+刪缺貨variant / 缺貨/下架→刪除"""
    global scrape_status
    scrape_status = {"running": True, "phase": "cron_sync", "progress": 0, "total": 0, "current_product": "開始智慧同步...",
        "products": [], "errors": [], "jsonl_file": "", "bulk_operation_id": "", "bulk_status": "", "deleted": 0, "variants_deleted": 0}
    try:
        cats = ['work', 'mens', 'womens', 'kids'] if category == 'all' else [category] if category in CATEGORIES else []
        if not cats: raise Exception(f'未知分類: {category}')

        scrape_status['current_product'] = '取得 Shopify 現有商品...'
        existing_products = fetch_workman_products_with_source()
        existing_handles = {p['handle']: p for p in existing_products}

        new_entries = []; scraped_handles = set()
        updated_count = 0; price_updated_count = 0; variants_deleted_count = 0

        for ck in cats:
            ci = CATEGORIES[ck]; cid = get_or_create_collection(ci['collection'])
            if not cid: continue
            links = fetch_all_product_links(ck)
            if not links: continue
            scrape_status['total'] += len(links)

            for link in links:
                scrape_status['progress'] += 1
                code = link.split('/')[-2] if link.endswith('/') else link.split('/')[-1]
                scrape_status['current_product'] = f"[{scrape_status['progress']}/{scrape_status['total']}] {code}"
                m = re.search(r'/g/g(\d+)/', link)
                mc = m.group(1) if m else ''
                mh = f"workman-{mc}" if mc else ''
                ei = existing_handles.get(mh) if mh else None

                if ei:
                    scraped_handles.add(mh)
                    # v2.3: 使用 check_workman_stock_with_variants
                    stock = check_workman_stock_with_variants(link)
                    if stock['available']:
                        try:
                            r = requests.get(link, headers=HEADERS, timeout=30)
                            if r.status_code == 200:
                                soup = BeautifulSoup(r.text, 'html.parser')
                                pe = soup.find('p', class_='block-goods-price') or soup.find(class_=re.compile(r'price'))
                                if pe:
                                    pm = re.search(r'[\d,]+', pe.get_text(strip=True))
                                    if pm:
                                        update_existing_product_price(ei['id'], {'price': int(pm.group().replace(',', ''))})
                                        price_updated_count += 1
                            if ei.get('status') == 'DRAFT':
                                set_product_active(ei['id'])
                                pids = get_all_publication_ids()
                                if pids:
                                    graphql_request("""mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) { publishablePublish(id: $id, input: $input) { userErrors { field message } } }""",
                                        {"id": ei['id'], "input": [{"publicationId": p} for p in pids]})

                            # v2.3: 比對 variant，刪除缺貨選項
                            if stock.get('available_variants') is not None:
                                available_keys = _available_variants_to_keys(stock['available_variants'])
                                vr = sync_product_variants(ei['id'], ei['title'], available_keys)
                                variants_deleted_count += vr['deleted']
                                if vr['deleted'] > 0:
                                    print(f"[SYNC v2.3] 👕 {ei['title'][:30]}: 保留 {vr['kept']}, 刪除 {vr['deleted']} 選項")

                            updated_count += 1
                        except Exception as e: scrape_status['errors'].append({'url': link, 'error': f'更新失敗: {str(e)}'})
                    else:
                        # 缺貨 → 直接刪除
                        print(f"[SYNC] 🗑 缺貨刪除: {ei['title'][:30]} ({stock['out_of_stock_reason']})")
                        if delete_product(ei['id']):
                            scrape_status['deleted'] = scrape_status.get('deleted', 0) + 1
                    time.sleep(0.3)
                else:
                    # 新商品 — v2.3: 只建立有貨的 variant
                    pd = parse_product_page(link)
                    if not pd: continue
                    if mc: scraped_handles.add(f"workman-{pd['manage_code']}")
                    try:
                        entry = product_to_jsonl_entry(pd, ci['tags'], ck, cid)
                        if entry:
                            new_entries.append(entry)
                            scrape_status['products'].append({'title': entry['productSet']['title'], 'handle': entry['productSet']['handle'], 'variants': len(entry['productSet'].get('variants', []))})
                        else:
                            print(f"[SYNC v2.3] ⏭ 所有選項缺貨，跳過: {pd['manage_code']}")
                    except Exception as e: scrape_status['errors'].append({'url': link, 'error': str(e)})
                    time.sleep(0.5)

        # 新商品批量上傳
        if new_entries:
            jp = os.path.join(JSONL_DIR, f"workman_{category}_{int(time.time())}.jsonl")
            with open(jp, 'w', encoding='utf-8') as f:
                for e in new_entries: f.write(json.dumps(e, ensure_ascii=False) + '\n')
            scrape_status['jsonl_file'] = jp; scrape_status['phase'] = 'uploading'
            scrape_status['current_product'] = f'批量上傳 {len(new_entries)} 個新商品...'
            staged = create_staged_upload()
            if not staged: raise Exception('Staged Upload 失敗')
            if not upload_jsonl_to_staged(staged, jp): raise Exception('JSONL 上傳失敗')
            sp = next((p['value'] for p in staged['parameters'] if p['name'] == 'key'), staged.get('resourceUrl', ''))
            result = run_bulk_mutation(sp)
            if 'errors' in result: raise Exception(f'Bulk 錯誤: {result["errors"]}')
            ue = result.get('data', {}).get('bulkOperationRunMutation', {}).get('userErrors', [])
            if ue: raise Exception(f'userErrors: {ue}')
            scrape_status['bulk_operation_id'] = result.get('data', {}).get('bulkOperationRunMutation', {}).get('bulkOperation', {}).get('id', '')
            scrape_status['current_product'] = '等待上傳完成...'
            for _ in range(120):
                s = check_bulk_operation_status()
                if s.get('status') == 'COMPLETED': break
                elif s.get('status') in ['FAILED', 'CANCELED']: raise Exception(f'失敗: {s.get("status")}')
                time.sleep(5)
            scrape_status['phase'] = 'publishing'; scrape_status['current_product'] = '發布新商品...'
            batch_publish_workman_products()

        # 下架商品直接刪除
        scrape_status['phase'] = 'deleting'
        scrape_status['current_product'] = '清理下架商品...'
        delete_count = scrape_status.get('deleted', 0)
        for handle, pi in existing_handles.items():
            if handle not in scraped_handles and pi.get('status', '') == 'ACTIVE':
                print(f"[SYNC] 🗑 刪除: {handle} - {pi.get('title', '')[:30]}")
                scrape_status['current_product'] = f"刪除: {pi.get('title', '')[:30]}"
                if delete_product(pi['id']):
                    delete_count += 1
                time.sleep(0.2)

        scrape_status['deleted'] = delete_count
        scrape_status['variants_deleted'] = variants_deleted_count
        scrape_status['current_product'] = f"✅ 完成！新商品 {len(new_entries)} 個，更新 {updated_count} 個，刪除商品 {delete_count} 個，刪除選項 {variants_deleted_count} 個"
        scrape_status['phase'] = 'completed'
        return {'success': True, 'new_products': len(new_entries), 'updated': updated_count, 'deleted': delete_count, 'variants_deleted': variants_deleted_count}
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)}); scrape_status['phase'] = 'error'
        return {'success': False, 'error': str(e)}
    finally: scrape_status['running'] = False


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
    jf = request.args.get('file', '')
    if not jf or not os.path.exists(jf): return jsonify({'error': 'JSONL 檔案不存在'})
    if scrape_status['running']: return jsonify({'error': '正在執行中'})
    threading.Thread(target=run_bulk_upload, args=(jf,)).start()
    return jsonify({'started': True, 'file': jf})

@app.route('/api/bulk_status')
def api_bulk_status():
    return jsonify(check_bulk_operation_status(scrape_status.get('bulk_operation_id') or None))

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
        scrape_status['running'] = True; scrape_status['phase'] = 'publishing'
        try:
            r = batch_publish_workman_products()
            scrape_status['current_product'] = f"發布完成！成功: {r.get('success', 0)}, 失敗: {r.get('failed', 0)}"
        except Exception as e: scrape_status['errors'].append({'error': str(e)})
        finally: scrape_status['running'] = False
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
        return jsonify({'count': graphql_request('{ productsCount(query: "vendor:WORKMAN") { count } }').get('data', {}).get('productsCount', {}).get('count', 0)})
    except Exception as e: return jsonify({'error': str(e)})

@app.route('/api/test_workman')
def api_test_workman():
    results = {}
    try:
        r = requests.get(SOURCE_URL, headers=HEADERS, timeout=10)
        results['homepage'] = {'status': r.status_code, 'ok': r.status_code == 200}
    except Exception as e: results['homepage'] = {'error': str(e), 'ok': False}
    try:
        r = requests.get(SOURCE_URL + '/shop/c/c54/', headers=HEADERS, timeout=10)
        results['kids_page'] = {'status': r.status_code, 'ok': r.status_code == 200}
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            gl = [l for l in soup.find_all('a', href=True) if '/shop/g/' in l.get('href', '')]
            results['kids_page']['goods_links_found'] = len(gl)
            if gl: results['kids_page']['first_link'] = gl[0].get('href', '')
    except Exception as e: results['kids_page'] = {'error': str(e), 'ok': False}
    return jsonify(results)

@app.route('/api/test_product')
def api_test_product():
    from flask import request
    pu = request.args.get('url', SOURCE_URL + '/shop/g/g2300022383210/')
    if not pu.startswith('http'): pu = SOURCE_URL + pu
    results = {'url': pu}
    try:
        r = requests.get(pu, headers=HEADERS, timeout=15)
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
            # v2.3: 測試 color_sizes 解析
            color_sizes = parse_color_sizes_from_spec(soup)
            if color_sizes:
                results['color_sizes'] = {k: list(v) if isinstance(v, set) else v for k, v in color_sizes.items()}
    except Exception as e: results['error'] = str(e)
    return jsonify(results)

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
    # v2.3: 回傳包含 variant 資訊的庫存檢查
    result = check_workman_stock_with_variants(url)
    # 把 available_variants 轉成可序列化的格式
    if result.get('available_variants'):
        result['available_variants'] = [(c, s) for c, s in result['available_variants']]
    return jsonify(result)


if __name__ == '__main__':
    print("WORKMAN 爬蟲工具 v2.3")
    app.run(host='0.0.0.0', port=8080, debug=True)
