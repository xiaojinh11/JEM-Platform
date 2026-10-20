import requests, json
r = requests.post("http://127.0.0.1:18000/api/v1/auth/login", json={"username":"admin","password":"admin1234"}, timeout=10)
print("status:", r.status_code)
print("body:", r.text[:500])
