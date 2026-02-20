"""
adidas.jp 爬蟲 (Playwright + Shopify) v2.2
======================================
- 使用 Playwright 模擬瀏覽器爬取 adidas.jp
- 支援男鞋 / 女鞋分類
- 定價公式: (adidas售價 + 1250) / 0.7 = Shopify售價 (日幣)
- 自動上架到 Shopify
- 翻譯: 日文 → 繁體中文 (ChatGPT API)
- v2.2: 缺貨商品自動刪除
"""

import os
import re
import json
import math
import time
import logging
import requests
from datetime import datetime
from urllib.parse import urljoin, unquote

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("adidas")

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROXY_URL = os.getenv("PROXY_URL", "")

CATEGORIES = {
    "men_originals": {
        "name": "男鞋 Originals",
        "url": "https://www.adidas.jp/%E3%83%A1%E3%83%B3%E3%82%BA-%E3%82%B7%E3%83%A5%E3%83%BC%E3%82%BA%E3%83%BB%E9%9D%B4-%E3%82%AA%E3%83%AA%E3%82%B8%E3%83%8A%E3%83%AB%E3%82%B9",
        "collection": "adidas 男鞋 Originals",
    },
    "women_originals": {
        "name": "女鞋 Originals",
        "url": "https://www.adidas.jp/%E3%83%AC%E3%83%87%E3%82%A3%E3%83%BC%E3%82%B9-%E3%82%B7%E3%83%A5%E3%83%BC%E3%82%BA%E3%83%BB%E9%9D%B4-%E3%82%AA%E3%83%AA%E3%82%B8%E3%83%8A%E3%83%AB%E3%82%B9",
        "collection": "adidas 女鞋 Originals",
    },
}

ITEMS_PER_PAGE = 48
BASE_URL = "https://www.adidas.jp"


def calculate_price(adidas_price_jpy: int) -> int:
    raw = (adidas_price_jpy + 1250) / 0.7
    return math.ceil(raw)


def _api_request_with_retry(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
            logger.warning(f"  ⏳ Rate limit (429)，等待 {retry_after}s 後重試... ({attempt+1}/{max_retries})")
            time.sleep(retry_after)
            continue
        return resp
    return resp


def translate_ja_to_zhtw(text: str) -> str:
    if not text or not text.strip(): return text
    if not OPENAI_API_KEY:
        logger.warning("未設定 OPENAI_API_KEY，跳過翻譯")
        return text
    for attempt in range(3):
        try:
            resp = requests.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [
                    {"role": "system", "content": "你是翻譯專家。請將以下日文翻譯成繁體中文。嚴格規則：1. 只回傳翻譯結果，不要加任何解釋。2. 品牌名和型號名保留英文原文。3. 輸出中絕對不能出現任何日文（平假名、片假名、漢字混日文）。4. 如果原文已經是英文或中文，直接回傳原文。5. 如果原文包含多行，保持相同的行數和格式。"},
                    {"role": "user", "content": text}], "temperature": 0, "max_tokens": 1000}, timeout=30)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            elif resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 3 * (attempt + 1)))
                logger.warning(f"  ⏳ OpenAI rate limit，等待 {wait}s... ({attempt+1}/3)")
                time.sleep(wait); continue
            else:
                logger.error(f"翻譯 API 錯誤: {resp.status_code}"); return text
        except Exception as e:
            logger.error(f"翻譯失敗 (attempt {attempt+1}): {e}")
            if attempt < 2: time.sleep(2); continue
            return text
    return text


