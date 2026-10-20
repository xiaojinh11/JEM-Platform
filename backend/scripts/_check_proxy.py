import requests, json
r = requests.post("http://127.0.0.1:8080/api/v1/auth/login",
                  json={"username":"admin","password":"admin1234"},
                  timeout=10)
print("Login via 8080:", r.status_code, r.text[:120])

tok = r.json()["access_token"]
r = requests.get("http://127.0.0.1:8080/api/v1/documents",
                 headers={"Authorization": f"Bearer {tok}"}, timeout=10)
print("List via 8080:", r.status_code, "count:", len(r.json()))

r = requests.get("http://127.0.0.1:8080/documents/7", timeout=10)
print("SPA fallback /documents/7:", r.status_code, "len:", len(r.text))
