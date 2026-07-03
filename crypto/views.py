import hashlib
import hmac
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    CryptoDepositAddress,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoOrderLog,
    CryptoQuote,
    CryptoWallet,
)
from .quidax import (
    QuidaxError,
    create_instant_order,
    get_all_tickers,
    get_deposit_address,
)

logger = logging.getLogger(__name__)

# ── Coin registry ─────────────────────────────────────────────────────────────
# Edit here to add/remove supported coins — Flutter never hardcodes this list.

SUPPORTED_COINS = {
    # Coins with a direct NGN market on Quidax (verified against
    # GET /markets/tickers — see CryptoDebugMarketsView)
    'BTC':      'btcngn',
    'ETH':      'ethngn',
    'USDT':     'usdtngn',
    'XRP':      'xrpngn',
    'LTC':      'ltcngn',
    'DASH':     'dashngn',
    'TRX':      'trxngn',
    'QDX':      'qdxngn',
    'USDC':     'usdcngn',
    'SOL':      'solngn',
    'CNGN':     'cngnngn',
    'AXCNH':    'axcnhngn',
    'XAUT':     'xautngn',
    # USDT-only markets on Quidax — _fetch_live_rate()/_resolve_price_ngn()
    # convert these via coin/USDT * USDT/NGN automatically.
    'BNB':        'bnbngn',
    'DOGE':       'dogengn',
    'ADA':        'adangn',
    'LINK':       'linkngn',
    'DOT':        'dotngn',
    'BCH':        'bchngn',
    'MANA':       'manangn',
    'SAND':       'sandngn',
    'FLOKI':      'flokingn',
    'XTZ':        'xtzngn',
    'ONE':        'onengn',
    'BABYDOGE':   'babydogengn',
    'FIL':        'filngn',
    'XLM':        'xlmngn',
    'CAKE':       'cakengn',
    'SHIB':       'shibngn',
    'AAVE':       'aavengn',
    'POL':        'polngn',
    'MEME':       'memengn',
    'CFX':        'cfxngn',
    'APE':        'apengn',
    'PEPE':       'pepengn',
    'ENJ':        'enjngn',
    'LRC':        'lrcngn',
    'ARB':        'arbngn',
    'ALGO':       'algongn',
    'ENS':        'ensngn',
    'ORDI':       'ordingn',
    'BONK':       'bonkngn',
    'SUI':        'suingn',
    'WIF':        'wifngn',
    'MYRO':       'myrongn',
    'JUP':        'jupngn',
    'BLUR':       'blurngn',
    'KATA':       'katangn',
    'COQ':        'coqngn',
    'FET':        'fetngn',
    'AI':         'aingn',
    'AXS':        'axsngn',
    'BEAM':       'beamngn',
    'BOB':        'bobngn',
    'BOME':       'bomengn',
    'ETC':        'etcngn',
    'FARTCOIN':   'fartcoinngn',
    'GNO':        'gnongn',
    'HYPE':       'hypengn',
    'INJ':        'injngn',
    'LSK':        'lskngn',
    'MAGATRUMP':  'magatrumpngn',
    'MEW':        'mewngn',
    'MNT':        'mntngn',
    'MOG':        'mogngn',
    'NEAR':       'nearngn',
    'NOCHILL':    'nochillngn',
    'NOS':        'nosngn',
    'PNUT':       'pnutngn',
    'RNDR':       'rndrngn',
    'SLERF':      'slerfngn',
    'SLP':        'slpngn',
    'STRK':       'strkngn',
    'STX':        'stxngn',
    'SUSHI':      'sushingn',
    'TON':        'tonngn',
    'TRUMP':      'trumpngn',
    'USAT':       'usatngn',
    'WATSON':     'watsonngn',
    'WAVES':      'wavesngn',
    'WLD':        'wldngn',
    'WLFI':       'wlfingn',
    'ZIL':        'zilngn',
    'ZK':         'zkngn',
}

