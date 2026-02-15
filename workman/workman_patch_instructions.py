"""
WORKMAN app.py 修正 Patch
========================
修正缺貨偵測不完整的問題

問題：workman.jp 上「予約受付は終了いたしました」的商品
     沒有被偵測為缺貨，導致 Shopify 上仍可下單

修正 3 個位置（用 Ctrl+F 搜尋替換即可）：
"""

# ============================================
# 修正 1: OUT_OF_STOCK_KEYWORDS
# ============================================
# 搜尋這段：
OLD_1 = """
OUT_OF_STOCK_KEYWORDS = [
    '店舗のみのお取り扱い',
    'オンラインストア販売終了',
    '店舗在庫を確認する',
]
"""

# 替換成這段：
NEW_1 = """
OUT_OF_STOCK_KEYWORDS = [
    '店舗のみのお取り扱い',
    'オンラインストア販売終了',
    '店舗在庫を確認する',
    '予約受付は終了',
    '受付終了',
    '販売を終了',
    '取り扱いを終了',
]
"""

# ============================================
# 修正 2: parse_product_page 裡的缺貨檢查
# ============================================
# 搜尋這段：
OLD_2 = """
        if '売り切れ' in page_text or '品切れ' in page_text:
            print(f"[跳過] 缺貨（売り切れ/品切れ）: {url}")
            return None
        # === 缺貨檢查結束 ===
"""

# 替換成這段：
NEW_2 = """
        if '売り切れ' in page_text or '品切れ' in page_text:
            print(f"[跳過] 缺貨（売り切れ/品切れ）: {url}")
            return None
        if '予約受付は終了' in page_text or '受付終了' in page_text:
            print(f"[跳過] 預約已結束: {url}")
            return None
        # === 缺貨檢查結束 ===
"""

# ============================================
# 修正 3: check_workman_stock 裡的缺貨檢查
# ============================================
# 搜尋這段：
OLD_3 = """
        if '売り切れ' in page_text or '品切れ' in page_text:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '売り切れ / 品切れ'}
        return result
"""

# 替換成這段：
NEW_3 = """
        if '売り切れ' in page_text or '品切れ' in page_text:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '売り切れ / 品切れ'}
        if '予約受付は終了' in page_text or '受付終了' in page_text:
            return {'available': False, 'page_exists': True, 'out_of_stock_reason': '予約受付終了'}
        return result
"""
