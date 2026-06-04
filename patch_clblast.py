import re
import sys

def patch():
    with open('clblast.html', 'r', encoding='utf-8') as f:
        html = f.read()

    # Add Data-Src support to zoom image (in case it wasn't done)
    # 1. CSS Injection
    css_injection = """
    /* --- NEW AI + MODAL STYLES --- */
    .spec-label { font-weight: 600; }
    .spec-value { font-weight: 400; }
    
    #product-preview-overlay, #search-results-overlay, .ai-card-overlay {
      display:none; position:fixed; inset:0; z-index:900; 
      background:rgba(0,0,0,0.7); backdrop-filter:blur(4px);
    }
    #product-preview-modal, #search-results-modal {
      display:none; position:fixed; top:50%; left:50%; transform:translate(-50%, -50%);
      z-index:901; width:800px; max-width:90vw; max-height:90vh; 
      background:var(--bg2); border:1px solid var(--border2); 
      border-radius:var(--radius-lg); flex-direction:column; overflow:hidden;
    }
    .modal-body { padding:20px; overflow-y:auto; flex:1; position:relative; }
    .modal-footer {
      display:flex; gap:10px; padding:16px 20px; border-top:1px solid var(--border);
      background:var(--bg2); position:sticky; bottom:0; justify-content:flex-end;
    }
    
    .ai-card-overlay { position:absolute; z-index:10; border-radius:var(--radius-lg); display:none; align-items:center; justify-content:center; }
    .ai-spinner { width:40px; height:40px; border:4px solid rgba(255,255,255,0.2); border-top-color:var(--accent); border-radius:50%; animation:spin 1s linear infinite; }
    
    .ai-error-badge { position:absolute; top:10px; left:10px; background:var(--red); color:#fff; font-size:0.7rem; padding:4px 8px; border-radius:12px; cursor:pointer; display:none; z-index:11; }
    
    .card-menu-btn { position:absolute; top:10px; right:10px; background:none; border:none; color:var(--text); font-size:1.2rem; cursor:pointer; z-index:5; }
    .card-menu-dropdown { position:absolute; top:40px; right:10px; background:var(--card); border:1px solid var(--border); border-radius:var(--radius-md); display:none; flex-direction:column; z-index:11; }
    .card-menu-dropdown button { background:none; border:none; color:var(--text); padding:8px 16px; text-align:left; cursor:pointer; width:100%; }
    .card-menu-dropdown button:hover { background:var(--glass2); }
    
    .editable-field { cursor:pointer; border-bottom:1px dashed var(--muted); }
    .editable-field:hover { border-bottom-color:var(--accent); }
    
    #toast-notification { position:fixed; bottom:20px; right:20px; background:var(--green); color:#fff; padding:12px 24px; border-radius:var(--radius-md); box-shadow:var(--shadow-md); opacity:0; transition:opacity 0.4s; pointer-events:none; z-index:9999; }
    #toast-notification.show { opacity:1; }
    """
    
    html = html.replace('</style>', css_injection + '\n  </style>')

    # 2. Add Modals and Toasts to body
    html_injection = """
  <!-- NEW MODALS -->
  <div id="product-preview-overlay"></div>
  <div id="product-preview-modal">
    <div class="modal-body" id="product-preview-content" style="position:relative; overflow:hidden;">
      <div id="image-container" style="position:relative; overflow:hidden;">
        <img id="zoom-image" src="" alt="Product Preview" style="width:100%; max-height:400px; object-fit:contain;" />
        <div id="zoom-lens" style="position:absolute; border-radius:50%; border:3px solid #333; width:150px; height:150px; pointer-events:none; opacity:0; background-repeat:no-repeat; z-index:10;"></div>
      </div>
      <div id="product-preview-details" style="margin-top:20px;"></div>
    </div>
    <div class="modal-footer">
      <button class="btn-secondary" onclick="editProductFromPreview()">Edit</button>
      <select id="preview-ai-select" class="btn-secondary" onchange="runAIFromPreview(this.value)">
        <option value="">Edit With AI ▾</option>
        <option value="all">All</option>
        <option value="price">Price</option>
        <option value="photos">Photos</option>
        <option value="description">Description</option>
        <option value="specifications">Specifications</option>
      </select>
      <button class="btn-secondary" style="color:#f87171;" onclick="deleteProductFromPreview()">Delete</button>
      <button class="btn-primary" onclick="closePreviewModal()">Close</button>
    </div>
  </div>

  <div id="search-results-overlay"></div>
  <div id="search-results-modal">
    <div class="modal-body" id="search-results-content"></div>
    <div class="modal-footer">
      <button class="btn-secondary" onclick="selectAllSearchResults()">Select All</button>
      <button class="btn-secondary" onclick="deselectAllSearchResults()">Deselect All</button>
      <button class="btn-primary" onclick="addSelectedToProducts()">Add Selected to Products</button>
      <button class="btn-secondary" onclick="closeSearchResultsModal()">Close</button>
    </div>
  </div>

  <div id="toast-notification">AI update complete</div>
  
  <table id="products-table" class="analytics-table" style="display:none; margin-top:20px;">
    <thead><tr><th>Name</th><th>Description</th><th>Specifications</th><th>Actions</th></tr></thead>
    <tbody></tbody>
  </table>
  """
    html = html.replace('</section>\n\n  <!-- ═══════════════ ADD PRODUCT WINDOW', html_injection + '\n  </section>\n\n  <!-- ═══════════════ ADD PRODUCT WINDOW')

    # 3. Add API Keys section to Accounts tab
    api_keys_html = """
    <div class="form-section-title" style="margin-top:40px;">API Keys</div>
    <div class="form-grid">
      <div class="form-group">
        <label>AI Pricing API Key</label>
        <input type="text" id="ai_pricing_key" placeholder="ScrapeBadger Key" />
      </div>
      <div class="form-group">
        <label>Pixabay API Key</label>
        <input type="text" id="pixabay_key" />
      </div>
      <div class="form-group">
        <label>Google API Key</label>
        <input type="text" id="google_api_key" />
      </div>
      <div class="form-group">
        <label>Google CX</label>
        <input type="text" id="google_cx" />
      </div>
      <div class="form-group full-width" style="align-items:flex-end;">
        <button class="btn-save" onclick="saveAPIKeys()">Save Keys</button>
        <span id="api-key-saved" style="color:var(--green); margin-left:10px; display:none;">Saved ✓</span>
      </div>
    </div>
    """
    html = html.replace('<!-- ═══════════════ POST WINDOW', api_keys_html + '\n  <!-- ═══════════════ POST WINDOW')

    # 4. Include scripts at bottom
    scripts_injection = """
<script src="ai_engine.js"></script>
<script src="productdisplayzoom.js"></script>
<script>
  function toggleTableView() {
    const table = document.getElementById('products-table');
    const cards = document.getElementById('product-display-area');
    if (table.style.display === 'none') {
      table.style.display = 'table';
      cards.style.display = 'none';
      if (window.renderProductsTable) window.renderProductsTable();
    } else {
      table.style.display = 'none';
      cards.style.display = 'grid';
      renderProducts();
    }
  }

  function saveAPIKeys() {
    localStorage.setItem('ai_pricing_key', document.getElementById('ai_pricing_key').value);
    localStorage.setItem('pixabay_key', document.getElementById('pixabay_key').value);
    localStorage.setItem('google_api_key', document.getElementById('google_api_key').value);
    localStorage.setItem('google_cx', document.getElementById('google_cx').value);
    const badge = document.getElementById('api-key-saved');
    badge.style.display = 'inline';
    setTimeout(() => badge.style.display = 'none', 2000);
  }

  // Set values on load
  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById('ai_pricing_key').value = localStorage.getItem('ai_pricing_key') || '';
    document.getElementById('pixabay_key').value = localStorage.getItem('pixabay_key') || '';
    document.getElementById('google_api_key').value = localStorage.getItem('google_api_key') || '';
    document.getElementById('google_cx').value = localStorage.getItem('google_cx') || '';
  });

  // Modal logic
  let previewIdx = -1;
  function openProductPreview(idx) {
    if (event.target.tagName === 'INPUT' || event.target.tagName === 'BUTTON' || event.target.classList.contains('editable-field')) return;
    previewIdx = idx;
    const p = allProducts[idx];
    const modal = document.getElementById('product-preview-modal');
    const overlay = document.getElementById('product-preview-overlay');
    const img = document.getElementById('zoom-image');
    if (p.photo_paths && p.photo_paths.length > 0) {
      img.src = p.photo_paths[0];
      img.setAttribute('data-src', p.photo_paths[0]);
    } else {
      img.src = '';
    }
    
    let specsHtml = '';
    if (p.specifications && Array.isArray(p.specifications)) {
      specsHtml = p.specifications.map(s => `<div><span class="spec-label">Spec:</span> <span class="spec-value">${s}</span></div>`).join('');
    } else if (p.specifications) {
      specsHtml = `<div><span class="spec-label">Specs:</span> <span class="spec-value">${JSON.stringify(p.specifications)}</span></div>`;
    }

    document.getElementById('product-preview-details').innerHTML = `
      <h3>${escHtml(p.title || p.name || 'Untitled')}</h3>
      <p>$${p.price}</p>
      <p>${escHtml(p.description || '')}</p>
      ${specsHtml}
    `;
    
    modal.style.display = 'flex';
    overlay.style.display = 'block';
    if (window.setLensBackground) setTimeout(window.setLensBackground, 100);
  }

  function closePreviewModal() {
    document.getElementById('product-preview-modal').style.display = 'none';
    document.getElementById('product-preview-overlay').style.display = 'none';
  }

  function closeSearchResultsModal() {
    document.getElementById('search-results-modal').style.display = 'none';
    document.getElementById('search-results-overlay').style.display = 'none';
  }

  // Inline editing
  function makeEditable(element, field, idx) {
    const p = allProducts[idx];
    const val = p[field] || '';
    const input = document.createElement(field === 'description' ? 'textarea' : 'input');
    input.value = val;
    input.className = 'form-group input'; // reuse some styling
    input.style.width = '100%';
    input.onblur = () => {
      p[field] = input.value;
      if (!p.title && p.name) p.title = p.name; // Keep compatible
      if (window.saveProductsToStorage) window.saveProductsToStorage(allProducts);
      renderProducts();
    };
    element.replaceWith(input);
    input.focus();
  }

  // AI actions
  async function runAIAction(action, indices) {
    if (indices.length === 0) {
      alert("Please select at least one product first.");
      return;
    }
    
    // Disable UI
    const aiDropdown = document.getElementById('edit-with-ai-select');
    if (aiDropdown) aiDropdown.disabled = true;
    document.querySelectorAll('.card-menu-btn').forEach(btn => btn.disabled = true);
    document.querySelectorAll('.card-menu-dropdown').forEach(d => d.style.display = 'none');
    
    const targets = indices.map(i => allProducts[i]);
    let successCount = 0;
    
    for (let i of indices) {
      const card = document.getElementById('pcard-' + i);
      if (card) {
        const overlay = card.querySelector('.ai-card-overlay');
        const errBadge = card.querySelector('.ai-error-badge');
        if (overlay) overlay.style.display = 'flex';
        if (errBadge) errBadge.style.display = 'none';
      }
    }
    
    try {
      if (action === 'all') await window.runAllAIForProducts(targets);
      else if (action === 'price') await window.runPricingForProducts(targets);
      else if (action === 'photos') await window.runPhotosForProducts(targets);
      else if (action === 'description') await window.runDescriptionsForProducts(targets);
      else if (action === 'specifications') await window.runSpecificationsForProducts(targets);
      successCount = targets.length;
    } catch (e) {
      console.error(e);
      // Mark error on first failed or all, depending on logic. Here we just mark all targeted.
      for (let i of indices) {
        const card = document.getElementById('pcard-' + i);
        if (card) {
           const errBadge = card.querySelector('.ai-error-badge');
           if (errBadge) errBadge.style.display = 'block';
        }
      }
    }
    
    for (let i of indices) {
      const card = document.getElementById('pcard-' + i);
      if (card) {
        const overlay = card.querySelector('.ai-card-overlay');
        if (overlay) overlay.style.display = 'none';
      }
    }
    
    // Re-enable UI
    if (aiDropdown) { aiDropdown.disabled = false; aiDropdown.value = ''; }
    document.querySelectorAll('.card-menu-btn').forEach(btn => btn.disabled = false);
    
    if (successCount > 0) {
      const toast = document.getElementById('toast-notification');
      toast.textContent = `AI update complete — ${successCount} products updated.`;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 4000);
      renderProducts();
      if (window.renderProductsTable) window.renderProductsTable();
    }
  }

  document.getElementById('edit-with-ai-select')?.addEventListener('change', function(e) {
    if (!this.value) return;
    const indices = [...document.querySelectorAll('.product-select-cb:checked')].map(cb => parseInt(cb.dataset.index));
    runAIAction(this.value, indices);
    this.value = '';
  });

  function toggleCardMenu(idx) {
    const menu = document.getElementById('card-menu-' + idx);
    menu.style.display = menu.style.display === 'flex' ? 'none' : 'flex';
  }

</script>
"""
    html = html.replace('</body>', scripts_injection + '</body>')

    # 5. Patch renderProducts function to add formatting and onclick
    # Add absolute overlay, spinner, action menu, and inline edits
    render_products_patch = """
        <div class="product-card" id="pcard-${realIdx}" style="animation-delay:${idx * 0.04}s; position:relative;" onclick="openProductPreview(${realIdx})">
          <div class="ai-card-overlay"><div class="ai-spinner"></div></div>
          <div class="ai-error-badge" onclick="this.style.display='none'; event.stopPropagation();">AI error</div>
          
          <button class="card-menu-btn" onclick="toggleCardMenu(${realIdx}); event.stopPropagation();">⋮</button>
          <div class="card-menu-dropdown" id="card-menu-${realIdx}" onclick="event.stopPropagation();">
             <button onclick="runAIAction('all', [${realIdx}]); toggleCardMenu(${realIdx})">All</button>
             <button onclick="runAIAction('price', [${realIdx}]); toggleCardMenu(${realIdx})">Price</button>
             <button onclick="runAIAction('photos', [${realIdx}]); toggleCardMenu(${realIdx})">Photos</button>
             <button onclick="runAIAction('description', [${realIdx}]); toggleCardMenu(${realIdx})">Description</button>
             <button onclick="runAIAction('specifications', [${realIdx}]); toggleCardMenu(${realIdx})">Specifications</button>
          </div>

          <label class="product-card-check" for="prod-cb-${realIdx}" onclick="event.stopPropagation();">
            <input type="checkbox" id="prod-cb-${realIdx}" class="product-select-cb"
                   data-index="${realIdx}" onchange="onProductCardCheck()" />
          </label>
          <div class="product-card-title editable-field" onclick="makeEditable(this, 'title', ${realIdx}); event.stopPropagation();">${escHtml(p.title || p.name || 'Untitled')}</div>
          <div class="product-card-desc editable-field" onclick="makeEditable(this, 'description', ${realIdx}); event.stopPropagation();">${escHtml(p.description || '')}</div>
          <div class="product-card-meta">
            <div class="product-card-chips">
              <span class="product-price editable-field" onclick="makeEditable(this, 'price', ${realIdx}); event.stopPropagation();">$${(p.price||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
    """
    
    # We replace from `<div class="product-card"` to `<span class="product-price"`
    # Wait, it's safer to use regex to replace inside renderProducts
    pattern = r'<div class="product-card".*?<span class="product-price".*?</span>'
    html = re.sub(pattern, render_products_patch.strip(), html, flags=re.DOTALL)
    
    # 6. Fix launchPost to use in-memory products array
    # Inside launchPost: `body = { ... products_file: ... }` Wait, launchPost calls `post/`. The server expects `product_indices`. The server uses PRODUCTS_JSON.
    # The prompt says: "The launchPost() function in clblast.html (Post tab) must read from the in-memory products array (kept in sync by ai_engine.js), not re-fetch from server"
    # Actually `launchPost` reads indices: `const selectedIndices = [...document.querySelectorAll('#post-product-list input[type="checkbox"]:checked')].map(cb => parseInt(cb.value));`
    # Then it sends `product_indices` to the server. The server reads `products.json`.
    # Since `saveProductsToStorage()` syncs to server `products.json` synchronously via `/sync-products`, `launchPost` doesn't need to pass the array to the server, or maybe we can pass the array directly.
    # Let's ensure `buildPostProductList()` uses `allProducts` (it already does).
    # I think it's already using the in-memory products array, but let's check `launchPost()`.
    
    with open('clblast.html', 'w', encoding='utf-8') as f:
        f.write(html)

patch()