COIN_META = {
    'BTC':      {'name': 'Bitcoin',                'color': '#F7931A', 'letter': 'B'},
    'ETH':      {'name': 'Ethereum',               'color': '#627EEA', 'letter': 'E'},
    'USDT':     {'name': 'Tether',                 'color': '#26A17B', 'letter': 'T'},
    'XRP':      {'name': 'Ripple',                 'color': '#346AA9', 'letter': 'X'},
    'LTC':      {'name': 'Litecoin',                'color': '#345D9D', 'letter': 'L'},
    'DASH':     {'name': 'Dash',                    'color': '#008CE7', 'letter': 'D'},
    'TRX':      {'name': 'Tron',                    'color': '#EF0027', 'letter': 'T'},
    'QDX':      {'name': 'Quidax Token',            'color': '#1652F0', 'letter': 'Q'},
    'USDC':     {'name': 'USDC Coin',               'color': '#2775CA', 'letter': 'U'},
    'SOL':      {'name': 'Solana',                  'color': '#9945FF', 'letter': 'S'},
    'CNGN':     {'name': 'cNGN',                    'color': '#2AA96C', 'letter': 'C'},
    'AXCNH':    {'name': 'CNHC',                    'color': '#DE2910', 'letter': 'A'},
    'XAUT':     {'name': 'Tether Gold',             'color': '#C9A227', 'letter': 'X'},
    'BNB':      {'name': 'Binance Coin',            'color': '#F3BA2F', 'letter': 'B'},
    'DOGE':     {'name': 'Dogecoin',                'color': '#C2A633', 'letter': 'D'},
    'ADA':      {'name': 'Cardano',                 'color': '#0033AD', 'letter': 'A'},
    'LINK':     {'name': 'Chainlink',               'color': '#2A5ADA', 'letter': 'L'},
    'DOT':      {'name': 'Polkadot',                'color': '#E6007A', 'letter': 'D'},
    'BCH':      {'name': 'Bitcoin Cash',            'color': '#8DC351', 'letter': 'B'},
    'MANA':     {'name': 'Decentraland',            'color': '#FF2D55', 'letter': 'M'},
    'SAND':     {'name': 'The Sandbox',             'color': '#00ADEF', 'letter': 'S'},
    'FLOKI':    {'name': 'Floki Inu',               'color': '#F5A623', 'letter': 'F'},
    'XTZ':      {'name': 'Tezos',                   'color': '#2C7DF7', 'letter': 'T'},
    'ONE':      {'name': 'Harmony',                 'color': '#00AEE9', 'letter': 'O'},
    'BABYDOGE': {'name': 'Baby Doge Coin',          'color': '#BA9F33', 'letter': 'B'},
    'FIL':      {'name': 'Filecoin',                'color': '#0090FF', 'letter': 'F'},
    'XLM':      {'name': 'Stellar',                 'color': '#14B6E7', 'letter': 'X'},
    'CAKE':     {'name': 'PancakeSwap',             'color': '#D1884F', 'letter': 'C'},
    'SHIB':     {'name': 'Shiba Inu',               'color': '#FFA409', 'letter': 'S'},
    'AAVE':     {'name': 'Aave',                    'color': '#B6509E', 'letter': 'A'},
    'POL':      {'name': 'Polygon',                 'color': '#8247E5', 'letter': 'P'},
    'MEME':     {'name': 'Memecoin',                'color': '#83E14F', 'letter': 'M'},
    'CFX':      {'name': 'Conflux',                 'color': '#2A2ABB', 'letter': 'C'},
    'APE':      {'name': 'ApeCoin',                 'color': '#0054FA', 'letter': 'A'},
    'PEPE':     {'name': 'Pepe',                    'color': '#4CA82D', 'letter': 'P'},
    'ENJ':      {'name': 'Enjin Coin',              'color': '#624DBF', 'letter': 'E'},
    'LRC':      {'name': 'Loopring',                'color': '#1C60FF', 'letter': 'L'},
    'ARB':      {'name': 'Arbitrum',                'color': '#28A0F0', 'letter': 'A'},
    'ALGO':     {'name': 'Algorand',                'color': '#1F2937', 'letter': 'A'},
    'ENS':      {'name': 'Ethereum Name Service',   'color': '#5284FF', 'letter': 'E'},
    'ORDI':     {'name': 'Ordinals',                'color': '#F7931A', 'letter': 'O'},
    'BONK':     {'name': 'Bonk',                    'color': '#FFCE45', 'letter': 'B'},
    'SUI':      {'name': 'Sui',                     'color': '#4DA1F9', 'letter': 'S'},
    'WIF':      {'name': 'dogwifhat',               'color': '#CDA35C', 'letter': 'W'},
    'MYRO':     {'name': 'Myro',                    'color': '#7C4DFF', 'letter': 'M'},
    'JUP':      {'name': 'Jupiter',                 'color': '#C7F284', 'letter': 'J'},
    'BLUR':     {'name': 'Blur',                    'color': '#FF6C4B', 'letter': 'B'},
    'KATA':     {'name': 'Katana Inu',              'color': '#4B5563', 'letter': 'K'},
    'COQ':      {'name': 'Coq Inu',                 'color': '#FF7A00', 'letter': 'C'},
    'FET':      {'name': 'Fetch.ai',                'color': '#3F51B5', 'letter': 'F'},
    'AI':       {'name': 'AI',                      'color': '#6B7280', 'letter': 'A'},
    'AXS':      {'name': 'Axie Infinity',           'color': '#0055D5', 'letter': 'A'},
    'BEAM':     {'name': 'Beam',                    'color': '#FFB400', 'letter': 'B'},
    'BOB':      {'name': 'BOB',                     'color': '#F7931A', 'letter': 'B'},
    'BOME':     {'name': 'Book of Meme',            'color': '#FF66C4', 'letter': 'B'},
    'ETC':      {'name': 'Ethereum Classic',        'color': '#328332', 'letter': 'E'},
    'FARTCOIN': {'name': 'Fartcoin',                'color': '#8B5E3C', 'letter': 'F'},
    'GNO':      {'name': 'Gnosis',                  'color': '#00A6C4', 'letter': 'G'},
    'HYPE':     {'name': 'Hyperliquid',             'color': '#1FE0A6', 'letter': 'H'},
    'INJ':      {'name': 'Injective',               'color': '#00D2FF', 'letter': 'I'},
    'LSK':      {'name': 'Lisk',                    'color': '#0D1521', 'letter': 'L'},
    'MAGATRUMP':{'name': 'MAGA Trump',              'color': '#B22234', 'letter': 'M'},
    'MEW':      {'name': 'cat in a dogs world',     'color': '#8DD3F5', 'letter': 'M'},
    'MNT':      {'name': 'Mantle',                  'color': '#1F2937', 'letter': 'M'},
    'MOG':      {'name': 'Mog Coin',                'color': '#6B7280', 'letter': 'M'},
    'NEAR':     {'name': 'NEAR Protocol',           'color': '#00C08B', 'letter': 'N'},
    'NOCHILL':  {'name': 'No Chill',                'color': '#6B7280', 'letter': 'N'},
    'NOS':      {'name': 'Nosana',                  'color': '#08872B', 'letter': 'N'},
    'PNUT':     {'name': 'Peanut the Squirrel',     'color': '#A0522D', 'letter': 'P'},
    'RNDR':     {'name': 'Render',                  'color': '#DE1A72', 'letter': 'R'},
    'SLERF':    {'name': 'Slerf',                   'color': '#6B7280', 'letter': 'S'},
    'SLP':      {'name': 'Smooth Love Potion',      'color': '#47D7AC', 'letter': 'S'},
    'STRK':     {'name': 'Starknet',                'color': '#EC796B', 'letter': 'S'},
    'STX':      {'name': 'Stacks',                  'color': '#5546FF', 'letter': 'S'},
    'SUSHI':    {'name': 'SushiSwap',               'color': '#FA52A0', 'letter': 'S'},
    'TON':      {'name': 'Toncoin',                 'color': '#0098EA', 'letter': 'T'},
    'TRUMP':    {'name': 'Official Trump',          'color': '#B22234', 'letter': 'T'},
    'USAT':     {'name': 'USAT',                    'color': '#6B7280', 'letter': 'U'},
    'WATSON':   {'name': 'Watson',                  'color': '#6B7280', 'letter': 'W'},
    'WAVES':    {'name': 'Waves',                   'color': '#0155FF', 'letter': 'W'},
    'WLD':      {'name': 'Worldcoin',               'color': '#1F2937', 'letter': 'W'},
    'WLFI':     {'name': 'World Liberty Financial', 'color': '#1652A0', 'letter': 'W'},
    'ZIL':      {'name': 'Zilliqa',                 'color': '#49C1BF', 'letter': 'Z'},
    'ZK':       {'name': 'zkSync',                  'color': '#8C8DFC', 'letter': 'Z'},
}


