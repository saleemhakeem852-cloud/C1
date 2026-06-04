import sys

with open('clblast.html', 'r', encoding='utf-8') as f:
    html = f.read()

missing_functions = """
  function deleteProductFromPreview() {
    if (previewIdx > -1) {
      if (confirm('Delete this product?')) {
        allProducts.splice(previewIdx, 1);
        if (window.saveProductsToStorage) window.saveProductsToStorage(allProducts);
        renderProducts();
        closePreviewModal();
      }
    }
  }

  function editProductFromPreview() {
    closePreviewModal();
    // In a real app we'd open an edit form, but inline editing covers it
    showToast('Click fields on the card to edit inline', 'info');
  }

  function runAIFromPreview(action) {
    if (!action || previewIdx === -1) return;
    runAIAction(action, [previewIdx]);
    document.getElementById('preview-ai-select').value = '';
    closePreviewModal();
  }

  function selectAllSearchResults() {
    document.querySelectorAll('#search-results-content input[type="checkbox"]').forEach(cb => cb.checked = true);
  }

  function deselectAllSearchResults() {
    document.querySelectorAll('#search-results-content input[type="checkbox"]').forEach(cb => cb.checked = false);
  }

  function addSelectedToProducts() {
    showToast('Not connected to a live search API in this demo.', 'info');
    closeSearchResultsModal();
  }
"""

html = html.replace('// Modal logic', missing_functions + '\n  // Modal logic')

with open('clblast.html', 'w', encoding='utf-8') as f:
    f.write(html)
