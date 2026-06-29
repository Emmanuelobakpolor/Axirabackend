import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from django.conf import settings

_BASE_AUTH = "https://auth.reloadly.com"
_token_lock = threading.Lock()
_access_token: str | None = None
_token_expires: float = 0.0


class ReloadlyError(Exception):
    pass


def _api_base() -> str:
    return (
        "https://giftcards-sandbox.reloadly.com"
        if settings.RELOADLY_SANDBOX
        else "https://giftcards.reloadly.com"
    )


def _get_token() -> str:
    global _access_token, _token_expires
    with _token_lock:
        if _access_token and time.time() < _token_expires - 60:
            return _access_token

        audience = _api_base()
        payload = json.dumps({
            "client_id": settings.RELOADLY_CLIENT_ID,
            "client_secret": settings.RELOADLY_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "audience": audience,
        }).encode()
        req = urllib.request.Request(
            f"{_BASE_AUTH}/oauth/token",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                body = json.loads(res.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                msg = body.get("error_description", "Reloadly auth failed")
            except Exception:
                msg = f"Reloadly auth failed (HTTP {e.code})"
            raise ReloadlyError(msg)

        _access_token = body["access_token"]
        _token_expires = time.time() + body.get("expires_in", 3600)
        return _access_token


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    token = _get_token()
    url = _api_base() + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/com.reloadly.giftcards-v1+json",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            msg = (
                body.get("errorMessage")
                or body.get("message")
                or f"Reloadly error (HTTP {e.code})"
            )
        except Exception:
            msg = f"Reloadly error (HTTP {e.code})"
        raise ReloadlyError(msg)


def get_products(brand_name: str, country_code: str) -> list:
    params = urlencode({
        "productName": brand_name,
        "countryCode": country_code.upper(),
        "size": 50,
        "page": 1,
    })
    result = _call("GET", f"/products?{params}")
    return result.get("content", [])


def get_brands(country_code: str) -> list:
    params = urlencode({
        "countryCode": country_code.upper(),
        "size": 200,
        "page": 1,
    })
    result = _call("GET", f"/products?{params}")
    products = result.get("content", [])

    seen: set[int] = set()
    brands = []
    for p in products:
        brand_info = p.get("brand") or {}
        brand_id = brand_info.get("brandId") or p.get("productId")
        brand_name = brand_info.get("brandName") or p.get("productName", "")
        logo_urls = p.get("logoUrls") or []
        logo_url = logo_urls[0] if logo_urls else ""

        if brand_id and brand_id not in seen:
            seen.add(brand_id)
            brands.append({
                "brand_id": brand_id,
                "brand_name": brand_name,
                "logo_url": logo_url,
            })

    return sorted(brands, key=lambda b: b["brand_name"])


def order_gift_card(
    product_id: int,
    unit_price: float,
    sender_name: str,
    recipient_email: str,
    custom_ref: str,
) -> dict:
    payload = {
        "productId": product_id,
        "quantity": 1,
        "unitPrice": unit_price,
        "customIdentifier": custom_ref,
        "senderName": sender_name,
        "recipientEmail": recipient_email,
        "preOrder": False,
    }
    return _call("POST", "/orders", payload=payload)