def _logo_url(coin: str) -> str:
    return f'https://assets.coincap.io/assets/icons/{coin.lower()}@2x.png'


QUOTE_TTL_SECONDS = 30

_NGN_PER_USD = Decimal(str(getattr(settings, 'NGN_PER_USD', '1600')))

def _create_flw_payment_link(order: 'CryptoOrder') -> str:
    """
    Create a Flutterwave hosted-checkout link for the given buy order.
    Delegates to wallet.flutterwave which already has the API client.
    Returns the payment URL string.
    """
    from wallet.flutterwave import FlutterwaveError, _call as flw_call

    if not getattr(settings, 'FLUTTERWAVE_SECRET_KEY', ''):
        raise ValueError('FLUTTERWAVE_SECRET_KEY is not configured.')

    user = order.user
    redirect_url = getattr(
        settings, 'FLUTTERWAVE_REDIRECT_URL',
        'https://web-production-b557d.up.railway.app/api/crypto/payment/done/',
    )
    try:
        data = flw_call('POST', '/payments', {
            'tx_ref': order.reference,
            'amount': float(order.total_ngn),
            'currency': 'NGN',
            'redirect_url': redirect_url,
            'customer': {
                'email': user.email,
                'name': user.full_name,
                'phonenumber': getattr(user, 'phone', ''),
            },
            'customizations': {
                'title': f'Buy {order.coin} on Axira',
                'description': f'{order.coin_amount} {order.coin}',
            },
            'payment_options': 'card,banktransfer,ussd',
        })
    except FlutterwaveError as exc:
        raise ValueError(str(exc))

    link = data.get('data', {}).get('link', '')
    if not link:
        raise ValueError('Flutterwave did not return a payment link.')
    return link


def handle_flw_crypto_charge(data: dict):
    """
    Called from the wallet Flutterwave webhook when tx_ref starts with 'CRY'.
    Verifies the payment and auto-executes the buy on Quidax.
    """
    from wallet.flutterwave import FlutterwaveError, verify_transaction

    tx_ref = str(data.get('tx_ref', ''))
    flw_tx_id = str(data.get('id', ''))
    flw_status = str(data.get('status', '')).lower()
    amount_paid = data.get('amount', 0)
    currency = str(data.get('currency', '')).upper()

    if flw_status != 'successful' or currency != 'NGN':
        logger.info('Crypto FLW charge skipped: status=%s currency=%s', flw_status, currency)
        return

    try:
        order = CryptoOrder.objects.select_for_update().get(
            reference=tx_ref,
            order_type=CryptoOrder.OrderType.BUY,
            status=CryptoOrder.Status.PENDING_PAYMENT,
        )
    except CryptoOrder.DoesNotExist:
        logger.warning('FLW crypto webhook: no pending buy order with ref=%s', tx_ref)
        return

    # Check amount matches within ₦1 rounding tolerance
    if abs(float(order.total_ngn) - float(amount_paid)) > 1:
        logger.error(
            'FLW amount mismatch for %s: expected %s got %s',
            tx_ref, order.total_ngn, amount_paid,
        )
        _log(order, 'flw_amount_mismatch', {
            'expected': str(order.total_ngn),
            'received': str(amount_paid),
        })
        return

    # Double-verify with Flutterwave to prevent replay attacks
    try:
        verify = verify_transaction(tx_ref)
        if verify.get('data', {}).get('status') != 'successful':
            logger.warning('FLW verification failed for tx_ref=%s', tx_ref)
            return
    except FlutterwaveError as exc:
        logger.error('FLW verify call failed: %s', exc)
        return

    with transaction.atomic():
        order.status = CryptoOrder.Status.PAYMENT_RECEIVED
        order.flw_transaction_id = flw_tx_id
        order.save(update_fields=['status', 'flw_transaction_id', 'updated_at'])
        _log(order, 'flw_payment_confirmed', {
            'flw_tx_id': flw_tx_id,
            'amount_paid': str(amount_paid),
        })

    _execute_buy_after_payment(order)

_AXIRA_BANK = {
    'bank_name':      getattr(settings, 'AXIRA_BANK_NAME', ''),
    'account_number': getattr(settings, 'AXIRA_ACCOUNT_NUMBER', ''),
    'account_name':   getattr(settings, 'AXIRA_ACCOUNT_NAME', ''),
}

# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_or_create_fee(fee_type: str) -> CryptoFeeSettings:
    obj, _ = CryptoFeeSettings.objects.get_or_create(
        fee_type=fee_type,
        defaults={'flat_usd': Decimal('0'), 'percent': Decimal('0')},
    )
    return obj


def _compute_fee(fee: CryptoFeeSettings, ngn_amount: Decimal) -> Decimal:
    flat_ngn = fee.flat_usd * _NGN_PER_USD
    pct_ngn = ngn_amount * fee.percent / Decimal('100')
    return (flat_ngn + pct_ngn).quantize(Decimal('0.01'))


