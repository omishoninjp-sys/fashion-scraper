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
    "men_shoes": {
        "name": "男鞋",
        "url": "https://www.adidas.jp/men-shoes",
        "collection": "adidas 男鞋",
    },
    "women_shoes": {
        "name": "女鞋",
        "url": "https://www.adidas.jp/women-shoes",
        "collection": "adidas 女鞋",
    },
}

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
def translate_ja_to_zhtw(text: str) -> str:
    """用 OpenAI ChatGPT 將日文翻譯為繁體中文"""
    if not text or not text.strip():
        return text
    if not OPENAI_API_KEY:
        logger.warning("未設定 OPENAI_API_KEY，跳過翻譯")
        return text

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
                            "只回傳翻譯結果，不要加任何解釋。"
                            "品牌名和型號名保留原文（英文）。"
                            "如果原文已經是英文或中文，直接回傳原文。"
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            logger.error(f"翻譯 API 錯誤: {resp.status_code}")
            return text
    except Exception as e:
        logger.error(f"翻譯失敗: {e}")
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
        """啟動瀏覽器"""
        from playwright.async_api import async_playwright

        self.pw = await async_playwright().start()
        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if PROXY_URL:
            launch_args["proxy"] = {"server": PROXY_URL}

        self.browser = await self.pw.chromium.launch(**launch_args)
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        self.page = await self.context.new_page()

    async def close_browser(self):
        """關閉瀏覽器"""
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def scrape_listing_page(self, category_url: str, max_pages: int = 5) -> list:
        """
        爬取商品列表頁，回傳商品基本資訊列表
        會自動滾動載入更多商品
        """
        products = []
        logger.info(f"正在載入列表頁: {category_url}")

        await self.page.goto(category_url, wait_until="networkidle", timeout=60000)
        # 等待商品卡片出現
        await self.page.wait_for_selector(
            '[data-testid="plp-product-card"]', timeout=30000
        )

        # 關閉可能出現的 cookie/popup 彈窗
        await self._close_popups()

        # 滾動載入更多商品
        loaded_pages = 0
        prev_count = 0
        while loaded_pages < max_pages:
            # 滾動到頁面底部
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(2000)

            # 嘗試點擊「載入更多」按鈕
            try:
                load_more = self.page.locator(
                    'button:has-text("もっと見る"), button:has-text("LOAD MORE"), [data-testid="load-more"]'
                )
                if await load_more.count() > 0:
                    await load_more.first.click()
                    await self.page.wait_for_timeout(3000)
            except Exception:
                pass

            # 計算目前的商品數量
            cards = await self.page.query_selector_all(
                '[data-testid="plp-product-card"]'
            )
            current_count = len(cards)
            logger.info(f"  目前載入 {current_count} 個商品")

            if current_count == prev_count:
                # 沒有新商品了
                break
            prev_count = current_count
            loaded_pages += 1

        # 解析所有商品卡片
        cards = await self.page.query_selector_all('[data-testid="plp-product-card"]')
        logger.info(f"共找到 {len(cards)} 個商品卡片")

        for card in cards:
            try:
                product = await self._parse_card(card)
                if product:
                    products.append(product)
            except Exception as e:
                logger.warning(f"解析商品卡片失敗: {e}")
                continue

        return products

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
        """
        try:
            await self.page.goto(product_url, wait_until="networkidle", timeout=60000)
            await self.page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"載入商品頁失敗: {product_url} - {e}")
            return None

        detail = {}

        # 商品描述
        try:
            desc_el = await self.page.query_selector(
                '[data-testid="product-description"], .pdp-description, [class*="description"]'
            )
            if desc_el:
                detail["description"] = await desc_el.inner_text()
            else:
                detail["description"] = ""
        except Exception:
            detail["description"] = ""

        # 所有商品圖片
        try:
            img_elements = await self.page.query_selector_all(
                '[data-testid="pdp-gallery-image"] img, '
                '[class*="gallery"] img, '
                '[class*="slider"] img[src*="assets.adidas.com"]'
            )
            images = []
            seen = set()
            for img in img_elements:
                src = await img.get_attribute("src")
                if src and "assets.adidas.com" in src:
                    # 高解析度
                    hi_res = re.sub(r"w_\d+,h_\d+", "w_840,h_840", src)
                    if hi_res not in seen:
                        seen.add(hi_res)
                        images.append(hi_res)
            detail["images"] = images
        except Exception:
            detail["images"] = []

        # 可選尺碼
        try:
            size_elements = await self.page.query_selector_all(
                '[data-testid="size-selector"] button, '
                '[class*="size"] button[data-testid*="size"]'
            )
            sizes = []
            for btn in size_elements:
                size_text = await btn.inner_text()
                is_disabled = await btn.get_attribute("disabled")
                sizes.append({
                    "size": size_text.strip(),
                    "available": is_disabled is None,
                })
            detail["sizes"] = sizes
        except Exception:
            detail["sizes"] = []

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
        self.base_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2024-01"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }
        self._existing_skus = None
        self._collection_cache = {}

    def get_existing_skus(self) -> set:
        """取得 Shopify 上已有的所有 SKU"""
        if self._existing_skus is not None:
            return self._existing_skus

        skus = set()
        url = f"{self.base_url}/products.json?limit=250&fields=id,variants"
        while url:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200:
                    logger.error(f"Shopify API 錯誤: {resp.status_code}")
                    break
                data = resp.json()
                for product in data.get("products", []):
                    for variant in product.get("variants", []):
                        sku = variant.get("sku", "")
                        if sku:
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
        """檢查 SKU 是否已存在"""
        return sku.upper() in self.get_existing_skus()

    def get_or_create_collection(self, title: str) -> int | None:
        """取得或建立 Collection"""
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
                        return c["id"]
        except Exception:
            pass

        # 建立新 Collection
        try:
            resp = requests.post(
                f"{self.base_url}/custom_collections.json",
                headers=self.headers,
                json={"custom_collection": {"title": title}},
                timeout=30,
            )
            if resp.status_code == 201:
                cid = resp.json()["custom_collection"]["id"]
                self._collection_cache[title] = cid
                logger.info(f"建立新 Collection: {title} (ID: {cid})")
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
        description = ""

        if detail:
            description = detail.get("description", "")

        # 翻譯
        if translate and OPENAI_API_KEY:
            title_zh = translate_ja_to_zhtw(title)
            desc_zh = translate_ja_to_zhtw(description) if description else ""
        else:
            title_zh = title
            desc_zh = description

        # 組合標題: 中文名 + 英文/日文原名
        if title_zh != title:
            full_title = f"{title_zh} / {title}"
        else:
            full_title = title

        # 圖片
        images = []
        if detail and detail.get("images"):
            for img_url in detail["images"][:10]:  # 最多 10 張
                images.append({"src": img_url})
        elif product.get("image"):
            images.append({"src": product["image"]})

        # 組合描述 HTML
        body_html = self._build_description_html(product, desc_zh)

        # Shopify product payload
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
                ],
                "variants": [
                    {
                        "price": str(product["selling_price"]),
                        "compare_at_price": None,
                        "sku": product["sku"],
                        "inventory_management": None,
                        "requires_shipping": True,
                    }
                ],
                "images": images,
                "status": "active",
            }
        }

        try:
            resp = requests.post(
                f"{self.base_url}/products.json",
                headers=self.headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code == 201:
                shopify_product = resp.json()["product"]
                product_id = shopify_product["id"]
                logger.info(
                    f"✅ 上架成功: {product['sku']} - {title} → ¥{product['selling_price']}"
                )

                # 加入 Collection
                if collection_id:
                    self._add_to_collection(product_id, collection_id)

                self._existing_skus.add(product["sku"].upper())
                return {"success": True, "product_id": product_id}
            else:
                logger.error(f"❌ 上架失敗: {product['sku']} - {resp.status_code} {resp.text[:200]}")
                return {"success": False, "error": resp.text[:200]}
        except Exception as e:
            logger.error(f"❌ 上架異常: {product['sku']} - {e}")
            return {"success": False, "error": str(e)}

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
    def _build_description_html(product: dict, description_zh: str) -> str:
        """組合商品描述 HTML"""
        parts = []

        if description_zh:
            parts.append(f"<p>{description_zh}</p>")

        parts.append("<hr>")
        parts.append("<table>")
        parts.append(f'<tr><td><strong>品牌</strong></td><td>adidas</td></tr>')
        parts.append(
            f'<tr><td><strong>系列</strong></td><td>{product.get("subtitle", "")}</td></tr>'
        )
        parts.append(f'<tr><td><strong>型號</strong></td><td>{product["sku"]}</td></tr>')
        parts.append(
            f'<tr><td><strong>日本官網售價</strong></td><td>¥{product["price_jpy"]:,}</td></tr>'
        )
        parts.append("</table>")
        parts.append("<hr>")
        parts.append(
            f'<p><small>📎 <a href="{product["url"]}" target="_blank">'
            f"adidas.jp 官網連結</a></small></p>"
        )

        return "\n".join(parts)
