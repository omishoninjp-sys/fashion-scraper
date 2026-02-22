/**
 * 價格計算工具
 * JPY → TWD 含代購費用、運費、利潤
 */

const axios = require('axios');
const { log } = require('./logger');

function getPriceConfig(overrideRate) {
  return {
    exchangeRate: overrideRate || parseFloat(process.env.JPY_TO_TWD_RATE) || 0.22,
    serviceFeeRate: parseFloat(process.env.SERVICE_FEE_RATE) || 0.10,
    shippingPerItem: parseFloat(process.env.SHIPPING_PER_ITEM) || 150,
    profitMargin: parseFloat(process.env.PROFIT_MARGIN) || 0.15,
    minProfit: parseFloat(process.env.MIN_PROFIT) || 50,
    roundTo: 10,
  };
}

function calculateTWDPrice(jpyPrice, overrideRate) {
  const cfg = getPriceConfig(overrideRate);
  const jpy = parseFloat(jpyPrice);
  if (isNaN(jpy) || jpy <= 0) return 0;

  const baseTWD = jpy * cfg.exchangeRate;
  const withServiceFee = baseTWD * (1 + cfg.serviceFeeRate);
  const withShipping = withServiceFee + cfg.shippingPerItem;
  const profit = Math.max(withShipping * cfg.profitMargin, cfg.minProfit);
  const finalPrice = withShipping + profit;

  return Math.ceil(finalPrice / cfg.roundTo) * cfg.roundTo;
}

async function updateAllPrices(overrideRate) {
  const cfg = getPriceConfig(overrideRate);

  log(`💰 價格更新: 匯率 ${cfg.exchangeRate}, 服務費 ${cfg.serviceFeeRate * 100}%, 運費 NT$${cfg.shippingPerItem}, 利潤 ${cfg.profitMargin * 100}%`);

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
        const twdPrice = calculateTWDPrice(jpyPrice, overrideRate);

        if (twdPrice > 0) {
          try {
            await api.put(`/variants/${variant.id}.json`, {
              variant: { id: variant.id, price: twdPrice.toString() },
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
  return { updated: updatedCount, rate: cfg.exchangeRate };
}

module.exports = { calculateTWDPrice, updateAllPrices };