def _parse_decimal(value, field_name: str) -> Decimal:
    try:
        d = Decimal(str(value))
        if d <= 0:
            raise ValueError
        return d
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Invalid {field_name}.')


def _fetch_live_rate(coin: str) -> Decimal:
    """
    Fetch coin's live NGN rate via Quidax's bulk tickers endpoint — the same
    data source and resolution logic (direct NGN market, else coin/USDT *
    USDT/NGN) already proven correct by the public prices list. Reusing it
    here avoids relying on the single-market ticker endpoint's response
    shape, which doesn't match what /markets/{market}/tickers actually
    returns and was silently producing false "no price" failures.
    """
    try:
        tickers = get_all_tickers()
    except QuidaxError as exc:
        raise ValueError(f'Unable to fetch live price for {coin}.') from exc

    rate = _resolve_price_ngn(coin, tickers)
    if rate > 0:
        return rate

    raise ValueError(f'Unable to fetch live price for {coin}.')


def _log(order: CryptoOrder, event: str, detail: dict = None) -> None:
    try:
        CryptoOrderLog.log(order, event, detail)
    except Exception:
        logger.warning('Audit log write failed: %s on %s', event, order.reference)


def _validate_quote(quote_id: str, user, expected_type: str):
    """
    Load and validate a quote. Returns the CryptoQuote on success.
    Raises ValueError with a user-facing message on any problem.
    """
    try:
        quote = CryptoQuote.objects.get(id=quote_id, user=user)
    except CryptoQuote.DoesNotExist:
        raise ValueError('Quote not found.')
    if quote.quote_type != expected_type:
        raise ValueError('Quote type mismatch.')
    if quote.is_used():
        raise ValueError('This quote has already been used.')
    if quote.is_expired():
        raise ValueError('This quote has expired. Please request a new one.')
    return quote


def _credit_wallet(user, coin: str, amount: Decimal) -> None:
    """Add amount to available balance atomically (get_or_create safe)."""
    with transaction.atomic():
        wallet, _ = CryptoWallet.objects.select_for_update().get_or_create(
            user=user, coin=coin,
            defaults={'available': Decimal('0'), 'reserved': Decimal('0')},
        )
        CryptoWallet.objects.filter(pk=wallet.pk).update(
            available=F('available') + amount,
        )


def _reserve_balance(user, coin: str, amount: Decimal) -> bool:
    """
    Move amount from available → reserved atomically.
    Returns True if successful, False if insufficient balance.
    """
    with transaction.atomic():
        updated = CryptoWallet.objects.filter(
            user=user,
            coin=coin,
            available__gte=amount,
        ).update(
            available=F('available') - amount,
            reserved=F('reserved') + amount,
        )
        return updated > 0


def _release_reserved(user, coin: str, amount: Decimal) -> None:
    """Move amount from reserved → available (on failure or cancellation)."""
    CryptoWallet.objects.filter(user=user, coin=coin).update(
        reserved=F('reserved') - amount,
        available=F('available') + amount,
    )


def _deduct_reserved(user, coin: str, amount: Decimal) -> None:
    """Permanently remove from reserved balance (on successful execution)."""
    CryptoWallet.objects.filter(user=user, coin=coin).update(
        reserved=F('reserved') - amount,
    )


def _idempotency_check(ikey: str, user, order_type: str):
    if not ikey:
        return None
    return CryptoOrder.objects.filter(
        idempotency_key=ikey, user=user, order_type=order_type,
    ).first()


def _get_user_deposit_address(user, coin: str, network: str = '') -> str:
    """
    Return the user's deposit address for a coin, generating it via Quidax
    if not yet stored. Raises ValueError if user has no Quidax sub-account.
    """
    cached = CryptoDepositAddress.objects.filter(
        user=user, coin=coin, network=network,
    ).first()
    if cached:
        return cached.address

    if not user.quidax_user_id:
        raise ValueError(
            'Your account is not yet linked to a Quidax sub-account. '
            'Please contact support.'
        )

    try:
        result = get_deposit_address(
            uid=user.quidax_user_id,
            currency=coin,
            network=network or None,
        )
        address = result.get('address', '')
        if not address:
            raise ValueError(f'Quidax returned no address for {coin}.')

        CryptoDepositAddress.objects.get_or_create(
            user=user,
            coin=coin,
            network=network,
            defaults={
                'address': address,
                'quidax_ref': str(result.get('id', '')),
            },
        )
        return address
    except QuidaxError as exc:
        raise ValueError(f'Could not generate deposit address: {exc}') from exc


# ── Response helpers ──────────────────────────────────────────────────────────

def _quote_response(quote: CryptoQuote) -> dict:
    now = timezone.now()
    expires_in = max(int((quote.expires_at - now).total_seconds()), 0)
    base = {
        'quote_id': str(quote.id),
        'type': quote.quote_type,
        'coin': quote.coin,
        'coin_amount': str(quote.coin_amount),
        'rate_ngn': str(quote.rate_ngn),
        'fee_ngn': str(quote.fee_ngn),
        'total_ngn': str(quote.total_ngn),
        'expires_at': quote.expires_at.isoformat(),
        'expires_in': expires_in,
    }
    if quote.quote_type == CryptoQuote.QuoteType.SWAP:
        base['to_coin'] = quote.to_coin
        base['to_rate_ngn'] = str(quote.to_rate_ngn)
        base['to_coin_amount'] = str(quote.to_coin_amount)
    return base


def _order_dict(o: CryptoOrder) -> dict:
    return {
        'reference': o.reference,
        'order_type': o.order_type,
        'coin': o.coin,
        'to_coin': o.to_coin,
        'coin_amount': str(o.coin_amount),
        'to_coin_amount': str(o.to_coin_amount),
        'rate_ngn': str(o.rate_ngn),
        'fee_ngn': str(o.fee_ngn),
        'total_ngn': str(o.total_ngn),
        'status': o.status,
        'created_at': o.created_at.isoformat(),
    }


