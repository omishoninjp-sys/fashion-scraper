"""
Onitsuka Tiger Collection 搬移 + 自動歸類腳本
================================================
Phase 1: 搬移舊 Collection 商品到新 Collection
  ① onitsuka男裝 → onitsuka 男性
  ② onitsuka女裝 → onitsuka 女性
  ③ Onitsuka Tiger 男裝 → onitsuka 男性  (爬蟲舊名稱)
  ④ Onitsuka Tiger 女裝 → onitsuka 女性  (爬蟲舊名稱)
  搬完後刪除舊 Collection

Phase 2: 找出沒被分到任何 onitsuka Collection 的商品
  根據 tags（男裝/女裝/UNISEX）自動歸類到正確的 Collection

用法: python migrate_collections.py
"""

import os
import requests
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 沒裝 python-dotenv 就用系統環境變數

# ============================================================
# 設定
# ============================================================
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "goyoutati")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")

BASE_URL = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2024-10"
HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json",
}

# Phase 1: 搬移對應表（舊名稱 → 新名稱）
MIGRATION_MAP = {
    "onitsuka男裝": "Onitsuka Tiger 男性",
    "onitsuka女裝": "Onitsuka Tiger 女性",
    "Onitsuka Tiger 男裝": "Onitsuka Tiger 男性",
    "Onitsuka Tiger 女裝": "Onitsuka Tiger 女性",
}

# Phase 2: 性別 → 目標 Collection
GENDER_COLLECTION_MAP = {
    "men": ["Onitsuka Tiger 男性"],
    "women": ["Onitsuka Tiger 女性"],
    "unisex": ["Onitsuka Tiger 男性", "Onitsuka Tiger 女性"],
    "kids": [],  # 目前沒有 kids collection，跳過
}


# ============================================================
# API helpers
# ============================================================
def api_get(endpoint, params=None):
    resp = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"  ❌ GET {endpoint} 失敗: {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json()


