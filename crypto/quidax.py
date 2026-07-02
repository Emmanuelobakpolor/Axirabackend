"""
Quidax Business API client (stdlib urllib — no extra dependencies).
Base URL: https://openapi.quidax.io/exchange-open-api/api/v1
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import logging

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
                body = json.loads(e.read().decode('utf-8'))
                msg = body.get('message', f'HTTP {e.code}')
            except Exception:
                msg = f'HTTP {e.code}: {e.reason}'
            retryable = e.code in (429, 500, 502, 503, 504)
            last_err = QuidaxError(msg, retryable=retryable)
            logger.warning(f"Quidax HTTPError {e.code}: {msg}")

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
    return _call('GET', f'/markets/{market}/tickers')


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

def get_deposit_address(uid: str, currency: str, network: str = None) -> dict:
    """
    Get or generate deposit address for a currency.
    network is optional (required for coins like USDT on multiple networks).
    """
    params = {'currency': currency.lower()}
    if network:
        params['network'] = network
    qs = urllib.parse.urlencode(params)
    return _call('GET', f'/users/{uid}/deposit_address?{qs}')


# ── Wallet Balances ─────────────────────────────────────────────────────────

def get_wallets(uid: str) -> list:
    """Get all wallet balances for a sub-account."""
    return _call('GET', f'/users/{uid}/wallets')


def get_wallet(uid: str, currency: str) -> dict:
    """Get single wallet balance."""
    return _call('GET', f'/users/{uid}/wallets/{currency.lower()}')


# ── Trade Execution ─────────────────────────────────────────────────────────

def create_instant_order(side: str, market: str, volume: str, uid: str = None) -> dict:
    """
    Create an instant buy/sell order.
    side: 'buy' or 'sell'
    market: e.g. 'btcngn'
    """
    user_id = uid or getattr(settings, 'QUIDAX_USER_ID', 'me')
    return _call('POST', f'/users/{user_id}/instant_orders', {
        'market': market,
        'side': side,
        'volume': volume,
        'unit': 'base_unit',
    })


def get_instant_order(order_id: str, uid: str = None) -> dict:
    """Fetch status of an instant order."""
    user_id = uid or getattr(settings, 'QUIDAX_USER_ID', 'me')
    return _call('GET', f'/users/{user_id}/instant_orders/{order_id}')