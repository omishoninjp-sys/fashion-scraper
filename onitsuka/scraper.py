"""
Onitsuka Tiger Japan 爬蟲 (GraphQL + Shopify)
===============================================
- 使用 Magento GraphQL API 爬取 onitsukatiger.com/jp
- 分類：男裝、女裝（全品類：鞋、服飾、包包、配件）
- 定價公式: (售價 + ¥1,250) ÷ 0.7 = Shopify售價 (日幣)
- ChatGPT 日文→繁體中文翻譯
- 自動上架到 Shopify + Collection 管理
- 重複商品自動跳過（SKU 比對）
"""

import os
import re
import json
import math
import time
import logging
import requests
from datetime import datetime
from html import unescape

# ============================================================
# Logging
# ============================================================
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("onitsuka")

# ============================================================
# 環境變數
# ============================================================
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ============================================================
# Onitsuka Tiger GraphQL 設定
# ============================================================
GRAPHQL_URL = "https://www.onitsukatiger.com/jp/ja-jp/graphql"
BASE_URL = "https://www.onitsukatiger.com"
STORE_CODE = "default"

# 分類設定：男裝 / 女裝
CATEGORIES = {
    "men": {
        "name": "男裝（全品類）",
        "collection": "Onitsuka Tiger 男裝",
        # MEN category in Magento — 會在 init 時用 GraphQL 取得 uid
        "url_path": "store/men",
        "uid": None,
    },
    "women": {
        "name": "女裝（全品類）",
        "collection": "Onitsuka Tiger 女裝",
        "url_path": "store/women",
        "uid": None,
    },
}

PAGE_SIZE = 48
REQUEST_DELAY = 0.3


# ============================================================
# 定價公式
# ============================================================
def calculate_price(original_price_jpy: int) -> int:
    """(售價 + 1250) / 0.7 = Shopify售價，無條件進位"""
    raw = (original_price_jpy + 1250) / 0.7
    return math.ceil(raw)


