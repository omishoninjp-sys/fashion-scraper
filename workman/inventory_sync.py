"""
WORKMAN 庫存同步模組

功能：
1. 從 Shopify 取得所有 WORKMAN 商品的來源連結（metafield: custom.link）
2. 到 workman.jp 檢查每個商品的庫存狀態
3. 如果出現以下標示，代表線上無法購買：
   - 店舗のみのお取り扱い（僅限門市）
   - オンラインストア販売終了（線上商店銷售結束）
   - 店舗在庫を確認する（確認門市庫存）按鈕出現在購買區域
4. 根據狀態更新 Shopify：
   - 部分規格缺貨 → 該規格庫存歸零
   - 全部規格都缺貨 → 商品設為 DRAFT（草稿）
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import time
import os

# ========== 設定 ==========
SOURCE_URL = "https://workman.jp"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en;q=0.9',
}

# 缺貨關鍵字
OUT_OF_STOCK_KEYWORDS = [
    '店舗のみのお取り扱い',
    'オンラインストア販売終了',
    '店舗在庫を確認する',
]

# 同步狀態（供 API 查詢）
sync_status = {
    "running": False,
    "phase": "",
    "progress": 0,
    "total": 0,
    "current_product": "",
    "results": {
        "checked": 0,
        "in_stock": 0,
        "out_of_stock": 0,
        "draft_set": 0,
        "inventory_zeroed": 0,
        "errors": 0,
        "page_gone": 0,
    },
    "details": [],
    "errors": [],
}


def reset_sync_status():
    global sync_status
    sync_status = {
        "running": False,
        "phase": "",
        "progress": 0,
        "total": 0,
        "current_product": "",
        "results": {
            "checked": 0,
            "in_stock": 0,
            "out_of_stock": 0,
            "draft_set": 0,
            "inventory_zeroed": 0,
            "errors": 0,
            "page_gone": 0,
        },
        "details": [],
        "errors": [],
    }


# ========== Shopify GraphQL ==========

def graphql_request(query, variables=None):
    """執行 Shopify GraphQL 請求"""
    shop = os.environ.get("SHOPIFY_SHOP", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    
    url = f"https://{shop}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': token,
        'Content-Type': 'application/json',
    }
    payload = {'query': query}
    if variables:
        payload['variables'] = variables
    
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    return response.json()


# ========== 從 Shopify 取得商品資料 ==========

def fetch_workman_products_with_source():
    """
    取得所有 WORKMAN 商品，包含：
    - 商品 ID、標題、handle、狀態
    - metafield custom.link（WORKMAN 官網連結）
    - 所有 variant 的 ID 和 inventoryItem ID
    """
    all_products = []
    cursor = None
    
    while True:
        after_clause = f', after: "{cursor}"' if cursor else ''
        
        query = f"""
        {{
          products(first: 50, query: "vendor:WORKMAN"{after_clause}) {{
            edges {{
              node {{
                id
                title
                handle
                status
                metafield(namespace: "custom", key: "link") {{
                  value
                }}
                variants(first: 100) {{
                  edges {{
                    node {{
                      id
                      sku
                      inventoryItem {{
                        id
                        inventoryLevels(first: 5) {{
                          edges {{
                            node {{
                              id
                              quantities(names: ["available"]) {{
                                name
                                quantity
                              }}
                              location {{
                                id
                              }}
                            }}
                          }}
                        }}
                      }}
                    }}
                  }}
                }}
              }}
              cursor
            }}
            pageInfo {{
              hasNextPage
            }}
          }}
        }}
        """
        
        result = graphql_request(query)
        products = result.get('data', {}).get('products', {})
        edges = products.get('edges', [])
        
        for edge in edges:
            node = edge['node']
            source_url = node.get('metafield', {}).get('value', '') if node.get('metafield') else ''
            
            variants = []
            for v_edge in node.get('variants', {}).get('edges', []):
                v_node = v_edge['node']
                inv_item = v_node.get('inventoryItem', {})
                inv_levels = inv_item.get('inventoryLevels', {}).get('edges', [])
                
                variant_info = {
                    'id': v_node['id'],
                    'sku': v_node.get('sku', ''),
                    'inventory_item_id': inv_item.get('id', ''),
                    'inventory_levels': []
                }
                
                for level_edge in inv_levels:
                    level_node = level_edge['node']
                    quantities = level_node.get('quantities', [])
                    available = 0
                    for q in quantities:
                        if q['name'] == 'available':
                            available = q['quantity']
                    
                    variant_info['inventory_levels'].append({
                        'id': level_node['id'],
                        'location_id': level_node.get('location', {}).get('id', ''),
                        'available': available
                    })
                
                variants.append(variant_info)
            
            all_products.append({
                'id': node['id'],
                'title': node['title'],
                'handle': node['handle'],
                'status': node['status'],
                'source_url': source_url,
                'variants': variants
            })
            
            cursor = edge['cursor']
        
        if not products.get('pageInfo', {}).get('hasNextPage', False):
            break
        
        time.sleep(0.5)
    
    print(f"[Sync] 取得 {len(all_products)} 個 WORKMAN 商品")
    return all_products


# ========== 檢查 WORKMAN 官網庫存 ==========

def check_workman_stock(product_url):
    """
    檢查 WORKMAN 官網商品頁面的庫存狀態
    
    回傳：
    {
        'available': True/False,      # 商品整體是否可購買
        'page_exists': True/False,    # 頁面是否存在
        'out_of_stock_reason': '',    # 缺貨原因
        'variant_stock': {}           # 各規格的庫存狀態（如果能判斷）
    }
    """
    result = {
        'available': True,
        'page_exists': True,
        'out_of_stock_reason': '',
        'variant_stock': {}
    }
    
    if not product_url:
        result['available'] = False
        result['page_exists'] = False
        result['out_of_stock_reason'] = '無來源連結'
        return result
    
    try:
        response = requests.get(product_url, headers=HEADERS, timeout=30)
        
        # 頁面不存在
        if response.status_code == 404:
            result['available'] = False
            result['page_exists'] = False
            result['out_of_stock_reason'] = '頁面已不存在 (404)'
            return result
        
        if response.status_code != 200:
            result['available'] = False
            result['page_exists'] = False
            result['out_of_stock_reason'] = f'HTTP {response.status_code}'
            return result
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # 檢查整頁是否包含缺貨關鍵字
        for keyword in OUT_OF_STOCK_KEYWORDS:
            if keyword in page_text:
                result['available'] = False
                result['out_of_stock_reason'] = keyword
                print(f"[Stock] ❌ 缺貨: {keyword}")
                return result
        
        # 額外檢查：「カートに入れる」按鈕是否存在且可用
        cart_button = soup.find('input', {'value': 'カートに入れる'})
        if not cart_button:
            cart_button = soup.find('button', string=re.compile(r'カートに入れる'))
        
        if not cart_button:
            # 找不到加入購物車按鈕，也可能是缺貨
            # 但要小心：有些頁面結構不同
            # 再檢查是否有「売り切れ」或「品切れ」
            if '売り切れ' in page_text or '品切れ' in page_text:
                result['available'] = False
                result['out_of_stock_reason'] = '売り切れ / 品切れ'
                return result
        
        # 商品有貨
        result['available'] = True
        return result
        
    except requests.exceptions.Timeout:
        result['available'] = True  # 超時不確定，先不動
        result['out_of_stock_reason'] = '連線超時'
        return result
    except Exception as e:
        result['available'] = True  # 錯誤時不確定，先不動
        result['out_of_stock_reason'] = f'檢查錯誤: {str(e)}'
        return result


# ========== 更新 Shopify 庫存 ==========

def set_product_to_draft(product_id):
    """將商品設為草稿（DRAFT）"""
    mutation = """
    mutation productUpdate($input: ProductInput!) {
      productUpdate(input: $input) {
        product {
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
    
    result = graphql_request(mutation, {
        "input": {
            "id": product_id,
            "status": "DRAFT"
        }
    })
    
    errors = result.get('data', {}).get('productUpdate', {}).get('userErrors', [])
    if errors:
        print(f"[Sync] ❌ 設定草稿失敗 {product_id}: {errors}")
        return False
    
    status = result.get('data', {}).get('productUpdate', {}).get('product', {}).get('status', '')
    print(f"[Sync] ✓ 已設為草稿: {product_id} -> {status}")
    return True


def zero_variant_inventory(inventory_item_id, location_id):
    """將某個 variant 的庫存設為 0"""
    mutation = """
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
      inventorySetQuantities(input: $input) {
        inventoryAdjustmentGroup {
          reason
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
            "reason": "correction",
            "name": "available",
            "quantities": [
                {
                    "inventoryItemId": inventory_item_id,
                    "locationId": location_id,
                    "quantity": 0
                }
            ]
        }
    })
    
    errors = result.get('data', {}).get('inventorySetQuantities', {}).get('userErrors', [])
    if errors:
        print(f"[Sync] ❌ 庫存歸零失敗 {inventory_item_id}: {errors}")
        return False
    
    return True


# ========== 主同步流程 ==========

def run_inventory_sync():
    """
    執行庫存同步：
    1. 取得所有 WORKMAN 商品
    2. 逐一檢查官網庫存
    3. 缺貨的 → 庫存歸零 + 設為草稿
    """
    global sync_status
    reset_sync_status()
    
    sync_status['running'] = True
    sync_status['phase'] = 'fetching'
    sync_status['current_product'] = '正在取得 Shopify 商品清單...'
    
    print(f"[Sync] ========== 開始庫存同步 ==========")
    print(f"[Sync] 時間: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        # 1. 取得所有 WORKMAN 商品
        products = fetch_workman_products_with_source()
        sync_status['total'] = len(products)
        
        if not products:
            sync_status['current_product'] = '沒有找到 WORKMAN 商品'
            sync_status['running'] = False
            return {'success': False, 'error': 'No WORKMAN products found'}
        
        # 2. 逐一檢查
        sync_status['phase'] = 'checking'
        
        for idx, product in enumerate(products):
            sync_status['progress'] = idx + 1
            sync_status['current_product'] = f"[{idx+1}/{len(products)}] {product['title'][:30]}"
            
            product_id = product['id']
            source_url = product['source_url']
            title = product['title']
            
            # 跳過已經是 DRAFT 的商品
            if product['status'] == 'DRAFT':
                sync_status['results']['checked'] += 1
                print(f"[Sync] ⏭ 跳過（已是草稿）: {title[:30]}")
                continue
            
            # 沒有來源連結的商品，無法檢查
            if not source_url:
                sync_status['results']['checked'] += 1
                sync_status['results']['errors'] += 1
                sync_status['errors'].append({
                    'product': title[:30],
                    'error': '無來源連結 (metafield custom.link)'
                })
                print(f"[Sync] ⚠️ 無來源連結: {title[:30]}")
                continue
            
            # 檢查官網庫存
            print(f"[Sync] 檢查: {title[:30]} -> {source_url}")
            stock = check_workman_stock(source_url)
            sync_status['results']['checked'] += 1
            
            if stock['available']:
                # 有貨，不需動作
                sync_status['results']['in_stock'] += 1
                print(f"[Sync] ✓ 有貨: {title[:30]}")
                
                sync_status['details'].append({
                    'title': title[:40],
                    'status': 'in_stock',
                    'source_url': source_url
                })
                
            elif not stock['page_exists']:
                # 頁面不存在 → 直接設為草稿
                sync_status['results']['out_of_stock'] += 1
                sync_status['results']['page_gone'] += 1
                
                print(f"[Sync] ❌ 頁面不存在: {title[:30]} -> 設為草稿")
                
                # 所有 variant 庫存歸零
                for variant in product['variants']:
                    for level in variant['inventory_levels']:
                        if level['available'] > 0:
                            zero_variant_inventory(
                                variant['inventory_item_id'],
                                level['location_id']
                            )
                            sync_status['results']['inventory_zeroed'] += 1
                
                # 設為草稿
                if set_product_to_draft(product_id):
                    sync_status['results']['draft_set'] += 1
                
                sync_status['details'].append({
                    'title': title[:40],
                    'status': 'page_gone',
                    'reason': stock['out_of_stock_reason'],
                    'source_url': source_url
                })
                
            else:
                # 官網顯示缺貨（但頁面還在）
                sync_status['results']['out_of_stock'] += 1
                
                reason = stock['out_of_stock_reason']
                print(f"[Sync] ❌ 缺貨 ({reason}): {title[:30]}")
                
                # 所有 variant 庫存歸零
                for variant in product['variants']:
                    for level in variant['inventory_levels']:
                        if level['available'] > 0:
                            zero_variant_inventory(
                                variant['inventory_item_id'],
                                level['location_id']
                            )
                            sync_status['results']['inventory_zeroed'] += 1
                
                # 設為草稿
                if set_product_to_draft(product_id):
                    sync_status['results']['draft_set'] += 1
                
                sync_status['details'].append({
                    'title': title[:40],
                    'status': 'out_of_stock',
                    'reason': reason,
                    'source_url': source_url
                })
            
            # 避免請求過快
            time.sleep(1)
        
        # 完成
        sync_status['phase'] = 'completed'
        sync_status['current_product'] = (
            f"✅ 同步完成！"
            f"檢查: {sync_status['results']['checked']}, "
            f"有貨: {sync_status['results']['in_stock']}, "
            f"缺貨: {sync_status['results']['out_of_stock']}, "
            f"設草稿: {sync_status['results']['draft_set']}"
        )
        
        print(f"[Sync] ========== 庫存同步完成 ==========")
        print(f"[Sync] 檢查: {sync_status['results']['checked']}")
        print(f"[Sync] 有貨: {sync_status['results']['in_stock']}")
        print(f"[Sync] 缺貨: {sync_status['results']['out_of_stock']}")
        print(f"[Sync] 頁面消失: {sync_status['results']['page_gone']}")
        print(f"[Sync] 設為草稿: {sync_status['results']['draft_set']}")
        print(f"[Sync] 庫存歸零: {sync_status['results']['inventory_zeroed']}")
        print(f"[Sync] 錯誤: {sync_status['results']['errors']}")
        
        return {
            'success': True,
            'results': sync_status['results']
        }
        
    except Exception as e:
        sync_status['errors'].append({'error': str(e)})
        sync_status['current_product'] = f"❌ 錯誤: {str(e)}"
        sync_status['phase'] = 'error'
        print(f"[Sync] ❌ 錯誤: {e}")
        import traceback
        traceback.print_exc()
        
        return {
            'success': False,
            'error': str(e)
        }
    finally:
        sync_status['running'] = False
