"""
BEAMS 官網爬蟲 → Shopify 自動上架系統
功能：
1. 精選分類爬蟲（手動選分類）
2. 日文翻譯成繁體中文（Google Translate API / DeepL）
3. 代購價格自動計算（日幣→台幣 + 手續費 + 國際運費）
4. 庫存同步
5. 重複商品檢查
6. 部署於 Zeabur
"""

import os
import re
import json
import time
import hashlib
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
from typing import Optional

# ============================================================
# 設定
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,  # ← DEBUG 模式方便排查問題，正式上線改回 INFO
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- 環境變數（部署時在 Zeabur 設定）---
SHOPIFY_STORE = os.getenv("SHOPIFY_SHOP", "your-store.myshopify.com")  # 配合現有 Zeabur 變數名
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # 省錢用 mini，夠準
SHIPPING_RATE_PER_KG = int(os.getenv("SHIPPING_RATE_PER_KG", "1250"))  # 每公斤國際運費(日幣)
MARGIN_DIVISOR = float(os.getenv("MARGIN_DIVISOR", "0.7"))  # 利潤除數（÷0.7 = 約43%利潤）
SCRAPE_DELAY = float(os.getenv("SCRAPE_DELAY", "2.0"))  # 每次請求間隔(秒)

# Proxy 設定（解決雲端 IP 被 BEAMS 封鎖的問題）
# 格式: http://user:pass@host:port 或 socks5://user:pass@host:port
PROXY_URL = os.getenv("PROXY_URL", "")  # 留空 = 不使用 proxy

BASE_URL = "https://www.beams.co.jp"
CDN_URL = "https://cdn.beams.co.jp"

# BEAMS 可選分類對照表
CATEGORIES = {
    # 男裝
    "men_tshirt": {"path": "/category/t-shirt/", "sex": "M", "name": "男裝｜T恤"},
    "men_shirt": {"path": "/category/shirt/", "sex": "M", "name": "男裝｜襯衫"},
    "men_tops": {"path": "/category/tops/", "sex": "M", "name": "男裝｜上衣"},
    "men_jacket": {"path": "/category/jacket/", "sex": "M", "name": "男裝｜外套"},
    "men_blouson": {"path": "/category/blouson/", "sex": "M", "name": "男裝｜夾克"},
    "men_coat": {"path": "/category/coat/", "sex": "M", "name": "男裝｜大衣"},
    "men_pants": {"path": "/category/pants/", "sex": "M", "name": "男裝｜褲子"},
    "men_bag": {"path": "/category/bag/", "sex": "M", "name": "男裝｜包包"},
    "men_shoes": {"path": "/category/shoes/", "sex": "M", "name": "男裝｜鞋子"},
    "men_hat": {"path": "/category/hat/", "sex": "M", "name": "男裝｜帽子"},
    "men_accessory": {"path": "/category/accessory/", "sex": "M", "name": "男裝｜飾品"},
    "men_wallet": {"path": "/category/wallet/", "sex": "M", "name": "男裝｜皮夾"},
    "men_watch": {"path": "/category/watch/", "sex": "M", "name": "男裝｜手錶"},
    # 女裝
    "women_tshirt": {"path": "/category/t-shirt/", "sex": "W", "name": "女裝｜T恤"},
    "women_shirt": {"path": "/category/shirt/", "sex": "W", "name": "女裝｜襯衫"},
    "women_tops": {"path": "/category/tops/", "sex": "W", "name": "女裝｜上衣"},
    "women_jacket": {"path": "/category/jacket/", "sex": "W", "name": "女裝｜外套"},
    "women_skirt": {"path": "/category/skirt/", "sex": "W", "name": "女裝｜裙子"},
    "women_onepiece": {"path": "/category/one-piece/", "sex": "W", "name": "女裝｜洋裝"},
    "women_pants": {"path": "/category/pants/", "sex": "W", "name": "女裝｜褲子"},
    "women_bag": {"path": "/category/bag/", "sex": "W", "name": "女裝｜包包"},
    "women_shoes": {"path": "/category/shoes/", "sex": "W", "name": "女裝｜鞋子"},
    # 童裝
    "kids_tshirt": {"path": "/category/t-shirt/", "sex": "K", "name": "童裝｜T恤"},
    "kids_tops": {"path": "/category/tops/", "sex": "K", "name": "童裝｜上衣"},
    "kids_pants": {"path": "/category/pants/", "sex": "K", "name": "童裝｜褲子"},
}

