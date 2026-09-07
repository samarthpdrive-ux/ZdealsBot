import os
import uuid
import requests

API_KEY = "AK_1Y2uVrEBMlhK7bkLD4AR_pZeAVoM-3E6Vp2R9TaZmhA"

response = requests.post(
    "https://zdeals-reseller-api.wasmer.app/api/reseller?action=order",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    json={
        "product_id": "330001",
        "quantity": 1,
        "external_order_id": f"pycharm-{uuid.uuid4().hex}",
    },
    timeout=30,
)

print(response.status_code)
print(response.json())