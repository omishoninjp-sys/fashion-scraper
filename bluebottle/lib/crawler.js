/**
 * Blue Bottle Coffee Japan 爬蟲核心邏輯
 * 從 Shopify JSON API 抓取 → OpenAI 翻譯 → 同步至目標 Shopify
 */

const axios = require('axios');
const { log } = require('./logger');

// ============================================================
// 設定
// ============================================================
const config = {
  source: {
    baseUrl: 'https://store.bluebottlecoffee.jp',
    productsJsonUrl: 'https://store.bluebottlecoffee.jp/products.json',
    pageSize: 250,
  },

  target: {
    shop: () => process.env.SHOPIFY_SHOP,
    accessToken: () => process.env.SHOPIFY_ACCESS_TOKEN,
    apiVersion: '2024-10',
  },

  openai: {
    apiKey: () => process.env.OPENAI_API_KEY,
    model: process.env.OPENAI_MODEL || 'gpt-4o-mini',
  },

  crawler: {
    delayBetweenRequests: 1000,
    delayBetweenTranslations: 500,
    maxRetries: 3,
    deleteUnavailableVariants: true,
    skipSubscription: true,
  },

  // 日文 collection handle → 中文標籤
  categoryMap: {
    'coffee': '咖啡',
    'blend': '綜合咖啡',
    'single-origin': '單品咖啡',
    'instant-coffee': '即溶咖啡',
    'nola-base': 'Nola Base',
    'coffee-set': '咖啡套組',
    'drinkwear': '飲品器皿',
    'mug': '馬克杯',
    'bottle': '隨行杯/水瓶',
    'brewing': '沖泡器具',
    'lifestyle': '生活雜貨',
    'apparel': '服飾配件',
    'others': '其他雜貨',
    'food': '食品',
    'granola': '穀麥片',
    'yokan': '羊羹',
    'drink': '其他飲品',
    'alcohol': '酒類',
    'hm': 'Human Made 聯名',
    'gift': '禮品套組',
    'new-item': '新品',
    'online_limited': '線上限定',
    'ranking': '暢銷排行',
  },
};

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ============================================================
// 1. 來源抓取
// ============================================================

async function fetchAllProducts() {
  const allProducts = [];
  let page = 1;
  let hasMore = true;

  log('開始抓取 Blue Bottle Coffee JP 商品...');

  while (hasMore) {
    try {
      const url = `${config.source.productsJsonUrl}?limit=${config.source.pageSize}&page=${page}`;
      log(`  抓取第 ${page} 頁...`);

      const response = await axios.get(url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
          'Accept': 'application/json',
        },
        timeout: 30000,
      });

      const products = response.data.products;

      if (!products || products.length === 0) {
        hasMore = false;
      } else {
        allProducts.push(...products);
        log(`  第 ${page} 頁: ${products.length} 個（累計 ${allProducts.length}）`);

        if (products.length < config.source.pageSize) {
          hasMore = false;
        } else {
          page++;
          await sleep(config.crawler.delayBetweenRequests);
        }
      }
    } catch (error) {
      log(`❌ 抓取第 ${page} 頁失敗: ${error.message}`);
      if (error.response?.status === 429) {
        log('  被限流，等待 10 秒...');
        await sleep(10000);
      } else {
        hasMore = false;
      }
    }
  }

  log(`✅ 共抓取 ${allProducts.length} 個商品`);
  return allProducts;
}

async function fetchCollectionProductHandles(collectionHandle) {
  const handles = [];
  let page = 1;
  let hasMore = true;

  while (hasMore) {
    try {
      const url = `${config.source.baseUrl}/collections/${collectionHandle}/products.json?limit=250&page=${page}`;
      const response = await axios.get(url, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
        timeout: 15000,
      });

      const products = response.data.products || [];
      handles.push(...products.map(p => p.handle));

      if (products.length < 250) {
        hasMore = false;
      } else {
        page++;
        await sleep(500);
      }
    } catch {
      hasMore = false;
    }
  }

  return handles;
}