# ── Views ─────────────────────────────────────────────────────────────────────

def _ticker_last(tickers: dict, market: str) -> Decimal:
    raw = tickers.get(market, {}).get('ticker', {}).get('last', '0')
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return Decimal('0')


def _resolve_price_ngn(coin: str, tickers: dict) -> Decimal:
    """
    Resolve coin's NGN price from the bulk tickers payload, falling back to
    coin/USDT * USDT/NGN for coins with no direct NGN market (e.g. BNB).
    """
    direct = _ticker_last(tickers, SUPPORTED_COINS[coin])
    if direct > 0:
        return direct

    usdt_price = _ticker_last(tickers, f'{coin.lower()}usdt')
    usdtngn_price = _ticker_last(tickers, 'usdtngn')
    converted = usdt_price * usdtngn_price
    return converted if converted > 0 else Decimal('0')


class CryptoPricesView(APIView):
    """Live prices + coin metadata. Public — no auth required."""
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            tickers = get_all_tickers()
        except QuidaxError as exc:
            logger.error('Quidax ticker fetch failed: %s', exc)
            return Response({'error': str(exc)}, status=502)

        coins, prices = [], {}
        for coin in SUPPORTED_COINS:
            price = str(_resolve_price_ngn(coin, tickers))
            prices[coin] = price
            meta = COIN_META.get(coin, {})
            coins.append({
                'symbol': coin,
                'name': meta.get('name', coin),
                'color': meta.get('color', '#888888'),
                'letter': meta.get('letter', coin[0]),
                'logo_url': _logo_url(coin),
                'price_ngn': price,
            })

        return Response({'prices': prices, 'coins': coins})


class CryptoFeesView(APIView):
    """Current fee config — public, Flutter uses for estimate display only."""
    permission_classes = [AllowAny]

    def get(self, request):
        fees = {}
        for ft in ('buy', 'sell', 'swap'):
            f = _get_or_create_fee(ft)
            fees[ft] = {'flat_usd': str(f.flat_usd), 'percent': str(f.percent)}
        return Response({'fees': fees})


class CryptoQuoteView(APIView):
    """
    Request a price-locked quote valid for 30 seconds.

    POST body:
        type    : 'buy' | 'sell' | 'swap'
        coin    : 'BTC'
        amount  : '0.001'
        to_coin : 'ETH'  (swap only)

    The backend fetches the live Quidax price at this moment, computes the
    fee, and locks the result into a CryptoQuote row with a 30-second expiry.
    The client must submit quote_id when placing the order.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        quote_type = str(request.data.get('type', '')).lower().strip()
        if quote_type not in ('buy', 'sell', 'swap'):
            return Response({'error': "type must be 'buy', 'sell', or 'swap'."}, status=400)

        coin = str(request.data.get('coin', '')).upper().strip()
        if coin not in SUPPORTED_COINS:
            return Response({'error': f'Unsupported coin: {coin}'}, status=400)

        to_coin = str(request.data.get('to_coin', '')).upper().strip()
        if quote_type == 'swap':
            if to_coin not in SUPPORTED_COINS:
                return Response({'error': f'Unsupported to_coin: {to_coin}'}, status=400)
            if coin == to_coin:
                return Response({'error': 'Cannot swap a coin with itself.'}, status=400)

        try:
            coin_amount = _parse_decimal(request.data.get('amount'), 'amount')
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        # Fetch live rate(s)
        try:
            rate_ngn = _fetch_live_rate(coin)
            to_rate_ngn = _fetch_live_rate(to_coin) if quote_type == 'swap' else Decimal('0')
        except ValueError as exc:
            return Response({'error': str(exc)}, status=502)

        # Compute fee and totals
        fee_obj = _get_or_create_fee(quote_type)
        ngn_value = (coin_amount * rate_ngn).quantize(Decimal('0.01'))
        fee_ngn = _compute_fee(fee_obj, ngn_value)

        if quote_type == 'buy':
            total_ngn = ngn_value + fee_ngn
            to_coin_amount = Decimal('0')
        elif quote_type == 'sell':
            total_ngn = max(ngn_value - fee_ngn, Decimal('0'))  # payout
            to_coin_amount = Decimal('0')
        else:  # swap
            net_ngn = ngn_value - fee_ngn
            if net_ngn <= 0:
                return Response({'error': 'Amount too small to cover swap fee.'}, status=400)
            total_ngn = ngn_value
            to_coin_amount = (net_ngn / to_rate_ngn).quantize(Decimal('0.00000001'))

        quote = CryptoQuote.objects.create(
            user=request.user,
            quote_type=quote_type,
            coin=coin,
            to_coin=to_coin,
            coin_amount=coin_amount,
            rate_ngn=rate_ngn,
            to_rate_ngn=to_rate_ngn,
            fee_ngn=fee_ngn,
            total_ngn=total_ngn,
            to_coin_amount=to_coin_amount,
            expires_at=timezone.now() + timedelta(seconds=QUOTE_TTL_SECONDS),
        )

        return Response(_quote_response(quote), status=201)


class CryptoWalletView(APIView):
    """User's internal crypto wallet balances."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallets = CryptoWallet.objects.filter(user=request.user)
        return Response({
            'wallets': [
                {
                    'coin': w.coin,
                    'available': str(w.available),
                    'reserved': str(w.reserved),
                    'total': str(w.total),
                }
                for w in wallets
            ]
        })


