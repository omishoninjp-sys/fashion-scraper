/**
 * 價格計算工具
 * 公式: 日幣 / 0.7 + 1250 = Shopify 零售價（日幣）
 */

const axios = require('axios');
const { log } = require('./logger');

function calculatePrice(jpyPrice) {
  const jpy = parseFloat(jpyPrice);
  if (isNaN(jpy) || jpy <= 0) return 0;
  return Math.ceil(jpy / 0.7 + 1250);
}

async function updateAllPrices() {
  log('💰 價格更新: 公式 = 日幣/0.7 + 1250');

  const shop = process.env.SHOPIFY_SHOP || '';
  const shopDomain = shop.includes('.') ? shop : `${shop}.myshopify.com`;

  const api = axios.create({
    baseURL: `https://${shopDomain}/admin/api/2024-10`,
    headers: {
      'X-Shopify-Access-Token': process.env.SHOPIFY_ACCESS_TOKEN,
      'Content-Type': 'application/json',
    },
  });

  let sinceId = 0, updatedCount = 0, hasMore = true;

  while (hasMore) {
    const res = await api.get(`/products.json?limit=250&since_id=${sinceId}&vendor=Blue+Bottle+Coffee`);
    const products = res.data.products;
    if (products.length === 0) { hasMore = false; break; }

    for (const product of products) {
      let originalJpy = null;
      try {
        const mfRes = await api.get(`/products/${product.id}/metafields.json?namespace=source&key=original_price_jpy`);
        const mf = mfRes.data.metafields?.[0];
        if (mf) originalJpy = parseFloat(mf.value);
      } catch (e) {}

      for (const variant of product.variants) {
        const jpyBase = originalJpy || parseFloat(variant.price);
        const newPrice = calculatePrice(jpyBase);

        if (newPrice > 0 && newPrice.toString() !== variant.price) {
          try {
            await api.put(`/variants/${variant.id}.json`, {
              variant: { id: variant.id, price: newPrice.toString() },
            });
            log(`  💰 ${product.title} | ${variant.title}: ¥${jpyBase} → ¥${newPrice}`);
            updatedCount++;
          } catch (error) {
            log(`  ❌ 更新失敗: ${variant.id} - ${error.message}`);
          }
          await new Promise(r => setTimeout(r, 200));
        }
      }
    }
    sinceId = products[products.length - 1].id;
  }

  log(`✅ 價格更新完成: ${updatedCount} 個 variant`);
  return { updated: updatedCount };
}

module.exports = { calculatePrice, updateAllPrices };
