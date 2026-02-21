"""
智慧選擇器 - 從全店最新上架的前20個商品中隨機選擇（不分系列）
"""

import random


class SmartSelector:
    """最新商品選擇器"""
    
    def __init__(self, shopify_client, config):
        self.shopify = shopify_client
        self.config = config
        self.last_category = None
    
    def get_next_product(self, category=None):
        """
        從全店最新上架的前20個商品中隨機選擇（不分系列）
        
        Returns:
            (product, category) 或 (None, None)
        """
        print(f"   📊 從全店最新 20 個商品中選擇（不分系列）")
        
        # 直接抓全店商品（Shopify API 預設按建立時間排序）
        all_products = self.shopify.get_all_products(limit=250)
        
        if not all_products:
            print(f"   ⚠️  沒有找到任何商品")
            return None, None
        
        # 按上架時間排序（新的優先）
        all_products.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        # 只取最新的前 20 個
        latest_products = all_products[:20]
        
        # 從最新 20 個中隨機選擇
        product = random.choice(latest_products)
        print(f"   ✅ 選擇商品: {product.get('title', 'Unknown')}（從最新 {len(latest_products)} 個中選出，全店共 {len(all_products)} 個）")
        
        self.last_category = 'fashion'
        return product, 'fashion'
    
    def mark_as_posted(self, product, category):
        """標記為已發文（目前不需要追蹤）"""
        pass
    
    def get_stats(self):
        """取得統計資訊"""
        all_products = self.shopify.get_all_products(limit=250)
        # 多頁的話要全部抓完
        total = len(all_products) if all_products else 0
        pool_size = min(total, 20)
        
        return {
            'souvenir': {
                'total': 0,
                'latest_10': 0,
                'round': 0,
                'posted_this_round': 0,
                'remaining': 0
            },
            'fashion': {
                'total': total,
                'latest_10': pool_size,
                'round': 1,
                'posted_this_round': 0,
                'remaining': pool_size
            }
        }