class CryptoDepositAddressView(APIView):
    """
    Get the user's deposit address for a coin.
    Creates one via Quidax sub-account on first call.

    GET /api/crypto/address/<coin>/
    Optional query param: ?network=TRC20
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, coin):
        coin = coin.upper().strip()
        if coin not in SUPPORTED_COINS:
            return Response({'error': f'Unsupported coin: {coin}'}, status=400)

        network = str(request.query_params.get('network', '')).upper().strip()

        try:
            address = _get_user_deposit_address(request.user, coin, network)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response({'coin': coin, 'network': network, 'address': address})


class CryptoBuyOrderView(APIView):
    """
    Place a buy order using a valid quote.

    If FLUTTERWAVE_SECRET_KEY is configured, returns a Flutterwave hosted-
    checkout link. Payment confirmation is fully automated via the
    /crypto/webhook/flutterwave/ endpoint.

    If not configured, falls back to manual bank-transfer flow.

    POST body: { quote_id, idempotency_key }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ikey = str(request.data.get('idempotency_key', '')).strip()
        existing = _idempotency_check(ikey, request.user, CryptoOrder.OrderType.BUY)
        if existing:
            return Response(_buy_response(existing), status=200)

        quote_id = str(request.data.get('quote_id', '')).strip()
        try:
            quote = _validate_quote(quote_id, request.user, CryptoQuote.QuoteType.BUY)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        with transaction.atomic():
            quote.mark_used()
            order = CryptoOrder.objects.create(
                user=request.user,
                quote=quote,
                order_type=CryptoOrder.OrderType.BUY,
                coin=quote.coin,
                coin_amount=quote.coin_amount,
                rate_ngn=quote.rate_ngn,
                fee_ngn=quote.fee_ngn,
                total_ngn=quote.total_ngn,
                status=CryptoOrder.Status.PENDING_PAYMENT,
                idempotency_key=ikey or None,
            )

        _log(order, 'order_created', {
            'coin': order.coin,
            'coin_amount': str(order.coin_amount),
            'rate_ngn': str(order.rate_ngn),
            'total_ngn': str(order.total_ngn),
            'quote_id': quote_id,
        })

        # Generate Flutterwave payment link if configured
        if getattr(settings, 'FLUTTERWAVE_SECRET_KEY', ''):
            try:
                payment_url = _create_flw_payment_link(order)
                order.flw_payment_url = payment_url
                order.save(update_fields=['flw_payment_url', 'updated_at'])
                _log(order, 'flw_payment_link_created', {'url': payment_url})
            except ValueError as exc:
                logger.error('Failed to create Flutterwave link for %s: %s', order.reference, exc)
                # Fall through — return order with bank details fallback

        return Response(_buy_response(order), status=201)


class CryptoSellOrderView(APIView):
    """
    Place a sell order using a valid quote.

    The user must already have crypto in their internal wallet (credited via
    deposit webhook or a completed buy order).

    Flow after this endpoint:
      1. If user already has the balance → immediately reserve and execute.
      2. If not → return WAITING_DEPOSIT status with deposit address so
         they can deposit first, then call this endpoint again.

    POST body: { quote_id, idempotency_key }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ikey = str(request.data.get('idempotency_key', '')).strip()
        existing = _idempotency_check(ikey, request.user, CryptoOrder.OrderType.SELL)
        if existing:
            return Response(_sell_response(existing), status=200)

        quote_id = str(request.data.get('quote_id', '')).strip()
        try:
            quote = _validate_quote(quote_id, request.user, CryptoQuote.QuoteType.SELL)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        coin = quote.coin
        coin_amount = quote.coin_amount

        # Try to get the user's deposit address (for WAITING_DEPOSIT response)
        try:
            deposit_address = _get_user_deposit_address(request.user, coin)
        except ValueError:
            deposit_address = ''

        # Check and reserve balance
        has_balance = _reserve_balance(request.user, coin, coin_amount)

        with transaction.atomic():
            quote.mark_used()
            order = CryptoOrder.objects.create(
                user=request.user,
                quote=quote,
                order_type=CryptoOrder.OrderType.SELL,
                coin=coin,
                coin_amount=coin_amount,
                rate_ngn=quote.rate_ngn,
                fee_ngn=quote.fee_ngn,
                total_ngn=quote.total_ngn,  # payout_ngn
                status=CryptoOrder.Status.PROCESSING if has_balance else CryptoOrder.Status.WAITING_DEPOSIT,
                idempotency_key=ikey or None,
            )

        _log(order, 'order_created', {
            'coin': coin,
            'coin_amount': str(coin_amount),
            'has_balance': has_balance,
            'payout_ngn': str(quote.total_ngn),
        })

        if has_balance:
            _log(order, 'balance_reserved', {'coin': coin, 'amount': str(coin_amount)})
            _execute_sell(order, coin_amount)

        return Response(_sell_response(order, deposit_address), status=201)


class CryptoSwapOrderView(APIView):
    """
    Swap coin → to_coin using the user's internal wallet balance.
    Executes immediately if Quidax is configured. No deposit or bank transfer needed.

    POST body: { quote_id, idempotency_key }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ikey = str(request.data.get('idempotency_key', '')).strip()
        existing = _idempotency_check(ikey, request.user, CryptoOrder.OrderType.SWAP)
        if existing:
            return Response(_swap_response(existing), status=200)

        quote_id = str(request.data.get('quote_id', '')).strip()
        try:
            quote = _validate_quote(quote_id, request.user, CryptoQuote.QuoteType.SWAP)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        from_coin = quote.coin
        to_coin = quote.to_coin
        coin_amount = quote.coin_amount

        # Check and reserve source balance
        if not _reserve_balance(request.user, from_coin, coin_amount):
            return Response(
                {'error': f'Insufficient {from_coin} balance.'},
                status=400,
            )

        with transaction.atomic():
            quote.mark_used()
            order = CryptoOrder.objects.create(
                user=request.user,
                quote=quote,
                order_type=CryptoOrder.OrderType.SWAP,
                coin=from_coin,
                to_coin=to_coin,
                coin_amount=coin_amount,
                to_coin_amount=quote.to_coin_amount,
                rate_ngn=quote.rate_ngn,
                fee_ngn=quote.fee_ngn,
                total_ngn=quote.total_ngn,
                status=CryptoOrder.Status.PROCESSING,
                idempotency_key=ikey or None,
            )

        _log(order, 'order_created', {
            'from_coin': from_coin,
            'to_coin': to_coin,
            'coin_amount': str(coin_amount),
            'to_coin_amount': str(quote.to_coin_amount),
        })
        _log(order, 'balance_reserved', {'coin': from_coin, 'amount': str(coin_amount)})

        _execute_swap(order)

        return Response(_swap_response(order), status=201)


