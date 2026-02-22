"""
Human Made 商品爬蟲 + Shopify 上架工具 v3.0
===========================================
v3.0 重大改版：humanmade.jp 已從 Shopify 遷移到自建平台
- 使用 Playwright (Chromium) 真實瀏覽器繞過 WAF/403 封鎖
- 攔截網路請求自動偵測 API 端點
- 從 HTML 解析商品資料（商品名、價格、顏色、尺寸、圖片等）
- 保留 Shopify 上架邏輯 + 安全機制（防誤刪）
- 支援 GraphQL 批次查詢 + Rate Limit 保護
"""

from flask import Flask, jsonify
import requests
import re
import json
import os
import time
import threading
import asyncio
from urllib.parse import urljoin
from dotenv import load_dotenv

# 載入 .env 檔案
load_dotenv()

app = Flask(__name__)

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
SOURCE_URL = "https://www.humanmade.jp"
# 用日文版取得 JPY 價格
ALL_ITEMS_URL = "https://www.humanmade.jp/all/"
ALL_ITEMS_URL_EN = "https://www.humanmade.jp/en/all/"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
DEFAULT_WEIGHT = 0.5
# 安全機制：來源商品少於此數量時跳過刪除
MIN_PRODUCTS_FOR_CLEANUP = 10

HEADERS_BROWSER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en;q=0.9',
}

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "skipped_exists": 0, "filtered_by_price": 0, "out_of_stock": 0,
    "deleted": 0, "price_updated": 0
}
status_lock = threading.Lock()
_token_loaded = False


# ========== Shopify Token ==========

def load_shopify_token():
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP, _token_loaded
    if _token_loaded:
        return True
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
        _token_loaded = True
        return True
    tf = "shopify_token.json"
    if os.path.exists(tf):
        with open(tf, 'r') as f:
            d = json.load(f)
            SHOPIFY_ACCESS_TOKEN = d.get('access_token', '')
            s = d.get('shop', '')
            if s:
                SHOPIFY_SHOP = s.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
            _token_loaded = True
            return True
    return False


def get_shopify_headers():
    return {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


# ========== Shopify API（含 Rate Limit）==========

def shopify_request(method, url, max_retries=3, **kwargs):
    headers = kwargs.pop('headers', None) or get_shopify_headers()
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            if r.status_code == 429:
                retry_after = float(r.headers.get('Retry-After', 2.0))
                print(f"[RATE LIMIT] 429, waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            call_limit = r.headers.get('X-Shopify-Shop-Api-Call-Limit', '')
            if call_limit:
                parts = call_limit.split('/')
                if len(parts) == 2 and int(parts[1]) - int(parts[0]) < 4:
                    time.sleep(1.0)
            return r
        except Exception as e:
            print(f"[REQUEST ERROR] {e} (attempt {attempt+1})")
            time.sleep(2)
    class FakeResponse:
        status_code = 500
        text = "Max retries exceeded"
        headers = {}
        def json(self): return {}
    return FakeResponse()


def shopify_graphql(query, variables=None):
    url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.headers.get('Retry-After', 2.0)))
                continue
            if r.status_code == 200:
                data = r.json()
                errors = data.get('errors', [])
                if errors and any('Throttled' in str(e) for e in errors):
                    time.sleep(2)
                    continue
                return data
        except Exception as e:
            print(f"[GQL ERROR] {e}")
            time.sleep(2)
    return {'errors': ['Max retries exceeded']}


def calculate_selling_price(cost, weight):
    if not cost or cost <= 0:
        return 0
    weight = weight if weight and weight > 0 else DEFAULT_WEIGHT
    return round((cost + weight * 1250) / 0.7)


# ========== 翻譯 ==========

def translate_with_chatgpt(title, description, max_retries=2):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本服飾品牌商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文/英文）：{title}
商品說明：{description[:1500] if description else ''}

請回傳 JSON 格式（不要加 markdown 標記）：
{{"title":"翻譯後的商品名稱（前面加上 Human Made）","description":"翻譯後的商品說明（HTML，用<br>換行）","page_title":"SEO標題50-60字","meta_description":"SEO描述100字內"}}