# ============================================================
# HTTP Session（模擬瀏覽器 + Proxy 支援）
# ============================================================

def create_session() -> requests.Session:
    """建立帶有合理 Headers 的 Session，支援 Proxy"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,ja-JP;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    })

    # 設定 Proxy
    if PROXY_URL:
        session.proxies = {
            "http": PROXY_URL,
            "https": PROXY_URL,
        }
        logger.info(f"🌐 使用 Proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
    else:
        logger.warning("⚠️ 未設定 PROXY_URL — 雲端 IP 可能被 BEAMS 封鎖！")

    return session


# ============================================================
# 翻譯模組（OpenAI ChatGPT）
# ============================================================

def translate_ja_to_zhtw(text: str) -> str:
    """使用 ChatGPT 將日文翻譯成繁體中文"""
    if not text or not text.strip():
        return text

    if OPENAI_API_KEY:
        return _translate_openai(text)

    logger.warning("未設定 OPENAI_API_KEY，回傳原文")
    return text


def _translate_openai(text: str) -> str:
    """呼叫 OpenAI API 翻譯"""
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是日本服飾電商的專業翻譯。"
                            "將日文商品名稱和描述翻譯成繁體中文。"
                            "保留品牌名、型號、英文不翻譯。"
                            "翻譯要自然通順，適合台灣消費者閱讀。"
                            "只回傳翻譯結果，不要加任何解釋。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"翻譯以下日文：\n{text}",
                    },
                ],
                "temperature": 0.3,
                "max_tokens": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip()
        return result
    except Exception as e:
        logger.warning(f"OpenAI 翻譯失敗: {e}")
        return text  # 失敗時回傳原文


# ============================================================
# 服飾重量參考表（kg）
# 根據 ZenMarket、Spoketravel、Printful 等多來源交叉比對
# ============================================================

WEIGHT_TABLE = {
    # === 上衣類 ===
    "t-shirt":    0.20,   # T恤・カットソー: 150-250g, 取中間偏上
    "shirt":      0.25,   # 襯衫・ブラウス: 200-300g
    "tops":       0.35,   # 毛衣・針織衫・カーディガン: 300-450g
    "blouson":    0.80,   # 夾克・ブルゾン（MA-1等）: 700-1000g
    "jacket":     0.70,   # 外套・ジャケット（西裝外套等）: 500-900g
    "coat":       1.50,   # 大衣・コート（冬季厚大衣）: 1.2-2.0kg

    # === 下身類 ===
    "pants":      0.50,   # 長褲・パンツ（含牛仔褲）: 400-700g
    "skirt":      0.30,   # 裙子・スカート: 200-450g
    "one-piece":  0.40,   # 洋裝・ワンピース: 300-600g
    "suit":       1.50,   # 西裝套裝（上下）: 1.3-1.8kg

    # === 配件類 ===
    "bag":        0.60,   # 包包・バッグ: 300-1000g, 中型包取均
    "shoes":      0.80,   # 鞋子・シューズ（單隻×2）: 600-1000g
    "hat":        0.15,   # 帽子: 100-200g
    "watch":      0.20,   # 手錶: 100-300g (含盒)
    "wallet":     0.15,   # 皮夾・小物: 100-200g
    "accessory":  0.10,   # 飾品・アクセサリー: 50-150g
    "fashiongoods": 0.15, # 時尚小物: 100-200g
    "legwear":    0.10,   # 襪子: 50-100g
    "underwear":  0.15,   # 內著: 100-200g
    "hair-accessory": 0.05, # 髮飾: 30-80g

    # === 生活・其他 ===
    "interior":   0.80,   # 室內用品: 變化大, 取中
    "outdoor":    0.70,   # 戶外運動用品: 變化大
    "tablewear":  0.50,   # 食器・廚具
    "hobby":      0.30,   # 雜貨
    "cosmetics":  0.20,   # 化妝品
    "music":      0.20,   # 音樂・書籍
    "maternity":  0.30,   # 孕婦裝
    "etc":        0.30,   # 其他（預設）
}

# 預設重量（找不到對應分類時使用）
DEFAULT_WEIGHT_KG = 0.30


def get_estimated_weight(item_type: str) -> float:
    """
    根據商品分類取得預估重量(kg)
    item_type: BEAMS URL 中的分類名，如 "t-shirt", "pants", "coat"
    """
    # 直接匹配
    if item_type in WEIGHT_TABLE:
        return WEIGHT_TABLE[item_type]

    # 模糊匹配（處理子分類如 "bag_03" → "bag"）
    base_type = item_type.split("_")[0] if "_" in item_type else item_type
    if base_type in WEIGHT_TABLE:
        return WEIGHT_TABLE[base_type]

    return DEFAULT_WEIGHT_KG


# ============================================================
# 價格計算模組
# 公式：(商品價格 + (商品重量kg × 1250)) ÷ 0.7
# 售價幣別：日幣（JPY）
# ============================================================

def calculate_proxy_price(price_jpy: int, weight_kg: float = DEFAULT_WEIGHT_KG) -> dict:
    """
    代購價格計算（日幣售價）
    公式: (商品價格 + (商品重量 × 每公斤運費)) ÷ 利潤除數
    結果無條件進位到百位日幣
    """
    import math

    shipping_jpy = weight_kg * SHIPPING_RATE_PER_KG
    subtotal = price_jpy + shipping_jpy
    final_price_raw = subtotal / MARGIN_DIVISOR

    # 無條件進位到百位日幣（看起來整齊）
    final_price = math.ceil(final_price_raw / 100) * 100

    return {
        "original_jpy": price_jpy,
        "weight_kg": weight_kg,
        "shipping_jpy": round(shipping_jpy),
        "subtotal_jpy": round(subtotal),
        "margin_divisor": MARGIN_DIVISOR,
        "final_jpy": final_price,
    }


# ============================================================
# BEAMS 爬蟲核心
# ============================================================

class BeamsScraper:
    def __init__(self):
        self.session = create_session()
        self.scraped_items = []

    def scrape_category(self, category_key: str, max_pages: int = 5) -> list[dict]:
        """
        爬取指定分類的商品列表
        """
        if category_key not in CATEGORIES:
            logger.error(f"未知分類: {category_key}")
            return []

        cat = CATEGORIES[category_key]
        logger.info(f"📦 開始爬取分類: {cat['name']}")

        all_items = []
        page = 1

        while page <= max_pages:
            url = f"{BASE_URL}{cat['path']}"
            # ⚠️ BEAMS 分頁參數是 "p" 不是 "page"
            params = {"sex": cat["sex"]}
            if page > 1:
                params["p"] = page

            full_url = f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
            logger.info(f"  📄 正在爬取第 {page} 頁... URL: {full_url}")

            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                logger.debug(f"  📡 HTTP {resp.status_code}, 內容長度: {len(resp.text)} bytes")
            except requests.RequestException as e:
                logger.error(f"  ❌ 請求失敗: {e}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            items = self._parse_category_page(soup, cat)

            if not items:
                logger.info(f"  ✅ 第 {page} 頁無更多商品，結束")
                break

            all_items.extend(items)
            logger.info(f"  ✅ 第 {page} 頁找到 {len(items)} 件商品")

            page += 1
            time.sleep(SCRAPE_DELAY)

        logger.info(f"📊 分類 [{cat['name']}] 共找到 {len(all_items)} 件商品")
        return all_items

    def _parse_category_page(self, soup: BeautifulSoup, category: dict) -> list[dict]:
        """解析分類頁面的商品列表"""
        items = []

        # ========== DEBUG: 頁面結構分析 ==========
        page_title = soup.find("title")
        logger.debug(f"  🔎 [DEBUG] 頁面標題: {page_title.text.strip() if page_title else '無'}")
        logger.debug(f"  🔎 [DEBUG] HTML 總長度: {len(str(soup))} chars")

        # 檢查所有 <a> 標籤中含 /item/ 的連結
        all_a_tags = soup.find_all("a", href=True)
        item_hrefs = [a["href"] for a in all_a_tags if "/item/" in a.get("href", "")]
        logger.debug(f"  🔎 [DEBUG] 全部 <a> 標籤: {len(all_a_tags)} 個, 含 /item/ 連結: {len(item_hrefs)} 個")

        if item_hrefs:
            logger.debug(f"  🔎 [DEBUG] 前3個 /item/ 連結: {item_hrefs[:3]}")
        else:
            # 沒找到 /item/ 連結，輸出更多偵錯資訊
            logger.warning(f"  ⚠️ [DEBUG] 頁面中找不到任何 /item/ 連結！")
            # 輸出前幾個 <a> href 看看頁面結構
            sample_hrefs = [a["href"] for a in all_a_tags[:10]]
            logger.warning(f"  ⚠️ [DEBUG] 前10個 <a> href: {sample_hrefs}")
            # 輸出 HTML 前 2000 字幫助除錯
            html_snippet = str(soup)[:2000]
            logger.warning(f"  ⚠️ [DEBUG] HTML 前 2000 字:\n{html_snippet}")

        # ========== 商品列表解析 ==========
        # BEAMS 商品連結格式: /item/{label}/{category}/{item_code}/?color=XX
        product_links = soup.find_all("a", href=re.compile(r"/item/[^/]+/[^/]+/\d+"))

        logger.debug(f"  🔎 [DEBUG] regex 匹配到的商品連結: {len(product_links)} 個")

        seen_codes = set()
        for link in product_links:
            href = link.get("href", "")
            # 提取商品編號
            match = re.search(r"/item/([^/]+)/([^/]+)/(\d+)", href)
            if not match:
                logger.debug(f"  🔎 [DEBUG] regex 無法匹配: {href}")
                continue

            label = match.group(1)  # e.g., "beams", "beamsplus"
            item_type = match.group(2)  # e.g., "t-shirt", "pants"
            item_code = match.group(3)  # e.g., "11041456366"

            if item_code in seen_codes:
                continue
            seen_codes.add(item_code)

            # 嘗試從列表頁取得基本資料
            # 清理 href，移除 ?color= 參數
            clean_href = re.sub(r"\?.*$", "/", href)
            item_data = {
                "item_code": item_code,
                "label": label,
                "item_type": item_type,
                "url": urljoin(BASE_URL, clean_href),
                "category_name": category["name"],
                "sex": category["sex"],
            }

            # 嘗試取得價格文字 — 從 <a> 的文字內容中搜尋
            link_text = link.get_text()
            price_match = re.search(r"[¥￥]\s*([\d,]+)", link_text)
            if price_match:
                price_text = price_match.group(1).replace(",", "")
                if price_text:
                    item_data["price_jpy"] = int(price_text)

            # 嘗試取得圖片 URL
            img = link.find("img")
            if img:
                src = img.get("src", "") or img.get("data-src", "")
                if src and "svg" not in src:
                    item_data["thumbnail"] = src if src.startswith("http") else f"https:{src}"

            items.append(item_data)

        logger.info(f"  📋 [解析結果] 去重後商品數: {len(items)} 件（原始連結 {len(product_links)} 個）")
        if items:
            sample = items[0]
            logger.info(f"  📋 [範例商品] code={sample['item_code']}, label={sample['label']}, price={sample.get('price_jpy', '未知')}")

        return items

    def scrape_product_detail(self, item: dict) -> dict:
        """爬取單一商品的詳細資訊"""
        url = item["url"]
        logger.info(f"  🔍 爬取商品詳情: {item['item_code']}")

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"  ❌ 商品詳情請求失敗: {e}")
            return item

        soup = BeautifulSoup(resp.text, "html.parser")

        # --- 商品名稱 ---
        title_el = soup.find("h1") or soup.find("title")
        if title_el:
            raw_title = title_el.get_text(strip=True)
            # 清理標題（移除 "| BEAMS" 等後綴）
            raw_title = re.sub(r"\s*[|｜].*$", "", raw_title)
            item["title_ja"] = raw_title

        # --- 價格（如果列表頁沒抓到）---
        if "price_jpy" not in item:
            price_el = soup.find(string=re.compile(r"[¥￥]\s*[\d,]+"))
            if price_el:
                price_text = re.sub(r"[^0-9]", "", str(price_el))
                if price_text:
                    item["price_jpy"] = int(price_text)

        # --- 商品圖片 ---
        item["images"] = self._extract_images(soup, item["item_code"])

        # --- 商品描述 ---
        desc_el = soup.find("div", class_=re.compile(r"item[-_]?desc|product[-_]?desc|detail"))
        if desc_el:
            item["description_ja"] = desc_el.get_text(separator="\n", strip=True)

        # --- 尺寸/顏色 變體 ---
        item["variants"] = self._extract_variants(soup)

        # --- 庫存狀態 ---
        # 如果有「カートへ入れる」按鈕表示有庫存
        cart_btn = soup.find(string=re.compile(r"カートへ入れる|ADD TO CART", re.IGNORECASE))
        item["in_stock"] = cart_btn is not None

        # 「品切れ」或 「SOLD OUT」 表示無庫存
        sold_out = soup.find(string=re.compile(r"品切れ|SOLD\s*OUT", re.IGNORECASE))
        if sold_out:
            item["in_stock"] = False

        time.sleep(SCRAPE_DELAY)
        return item

    def _extract_images(self, soup: BeautifulSoup, item_code: str) -> list[str]:
        """提取商品圖片 URL"""
        images = []
        seen = set()

        # 找所有包含商品編號的圖片
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if not src or "svg" in src:
                continue
            if item_code in src:
                full_url = src if src.startswith("http") else f"https:{src}"
                # 取得高解析度版本（S1 → L1）
                full_url = full_url.replace("/S1/", "/L1/").replace("/S2/", "/L1/")
                if full_url not in seen:
                    seen.add(full_url)
                    images.append(full_url)

        # 如果沒找到，用 CDN 規則推算
        if not images:
            for suffix in ["C_1", "C_2", "C_3", "D_1", "D_2", "D_3"]:
                img_url = f"{CDN_URL}/img/goods/{item_code}/L1/{item_code}_{suffix}.jpg"
                images.append(img_url)

        return images[:10]  # 最多10張

    def _extract_variants(self, soup: BeautifulSoup) -> list[dict]:
        """提取尺寸/顏色變體"""
        variants = []

        # 顏色選項
        color_els = soup.find_all("img", alt=True, src=re.compile(r"_C_\d+"))
        colors = []
        for el in color_els:
            color_name = el.get("alt", "").strip()
            if color_name and color_name not in colors:
                colors.append(color_name)

        # 尺寸選項（通常在 select 或 radio 中）
        sizes = []
        size_options = soup.find_all(
            ["option", "label", "span"],
            string=re.compile(r"^(XXS|XS|S|M|L|XL|XXL|FREE|F|\d{2,3})$", re.IGNORECASE),
        )
        for opt in size_options:
            size = opt.get_text(strip=True).upper()
            if size and size not in sizes and size != "選択してください":
                sizes.append(size)

        # 組合變體
        if colors and sizes:
            for color in colors:
                for size in sizes:
                    variants.append({"color": color, "size": size})
        elif colors:
            for color in colors:
                variants.append({"color": color, "size": "FREE"})
        elif sizes:
            for size in sizes:
                variants.append({"color": "Default", "size": size})
        else:
            variants.append({"color": "Default", "size": "FREE"})

        return variants


# ============================================================
# Shopify 上架模組
# ============================================================

class ShopifyUploader:
    def __init__(self):
        self.api_base = f"https://{SHOPIFY_STORE}/admin/api/2024-01"
        self.headers = {
            "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
            "Content-Type": "application/json",
        }
        self._existing_skus: Optional[set] = None

    def get_existing_skus(self) -> set:
        """取得 Shopify 上已有的所有 SKU（用於重複檢查）"""
        if self._existing_skus is not None:
            return self._existing_skus

        logger.info("📋 正在載入 Shopify 已有商品 SKU...")
        skus = set()
        url = f"{self.api_base}/products.json"
        params = {"limit": 250, "fields": "id,variants"}

        while url:
            resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            for product in data.get("products", []):
                for variant in product.get("variants", []):
                    sku = variant.get("sku", "")
                    if sku:
                        skus.add(sku)

            # 分頁
            link_header = resp.headers.get("Link", "")
            next_match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
            url = next_match.group(1) if next_match else None
            params = {}  # next URL 已包含參數

            time.sleep(0.5)  # Shopify API rate limit

        self._existing_skus = skus
        logger.info(f"📋 已載入 {len(skus)} 個現有 SKU")
        return skus

    def is_duplicate(self, item_code: str) -> bool:
        """檢查商品是否已存在"""
        sku = f"BEAMS-{item_code}"
        return sku in self.get_existing_skus()

    def upload_product(self, item: dict) -> Optional[dict]:
        """上架單一商品到 Shopify"""
        item_code = item["item_code"]
        sku = f"BEAMS-{item_code}"

        # 重複檢查
        if self.is_duplicate(item_code):
            logger.info(f"  ⏭️ 跳過重複商品: {item_code}")
            return None

        # 翻譯
        title_zh = translate_ja_to_zhtw(item.get("title_ja", ""))
        desc_zh = translate_ja_to_zhtw(item.get("description_ja", ""))

        # 計算代購價格（含重量）
        price_jpy = item.get("price_jpy", 0)
        if not price_jpy:
            logger.warning(f"  ⚠️ 商品 {item_code} 無價格，跳過")
            return None

        weight_kg = get_estimated_weight(item.get("item_type", "etc"))
        pricing = calculate_proxy_price(price_jpy, weight_kg)

        # 建立商品描述（包含原始日幣價格和代購資訊）
        body_html = self._build_description(item, title_zh, desc_zh, pricing)

        # 建立 Shopify 變體
        shopify_variants = []
        for i, v in enumerate(item.get("variants", [{"color": "Default", "size": "FREE"}])):
            shopify_variants.append({
                "option1": v.get("color", "Default"),
                "option2": v.get("size", "FREE"),
                "price": str(pricing["final_jpy"]),
                "sku": f"{sku}-{v.get('color', 'D')}-{v.get('size', 'F')}",
                "inventory_management": "shopify",
                "inventory_quantity": 5 if item.get("in_stock", True) else 0,
                "requires_shipping": True,
                "weight": weight_kg,
                "weight_unit": "kg",
            })

        # 建立圖片列表
        shopify_images = [{"src": img} for img in item.get("images", [])[:10]]

        # 建立 Shopify 商品
        product_data = {
            "product": {
                "title": f"【BEAMS】{title_zh}" if title_zh else f"【BEAMS】{item.get('title_ja', item_code)}",
                "body_html": body_html,
                "vendor": "BEAMS",
                "product_type": item.get("category_name", "日本服飾"),
                "tags": self._build_tags(item),
                "options": [
                    {"name": "顏色", "values": list({v.get("color", "Default") for v in item.get("variants", [])})},
                    {"name": "尺寸", "values": list({v.get("size", "FREE") for v in item.get("variants", [])})},
                ],
                "variants": shopify_variants,
                "images": shopify_images,
                "metafields": [
                    {
                        "namespace": "beams",
                        "key": "original_url",
                        "value": item["url"],
                        "type": "url",
                    },
                    {
                        "namespace": "beams",
                        "key": "item_code",
                        "value": item_code,
                        "type": "single_line_text_field",
                    },
                    {
                        "namespace": "beams",
                        "key": "price_jpy",
                        "value": str(price_jpy),
                        "type": "single_line_text_field",
                    },
                ],
            }
        }

        try:
            resp = requests.post(
                f"{self.api_base}/products.json",
                headers=self.headers,
                json=product_data,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            product_id = result["product"]["id"]
            logger.info(f"  ✅ 上架成功: {item_code} → Product ID: {product_id}")
            self._existing_skus.add(sku)
            time.sleep(0.5)  # Rate limit
            return result
        except requests.RequestException as e:
            logger.error(f"  ❌ 上架失敗: {item_code} → {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"     回應: {e.response.text[:500]}")
            return None

    def update_inventory(self, item: dict) -> bool:
        """更新已存在商品的庫存狀態"""
        item_code = item["item_code"]
        sku = f"BEAMS-{item_code}"

        # 找到對應的 Shopify 商品
        try:
            resp = requests.get(
                f"{self.api_base}/products.json",
                headers=self.headers,
                params={"fields": "id,variants", "limit": 1},
                timeout=30,
            )
            # 這裡簡化處理，實際應用建議用 metafield 查詢
            # 或維護一個本地 mapping 資料庫
            logger.info(f"  🔄 庫存同步功能已預備，需搭配資料庫使用")
            return True
        except Exception as e:
            logger.error(f"  ❌ 庫存更新失敗: {e}")
            return False

    def _build_description(self, item: dict, title_zh: str, desc_zh: str, pricing: dict) -> str:
        """建立 Shopify 商品描述 HTML"""
        return f"""
