import sys

with open('clblast.html', 'r', encoding='utf-8') as f:
    html = f.read()

html = html.replace('loadProducts().then(buildPostProductList);', 'buildPostProductList();')

with open('clblast.html', 'w', encoding='utf-8') as f:
    f.write(html)