def api_post(endpoint, data):
    resp = requests.post(f"{BASE_URL}/{endpoint}", headers=HEADERS, json=data, timeout=30)
    if resp.status_code not in (200, 201):
        print(f"  ❌ POST {endpoint} 失敗: {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json()


def api_delete(endpoint):
    resp = requests.delete(f"{BASE_URL}/{endpoint}", headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"  ❌ DELETE {endpoint} 失敗: {resp.status_code} {resp.text[:200]}")
        return False
    return True


# ============================================================
# Collection helpers
# ============================================================
def find_all_collections():
    """取得所有 Collections（custom + smart），回傳 dict: title → {id, type}"""
    all_cols = {}

    # Custom collections
    params = {"limit": 250}
    while True:
        data = api_get("custom_collections.json", params)
        if not data:
            break
        for c in data.get("custom_collections", []):
            all_cols[c["title"]] = {"id": c["id"], "type": "custom"}
        if len(data.get("custom_collections", [])) < 250:
            break
        params["since_id"] = data["custom_collections"][-1]["id"]

    # Smart collections
    params = {"limit": 250}
    while True:
        data = api_get("smart_collections.json", params)
        if not data:
            break
        for c in data.get("smart_collections", []):
            all_cols[c["title"]] = {"id": c["id"], "type": "smart"}
        if len(data.get("smart_collections", [])) < 250:
            break
        params["since_id"] = data["smart_collections"][-1]["id"]

    return all_cols


def get_collects_for_collection(collection_id):
    """取得 Collection 內所有 collect（product_id + collect_id）"""
    results = []
    params = {"collection_id": collection_id, "limit": 250}
    while True:
        data = api_get("collects.json", params)
        if not data:
            break
        collects = data.get("collects", [])
        for c in collects:
            results.append({"product_id": c["product_id"], "collect_id": c["id"]})
        if len(collects) < 250:
            break
        params["since_id"] = collects[-1]["id"]
    return results


def get_product_collection_ids(product_id):
    """取得某商品所在的所有 collection_id"""
    col_ids = set()
    params = {"product_id": product_id, "limit": 250}
    data = api_get("collects.json", params)
    if data:
        for c in data.get("collects", []):
            col_ids.add(c["collection_id"])
    return col_ids


def add_product_to_collection(product_id, collection_id):
    """把商品加入 Collection"""
    data = api_post("collects.json", {
        "collect": {
            "product_id": product_id,
            "collection_id": collection_id,
        }
    })
    return data is not None


def delete_collection(collection_id, collection_type):
    """刪除 Collection"""
    if collection_type == "custom":
        return api_delete(f"custom_collections/{collection_id}.json")
    else:
        return api_delete(f"smart_collections/{collection_id}.json")


# ============================================================
# 取得所有 Onitsuka Tiger 商品
# ============================================================
def get_all_onitsuka_products():
    """取得 Shopify 上所有 vendor='Onitsuka Tiger' 的商品"""
    all_products = []
    params = {"limit": 250, "vendor": "Onitsuka Tiger"}
    while True:
        data = api_get("products.json", params)
        if not data:
            break
        products = data.get("products", [])
        all_products.extend(products)
        if len(products) < 250:
            break
        params["since_id"] = products[-1]["id"]
    return all_products


def detect_gender_from_tags(product):
    """
    從商品 tags 判斷性別
    爬蟲存的 tags: 男裝/女裝/UNISEX/童裝
    """
    tags = product.get("tags", "")
    if isinstance(tags, list):
        tag_list = [t.strip().lower() for t in tags]
    else:
        tag_list = [t.strip().lower() for t in tags.split(",")]

    has_men = "男裝" in tag_list
    has_women = "女裝" in tag_list
    has_unisex = "unisex" in tag_list

    if has_unisex or (has_men and has_women):
        return "unisex"
    elif has_men:
        return "men"
    elif has_women:
        return "women"
    elif "童裝" in tag_list:
        return "kids"

    # Fallback: 從 handle 或標題猜
    title = product.get("title", "").lower()
    handle = product.get("handle", "").lower()
    text = f"{title} {handle}"

    if "men" in text and "women" not in text:
        return "men"
    elif "women" in text or "ladies" in text:
        return "women"

    # 無法判斷 → unisex（加到男+女）
    return "unisex"


# ============================================================
# Phase 1: 搬移舊 Collection
# ============================================================
def phase1_migrate(all_cols):
    print("\n" + "=" * 60)
    print("📦 Phase 1: 搬移舊 Collection → 新 Collection")
    print("=" * 60)

    old_cols_to_delete = []

    for old_title, new_title in MIGRATION_MAP.items():
        old_col = all_cols.get(old_title)
        if not old_col:
            continue  # 這個舊名稱不存在，跳過

        new_col = all_cols.get(new_title)
        if not new_col:
            print(f"\n  ⚠️  找不到目標「{new_title}」！請先手動建立")
            continue

        print(f"\n{'─' * 50}")
        print(f"  {old_title} → {new_title}")
        print(f"  舊: ID {old_col['id']} ({old_col['type']})")
        print(f"  新: ID {new_col['id']} ({new_col['type']})")

        collects = get_collects_for_collection(old_col["id"])
        print(f"  📊 {len(collects)} 個商品需要搬移")

        moved = 0
        failed = 0
        for item in collects:
            pid = item["product_id"]
            if add_product_to_collection(pid, new_col["id"]):
                moved += 1
            else:
                failed += 1
            if moved % 10 == 0 and moved > 0:
                print(f"    ... 已搬移 {moved}/{len(collects)}")
            time.sleep(0.3)

        print(f"  ✅ 搬移完成: 成功 {moved}, 失敗 {failed}")
        old_cols_to_delete.append((old_title, old_col))

    # 刪除舊 Collection
    for old_title, old_col in old_cols_to_delete:
        print(f"\n  🗑  刪除「{old_title}」(ID: {old_col['id']})...")
        if delete_collection(old_col["id"], old_col["type"]):
            print(f"  ✅ 已刪除")
        else:
            print(f"  ❌ 刪除失敗，請手動刪除")

    if not old_cols_to_delete:
        print("\n  ℹ️  沒有找到需要搬移的舊 Collection")


# ============================================================
# Phase 2: 歸類沒被分到的商品
# ============================================================
def phase2_assign_orphans(all_cols):
    print("\n" + "=" * 60)
    print("🔍 Phase 2: 歸類沒有 Collection 的 Onitsuka 商品")
    print("=" * 60)

    men_col = all_cols.get("Onitsuka Tiger 男性")
    women_col = all_cols.get("Onitsuka Tiger 女性")

    if not men_col or not women_col:
        print("  ❌ 找不到「Onitsuka Tiger 男性」或「Onitsuka Tiger 女性」！")
        return

    men_col_id = men_col["id"]
    women_col_id = women_col["id"]
    target_col_ids = {men_col_id, women_col_id}

    print(f"  Onitsuka Tiger 男性: ID {men_col_id}")
    print(f"  Onitsuka Tiger 女性: ID {women_col_id}")

    # 取得所有 Onitsuka 商品
    print(f"\n  載入所有 Onitsuka Tiger 商品...")
    products = get_all_onitsuka_products()
    print(f"  📊 共 {len(products)} 個 Onitsuka Tiger 商品")

    # 找出不在目標 Collection 的商品
    print(f"  🔎 檢查歸類狀態...")
    orphans = []
    checked = 0
    for p in products:
        product_cols = get_product_collection_ids(p["id"])
        if not product_cols.intersection(target_col_ids):
            orphans.append(p)
        checked += 1
        if checked % 20 == 0:
            print(f"    ... 已檢查 {checked}/{len(products)}")
        time.sleep(0.1)

    print(f"\n  🔎 找到 {len(orphans)} 個未歸類商品")

    if not orphans:
        print("  ✅ 所有商品都已正確歸類！")
        return

    # 逐一歸類
    assigned = 0
    skipped = 0
    for p in orphans:
        gender = detect_gender_from_tags(p)
        target_names = GENDER_COLLECTION_MAP.get(gender, [])

        if not target_names:
            print(f"  ⏭️  {p['title'][:40]} → {gender}（無對應 Collection，跳過）")
            skipped += 1
            continue

        for col_name in target_names:
            col = all_cols.get(col_name)
            if col:
                add_product_to_collection(p["id"], col["id"])
            time.sleep(0.2)

        assigned += 1
        label = " + ".join(target_names)
        print(f"  ✅ [{assigned}] {p['title'][:40]} → {gender} → {label}")

    print(f"\n  📊 歸類結果: 成功 {assigned}, 跳過 {skipped}")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("🐯 Onitsuka Tiger Collection 搬移 + 自動歸類")
    print("=" * 60)

    if not SHOPIFY_ACCESS_TOKEN:
        print("❌ 請設定 SHOPIFY_ACCESS_TOKEN 環境變數")
        return

    print(f"🏪 商店: {SHOPIFY_STORE}")

    # 載入所有 Collection
    print("\n📋 載入所有 Collections...")
    all_cols = find_all_collections()
    print(f"  共 {len(all_cols)} 個 Collections")

    # 顯示相關的 onitsuka collections
    onitsuka_cols = {k: v for k, v in all_cols.items()
                     if "onitsuka" in k.lower()}
    if onitsuka_cols:
        print("\n  🐯 Onitsuka 相關 Collections:")
        for title, info in sorted(onitsuka_cols.items()):
            print(f"    • {title} (ID: {info['id']}, {info['type']})")

    # Phase 1: 搬移舊 → 新
    phase1_migrate(all_cols)

    # 重新載入（舊的已被刪除）
    print("\n📋 重新載入 Collections...")
    all_cols = find_all_collections()

    # Phase 2: 歸類孤兒商品
    phase2_assign_orphans(all_cols)

    # 提醒更新爬蟲
    print("\n" + "=" * 60)
    print("⚠️  記得更新 Onitsuka 爬蟲 scraper.py 的 _get_collections_by_gender():")
    print('     men    → "Onitsuka Tiger 男性"')
    print('     women  → "Onitsuka Tiger 女性"')
    print('     unisex → ["Onitsuka Tiger 男性", "Onitsuka Tiger 女性"]')
    print("=" * 60)
    print("\n✅ 全部完成！")


if __name__ == "__main__":
    main()
