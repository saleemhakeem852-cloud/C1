/**
 * Returns the product's display name regardless of which field
 * it uses (products.js uses 'name', products.json uses 'title').
 */
function getProductLabel(product) {
  return product.name || product.title || 'Unknown Product';
}

// --- SECTION A: STORAGE ---
const API_BASE = (typeof SERVER !== 'undefined') ? SERVER : 'https://clblast.up.railway.app';

async function saveProductsToStorage(productList) {
  localStorage.setItem('products', JSON.stringify(productList));
  try {
    const response = await fetch(`${API_BASE}/sync-products`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(productList)
    });
    if (!response.ok) {
      console.warn('Server sync failed with status:', response.status);
    }
  } catch (error) {
    console.warn('Server is offline or unreachable. LocalStorage updated.', error);
  }
}

async function loadProductsFromStorage() {
  // 1. Always try Render/Railway backend first (source of truth)
  try {
    const res = await fetch(`${API_BASE}/products`, {
      signal: AbortSignal.timeout(5000)
    });
    if (res.ok) {
      const serverProducts = await res.json();
      if (serverProducts && serverProducts.length > 0) {
        // Update localStorage cache with server data
        localStorage.setItem('products', JSON.stringify(serverProducts));
        return serverProducts;
      }
    }
  } catch (error) {
    console.warn('Server unreachable, falling back to local data.', error);
  }

  // 2. Fall back to localStorage
  const saved = localStorage.getItem('products');
  if (saved) {
    try {
      return JSON.parse(saved);
    } catch {
      // corrupted localStorage, fall through
    }
  }

  // 3. Last resort: static products.js array
  return window.products || [];
}

// --- SECTION B: AI PRICING ---

async function fetchAIPrice(product) {
  try {
    const AI_PRICING_API_KEY = localStorage.getItem('ai_pricing_key') || '';
    const productLabel = getProductLabel(product);
    const url = `https://api.scrapebadger.com/google/shopping/search?q=${encodeURIComponent(productLabel)}&api_key=${AI_PRICING_API_KEY}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    const data = await response.json();
    const results = data.results;
    if (results && results.length > 0) {
      const totalPrice = results.reduce((sum, item) => sum + parseFloat(item.price.value), 0);
      return totalPrice / results.length;
    }
    return null;
  } catch (error) {
    console.error(`Error fetching prices for ${getProductLabel(product)}:`, error.message);
    return null;
  }
}

async function runPricingForProducts(productList) {
  for (let product of productList) {
    const avgPrice = await fetchAIPrice(product);
    if (avgPrice !== null) {
      product.price = avgPrice.toFixed(2);
    }
  }
  await saveProductsToStorage(productList);
  return productList;
}

// --- SECTION C: AI PHOTO FETCHING ---

function buildSearchQuery(productName, view, specifications) {
  let query = `${productName} ${view}`;
  if (specifications) {
    if (Array.isArray(specifications)) {
      query += ' ' + specifications.join(' ');
    } else if (typeof specifications === 'object') {
      for (const [key, value] of Object.entries(specifications)) {
        query += ` ${value}`;
      }
    }
  }
  return query.trim();
}

async function fetchImagesFromPixabay(query) {
  const PIXABAY_API_KEY = localStorage.getItem('pixabay_key') || '';
  if (!PIXABAY_API_KEY) return [];
  const url = `https://pixabay.com/api/?key=${PIXABAY_API_KEY}&q=${encodeURIComponent(query)}&image_type=photo&orientation=horizontal`;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Pixabay API error: ${response.status}`);
    const data = await response.json();
    return data.hits.map(image => image.webformatURL);
  } catch (error) {
    console.error('Error fetching images from Pixabay:', error.message);
    return [];
  }
}

