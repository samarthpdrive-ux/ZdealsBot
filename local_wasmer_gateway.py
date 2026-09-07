import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests


PROJECT_DIR = Path(r"E:\nomanresell2")
ENV_FILE = PROJECT_DIR / ".env"

BOT_URL = "http://127.0.0.1:10000"


def get_env_value(name):
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")

    return ""


INTERNAL_SECRET = get_env_value("INTERNAL_API_SECRET")

if not INTERNAL_SECRET:
    raise RuntimeError("INTERNAL_API_SECRET is missing from .env")


class Gateway(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)

        if parsed.path == "/health":
            self.send_json(200, {
                "status": "ok",
                "service": "Local Wasmer reseller API",
            })
            return

        if parsed.path != "/api/reseller":
            self.send_json(404, {"error": "not_found"})
            return

        api_key = self.headers.get("Authorization", "").replace("Bearer ", "").strip()

        if not api_key:
            self.send_json(401, {
                "error": "missing_api_key"
            })
            return

        action = parse_qs(parsed.query).get("action", [""])[0]

        routes = {
            "products": "/internal/v1/products",
            "balance": "/internal/v1/me",
            "orders": "/internal/v1/orders",
        }

        if action not in routes:
            self.send_json(404, {
                "error": "invalid_action"
            })
            return

        response = requests.get(
            BOT_URL + routes[action],
            headers={
                "X-API-Key": api_key,
                "X-Internal-Bot-Secret": INTERNAL_SECRET,
            },
            timeout=60,
        )

        self.send_json(
            response.status_code,
            response.json(),
        )

    def do_POST(self):
        parsed = urlsplit(self.path)

        if parsed.path != "/api/reseller":
            self.send_json(404, {"error": "not_found"})
            return

        api_key = self.headers.get("Authorization", "").replace("Bearer ", "").strip()

        if not api_key:
            self.send_json(401, {
                "error": "missing_api_key"
            })
            return

        action = parse_qs(parsed.query).get("action", [""])[0]

        if action != "order":
            self.send_json(404, {
                "error": "invalid_action"
            })
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))

            bot_order = {
                "service_id": int(data["product_id"]),
                "quantity": int(data.get("quantity", 1)),
                "client_order_id": str(data["external_order_id"]),
            }

            if data.get("delivery_telegram_id"):
                bot_order["delivery_telegram_id"] = int(
                    data["delivery_telegram_id"]
                )

        except Exception:
            self.send_json(400, {
                "error": "invalid_order",
                "message": "Send product_id, quantity, and external_order_id.",
            })
            return

        response = requests.post(
            BOT_URL + "/internal/v1/order",
            headers={
                "X-API-Key": api_key,
                "X-Internal-Bot-Secret": INTERNAL_SECRET,
            },
            json=bot_order,
            timeout=60,
        )

        self.send_json(
            response.status_code,
            response.json(),
        )


print("Local public reseller API started")
print("http://127.0.0.1:8081/api/reseller?action=products")

ThreadingHTTPServer(
    ("127.0.0.1", 8081),
    Gateway,
).serve_forever()