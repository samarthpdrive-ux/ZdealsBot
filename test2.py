import requests

API_URL = "http://127.0.0.1:8081/api/reseller"
API_KEY = "AK__fKpvT2_4g5ILsrXys_Ig2gjQ6t7bQGxdpdFWLPHkqw"

headers = {
    "Authorization": f"Bearer {API_KEY}",
}

health = requests.get(
    "http://127.0.0.1:8081/health",
    timeout=10,
)

print("HEALTH:", health.status_code)
print(health.text)

products = requests.get(
    f"{API_URL}?action=products",
    headers=headers,
    timeout=60,
)

print("\nPRODUCTS:", products.status_code)
print(products.text)