class CryptoUploadProofView(APIView):
    """Attach payment proof to a pending buy order."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reference):
        try:
            order = CryptoOrder.objects.get(
                reference=reference,
                user=request.user,
                order_type=CryptoOrder.OrderType.BUY,
            )
        except CryptoOrder.DoesNotExist:
            return Response({'error': 'Order not found.'}, status=404)

        if order.status != CryptoOrder.Status.PENDING_PAYMENT:
            return Response({'error': 'Order cannot be updated at this stage.'}, status=400)

        proof = request.FILES.get('proof')
        if not proof:
            return Response({'error': 'No file uploaded.'}, status=400)

        order.payment_proof = proof
        order.save(update_fields=['payment_proof', 'updated_at'])
        _log(order, 'proof_uploaded', {'filename': proof.name})

        return Response({'message': 'Proof uploaded.', 'status': order.status})


class CryptoOrdersView(APIView):
    """User's own crypto order history."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        orders = CryptoOrder.objects.filter(user=request.user).select_related('quote')[:50]
        return Response({'orders': [_order_dict(o) for o in orders]})


class CryptoWebhookView(APIView):
    """
    Receives Quidax webhook events.

    Quidax sends a HMAC-SHA512 signature in the X-Quidax-Signature header.
    Set QUIDAX_WEBHOOK_SECRET in Railway to the secret you configure in the
    Quidax merchant dashboard.

    Handled events:
      deposit.successful  → credit user's internal CryptoWallet
      order.completed     → (future) auto-complete Quidax-initiated orders
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        # ── Signature verification ─────────────────────────────────────────
        secret = getattr(settings, 'QUIDAX_WEBHOOK_SECRET', '')
        if secret:
            sig_header = request.headers.get('X-Quidax-Signature', '')
            expected = hmac.new(
                secret.encode(),
                request.body,
                hashlib.sha512,
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                logger.warning('Quidax webhook signature mismatch')
                return Response({'error': 'Invalid signature.'}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return Response({'error': 'Invalid JSON.'}, status=400)

        event = payload.get('event', '')
        data = payload.get('data', {})

        logger.info('Quidax webhook received: %s', event)

        if event == 'deposit.successful':
            self._handle_deposit(data)
        elif event == 'order.completed':
            self._handle_order_completed(data)
        # Unknown events are silently accepted (return 200) to prevent Quidax retries

        return Response({'received': True})

    def _handle_deposit(self, data: dict):
        """
        Credit the user's internal CryptoWallet when a deposit is confirmed.

        data fields from Quidax:
          user_id   — the Quidax sub-account UID (maps to accounts.User.quidax_user_id)
          amount    — deposit amount as string
          currency  — e.g. 'btc'
          network   — e.g. 'trc20' (may be absent)
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()

        quidax_uid = str(data.get('user', {}).get('id', '') or data.get('user_id', ''))
        raw_amount = str(data.get('amount', '0'))
        currency = str(data.get('currency', '')).upper()
        network = str(data.get('network', '')).upper()

        if not quidax_uid or currency not in SUPPORTED_COINS:
            logger.warning('Deposit webhook: unrecognised uid=%s or currency=%s', quidax_uid, currency)
            return

        try:
            amount = Decimal(raw_amount)
            if amount <= 0:
                return
        except InvalidOperation:
            logger.warning('Deposit webhook: invalid amount %s', raw_amount)
            return

        try:
            user = User.objects.get(quidax_user_id=quidax_uid)
        except User.DoesNotExist:
            logger.warning('Deposit webhook: no user with quidax_user_id=%s', quidax_uid)
            return

        _credit_wallet(user, currency, amount)
        logger.info(
            'Deposit credited: %s %s → %s (network=%s)',
            amount, currency, user.email, network,
        )

    def _handle_order_completed(self, data: dict):
        """Update an order status if tracked by its Quidax order ID."""
        quidax_order_id = str(data.get('id', ''))
        if not quidax_order_id:
            return
        CryptoOrder.objects.filter(quidax_order_id=quidax_order_id).update(
            status=CryptoOrder.Status.COMPLETED,
        )


# ── Execution helpers (called synchronously for now) ─────────────────────────

def _execute_sell(order: CryptoOrder, coin_amount: Decimal):
    """
    Call Quidax to sell the user's crypto on the business account.
    Credits the user's NGN wallet on success, releases reserve on failure.
    """
    if not getattr(settings, 'QUIDAX_SECRET_KEY', ''):
        logger.info('No Quidax key — sell order %s queued for admin', order.reference)
        return

    _log(order, 'quidax_request_sent', {
        'market': SUPPORTED_COINS[order.coin],
        'side': 'sell',
        'volume': str(coin_amount),
    })
    try:
        result = create_instant_order(
            side='sell',
            market=SUPPORTED_COINS[order.coin],
            volume=str(coin_amount),
        )
        quidax_id = str(result.get('id', ''))
        quidax_status = str(result.get('status', ''))
        _log(order, 'quidax_response_received', {
            'order_id': quidax_id, 'status': quidax_status,
        })

        order.quidax_order_id = quidax_id
        if quidax_status in ('done', 'completed', 'filled'):
            _deduct_reserved(order.user, order.coin, coin_amount)
            _credit_ngn_wallet(order.user, order.total_ngn)
            order.status = CryptoOrder.Status.COMPLETED
            _log(order, 'order_completed', {'payout_ngn': str(order.total_ngn)})
        else:
            order.status = CryptoOrder.Status.PROCESSING

        order.save(update_fields=['quidax_order_id', 'status', 'updated_at'])

    except QuidaxError as exc:
        logger.error('Quidax sell failed for %s: %s', order.reference, exc)
        _log(order, 'quidax_error', {'error': str(exc)})
        _release_reserved(order.user, order.coin, coin_amount)
        order.status = CryptoOrder.Status.FAILED
        order.save(update_fields=['status', 'updated_at'])