async function buildProductCategoryMap() {
  const productCategories = {};

  log('建立商品分類對照表...');

  for (const [handle, zhLabel] of Object.entries(config.categoryMap)) {
    try {
      const handles = await fetchCollectionProductHandles(handle);
      for (const h of handles) {
        if (!productCategories[h]) productCategories[h] = [];
        productCategories[h].push(zhLabel);
      }
      log(`  ${handle} → ${zhLabel}: ${handles.length} 商品`);
      await sleep(500);
    } catch (error) {
      log(`  ⚠️ ${handle} 失敗: ${error.message}`);
    }
  }

  return productCategories;
}

// ============================================================
// 2. OpenAI 翻譯
// ============================================================

async function translateProduct(product) {
  const textsToTranslate = {
    title: product.title,
    body_html: product.body_html || '',
  };

  const optionValues = new Set();
  if (product.options) {
    product.options.forEach(opt => {
      if (opt.name && opt.name !== 'Title') optionValues.add(opt.name);
      if (opt.values) opt.values.forEach(v => {
        if (v !== 'Default Title') optionValues.add(v);
      });
    });
  }

  const prompt = `你是專業的日文翻譯，請將以下 JSON 的值從日文翻譯成繁體中文。
保留 HTML 標籤結構，只翻譯文字內容。
品牌名稱 "ブルーボトルコーヒー" 翻譯為 "藍瓶咖啡"。
"ヒューマンメイド" 翻譯為 "Human Made"。
咖啡專有名詞保留原文（如 Bella Donovan, Three Africas 等）。
產地名保留原文或常用翻譯。重量/容量單位不變。
請只回傳 JSON，不要加任何說明。

${JSON.stringify(textsToTranslate, null, 2)}

${optionValues.size > 0 ? `\n也請翻譯這些選項值:\n${JSON.stringify([...optionValues])}` : ''}`;

  for (let retry = 0; retry < config.crawler.maxRetries; retry++) {
    try {
      const response = await axios.post('https://api.openai.com/v1/chat/completions', {
        model: config.openai.model,
        messages: [
          { role: 'system', content: '你是專業翻譯，只回傳 JSON 格式。' },
          { role: 'user', content: prompt },
        ],
        temperature: 0.3,
        max_tokens: 4000,
      }, {
        headers: {
          'Authorization': `Bearer ${config.openai.apiKey()}`,
          'Content-Type': 'application/json',
        },
        timeout: 60000,
      });

      const content = response.data.choices[0].message.content;
      const cleanJson = content.replace(/```json\n?/g, '').replace(/```\n?/g, '').trim();
      const translated = JSON.parse(cleanJson);

      return {
        title: translated.title || product.title,
        body_html: translated.body_html || product.body_html,
      };
    } catch (error) {
      log(`  ⚠️ 翻譯重試 ${retry + 1}: ${error.message}`);
      await sleep(2000);
    }
  }

  log(`  ❌ 翻譯失敗，使用原文: ${product.title}`);
  return { title: product.title, body_html: product.body_html };
}

// ============================================================
// 3. 目標 Shopify 操作
// ============================================================

function shopifyApi() {
  return axios.create({
    baseURL: `https://${config.target.shop()}/admin/api/${config.target.apiVersion}`,
    headers: {
      'X-Shopify-Access-Token': config.target.accessToken(),
      'Content-Type': 'application/json',
    },
    timeout: 30000,
  });
}

async function findProductByHandle(handle) {
  try {
    const api = shopifyApi();
    const res = await api.get(`/products.json?handle=${handle}&limit=1`);
    return res.data.products.length > 0 ? res.data.products[0] : null;
  } catch (error) {
    log(`  ⚠️ 查詢失敗 (${handle}): ${error.message}`);
    return null;
  }
}

async function createProduct(data) {
  try {
    const api = shopifyApi();
    const res = await api.post('/products.json', { product: data });
    return res.data.product;
  } catch (error) {
    const msg = error.response ? JSON.stringify(error.response.data) : error.message;
    log(`  ❌ 建立失敗: ${msg}`);
    return null;
  }
}

