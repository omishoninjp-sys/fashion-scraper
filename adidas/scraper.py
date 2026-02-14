"""
adidas.jp 爬蟲 (Playwright + Shopify)
======================================
- 使用 Playwright 模擬瀏覽器爬取 adidas.jp
- 支援男鞋 / 女鞋分類
- 定價公式: (adidas售價 + 1250) / 0.7 = Shopify售價 (日幣)
- 自動上架到 Shopify
- 翻譯: 日文 → 繁體中文 (ChatGPT API)
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

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("adidas")

# ============================================================
# 環境變數
# ============================================================
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")          # e.g. goyoutati
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROXY_URL = os.getenv("PROXY_URL", "")                  # 可選: http://user:pass@host:port

# ============================================================
# adidas.jp 分類 URL
# ============================================================
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

# 分頁設定: 每頁 48 個商品
ITEMS_PER_PAGE = 48

BASE_URL = "https://www.adidas.jp"


# ============================================================
# 定價公式
# ============================================================
def calculate_price(adidas_price_jpy: int) -> int:
    """
    (adidas售價 + 1250) / 0.7 = Shopify售價
    無條件進位到整數
    """
    raw = (adidas_price_jpy + 1250) / 0.7
    return math.ceil(raw)


# ============================================================
# 翻譯 (ChatGPT API)
# ============================================================
def _api_request_with_retry(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """帶 retry 的 API 請求（處理 429 rate limit）"""
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
            logger.warning(f"  ⏳ Rate limit (429)，等待 {retry_after}s 後重試... ({attempt+1}/{max_retries})")
            time.sleep(retry_after)
            continue
        return resp
    return resp  # 最後一次的回應


def translate_ja_to_zhtw(text: str) -> str:
    """用 OpenAI ChatGPT 將日文翻譯為繁體中文（含 retry）"""
    if not text or not text.strip():
        return text
    if not OPENAI_API_KEY:
        logger.warning("未設定 OPENAI_API_KEY，跳過翻譯")
        return text

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是翻譯專家。請將以下日文翻譯成繁體中文。"
                                "嚴格規則："
                                "1. 只回傳翻譯結果，不要加任何解釋。"
                                "2. 品牌名和型號名保留英文原文。"
                                "3. 輸出中絕對不能出現任何日文（平假名、片假名、漢字混日文）。"
                                "4. 如果原文已經是英文或中文，直接回傳原文。"
                                "5. 如果原文包含多行，保持相同的行數和格式。"
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0,
                    "max_tokens": 1000,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            elif resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 3 * (attempt + 1)))
                logger.warning(f"  ⏳ OpenAI rate limit，等待 {wait}s... ({attempt+1}/3)")
                time.sleep(wait)
                continue
            else:
                logger.error(f"翻譯 API 錯誤: {resp.status_code}")
                return text
        except Exception as e:
            logger.error(f"翻譯失敗 (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(2)
                continue
            return text
    return text


# ============================================================
# Playwright 爬蟲核心
# ============================================================
class AdidasScraper:
    """使用 Playwright 爬取 adidas.jp 商品"""

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None

    async def init_browser(self):
        """啟動瀏覽器（含反偵測）"""
        from playwright.async_api import async_playwright

        self.pw = await async_playwright().start()
        self._proxy_url = PROXY_URL
        await self._launch_browser()

    async def _launch_browser(self):
        """啟動或重啟整個瀏覽器（browser + context + page）"""
        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--disable-gpu",
                "--disable-extensions",
            ],
        }
        if self._proxy_url:
            launch_args["proxy"] = {"server": self._proxy_url}

        self.browser = await self.pw.chromium.launch(**launch_args)
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            extra_http_headers={
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            },
        )

        # 反偵測: 移除 navigator.webdriver 標記
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['ja', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)

        self.page = await self.context.new_page()

    async def _restart_browser(self):
        """完全重啟瀏覽器（解決 Page crash 後 connection 損壞的問題）"""
        logger.info("  🔄 完全重啟瀏覽器...")
        # 關閉舊的（忽略所有錯誤）
        try:
            await self.browser.close()
        except Exception:
            pass
        # 等待一下讓進程完全結束
        import asyncio
        await asyncio.sleep(2)
        # 重新啟動
        await self._launch_browser()
        logger.info("  ✅ 瀏覽器重啟完成")

    async def close_browser(self):
        """關閉瀏覽器"""
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def scrape_listing_page(self, category_url: str, max_pages: int = 0) -> list:
        """
        爬取商品列表頁，使用 URL 分頁 (?start=0, 48, 96, ...)
        max_pages=0 表示爬全部頁面
        """
        products = []
        page_num = 0

        while True:
            # 組合分頁 URL
            if page_num == 0:
                url = category_url
            else:
                start = page_num * ITEMS_PER_PAGE
                separator = "&" if "?" in category_url else "?"
                url = f"{category_url}{separator}start={start}"

            logger.info(f"正在載入第 {page_num + 1} 頁: {url}")

            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # 等久一點讓 JS 渲染完成
                await self.page.wait_for_timeout(5000)

                # 先嘗試關閉彈窗（可能擋住內容）
                await self._close_popups()
                await self.page.wait_for_timeout(2000)

                # 等待商品卡片出現
                await self.page.wait_for_selector(
                    '[data-testid="plp-product-card"]', timeout=20000
                )
            except Exception as e:
                err_msg = str(e)
                
                # Page crashed → 完全重啟瀏覽器
                if "crash" in err_msg.lower() or "closed" in err_msg.lower() or "object" in err_msg.lower():
                    logger.warning(f"  🔄 列表頁崩潰，完全重啟瀏覽器...")
                    try:
                        await self._restart_browser()
                        # 重試載入同一頁
                        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        await self.page.wait_for_timeout(5000)
                        await self._close_popups()
                        await self.page.wait_for_timeout(2000)
                        await self.page.wait_for_selector(
                            '[data-testid="plp-product-card"]', timeout=20000
                        )
                    except Exception as retry_e:
                        logger.error(f"  ❌ 重試後仍失敗: {retry_e}")
                        if page_num == 0:
                            break
                        else:
                            logger.info(f"第 {page_num + 1} 頁跳過，繼續下一頁")
                            page_num += 1
                            continue
                else:
                    # 截圖 debug
                    screenshot_path = f"/tmp/adidas_debug_page{page_num + 1}.png"
                    try:
                        await self.page.screenshot(path=screenshot_path, full_page=False)
                        logger.info(f"📸 Debug 截圖已儲存: {screenshot_path}")
                    except Exception:
                        pass

                    # 記錄頁面標題和 URL
                    try:
                        page_title = await self.page.title()
                        page_url = self.page.url
                        page_text = await self.page.inner_text("body")
                        logger.info(f"📄 頁面標題: {page_title}")
                        logger.info(f"📄 頁面 URL: {page_url}")
                        logger.info(f"📄 頁面前500字: {page_text[:500]}")
                    except Exception:
                        pass

                    if page_num == 0:
                        logger.error(f"第 1 頁載入失敗: {e}")
                        break
                    else:
                        logger.info(f"第 {page_num + 1} 頁無商品，結束分頁")
                        break

            # 滾動頁面確保所有商品都載入
            await self._scroll_page()

            # 解析商品卡片
            cards = await self.page.query_selector_all('[data-testid="plp-product-card"]')
            logger.info(f"  第 {page_num + 1} 頁找到 {len(cards)} 個商品")

            if len(cards) == 0:
                logger.info("沒有更多商品，結束分頁")
                break

            page_product_count = 0
            for card in cards:
                try:
                    product = await self._parse_card(card)
                    if product:
                        # 避免重複（跨頁可能重複）
                        if not any(p["sku"] == product["sku"] for p in products):
                            products.append(product)
                            page_product_count += 1
                except Exception as e:
                    logger.warning(f"解析商品卡片失敗: {e}")
                    continue

            logger.info(f"  第 {page_num + 1} 頁新增 {page_product_count} 個商品（累計 {len(products)} 個）")

            # 如果這一頁商品數少於 ITEMS_PER_PAGE，代表是最後一頁
            if len(cards) < ITEMS_PER_PAGE:
                logger.info("已到最後一頁")
                break

            page_num += 1

            # 檢查是否達到最大頁數限制
            if max_pages > 0 and page_num >= max_pages:
                logger.info(f"已達最大頁數限制 ({max_pages} 頁)")
                break

            # 頁間延遲
            await self.page.wait_for_timeout(2000)

        logger.info(f"總共找到 {len(products)} 個不重複商品")
        return products

    async def _scroll_page(self):
        """滾動頁面確保所有商品都載入"""
        for _ in range(5):
            await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
            await self.page.wait_for_timeout(500)
        # 滾回頂部
        await self.page.evaluate("window.scrollTo(0, 0)")
        await self.page.wait_for_timeout(500)

    async def _parse_card(self, card) -> dict | None:
        """解析單個商品卡片"""
        # 商品連結
        link_el = await card.query_selector('[data-testid="product-card-image-link"]')
        if not link_el:
            return None
        href = await link_el.get_attribute("href")
        if not href:
            return None

        # 從 URL 取得 SKU (例如 /サンバ-og-samba-og/B75806.html → B75806)
        sku_match = re.search(r"/([A-Z0-9]{5,10})\.html", href)
        sku = sku_match.group(1) if sku_match else ""

        # 商品名稱
        title_el = await card.query_selector('[data-testid="product-card-title"]')
        title = await title_el.inner_text() if title_el else ""

        # 副標題（系列名）
        subtitle_el = await card.query_selector('[data-testid="product-card-subtitle"]')
        subtitle = await subtitle_el.inner_text() if subtitle_el else ""

        # 價格
        price_el = await card.query_selector('[data-testid="main-price"] span:last-child')
        price_text = await price_el.inner_text() if price_el else ""
        price_jpy = self._parse_price(price_text)

        # 顏色數
        colors_el = await card.query_selector('[data-testid="product-card-colours"]')
        colors_text = await colors_el.inner_text() if colors_el else ""

        # 圖片
        img_el = await card.query_selector('[data-testid="product-card-primary-image"]')
        img_src = await img_el.get_attribute("src") if img_el else ""
        # 取得高解析度圖片 (替換為 w_840)
        hi_res_img = re.sub(r"w_\d+,h_\d+", "w_840,h_840", img_src) if img_src else ""

        if not sku or not price_jpy:
            return None

        return {
            "sku": sku,
            "title": title,
            "subtitle": subtitle,
            "price_jpy": price_jpy,
            "selling_price": calculate_price(price_jpy),
            "colors_text": colors_text,
            "url": urljoin(BASE_URL, href),
            "image": hi_res_img,
            "scraped_at": datetime.now().isoformat(),
        }

    async def scrape_product_detail(self, product_url: str) -> dict | None:
        """
        爬取商品詳細頁，取得完整資訊（描述、所有圖片、尺碼等）
        如果頁面 crash，重建頁面後重試
        """
        detail = {}
        page_loaded = False

        # 嘗試載入頁面（兩種策略）
        for attempt, wait_until in enumerate(["domcontentloaded", "commit"], 1):
            try:
                logger.info(f"  嘗試載入詳細頁 (策略{attempt}: {wait_until}): {product_url}")
                await self.page.goto(product_url, wait_until=wait_until, timeout=30000)
                await self.page.wait_for_timeout(5000)
                # 關閉彈窗
                await self._close_popups()
                await self.page.wait_for_timeout(2000)
                # 滾動觸發懶載入圖片
                await self._scroll_page()
                page_loaded = True
                break
            except Exception as e:
                err_msg = str(e)
                logger.warning(f"  詳細頁載入策略{attempt}失敗: {err_msg}")
                
                # Page crashed → 完全重啟瀏覽器
                if "crash" in err_msg.lower() or "closed" in err_msg.lower() or "object" in err_msg.lower():
                    try:
                        await self._restart_browser()
                    except Exception as re_err:
                        logger.error(f"  ❌ 瀏覽器重啟失敗: {re_err}")
                        return None

        if not page_loaded:
            logger.error(f"  ❌ 詳細頁完全無法載入: {product_url}")
            return None

        # 性別判斷 (メンズ / レディース / ユニセックス)
        try:
            category_el = await self.page.query_selector('[data-auto-id="product-category"] span')
            if category_el:
                category_text = await category_el.inner_text()
                detail["category_text"] = category_text
                if "メンズ" in category_text and "レディース" in category_text:
                    detail["gender"] = "unisex"
                elif "メンズ" in category_text:
                    detail["gender"] = "men"
                elif "レディース" in category_text:
                    detail["gender"] = "women"
                else:
                    detail["gender"] = "unisex"
                logger.info(f"  性別判斷: {category_text} → {detail['gender']}")
            else:
                detail["gender"] = "unisex"
                logger.info("  性別標籤未找到 → unisex")
        except Exception:
            detail["gender"] = "unisex"

        # 商品描述（説明區塊）
        try:
            # 説明：副標題 + 描述文字
            subtitle_el = await self.page.query_selector('.description_subtitle__5h3_L, [class*="description_subtitle"]')
            desc_text_el = await self.page.query_selector('.description_margin__cBW26, [class*="description_margin"], .description_text-content__zFrZJ p')
            
            subtitle = (await subtitle_el.inner_text()).strip() if subtitle_el else ""
            desc_text = (await desc_text_el.inner_text()).strip() if desc_text_el else ""
            
            # fallback: 整個 description 區塊
            if not subtitle and not desc_text:
                desc_el = await self.page.query_selector('[data-testid="product-description"], [class*="description_description"]')
                if desc_el:
                    desc_text = (await desc_el.inner_text()).strip()
            
            detail["subtitle"] = subtitle
            detail["description"] = desc_text
        except Exception:
            detail["subtitle"] = ""
            detail["description"] = ""

        # 商品詳細（詳細區塊：規格列表 + 生產國）
        try:
            # 規格 bullet points
            spec_items = await self.page.query_selector_all(
                '[data-testid="specifications-section"] li, [data-auto-id="specifications-section"] li'
            )
            specs = []
            for item in spec_items:
                text = (await item.inner_text()).strip()
                if text:
                    specs.append(text)
            detail["specifications"] = specs
            
            # 生產國
            origin_el = await self.page.query_selector('[data-testid="specifications-table"] [role="cell"] .gl-table__cell-inner')
            detail["origin"] = (await origin_el.inner_text()).strip() if origin_el else ""
            
            if specs:
                logger.info(f"  📝 說明: {len(specs)} 項規格, 產地: {detail['origin']}")
        except Exception:
            detail["specifications"] = []
            detail["origin"] = ""

        # ===== 圖片抓取 =====
        # 按頁面上 hi-res-image 出現順序抓取，保持 adidas 原始排序
        images = []
        seen = set()
        
        sku_match = re.search(r"/([A-Z0-9]{5,10})\.html", product_url)
        sku_for_img = sku_match.group(1) if sku_match else ""

        if sku_for_img:
            try:
                # 方法1: 按 DOM 順序抓 hi-res-image（保持 adidas 頁面排列順序）
                hires_imgs = await self.page.query_selector_all(
                    'img[data-testid="pdp__image-viewer__desktop-zoom__hi-res-image"]'
                )
                for img in hires_imgs:
                    src = await img.get_attribute("src")
                    if src and sku_for_img in src and src not in seen:
                        seen.add(src)
                        images.append(src)
                
                # 方法2: 如果 hi-res 沒抓到，從 HTML 源碼按出現順序抓
                if not images:
                    page_content = await self.page.content()
                    # 用 finditer 保持出現順序
                    hires_pattern = rf'(https://assets\.adidas\.com/images/h_2000[^"\'>\s]+{sku_for_img}[^"\'>\s]+\.jpg)'
                    for m in re.finditer(hires_pattern, page_content):
                        url = m.group(1)
                        if url not in seen:
                            seen.add(url)
                            images.append(url)
                
                # 方法3: fallback 到 h_840
                if not images:
                    page_content = await self.page.content() if 'page_content' not in dir() else page_content
                    fallback_pattern = rf'(https://assets\.adidas\.com/images/h_840[^"\'>\s]+{sku_for_img}[^"\'>\s]+\.jpg)'
                    for m in re.finditer(fallback_pattern, page_content):
                        url = m.group(1)
                        if url not in seen:
                            seen.add(url)
                            images.append(url)
                
                # Debug
                for i, img_url in enumerate(images[:3]):
                    fname = img_url.split("/")[-1]
                    logger.info(f"    圖{i+1}: {fname}")
                
            except Exception as e:
                logger.warning(f"  圖片抓取失敗: {e}")

        detail["images"] = images
        logger.info(f"  📸 找到 {len(images)} 張商品圖片 (SKU: {sku_for_img})")

        # 可選尺碼
        try:
            size_buttons = await self.page.query_selector_all(
                '[data-auto-id="size-selector"] button[role="radio"]'
            )
            sizes = []
            for btn in size_buttons:
                size_text = await btn.inner_text()
                aria_label = await btn.get_attribute("aria-label") or ""
                # 有 unavailable class 或 aria-label 含「ご購入いただけません」= 缺貨
                cls = await btn.get_attribute("class") or ""
                is_unavailable = "unavailable" in cls or "ご購入いただけません" in aria_label
                sizes.append({
                    "size": size_text.strip(),
                    "available": not is_unavailable,
                })
            detail["sizes"] = sizes
            available_count = sum(1 for s in sizes if s["available"])
            logger.info(f"  👟 找到 {len(sizes)} 個尺碼（{available_count} 個有貨）")
        except Exception:
            detail["sizes"] = []

        # 當前顏色名稱
        try:
            color_label = await self.page.query_selector('[data-auto-id="color-label"], [data-testid="color-label"]')
            if color_label:
                detail["color"] = (await color_label.inner_text()).strip()
                logger.info(f"  🎨 顏色: {detail['color']}")
            else:
                detail["color"] = ""
        except Exception:
            detail["color"] = ""

        return detail

    async def _close_popups(self):
        """關閉可能出現的彈窗"""
        popup_selectors = [
            '[data-testid="cookie-banner-accept"]',
            '[data-testid="modal-close"]',
            'button:has-text("同意")',
            'button:has-text("閉じる")',
            'button:has-text("Accept")',
            '[class*="cookie"] button',
            '[class*="popup"] [class*="close"]',
        ]
        for selector in popup_selectors:
            try:
                btn = self.page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click()
                    await self.page.wait_for_timeout(500)
            except Exception:
                continue

    @staticmethod
    def _parse_price(text: str) -> int:
        """解析價格文字 '¥15,950' → 15950"""
        if not text:
            return 0
        nums = re.findall(r"\d+", text.replace(",", ""))
        return int("".join(nums)) if nums else 0


# ============================================================
# Shopify 上架
# ============================================================
class ShopifyUploader:
    """將商品上架到 Shopify"""

    def __init__(self):
        if not SHOPIFY_STORE or not SHOPIFY_ACCESS_TOKEN:
            logger.warning("未設定 Shopify 環境變數，上架功能不可用")
        self.base_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }
        self._existing_skus = None
        self._collection_cache = {}
        self._publication_ids = None

    def get_publication_ids(self) -> list:
        """用 GraphQL 取得所有銷售管道的 publication ID"""
        if self._publication_ids is not None:
            return self._publication_ids

        self._publication_ids = []
        graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01/graphql.json"
        headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }
        
        # 先查 access scopes
        scope_query = """{ currentAppInstallation { accessScopes { handle } } }"""
        try:
            resp = requests.post(graphql_url, headers=headers, json={"query": scope_query}, timeout=15)
            if resp.status_code == 200:
                result = resp.json()
                scopes = result.get("data", {}).get("currentAppInstallation", {}).get("accessScopes", [])
                scope_list = [s.get("handle", "") for s in scopes]
                has_pub = any("publication" in s for s in scope_list)
                logger.info(f"API Scopes 含 publication: {has_pub}")
                if not has_pub:
                    logger.warning("⚠️ Token 可能缺少 write_publications 權限！")
        except Exception:
            pass

        query = """{ publications(first: 20) { edges { node { id name } } } }"""

        try:
            resp = requests.post(graphql_url, headers=headers, json={"query": query}, timeout=15)
            if resp.status_code == 200:
                result = resp.json()
                pubs = result.get("data", {}).get("publications", {}).get("edges", [])
                seen_names = set()
                for pub in pubs:
                    name = pub["node"]["name"]
                    if name not in seen_names:
                        seen_names.add(name)
                        self._publication_ids.append(pub["node"]["id"])
                names = list(seen_names)
                logger.info(f"找到 {len(self._publication_ids)} 個銷售管道: {', '.join(names)}")
            else:
                logger.error(f"取得銷售管道失敗: {resp.status_code}")
        except Exception as e:
            logger.error(f"取得銷售管道異常: {e}")

        return self._publication_ids

    def publish_to_all_channels(self, resource_type: str, resource_id: int):
        """
        用 GraphQL 將商品或 Collection 發布到所有銷售管道
        resource_type: 'Product' 或 'Collection'
        """
        pub_ids = self.get_publication_ids()
        if not pub_ids:
            logger.warning("沒有找到任何銷售管道")
            return

        graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01/graphql.json"
        headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }

        if resource_type == "Product":
            mutation = """
            mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
              publishablePublish(id: $id, input: $input) {
                publishable { availablePublicationsCount { count } }
                userErrors { field message }
              }
            }
            """
            gid = f"gid://shopify/Product/{resource_id}"
            variables = {
                "id": gid,
                "input": [{"publicationId": pid} for pid in pub_ids],
            }

            try:
                resp = requests.post(
                    graphql_url,
                    headers=headers,
                    json={"query": mutation, "variables": variables},
                    timeout=15,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    errors = result.get("data", {}).get("publishablePublish", {}).get("userErrors", [])
                    if errors:
                        for err in errors:
                            logger.warning(f"  發布警告 (Product {resource_id}): {err.get('message')}")
                    else:
                        logger.info(f"  ✅ Product {resource_id} 已發布到 {len(pub_ids)} 個管道")
                else:
                    logger.error(f"  發布失敗: {resp.status_code}")
            except Exception as e:
                logger.error(f"  發布異常: {e}")

        elif resource_type == "Collection":
            # Collection 也用 publishablePublish（Collection 是 Publishable 資源）
            mutation = """
            mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
              publishablePublish(id: $id, input: $input) {
                publishable {
                  availablePublicationsCount { count }
                  ... on Collection {
                    resourcePublicationsCount { count }
                  }
                }
                userErrors { field message }
              }
            }
            """
            gid = f"gid://shopify/Collection/{resource_id}"
            variables = {
                "id": gid,
                "input": [{"publicationId": pid} for pid in pub_ids],
            }

            try:
                resp = requests.post(
                    graphql_url,
                    headers=headers,
                    json={"query": mutation, "variables": variables},
                    timeout=15,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    logger.info(f"  📦 Collection publish 回應: {json.dumps(result, ensure_ascii=False)[:500]}")
                    data = result.get("data", {}).get("publishablePublish", {})
                    errors = data.get("userErrors", [])
                    if errors:
                        for err in errors:
                            logger.warning(f"  發布警告 (Collection {resource_id}): {err.get('message')}")
                    else:
                        count = data.get("publishable", {}).get("availablePublicationsCount", {}).get("count", "?")
                        logger.info(f"  ✅ Collection {resource_id} 已發布到 {count} 個管道")
                else:
                    logger.error(f"  Collection 發布失敗: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                logger.error(f"  Collection 發布異常: {e}")

    def get_existing_skus(self) -> set:
        """取得 Shopify 上已有的所有商品貨號（adidas 商品編號）"""
        if self._existing_skus is not None:
            return self._existing_skus

        skus = set()
        url = f"{self.base_url}/products.json?limit=250&fields=id,variants,tags"
        while url:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200:
                    logger.error(f"Shopify API 錯誤: {resp.status_code}")
                    break
                data = resp.json()
                for product in data.get("products", []):
                    # 從 variant SKU 提取基礎貨號（B75807-220 → B75807）
                    for variant in product.get("variants", []):
                        sku = variant.get("sku", "")
                        if sku:
                            base_sku = sku.split("-")[0].upper()
                            skus.add(base_sku)
                            skus.add(sku.upper())

                # 分頁
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    import re as _re
                    match = _re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                    url = match.group(1) if match else None
                else:
                    url = None
            except Exception as e:
                logger.error(f"取得 SKU 失敗: {e}")
                break

        logger.info(f"Shopify 已有 {len(skus)} 個 SKU")
        self._existing_skus = skus
        return skus

    def is_duplicate(self, sku: str) -> bool:
        """檢查商品貨號是否已存在"""
        return sku.upper() in self.get_existing_skus()

    def get_or_create_collection(self, title: str) -> int | None:
        """取得或建立 Collection，並確保發布到所有管道"""
        if title in self._collection_cache:
            return self._collection_cache[title]

        # 搜尋現有 Collection
        try:
            resp = requests.get(
                f"{self.base_url}/custom_collections.json?title={title}",
                headers=self.headers,
                timeout=30,
            )
            if resp.status_code == 200:
                collections = resp.json().get("custom_collections", [])
                for c in collections:
                    if c["title"] == title:
                        self._collection_cache[title] = c["id"]
                        logger.info(f"找到現有 Collection: {title} (ID: {c['id']})")
                        # 確保既有的也發布到所有管道
                        self.publish_to_all_channels("Collection", c["id"])
                        return c["id"]
        except Exception:
            pass

        # 建立新 Collection
        try:
            resp = requests.post(
                f"{self.base_url}/custom_collections.json",
                headers=self.headers,
                json={"custom_collection": {"title": title, "published": True}},
                timeout=30,
            )
            if resp.status_code == 201:
                cid = resp.json()["custom_collection"]["id"]
                self._collection_cache[title] = cid
                logger.info(f"建立新 Collection: {title} (ID: {cid})")
                # 發布到所有銷售管道
                self.publish_to_all_channels("Collection", cid)
                return cid
        except Exception as e:
            logger.error(f"建立 Collection 失敗: {e}")

        return None

    def upload_product(
        self,
        product: dict,
        detail: dict | None,
        collection_id: int | None,
        translate: bool = True,
    ) -> dict:
        """上架單個商品到 Shopify"""
        title = product["title"]
        
        # 清理標題：移除日文部分，只保留英文
        # adidas 標題格式通常是 "日文名 / 英文名" 或 "英文名"
        def clean_title(t: str) -> str:
            """從標題中提取英文部分，移除日文"""
            import unicodedata
            parts = [p.strip() for p in t.split("/")]
            english_parts = []
            seen = set()
            for p in parts:
                # 檢查是否包含日文字元（平假名、片假名、CJK）
                has_japanese = any(
                    unicodedata.name(c, "").startswith(("HIRAGANA", "KATAKANA", "CJK"))
                    for c in p if c.strip()
                )
                if not has_japanese and p and p.lower() not in seen:
                    seen.add(p.lower())
                    english_parts.append(p)
            return " / ".join(english_parts) if english_parts else t
        
        clean_en_title = clean_title(title)
        
        # 取出描述各部分
        subtitle = detail.get("subtitle", "") if detail else ""
        description = detail.get("description", "") if detail else ""
        specs = detail.get("specifications", []) if detail else []
        origin = detail.get("origin", "") if detail else ""
        color = detail.get("color", "") if detail else ""

        # 翻譯（標題直接用英文，不經過翻譯）
        if translate and OPENAI_API_KEY:
            subtitle_zh = translate_ja_to_zhtw(subtitle) if subtitle else ""
            desc_zh = translate_ja_to_zhtw(description) if description else ""
            # 規格一次性翻譯（合併送出節省 API 呼叫）
            if specs:
                specs_text = "\n".join(specs)
                specs_zh = translate_ja_to_zhtw(specs_text)
                specs_zh_list = [s.strip() for s in specs_zh.split("\n") if s.strip()]
            else:
                specs_zh_list = []
            origin_zh = translate_ja_to_zhtw(origin) if origin else ""
        else:
            subtitle_zh = subtitle
            desc_zh = description
            specs_zh_list = specs
            origin_zh = origin

        # 標題直接用清理後的英文名，加上前綴
        full_title = f"adidas｜original｜原創系列｜{clean_en_title}"

        # 圖片
        images = []
        if detail and detail.get("images"):
            for img_url in detail["images"][:20]:  # 最多 20 張
                images.append({"src": img_url})
        elif product.get("image"):
            images.append({"src": product["image"]})

        # 組合描述 HTML
        body_html = self._build_description_html(
            subtitle_zh=subtitle_zh,
            desc_zh=desc_zh,
            specs=specs_zh_list,
            origin=origin_zh,
            color=color,
            sku=product["sku"],
        )

        # 根據性別決定 Collections
        gender = detail.get("gender", "unisex") if detail else "unisex"
        collection_names = self._get_collection_names_by_gender(gender, product.get("collection_name", ""))

        # 建立尺碼 variants（全部尺碼，庫存依有無貨設定）
        sizes = detail.get("sizes", []) if detail else []
        color = detail.get("color", "") if detail else ""
        
        if sizes:
            variants = []
            for s in sizes:
                variant = {
                    "option1": s["size"],
                    "price": str(product["selling_price"]),
                    "compare_at_price": None,
                    "sku": f"{product['sku']}-{s['size'].replace('.', '').replace('cm', '')}",
                    "inventory_management": "shopify",
                    "requires_shipping": True,
                }
                variants.append(variant)
            options = [{"name": "尺碼", "values": [s["size"] for s in sizes]}]
            # 記錄每個尺碼的庫存量（有貨=2, 缺貨=0）
            size_stock = {s["size"]: 2 if s["available"] else 0 for s in sizes}
        else:
            variants = [
                {
                    "price": str(product["selling_price"]),
                    "compare_at_price": None,
                    "sku": product["sku"],
                    "inventory_management": "shopify",
                    "requires_shipping": True,
                }
            ]
            options = []
            size_stock = {"__default__": 2}  # 無尺碼時預設庫存 2

        # 用 ChatGPT 生成 SEO meta title 和 description
        seo = self._generate_seo(clean_en_title, subtitle_zh, desc_zh, color, product["sku"])

        # Shopify product payload - 所有銷路管道全開
        payload = {
            "product": {
                "title": full_title,
                "body_html": body_html,
                "vendor": "adidas",
                "product_type": "鞋類",
                "tags": [
                    "adidas",
                    product.get("subtitle", ""),
                    product["sku"],
                    color,
                ],
                "variants": variants,
                "images": images,
                "status": "active",
                "published": True,
                "published_scope": "global",
                "metafields_global_title_tag": seo.get("title", full_title),
                "metafields_global_description_tag": seo.get("description", ""),
            }
        }
        
        if options:
            payload["product"]["options"] = options

        try:
            resp = _api_request_with_retry(
                "POST",
                f"{self.base_url}/products.json",
                headers=self.headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code == 201:
                shopify_product = resp.json()["product"]
                product_id = shopify_product["id"]

                # 設定庫存數量
                if size_stock:
                    self._set_inventory_levels(shopify_product, size_stock)

                # 設定 metafield custom.link（原始商品連結）
                self._set_product_metafield(product_id, product["url"])

                # 加入所有相關 Collections
                for col_name in collection_names:
                    col_id = self.get_or_create_collection(col_name)
                    if col_id:
                        self._add_to_collection(product_id, col_id)
                        logger.info(f"  📂 加入 Collection: {col_name}")

                # 發布到所有銷售管道
                self.publish_to_all_channels("Product", product_id)

                gender_label = {"men": "男", "women": "女", "unisex": "男+女"}
                logger.info(
                    f"✅ 上架成功: {product['sku']} - {title} → ¥{product['selling_price']} "
                    f"[{gender_label.get(gender, '?')}]"
                )

                self._existing_skus.add(product["sku"].upper())
                return {"success": True, "product_id": product_id}
            else:
                logger.error(f"❌ 上架失敗: {product['sku']} - {resp.status_code} {resp.text[:200]}")
                return {"success": False, "error": resp.text[:200]}
        except Exception as e:
            logger.error(f"❌ 上架異常: {product['sku']} - {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def _generate_seo(title_en: str, subtitle_zh: str, desc_zh: str, color: str, sku: str) -> dict:
        """用 ChatGPT 生成 SEO meta title 和 description（含 retry）"""
        if not OPENAI_API_KEY:
            return {}
        
        prompt_text = f"""商品名稱: {title_en}
