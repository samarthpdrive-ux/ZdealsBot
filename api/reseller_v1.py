"""Private delivery bridge for the public Wasmer reseller API.

Only the Wasmer gateway may call these endpoints.  API keys identify a
Telegram user, while ``X-Internal-Bot-Secret`` proves the request came from
the gateway.  Every order uses the bot's live balance, custom rates, stock
checks, and existing delivery logic.
"""

import asyncio
import hmac
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from config import API_RATE_LIMIT_PER_SECOND, INTERNAL_API_SECRET
from database import SessionLocal
from models.api_key import ApiKey, ApiOrder
from models.order import Order
from models.product import Product
from models.user import User
from services.api_keys import ApiKeyConfigurationError, authenticate_api_key

# Reuse the same financial and stock-safe checkout implementation as Telegram.
from handlers.products import _active_custom_price, _do_purchase, _do_reseller_purchase, _money, _real_stock


async def require_wasmer_gateway(
    x_internal_bot_secret: str | None = Header(default=None, alias="X-Internal-Bot-Secret"),
) -> None:
    """Reject every direct request to the bot's delivery bridge.

    A 404 is intentional for an incorrect or absent secret so the hidden
    bridge is not advertised as a public API endpoint.
    """
    if not INTERNAL_API_SECRET:
        raise HTTPException(status_code=503, detail="Internal API is not configured.")
    if not x_internal_bot_secret or not hmac.compare_digest(x_internal_bot_secret, INTERNAL_API_SECRET):
        raise HTTPException(status_code=404, detail="Not found")


router = APIRouter(
    prefix="/internal/v1",
    tags=["Internal Wasmer gateway"],
    dependencies=[Depends(require_wasmer_gateway)],
    include_in_schema=False,
)

_client_order_id = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_rate_windows: dict[int, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()


@dataclass(frozen=True)
class ApiPrincipal:
    key_id: int
    telegram_id: int


class OrderRequest(BaseModel):
    service_id: int = Field(description="Product ID returned by GET /api/v1/products", gt=0)
    quantity: int = Field(default=1, ge=1, le=100)
    # Required to make retrying safe. Send the same value again after a timeout.
    client_order_id: str = Field(min_length=1, max_length=80)


def _api_error(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"error": code, "message": message})


async def _limit_key(key_id: int) -> None:
    """Small in-process guard. Hosting/WAF can add a global rate limit later."""
    now = time.monotonic()
    async with _rate_lock:
        window = _rate_windows[key_id]
        while window and now - window[0] >= 1:
            window.popleft()
        if len(window) >= API_RATE_LIMIT_PER_SECOND:
            raise _api_error("rate_limited", "Maximum 3 requests per second per API key.", 429)
        window.append(now)


def _load_principal(raw_key: str) -> ApiPrincipal | None:
    key = authenticate_api_key(raw_key)
    if not key:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == key.telegram_id).first()
        if not user or user.is_banned:
            return None
        return ApiPrincipal(key_id=key.id, telegram_id=key.telegram_id)
    finally:
        db.close()


async def api_principal(
    x_api_key: str = Header(..., alias="X-API-Key", description="Your reseller API key"),
) -> ApiPrincipal:
    try:
        principal = await asyncio.to_thread(_load_principal, x_api_key.strip())
    except ApiKeyConfigurationError:
        raise _api_error("server_configuration", "The API key service is not configured.", 503)
    if not principal:
        raise _api_error("invalid_api_key", "Missing, invalid, revoked, or blocked API key.", 401)
    await _limit_key(principal.key_id)
    return principal


def _decimal_text(value: Decimal | None) -> str:
    value = _money(value or Decimal("0"))
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _product_payload(product: Product, price: Decimal, stock: int) -> dict:
    return {
        "service_id": str(product.id),
        "name": product.name,
        "description": product.description or "",
        "category": product.category or "General",
        "price": _decimal_text(price),
        "currency": "USDT",
        "stock": stock,
        "preorder": bool(product.preorder),
        "delivery_type": product.delivery_type or "automatic",
    }


def _list_products(telegram_id: int) -> list[dict]:
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.is_active == True).order_by(Product.id.asc()).all()
        data = []
        for product in products:
            custom_price = _active_custom_price(db, telegram_id, product.id)
            price = custom_price if custom_price is not None else _money(product.price)
            data.append(_product_payload(product, price, _real_stock(product)))
        return data
    finally:
        db.close()


def _account_details(telegram_id: int) -> dict | None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user or user.is_banned:
            return None
        return {
            "telegram_id": user.telegram_id,
            "name": user.full_name,
            "wallet_balance": _decimal_text(user.balance),
            "currency": "USDT",
        }
    finally:
        db.close()


