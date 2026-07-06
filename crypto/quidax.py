"""
Quidax Business API client (stdlib urllib — no extra dependencies).
Base URL: ``
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import logging
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

QUIDAX_BASE = 'https://openapi.quidax.io/exchange-open-api/api/v1'


class QuidaxError(Exception):
    """Custom exception for Quidax API errors."""
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _get_secret_key():
    """Ensure secret key is configured."""
    key = getattr(settings, 'QUIDAX_SECRET_KEY', None)
    if not key:
        raise QuidaxError("QUIDAX_SECRET_KEY is not set in Django settings. "
                         "Add it to your .env or settings.py")
    return key


def _headers():
    return {
        'Authorization': f'Bearer {_get_secret_key()}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (compatible; Axira/1.0)',
    }


def _call(method: str, path: str, payload: dict = None, max_retries: int = 3):
    """
    Internal method to make HTTP requests to Quidax API with retry logic.
    """
    url = QUIDAX_BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    last_err = None

    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                body = json.loads(res.read().decode('utf-8'))

            if body.get('status') != 'success':
                error_msg = body.get('message', 'Unknown Quidax error')
                logger.error(f"Quidax API error: {error_msg}")
                raise QuidaxError(error_msg, retryable=False)

            logger.info(f"Quidax {method} {path} succeeded")
            return body.get('data', {})

        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode('utf-8')
                body = json.loads(raw) if raw else {}
                data = body.get('data') if isinstance(body.get('data'), dict) else {}
                msg = (
                    body.get('message')
                    or data.get('message')
                    or (f'HTTP {e.code}' if e.code != 404 else f'Endpoint not found: {path}')
                )
            except Exception:
                msg = f'HTTP {e.code}: {e.reason or "Unknown error"}'
            retryable = e.code in (429, 500, 502, 503, 504)
            last_err = QuidaxError(msg, retryable=retryable)
            logger.warning(f"Quidax HTTPError {e.code} {method} {path}: {msg}")

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = QuidaxError(f'Network error: {e}', retryable=True)
            logger.warning(f"Quidax network error on attempt {attempt+1}: {e}")

        if last_err and not last_err.retryable:
            raise last_err

        if attempt < max_retries - 1:
            sleep_time = min(2 ** attempt, 8)
            time.sleep(sleep_time)

    raise last_err or QuidaxError("Max retries exceeded")


# ── Market Data ─────────────────────────────────────────────────────────────

def get_all_tickers() -> dict:
    """Returns dict keyed by market symbol e.g. {'btcngn': {...}}"""
    return _call('GET', '/markets/tickers')


def get_ticker(market: str) -> dict:
    """Returns ticker data for one market."""
    return _call('GET', f'/markets/tickers/{market}')


# ── Sub-account Management ──────────────────────────────────────────────────

def create_sub_account(email: str, first_name: str, last_name: str, phone_number: str = None) -> dict:
    """
    Create a Quidax sub-account.
    Returns the sub-account data (including 'id' to store in your User model).
    """
    payload = {
        'email': email,
        'first_name': first_name,
        'last_name': last_name,
    }
    if phone_number:
        payload['phone_number'] = phone_number

    return _call('POST', '/users', payload)


def get_sub_account(uid: str) -> dict:
    """Fetch a sub-account by its Quidax UID."""
    return _call('GET', f'/users/{uid}')


# ── Deposit Addresses ───────────────────────────────────────────────────────

def list_deposit_addresses(uid: str, currency: str) -> list:
    """List payment addresses already generated for a currency (may be empty)."""
    result = _call('GET', f'/users/{uid}/wallets/{currency.lower()}/addresses')
    if isinstance(result, list):
        return result
    return [result] if result else []


def create_deposit_address(uid: str, currency: str, network: str = None) -> dict:
    """
    Trigger generation of a new payment address for a currency. Quidax creates
    it asynchronously (delivered via the wallet.address.generated webhook) —
    callers should re-poll list_deposit_addresses() shortly after calling this.
    """
    qs = f'?{urllib.parse.urlencode({"network": network})}' if network else ''
    return _call('POST', f'/users/{uid}/wallets/{currency.lower()}/addresses{qs}')


# ── Wallet Balances ─────────────────────────────────────────────────────────

def get_wallets(uid: str) -> list:
    """Get all wallet balances for a sub-account."""
    return _call('GET', f'/users/{uid}/wallets')


def get_wallet(uid: str, currency: str) -> dict:
    """Get single wallet balance."""
    return _call('GET', f'/users/{uid}/wallets/{currency.lower()}')


# ── Trade Execution ─────────────────────────────────────────────────────────

def _plain_decimal_str(value) -> str:
    """
    Strip insignificant trailing zeros without falling back to exponential
    notation (Decimal.normalize() alone turns e.g. 100 into '1E+2'). Quidax
    validates the literal decimal places in the volume string against each
    market's precision limit — many NGN pairs (e.g. XRP/NGN) allow 0 decimal
    places, so an internally-padded '1.00000000' gets rejected even though
    the value 1 is valid.
    """
    return format(Decimal(str(value)).normalize(), 'f')


def create_instant_order(side: str, market: str, volume: str, uid: str = None) -> dict:
    """
    Create a market buy/sell order on the Quidax exchange.
    side: 'buy' or 'sell'
    market: e.g. 'btcngn'
    """
    user_id = uid or getattr(settings, 'QUIDAX_USER_ID', 'me')
    return _call('POST', f'/users/{user_id}/orders', {
        'market': market,
        'side': side,
        'ord_type': 'market',
        'volume': _plain_decimal_str(volume),
    })


def get_instant_order(order_id: str, uid: str = None) -> dict:
    """Fetch status of a market order."""
    user_id = uid or getattr(settings, 'QUIDAX_USER_ID', 'me')
    return _call('GET', f'/users/{user_id}/orders/{order_id}')


# ── Withdrawals ────────────────────────────────────────────────────────────

def create_withdrawal(
    currency: str,
    amount: str,
    address: str,
    network: str = '',
    reference: str = '',
    uid: str = None,
) -> dict:
    """
    Send crypto out to an external address. Executes immediately on Quidax's
    side (status starts 'processing') — final outcome arrives later via the
    withdraw.successful / withdraw.rejected webhook, not in this response.
    """
    user_id = uid or getattr(settings, 'QUIDAX_USER_ID', 'me')
    payload = {
        'currency': currency.lower(),
        'amount': _plain_decimal_str(amount),
        'fund_uid': address,
    }
    if network:
        payload['network'] = network
    if reference:
        payload['reference'] = reference
    return _call('POST', f'/users/{user_id}/withdraws', payload)