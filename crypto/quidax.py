"""
Quidax Business API client (stdlib urllib — no extra dependencies).
Base URL: https://www.quidax.com/api/v1
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

QUIDAX_BASE = 'https://www.quidax.com/api/v1'


class QuidaxError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _headers():
    return {
        'Authorization': f'Bearer {settings.QUIDAX_SECRET_KEY}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }


def _call(method, path, payload=None, max_retries=3):
    url = QUIDAX_BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    last_err = None

    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                body = json.loads(res.read())
            if body.get('status') != 'success':
                raise QuidaxError(body.get('message', 'Unknown Quidax error'), retryable=False)
            return body.get('data', {})

        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                msg = body.get('message', f'HTTP {e.code}')
            except Exception:
                msg = f'HTTP {e.code}: {e.reason}'
            retryable = e.code in (429, 500, 502, 503, 504)
            last_err = QuidaxError(msg, retryable=retryable)

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = QuidaxError(f'Network error: {e}', retryable=True)

        if last_err and not last_err.retryable:
            raise last_err
        if attempt < max_retries - 1:
            time.sleep(min(2 ** attempt, 8))

    raise last_err


# ── Market data ───────────────────────────────────────────────────────────────

def get_all_tickers() -> dict:
    """Returns dict keyed by market symbol e.g. {'btcngn': {...}}"""
    return _call('GET', '/markets/tickers')


def get_ticker(market: str) -> dict:
    """Returns {'at': ..., 'ticker': {'last': ..., ...}} for one market."""
    return _call('GET', f'/markets/{market}/tickers')


# ── Sub-account management ────────────────────────────────────────────────────

def create_sub_account(email: str, first_name: str, last_name: str) -> dict:
    """
    Create a Quidax sub-account for a new Axira user.
    Returns the sub-account data including 'id' (the Quidax UID to store).
    """
    return _call('POST', '/users', {
        'email': email,
        'first_name': first_name,
        'last_name': last_name,
    })


def get_sub_account(uid: str) -> dict:
    """Fetch a sub-account by its Quidax UID."""
    return _call('GET', f'/users/{uid}')


# ── Deposit addresses ─────────────────────────────────────────────────────────

def get_deposit_address(uid: str, currency: str, network: str = None) -> dict:
    """
    Get (or generate) the deposit address for a currency on a user's sub-account.
    network is optional — required for multi-network coins (e.g. USDT on TRC20).
    Returns {'address': '...', 'currency': 'btc', 'network': '...'}
    """
    params = {'currency': currency.lower()}
    if network:
        params['network'] = network
    qs = urllib.parse.urlencode(params)
    return _call('GET', f'/users/{uid}/deposit_address?{qs}')


# ── Wallet balances ───────────────────────────────────────────────────────────

def get_wallets(uid: str) -> list:
    """
    Get all wallet balances for a Quidax sub-account.
    Returns list of wallet dicts: [{'currency': 'btc', 'balance': '0.001', ...}]
    """
    return _call('GET', f'/users/{uid}/wallets')


def get_wallet(uid: str, currency: str) -> dict:
    """Get a single wallet balance for a currency."""
    return _call('GET', f'/users/{uid}/wallets/{currency.lower()}')


# ── Trade execution ───────────────────────────────────────────────────────────

def create_instant_order(side: str, market: str, volume: str, uid: str = None) -> dict:
    """
    Create an instant buy/sell order.
    uid: Quidax user ID. Defaults to QUIDAX_USER_ID (business account).
    side: 'buy' | 'sell'
    market: e.g. 'btcngn'
    volume: amount in base currency
    """
    user_id = uid or settings.QUIDAX_USER_ID
    return _call('POST', f'/users/{user_id}/instant_orders', {
        'market': market,
        'side': side,
        'volume': volume,
        'unit': 'base_unit',
    })


def get_instant_order(order_id: str, uid: str = None) -> dict:
    """Fetch the status of an instant order."""
    user_id = uid or settings.QUIDAX_USER_ID
    return _call('GET', f'/users/{user_id}/instant_orders/{order_id}')