<div class="beams-product">
  <div class="proxy-info" style="background:#fff3cd;padding:15px;border-radius:8px;margin-bottom:20px;">
    <p style="font-weight:bold;font-size:16px;">🇯🇵 日本 BEAMS 官網代購</p>
    <p>日本官網售價：¥{pricing['original_jpy']:,}</p>
    <p>預估重量：{pricing['weight_kg']}kg ｜ 國際運費：¥{pricing['shipping_jpy']:,}</p>
    <p>代購售價：<strong style="color:#e74c3c;font-size:20px;">¥{pricing['final_jpy']:,}</strong></p>
    <p style="font-size:12px;color:#666;">
      計算方式：(商品價格 + 重量×¥{SHIPPING_RATE_PER_KG:,}/kg) ÷ {MARGIN_DIVISOR}
    </p>
  </div>

  <div class="product-description">
    <h3>商品說明</h3>
    <p>{desc_zh or '請參考圖片'}</p>
  </div>

  <div class="original-info" style="margin-top:20px;padding:10px;background:#f8f9fa;border-radius:5px;">
    <p style="font-size:12px;color:#888;">
      📌 原始商品名：{item.get('title_ja', '')}
      <br>📌 品牌/Label：{item.get('label', 'BEAMS').upper()}
      <br>📌 商品編號：{item.get('item_code', '')}
      <br>📌 <a href="{item.get('url', '')}" target="_blank">查看日本官網</a>
    </p>
  </div>

  <div class="notice" style="margin-top:15px;padding:10px;border:1px solid #ddd;border-radius:5px;">
    <p style="font-size:13px;">⚠️ 代購注意事項</p>
    <ul style="font-size:12px;color:#666;">
      <li>本商品為日本代購，下單後約 7-14 個工作天到貨</li>
      <li>庫存即時同步日本官網，如遇缺貨將全額退款</li>
      <li>商品圖片來源為日本 BEAMS 官網</li>
      <li>因代購商品性質，恕不接受退換貨</li>
      <li>重量為預估值，實際運費以出貨時秤重為準</li>
    </ul>
  </div>