# ============================================================
# 翻譯 (ChatGPT API)
# ============================================================
def translate_ja_to_zhtw(text: str) -> str:
    """用 OpenAI ChatGPT 將日文翻譯為繁體中文"""
    if not text or not text.strip():
        return text
    if not OPENAI_API_KEY:
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
                                "你是翻譯專家。請將以下日文商品描述翻譯成繁體中文。\n"
                                "嚴格規則：\n"
                                "1. 只回傳翻譯結果，不要加任何解釋。\n"
                                "2. 品牌名和型號名保留英文原文（如 MEXICO 66, SERRANO, Onitsuka Tiger 等）。\n"
                                "3. 【最重要】輸出中絕對禁止出現任何日文字元：\n"
                                "   - 禁止平假名（あ-ん）\n"
                                "   - 禁止片假名（ア-ン、オニツカタイガー→Onitsuka Tiger、ストライプ→條紋）\n"
                                "   - 所有片假名外來語必須翻譯成中文或還原成英文原文\n"
                                "   - 例：オニツカタイガーストライプ→Onitsuka Tiger 條紋\n"
                                "   - 例：デラックス→DELUXE、レザー→皮革、スニーカー→運動鞋\n"
                                "4. 如果原文已經是英文或中文，直接回傳原文。\n"
                                "5. 適當換行讓內容好閱讀：\n"
                                "   - 每個句子結束後換行\n"
                                "   - 商品特點用 ・ 開頭，每項獨立一行\n"
                                "   - 不要使用 HTML 標籤換行，直接用換行符\n"
                                "6. HTML 標籤保持不變。\n"
                                "7. 翻譯完成後自我檢查，如果輸出中仍有任何日文字元，必須全部替換。"
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0,
                    "max_tokens": 2000,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"].strip()
                # 最後防線：程式化清除殘留日文
                result = _strip_japanese_chars(result)
                return result
            elif resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 3 * (attempt + 1)))
                logger.warning(f"  ⏳ OpenAI rate limit，等待 {wait}s...")
                time.sleep(wait)
                continue
            else:
                logger.error(f"翻譯 API 錯誤: {resp.status_code}")
                return _strip_japanese_chars(text)
        except Exception as e:
            logger.error(f"翻譯失敗 (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(2)
            return _strip_japanese_chars(text)
    return _strip_japanese_chars(text)


def _strip_japanese_chars(text: str) -> str:
    """
    程式化清除文字中殘留的日文字元（平假名、片假名）
    這是翻譯後的最後防線
    """
    if not text:
        return text
    # 平假名 U+3040-U+309F, 片假名 U+30A0-U+30FF
    # 但保留常用中日共用標點（・等）
    cleaned = re.sub(r'[\u3040-\u309F]', '', text)         # 移除平假名
    cleaned = re.sub(r'[\u30A1-\u30F6\u30F8-\u30FA]', '', cleaned)  # 移除片假名（保留 ・ U+30FB）
    cleaned = re.sub(r'[\u30FC]', '—', cleaned)             # 長音符 ー → 破折號
    # 清理多餘空白
    cleaned = re.sub(r'  +', ' ', cleaned)
    cleaned = re.sub(r' ([，。、])', r'\1', cleaned)
    return cleaned.strip()


class DailyLimitReached(Exception):
    """Shopify 每日 variant 建立上限已達"""
    pass


def _api_request_with_retry(method, url, max_retries=3, **kwargs):
    """帶 retry 的 API 請求（處理 429 rate limit）"""
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429:
            # 檢查是否為 daily limit（不是一般 rate limit，retry 也沒用）
            try:
                body = resp.json()
                errors = body.get("errors", {})
                error_text = json.dumps(errors)
                if "Daily variant creation limit" in error_text or "daily" in error_text.lower():
                    raise DailyLimitReached("Shopify 每日 variant 建立上限已達，需等待 24 小時重置")
            except (ValueError, DailyLimitReached) as e:
                if isinstance(e, DailyLimitReached):
                    raise
            # 一般 rate limit → retry
            retry_after = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
            logger.warning(f"  ⏳ Rate limit (429)，等待 {retry_after}s...")
            time.sleep(retry_after)
            continue
        return resp
    return resp


# ============================================================
# GraphQL 爬蟲核心
# ============================================================
class OnitsukaScraper:
    """使用 Magento GraphQL API 爬取 Onitsuka Tiger Japan"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Content-Type": "application/json",
            "Store": STORE_CODE,
            "Referer": f"{BASE_URL}/jp/ja-jp/",
            "Origin": BASE_URL,
        })

    def init(self):
        """初始化：取 cookies + 解析分類 UID + 解析 gender 對應表"""
        logger.info("初始化 session...")
        self._gender_map = {}  # Magento option_id → 性別文字
        try:
            self.session.get(f"{BASE_URL}/jp/ja-jp/", timeout=15)
            logger.info(f"  Cookies: {list(self.session.cookies.get_dict().keys())}")
        except Exception as e:
            logger.warning(f"  取 cookies 失敗: {e}")

        # 取得分類 UID
        self._resolve_category_uids()
        # 取得 gender attribute 的 option 對應表
        self._resolve_gender_mapping()

    def _graphql(self, query, retries=3):
        """發送 GraphQL 請求"""
        for attempt in range(retries):
            try:
                resp = self.session.post(
                    GRAPHQL_URL,
                    json={"query": query},
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if "errors" in data:
                        logger.warning(f"  GraphQL errors: {json.dumps(data['errors'], ensure_ascii=False)[:200]}")
                    return data.get("data")
                elif resp.status_code == 429:
                    time.sleep((attempt + 1) * 3)
                    continue
                elif resp.status_code == 503:
                    time.sleep((attempt + 1) * 2)
                    continue
                else:
                    logger.error(f"  GraphQL HTTP {resp.status_code}")
                    return None
            except requests.exceptions.Timeout:
                logger.warning(f"  GraphQL timeout, retry {attempt+1}/{retries}")
                time.sleep(2)
            except Exception as e:
                logger.error(f"  GraphQL error: {e}")
                return None
        return None

    def _resolve_category_uids(self):
        """用 GraphQL 取得男裝/女裝分類的 UID"""
        logger.info("解析分類 UID...")
        query = """
        {
            categories(filters: {}, pageSize: 50, currentPage: 1) {
                items {
                    id uid name url_path product_count level
                    children {
                        id uid name url_path product_count level
                    }
                }
            }
        }
        """
        data = self._graphql(query)
        if not data:
            logger.error("無法取得分類，將使用搜尋模式")
            return

        all_cats = data.get("categories", {}).get("items", [])

        # 展平搜尋
        def find_cat(cats, target_path):
            for c in cats:
                if c.get("url_path") == target_path:
                    return c
                children = c.get("children", [])
                found = find_cat(children, target_path)
                if found:
                    return found
            return None

        for key, cat_config in CATEGORIES.items():
            found = find_cat(all_cats, cat_config["url_path"])
            if found:
                cat_config["uid"] = found["uid"]
                cat_config["magento_id"] = found["id"]
                logger.info(f"  ✅ {cat_config['name']}: uid={found['uid']}, id={found['id']}, products={found.get('product_count', '?')}")
            else:
                logger.warning(f"  ⚠️ 找不到分類: {cat_config['url_path']}")

    def _resolve_gender_mapping(self):
        """
        用 customAttributeMetadata 查詢 gender attribute 的 option 對應表
        Magento 的 gender 回傳數字 (如 2787)，需要對應到 MEN/WOMEN/UNISEX
        """
        logger.info("解析 gender 對應表...")
        query = """
        {
            customAttributeMetadata(attributes: [
                { attribute_code: "gender", entity_type: "catalog_product" }
            ]) {
                items {
                    attribute_code
                    attribute_options {
                        value
                        label
                    }
                }
            }
        }
        """
        data = self._graphql(query)
        if not data:
            logger.warning("  ⚠️ 無法查詢 gender 對應表，使用分類 fallback")
            return

        items = data.get("customAttributeMetadata", {}).get("items", [])
        for item in items:
            if item.get("attribute_code") == "gender":
                for opt in item.get("attribute_options", []):
                    val = str(opt.get("value", ""))
                    label = str(opt.get("label", "")).strip().upper()
                    self._gender_map[val] = label
                    logger.info(f"  gender {val} → {label}")

        if self._gender_map:
            logger.info(f"  ✅ gender 對應表: {len(self._gender_map)} 個選項")
        else:
            logger.warning("  ⚠️ gender attribute 沒有 options，使用分類 fallback")

    def scrape_category(self, category_key: str, max_pages: int = 0) -> list:
        """
        爬取指定分類的所有商品
        max_pages=0 表示全部
        """
        cat = CATEGORIES.get(category_key)
        if not cat:
            logger.error(f"無效分類: {category_key}")
            return []

        uid = cat.get("uid")
        if not uid:
            logger.warning(f"分類 {cat['name']} 沒有 UID，嘗試用搜尋...")
            return []

        logger.info(f"=== 開始爬取: {cat['name']} (uid={uid}) ===")

        all_products = []
        page = 1

        while True:
            # 帶 gender 欄位查詢（Magento 自訂屬性可能叫 gender 也可能不存在）
            items_fields = """
                        id uid name sku url_key type_id
                        stock_status
                        %s
                        price_range {
                            minimum_price {
                                regular_price { value currency }
                                final_price { value currency }
                                discount { amount_off percent_off }
                            }
                        }
                        image { url label }
                        media_gallery { url label position }
                        short_description { html }
                        description { html }
                        ... on ConfigurableProduct {
                            configurable_options {
                                attribute_code label
                                values { value_index label }
                            }
                            variants {
                                product {
                                    id sku name stock_status
                                    image { url label }
                                }
                                attributes { code label value_index }
                            }
                        }
            """

            # 第一次嘗試帶 gender
            gender_field = "gender" if not hasattr(self, '_gender_field_broken') else ""
            query = """
            {
                products(
                    filter: { category_uid: { eq: "%s" } }
                    pageSize: %d
                    currentPage: %d
                    sort: { position: ASC }
                ) {
                    total_count
                    items {
                        %s
                    }
                    page_info { current_page page_size total_pages }
                }
            }
            """ % (uid, PAGE_SIZE, page, items_fields % gender_field)

            data = self._graphql(query)

            # 如果 gender 欄位導致 GraphQL 報錯，標記後不帶 gender 重試
            if data is None and not hasattr(self, '_gender_field_broken'):
                logger.warning("  ⚠️ GraphQL 查詢失敗，嘗試不帶 gender 欄位...")
                self._gender_field_broken = True
                query = """
                {
                    products(
                        filter: { category_uid: { eq: "%s" } }
                        pageSize: %d
                        currentPage: %d
                        sort: { position: ASC }
                    ) {
                        total_count
                        items {
                            %s
                        }
                        page_info { current_page page_size total_pages }
                    }
                }
                """ % (uid, PAGE_SIZE, page, items_fields % "")
                data = self._graphql(query)

            if not data or "products" not in data:
                logger.error(f"  第 {page} 頁查詢失敗")
                break

            products = data["products"]
            items = products.get("items", [])
            total_count = products.get("total_count", 0)
            total_pages = products.get("page_info", {}).get("total_pages", 1)

            if page == 1:
                logger.info(f"  共 {total_count} 個商品, {total_pages} 頁")

            # 轉換為統一格式
            for item in items:
                product = self._normalize_product(item, category_key)
                if product:
                    # SKU 去重
                    if not any(p["sku"] == product["sku"] for p in all_products):
                        all_products.append(product)

            logger.info(f"  第 {page}/{total_pages} 頁: +{len(items)} 商品 (累計 {len(all_products)})")

            if page >= total_pages:
                break
            if max_pages > 0 and page >= max_pages:
                logger.info(f"  已達最大頁數限制 ({max_pages})")
                break

            page += 1
            time.sleep(REQUEST_DELAY)

        logger.info(f"  ✅ {cat['name']} 共取得 {len(all_products)} 個不重複商品")
        return all_products

    def _normalize_product(self, item: dict, category_key: str) -> dict | None:
        """將 GraphQL 商品資料正規化為統一格式"""
        sku = item.get("sku", "")
        if not sku:
            return None

        # 價格
        price_info = item.get("price_range", {}).get("minimum_price", {})
        regular_price = price_info.get("regular_price", {}).get("value", 0)
        final_price = price_info.get("final_price", {}).get("value", 0)
        discount = price_info.get("discount", {})

        # 取整數價格
        price_jpy = int(round(final_price)) if final_price else int(round(regular_price))
        if price_jpy <= 0:
            return None

        # URL（在圖片之前計算，因為 fallback 抓圖需要）
        url_key = item.get("url_key", "")
        product_url = f"{BASE_URL}/jp/ja-jp/{url_key}.html" if url_key else ""

        # 圖片策略：
        # GraphQL 列表查詢的 media_gallery 只回 1 張縮圖
        # 優先用 Scene7 CDN 組合高畫質圖（商品頁實際使用的圖片來源）
        # Scene7 沒圖時，抓商品頁 HTML 提取實際圖片
        scene7_images = self._build_scene7_images(sku)
        if scene7_images:
            all_images = scene7_images
            main_image = all_images[0]
        else:
            # Scene7 沒圖 → 抓商品頁 HTML 取圖
            full_images = self._fetch_product_images(sku, product_url)
            if full_images:
                all_images = full_images
                main_image = all_images[0]
            else:
                # 最後 fallback: 列表查詢的 media_gallery
                gallery_images = []
                for media in sorted(item.get("media_gallery", []), key=lambda x: x.get("position", 99)):
                    url = media.get("url", "")
                    if url and url not in gallery_images:
                        gallery_images.append(url)
                if gallery_images:
                    all_images = gallery_images
                    main_image = all_images[0]
                    logger.warning(f"  ⚠️ 僅列表縮圖: {len(all_images)} 張 ({sku})")
                else:
                    main_image = item.get("image", {}).get("url", "")
                    all_images = [main_image] if main_image else []

        # 尺寸
        sizes = []
        configurable_options = item.get("configurable_options", [])
        variants = item.get("variants", [])

        for variant in variants:
            v_product = variant.get("product", {})
            v_attrs = variant.get("attributes", [])
            size_label = ""
            for attr in v_attrs:
                if attr.get("code", "").lower() in ("size", "shoe_size", "clothing_size"):
                    size_label = attr.get("label", "")
                    break
            # 如果沒有明確 size attribute，用第一個 attribute
            if not size_label and v_attrs:
                size_label = v_attrs[0].get("label", "")

            if size_label:
                sizes.append({
                    "size": size_label,
                    "sku": v_product.get("sku", ""),
                    "available": v_product.get("stock_status") == "IN_STOCK",
                })

        # 描述
        desc_html = item.get("description", {}).get("html", "")
        short_desc_html = item.get("short_description", {}).get("html", "")

        # SKU 解析
        sku_parts = sku.split("_")
        item_code = sku_parts[0] if sku_parts else sku
        color_code = sku_parts[1] if len(sku_parts) > 1 else ""

        # 性別判斷
        # GraphQL 可能回傳 gender 欄位（數值或文字）
        # Magento 常見: 1=MEN, 2=WOMEN, 3=UNISEX，或直接文字
        raw_gender = item.get("gender")
        gender = self._parse_gender(raw_gender, category_key)
        # 只對前幾個商品印 debug（避免 log 爆量）
        if not hasattr(self, '_gender_log_count'):
            self._gender_log_count = 0
        if self._gender_log_count < 5:
            logger.info(f"  👤 {sku}: gender raw={raw_gender} → {gender}")
            self._gender_log_count += 1

        # 根據性別決定 Collections（可多個）
        collection_names = self._get_collections_by_gender(gender)

        return {
            "sku": sku,
            "item_code": item_code,
            "color_code": color_code,
            "title": item.get("name", ""),
            "price_jpy": price_jpy,
            "selling_price": calculate_price(price_jpy),
            "regular_price_jpy": int(round(regular_price)),
            "discount_percent": discount.get("percent_off", 0),
            "stock_status": item.get("stock_status", ""),
            "type": item.get("type_id", ""),
            "url": product_url,
            "image": main_image,
            "images": all_images,
            "sizes": sizes,
            "description_html": desc_html,
            "short_description_html": short_desc_html,
            "configurable_options": configurable_options,
            "category": category_key,
            "gender": gender,
            "collection_names": collection_names,
            "scraped_at": datetime.now().isoformat(),
        }

    @staticmethod
    def strip_html(html_text: str) -> str:
        """移除 HTML 標籤，取得純文字"""
        if not html_text:
            return ""
        text = re.sub(r'<[^>]+>', '', html_text)
        text = unescape(text)
        return text.strip()

    def _fetch_product_images(self, sku: str, product_url: str = "") -> list:
        """
        Scene7 沒圖的商品，直接抓商品頁 HTML 取得實際圖片 URL
        圖片在 <div class="pdp-gallery-bigimg"> 裡的 <img> tags
        URL 格式: https://asics.scene7.com/is/image/asics/...?$otmag_zoom$&qlt=99,1
        或 Magento CDN: https://static-ojp.onitsukatiger.com/media/catalog/product/...
        """
        if not product_url:
            # 從 SKU 組合 URL（需要 url_key，這裡用備用方式）
            return []

        try:
            resp = self.session.get(product_url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"  ⚠️ 商品頁 {resp.status_code}: {product_url}")
                return []

            html = resp.text
            images = []

            # 方法1: 從 pdp-gallery-bigimg 區塊提取 Scene7 圖片
            # <img src="https://asics.scene7.com/is/image/asics/1182A187_101_SR_RT_GLB-1?$otmag_zoom$&qlt=99,1"
            gallery_match = re.findall(
                r'class="pdp-gallery-img"[^>]*>.*?<img[^>]+src="([^"]+)"',
                html, re.DOTALL
            )
            if gallery_match:
                for url in gallery_match:
                    if url and url not in images:
                        images.append(url)

            # 方法2: 從 JSON-LD 或 script 裡的 gallery data 提取
            if not images:
                # 有些 Magento 會在 script 裡放 gallery JSON
                json_match = re.findall(
                    r'"full"\s*:\s*"(https?://[^"]+(?:scene7|onitsukatiger)[^"]*)"',
                    html
                )
                for url in json_match:
                    if url and url not in images:
                        images.append(url)

            # 方法3: 抓所有 scene7 或 media/catalog 圖片 URL
            if not images:
                all_img_urls = re.findall(
                    r'(https://asics\.scene7\.com/is/image/asics/[^"\'&\s]+)',
                    html
                )
                seen = set()
                for url in all_img_urls:
                    # 過濾掉 swatch 小圖和重複
                    if 'swatch' in url.lower() or 'thumbnail' in url.lower():
                        continue
                    base_url = url.split('?')[0]  # 去掉 query params 做去重
                    if base_url not in seen:
                        seen.add(base_url)
                        # 加上高畫質參數
                        final_url = f"{base_url}?$otmag_zoom$&qlt=99,1"
                        images.append(final_url)

            if images:
                logger.info(f"  📸 商品頁: {len(images)} 張圖片 ({sku})")
            else:
                logger.warning(f"  ⚠️ 商品頁也沒找到圖片: {sku}")

            time.sleep(0.5)  # 避免太快觸發反爬
            return images

        except Exception as e:
            logger.warning(f"  ⚠️ 抓商品頁失敗 ({sku}): {e}")
            return []

    def _parse_gender(self, raw_gender, fallback_category: str = "") -> str:
        """
        解析性別欄位
        Magento gender 回傳的是 attribute option ID (如 2787)
        需要透過 _gender_map 對應到 MEN/WOMEN/UNISEX
        """
        if raw_gender is None:
            if fallback_category == "men":
                return "men"
            elif fallback_category == "women":
                return "women"
            return "unisex"

        raw_str = str(raw_gender).strip()

        # 先查對應表（數字 option_id → label）
        if raw_str in self._gender_map:
            label = self._gender_map[raw_str]
        else:
            label = raw_str.upper()

        # 解析 label
        if label in ("MEN", "MALE", "M", "メンズ"):
            return "men"
        elif label in ("WOMEN", "FEMALE", "W", "F", "レディース", "ウィメンズ"):
            return "women"
        elif label in ("UNISEX", "U"):
            return "unisex"
        elif label in ("KIDS", "CHILDREN", "キッズ"):
            return "kids"

        # fallback
        if fallback_category in ("men", "women"):
            return fallback_category
        return "unisex"

    @staticmethod
    def _get_collections_by_gender(gender: str) -> list:
        """根據性別決定要加入哪些 Collections"""
        if gender == "men":
            return ["Onitsuka Tiger 男裝"]
        elif gender == "women":
            return ["Onitsuka Tiger 女裝"]
        elif gender == "kids":
            return ["Onitsuka Tiger 童裝"]
        else:
            # unisex → 男女都加
            return ["Onitsuka Tiger 男裝", "Onitsuka Tiger 女裝"]

    def _build_scene7_images(self, sku: str) -> list:
        """
        用 ASICS Scene7 CDN 組合商品圖片 URL
        
        策略：只檢查主要的 4 個角度（對應商品頁 HTML 實際顯示的），
        不浪費時間檢查所有 10 個後綴。
        """
        if not sku or "_" not in sku:
            return []

        scene7_base = f"https://asics.scene7.com/is/image/asics/{sku}"
        quality_param = "?$otmag_zoom$&qlt=99,1"

        # 商品頁實際顯示的 4 個主要角度（從你貼的 HTML 看到的）
        primary_suffixes = [
            "SR_RT_GLB-1",   # 右側（主圖）
            "SB_FR_GLB",     # 正面右
            "SR_LT_GLB",     # 左側
            "SB_FL_GLB",     # 正面左
        ]
        # 額外角度
        extra_suffixes = [
            "SB_TP_GLB",     # 俯視
            "SB_BT_GLB",     # 底部
            "SR_BK_GLB",     # 後面
        ]

        images = []

        # 先檢查主圖是否存在（如果主圖都不存在，這個 SKU 就不在 Scene7 上）
        main_url = f"{scene7_base}_{primary_suffixes[0]}{quality_param}"
        if not self._check_image_exists(main_url):
            # 嘗試不帶 -1 的主圖
            alt_main = f"{scene7_base}_SR_RT_GLB{quality_param}"
            if not self._check_image_exists(alt_main):
                return []
            else:
                images.append(alt_main)
                # 用不帶 -1 的模式繼續
                for suffix in ["SB_FR_GLB", "SR_LT_GLB", "SB_FL_GLB"]:
                    url = f"{scene7_base}_{suffix}{quality_param}"
                    if self._check_image_exists(url):
                        images.append(url)
                logger.info(f"  📸 Scene7: {len(images)} 張圖片 ({sku})")
                return images

        images.append(main_url)

        # 主圖存在 → 其餘 3 個大概率也存在，直接加入（省掉 HEAD 請求）
        for suffix in primary_suffixes[1:]:
            images.append(f"{scene7_base}_{suffix}{quality_param}")

        # 額外角度用檢查（可能不存在）
        for suffix in extra_suffixes:
            url = f"{scene7_base}_{suffix}{quality_param}"
            if self._check_image_exists(url):
                images.append(url)

        logger.info(f"  📸 Scene7: {len(images)} 張圖片 ({sku})")
        return images

    def _check_image_exists(self, url: str) -> bool:
        """
        檢查 Scene7 圖片是否存在
        Scene7 對不存在的 SKU 會回傳：
        - 200 OK + 一個極小的預設佔位圖 (通常 < 2KB)
        - 或 200 OK + 含 "default image" 的回應
        真正的商品圖片通常 > 10KB
        """
        try:
            # 用 Range header 只下載前 bytes 來判斷 Content-Length
            resp = self.session.get(
                url,
                timeout=5,
                allow_redirects=True,
                stream=True,
                headers={**self.session.headers, "Range": "bytes=0-0"},
            )
            # 檢查 Content-Range 或 Content-Length
            if resp.status_code in (200, 206):
                # 從 Content-Range 取得完整大小: "bytes 0-0/123456"
                content_range = resp.headers.get("Content-Range", "")
                if "/" in content_range:
                    total_size = int(content_range.split("/")[-1])
                    resp.close()
                    return total_size > 10000  # > 10KB = 真圖
                # 沒有 Content-Range，用 Content-Length
                content_length = int(resp.headers.get("Content-Length", "0"))
                resp.close()
                return content_length > 10000
            resp.close()
            return False
        except Exception:
            return False


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

    # --- 銷售管道 ---
    def get_publication_ids(self) -> list:
        if self._publication_ids is not None:
            return self._publication_ids
        self._publication_ids = []
        graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01/graphql.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}
        query = '{ publications(first: 20) { edges { node { id name } } } }'
        try:
            resp = requests.post(graphql_url, headers=headers, json={"query": query}, timeout=15)
            if resp.status_code == 200:
                pubs = resp.json().get("data", {}).get("publications", {}).get("edges", [])
                seen = set()
                for pub in pubs:
                    name = pub["node"]["name"]
                    if name not in seen:
                        seen.add(name)
                        self._publication_ids.append(pub["node"]["id"])
                logger.info(f"找到 {len(self._publication_ids)} 個銷售管道: {', '.join(seen)}")
        except Exception as e:
            logger.error(f"取得銷售管道異常: {e}")
        return self._publication_ids

    def publish_to_all_channels(self, resource_type: str, resource_id: int):
        pub_ids = self.get_publication_ids()
        if not pub_ids:
            return
        graphql_url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2026-01/graphql.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}
        mutation = """
        mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) {
            publishable { availablePublicationsCount { count } }
            userErrors { field message }
          }
        }
        """
        gid = f"gid://shopify/{resource_type}/{resource_id}"
        variables = {"id": gid, "input": [{"publicationId": pid} for pid in pub_ids]}
        try:
            resp = requests.post(graphql_url, headers=headers, json={"query": mutation, "variables": variables}, timeout=15)
            if resp.status_code == 200:
                errors = resp.json().get("data", {}).get("publishablePublish", {}).get("userErrors", [])
                if errors:
                    for err in errors:
                        logger.warning(f"  發布警告: {err.get('message')}")
                else:
                    logger.info(f"  ✅ {resource_type} {resource_id} 已發布到 {len(pub_ids)} 個管道")
        except Exception as e:
            logger.error(f"  發布異常: {e}")

    # --- SKU 重複檢查 ---
    def get_existing_skus(self) -> set:
        if self._existing_skus is not None:
            return self._existing_skus
        skus = set()
        url = f"{self.base_url}/products.json?limit=250&fields=id,variants,tags"
        while url:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200:
                    break
                for product in resp.json().get("products", []):
                    for variant in product.get("variants", []):
                        sku = variant.get("sku", "")
                        if sku:
                            base_sku = sku.split("-")[0].upper()
                            skus.add(base_sku)
                            skus.add(sku.upper())
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
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
        existing = self.get_existing_skus()
        # Onitsuka Tiger 每個色號是獨立商品，只查完整 SKU
        return sku.upper() in existing

    def batch_rename_titles(self, old_prefix: str, new_prefix: str) -> dict:
        """
        批次修改商品標題前綴
        例: "Onitsuka Tiger｜" → "Onitsuka Tiger 鬼塚虎｜"
        """
        updated = 0
        skipped = 0
        errors = 0
        url = f"{self.base_url}/products.json?limit=250&fields=id,title"
        while url:
            try:
                resp = requests.get(url, headers=self.headers, timeout=30)
                if resp.status_code != 200:
                    logger.error(f"取得商品失敗: {resp.status_code}")
                    break
                products = resp.json().get("products", [])
                for p in products:
                    if old_prefix in p["title"] and new_prefix not in p["title"]:
                        new_title = p["title"].replace(old_prefix, new_prefix, 1)
                        put_resp = _api_request_with_retry(
                            "PUT",
                            f"{self.base_url}/products/{p['id']}.json",
                            headers=self.headers,
                            json={"product": {"id": p["id"], "title": new_title}},
                            timeout=15,
                        )
                        if put_resp.status_code == 200:
                            updated += 1
                            if updated % 20 == 0:
                                logger.info(f"  已更新 {updated} 個商品標題...")
                        else:
                            errors += 1
                            logger.warning(f"  更新失敗 {p['id']}: {put_resp.status_code}")
                        time.sleep(0.3)
                    else:
                        skipped += 1
                # 分頁
                link_header = resp.headers.get("Link", "")
                if 'rel="next"' in link_header:
                    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                    url = match.group(1) if match else None
                else:
                    url = None
            except Exception as e:
                logger.error(f"批次更新異常: {e}")
                break
        logger.info(f"✅ 標題更新完成: {updated} 個更新, {skipped} 個跳過, {errors} 個失敗")
        return {"updated": updated, "skipped": skipped, "errors": errors}

    # --- Collection ---
    def get_or_create_collection(self, title: str) -> int | None:
        if title in self._collection_cache:
            return self._collection_cache[title]
        try:
            resp = requests.get(
                f"{self.base_url}/custom_collections.json?title={title}",
                headers=self.headers, timeout=30,
            )
            if resp.status_code == 200:
                for c in resp.json().get("custom_collections", []):
                    if c["title"] == title:
                        self._collection_cache[title] = c["id"]
                        self.publish_to_all_channels("Collection", c["id"])
                        return c["id"]
        except Exception:
            pass
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
                self.publish_to_all_channels("Collection", cid)
                return cid
        except Exception as e:
            logger.error(f"建立 Collection 失敗: {e}")
        return None

    def _add_to_collection(self, product_id: int, collection_id: int):
        try:
            requests.post(
                f"{self.base_url}/collects.json",
                headers=self.headers,
                json={"collect": {"product_id": product_id, "collection_id": collection_id}},
                timeout=30,
            )
        except Exception:
            pass

    # --- 上架商品 ---
    def upload_product(self, product: dict, translate: bool = True) -> dict:
        """上架單個商品到 Shopify"""
        title = product["title"]
        sku = product["sku"]
        desc_html = product.get("description_html", "")
        short_desc = product.get("short_description_html", "")

        # 翻譯描述
        if translate and OPENAI_API_KEY:
            if desc_html:
                desc_html = translate_ja_to_zhtw(desc_html)
            if short_desc:
                short_desc = translate_ja_to_zhtw(short_desc)

        # 組合 Shopify 標題
        full_title = f"Onitsuka Tiger 鬼塚虎｜{title}"

        # 組合描述 HTML
        body_parts = []
        if short_desc:
            body_parts.append(short_desc)
        if desc_html:
            body_parts.append(desc_html)

        # 合併描述文字，統一換行處理
        raw_desc = "\n".join(body_parts)
        # 先去掉多餘的 HTML 標籤（Magento 來的可能包 <p> 等）
        raw_desc = re.sub(r'</?p[^>]*>', '\n', raw_desc)
        raw_desc = re.sub(r'<br\s*/?>', '\n', raw_desc)
        # 清理連續空行
        raw_desc = re.sub(r'\n{3,}', '\n\n', raw_desc).strip()
        # 每行轉成 <p> 或 <br>，讓 Shopify 正確換行顯示
        lines = [line.strip() for line in raw_desc.split('\n') if line.strip()]
        body_html = '<br>\n'.join(lines)

        # 商品資訊表
        info_rows = []
        if product.get("color_code"):
            info_rows.append(f'<tr><td><strong>色碼</strong></td><td>{product["color_code"]}</td></tr>')
        info_rows.append(f'<tr><td><strong>型號</strong></td><td>{sku}</td></tr>')
        info_rows.append(f'<tr><td><strong>品番</strong></td><td>{product["item_code"]}</td></tr>')
        if info_rows:
            body_html += "\n<br><br>\n<table>" + "".join(info_rows) + "</table>"

        # 圖片
        images = []
        for img_url in product.get("images", [])[:20]:
            images.append({"src": img_url})
        if not images and product.get("image"):
            images.append({"src": product["image"]})

        # 建立尺碼 variants
        sizes = product.get("sizes", [])
        if sizes:
            variants = []
            size_stock = {}
            for s in sizes:
                size_name = s["size"]
                variant_sku = f"{sku}-{size_name.replace('.', '').replace(' ', '')}"
                variants.append({
                    "option1": size_name,
                    "price": str(product["selling_price"]),
                    "compare_at_price": None,
                    "sku": variant_sku,
                    "inventory_management": "shopify",
                    "requires_shipping": True,
                })
                size_stock[size_name] = 2 if s.get("available", True) else 0
            options = [{"name": "尺碼", "values": [s["size"] for s in sizes]}]
        else:
            variants = [{
                "price": str(product["selling_price"]),
                "compare_at_price": None,
                "sku": sku,
                "inventory_management": "shopify",
                "requires_shipping": True,
            }]
            options = []
            size_stock = {"__default__": 2}

        # SEO
        seo = self._generate_seo(title, short_desc, sku)

        # Tags — 用實際性別而非爬取分類
        gender = product.get("gender", "unisex")
        tags = ["Onitsuka Tiger", "鬼塚虎", sku, product.get("item_code", "")]
        if gender == "men":
            tags.append("男裝")
        elif gender == "women":
            tags.append("女裝")
        elif gender == "unisex":
            tags.extend(["男裝", "女裝", "UNISEX"])
        elif gender == "kids":
            tags.append("童裝")

        # Shopify payload
        payload = {
            "product": {
                "title": full_title,
                "body_html": body_html,
                "vendor": "Onitsuka Tiger",
                "product_type": "服飾",
                "tags": tags,
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
                "POST", f"{self.base_url}/products.json",
                headers=self.headers, json=payload, timeout=60,
            )
            if resp.status_code == 201:
                shopify_product = resp.json()["product"]
                product_id = shopify_product["id"]

                # 設定庫存
                self._set_inventory_levels(shopify_product, size_stock)
                # 設定原始連結 metafield
                self._set_product_metafield(product_id, product.get("url", ""))
                # 加入所有相關 Collections（根據性別）
                for col_name in product.get("collection_names", []):
                    col_id = self.get_or_create_collection(col_name)
                    if col_id:
                        self._add_to_collection(product_id, col_id)
                        logger.info(f"  📂 加入 Collection: {col_name}")
                # 發布
                self.publish_to_all_channels("Product", product_id)

                gender_label = {"men": "男", "women": "女", "unisex": "男+女", "kids": "童"}
                logger.info(
                    f"✅ 上架成功: {sku} - {title} → ¥{product['selling_price']} "
                    f"[{gender_label.get(gender, '?')}]"
                )
                self._existing_skus.add(sku.upper())
                return {"success": True, "product_id": product_id}
            else:
                logger.error(f"❌ 上架失敗: {sku} - {resp.status_code} {resp.text[:200]}")
                return {"success": False, "error": resp.text[:200]}
        except DailyLimitReached:
            # 向上拋出，讓 app.py 處理（暫停等待）
            raise
        except Exception as e:
            logger.error(f"❌ 上架異常: {sku} - {e}")
            return {"success": False, "error": str(e)}

    def _set_product_metafield(self, product_id: int, url: str):
        if not url:
            return
        try:
            _api_request_with_retry(
                "POST", f"{self.base_url}/products/{product_id}/metafields.json",
                headers=self.headers,
                json={"metafield": {"namespace": "custom", "key": "link", "value": url, "type": "url"}},
                timeout=30,
            )
        except Exception:
            pass

    def _set_inventory_levels(self, shopify_product: dict, size_stock: dict):
        try:
            first_variant = shopify_product.get("variants", [{}])[0]
            first_inv_id = first_variant.get("inventory_item_id")
            if not first_inv_id:
                return
            inv_resp = _api_request_with_retry(
                "GET", f"{self.base_url}/inventory_levels.json?inventory_item_ids={first_inv_id}",
                headers=self.headers, timeout=30,
            )
            inv_levels = inv_resp.json().get("inventory_levels", [])
            if inv_levels:
                location_id = inv_levels[0]["location_id"]
            else:
                loc_resp = _api_request_with_retry(
                    "GET", f"{self.base_url}/locations.json",
                    headers=self.headers, timeout=30,
                )
                locations = loc_resp.json().get("locations", [])
                if not locations:
                    return
                location_id = locations[0]["id"]

            has_default = "__default__" in size_stock
            in_stock = out_stock = 0
            for variant in shopify_product.get("variants", []):
                size_name = variant.get("option1", "")
                qty = size_stock["__default__"] if has_default else size_stock.get(size_name, 0)
                inv_item_id = variant.get("inventory_item_id")
                if not inv_item_id:
                    continue
                resp = _api_request_with_retry(
                    "POST", f"{self.base_url}/inventory_levels/set.json",
                    headers=self.headers,
                    json={"location_id": location_id, "inventory_item_id": inv_item_id, "available": qty},
                    timeout=30,
                )
                if resp.status_code == 200:
                    if qty > 0:
                        in_stock += 1
                    else:
                        out_stock += 1
            logger.info(f"  📦 庫存: {in_stock} 有貨, {out_stock} 缺貨")
        except Exception as e:
            logger.warning(f"  ⚠️ 庫存設定失敗: {e}")

    @staticmethod
    def _generate_seo(title: str, desc: str, sku: str) -> dict:
        if not OPENAI_API_KEY:
            return {}
        prompt_text = f"""商品名稱: {title}
商品描述: {desc[:200] if desc else ''}
型號: {sku}
品牌: Onitsuka Tiger (鬼塚虎)
商店: GOYOUTATI 日本代購"""

        for attempt in range(3):
            try:
                resp = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": (
                                "你是 SEO 專家。根據商品資訊生成搜尋引擎優化的頁面標題和 Meta 描述。"
                                "規則："
                                "1. 頁面標題(title)：最多 60 字元，包含品牌名、商品名、關鍵字。格式範例：Onitsuka Tiger MEXICO 66 經典鞋款｜GOYOUTATI 日本代購"
                                "2. Meta 描述(description)：最多 155 字元，自然流暢的繁體中文。"
                                "3. 不要出現日文。4. 只回傳 JSON：{\"title\": \"...\", \"description\": \"...\"}"
                            )},
                            {"role": "user", "content": prompt_text},
                        ],
                        "temperature": 0, "max_tokens": 300,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"].strip()
                    content = content.replace("```json", "").replace("```", "").strip()
                    return json.loads(content)
                elif resp.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
            except Exception:
                pass
        return {}