async function updateProduct(id, data) {
  try {
    const api = shopifyApi();
    const res = await api.put(`/products/${id}.json`, { product: data });
    return res.data.product;
  } catch (error) {
    const msg = error.response ? JSON.stringify(error.response.data) : error.message;
    log(`  ❌ 更新失敗 (${id}): ${msg}`);
    return null;
  }
}

async function deleteVariant(productId, variantId) {
  try {
    const api = shopifyApi();
    await api.delete(`/products/${productId}/variants/${variantId}.json`);
    return true;
  } catch (error) {
    log(`  ⚠️ 刪除 variant 失敗 (${variantId}): ${error.message}`);
    return false;
  }
}

async function setVariantUnavailable(variantId) {
  try {
    const api = shopifyApi();
    await api.put(`/variants/${variantId}.json`, {
      variant: { id: variantId, inventory_management: 'shopify', inventory_policy: 'deny' },
    });

    const variantRes = await api.get(`/variants/${variantId}.json`);
    const inventoryItemId = variantRes.data.variant.inventory_item_id;

    const locRes = await api.get('/locations.json');
    const locationId = locRes.data.locations[0]?.id;

    if (locationId && inventoryItemId) {
      await api.post('/inventory_levels/set.json', {
        location_id: locationId,
        inventory_item_id: inventoryItemId,
        available: 0,
      });
    }
    return true;
  } catch (error) {
    log(`  ⚠️ 設定售罄失敗 (${variantId}): ${error.message}`);
    return false;
  }
}

// ============================================================
// 4. 商品轉換
// ============================================================

function transformProduct(source, translated, categoryTags = []) {
  if (config.crawler.skipSubscription && source.handle?.startsWith('su')) {
    return null;
  }

  const isAvailable = source.variants?.some(v => v.available) ?? true;

  const tags = ['Blue Bottle Coffee', '藍瓶咖啡', '日本代購', ...categoryTags];
  if (!isAvailable) tags.push('售罄');
  if (source.title?.includes('ヒューマンメイド') || source.title?.includes('Human Made')) {
    tags.push('Human Made 聯名');
  }
  if (source.title?.includes('オンライン限定')) tags.push('線上限定');
  if (source.title?.includes('期間限定')) tags.push('期間限定');

  const variants = source.variants?.map(v => ({
    title: v.title,
    price: v.price,
    compare_at_price: v.compare_at_price || null,
    sku: `BBC-${v.sku || source.handle}-${v.id}`,
    weight: v.grams ? v.grams / 1000 : null,
    weight_unit: 'kg',
    inventory_management: 'shopify',
    inventory_policy: 'deny',
    requires_shipping: true,
    option1: v.option1,
    option2: v.option2,
    option3: v.option3,
    _available: v.available,
    _source_id: v.id,
  })) || [];

  const images = source.images?.map(img => ({
    src: img.src,
    alt: translated.title,
  })) || [];

  const titlePrefix = '【藍瓶咖啡】';
  const finalTitle = translated.title.startsWith(titlePrefix)
    ? translated.title
    : `${titlePrefix}${translated.title}`;

  const descFooter = `
<div class="product-source-info" style="margin-top:20px;padding:15px;background:#f7f7f7;border-radius:8px;">
  <p style="margin:0 0 8px;font-weight:bold;">📦 日本 Blue Bottle Coffee 官方商品</p>
  <p style="margin:0 0 5px;font-size:14px;">• 日本官網直送，100% 正品保證</p>
  <p style="margin:0 0 5px;font-size:14px;">• 商品來源：<a href="${config.source.baseUrl}/products/${source.handle}" target="_blank">Blue Bottle Coffee Japan</a></p>
  <p style="margin:0;font-size:14px;">• 到貨時間約 7-14 個工作天</p>
</div>`;

  return {
    title: finalTitle,
    handle: `bbc-${source.handle}`,
    body_html: (translated.body_html || '') + descFooter,
    vendor: 'Blue Bottle Coffee',
    product_type: categoryTags[0] || '咖啡',
    tags: tags.join(', '),
    published: isAvailable,
    variants,
    images,
    options: source.options?.map(opt => ({
      name: opt.name === 'Title' ? 'Title' : (opt.name || 'Title'),
      values: opt.values || ['Default Title'],
    })),
    metafields: [
      { namespace: 'source', key: 'original_url', value: `${config.source.baseUrl}/products/${source.handle}`, type: 'single_line_text_field' },
      { namespace: 'source', key: 'original_price_jpy', value: source.variants?.[0]?.price || '0', type: 'single_line_text_field' },
      { namespace: 'source', key: 'last_synced', value: new Date().toISOString(), type: 'single_line_text_field' },
    ],
  };
}

