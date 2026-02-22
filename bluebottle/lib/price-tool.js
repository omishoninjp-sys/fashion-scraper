/**
 * 價格計算工具
 * 公式: 日幣 / 0.7 + 重量(kg) * 1250 = Shopify 零售價
 */

const axios = require('axios');
const { log } = require('./logger');

const DIVISOR = parseFloat(process.env.PRICE_DIVISOR) || 0.7;
const WEIGHT_MULTIPLIER = parseFloat(process.env.WEIGHT_MULTIPLIER) || 1250;
const ROUND_TO = parseInt(process.env.PRICE_ROUND_TO) || 10;

function calculatePrice(jpyPrice, weightKg = 0) {
  const jpy = parseFloat(jpyPrice);
  if (isNaN(jpy) || jpy <= 0) return 0;

  const wt = parseFloat(weightKg) || 0;
  const raw = jpy / DIVISOR + wt * WEIGHT_MULTIPLIER;

  return Math.ceil(raw / ROUND_TO) * ROUND_TO;
}

async function updateAllPrices() {
  log(`💰 價格更新: 公式 = JPY / ${DIVISOR} + 重量(kg) × ${WEIGHT_MULTIPLIER}`);

  const api = axios.create({
    baseURL: `https://${process.env.SHOPIFY_SHOP}/admin/api/2024-10`,
    headers: {
      'X-Shopify-Access-Token': process.env.SHOPIFY_ACCESS_TOKEN,
      'Content-Type': 'application/json',
    },
  });

  let sinceId = 0;
  let updatedCount = 0;
  let hasMore = true;

  while (hasMore) {
    const res = await api.get(`/products.json?limit=250&since_id=${sinceId}&vendor=Blue+Bottle+Coffee`);
    const products = res.data.products;

    if (products.length === 0) { hasMore = false; break; }

    for (const product of products) {
      for (const variant of product.variants) {
        const jpyPrice = parseFloat(variant.price);
        const weightKg = variant.grams ? variant.grams / 1000 : 0;
        const newPrice = calculatePrice(jpyPrice, weightKg);

        if (newPrice > 0) {
          try {
            await api.put(`/variants/${variant.id}.json`, {
              variant: { id: variant.id, price: newPrice.toString() },
            });
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
