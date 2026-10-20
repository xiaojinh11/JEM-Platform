import requests

# Test the actual API endpoints
base = "http://127.0.0.1:18000"

# Login
r = requests.post(f"{base}/api/v1/auth/login", json={"username":"admin","password":"admin1234"})
print("Login:", r.status_code)
tok = r.json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}

# List documents
r = requests.get(f"{base}/api/v1/documents", headers=H)
print("List:", r.status_code, "count:", len(r.json()))
if r.status_code == 200:
    docs = r.json()
    if docs:
        first_id = docs[0]["id"]
        print("First doc id:", first_id, "title:", docs[0].get("title"))
        # Get detail
        r = requests.get(f"{base}/api/v1/documents/{first_id}", headers=H)
        print("Detail:", r.status_code, "size:", len(r.content))
        if r.status_code != 200:
            print("body:", r.text[:300])
