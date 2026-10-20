import requests
r = requests.get("http://127.0.0.1:8080/assets/DocumentsPage-C8H9gKl8.js", timeout=10)
print("status:", r.status_code, "size:", len(r.text))
# Check for the new code
print("has tocTree:", "tocTree" in r.text)
print("has isInfrastructureFile:", "isInfrastructureFile" in r.text)
print("has visibleSections:", "visibleSections" in r.text)
print("has metaSections:", "metaSections" in r.text)
print("has filterByVendor:", "filterByVendor" in r.text)
print("has meta-collapse:", "meta-collapse" in r.text)