// ============================================================
// 5. Variant 同步
// ============================================================

async function syncVariants(existing, transformed) {
  const sourceVariants = transformed.variants || [];
  const existingVariants = existing.variants || [];

  for (const ev of existingVariants) {
    const matching = sourceVariants.find(sv => sv.sku === ev.sku);

    if (matching && !matching._available) {
      if (existingVariants.length > 1) {
        log(`    🗑️ 刪除售罄 variant: ${ev.title}`);
        await deleteVariant(existing.id, ev.id);
        await sleep(300);
      } else {
        log(`    📦 設定售罄: ${ev.title}`);
        await setVariantUnavailable(ev.id);
      }
    }
  }
}

// ============================================================
// 6. 主要同步
// ============================================================

async function syncProducts() {
  log('========================================');
  log('Blue Bottle Coffee JP 同步開始');
  log('========================================');

  const sourceProducts = await fetchAllProducts();
  if (sourceProducts.length === 0) {
    log('❌ 未抓取到任何商品');
    return { created: 0, updated: 0, skipped: 0, errors: 0, total: 0 };
  }

  const productCategories = await buildProductCategoryMap();

  let created = 0, updated = 0, skipped = 0, errors = 0;

  for (let i = 0; i < sourceProducts.length; i++) {
    const source = sourceProducts[i];
    log(`[${i + 1}/${sourceProducts.length}] ${source.title} (${source.handle})`);

    if (config.crawler.skipSubscription && source.handle?.startsWith('su')) {
      log('  ⏭️ 跳過定期便');
      skipped++;
      continue;
    }

    try {
      log('  🌐 翻譯中...');
      const translated = await translateProduct(source);
      await sleep(config.crawler.delayBetweenTranslations);

      const categoryTags = productCategories[source.handle] || [];
      const transformed = transformProduct(source, translated, categoryTags);

      if (!transformed) { skipped++; continue; }

      const existing = await findProductByHandle(`bbc-${source.handle}`);
      await sleep(300);

      if (existing) {
        log(`  🔄 更新 (ID: ${existing.id})`);

        if (config.crawler.deleteUnavailableVariants) {
          await syncVariants(existing, transformed);
        }

        const result = await updateProduct(existing.id, {
          id: existing.id,
          title: transformed.title,
          body_html: transformed.body_html,
          tags: transformed.tags,
          published: transformed.published,
        });

        result ? updated++ : errors++;
      } else {
        log('  🆕 建立新商品');

        transformed.variants = transformed.variants.map(v => {
          const { _available, _source_id, ...clean } = v;
          return clean;
        });

        const result = await createProduct(transformed);
        if (result) {
          if (config.crawler.deleteUnavailableVariants) {
            for (let vi = 0; vi < (source.variants || []).length; vi++) {
              if (!source.variants[vi].available && result.variants[vi]) {
                await setVariantUnavailable(result.variants[vi].id);
              }
            }
          }
          created++;
        } else {
          errors++;
        }
      }

      await sleep(config.crawler.delayBetweenRequests);
    } catch (error) {
      log(`  ❌ 失敗: ${error.message}`);
      errors++;
    }
  }

  const result = { created, updated, skipped, errors, total: sourceProducts.length };
  log('========================================');
  log(`完成: 新建 ${created} / 更新 ${updated} / 跳過 ${skipped} / 錯誤 ${errors}`);
  log('========================================');

  return result;
}

/**
 * 測試上架：只抓取並上架前 N 個商品（跳過已存在的）
 * 用來確認整個流程（抓取→翻譯→上架）是否正常
 */