async function fetchImagesFromGoogle(query) {
  const GOOGLE_API_KEY = localStorage.getItem('google_api_key') || '';
  const GOOGLE_CX = localStorage.getItem('google_cx') || '';
  if (!GOOGLE_API_KEY || !GOOGLE_CX) return [];
  const url = `https://www.googleapis.com/customsearch/v1?q=${encodeURIComponent(query)}&searchType=image&key=${GOOGLE_API_KEY}&cx=${GOOGLE_CX}`;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Google API error: ${response.status}`);
    const data = await response.json();
    if (data.items) {
      return data.items.map(item => item.link);
    }
    return [];
  } catch (error) {
    console.error('Error fetching images from Google:', error.message);
    return [];
  }
}

async function fetchProductImages(productName, view, specifications) {
  const query = buildSearchQuery(productName, view, specifications);
  try {
    const [pixabayImages, googleImages] = await Promise.all([
      fetchImagesFromPixabay(query),
      fetchImagesFromGoogle(query)
    ]);
    return [...new Set([...pixabayImages, ...googleImages])];
  } catch (err) {
    console.error('fetchProductImages error:', err);
    return [];
  }
}

async function runPhotosForProducts(productList) {
  const views = ['front view', 'left side view', 'right side view', 'inside view', 'back view'];
  for (const product of productList) {
    const photoUrls = [];
    for (const view of views) {
      const imageUrls = await fetchProductImages(getProductLabel(product), view, product.specifications);
      if (imageUrls.length > 0) {
        photoUrls.push(imageUrls[0]);
      }
    }
    if (photoUrls.length > 0) {
      product.photo_paths = photoUrls;
      product.images = photoUrls;
    }
  }
  await saveProductsToStorage(productList);
  return productList;
}

// --- SECTION D: AI DESCRIPTION + SPECIFICATIONS ---

async function fetchAIDescription(product) {
  try {
    const response = await fetch('https://clblast.up.railway.app/ai/anthropic-proxy', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Anthropic-Key': localStorage.getItem('anthropic_key') || ''
      },
      body: JSON.stringify({
        messages: [{
          role: "user",
          content: "Write a 3-sentence product description for: " + getProductLabel(product) +
                   ". Category: " + (product.category || "General") +
                   ". Current description: " + (product.description || "none") +
                   ". Return only the description text, no labels or intro."
        }],
        max_tokens: 300
      })
    });
    if (!response.ok) {
      const errData = await response.json().catch(() => ({}));
      const errMsg = errData.error?.message || `HTTP ${response.status}`;
      throw new Error(`Anthropic API error: ${errMsg}`);
    }
    const data = await response.json();
    if (data.error) throw new Error(data.error.message || 'Anthropic API error');
    if (data.content && data.content.length > 0) {
      return data.content[0].text;
    }
    return null;
  } catch (error) {
    console.error('Error fetching AI description:', error);
    return null;
  }
}

async function fetchAISpecifications(product) {
  try {
    const response = await fetch('https://clblast.up.railway.app/ai/anthropic-proxy', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Anthropic-Key': localStorage.getItem('anthropic_key') || ''
      },
      body: JSON.stringify({
        messages: [{
          role: "user",
          content: "List 5 key product specifications for: " + getProductLabel(product) +
                   ". Return ONLY a JSON array of strings like [\"Spec 1\", \"Spec 2\", ...] with no extra text, no markdown, no backticks."
        }],
        max_tokens: 300
      })
    });
    if (!response.ok) {
      const errData = await response.json().catch(() => ({}));
      const errMsg = errData.error?.message || `HTTP ${response.status}`;
      throw new Error(`Anthropic API error: ${errMsg}`);
    }
    const data = await response.json();
    if (data.error) throw new Error(data.error.message || 'Anthropic API error');
    if (data.content && data.content.length > 0) {
      try {
        const text = data.content[0].text.trim();
        // Sometimes LLMs return markdown block even when told not to.
        const cleanText = text.replace(/```json/g, '').replace(/```/g, '').trim();
        return JSON.parse(cleanText);
      } catch (parseError) {
        console.error('Error parsing AI specifications:', parseError);
        return null;
      }
    }
    return null;
  } catch (error) {
    console.error('Error fetching AI specifications:', error);
    return null;
  }
}

async function runDescriptionsForProducts(productList) {
  for (const product of productList) {
    const desc = await fetchAIDescription(product);
    if (desc !== null) {
      product.description = desc;
    }
  }
  await saveProductsToStorage(productList);
  return productList;
}

async function runSpecificationsForProducts(productList) {
  for (const product of productList) {
    const specs = await fetchAISpecifications(product);
    if (specs !== null) {
      product.specifications = specs;
    }
  }
  await saveProductsToStorage(productList);
  return productList;
}

// --- SECTION E: RUN ALL ---

async function runAllAIForProducts(productList) {
  console.log("Running Pricing...");
  await runPricingForProducts(productList);
  console.log("Running Photos...");
  await runPhotosForProducts(productList);
  console.log("Running Descriptions...");
  await runDescriptionsForProducts(productList);
  console.log("Running Specifications...");
  await runSpecificationsForProducts(productList);
  return productList;
}

// --- SECTION F: TABLE VIEW / UI RENDERING ---

function renderProductsTable() {
  const tbody = document.querySelector('#products-table tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  const productsList = window.allProducts || window.products || [];

  productsList.forEach(product => {
    const tr = document.createElement('tr');

    // Name
    const nameTd = document.createElement('td');
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.value = product.name || product.title || '';
    nameInput.onblur = () => {
      // Write to whichever field this product uses
      if ('title' in product) product.title = nameInput.value;
      else product.name = nameInput.value;
      saveProductsToStorage(productsList);
    };
    nameTd.appendChild(nameInput);
    tr.appendChild(nameTd);

    // Description
    const descTd = document.createElement('td');
    const descTextarea = document.createElement('textarea');
    descTextarea.value = product.description || '';
    descTextarea.onblur = () => {
      product.description = descTextarea.value;
      saveProductsToStorage(productsList);
    };
    descTd.appendChild(descTextarea);
    tr.appendChild(descTd);

    // Specifications
    const specsTd = document.createElement('td');
    specsTd.className = 'spec-cell';
    const specsList = Array.isArray(product.specifications) ? product.specifications : [];
    
    specsList.forEach((spec, idx) => {
      const specInput = document.createElement('input');
      specInput.type = 'text';
      specInput.value = spec;
      specInput.style.marginBottom = '5px';
      specInput.onblur = () => {
        product.specifications[idx] = specInput.value;
        saveProductsToStorage(productsList);
      };
      specsTd.appendChild(specInput);
      specsTd.appendChild(document.createElement('br'));
    });

    const addSpecBtn = document.createElement('button');
    addSpecBtn.textContent = "Add Spec";
    addSpecBtn.className = "btn-secondary";
    addSpecBtn.style.padding = "4px 8px";
    addSpecBtn.onclick = () => {
      if (!Array.isArray(product.specifications)) product.specifications = [];
      product.specifications.push('');
      saveProductsToStorage(productsList);
      renderProductsTable();
    };
    specsTd.appendChild(addSpecBtn);
    tr.appendChild(specsTd);

    // Actions
    const actionsTd = document.createElement('td');
    const removeBtn = document.createElement('button');
    removeBtn.textContent = "Remove";
    removeBtn.className = "product-delete-btn";
    removeBtn.onclick = () => {
      const liveList = window.allProducts || window.products || [];
      const idx = liveList.indexOf(product);
      if (idx !== -1) {
        liveList.splice(idx, 1);
        saveProductsToStorage(liveList);
        renderProductsTable();
        if (typeof window.renderProducts === 'function') window.renderProducts();
      }
    };
    actionsTd.appendChild(removeBtn);
    tr.appendChild(actionsTd);

    tbody.appendChild(tr);
  });
}

// Expose functions globally
window.saveProductsToStorage = saveProductsToStorage;
window.loadProductsFromStorage = loadProductsFromStorage;
window.fetchAIPrice = fetchAIPrice;
window.runPricingForProducts = runPricingForProducts;
window.fetchImagesFromPixabay = fetchImagesFromPixabay;
window.fetchImagesFromGoogle = fetchImagesFromGoogle;
window.runPhotosForProducts = runPhotosForProducts;
window.fetchAIDescription = fetchAIDescription;
window.runDescriptionsForProducts = runDescriptionsForProducts;
window.fetchAISpecifications = fetchAISpecifications;
window.runSpecificationsForProducts = runSpecificationsForProducts;
window.runAllAIForProducts = runAllAIForProducts;
window.renderProductsTable = renderProductsTable;
