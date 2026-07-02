from django.conf import settings
from django.http import JsonResponse
from django.urls import path

from .views import (
    CryptoBuyOrderView,
    CryptoDepositAddressView,
    CryptoFeesView,
    CryptoOrdersView,
    CryptoPricesView,
    CryptoQuoteView,
    CryptoSellOrderView,
    CryptoSwapOrderView,
    CryptoUploadProofView,
    CryptoWalletView,
    CryptoWebhookView,
)


def _debug_quidax(request):
    import json as _json
    import urllib.error
    import urllib.request

    key = getattr(settings, 'QUIDAX_SECRET_KEY', None) or ''
    base = 'https://openapi.quidax.io/exchange-open-api/api/v1'

    key_info = {
        'key_set': bool(key),
        'key_prefix': key[:8] if key else '',
        'key_length': len(key),
        'has_bearer_prefix': key.startswith('Bearer '),
    }

    # Test 1: ticker fetch
    ticker_result = {}
    try:
        req = urllib.request.Request(
            f'{base}/markets/tickers',
            headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            ticker_result = {'status': res.status, 'ok': True}
    except urllib.error.HTTPError as e:
        body_raw = e.read()
        try:
            body = _json.loads(body_raw)
        except Exception:
            body = body_raw.decode(errors='replace')
        ticker_result = {'status': e.code, 'ok': False, 'body': body}
    except Exception as ex:
        ticker_result = {'ok': False, 'error': str(ex)}

    # Test 2: create sub-account (use a throwaway email)
    sub_result = {}
    try:
        payload = _json.dumps({'email': 'debug-test-axira@example.com', 'first_name': 'Debug', 'last_name': 'Test'}).encode()
        req = urllib.request.Request(
            f'{base}/users',
            data=payload,
            headers={
                'Authorization': f'Bearer {key}',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            body = _json.loads(res.read())
            sub_result = {'status': res.status, 'ok': True, 'uid': body.get('data', {}).get('id')}
    except urllib.error.HTTPError as e:
        body_raw = e.read()
        try:
            body = _json.loads(body_raw)
        except Exception:
            body = body_raw.decode(errors='replace')
        sub_result = {'status': e.code, 'ok': False, 'body': body}
    except Exception as ex:
        sub_result = {'ok': False, 'error': str(ex)}

    return JsonResponse({'key_info': key_info, 'ticker_test': ticker_result, 'sub_account_test': sub_result})

urlpatterns = [
    # Public-ish data
    path('crypto/prices/', CryptoPricesView.as_view()),
    path('crypto/fees/', CryptoFeesView.as_view()),

    # Quote — must call before placing any order
    path('crypto/quote/', CryptoQuoteView.as_view()),

    # User wallets and addresses
    path('crypto/wallets/', CryptoWalletView.as_view()),
    path('crypto/address/<str:coin>/', CryptoDepositAddressView.as_view()),

    # Orders
    path('crypto/orders/', CryptoOrdersView.as_view()),
    path('crypto/orders/buy/', CryptoBuyOrderView.as_view()),
    path('crypto/orders/sell/', CryptoSellOrderView.as_view()),
    path('crypto/orders/swap/', CryptoSwapOrderView.as_view()),
    path('crypto/orders/<str:reference>/proof/', CryptoUploadProofView.as_view()),

    # Webhook (no auth — HMAC verified internally)
    path('crypto/webhook/quidax/', CryptoWebhookView.as_view()),

    # TEMPORARY DEBUG — remove after confirming QUIDAX_SECRET_KEY
    path('crypto/debug-key/', _debug_quidax),
]