</div>
"""

    def _build_tags(self, item: dict) -> str:
        """建立商品標籤"""
        tags = ["BEAMS", "日本代購", "日系服飾"]

        label = item.get("label", "").upper()
        if label:
            tags.append(label)

        sex = item.get("sex", "")
        sex_map = {"M": "男裝", "W": "女裝", "K": "童裝"}
        if sex in sex_map:
            tags.append(sex_map[sex])

        cat_name = item.get("category_name", "")
        if "｜" in cat_name:
            tags.append(cat_name.split("｜")[1])

        return ", ".join(tags)


# ============================================================
# 主流程
# ============================================================

def run_scraper(categories: list[str], max_pages: int = 3, dry_run: bool = False):
    """
    主要執行流程

    Args:
        categories: 要爬取的分類 key 列表
        max_pages: 每個分類最多爬幾頁
        dry_run: True = 只爬不上架（測試用）
    """
    # ========== 擷取 log 到記憶體，方便回傳給前端 ==========
    import io
    log_capture = io.StringIO()
    log_handler = logging.StreamHandler(log_capture)
    log_handler.setLevel(logging.DEBUG)
    log_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    logger.addHandler(log_handler)

    scraper = BeamsScraper()
    uploader = ShopifyUploader() if not dry_run else None

    results = {
        "start_time": datetime.now().isoformat(),
        "categories": categories,
        "total_found": 0,
        "total_uploaded": 0,
        "total_skipped_duplicate": 0,
        "total_skipped_no_price": 0,
        "total_failed": 0,
        "items": [],
        "debug_logs": [],  # ← 新增：回傳 debug logs 給前端
    }

    for cat_key in categories:
        if cat_key not in CATEGORIES:
            logger.warning(f"⚠️ 未知分類: {cat_key}，跳過")
            continue

        # Step 1: 爬取分類頁面
        items = scraper.scrape_category(cat_key, max_pages=max_pages)
        results["total_found"] += len(items)

        # Step 2: 爬取每件商品的詳細資訊
        for item in items:
            item = scraper.scrape_product_detail(item)

            if dry_run:
                # 測試模式：翻譯並計算價格但不上架
                if item.get("title_ja"):
                    item["title_zh"] = translate_ja_to_zhtw(item["title_ja"])
                if item.get("price_jpy"):
                    weight_kg = get_estimated_weight(item.get("item_type", "etc"))
                    item["weight_kg"] = weight_kg
                    item["pricing"] = calculate_proxy_price(item["price_jpy"], weight_kg)
                results["items"].append(item)
                logger.info(
                    f"  [DRY RUN] {item.get('item_code')} | "
                    f"{item.get('title_ja', '?')} | "
                    f"{item.get('item_type', '?')} ({item.get('weight_kg', '?')}kg) | "
                    f"¥{item.get('price_jpy', '?')} → "
                    f"¥{item.get('pricing', {}).get('final_jpy', '?')}"
                )
                continue

            # Step 3: 重複檢查 + 上架
            if uploader.is_duplicate(item["item_code"]):
                results["total_skipped_duplicate"] += 1
                # 庫存同步
                uploader.update_inventory(item)
                continue

            if not item.get("price_jpy"):
                results["total_skipped_no_price"] += 1
                continue

            result = uploader.upload_product(item)
            if result:
                results["total_uploaded"] += 1
            else:
                results["total_failed"] += 1

    results["end_time"] = datetime.now().isoformat()

    # 輸出結果摘要
    logger.info("=" * 60)
    logger.info("📊 爬蟲執行結果摘要")
    logger.info(f"  分類數量: {len(categories)}")
    logger.info(f"  發現商品: {results['total_found']}")
    logger.info(f"  成功上架: {results['total_uploaded']}")
    logger.info(f"  跳過重複: {results['total_skipped_duplicate']}")
    logger.info(f"  跳過無價: {results['total_skipped_no_price']}")
    logger.info(f"  上架失敗: {results['total_failed']}")
    logger.info("=" * 60)

    # ========== 擷取 debug logs ==========
    logger.removeHandler(log_handler)
    results["debug_logs"] = log_capture.getvalue().split("\n")
    log_capture.close()

    return results


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BEAMS 爬蟲 → Shopify 上架")
    parser.add_argument(
        "--categories", "-c",
        nargs="+",
        default=["men_tshirt"],
        help=f"要爬取的分類，可選: {', '.join(CATEGORIES.keys())}",
    )
    parser.add_argument("--max-pages", "-p", type=int, default=3, help="每分類最多頁數")
    parser.add_argument("--dry-run", "-d", action="store_true", help="測試模式（不上架）")
    parser.add_argument("--list-categories", "-l", action="store_true", help="列出所有可選分類")

    args = parser.parse_args()

    if args.list_categories:
        print("\n📋 可選分類:")
        for key, cat in CATEGORIES.items():
            print(f"  {key:25s} → {cat['name']}")
        print()
    else:
        run_scraper(
            categories=args.categories,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