# ============================================================
# Playwright 爬蟲核心
# ============================================================
class AdidasScraper:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None

    async def init_browser(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self._proxy_url = PROXY_URL
        await self._launch_browser()

    async def _launch_browser(self):
        launch_args = {"headless": True, "args": [
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled", "--disable-infobars",
            "--window-size=1920,1080", "--disable-gpu", "--disable-extensions"]}
        if self._proxy_url: launch_args["proxy"] = {"server": self._proxy_url}
        self.browser = await self.pw.chromium.launch(**launch_args)
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ja-JP", timezone_id="Asia/Tokyo",
            extra_http_headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"})
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['ja', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)
        self.page = await self.context.new_page()

    async def _restart_browser(self):
        logger.info("  🔄 完全重啟瀏覽器...")
        try: await self.browser.close()
        except Exception: pass
        import asyncio; await asyncio.sleep(2)
        await self._launch_browser()
        logger.info("  ✅ 瀏覽器重啟完成")

    async def close_browser(self):
        if self.browser: await self.browser.close()
        if self.pw: await self.pw.stop()

    async def scrape_listing_page(self, category_url: str, max_pages: int = 0) -> list:
        products = []; page_num = 0
        while True:
            if page_num == 0: url = category_url
            else:
                start = page_num * ITEMS_PER_PAGE
                separator = "&" if "?" in category_url else "?"
                url = f"{category_url}{separator}start={start}"
            logger.info(f"正在載入第 {page_num + 1} 頁: {url}")
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await self.page.wait_for_timeout(5000)
                await self._close_popups()
                await self.page.wait_for_timeout(2000)
                await self.page.wait_for_selector('[data-testid="plp-product-card"]', timeout=20000)
            except Exception as e:
                err_msg = str(e)
                if "crash" in err_msg.lower() or "closed" in err_msg.lower() or "object" in err_msg.lower():
                    logger.warning(f"  🔄 列表頁崩潰，完全重啟瀏覽器...")
                    try:
                        await self._restart_browser()
                        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        await self.page.wait_for_timeout(5000)
                        await self._close_popups()
                        await self.page.wait_for_timeout(2000)
                        await self.page.wait_for_selector('[data-testid="plp-product-card"]', timeout=20000)
                    except Exception as retry_e:
                        logger.error(f"  ❌ 重試後仍失敗: {retry_e}")
                        if page_num == 0: break
                        else: page_num += 1; continue
                else:
                    screenshot_path = f"/tmp/adidas_debug_page{page_num + 1}.png"
                    try: await self.page.screenshot(path=screenshot_path, full_page=False)
                    except Exception: pass
                    try:
                        page_title = await self.page.title()
                        page_url = self.page.url
                        page_text = await self.page.inner_text("body")
                        logger.info(f"📄 頁面標題: {page_title}")
                        logger.info(f"📄 頁面 URL: {page_url}")
                        logger.info(f"📄 頁面前500字: {page_text[:500]}")
                    except Exception: pass
                    if page_num == 0:
                        logger.error(f"第 1 頁載入失敗: {e}"); break
                    else:
                        logger.info(f"第 {page_num + 1} 頁無商品，結束分頁"); break
            await self._scroll_page()
            cards = await self.page.query_selector_all('[data-testid="plp-product-card"]')
            logger.info(f"  第 {page_num + 1} 頁找到 {len(cards)} 個商品")
            if len(cards) == 0: break
            page_product_count = 0
            for card in cards:
                try:
                    product = await self._parse_card(card)
                    if product and not any(p["sku"] == product["sku"] for p in products):
                        products.append(product); page_product_count += 1
                except Exception as e:
                    logger.warning(f"解析商品卡片失敗: {e}"); continue
            logger.info(f"  第 {page_num + 1} 頁新增 {page_product_count} 個商品（累計 {len(products)} 個）")
            if len(cards) < ITEMS_PER_PAGE: break
            page_num += 1
            if max_pages > 0 and page_num >= max_pages: break
            await self.page.wait_for_timeout(2000)
        logger.info(f"總共找到 {len(products)} 個不重複商品")
        return products

    async def _scroll_page(self):
        for _ in range(5):
            await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
            await self.page.wait_for_timeout(500)
        await self.page.evaluate("window.scrollTo(0, 0)")
        await self.page.wait_for_timeout(500)

    async def _parse_card(self, card) -> dict | None:
        link_el = await card.query_selector('[data-testid="product-card-image-link"]')
        if not link_el: return None
        href = await link_el.get_attribute("href")
        if not href: return None
        sku_match = re.search(r"/([A-Z0-9]{5,10})\.html", href)
        sku = sku_match.group(1) if sku_match else ""
        title_el = await card.query_selector('[data-testid="product-card-title"]')
        title = await title_el.inner_text() if title_el else ""
        subtitle_el = await card.query_selector('[data-testid="product-card-subtitle"]')
        subtitle = await subtitle_el.inner_text() if subtitle_el else ""
        price_el = await card.query_selector('[data-testid="main-price"] span:last-child')
        price_text = await price_el.inner_text() if price_el else ""
        price_jpy = self._parse_price(price_text)
        colors_el = await card.query_selector('[data-testid="product-card-colours"]')
        colors_text = await colors_el.inner_text() if colors_el else ""
        img_el = await card.query_selector('[data-testid="product-card-primary-image"]')
        img_src = await img_el.get_attribute("src") if img_el else ""
        hi_res_img = re.sub(r"w_\d+,h_\d+", "w_840,h_840", img_src) if img_src else ""
        if not sku or not price_jpy: return None
        return {"sku": sku, "title": title, "subtitle": subtitle, "price_jpy": price_jpy,
                "selling_price": calculate_price(price_jpy), "colors_text": colors_text,
                "url": urljoin(BASE_URL, href), "image": hi_res_img, "scraped_at": datetime.now().isoformat()}

    async def scrape_product_detail(self, product_url: str) -> dict | None:
        detail = {}; page_loaded = False
        for attempt, wait_until in enumerate(["domcontentloaded", "commit"], 1):
            try:
                logger.info(f"  嘗試載入詳細頁 (策略{attempt}: {wait_until}): {product_url}")
                await self.page.goto(product_url, wait_until=wait_until, timeout=30000)
                await self.page.wait_for_timeout(5000)
                await self._close_popups()
                await self.page.wait_for_timeout(2000)
                await self._scroll_page()
                page_loaded = True; break
            except Exception as e:
                err_msg = str(e)
                logger.warning(f"  詳細頁載入策略{attempt}失敗: {err_msg}")
                if "crash" in err_msg.lower() or "closed" in err_msg.lower() or "object" in err_msg.lower():
                    try: await self._restart_browser()
                    except Exception as re_err:
                        logger.error(f"  ❌ 瀏覽器重啟失敗: {re_err}"); return None
        if not page_loaded:
            logger.error(f"  ❌ 詳細頁完全無法載入: {product_url}"); return None

        # 性別
        try:
            category_el = await self.page.query_selector('[data-auto-id="product-category"] span')
            if category_el:
                category_text = await category_el.inner_text()
                detail["category_text"] = category_text
                if "メンズ" in category_text and "レディース" in category_text: detail["gender"] = "unisex"
                elif "メンズ" in category_text: detail["gender"] = "men"
                elif "レディース" in category_text: detail["gender"] = "women"
                else: detail["gender"] = "unisex"
            else: detail["gender"] = "unisex"
        except Exception: detail["gender"] = "unisex"

        # 描述
        try:
            subtitle_el = await self.page.query_selector('.description_subtitle__5h3_L, [class*="description_subtitle"]')
            desc_text_el = await self.page.query_selector('.description_margin__cBW26, [class*="description_margin"], .description_text-content__zFrZJ p')
            subtitle = (await subtitle_el.inner_text()).strip() if subtitle_el else ""
            desc_text = (await desc_text_el.inner_text()).strip() if desc_text_el else ""
            if not subtitle and not desc_text:
                desc_el = await self.page.query_selector('[data-testid="product-description"], [class*="description_description"]')
                if desc_el: desc_text = (await desc_el.inner_text()).strip()
            detail["subtitle"] = subtitle; detail["description"] = desc_text
        except Exception: detail["subtitle"] = ""; detail["description"] = ""

        # 規格
        try:
            spec_items = await self.page.query_selector_all('[data-testid="specifications-section"] li, [data-auto-id="specifications-section"] li')
            specs = []
            for item in spec_items:
                text = (await item.inner_text()).strip()
                if text: specs.append(text)
            detail["specifications"] = specs
            origin_el = await self.page.query_selector('[data-testid="specifications-table"] [role="cell"] .gl-table__cell-inner')
            detail["origin"] = (await origin_el.inner_text()).strip() if origin_el else ""
        except Exception: detail["specifications"] = []; detail["origin"] = ""

        # 圖片
        images = []; seen = set()
        sku_match = re.search(r"/([A-Z0-9]{5,10})\.html", product_url)
        sku_for_img = sku_match.group(1) if sku_match else ""
        if sku_for_img:
            try:
                hires_imgs = await self.page.query_selector_all('img[data-testid="pdp__image-viewer__desktop-zoom__hi-res-image"]')
                for img in hires_imgs:
                    src = await img.get_attribute("src")
                    if src and sku_for_img in src and src not in seen: seen.add(src); images.append(src)
                if not images:
                    page_content = await self.page.content()
                    for m in re.finditer(rf'(https://assets\.adidas\.com/images/h_2000[^"\'>\s]+{sku_for_img}[^"\'>\s]+\.jpg)', page_content):
                        url = m.group(1)
                        if url not in seen: seen.add(url); images.append(url)
                if not images:
                    if 'page_content' not in dir(): page_content = await self.page.content()
                    for m in re.finditer(rf'(https://assets\.adidas\.com/images/h_840[^"\'>\s]+{sku_for_img}[^"\'>\s]+\.jpg)', page_content):
                        url = m.group(1)
                        if url not in seen: seen.add(url); images.append(url)
            except Exception as e: logger.warning(f"  圖片抓取失敗: {e}")
        detail["images"] = images

        # 尺碼
        try:
            size_buttons = await self.page.query_selector_all('[data-auto-id="size-selector"] button[role="radio"]')
            sizes = []
            for btn in size_buttons:
                size_text = await btn.inner_text()
                aria_label = await btn.get_attribute("aria-label") or ""
                cls = await btn.get_attribute("class") or ""
                is_unavailable = "unavailable" in cls or "ご購入いただけません" in aria_label
                sizes.append({"size": size_text.strip(), "available": not is_unavailable})
            detail["sizes"] = sizes
            available_count = sum(1 for s in sizes if s["available"])
            logger.info(f"  👟 找到 {len(sizes)} 個尺碼（{available_count} 個有貨）")
        except Exception: detail["sizes"] = []

        # 顏色
        try:
            color_label = await self.page.query_selector('[data-auto-id="color-label"], [data-testid="color-label"]')
            if color_label: detail["color"] = (await color_label.inner_text()).strip()
            else: detail["color"] = ""
        except Exception: detail["color"] = ""

        return detail

    async def _close_popups(self):
        for selector in ['[data-testid="cookie-banner-accept"]', '[data-testid="modal-close"]',
                         'button:has-text("同意")', 'button:has-text("閉じる")', 'button:has-text("Accept")',
                         '[class*="cookie"] button', '[class*="popup"] [class*="close"]']:
            try:
                btn = self.page.locator(selector)
                if await btn.count() > 0: await btn.first.click(); await self.page.wait_for_timeout(500)
            except Exception: continue

    @staticmethod
    def _parse_price(text: str) -> int:
        if not text: return 0
        nums = re.findall(r"\d+", text.replace(",", ""))
        return int("".join(nums)) if nums else 0


# ============================================================
# Shopify 上架
# ============================================================
class ShopifyUploader:
    def __init__(self):
        if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
            logger.warning("未設定 Shopify 環境變數，上架功能不可用")
        self.base_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01"
        self.headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}
        self._existing_skus = None
        self._collection_cache = {}
        self._publication_ids = None

    def get_publication_ids(self) -> list:
        if self._publication_ids is not None: return self._publication_ids
        self._publication_ids = []
        graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01/graphql.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}
        try:
            resp = requests.post(graphql_url, headers=headers,
                json={"query": """{ currentAppInstallation { accessScopes { handle } } }"""}, timeout=15)
            if resp.status_code == 200:
                scopes = resp.json().get("data", {}).get("currentAppInstallation", {}).get("accessScopes", [])
                has_pub = any("publication" in s.get("handle", "") for s in scopes)
                if not has_pub: logger.warning("⚠️ Token 可能缺少 write_publications 權限！")
        except Exception: pass
        try:
            resp = requests.post(graphql_url, headers=headers,
                json={"query": """{ publications(first: 20) { edges { node { id name } } } }"""}, timeout=15)
            if resp.status_code == 200:
                pubs = resp.json().get("data", {}).get("publications", {}).get("edges", [])
                seen_names = set()
                for pub in pubs:
                    name = pub["node"]["name"]
                    if name not in seen_names: seen_names.add(name); self._publication_ids.append(pub["node"]["id"])
                logger.info(f"找到 {len(self._publication_ids)} 個銷售管道: {', '.join(seen_names)}")
        except Exception as e: logger.error(f"取得銷售管道異常: {e}")
        return self._publication_ids

    def publish_to_all_channels(self, resource_type: str, resource_id: int):
        pub_ids = self.get_publication_ids()
        if not pub_ids: return
        graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01/graphql.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}
        mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) { publishable { availablePublicationsCount { count } } userErrors { field message } } }"""
        gid = f"gid://shopify/{resource_type}/{resource_id}"
        variables = {"id": gid, "input": [{"publicationId": pid} for pid in pub_ids]}
        try:
            resp = requests.post(graphql_url, headers=headers, json={"query": mutation, "variables": variables}, timeout=15)
            if resp.status_code == 200:
                errors = resp.json().get("data", {}).get("publishablePublish", {}).get("userErrors", [])
                if errors:
                    for err in errors: logger.warning(f"  發布警告 ({resource_type} {resource_id}): {err.get('message')}")
                else:
                    logger.info(f"  ✅ {resource_type} {resource_id} 已發布到 {len(pub_ids)} 個管道")
        except Exception as e: logger.error(f"  發布異常: {e}")

    def get_existing_skus(self) -> set:
        if self._existing_skus is not None: return self._existing_skus
        skus = set()
        url = f"{self.base_url}/products.json?limit=250&fields=id,variants,tags"
        while url:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200: break
                for product in resp.json().get("products", []):
                    for variant in product.get("variants", []):
                        sku = variant.get("sku", "")
                        if sku:
                            base_sku = sku.split("-")[0].upper()
                            skus.add(base_sku); skus.add(sku.upper())
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                    url = match.group(1) if match else None
                else: url = None
            except Exception as e:
                logger.error(f"取得 SKU 失敗: {e}"); break
        logger.info(f"Shopify 已有 {len(skus)} 個 SKU")
        self._existing_skus = skus
        return skus

    def is_duplicate(self, sku: str) -> bool:
        return sku.upper() in self.get_existing_skus()

    # === v2.2: Collection 商品對照表 ===
    def get_collection_products_map(self, collection_id: int) -> dict:
        """取得 collection 中所有商品的 SKU → product_id 對照"""
        pm = {}
        if not collection_id: return pm
        url = f"{self.base_url}/collections/{collection_id}/products.json?limit=250"
        while url:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200: break
                for p in resp.json().get("products", []):
                    pid = p.get("id")
                    for v in p.get("variants", []):
                        sk = v.get("sku", "")
                        if sk and pid:
                            base_sku = sk.split("-")[0].upper()
                            pm[base_sku] = pid
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                    url = match.group(1) if match else None
                else: url = None
            except Exception as e:
                logger.error(f"取得 Collection 商品失敗: {e}"); break
        return pm

    # === v2.2: 刪除商品 ===
    def delete_product(self, product_id: int) -> bool:
        """從 Shopify 刪除商品"""
        try:
            resp = requests.delete(f"{self.base_url}/products/{product_id}.json",
                                   headers=self.headers, timeout=30)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"刪除商品失敗: {e}")
            return False

    def get_or_create_collection(self, title: str) -> int | None:
        if title in self._collection_cache: return self._collection_cache[title]
        try:
            resp = requests.get(f"{self.base_url}/custom_collections.json?title={title}",
                headers=self.headers, timeout=30)
            if resp.status_code == 200:
                for c in resp.json().get("custom_collections", []):
                    if c["title"] == title:
                        self._collection_cache[title] = c["id"]
                        self.publish_to_all_channels("Collection", c["id"])
                        return c["id"]
        except Exception: pass
        try:
            resp = requests.post(f"{self.base_url}/custom_collections.json", headers=self.headers,
                json={"custom_collection": {"title": title, "published": True}}, timeout=30)
            if resp.status_code == 201:
                cid = resp.json()["custom_collection"]["id"]
                self._collection_cache[title] = cid
                self.publish_to_all_channels("Collection", cid)
                return cid
        except Exception as e: logger.error(f"建立 Collection 失敗: {e}")
        return None

    def upload_product(self, product: dict, detail: dict | None, collection_id: int | None, translate: bool = True) -> dict:
        title = product["title"]
        def clean_title(t: str) -> str:
            import unicodedata
            parts = [p.strip() for p in t.split("/")]
            english_parts = []; seen = set()
            for p in parts:
                has_japanese = any(unicodedata.name(c, "").startswith(("HIRAGANA", "KATAKANA", "CJK")) for c in p if c.strip())
                if not has_japanese and p and p.lower() not in seen: seen.add(p.lower()); english_parts.append(p)
            return " / ".join(english_parts) if english_parts else t
        clean_en_title = clean_title(title)
        subtitle = detail.get("subtitle", "") if detail else ""
        description = detail.get("description", "") if detail else ""
        specs = detail.get("specifications", []) if detail else []
        origin = detail.get("origin", "") if detail else ""
        color = detail.get("color", "") if detail else ""
        if translate and OPENAI_API_KEY:
            subtitle_zh = translate_ja_to_zhtw(subtitle) if subtitle else ""
            desc_zh = translate_ja_to_zhtw(description) if description else ""
            if specs:
                specs_text = "\n".join(specs)
                specs_zh = translate_ja_to_zhtw(specs_text)
                specs_zh_list = [s.strip() for s in specs_zh.split("\n") if s.strip()]
            else: specs_zh_list = []
            origin_zh = translate_ja_to_zhtw(origin) if origin else ""
        else:
            subtitle_zh = subtitle; desc_zh = description; specs_zh_list = specs; origin_zh = origin
        full_title = f"adidas｜original｜原創系列｜{clean_en_title}"
        images = []
        if detail and detail.get("images"):
            for img_url in detail["images"][:20]: images.append({"src": img_url})
        elif product.get("image"): images.append({"src": product["image"]})
        body_html = self._build_description_html(subtitle_zh=subtitle_zh, desc_zh=desc_zh, specs=specs_zh_list,
                                                  origin=origin_zh, color=color, sku=product["sku"])
        gender = detail.get("gender", "unisex") if detail else "unisex"
        collection_names = self._get_collection_names_by_gender(gender, product.get("collection_name", ""))
        sizes = detail.get("sizes", []) if detail else []
        color = detail.get("color", "") if detail else ""
        if sizes:
            variants = []
            for s in sizes:
                variants.append({"option1": s["size"], "price": str(product["selling_price"]),
                    "compare_at_price": None, "sku": f"{product['sku']}-{s['size'].replace('.', '').replace('cm', '')}",
                    "inventory_management": "shopify", "requires_shipping": True})
            options = [{"name": "尺碼", "values": [s["size"] for s in sizes]}]
            size_stock = {s["size"]: 2 if s["available"] else 0 for s in sizes}
        else:
            variants = [{"price": str(product["selling_price"]), "compare_at_price": None, "sku": product["sku"],
                         "inventory_management": "shopify", "requires_shipping": True}]
            options = []; size_stock = {"__default__": 2}
        seo = self._generate_seo(clean_en_title, subtitle_zh, desc_zh, color, product["sku"])
        payload = {"product": {
            "title": full_title, "body_html": body_html, "vendor": "adidas", "product_type": "鞋類",
            "tags": ["adidas", product.get("subtitle", ""), product["sku"], color],
            "variants": variants, "images": images, "status": "active", "published": True, "published_scope": "global",
            "metafields_global_title_tag": seo.get("title", full_title),
            "metafields_global_description_tag": seo.get("description", "")}}
        if options: payload["product"]["options"] = options
        try:
            resp = _api_request_with_retry("POST", f"{self.base_url}/products.json",
                headers=self.headers, json=payload, timeout=60)
            if resp.status_code == 201:
                shopify_product = resp.json()["product"]; product_id = shopify_product["id"]
                if size_stock: self._set_inventory_levels(shopify_product, size_stock)
                self._set_product_metafield(product_id, product["url"])
                for col_name in collection_names:
                    col_id = self.get_or_create_collection(col_name)
                    if col_id: self._add_to_collection(product_id, col_id)
                self.publish_to_all_channels("Product", product_id)
                self._existing_skus.add(product["sku"].upper())
                return {"success": True, "product_id": product_id}
            else:
                logger.error(f"❌ 上架失敗: {product['sku']} - {resp.status_code} {resp.text[:200]}")
                return {"success": False, "error": resp.text[:200]}
        except Exception as e:
            logger.error(f"❌ 上架異常: {product['sku']} - {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def _generate_seo(title_en, subtitle_zh, desc_zh, color, sku) -> dict:
        if not OPENAI_API_KEY: return {}
        prompt_text = f"商品名稱: {title_en}\n商品描述: {subtitle_zh} {desc_zh}\n顏色: {color}\n型號: {sku}\n品牌: adidas Originals\n商店: GOYOUTATI 日本代購"
        for attempt in range(3):
            try:
                resp = requests.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "messages": [
                        {"role": "system", "content": "你是 SEO 專家。根據商品資訊生成搜尋引擎優化的頁面標題和 Meta 描述。規則：1. 頁面標題(title)：最多 60 字元，包含品牌名、商品名、關鍵字。格式範例：adidas Samba OG 經典鞋款｜GOYOUTATI 日本代購 2. Meta 描述(description)：最多 155 字元，自然流暢的繁體中文，包含商品賣點、顏色、適合場景。3. 不要出現日文。4. 只回傳 JSON 格式：{\"title\": \"...\", \"description\": \"...\"} 5. 不要加 markdown 格式。"},
                        {"role": "user", "content": prompt_text}], "temperature": 0, "max_tokens": 300}, timeout=30)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"].strip().replace("```json", "").replace("```", "").strip()
                    return json.loads(content)
                elif resp.status_code == 429:
                    time.sleep(float(resp.headers.get("Retry-After", 3 * (attempt + 1)))); continue
                else: return {}
            except Exception as e:
                if attempt < 2: time.sleep(2); continue
                return {}
        return {}

    def _set_product_metafield(self, product_id, url):
        try:
            _api_request_with_retry("POST", f"{self.base_url}/products/{product_id}/metafields.json",
                headers=self.headers, json={"metafield": {"namespace": "custom", "key": "link", "value": url, "type": "url"}}, timeout=30)
        except Exception: pass

    def _set_inventory_levels(self, shopify_product, size_stock):
        try:
            first_variant = shopify_product.get("variants", [{}])[0]
            first_inv_id = first_variant.get("inventory_item_id")
            if not first_inv_id: return
            inv_resp = _api_request_with_retry("GET", f"{self.base_url}/inventory_levels.json?inventory_item_ids={first_inv_id}",
                headers=self.headers, timeout=30)
            inv_levels = inv_resp.json().get("inventory_levels", [])
            if inv_levels: location_id = inv_levels[0]["location_id"]
            else:
                loc_resp = _api_request_with_retry("GET", f"{self.base_url}/locations.json", headers=self.headers, timeout=30)
                locations = loc_resp.json().get("locations", [])
                if not locations: return
                location_id = locations[0]["id"]
            has_default = "__default__" in size_stock
            for variant in shopify_product.get("variants", []):
                size_name = variant.get("option1", "")
                qty = size_stock["__default__"] if has_default else size_stock.get(size_name, 0)
                inventory_item_id = variant.get("inventory_item_id")
                if not inventory_item_id: continue
                _api_request_with_retry("POST", f"{self.base_url}/inventory_levels/set.json",
                    headers=self.headers, json={"location_id": location_id, "inventory_item_id": inventory_item_id, "available": qty}, timeout=30)
        except Exception as e: logger.warning(f"  ⚠️ 庫存設定失敗: {e}")

    @staticmethod
    def _get_collection_names_by_gender(gender, default_collection) -> list:
        if gender == "men": return ["adidas 男鞋"]
        elif gender == "women": return ["adidas 女鞋"]
        else: return ["adidas 男鞋", "adidas 女鞋"]

    def _add_to_collection(self, product_id, collection_id):
        try:
            requests.post(f"{self.base_url}/collects.json", headers=self.headers,
                json={"collect": {"product_id": product_id, "collection_id": collection_id}}, timeout=30)
        except Exception: pass

    @staticmethod
    def _build_description_html(subtitle_zh, desc_zh, specs, origin, color, sku) -> str:
        parts = []
        if subtitle_zh: parts.append(f"<h3>{subtitle_zh}</h3>")
        if desc_zh: parts.append(f"<p>{desc_zh}</p>")
        if specs:
            parts.append("<h3>商品詳細</h3><ul>")
            for spec in specs: parts.append(f"  <li>{spec}</li>")
            parts.append("</ul>")
        info_rows = []
        if color: info_rows.append(f'<tr><td><strong>顏色</strong></td><td>{color}</td></tr>')
        info_rows.append(f'<tr><td><strong>型號</strong></td><td>{sku}</td></tr>')
        if origin: info_rows.append(f'<tr><td><strong>產地</strong></td><td>{origin}</td></tr>')
        if info_rows: parts.append("<table>"); parts.extend(info_rows); parts.append("</table>")
        return "\n".join(parts)