規則：1. Human Made 潮流品牌 2. 開頭「Human Made」3. 禁日文 4. 自然流暢 5. 只回傳JSON"""

    for attempt in range(max_retries):
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。輸出禁止任何日文字元。"},
                    {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1000}, timeout=60)
            if r.status_code == 200:
                c = r.json()['choices'][0]['message']['content'].strip()
                if c.startswith('```'):
                    c = c.split('\n', 1)[1]
                if c.endswith('```'):
                    c = c.rsplit('```', 1)[0]
                t = json.loads(c.strip())
                tt = t.get('title', title)
                if not tt.startswith('Human Made'):
                    tt = f"Human Made {tt}"
                return {'success': True, 'title': tt, 'description': t.get('description', description),
                        'page_title': t.get('page_title', ''), 'meta_description': t.get('meta_description', '')}
            elif r.status_code == 429:
                time.sleep(5)
                continue
        except json.JSONDecodeError:
            continue
        except Exception as e:
            print(f"[翻譯錯誤] {e}")
            break

    return {'success': False, 'title': f"Human Made {title}", 'description': description,
            'page_title': '', 'meta_description': ''}


# ========== Playwright 爬蟲核心 ==========

async def scrape_all_products_playwright():
    """
    使用 Playwright 真實瀏覽器爬取 humanmade.jp 所有商品
    策略：
    1. 開啟商品列表頁，攔截網路請求找 API
    2. 滾動載入所有商品卡片
    3. 收集商品連結
    4. 逐一進入商品頁面解析詳細資料
    """
    from playwright.async_api import async_playwright

    products = []
    api_responses = []  # 攔截到的 API 回應

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            locale='ja-JP',
            extra_http_headers={
                'Accept-Language': 'ja,en;q=0.9',
            }
        )

        # === Phase 1: 取得商品列表 ===
        page = await context.new_page()

        # 攔截 API 請求（自動偵測後端 API）
        async def handle_response(response):
            url = response.url
            if any(kw in url for kw in ['products', 'items', 'catalog', 'api', 'graphql']):
                try:
                    ct = response.headers.get('content-type', '')
                    if 'json' in ct:
                        body = await response.json()
                        api_responses.append({'url': url, 'data': body})
                        print(f"[API 攔截] {url[:100]}")
                except:
                    pass

        page.on('response', handle_response)

        print("[Phase 1] 載入商品列表頁面...")
        update_status(current_product="載入商品列表頁面...")

        try:
            await page.goto(ALL_ITEMS_URL, wait_until='networkidle', timeout=60000)
        except Exception as e:
            print(f"[WARNING] networkidle timeout, continuing... {e}")
            await page.wait_for_timeout(5000)

        # === 關閉 Cookie 彈窗 ===
        try:
            cookie_btn = page.locator('text=同意する').first
            if await cookie_btn.is_visible(timeout=3000):
                await cookie_btn.click()
                print("[Phase 1] ✓ 已關閉 Cookie 彈窗")
                await page.wait_for_timeout(1000)
        except:
            pass

        # === 關閉 Global-e 國際運送彈窗 ===
        try:
            await page.evaluate('''() => {
                const ge = document.getElementById('globalePopupWrapper');
                if (ge) ge.remove();
                document.querySelectorAll('[class*="globale"], [id*="globale"]').forEach(el => {
                    if (getComputedStyle(el).position === 'fixed') el.remove();
                });
            }''')
            print("[Phase 1] ✓ 已移除 Global-e 彈窗")
            await page.wait_for_timeout(1000)
        except:
            pass

        # === 點擊 VIEW MORE 載入所有商品 ===
        print("[Phase 1] 點擊 VIEW MORE 載入所有商品...")
        update_status(current_product="點擊 VIEW MORE 載入所有商品...")

        for click_round in range(50):  # 最多點 50 次
            try:
                # 先滾到底部讓按鈕可見
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await page.wait_for_timeout(1500)

                # 清除可能新出現的彈窗
                await page.evaluate('''() => {
                    const ge = document.getElementById('globalePopupWrapper');
                    if (ge) ge.remove();
                }''')

                # 找 VIEW MORE 按鈕
                view_more = page.locator('button.show-more').first
                if await view_more.is_visible(timeout=3000):
                    await view_more.click(force=True)
                    count = await page.evaluate('''() => {
                        const ids = new Set();
                        document.querySelectorAll('a[href]').forEach(a => {
                            const m = a.href.match(/\\/[^\\/]+\\/([A-Z][A-Z0-9]+)\\.html/);
                            if (m) ids.add(m[1]);
                        });
                        return ids.size;
                    }''')
                    print(f"[Phase 1] VIEW MORE 第 {click_round + 1} 次，目前 {count} 個商品")
                    await page.wait_for_timeout(3000)
                else:
                    print(f"[Phase 1] 沒有更多 VIEW MORE 按鈕，載入完成")
                    break
            except Exception as e:
                print(f"[Phase 1] VIEW MORE 點擊結束: {e}")
                break

        # 收集商品連結
        product_links = await page.evaluate('''() => {
            const links = new Map();  // item_id -> category_path
            // 排除非商品頁面（純小寫+連字號的是資訊頁）
            const excludePages = ['about', 'faq', 'shipping', 'payment', 'privacy', 'terms', 'inquiries', 'dealers', 'legal', 'counterfeit', 'maintenance'];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.getAttribute('href') || '';
                // 匹配: /{category}/{ITEM_ID}.html 其中 ITEM_ID 以大寫字母開頭
                const match = href.match(/\\/([^\\/]+)\\/([A-Z][A-Z0-9]+)\\.html/);
                if (match) {
                    const category = match[1];
                    const itemId = match[2];
                    // 排除非商品頁
                    if (!excludePages.some(ex => category.includes(ex)) && !excludePages.some(ex => itemId.toLowerCase().includes(ex))) {
                        links.set(itemId, category);
                    }
                }
            });
            return Array.from(links.entries());  // [[itemId, category], ...]
        }''')

        print(f"[Phase 1] 共找到 {len(product_links)} 個不重複商品 ID")

        # 如果有攔截到 API，嘗試從中取得結構化資料
        if api_responses:
            print(f"[API] 攔截到 {len(api_responses)} 個 API 回應，嘗試解析...")
            for api_resp in api_responses:
                print(f"  - {api_resp['url'][:120]}")
                # 儲存以便除錯
                try:
                    with open('/tmp/humanmade_api_responses.json', 'w') as f:
                        json.dump(api_responses, f, ensure_ascii=False, indent=2, default=str)
                except:
                    pass

        # === Phase 2: 逐一爬取商品詳情 ===
        print(f"\n[Phase 2] 開始爬取 {len(product_links)} 個商品詳情...")
        update_status(total=len(product_links))

        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 5

        for idx, (item_id, category) in enumerate(product_links):
            update_status(progress=idx + 1, current_product=f"爬取商品: {item_id}")

            product_url = f"{SOURCE_URL}/{category}/{item_id}.html"
            product_data = None

            # 重試最多 2 次
            for retry in range(2):
                product_data = await scrape_product_page(page, product_url, item_id)
                if product_data:
                    break
                print(f"  [RETRY] {item_id} 第 {retry+1} 次重試...")
                await page.wait_for_timeout(3000)

            if product_data:
                product_data['category_path'] = category
                products.append(product_data)
                print(f"[{idx+1}/{len(product_links)}] ✓ {item_id}: {product_data.get('title', 'N/A')} - ¥{product_data.get('price_jpy', 0)}")
                consecutive_failures = 0
            else:
                print(f"[{idx+1}/{len(product_links)}] ✗ {item_id}: 解析失敗")
                consecutive_failures += 1

            # 連續失敗太多次 → 重啟瀏覽器
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[⚠️] 連續 {MAX_CONSECUTIVE_FAILURES} 次失敗，重啟瀏覽器...")
                try:
                    await page.close()
                    await context.close()
                    await browser.close()
                except:
                    pass
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    locale='ja-JP',
                    extra_http_headers={'Accept-Language': 'ja,en;q=0.9'}
                )
                page = await context.new_page()
                consecutive_failures = 0
                print(f"[✓] 瀏覽器已重啟")

            # 控速避免被封
            await page.wait_for_timeout(1500)

        await browser.close()

    print(f"\n[完成] 共成功爬取 {len(products)} 個商品")
    return products


async def scrape_product_page(page, url, item_id):
    """解析單一商品頁面"""
    try:
        await page.goto(url, wait_until='networkidle', timeout=30000)
    except Exception as e:
        try:
            # networkidle timeout 但頁面可能已載入
            await page.wait_for_timeout(3000)
        except:
            return None

    # 每次進入商品頁都清除可能的彈窗
    try:
        await page.evaluate('''() => {
            const ge = document.getElementById('globalePopupWrapper');
            if (ge) ge.remove();
        }''')
    except:
        pass

    try:
        data = await page.evaluate('''() => {
            const result = {
                title: '',
                description: '',
                price_text: '',
                colors: [],
                sizes: [],
                images: [],
                item_id: '',
                material: '',
                made_in: '',
                available: true,
                url: window.location.href
            };

            // === 商品名稱 ===
            // 嘗試多種 selector
            const titleSelectors = [
                'h1', '.product-title', '.product-name',
                '[class*="product"] h1', '[class*="item"] h1',
                'h2.product', 'main h1'
            ];
            for (const sel of titleSelectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim().length > 2) {
                    result.title = el.textContent.trim();
                    break;
                }
            }

            // === 價格（取得日圓價格）===
            const priceSelectors = [
                '.prices .price .value', '.sales .value',  // SFCC 常見
                '[class*="price"]', '.product-price', '.price',
                '[data-price]'
            ];
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                const text = el.textContent.trim();
                // 找包含 ¥ 或 NT$ 的元素
                if (text.match(/[¥￥]\\s*[\\d,]+/) && el.children.length < 3) {
                    result.price_text = text;
                    break;
                }
                if (text.match(/NT\\$\\s*[\\d,]+/) && el.children.length < 3) {
                    result.price_text = text;
                    break;
                }
            }

            // === 顏色 ===
            const colorLabels = document.querySelectorAll(
                '[class*="color"] label, [class*="color"] span, ' +
                '[class*="Color"] span, [data-option="color"] span'
            );
            colorLabels.forEach(el => {
                const text = el.textContent.trim();
                if (text && text.length < 30 && !text.match(/^(Color|色|カラー)$/i)) {
                    result.colors.push(text);
                }
            });
            // 也從圖片 alt 嘗試
            if (result.colors.length === 0) {
                document.querySelectorAll('[class*="color"] img, [class*="swatch"] img').forEach(img => {
                    const alt = img.alt || img.title || '';
                    if (alt) result.colors.push(alt.trim());
                });
            }

            // === 尺寸 ===
            const sizeElements = document.querySelectorAll(
                '[class*="size"] button, [class*="size"] label, ' +
                '[class*="Size"] button, [class*="Size"] label, ' +
                '[data-option="size"] button, [data-option="size"] label'
            );
            sizeElements.forEach(el => {
                const text = el.textContent.trim();
                if (text && text.length < 10 && text.match(/^(XXS|XS|S|M|L|XL|2XL|3XL|ONE SIZE|FREE|\\d+)$/i)) {
                    result.sizes.push(text);
                }
            });

            // === 圖片 ===
            // SFCC/Demandware 常見的商品圖片容器
            const imageSelectors = [
                '.product-detail img', '.pdp-main img',
                '.product-images img', '.product-gallery img',
                '[class*="carousel"] img', '[class*="slider"] img',
                '[class*="gallery"] img', '[class*="product"] img',
                '.primary-images img', '.pdp-images img',
                'main img[src]', '.container img[src]'
            ];
            const seenSrc = new Set();
            // 排除的關鍵字
            const excludePatterns = ['icon', 'logo', 'svg', 'pixel', 'tracking', 'spacer', 'blank', 'globale', 'banner', 'badge', 'flag', 'payment'];
            
            for (const sel of imageSelectors) {
                document.querySelectorAll(sel).forEach(img => {
                    let src = img.src || img.dataset.src || img.dataset.lazySrc || img.dataset.highresSrc || '';
                    // 也檢查 srcset
                    if (!src && img.srcset) {
                        const firstSrc = img.srcset.split(',')[0].trim().split(' ')[0];
                        if (firstSrc) src = firstSrc;
                    }
                    if (src && !seenSrc.has(src)) {
                        const srcLower = src.toLowerCase();
                        const isExcluded = excludePatterns.some(p => srcLower.includes(p));
                        // 排除太小的圖（通常是 icon）和 data: URI
                        if (!isExcluded && !src.startsWith('data:') && src.startsWith('http')) {
                            seenSrc.add(src);
                            result.images.push(src);
                        }
                    }
                });
                if (result.images.length >= 3) break;  // 找到 3+ 張就夠了
            }
            // Fallback: 拿所有大圖
            if (result.images.length === 0) {
                document.querySelectorAll('img').forEach(img => {
                    const src = img.src || img.dataset.src || '';
                    if (src && src.startsWith('http') && !seenSrc.has(src)) {
                        const srcLower = src.toLowerCase();
                        const isExcluded = excludePatterns.some(p => srcLower.includes(p));
                        if (!isExcluded && (img.naturalWidth > 200 || img.width > 200 || !img.complete)) {
                            seenSrc.add(src);
                            result.images.push(src);
                        }
                    }
                });
            }
            
            // Debug: 記錄所有 img src 供診斷
            result.debug_all_imgs = [];
            document.querySelectorAll('img').forEach(img => {
                const src = img.src || img.dataset.src || '';
                if (src && src.startsWith('http')) {
                    result.debug_all_imgs.push({
                        src: src.substring(0, 150),
                        w: img.naturalWidth || img.width || 0,
                        cls: (img.className || '').substring(0, 50),
                        parent: (img.parentElement?.className || '').substring(0, 50)
                    });
                }
            });

            // === 商品說明 / ITEM ID / MATERIAL ===
            const bodyText = document.body.innerText;
            const itemIdMatch = bodyText.match(/ITEM\\s*ID[：:]\\s*([A-Z0-9]+)/i);
            if (itemIdMatch) result.item_id = itemIdMatch[1];

            const materialMatch = bodyText.match(/MATERIAL[：:]\\s*([^\\n]+)/i);
            if (materialMatch) result.material = materialMatch[1].trim();

            const madeInMatch = bodyText.match(/MADE\\s+IN\\s+([A-Z]+)/i);
            if (madeInMatch) result.made_in = madeInMatch[1].trim();

            // 商品說明（取 product description 區塊）
            // SFCC 的說明文在 #collapsible-description-1 裡
            const descSelectors = [
                '#collapsible-description-1 p',
                '#collapsible-description-1',
                '.value.content p',
                '.value.content',
                '.product-description .description-text',
                '.product-description .content',
                '.product-description p',
                '.product-description',
                '.description-and-detail .description',
                '.pdp-description',
            ];
            for (const sel of descSelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const text = el.innerText.trim();
                    if (text.length > 20) {
                        result.description = text;
                        break;
                    }
                }
            }
            // Fallback: 從頁面文字中找 ITEM ID 附近的說明文
            if (!result.description) {
                const bodyText = document.body.innerText;
                // 找 ITEM ID 前面的段落作為說明
                const itemIdIdx = bodyText.indexOf('ITEM ID');
                if (itemIdIdx > 0) {
                    // 往前找一段文字
                    const beforeText = bodyText.substring(Math.max(0, itemIdIdx - 500), itemIdIdx).trim();
                    const lines = beforeText.split('\\n').filter(l => l.trim().length > 10);
                    if (lines.length > 0) {
                        result.description = lines.join('\\n');
                    }
                }
            }
            // Debug
            result.debug_desc_length = (result.description || '').length;
            result.debug_desc_preview = (result.description || '').substring(0, 200);

            // === 是否可購買 ===
            const soldOutEl = document.querySelector(
                '[class*="sold-out"], [class*="soldout"], .notify-me'
            );
            if (soldOutEl) {
                result.available = false;
            }
            // 檢查按鈕文字判斷是否為 NOTIFY ME / SOLD OUT
            const allButtons = document.querySelectorAll('button, a.btn, [role="button"]');
            for (const btn of allButtons) {
                const txt = btn.textContent.trim().toUpperCase();
                if (txt.includes('NOTIFY') || txt.includes('SOLD OUT') || txt.includes('品切れ')) {
                    result.available = false;
                    if (txt.includes('NOTIFY')) result.notify_me = true;
                    break;
                }
            }

            return result;
        }''')

        if not data or not data.get('title'):
            return None

        # Debug: 印出圖片診斷資訊
        debug_imgs = data.pop('debug_all_imgs', [])
        debug_desc = data.pop('debug_desc_preview', '')
        debug_desc_len = data.pop('debug_desc_length', 0)
        
        print(f"  [DESC] {item_id}: 說明文 {debug_desc_len} 字 - {debug_desc[:100]}")
        
        if len(data.get('images', [])) == 0:
            print(f"  [IMG DEBUG] {item_id}: 沒抓到商品圖片！頁面上所有 img:")
            for di in debug_imgs[:15]:
                print(f"    {di['src']} (w={di['w']}, class={di['cls']}, parent={di['parent']})")
        else:
            print(f"  [IMG] {item_id}: 抓到 {len(data.get('images', []))} 張圖片")
            for img in data['images'][:3]:
                print(f"    {img[:120]}")

        # 解析價格（從日圓或台幣文字）
        price_jpy = 0
        price_text = data.get('price_text', '')
        # 嘗試提取 JPY
        jpy_match = re.search(r'[¥￥]\s*([\d,]+)', price_text)
        if jpy_match:
            price_jpy = int(jpy_match.group(1).replace(',', ''))
        else:
            # NT$ → 大約換算回 JPY（僅用於價格門檻判斷，實際上架用 JPY）
            ntd_match = re.search(r'NT\$\s*([\d,]+)', price_text)
            if ntd_match:
                ntd = int(ntd_match.group(1).replace(',', ''))
                price_jpy = int(ntd * 4.5)  # 大約匯率

        data['price_jpy'] = price_jpy
        data['item_id'] = data.get('item_id') or item_id
        data['handle'] = item_id  # 用 item_id 當 handle

        # 價格合理性檢查（JPY 通常 > 1000）
        if price_jpy > 0 and price_jpy < 500:
            print(f"  [⚠️ 價格] {item_id}: ¥{price_jpy} 可能不是日圓（NTD?），原始: {price_text}")

        return data

    except Exception as e:
        print(f"[解析錯誤] {url}: {e}")
        return None


# ========== Shopify 工具函數 ==========

def get_collection_products_with_details(collection_id):
    """GraphQL 批次查詢"""
    products_map = {}
    if not collection_id:
        return products_map

    query = """
    query($collectionId: ID!, $cursor: String) {
      collection(id: $collectionId) {
        products(first: 50, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          edges {
            node {
              id handle
              variants(first: 100) {
                edges {
                  node {
                    id price sku
                    selectedOptions { name value }
                    inventoryItem { unitCost { amount } }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    cursor = None
    while True:
        data = shopify_graphql(query, {
            "collectionId": f"gid://shopify/Collection/{collection_id}",
            "cursor": cursor
        })
        collection = data.get('data', {}).get('collection')
        if not collection:
            break
        products_data = collection.get('products', {})
        for edge in products_data.get('edges', []):
            node = edge['node']
            product_id = int(node['id'].split('/')[-1])
            handle = node['handle']
            variants_info = []
            for ve in node.get('variants', {}).get('edges', []):
                vn = ve['node']
                variant_id = int(vn['id'].split('/')[-1])
                cost = None
                uc = (vn.get('inventoryItem') or {}).get('unitCost')
                if uc:
                    cost = uc.get('amount')
                opts = vn.get('selectedOptions', [])
                variants_info.append({
                    'variant_id': variant_id, 'price': vn.get('price'), 'cost': cost,
                    'sku': vn.get('sku'),
                    'option1': opts[0]['value'] if len(opts) > 0 else '',
                    'option2': opts[1]['value'] if len(opts) > 1 else '',
                    'option3': opts[2]['value'] if len(opts) > 2 else ''
                })
            products_map[handle] = {'product_id': product_id, 'variants': variants_info}
        page_info = products_data.get('pageInfo', {})
        if page_info.get('hasNextPage'):
            cursor = page_info['endCursor']
            time.sleep(0.5)
        else:
            break
    print(f"[INFO] Collection 內有 {len(products_map)} 個商品")
    return products_map


def delete_product(product_id):
    r = shopify_request('DELETE', shopify_api_url(f"products/{product_id}.json"))
    if r.status_code == 200:
        print(f"[已刪除] Product ID: {product_id}")
        return True
    return False


def publish_to_channels(resource_type, resource_id):
    """發佈到所有銷售頻道"""
    data = shopify_graphql('{ publications(first:20){ edges{ node{ id name }}}}')
    pubs = data.get('data', {}).get('publications', {}).get('edges', [])
    seen = set()
    uq = []
    for p in pubs:
        if p['node']['name'] not in seen:
            seen.add(p['node']['name'])
            uq.append(p['node'])
    mut = """mutation publishablePublish($id:ID!,$input:[PublicationInput!]!){
        publishablePublish(id:$id,input:$input){userErrors{field message}}}"""
    shopify_graphql(mut, {
        "id": f"gid://shopify/{resource_type}/{resource_id}",
        "input": [{"publicationId": p['id']} for p in uq]
    })


def get_or_create_collection(ct="Human Made"):
    r = shopify_request('GET', shopify_api_url(f'custom_collections.json?title={ct}'))
    if r.status_code == 200:
        for c in r.json().get('custom_collections', []):
            if c['title'] == ct:
                publish_to_channels('Collection', c['id'])
                return c['id']
    r = shopify_request('POST', shopify_api_url('custom_collections.json'),
        json={'custom_collection': {'title': ct, 'published': True}})
    if r.status_code == 201:
        cid = r.json()['custom_collection']['id']
        publish_to_channels('Collection', cid)
        return cid
    return None


def add_product_to_collection(pid, cid):
    return shopify_request('POST', shopify_api_url('collects.json'),
        json={'collect': {'product_id': pid, 'collection_id': cid}}).status_code == 201


# ========== 上架到 Shopify（適配新網站結構）==========

def build_variants_from_product(product_data):
    """從爬取的商品資料建構 Shopify variants"""
    colors = product_data.get('colors', []) or ['Default']
    sizes = product_data.get('sizes', []) or ['ONE SIZE']
    price_jpy = product_data.get('price_jpy', 0)
    weight = DEFAULT_WEIGHT

    variants = []
    options = []

    if len(colors) > 1 or (len(colors) == 1 and colors[0] != 'Default'):
        options.append({'name': 'Color', 'values': colors})
    if len(sizes) > 1 or (len(sizes) == 1 and sizes[0] != 'ONE SIZE'):
        options.append({'name': 'Size', 'values': sizes})

    if not options:
        options = [{'name': 'Title', 'values': ['Default Title']}]
        selling_price = calculate_selling_price(price_jpy, weight)
        variants.append({
            'variant_data': {
                'title': 'Default Title', 'price': f"{selling_price:.2f}",
                'sku': product_data.get('item_id', ''),
                'weight': weight, 'weight_unit': 'kg',
                'inventory_management': None, 'inventory_policy': 'continue',
                'requires_shipping': True, 'option1': 'Default Title'
            },
            'cost': price_jpy
        })
    else:
        # 建構所有顏色 x 尺寸組合
        color_list = colors if any(c != 'Default' for c in colors) else [None]
        size_list = sizes if any(s != 'ONE SIZE' for s in sizes) else [None]

        for color in color_list:
            for size in size_list:
                selling_price = calculate_selling_price(price_jpy, weight)
                sku = product_data.get('item_id', '')
                if color:
                    sku += f"-{color[:3].upper()}"
                if size:
                    sku += f"-{size}"

                vd = {
                    'price': f"{selling_price:.2f}",
                    'sku': sku,
                    'weight': weight, 'weight_unit': 'kg',
                    'inventory_management': None, 'inventory_policy': 'continue',
                    'requires_shipping': True
                }
                opt_idx = 1
                if color:
                    vd[f'option{opt_idx}'] = color
                    opt_idx += 1
                if size:
                    vd[f'option{opt_idx}'] = size

                variants.append({'variant_data': vd, 'cost': price_jpy})

    return options, variants


def upload_to_shopify(product_data, collection_id=None):
    """上架商品到 Shopify"""
    title = product_data.get('title', '')
    description = product_data.get('description', '')
    handle = product_data.get('handle', '')
    item_id = product_data.get('item_id', handle)

    translated = translate_with_chatgpt(title, description)
    options, variants = build_variants_from_product(product_data)
    
    # Shopify 限制：最多 100 個 variants
    if len(variants) > 100:
        print(f"  [⚠️] {handle}: {len(variants)} variants 超過 Shopify 上限 100，截斷")
        variants = variants[:100]

    # 圖片使用 URL
    images = []
    for idx, img_url in enumerate(product_data.get('images', [])[:10]):  # 最多 10 張
        images.append({
            'src': img_url,
            'position': idx + 1,
            'filename': f"humanmade_{handle}_{idx+1}.jpg"
        })

    product_type = ''
    if any(kw in title.upper() for kw in ['JACKET', 'COAT']):
        product_type = 'Outerwear'
    elif any(kw in title.upper() for kw in ['T-SHIRT', 'TEE']):
        product_type = 'T-Shirts'
    elif any(kw in title.upper() for kw in ['HOODIE', 'SWEAT']):
        product_type = 'Sweatshirts'
    elif any(kw in title.upper() for kw in ['SHIRT']):
        product_type = 'Shirts'
    elif any(kw in title.upper() for kw in ['PANTS', 'TROUSER', 'SHORTS']):
        product_type = 'Pants'
    elif any(kw in title.upper() for kw in ['CAP', 'HAT', 'BEANIE']):
        product_type = 'Headwear'
    elif any(kw in title.upper() for kw in ['BAG', 'POUCH', 'TOTE']):
        product_type = 'Bags'

    shopify_product = {'product': {
        'title': translated['title'],
        'body_html': translated['description'],
        'vendor': 'Human Made',
        'product_type': product_type,
        'status': 'active',
        'published': True,
        'handle': f"humanmade-{handle}",
        'options': options,
        'variants': [v['variant_data'] for v in variants],
        'images': images,
        'tags': f"Human Made, 日本, 潮流, 服飾, {product_type}",
        'metafields_global_title_tag': translated['page_title'],
        'metafields_global_description_tag': translated['meta_description'],
        'metafields': [{'namespace': 'custom', 'key': 'link',
                        'value': f"{SOURCE_URL}/{product_data.get('category_path', 'all')}/{handle}.html", 'type': 'url'}]
    }}

    response = shopify_request('POST', shopify_api_url('products.json'), json=shopify_product)

    if response.status_code == 201:
        created = response.json()['product']
        product_id = created['id']
        created_variants = created.get('variants', [])

        # 更新 cost
        for idx, cv in enumerate(created_variants):
            if idx < len(variants):
                shopify_request('PUT', shopify_api_url(f"variants/{cv['id']}.json"),
                    json={'variant': {'id': cv['id'], 'cost': f"{variants[idx]['cost']:.2f}"}})

        if collection_id:
            add_product_to_collection(product_id, collection_id)
        publish_to_channels('Product', product_id)

        return {'success': True, 'product': created, 'translated': translated,
                'variants_count': len(created_variants)}
    else:
        return {'success': False, 'error': response.text}


# ========== Thread-safe 狀態更新 ==========

def update_status(**kwargs):
    with status_lock:
        scrape_status.update(kwargs)


def increment_status(key, value=1):
    with status_lock:
        scrape_status[key] = scrape_status.get(key, 0) + value


# ========== 主流程 ==========

def run_scrape():
    global scrape_status
    try:
        with status_lock:
            scrape_status = {
                "running": True, "progress": 0, "total": 0, "current_product": "",
                "products": [], "errors": [], "uploaded": 0, "skipped": 0,
                "skipped_exists": 0, "filtered_by_price": 0, "out_of_stock": 0,
                "deleted": 0, "price_updated": 0
            }

        # === Step 1: Shopify Collection 設定 ===
        update_status(current_product="設定 Shopify Collection...")
        collection_id = get_or_create_collection("Human Made")

        update_status(current_product="取得 Collection 內現有商品（GraphQL）...")
        collection_products_map = get_collection_products_with_details(collection_id)
        existing_handles = set(collection_products_map.keys())

        # === Step 2: Playwright 爬取所有商品 ===
        update_status(current_product="啟動瀏覽器爬取 humanmade.jp...")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        product_list = loop.run_until_complete(scrape_all_products_playwright())
        loop.close()

        # === 安全機制：來源商品太少則跳過刪除 ===
        source_too_few = len(product_list) < MIN_PRODUCTS_FOR_CLEANUP
        if source_too_few:
            print(f"[⚠️ 安全機制] 來源僅 {len(product_list)} 個商品（門檻 {MIN_PRODUCTS_FOR_CLEANUP}），將跳過刪除")

        update_status(total=len(product_list))
        in_stock_handles = set()

        # === Step 3: 上架/更新商品 ===
        for idx, product in enumerate(product_list):
            update_status(progress=idx + 1)
            handle = product.get('handle', '')
            title = product.get('title', '')
            my_handle = f"humanmade-{handle}"
            price_jpy = product.get('price_jpy', 0)
            is_available = product.get('available', True)

            update_status(current_product=f"處理: {title[:30]}")

            if is_available:
                in_stock_handles.add(my_handle)

            # 已存在的商品 → 跳過（未來可加價格更新）
            if my_handle in existing_handles:
                increment_status('skipped_exists')
                increment_status('skipped')
                continue

            # 價格過濾
            if price_jpy < MIN_PRICE:
                increment_status('filtered_by_price')
                increment_status('skipped')
                continue

            # 無庫存 / 尚未開賣
            if not is_available:
                increment_status('out_of_stock')
                increment_status('skipped')
                continue

            # 上架
            result = upload_to_shopify(product, collection_id)
            if result['success']:
                in_stock_handles.add(my_handle)
                existing_handles.add(my_handle)
                increment_status('uploaded')
                with status_lock:
                    scrape_status['products'].append({
                        'handle': handle,
                        'title': result.get('translated', {}).get('title', title),
                        'original_title': title,
                        'variants_count': result.get('variants_count', 0),
                        'status': 'success'
                    })
            else:
                error_msg = result.get('error', '')[:300]
                print(f"  [上架失敗] {handle}: {error_msg}")
                with status_lock:
                    scrape_status['errors'].append({
                        'handle': handle, 'title': title, 'error': error_msg
                    })
                # 如果是 429 rate limit，多等一下
                if '429' in str(error_msg) or 'throttle' in str(error_msg).lower():
                    print(f"  [RATE LIMIT] 等待 10 秒...")
                    time.sleep(10)
            time.sleep(1.0)  # 每個商品間隔 1 秒（翻譯 + Shopify API）

        # === Step 4: 清理（含安全機制）===
        if source_too_few:
            update_status(current_product="⚠️ 來源商品過少，跳過清理以避免誤刪")
            print(f"[安全機制] 跳過刪除步驟")
        else:
            update_status(current_product="清理下架/缺貨商品...")
            for my_handle, product_info in collection_products_map.items():
                if my_handle not in in_stock_handles:
                    update_status(current_product=f"刪除: {my_handle}")
                    print(f"[刪除] {my_handle} / ID: {product_info['product_id']}")
                    if delete_product(product_info['product_id']):
                        increment_status('deleted')
                    time.sleep(0.5)

        update_status(current_product="完成！")

    except Exception as e:
        import traceback
        traceback.print_exc()
        with status_lock:
            scrape_status['errors'].append({'error': str(e)})
    finally:
        with status_lock:
            scrape_status['running'] = False


# ========== Flask 路由 + 前端 ==========

@app.route('/')
def index():
    token_loaded = load_shopify_token()
    token_status = '<span style="color: green;">✓ 已載入</span>' if token_loaded else '<span style="color: red;">✗ 未設定</span>'
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Human Made 爬蟲工具 v3.0</title>
<style>*{{box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}}h1{{color:#333;border-bottom:2px solid #E74C3C;padding-bottom:10px}}.card{{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}.btn{{background:#E74C3C;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px}}.btn:hover{{background:#C0392B}}.btn:disabled{{background:#ccc;cursor:not-allowed}}.btn-secondary{{background:#3498db}}.btn-warning{{background:#e67e22}}.progress-bar{{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}}.progress-fill{{height:100%;background:linear-gradient(90deg,#E74C3C,#F39C12);transition:width 0.3s}}.status{{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}}.log{{max-height:400px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}}.stats{{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}}.stat{{flex:1;min-width:80px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}}.stat-number{{font-size:24px;font-weight:bold;color:#E74C3C}}.stat-label{{font-size:11px;color:#666;margin-top:5px}}.badge{{display:inline-block;padding:3px 8px;border-radius:3px;font-size:11px;font-weight:bold}}.badge-new{{background:#e74c3c;color:white}}.badge-info{{background:#3498db;color:white}}</style></head>
<body>
<h1>❤️ Human Made 爬蟲工具 <small style="font-size:14px;color:#999;">v3.0</small> <span class="badge badge-new">Playwright</span></h1>
<div class="card">
<h3>⚡ v3.0 重大更新</h3>
<p style="color:#666;font-size:14px;">humanmade.jp 已從 Shopify 遷移到自建平台，本版本使用 Playwright (Chromium) 真實瀏覽器爬取。</p>
<ul style="font-size:14px;color:#555;">
<li>✅ Playwright headless browser 繞過 WAF 封鎖</li>
<li>✅ 自動攔截 API 請求偵測資料端點</li>
<li>✅ 新 URL 格式：<code>/all/HMxxxxxx.html</code></li>
<li>✅ 安全機制：來源商品不足時跳過刪除</li>
</ul>
</div>
<div class="card"><h3>Shopify 連線狀態</h3><p>Token: {token_status}</p>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
<button class="btn btn-warning" onclick="testScrape()">🔍 測試爬取（前 3 個）</button>
<button class="btn" style="background:#27ae60" onclick="testUpload()">🧪 測試上架（前 3 個）</button></div>
<div class="card"><h3>開始爬取</h3>
<p>爬取 www.humanmade.jp 所有商品並上架到 Shopify</p>
<p style="color:#666;font-size:14px;">※ 成本價低於 ¥{MIN_PRICE} 或無庫存的商品將自動跳過</p>
<p style="color:#e67e22;font-size:14px;font-weight:bold;">※ 安全機制：來源商品少於 {MIN_PRODUCTS_FOR_CLEANUP} 個時跳過刪除</p>
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
function log(msg,type=''){{const a=document.getElementById('logArea');const t=new Date().toLocaleTimeString();const c=type==='success'?'#4ec9b0':type==='error'?'#f14c4c':type==='warn'?'#dcdcaa':'#d4d4d4';a.innerHTML+='<div style="color:'+c+'">['+t+'] '+msg+'</div>';a.scrollTop=a.scrollHeight}}
function clearLog(){{document.getElementById('logArea').innerHTML=''}}
async function testShopify(){{log('測試 Shopify 連線...');try{{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ 連線成功！商店: '+d.shop.name,'success');else log('✗ 連線失敗: '+d.error,'error')}}catch(e){{log('✗ '+e.message,'error')}}}}
async function testScrape(){{log('測試爬取 humanmade.jp（前 3 個商品）...');log('⏳ 啟動瀏覽器中，可能需要 30-60 秒...','warn');try{{const r=await fetch('/api/test-scrape',{{timeout:120000}});const d=await r.json();if(d.success){{log('✓ 測試成功！找到 '+d.total_links+' 個商品連結','success');(d.samples||[]).forEach(s=>log('  - '+s.item_id+': '+s.title+' ¥'+s.price_jpy))}}else log('✗ 測試失敗: '+(d.error||'未知錯誤'),'error')}}catch(e){{log('✗ '+e.message,'error')}}}}
async function testUpload(){{log('🧪 測試上架（爬取前 3 個商品 + 上架到 Shopify）...');log('⏳ 啟動瀏覽器 + 翻譯 + 上架，約需 2-3 分鐘...','warn');try{{const r=await fetch('/api/test-upload',{{method:'POST'}});const d=await r.json();if(d.success){{log('========== 測試上架結果 ==========','success');(d.results||[]).forEach(s=>{{if(s.status==='uploaded')log('✓ '+s.item_id+': '+s.title+' ('+s.variants_count+' variants)','success');else log('✗ '+s.item_id+': '+s.status+' '+(s.error||''),'error')}})}}else log('✗ 測試失敗: '+(d.error||'未知錯誤'),'error')}}catch(e){{log('✗ '+e.message,'error')}}}}
async function startScrape(){{clearLog();log('開始爬取流程（Playwright v3.0）...');log('⏳ 啟動 Chromium 瀏覽器...','warn');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';try{{const r=await fetch('/api/start',{{method:'POST'}});const d=await r.json();if(!d.success){{log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}}log('✓ 爬取任務已啟動','success');pollInterval=setInterval(pollStatus,2000)}}catch(e){{log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}}}
async function pollStatus(){{try{{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('priceUpdatedCount').textContent=d.price_updated||0;document.getElementById('skippedCount').textContent=d.skipped;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('outOfStockCount').textContent=d.out_of_stock||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(!d.running&&d.progress>0){{clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;log('========== 爬取完成 ==========','success')}}}}catch(e){{}}}}
</script></body></html>'''


@app.route('/api/status')
def get_status():
    with status_lock:
        return jsonify(dict(scrape_status))


@app.route('/api/start', methods=['GET', 'POST'])
def api_start():
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    threading.Thread(target=run_scrape, daemon=True).start()
    return jsonify({'success': True, 'message': 'Human Made v3.0 爬蟲已啟動'})


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    r = shopify_request('GET', shopify_api_url('shop.json'))
    if r.status_code == 200:
        return jsonify({'success': True, 'shop': r.json()['shop']})
    return jsonify({'success': False, 'error': r.text}), 400


@app.route('/api/test-scrape')
def test_scrape():
    """測試爬取（只爬前 3 個商品）"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def test():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    locale='ja-JP'
                )
                page = await context.new_page()
                await page.goto(ALL_ITEMS_URL, wait_until='networkidle', timeout=60000)
                await page.wait_for_timeout(3000)

                # 關閉 Cookie 彈窗（多種方式嘗試）
                print("[TEST] 嘗試關閉 Cookie 彈窗...")
                cookie_closed = False
                try:
                    btn = page.locator('text=同意する').first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        cookie_closed = True
                        print("[TEST] ✓ 點擊「同意する」關閉 Cookie")
                        await page.wait_for_timeout(1000)
                except:
                    pass
                if not cookie_closed:
                    print("[TEST] ⚠ Cookie 彈窗未找到或已關閉")

                # 關閉 Global-e 國際運送彈窗
                print("[TEST] 嘗試關閉 Global-e 彈窗...")
                try:
                    await page.evaluate('''() => {
                        // 直接移除 Global-e 彈窗
                        const ge = document.getElementById('globalePopupWrapper');
                        if (ge) { ge.remove(); console.log('removed globalePopupWrapper'); }
                        // 也移除其他可能的遮罩
                        document.querySelectorAll('[class*="globale"], [id*="globale"], [class*="overlay"]').forEach(el => {
                            if (el.style.position === 'fixed' || el.style.position === 'absolute' || 
                                getComputedStyle(el).position === 'fixed') {
                                el.remove();
                            }
                        });
                    }''')
                    print("[TEST] ✓ 已移除 Global-e 彈窗")
                    await page.wait_for_timeout(1000)
                except Exception as e:
                    print(f"[TEST] Global-e 處理: {e}")

                # 截圖 debug
                await page.screenshot(path='/tmp/humanmade_test.png')
                print("[TEST] 截圖已存至 /tmp/humanmade_test.png")

                # 測試只用第一頁，不點 VIEW MORE

                # 取得商品連結
                links = await page.evaluate('''() => {
                    const items = new Map();
                    const excludePages = ['about', 'faq', 'shipping', 'payment', 'privacy', 'terms', 'inquiries', 'dealers', 'legal', 'counterfeit', 'maintenance'];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.getAttribute('href') || '';
                        const match = href.match(/\\/([^\\/]+)\\/([A-Z][A-Z0-9]+)\\.html/);
                        if (match) {
                            const category = match[1];
                            const itemId = match[2];
                            if (!excludePages.some(ex => category.includes(ex))) {
                                items.set(itemId, category);
                            }
                        }
                    });
                    return Array.from(items.entries());
                }''')

                samples = []
                for item_id, category in links[:3]:
                    url = f"{SOURCE_URL}/{category}/{item_id}.html"
                    data = await scrape_product_page(page, url, item_id)
                    if data:
                        samples.append({
                            'item_id': item_id,
                            'title': data.get('title', 'N/A'),
                            'price_jpy': data.get('price_jpy', 0),
                            'colors': data.get('colors', []),
                            'sizes': data.get('sizes', []),
                            'images_count': len(data.get('images', [])),
                            'available': data.get('available', False)
                        })
                    await page.wait_for_timeout(1500)

                await browser.close()
                return {'total_links': len(links), 'samples': samples}

        result = loop.run_until_complete(test())
        loop.close()
        return jsonify({'success': True, **result})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-upload', methods=['POST'])
def test_upload():
    """測試上架：爬取前 3 個商品並上架到 Shopify"""
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def do_test_upload():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    locale='ja-JP'
                )
                page = await context.new_page()
                await page.goto(ALL_ITEMS_URL, wait_until='networkidle', timeout=60000)
                await page.wait_for_timeout(3000)

                # 關閉彈窗
                try:
                    btn = page.locator('text=同意する').first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        await page.wait_for_timeout(1000)
                except:
                    pass
                try:
                    await page.evaluate('''() => {
                        const ge = document.getElementById('globalePopupWrapper');
                        if (ge) ge.remove();
                    }''')
                except:
                    pass

                # 取得商品連結（不用 VIEW MORE，首頁就夠了）
                links = await page.evaluate('''() => {
                    const items = new Map();
                    const excludePages = ['about', 'faq', 'shipping', 'payment', 'privacy', 'terms', 'inquiries', 'dealers', 'legal', 'counterfeit', 'maintenance'];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.getAttribute('href') || '';
                        const match = href.match(/\\/([^\\/]+)\\/([A-Z][A-Z0-9]+)\\.html/);
                        if (match) {
                            const category = match[1];
                            const itemId = match[2];
                            if (!excludePages.some(ex => category.includes(ex))) {
                                items.set(itemId, category);
                            }
                        }
                    });
                    return Array.from(items.entries());
                }''')

                # 取前 3 個爬取 + 上架
                collection_id = get_or_create_collection("Human Made")
                results = []

                for item_id, category in links[:3]:
                    url = f"{SOURCE_URL}/{category}/{item_id}.html"
                    print(f"[TEST UPLOAD] 爬取: {item_id}...")
                    data = await scrape_product_page(page, url, item_id)

                    if not data:
                        results.append({'item_id': item_id, 'status': 'scrape_failed'})
                        continue

                    data['category_path'] = category  # 保存類別路徑

                    print(f"[TEST UPLOAD] 上架: {data.get('title', item_id)} ¥{data.get('price_jpy', 0)}")
                    upload_result = upload_to_shopify(data, collection_id)

                    if upload_result['success']:
                        results.append({
                            'item_id': item_id,
                            'title': upload_result.get('translated', {}).get('title', ''),
                            'original_title': data.get('title', ''),
                            'price_jpy': data.get('price_jpy', 0),
                            'variants_count': upload_result.get('variants_count', 0),
                            'status': 'uploaded'
                        })
                        print(f"[TEST UPLOAD] ✓ 上架成功: {item_id}")
                    else:
                        results.append({
                            'item_id': item_id,
                            'title': data.get('title', ''),
                            'status': 'upload_failed',
                            'error': upload_result.get('error', '')[:200]
                        })
                        print(f"[TEST UPLOAD] ✗ 上架失敗: {upload_result.get('error', '')[:100]}")

                    await page.wait_for_timeout(1500)

                await browser.close()
                return results

        results = loop.run_until_complete(do_test_upload())
        loop.close()
        return jsonify({'success': True, 'results': results})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


if __name__ == '__main__':
    print("Human Made 爬蟲工具 v3.0 (Playwright)")
    load_shopify_token()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
