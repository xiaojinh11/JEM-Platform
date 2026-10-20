import requests
r = requests.get("http://127.0.0.1:8080/assets/DocumentsPage-C8H9gKl8.js", timeout=10)
print("status:", r.status_code, "size:", len(r.text))
print("profile.xml:", "profile.xml" in r.text)
print("navi.xml:", "navi.xml" in r.text)
print("hedex:", "hedex" in r.text)
print("crumb-link:", "crumb-link" in r.text)
print("meta-collapse:", "meta-collapse" in r.text)
print("toc-folder:", "toc-folder" in r.text)