商品描述: {subtitle_zh} {desc_zh}
顏色: {color}
型號: {sku}
品牌: adidas Originals
商店: GOYOUTATI 日本代購"""

        for attempt in range(3):
            try:
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "你是 SEO 專家。根據商品資訊生成搜尋引擎優化的頁面標題和 Meta 描述。"
                                    "規則："
                                    "1. 頁面標題(title)：最多 60 字元，包含品牌名、商品名、關鍵字。格式範例：adidas Samba OG 經典鞋款｜GOYOUTATI 日本代購"
                                    "2. Meta 描述(description)：最多 155 字元，自然流暢的繁體中文，包含商品賣點、顏色、適合場景，吸引點擊。"
                                    "3. 不要出現日文。"
                                    "4. 只回傳 JSON 格式：{\"title\": \"...\", \"description\": \"...\"}"
                                    "5. 不要加 markdown 格式或反引號。"
                                ),
                            },
                            {"role": "user", "content": prompt_text},
                        ],
                        "temperature": 0,
                        "max_tokens": 300,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"].strip()
                    content = content.replace("```json", "").replace("```", "").strip()
                    import json
                    seo = json.loads(content)
                    logger.info(f"  🔍 SEO: {seo.get('title', '')[:50]}...")
                    return seo
                elif resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 3 * (attempt + 1)))
                    logger.warning(f"  ⏳ OpenAI rate limit (SEO)，等待 {wait}s...")
                    time.sleep(wait)
                    continue
                else:
                    logger.warning(f"  ⚠️ SEO 生成失敗: {resp.status_code}")
                    return {}
            except Exception as e:
                logger.warning(f"  ⚠️ SEO 生成失敗 (attempt {attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(2)
                    continue
                return {}
        return {}

    def _set_product_metafield(self, product_id: int, url: str):
        """設定商品 metafield custom.link"""
        try:
            resp = _api_request_with_retry(
                "POST",
                f"{self.base_url}/products/{product_id}/metafields.json",
                headers=self.headers,
                json={
                    "metafield": {
                        "namespace": "custom",
                        "key": "link",
                        "value": url,
                        "type": "url",
                    }
                },
                timeout=30,
            )
            if resp.status_code in (200, 201):
                logger.info(f"  🔗 Metafield custom.link 已設定")
            else:
                logger.warning(f"  ⚠️ Metafield 設定失敗: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            logger.warning(f"  ⚠️ Metafield 設定失敗: {e}")

    def _set_inventory_levels(self, shopify_product: dict, size_stock: dict):
        """設定每個 variant 的庫存數量"""
        try:
            # 從第一個 variant 的 inventory_item_id 查出 location_id
            first_variant = shopify_product.get("variants", [{}])[0]
            first_inv_id = first_variant.get("inventory_item_id")
            
            if not first_inv_id:
                logger.warning("  ⚠️ 找不到 inventory_item_id")
                return

            # 方法1: 透過 inventory_levels 取得 location_id
            inv_url = f"{self.base_url}/inventory_levels.json?inventory_item_ids={first_inv_id}"
            inv_resp = _api_request_with_retry("GET", inv_url, headers=self.headers, timeout=30)
            
            inv_levels = inv_resp.json().get("inventory_levels", [])
            
            if inv_levels:
                location_id = inv_levels[0]["location_id"]
            else:
                # 方法2: locations API
                loc_url = f"{self.base_url}/locations.json"
                loc_resp = _api_request_with_retry("GET", loc_url, headers=self.headers, timeout=30)
                
                locations = loc_resp.json().get("locations", [])
                if not locations:
                    logger.warning(f"  ⚠️ 找不到 location ({inv_resp.status_code})")
                    return
                location_id = locations[0]["id"]

            # 設定庫存
            in_stock = 0
            out_stock = 0
            errors = 0
            has_default = "__default__" in size_stock
            for variant in shopify_product.get("variants", []):
                size_name = variant.get("option1", "")
                if has_default:
                    qty = size_stock["__default__"]
                else:
                    qty = size_stock.get(size_name, 0)
                inventory_item_id = variant.get("inventory_item_id")
                if not inventory_item_id:
                    continue

                resp = _api_request_with_retry(
                    "POST",
                    f"{self.base_url}/inventory_levels/set.json",
                    headers=self.headers,
                    json={
                        "location_id": location_id,
                        "inventory_item_id": inventory_item_id,
                        "available": qty,
                    },
                    timeout=30,
                )
                if resp.status_code != 200:
                    if errors == 0:  # 只印第一個錯誤
                        logger.warning(f"    庫存設定失敗 {size_name}: {resp.status_code} {resp.text[:200]}")
                    errors += 1
                    continue
                    
                if qty > 0:
                    in_stock += 1
                else:
                    out_stock += 1

            if errors:
                logger.warning(f"  ⚠️ 庫存設定: {errors} 個失敗")
            logger.info(f"  📦 庫存設定完成: {in_stock} 個有貨(2), {out_stock} 個缺貨(0)")
        except Exception as e:
            logger.warning(f"  ⚠️ 庫存設定失敗: {e}")
            import traceback
            logger.warning(traceback.format_exc())

    @staticmethod
    def _get_collection_names_by_gender(gender: str, default_collection: str) -> list:
        """根據性別決定要加入哪些 Collections"""
        if gender == "men":
            return ["adidas 男鞋"]
        elif gender == "women":
            return ["adidas 女鞋"]
        else:
            # unisex 或未知 → 兩個都加
            return ["adidas 男鞋", "adidas 女鞋"]

    def _add_to_collection(self, product_id: int, collection_id: int):
        """將商品加入 Collection"""
        try:
            requests.post(
                f"{self.base_url}/collects.json",
                headers=self.headers,
                json={
                    "collect": {
                        "product_id": product_id,
                        "collection_id": collection_id,
                    }
                },
                timeout=30,
            )
        except Exception:
            pass

    @staticmethod
    def _build_description_html(
        subtitle_zh: str,
        desc_zh: str,
        specs: list,
        origin: str,
        color: str,
        sku: str,
    ) -> str:
        """組合商品描述 HTML"""
        parts = []

        # 説明區塊
        if subtitle_zh:
            parts.append(f"<h3>{subtitle_zh}</h3>")
        if desc_zh:
            parts.append(f"<p>{desc_zh}</p>")

        # 詳細規格
        if specs:
            parts.append("<h3>商品詳細</h3>")
            parts.append("<ul>")
            for spec in specs:
                parts.append(f"  <li>{spec}</li>")
            parts.append("</ul>")

        # 商品資訊表
        info_rows = []
        if color:
            info_rows.append(f'<tr><td><strong>顏色</strong></td><td>{color}</td></tr>')
        info_rows.append(f'<tr><td><strong>型號</strong></td><td>{sku}</td></tr>')
        if origin:
            info_rows.append(f'<tr><td><strong>產地</strong></td><td>{origin}</td></tr>')
        
        if info_rows:
            parts.append("<table>")
            parts.extend(info_rows)
            parts.append("</table>")

        return "\n".join(parts)