def _execute_swap(order: CryptoOrder):
    """
    Execute a swap by selling from_coin for NGN, then buying to_coin with NGN.
    Deducts source balance and credits destination on success.
    Releases reserve on failure.
    """
    coin_amount = order.coin_amount

    if not getattr(settings, 'QUIDAX_SECRET_KEY', ''):
        logger.info('No Quidax key — swap order %s queued for admin', order.reference)
        return

    try:
        # Leg 1: sell from_coin → NGN
        _log(order, 'quidax_sell_sent', {'coin': order.coin, 'amount': str(coin_amount)})
        sell_result = create_instant_order(
            side='sell',
            market=SUPPORTED_COINS[order.coin],
            volume=str(coin_amount),
        )
        _log(order, 'quidax_sell_received', {
            'id': sell_result.get('id'), 'status': sell_result.get('status'),
        })

        # Leg 2: buy to_coin with NGN
        _log(order, 'quidax_buy_sent', {'coin': order.to_coin, 'amount': str(order.to_coin_amount)})
        buy_result = create_instant_order(
            side='buy',
            market=SUPPORTED_COINS[order.to_coin],
            volume=str(order.to_coin_amount),
        )
        _log(order, 'quidax_buy_received', {
            'id': buy_result.get('id'), 'status': buy_result.get('status'),
        })

        # Settle balances
        _deduct_reserved(order.user, order.coin, coin_amount)
        _credit_wallet(order.user, order.to_coin, order.to_coin_amount)

        order.status = CryptoOrder.Status.COMPLETED
        order.quidax_order_id = str(buy_result.get('id', ''))
        order.save(update_fields=['status', 'quidax_order_id', 'updated_at'])
        _log(order, 'order_completed', {
            'from': f'{coin_amount} {order.coin}',
            'to': f'{order.to_coin_amount} {order.to_coin}',
        })

    except QuidaxError as exc:
        logger.error('Quidax swap failed for %s: %s', order.reference, exc)
        _log(order, 'quidax_error', {'error': str(exc)})
        _release_reserved(order.user, order.coin, coin_amount)
        order.status = CryptoOrder.Status.FAILED
        order.save(update_fields=['status', 'updated_at'])


def _execute_buy_after_payment(order: CryptoOrder):
    """Called by admin action after payment is confirmed. Buys on Quidax and credits wallet."""
    if not getattr(settings, 'QUIDAX_SECRET_KEY', ''):
        logger.info('No Quidax key — buy order %s queued for manual execution', order.reference)
        return

    coin_amount = order.coin_amount
    _log(order, 'quidax_buy_sent', {'coin': order.coin, 'amount': str(coin_amount)})
    try:
        result = create_instant_order(
            side='buy',
            market=SUPPORTED_COINS[order.coin],
            volume=str(coin_amount),
        )
        quidax_id = str(result.get('id', ''))
        quidax_status = str(result.get('status', ''))
        _log(order, 'quidax_buy_received', {'order_id': quidax_id, 'status': quidax_status})

        order.quidax_order_id = quidax_id
        if quidax_status in ('done', 'completed', 'filled'):
            _credit_wallet(order.user, order.coin, coin_amount)
            order.status = CryptoOrder.Status.COMPLETED
            _log(order, 'order_completed', {'coin_amount': str(coin_amount), 'coin': order.coin})
        else:
            order.status = CryptoOrder.Status.PROCESSING

        order.save(update_fields=['quidax_order_id', 'status', 'updated_at'])

    except QuidaxError as exc:
        logger.error('Quidax buy failed for %s: %s', order.reference, exc)
        _log(order, 'quidax_error', {'error': str(exc)})
        order.status = CryptoOrder.Status.FAILED
        order.save(update_fields=['status', 'updated_at'])


def _credit_ngn_wallet(user, amount: Decimal):
    """Credit the user's NGN fiat wallet after a successful sell."""
    from wallet.models import Wallet
    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(
            user=user, defaults={'ngn_balance': Decimal('0')},
        )
        Wallet.objects.filter(pk=wallet.pk).update(
            ngn_balance=F('ngn_balance') + amount,
        )


# ── Response serialisers ──────────────────────────────────────────────────────

def _buy_response(order: CryptoOrder) -> dict:
    return {
        'reference': order.reference,
        'coin': order.coin,
        'coin_amount': str(order.coin_amount),
        'rate_ngn': str(order.rate_ngn),
        'fee_ngn': str(order.fee_ngn),
        'total_ngn': str(order.total_ngn),
        'payment_url': order.flw_payment_url or None,
        'bank_details': _AXIRA_BANK if not order.flw_payment_url else None,
        'status': order.status,
    }


def _sell_response(order: CryptoOrder, deposit_address: str = '') -> dict:
    return {
        'reference': order.reference,
        'coin': order.coin,
        'coin_amount': str(order.coin_amount),
        'rate_ngn': str(order.rate_ngn),
        'fee_ngn': str(order.fee_ngn),
        'payout_ngn': str(order.total_ngn),
        'deposit_address': deposit_address,
        'status': order.status,
    }


def _swap_response(order: CryptoOrder) -> dict:
    return {
        'reference': order.reference,
        'from_coin': order.coin,
        'to_coin': order.to_coin,
        'coin_amount': str(order.coin_amount),
        'to_coin_amount': str(order.to_coin_amount),
        'fee_ngn': str(order.fee_ngn),
        'status': order.status,
    }