async function testUpload(count = 3) {
  log('========================================');
  log(`🧪 測試上架模式：上架 ${count} 個商品`);
  log('========================================');

  // Step 1: 抓取來源商品
  const sourceProducts = await fetchAllProducts();
  if (sourceProducts.length === 0) {
    log('❌ 未抓取到任何商品');
    return { created: 0, skipped: 0, errors: 0, total: 0, products: [] };
  }

  // Step 2: 建立分類
  const productCategories = await buildProductCategoryMap();

  // Step 3: 逐一處理，直到成功上架 N 個
  let created = 0, skipped = 0, errors = 0;
  const products = []; // 前端顯示用

  for (let i = 0; i < sourceProducts.length; i++) {
    if (created >= count) break; // 已達目標數量

    const source = sourceProducts[i];
    log(`[${i + 1}/${sourceProducts.length}] ${source.title} (${source.handle})`);

    // 跳過定期便
    if (config.crawler.skipSubscription && source.handle?.startsWith('su')) {
      log('  ⏭️ 跳過定期便');
      products.push({ handle: source.handle, title: source.title, price: source.variants?.[0]?.price, status: 'skip', status_text: '定期便' });
      skipped++;
      continue;
    }

    // 跳過售罄
    const isAvailable = source.variants?.some(v => v.available) ?? true;
    if (!isAvailable) {
      log('  ⏭️ 跳過售罄');
      products.push({ handle: source.handle, title: source.title, price: source.variants?.[0]?.price, status: 'skip', status_text: '售罄' });
      skipped++;
      continue;
    }

    try {
      // 翻譯
      log('  🌐 翻譯中...');
      const translated = await translateProduct(source);
      await sleep(config.crawler.delayBetweenTranslations);

      // 轉換
      const categoryTags = productCategories[source.handle] || [];
      const transformed = transformProduct(source, translated, categoryTags);
      if (!transformed) {
        products.push({ handle: source.handle, title: source.title, price: source.variants?.[0]?.price, status: 'skip', status_text: '轉換失敗' });
        skipped++;
        continue;
      }

      // 檢查是否已存在
      const existing = await findProductByHandle(`bbc-${source.handle}`);
      await sleep(300);

      if (existing) {
        log(`  ⏭️ 已存在 (ID: ${existing.id})`);
        products.push({ handle: source.handle, title: translated.title || source.title, price: source.variants?.[0]?.price, status: 'skip', status_text: '已存在' });
        skipped++;
        continue;
      }

      // 上架
      log('  🆕 上架中...');
      transformed.variants = transformed.variants.map(v => {
        const { _available, _source_id, ...clean } = v;
        return clean;
      });

      const result = await createProduct(transformed);
      if (result) {
        // 設定售罄 variant 庫存
        if (config.crawler.deleteUnavailableVariants) {
          for (let vi = 0; vi < (source.variants || []).length; vi++) {
            if (!source.variants[vi].available && result.variants[vi]) {
              await setVariantUnavailable(result.variants[vi].id);
            }
          }
        }
        log(`  ✅ 上架成功: ${result.title} (ID: ${result.id})`);
        products.push({
          handle: source.handle,
          title: transformed.title,
          price: source.variants?.[0]?.price,
          shopify_id: result.id,
          status: 'success',
          status_text: '已上架',
        });
        created++;
      } else {
        products.push({ handle: source.handle, title: source.title, price: source.variants?.[0]?.price, status: 'error', status_text: '上架失敗' });
        errors++;
      }

      await sleep(config.crawler.delayBetweenRequests);
    } catch (error) {
      log(`  ❌ 失敗: ${error.message}`);
      products.push({ handle: source.handle, title: source.title, price: source.variants?.[0]?.price, status: 'error', status_text: error.message.slice(0, 50) });
      errors++;
    }
  }

  log('========================================');
  log(`🧪 測試上架完成: 成功 ${created} / 跳過 ${skipped} / 失敗 ${errors}`);
  log('========================================');

  return { created, skipped, errors, total: created + skipped + errors, products };
}

module.exports = {
  fetchAllProducts,
  buildProductCategoryMap,
  syncProducts,
  testUpload,
};
