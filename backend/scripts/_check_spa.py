import requests

# Test SPA fallback
r = requests.get("http://127.0.0.1:8080/documents/196")
print("Direct /documents/196:", r.status_code, "len:", len(r.text))
print("contains root div:", "<div id=\"app\"" in r.text or 'id="app"' in r.text)

# Test normal asset
r = requests.get("http://127.0.0.1:8080/assets/index-Cfa-JcyW.js")
print("Asset:", r.status_code, "len:", len(r.content))