def _order_payload(order: Order) -> dict:
    delivered = [line for line in (order.delivered_account or "").splitlines() if line]
    return {
        "order_id": str(order.id),
        "service_id": str(order.product_id) if order.product_id is not None else None,
        "service": order.product_name,
        "quantity": order.quantity,
        "amount": _decimal_text(order.amount),
        "currency": "USDT",
        "status": order.status,
        "delivery_type": order.delivery_type,
        "delivered_products": delivered,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def _find_api_order(api_key_id: int, order_id: int) -> Order | None:
    db = SessionLocal()
    try:
        row = db.query(ApiOrder).filter(ApiOrder.api_key_id == api_key_id, ApiOrder.order_id == order_id).first()
        if not row:
            return None
        return db.get(Order, order_id)
    finally:
        db.close()


def _list_api_orders(api_key_id: int, offset: int, limit: int) -> tuple[int, list[Order]]:
    db = SessionLocal()
    try:
        query = db.query(Order).join(ApiOrder, ApiOrder.order_id == Order.id).filter(ApiOrder.api_key_id == api_key_id)
        total = query.count()
        orders = query.order_by(Order.id.desc()).offset(offset).limit(limit).all()
        return total, orders
    finally:
        db.close()


def _reserve_order(api_key_id: int, client_order_id: str) -> tuple[ApiOrder, bool]:
    db = SessionLocal()
    try:
        existing = db.query(ApiOrder).filter(
            ApiOrder.api_key_id == api_key_id, ApiOrder.client_order_id == client_order_id
        ).first()
        if existing:
            return existing, False
        row = ApiOrder(api_key_id=api_key_id, client_order_id=client_order_id, status="processing")
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.query(ApiOrder).filter(
                ApiOrder.api_key_id == api_key_id, ApiOrder.client_order_id == client_order_id
            ).first()
            return row, False
        db.refresh(row)
        return row, True
    finally:
        db.close()


def _set_api_order_result(api_order_id: int, order_id: int | None, error_message: str | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(ApiOrder, api_order_id)
        if not row:
            return
        row.order_id = order_id
        row.status = "completed" if order_id else "failed"
        row.error_message = (error_message or "")[:500] or None
        db.commit()
    finally:
        db.close()


def _product_source(product_id: int) -> str | None:
    db = SessionLocal()
    try:
        product = db.get(Product, product_id)
        return (product.source or "own") if product and product.is_active else None
    finally:
        db.close()


@router.get("/me")
async def get_me(principal: ApiPrincipal = Depends(api_principal)):
    account = await asyncio.to_thread(_account_details, principal.telegram_id)
    if not account:
        raise _api_error("account_unavailable", "The API account is unavailable.", 403)
    return account


@router.get("/products")
async def get_products(principal: ApiPrincipal = Depends(api_principal)):
    products = await asyncio.to_thread(_list_products, principal.telegram_id)
    return {"success": True, "services": products}


@router.post("/order", status_code=status.HTTP_200_OK)
async def create_order(payload: OrderRequest, principal: ApiPrincipal = Depends(api_principal)):
    if not _client_order_id.fullmatch(payload.client_order_id):
        raise _api_error("invalid_client_order_id", "Use only letters, numbers, dot, underscore, dash, or colon.", 400)

    request_row, is_new = await asyncio.to_thread(_reserve_order, principal.key_id, payload.client_order_id)
    if not request_row:
        raise _api_error("order_reservation_failed", "Could not reserve this order. Retry with the same client_order_id.", 503)
    if not is_new:
        if request_row.order_id:
            order = await asyncio.to_thread(_find_api_order, principal.key_id, request_row.order_id)
            if order:
                return {"success": True, "idempotent_replay": True, "order": _order_payload(order)}
        if request_row.status == "processing":
            raise _api_error("order_processing", "This client_order_id is already being processed. Retry shortly with the same ID.", 409)
        raise _api_error("previous_order_failed", request_row.error_message or "This client_order_id previously failed. Use a new ID after fixing the issue.", 409)

    source = await asyncio.to_thread(_product_source, payload.service_id)
    if not source:
        await asyncio.to_thread(_set_api_order_result, request_row.id, None, "Product not found or unavailable.")
        raise _api_error("product_unavailable", "Product not found or unavailable.", 404)

    if source == "reseller":
        result = await _do_reseller_purchase(principal.telegram_id, payload.service_id, payload.quantity)
    else:
        result = await asyncio.to_thread(_do_purchase, principal.telegram_id, payload.service_id, payload.quantity)

    if result.get("error"):
        message = str(result["error"])
        await asyncio.to_thread(_set_api_order_result, request_row.id, None, message)
        if message == "insufficient_balance":
            raise _api_error("insufficient_balance", "Insufficient wallet balance.", 400)
        raise _api_error("order_failed", message, 400)

    order_id = int(result["order_id"])
    await asyncio.to_thread(_set_api_order_result, request_row.id, order_id)
    order = await asyncio.to_thread(_find_api_order, principal.key_id, order_id)
    return {"success": True, "idempotent_replay": False, "order": _order_payload(order) if order else {"order_id": str(order_id)}}


@router.get("/orders")
async def get_orders(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    principal: ApiPrincipal = Depends(api_principal),
):
    total, orders = await asyncio.to_thread(_list_api_orders, principal.key_id, (page - 1) * limit, limit)
    return {
        "success": True,
        "page": page,
        "limit": limit,
        "total_orders": total,
        "total_pages": (total + limit - 1) // limit,
        "orders": [_order_payload(order) for order in orders],
    }


@router.get("/order/{order_id}")
async def get_order(order_id: int, principal: ApiPrincipal = Depends(api_principal)):
    order = await asyncio.to_thread(_find_api_order, principal.key_id, order_id)
    if not order:
        raise _api_error("order_not_found", "No API order with that ID belongs to this API key.", 404)
    return {"success": True, "order": _order_payload(order)}
