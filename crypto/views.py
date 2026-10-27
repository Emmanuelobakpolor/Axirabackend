import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    CryptoDeposit,
    CryptoDepositAddress,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoOrderLog,
    CryptoQuote,
    CryptoWallet,
    CryptoWithdrawal,
    CryptoWithdrawalLog,
    CryptoSweep,
)
from .quidax import (
    QuidaxError,
    create_deposit_address,
    create_instant_order,
    create_withdrawal,
    get_all_tickers,
    get_default_network,
    get_master_uid,
    get_wallet,
    get_instant_order,
    get_withdrawal,
    list_deposit_addresses,
    list_deposits,
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


# ── Withdrawal address validation ───────────────────────────────────────────
# A first line of defense against obvious typos/malformed addresses before
# money-losing Quidax call — Quidax itself is the authoritative validator,
# this just catches the cheap, common mistakes early. Keyed by network first
# (multi-chain tokens like USDT/USDC ride ERC20/BEP20/TRC20/SOL), falling
# back to the coin itself for single-network coins.
_NETWORK_ADDRESS_PATTERNS = {
    'ERC20':    r'^0x[a-fA-F0-9]{40}$',
    'BEP20':    r'^0x[a-fA-F0-9]{40}$',
    'POLYGON':  r'^0x[a-fA-F0-9]{40}$',
    'TRC20':    r'^T[1-9A-HJ-NP-Za-km-z]{33}$',
    'SOL':      r'^[1-9A-HJ-NP-Za-km-z]{32,44}$',
}
_COIN_ADDRESS_PATTERNS = {
    'BTC':  r'^(bc1[a-zA-HJ-NP-Z0-9]{25,39}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$',
    'ETH':  r'^0x[a-fA-F0-9]{40}$',
    'BNB':  r'^0x[a-fA-F0-9]{40}$',
    'XRP':  r'^r[1-9A-HJ-NP-Za-km-z]{24,34}$',
    'LTC':  r'^(ltc1[a-zA-HJ-NP-Z0-9]{25,39}|[LM3][a-km-zA-HJ-NP-Z1-9]{25,34})$',
    'DOGE': r'^D[5-9A-HJ-NP-U][1-9A-HJ-NP-Za-km-z]{32}$',
    'TRX':  r'^T[1-9A-HJ-NP-Za-km-z]{33}$',
    'SOL':  r'^[1-9A-HJ-NP-Za-km-z]{32,44}$',
    'ADA':  r'^addr1[a-z0-9]{50,}$',
    'DASH': r'^X[1-9A-HJ-NP-Za-km-z]{33}$',
}
_GENERIC_ADDRESS_RE = re.compile(r'^\S{20,100}$')


def _validate_withdrawal_address(coin: str, network: str, address: str) -> bool:
    if not address or any(ch.isspace() for ch in address):
        return False
    pattern = _NETWORK_ADDRESS_PATTERNS.get(network.upper()) or _COIN_ADDRESS_PATTERNS.get(coin.upper())
    if pattern:
        return bool(re.match(pattern, address))
    return bool(_GENERIC_ADDRESS_RE.match(address))


QUOTE_TTL_SECONDS = 30

# Quidax rejects any order (or swap leg) worth less than this with error
# 110112 "Price is below allowed minimum buy price."
QUIDAX_MIN_NGN_ORDER = Decimal('1000')

# Withdrawals execute instantly with no admin approval step, so this is the
# only brake on a compromised session draining funds in one session.
CRYPTO_WITHDRAW_DAILY_LIMIT_NGN = Decimal(
    str(getattr(settings, 'CRYPTO_WITHDRAW_DAILY_LIMIT_NGN', '2000000'))
)

_NGN_PER_USD = Decimal(str(getattr(settings, 'NGN_PER_USD', '1600')))


def _load_coin_amounts(setting_name: str) -> dict:
    raw = getattr(settings, setting_name, '{}') or '{}'
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return {str(k).upper(): Decimal(str(v)) for k, v in parsed.items()}
    except (ValueError, TypeError, InvalidOperation):
        logger.error('%s is not valid JSON: %r', setting_name, raw)
        return {}


# Quidax's per-coin minimum withdrawal amounts (see settings.py).
CRYPTO_WITHDRAW_MIN_AMOUNTS = _load_coin_amounts('CRYPTO_WITHDRAW_MIN_AMOUNTS')

# Quidax's per-withdrawal on-chain fee, by coin or "COIN:NETWORK".
CRYPTO_WITHDRAW_NETWORK_FEES = _load_coin_amounts('CRYPTO_WITHDRAW_NETWORK_FEES')


def _withdraw_network_fee(coin: str, network: str) -> Decimal:
    """
    The fee to deduct from a customer's withdrawal so Axira doesn't pay
    Quidax's on-chain fee. Quidax takes that fee from the master account on
    top of the amount sent, so whatever isn't deducted here comes out of
    Axira's own funds. Uses the higher of the configured fee and the last
    fee Quidax actually charged, so a fee increase is picked up
    automatically once one withdrawal has gone through at the new rate.
    """
    coin, network = coin.upper(), (network or '').upper()
    configured = (
        CRYPTO_WITHDRAW_NETWORK_FEES.get(f'{coin}:{network}')
        or CRYPTO_WITHDRAW_NETWORK_FEES.get(coin)
        or Decimal('0')
    )
    last = (
        CryptoWithdrawal.objects
        .filter(coin=coin, network=network, fee__gt=0)
        .order_by('-created_at')
        .values_list('fee', flat=True)
        .first()
    ) or Decimal('0')
    fee = max(configured, last)
    if fee <= 0:
        logger.warning("No network fee known for %s/%s — Axira will pay Quidax's fee.", coin, network)
    return fee


def _network_fee_table() -> dict:
    """
    Network fees for the app to preview, keyed "COIN:NETWORK" and "COIN"
    (the app sends no network for single-chain coins). Display only — the
    withdraw view works out the exact fee itself via _withdraw_network_fee.
    """
    table = dict(CRYPTO_WITHDRAW_NETWORK_FEES)
    seen = set()
    for coin, network, fee in (
        CryptoWithdrawal.objects
        .filter(fee__gt=0)
        .order_by('-created_at')
        .values_list('coin', 'network', 'fee')
    ):
        key = f'{coin}:{network}'
        if key in seen:
            continue  # only the latest fee per coin/network counts
        seen.add(key)
        table[key] = max(table.get(key, Decimal('0')), fee)
        table[coin] = max(table.get(coin, Decimal('0')), fee)
    return table


def _fmt_coin(amount: Decimal) -> str:
    return format(amount.normalize(), 'f')


def _friendly_order_error(action: str, coin: str, raw: str) -> str:
    """
    Customer-facing text for a failed Quidax buy/sell/swap. As with
    withdrawals, "insufficient balance" from Quidax is about Axira's
    account, never the customer's, so it must not be shown as-is.
    """
    msg = (raw or '').lower()
    if 'insufficient' in msg or 'balance' in msg:
        return (
            f"We couldn't complete your {coin} {action} right now because of a "
            'temporary issue on our side. Please try again shortly or contact support.'
        )
    if 'minimum' in msg or 'below' in msg:
        return f'This amount is below the minimum {coin} {action} size. Please enter a higher amount.'
    return f'Your {coin} {action} could not be completed. Please try again or contact support.'


def _order_error_detail(order: 'CryptoOrder', action: str) -> str:
    last_error = order.logs.filter(event='quidax_error').order_by('-created_at').first()
    raw = last_error.detail.get('error', '') if last_error else ''
    return _friendly_order_error(action, order.coin, raw)


def _friendly_withdraw_error(coin: str, raw: str) -> str:
    """
    Turn a raw Quidax withdrawal error into something a customer can act on.
    Raw errors describe Axira's Quidax account, not the customer's wallet:
    "Insufficient account balance" means our account can't cover the
    transfer, even though the customer's own balance is fine.
    """
    msg = (raw or '').lower()
    if 'insufficient' in msg or 'balance' in msg:
        return (
            f'{coin} transfers are temporarily unavailable on our side. '
            f'Your {coin} balance is safe and has not been touched. '
            'Please try again later or contact support.'
        )
    if 'minimum' in msg:
        min_amt = CRYPTO_WITHDRAW_MIN_AMOUNTS.get(coin)
        if min_amt:
            return f'The minimum {coin} transfer is {_fmt_coin(min_amt)} {coin}. Please enter a higher amount.'
        return f'This amount is below the minimum {coin} transfer allowed on the network. Please enter a higher amount.'
    if 'address' in msg:
        return "The receiving wallet address was rejected. Please check it and try again."
    if 'network' in msg or 'disabled' in msg or 'suspend' in msg:
        return f'{coin} transfers on this network are temporarily unavailable. Please try again later.'
    return 'Transfer failed. Your balance has not been charged. Please try again or contact support.'

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
        order = CryptoOrder.objects.get(
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

    order = _claim_flw_payment(order, flw_tx_id)
    if order is None:
        logger.info('FLW crypto webhook: %s already claimed by another path', tx_ref)
        return
    _log(order, 'flw_payment_confirmed', {
        'flw_tx_id': flw_tx_id,
        'amount_paid': str(amount_paid),
    })

    _execute_buy_after_payment(order, refund_on_failure=False)


def _claim_flw_payment(order: CryptoOrder, flw_tx_id: str):
    """
    Atomically move a buy order from PENDING_PAYMENT to PAYMENT_RECEIVED.
    The webhook and the client verify endpoint can both fire for the same
    payment; only the caller that wins this conditional UPDATE may execute
    the Quidax buy. Returns the refreshed order, or None if already claimed.
    """
    claimed = CryptoOrder.objects.filter(
        pk=order.pk,
        status=CryptoOrder.Status.PENDING_PAYMENT,
    ).update(
        status=CryptoOrder.Status.PAYMENT_RECEIVED,
        flw_transaction_id=flw_tx_id,
        updated_at=timezone.now(),
    )
    if not claimed:
        return None
    order.refresh_from_db()
    return order

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


def _ngn_volume_str(ngn_amount: Decimal) -> str:
    """
    Quidax market buys are NGN-denominated, not crypto-denominated — per
    Quidax's own support docs: "For a market BUY, input the amount of NGN
    you want to transact" (market sells use crypto quantity instead, which
    is what str(coin_amount) already gives us elsewhere). NGN volumes only
    accept whole Naira, so this floors rather than rounds to guarantee we
    never send Quidax more NGN than was actually collected for the trade.
    """
    return str(ngn_amount.quantize(Decimal('1'), rounding=ROUND_DOWN))


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


def _wlog(withdrawal: CryptoWithdrawal, event: str, detail: dict = None) -> None:
    try:
        CryptoWithdrawalLog.log(withdrawal, event, detail)
    except Exception:
        logger.warning('Withdrawal audit log write failed: %s on %s', event, withdrawal.reference)


def _todays_withdrawal_ngn_total(user, tickers: dict) -> Decimal:
    """NGN-equivalent of everything the user has withdrawn (or has in
    flight) since midnight, valued at current rates — good enough for a
    same-day safety cap, doesn't need to match historical rates exactly."""
    since = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    qs = CryptoWithdrawal.objects.filter(
        user=user,
        created_at__gte=since,
        status__in=(CryptoWithdrawal.Status.PROCESSING, CryptoWithdrawal.Status.COMPLETED),
    )
    return sum(
        (w.amount * _resolve_price_ngn(w.coin, tickers) for w in qs),
        Decimal('0'),
    )


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


def _debit_ngn_wallet_if_sufficient(user, amount: Decimal) -> bool:
    """
    Atomically deduct amount from the user's NGN wallet if the balance
    covers it. Returns True if successful, False if insufficient (or no
    wallet exists yet) — mirrors _reserve_balance's conditional-update
    pattern for crypto wallets.
    """
    from wallet.models import Wallet
    with transaction.atomic():
        updated = Wallet.objects.filter(
            user=user, ngn_balance__gte=amount,
        ).update(ngn_balance=F('ngn_balance') - amount)
        return updated > 0


def _deduct_reserved(user, coin: str, amount: Decimal) -> None:
    """Permanently remove from reserved balance (on successful execution)."""
    CryptoWallet.objects.filter(user=user, coin=coin).update(
        reserved=F('reserved') - amount,
    )


def admin_settle_withdrawal(pk, action: str, admin_email: str) -> str:
    """
    Manually settle a withdrawal whose Quidax webhook never arrived — shared
    by the Django admin actions and the admin dashboard.

    action: 'complete' (coin confirmed sent on Quidax) or 'refund' (it never
    left). A PROCESSING withdrawal with no quidax_withdrawal_id never got
    Quidax's acceptance, so its amount is still in reserved rather than
    already deducted, and is settled from there. Returns a short outcome
    string; raises ValueError if the withdrawal can't take that action.
    """
    S = CryptoWithdrawal.Status
    with transaction.atomic():
        w = CryptoWithdrawal.objects.select_for_update().get(pk=pk)
        still_reserved = w.status == S.PROCESSING and not w.quidax_withdrawal_id

        if action == 'complete':
            if w.status == S.COMPLETED:
                raise ValueError('Withdrawal is already completed.')
            if w.status != S.PROCESSING:
                raise ValueError(f'A {w.status} withdrawal has already been refunded — it cannot be completed.')
            if still_reserved:
                _deduct_reserved(w.user, w.coin, w.amount)
            w.status = S.COMPLETED
            outcome = 'deducted_reserved' if still_reserved else 'already_deducted'
            event = 'withdrawal_completed_by_admin'

        elif action == 'refund':
            if w.status in (S.COMPLETED, S.REJECTED):
                raise ValueError(f'Withdrawal is already {w.status}.')
            if w.status == S.FAILED:
                outcome = 'none'  # released when it failed
            elif still_reserved:
                _release_reserved(w.user, w.coin, w.amount)
                outcome = 'released_reserved'
            else:
                _credit_wallet(w.user, w.coin, w.amount)
                outcome = 'credited_available'
            w.status = S.REJECTED
            event = 'withdrawal_rejected_refunded_by_admin'

        elif action == 'undo':
            return _undo_admin_settlement(w, admin_email)

        else:
            raise ValueError('action must be "complete", "refund" or "undo".')

        w.save(update_fields=['status', 'updated_at'])
        _wlog(w, event, {'admin': admin_email, 'outcome': outcome})
    return outcome


ADMIN_SETTLE_UNDO_WINDOW = timedelta(hours=24)


def withdrawal_undoable(w: CryptoWithdrawal) -> bool:
    """Whether the dashboard should offer Undo — mirrors _undo_admin_settlement's checks."""
    if w.status not in (CryptoWithdrawal.Status.COMPLETED, CryptoWithdrawal.Status.REJECTED):
        return False
    last = w.logs.order_by('-created_at').first()
    return bool(
        last
        and last.event in ('withdrawal_completed_by_admin', 'withdrawal_rejected_refunded_by_admin')
        and 'outcome' in (last.detail or {})
        and timezone.now() - last.created_at <= ADMIN_SETTLE_UNDO_WINDOW
    )


def _undo_admin_settlement(w: CryptoWithdrawal, admin_email: str) -> str:
    """
    Reverse the most recent admin_settle_withdrawal on w (caller holds the
    row lock). Only an admin's own settlement can be undone — never a
    Quidax webhook result — and only within ADMIN_SETTLE_UNDO_WINDOW, using
    the outcome recorded in the log so the exact balance move is reversed.
    """
    S = CryptoWithdrawal.Status
    last = w.logs.order_by('-created_at').first()
    settle_events = ('withdrawal_completed_by_admin', 'withdrawal_rejected_refunded_by_admin')
    if not last or last.event not in settle_events or 'outcome' not in (last.detail or {}):
        raise ValueError('Only a send an admin just marked sent or refunded can be undone.')
    if timezone.now() - last.created_at > ADMIN_SETTLE_UNDO_WINDOW:
        raise ValueError('Too late to undo — this was settled more than 24 hours ago. Fix it manually.')

    outcome = last.detail['outcome']
    wallets = CryptoWallet.objects.filter(user=w.user, coin=w.coin)

    if last.event == 'withdrawal_completed_by_admin':
        if w.status != S.COMPLETED:
            raise ValueError('Withdrawal is no longer completed.')
        if outcome == 'deducted_reserved':
            wallets.update(reserved=F('reserved') + w.amount)
        restored = S.PROCESSING

    else:
        if w.status != S.REJECTED:
            raise ValueError('Withdrawal is no longer rejected.')
        if outcome in ('released_reserved', 'credited_available'):
            # The refund may already have been spent — never go negative.
            moved = wallets.filter(available__gte=w.amount).update(
                available=F('available') - w.amount,
                **({'reserved': F('reserved') + w.amount} if outcome == 'released_reserved' else {}),
            )
            if not moved:
                raise ValueError('The user has already spent part of the refund — cannot undo. Fix it manually.')
        restored = S.FAILED if outcome == 'none' else S.PROCESSING

    w.status = restored
    w.save(update_fields=['status', 'updated_at'])
    _wlog(w, 'admin_settlement_undone', {
        'admin': admin_email, 'undid': last.event, 'outcome': outcome, 'restored_status': restored,
    })
    return f'restored_{restored}'


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

    def _match(addresses):
        for a in addresses:
            if not a.get('address'):
                continue
            if network and (a.get('network') or '').upper() != network.upper():
                continue
            return a
        return None

    try:
        match = _match(list_deposit_addresses(user.quidax_user_id, coin))

        if not match:
            # No address yet — trigger generation and briefly poll, since
            # Quidax creates it asynchronously (wallet.address.generated).
            create_deposit_address(user.quidax_user_id, coin, network or None)
            for _ in range(4):
                time.sleep(1.5)
                match = _match(list_deposit_addresses(user.quidax_user_id, coin))
                if match:
                    break

        if not match:
            raise ValueError(
                'Your deposit address is still being generated. '
                'Please try again in a few seconds.'
            )

        address = match.get('address', '')
        CryptoDepositAddress.objects.get_or_create(
            user=user,
            coin=coin,
            network=network,
            defaults={
                'address': address,
                'quidax_ref': str(match.get('id', '')),
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


def _withdrawal_response(w: CryptoWithdrawal) -> dict:
    resp = {
        'reference': w.reference,
        'coin': w.coin,
        'network': w.network,
        'address': w.address,
        'amount': str(w.amount),
        'network_fee': str(w.network_fee),
        'receive_amount': str(w.amount - w.network_fee),
        'fee': str(w.fee),
        'status': w.status,
        'tx_id': w.tx_id,
        'created_at': w.created_at.isoformat(),
    }
    if w.status == CryptoWithdrawal.Status.FAILED:
        resp['error_detail'] = w.note or 'Withdrawal failed. Please try again or contact support.'
    return resp


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


def _resolve_price_usd(coin: str, tickers: dict, price_ngn: Decimal, ngn_per_usd: Decimal) -> Decimal:
    """USD price: the coin/USDT market when one exists, else derived from NGN."""
    if coin in ('USDT', 'USDC'):
        return Decimal('1')
    direct = _ticker_last(tickers, f'{coin.lower()}usdt')
    if direct > 0:
        return direct
    if price_ngn > 0 and ngn_per_usd > 0:
        return (price_ngn / ngn_per_usd).quantize(Decimal('0.00000001'))
    return Decimal('0')


class CryptoPricesView(APIView):
    """Live prices + coin metadata. Public — no auth required."""
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            tickers = get_all_tickers()
        except QuidaxError as exc:
            logger.error('Quidax ticker fetch failed: %s', exc)
            return Response({'error': str(exc)}, status=502)

        # Live USDT/NGN as the dollar rate; the configured rate is only a fallback.
        ngn_per_usd = _ticker_last(tickers, 'usdtngn')
        if ngn_per_usd <= 0:
            ngn_per_usd = _NGN_PER_USD

        coins, prices = [], {}
        for coin in SUPPORTED_COINS:
            price_ngn = _resolve_price_ngn(coin, tickers)
            prices[coin] = str(price_ngn)
            meta = COIN_META.get(coin, {})
            coins.append({
                'symbol': coin,
                'name': meta.get('name', coin),
                'color': meta.get('color', '#888888'),
                'letter': meta.get('letter', coin[0]),
                'logo_url': _logo_url(coin),
                'price_ngn': str(price_ngn),
                'price_usd': str(_resolve_price_usd(coin, tickers, price_ngn, ngn_per_usd)),
            })

        return Response({'prices': prices, 'coins': coins, 'ngn_per_usd': str(ngn_per_usd)})


class CryptoFeesView(APIView):
    """Current fee config — public, Flutter uses for estimate display only."""
    permission_classes = [AllowAny]

    def get(self, request):
        fees = {}
        for ft in ('buy', 'sell', 'swap'):
            f = _get_or_create_fee(ft)
            fees[ft] = {'flat_usd': str(f.flat_usd), 'percent': str(f.percent)}
        return Response({'fees': fees})


class CryptoLimitsView(APIView):
    """
    Minimums and the daily transfer cap, so the app can tell customers the
    limits up front instead of after a rejected order.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        used_today = Decimal('0')
        try:
            used_today = _todays_withdrawal_ngn_total(request.user, get_all_tickers())
        except QuidaxError as exc:
            logger.warning('Limits: ticker fetch failed, reporting 0 used today: %s', exc)
        remaining = max(CRYPTO_WITHDRAW_DAILY_LIMIT_NGN - used_today, Decimal('0'))
        return Response({
            'min_order_ngn': str(QUIDAX_MIN_NGN_ORDER),
            'withdraw_min_ngn': str(QUIDAX_MIN_NGN_ORDER),
            'withdraw_daily_limit_ngn': str(CRYPTO_WITHDRAW_DAILY_LIMIT_NGN),
            'withdraw_remaining_today_ngn': str(remaining.quantize(Decimal('0.01'))),
            'withdraw_min_amounts': {
                coin: _fmt_coin(amt) for coin, amt in CRYPTO_WITHDRAW_MIN_AMOUNTS.items()
            },
            'withdraw_network_fees': {
                key: _fmt_coin(fee) for key, fee in _network_fee_table().items()
            },
        })


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

        # Only enforce Quidax's own order minimum when a quote will actually
        # be executed on Quidax — orders queued for manual admin handling
        # (no QUIDAX_SECRET_KEY) never hit this limit.
        quidax_live = bool(getattr(settings, 'QUIDAX_SECRET_KEY', ''))
        if quidax_live and ngn_value < QUIDAX_MIN_NGN_ORDER:
            return Response(
                {'error': f'Minimum order value is ₦{QUIDAX_MIN_NGN_ORDER:,.0f}.'},
                status=400,
            )

        if quote_type == 'buy':
            total_ngn = ngn_value + fee_ngn
            to_coin_amount = Decimal('0')
        elif quote_type == 'sell':
            total_ngn = max(ngn_value - fee_ngn, Decimal('0'))  # payout
            to_coin_amount = Decimal('0')
        else:  # swap
            net_ngn = ngn_value - fee_ngn
            if net_ngn <= 0 or (quidax_live and net_ngn < QUIDAX_MIN_NGN_ORDER):
                return Response(
                    {'error': f'Amount too small — minimum is ₦{QUIDAX_MIN_NGN_ORDER:,.0f} after fees.'},
                    status=400,
                )
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
        # Pick up deposits the webhook missed before reporting balances.
        _sync_deposits(request.user)
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


class CryptoWithdrawView(APIView):
    """
    Send crypto to an external address. Executes immediately against Quidax
    — there is no admin approval step, so address validation, the minimum
    order value, the daily cap, and balance reservation below are the only
    guard before funds leave custody.

    POST body: { coin, network (optional), address, amount, idempotency_key }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ikey = str(request.data.get('idempotency_key', '')).strip()
        if ikey:
            existing = CryptoWithdrawal.objects.filter(
                idempotency_key=ikey, user=request.user,
            ).first()
            if existing:
                return Response(_withdrawal_response(existing), status=200)

        coin = str(request.data.get('coin', '')).upper().strip()
        if coin not in SUPPORTED_COINS:
            return Response({'error': f'Unsupported coin: {coin}'}, status=400)

        network = str(request.data.get('network', '')).upper().strip()
        address = str(request.data.get('address', '')).strip()

        try:
            amount = _parse_decimal(request.data.get('amount'), 'amount')
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        # Quidax rejects withdrawals without a network ("Network is required").
        # The app only shows a chain picker for multi-chain tokens, so resolve
        # the coin's default chain here — before reserving any balance.
        if not network:
            try:
                network = get_default_network(coin).upper()
            except QuidaxError as exc:
                logger.error('Default network lookup failed for %s: %s', coin, exc)
            if not network:
                return Response(
                    {'error': f'{coin} withdrawals are temporarily unavailable. Please try again later.'},
                    status=502,
                )

        if not _validate_withdrawal_address(coin, network, address):
            return Response(
                {'error': "That address doesn't look valid for this coin/network."},
                status=400,
            )

        try:
            tickers = get_all_tickers()
        except QuidaxError:
            return Response({'error': f'Unable to fetch live price for {coin}.'}, status=502)

        rate_ngn = _resolve_price_ngn(coin, tickers)
        if rate_ngn <= 0:
            return Response({'error': f'Unable to fetch live price for {coin}.'}, status=502)

        # The network fee comes out of the amount: the customer is charged
        # `amount`, the recipient gets `send_amount`.
        network_fee = _withdraw_network_fee(coin, network)
        send_amount = (amount - network_fee).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        if send_amount <= 0:
            return Response(
                {'error': f'The amount must be more than the {_fmt_coin(network_fee)} {coin} network fee.'},
                status=400,
            )

        coin_min = CRYPTO_WITHDRAW_MIN_AMOUNTS.get(coin)
        if coin_min and send_amount < coin_min:
            return Response(
                {'error': f'The minimum {coin} transfer is {_fmt_coin(coin_min + network_fee)} {coin} '
                          f'({_fmt_coin(coin_min)} {coin} plus the {_fmt_coin(network_fee)} {coin} network fee). '
                          'Please enter a higher amount.'},
                status=400,
            )

        ngn_value = amount * rate_ngn
        if ngn_value < QUIDAX_MIN_NGN_ORDER:
            min_coin = (QUIDAX_MIN_NGN_ORDER / rate_ngn).quantize(Decimal('0.00000001'))
            return Response(
                {'error': f'The minimum transfer is ₦{QUIDAX_MIN_NGN_ORDER:,.0f} '
                          f'(about {_fmt_coin(min_coin)} {coin}). Please enter a higher amount.'},
                status=400,
            )

        todays_total = _todays_withdrawal_ngn_total(request.user, tickers)
        if todays_total + ngn_value > CRYPTO_WITHDRAW_DAILY_LIMIT_NGN:
            remaining = max(CRYPTO_WITHDRAW_DAILY_LIMIT_NGN - todays_total, Decimal('0'))
            return Response(
                {'error': f'Your daily transfer limit is ₦{CRYPTO_WITHDRAW_DAILY_LIMIT_NGN:,.0f}. '
                          f'You can send up to ₦{remaining:,.0f} more today.'},
                status=400,
            )

        if not _reserve_balance(request.user, coin, amount):
            return Response({'error': f'Insufficient {coin} balance.'}, status=400)

        withdrawal = CryptoWithdrawal.objects.create(
            user=request.user,
            coin=coin,
            network=network,
            address=address,
            amount=amount,
            network_fee=network_fee,
            idempotency_key=ikey or None,
        )
        _wlog(withdrawal, 'withdrawal_created', {
            'coin': coin, 'network': network, 'address': address, 'amount': str(amount),
            'network_fee': str(network_fee), 'send_amount': str(send_amount),
        })

        try:
            result = create_withdrawal(
                currency=coin,
                amount=str(send_amount),
                address=address,
                network=network,
                reference=withdrawal.reference,
            )
            withdrawal.quidax_withdrawal_id = str(result.get('id', ''))
            fee = result.get('fee')
            if fee is not None:
                try:
                    withdrawal.fee = Decimal(str(fee))
                except InvalidOperation:
                    pass
            withdrawal.save(update_fields=['quidax_withdrawal_id', 'fee', 'updated_at'])
            _wlog(withdrawal, 'quidax_withdrawal_sent', {
                'id': withdrawal.quidax_withdrawal_id, 'fee': str(withdrawal.fee),
            })

            # Quidax has accepted and is already processing the transfer —
            # treat the reserved balance as gone, matching how a Quidax-
            # accepted buy/sell is treated as committed elsewhere in this file.
            _deduct_reserved(request.user, coin, amount)

        except QuidaxError as exc:
            error_msg = str(exc)
            logger.error('Quidax withdrawal failed for %s: %s', withdrawal.reference, error_msg)
            _wlog(withdrawal, 'quidax_error', {'error': error_msg})

            if 'insufficient' in error_msg.lower():
                # The customer's ledger balance covered this, so Axira's
                # Quidax account is short — ops needs to top it up.
                logger.critical(
                    'Quidax account cannot cover %s %s withdrawal %s — top up the Quidax %s balance.',
                    amount, coin, withdrawal.reference, coin,
                )

            _release_reserved(request.user, coin, amount)
            withdrawal.status = CryptoWithdrawal.Status.FAILED
            # The raw Quidax error stays in the withdrawal log above.
            withdrawal.note = _friendly_withdraw_error(coin, error_msg)
            withdrawal.save(update_fields=['status', 'note', 'updated_at'])

        return Response(_withdrawal_response(withdrawal), status=201)


# ── Withdrawal settlement (shared by the webhook and status polling) ─────────

_QUIDAX_WITHDRAW_DONE = {'done', 'successful', 'success', 'completed'}
_QUIDAX_WITHDRAW_FAILED = {'rejected', 'failed', 'cancelled', 'canceled'}

# Cap on Quidax lookups per history request so a user with many stuck
# withdrawals can't make the endpoint slow.
_WITHDRAW_SYNC_MAX = 5


def _complete_withdrawal(pk, tx_id: str = '', fee=None, via: str = 'webhook') -> None:
    with transaction.atomic():
        w = CryptoWithdrawal.objects.select_for_update().get(pk=pk)
        if w.status != CryptoWithdrawal.Status.PROCESSING:
            return
        w.status = CryptoWithdrawal.Status.COMPLETED
        if tx_id:
            w.tx_id = tx_id
        if fee is not None:
            try:
                w.fee = Decimal(str(fee))
            except InvalidOperation:
                pass
        w.save(update_fields=['status', 'tx_id', 'fee', 'updated_at'])
        _wlog(w, 'withdrawal_completed', {'tx_id': w.tx_id, 'via': via})


def _reject_withdrawal(pk, reason: str, via: str = 'webhook') -> None:
    """
    Quidax initially accepted the withdrawal (we already deducted the
    reserved balance as committed) but the transfer ultimately failed —
    credit the amount back since it never actually left custody.
    """
    with transaction.atomic():
        w = CryptoWithdrawal.objects.select_for_update().get(pk=pk)
        if w.status != CryptoWithdrawal.Status.PROCESSING:
            return
        w.status = CryptoWithdrawal.Status.REJECTED
        w.note = reason or 'Rejected by Quidax.'
        w.save(update_fields=['status', 'note', 'updated_at'])
        _credit_wallet(w.user, w.coin, w.amount)
        _wlog(w, 'withdrawal_rejected_refunded', {'amount': str(w.amount), 'via': via})


def _quidax_tx_id(data: dict) -> str:
    return str(data.get('txid') or data.get('txId') or data.get('tx_id') or '')


def _sync_withdrawal(w: CryptoWithdrawal) -> CryptoWithdrawal:
    """
    Ask Quidax for the current state of a PROCESSING withdrawal and settle
    it locally — the fallback for when the withdraw.* webhook never arrives.
    """
    if w.status != CryptoWithdrawal.Status.PROCESSING or not w.quidax_withdrawal_id:
        return w
    try:
        data = get_withdrawal(w.quidax_withdrawal_id)
    except QuidaxError as exc:
        logger.warning('Withdrawal status sync failed for %s: %s', w.reference, exc)
        return w

    q_status = str(data.get('status', '')).strip().lower()
    if q_status in _QUIDAX_WITHDRAW_DONE:
        _complete_withdrawal(w.pk, _quidax_tx_id(data), data.get('fee'), via='status_sync')
    elif q_status in _QUIDAX_WITHDRAW_FAILED:
        reason = str(data.get('reason') or data.get('message') or f'{q_status.capitalize()} by Quidax.')
        _reject_withdrawal(w.pk, reason, via='status_sync')
    else:
        # Still in flight, but pick up the tx hash once Quidax has broadcast it.
        tx_id = _quidax_tx_id(data)
        if tx_id and tx_id != w.tx_id:
            CryptoWithdrawal.objects.filter(pk=w.pk).update(tx_id=tx_id)
    return CryptoWithdrawal.objects.get(pk=w.pk)


# ── Deposit crediting (shared by the webhook and Quidax polling) ─────────────

# Quidax states for a deposit that has landed in the sub-account.
_QUIDAX_DEPOSIT_DONE = {'accepted', 'done', 'successful', 'success', 'completed'}
_QUIDAX_DEPOSIT_FAILED = {'rejected', 'failed', 'cancelled', 'canceled'}

# Min seconds between Quidax deposit lookups per user, so opening the
# wallet/history screens repeatedly doesn't hammer the Quidax API.
_DEPOSIT_SYNC_INTERVAL = 30


def _record_deposit(user, data: dict, via: str) -> bool:
    """
    Credit one confirmed Quidax deposit to the user's CryptoWallet, at most
    once (keyed on the Quidax deposit id). Returns True if newly credited.
    """
    raw_amount = str(data.get('amount', '0'))
    currency = str(data.get('currency', '')).upper()
    network = str(data.get('network', '') or '').upper()

    if currency not in SUPPORTED_COINS:
        logger.warning('Deposit (%s): unsupported currency=%s for %s', via, currency, user.email)
        return False

    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        logger.warning('Deposit (%s): invalid amount %s', via, raw_amount)
        return False
    if amount <= 0:
        return False

    # Quidax re-delivers webhooks it thinks failed, and polling sees the same
    # deposits every time, so each deposit must be credited at most once.
    # Without its id there's no way to tell a repeat from a new deposit —
    # leave those for an admin to credit.
    deposit_id = str(data.get('id', '')).strip()
    tx_id = _quidax_tx_id(data).strip()
    if not deposit_id:
        logger.error(
            'Deposit (%s) without id — NOT credited, credit manually: '
            '%s %s → %s (txid=%s)', via, amount, currency, user.email, tx_id,
        )
        return False

    try:
        with transaction.atomic():
            CryptoDeposit.objects.create(
                user=user,
                quidax_deposit_id=deposit_id,
                coin=currency,
                network=network,
                amount=amount,
                tx_id=tx_id,
            )
            _credit_wallet(user, currency, amount)
    except IntegrityError:
        logger.info('Deposit %s already credited — %s ignored', deposit_id, via)
        return False

    logger.info(
        'Deposit credited via %s: %s %s → %s (network=%s)',
        via, amount, currency, user.email, network,
    )

    # The credit above is already committed — never let a failure here
    # bubble up as a 500, or Quidax retries the webhook and credits twice.
    # Sweep first: waiting sells execute on the master account, so the
    # coins need to be there before they're sold.
    try:
        sweep_to_master(user, currency, network, via='deposit')
    except Exception:
        logger.exception('Sweep after deposit failed for %s %s', user.email, currency)
    try:
        _fulfil_waiting_sells(user, currency)
    except Exception:
        logger.exception('Failed to fulfil waiting sells for %s %s', user.email, currency)
    return True


def sweep_to_master(user, coin: str, network: str = '', via: str = '') -> CryptoSweep | None:
    """
    Move the user's whole available `coin` balance from their Quidax deposit
    sub-account into the master account, as an internal (off-chain) Quidax
    transfer. All sells and withdrawals run on the master account, so coins
    left in a sub-account can't be sold or sent anywhere.

    Moves the live sub-account balance rather than one deposit's amount, so
    a retry also picks up anything an earlier failed sweep left behind.
    Never touches CryptoWallet — the customer's ledger balance was already
    credited when the deposit was recorded. Returns the sweep record, or
    None if there was nothing to move.
    """
    if not getattr(settings, 'QUIDAX_SECRET_KEY', ''):
        return None
    sub_uid = str(getattr(user, 'quidax_user_id', '') or '')
    if not sub_uid:
        return None

    master_uid = get_master_uid()
    if not master_uid or master_uid == sub_uid:
        logger.error('Sweep skipped for %s: master Quidax uid unresolved (%r)', user.email, master_uid)
        return None

    wallet = get_wallet(sub_uid, coin) or {}
    try:
        balance = Decimal(str(wallet.get('balance') or '0'))
    except InvalidOperation:
        balance = Decimal('0')
    if balance <= 0:
        return None

    network = (network or '').lower() or str(wallet.get('default_network') or '').lower()
    if not network:
        network = get_default_network(coin, uid=sub_uid)

    sweep = CryptoSweep(
        user=user,
        coin=coin,
        network=network.upper(),
        amount=balance,
        reference=f'SWP{uuid.uuid4().hex[:20].upper()}',
        via=via,
    )
    try:
        result = create_withdrawal(
            currency=coin,
            amount=str(balance),
            address=master_uid,  # a Quidax user id → internal transfer
            network=network,
            reference=sweep.reference,
            uid=sub_uid,
        )
        sweep.quidax_withdrawal_id = str(result.get('id', ''))
        sweep.status = CryptoSweep.Status.SENT
        logger.info('Swept %s %s from %s to master (%s)', balance, coin, user.email, sweep.reference)
    except QuidaxError as exc:
        sweep.status = CryptoSweep.Status.FAILED
        sweep.error = str(exc)
        logger.error('Sweep of %s %s for %s failed: %s', balance, coin, user.email, exc)
    sweep.save()
    return sweep


def _sync_deposits(user) -> list:
    """
    Pull the user's deposits from Quidax and credit any confirmed ones the
    webhook missed. Only coins the user has a deposit address for are
    checked (they can't have received anything else). Returns deposits
    Quidax has seen but not yet confirmed, e.g. BTC awaiting confirmations.
    """
    if not user.quidax_user_id:
        return []

    from django.core.cache import cache
    cache_key = f'crypto_deposit_sync:{user.pk}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    coins = set(
        CryptoDepositAddress.objects.filter(user=user).values_list('coin', flat=True)
    )
    credited = set(
        CryptoDeposit.objects.filter(user=user).values_list('quidax_deposit_id', flat=True)
    )
    pending = []
    for coin in coins:
        try:
            deposits = list_deposits(user.quidax_user_id, coin)
        except QuidaxError as exc:
            logger.warning('Deposit sync failed for %s %s: %s', user.email, coin, exc)
            continue
        for d in deposits:
            if not isinstance(d, dict):
                continue
            q_status = str(d.get('status') or d.get('state') or '').strip().lower()
            if str(d.get('id', '')) in credited or q_status in _QUIDAX_DEPOSIT_FAILED:
                continue
            if q_status in _QUIDAX_DEPOSIT_DONE:
                _record_deposit(user, d, via='deposit_sync')
            else:
                pending.append({
                    'reference': str(d.get('id', '')),
                    'coin': str(d.get('currency', coin)).upper(),
                    'network': str(d.get('network', '') or '').upper(),
                    'amount': str(d.get('amount', '0')),
                    'status': 'pending',
                    'tx_id': _quidax_tx_id(d),
                    'created_at': str(d.get('created_at', '')),
                })

    cache.set(cache_key, pending, _DEPOSIT_SYNC_INTERVAL)
    return pending


def _deposit_response(d: CryptoDeposit) -> dict:
    return {
        'reference': d.quidax_deposit_id,
        'coin': d.coin,
        'network': d.network,
        'amount': str(d.amount),
        'status': 'completed',
        'tx_id': d.tx_id,
        'created_at': d.created_at.isoformat(),
    }


class CryptoDepositsView(APIView):
    """
    User's own crypto deposit history. Reconciles with Quidax first, so a
    deposit shows up (and is credited) even if the webhook never arrived.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pending = _sync_deposits(request.user)
        deposits = CryptoDeposit.objects.filter(user=request.user)[:50]
        return Response({
            'deposits': pending + [_deposit_response(d) for d in deposits],
        })


class CryptoWithdrawalsView(APIView):
    """User's own crypto withdrawal history."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        withdrawals = list(CryptoWithdrawal.objects.filter(user=request.user)[:50])
        synced = 0
        for i, w in enumerate(withdrawals):
            if synced >= _WITHDRAW_SYNC_MAX:
                break
            if w.status == CryptoWithdrawal.Status.PROCESSING and w.quidax_withdrawal_id:
                withdrawals[i] = _sync_withdrawal(w)
                synced += 1
        return Response({'withdrawals': [_withdrawal_response(w) for w in withdrawals]})


class CryptoWithdrawalDetailView(APIView):
    """Current status of one withdrawal — polled by the receipt screen."""
    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        w = CryptoWithdrawal.objects.filter(user=request.user, reference=reference).first()
        if not w:
            return Response({'error': 'Withdrawal not found.'}, status=404)
        return Response(_withdrawal_response(_sync_withdrawal(w)))


class CryptoBuyOrderView(APIView):
    """
    Place a buy order using a valid quote.

    If the user's NGN wallet balance covers the total, it's debited and the
    buy executes immediately — no Flutterwave involved. Otherwise, the
    response carries a Flutterwave public key + customer info for an in-app
    SDK charge (flutterwave_standard); the client must then POST the
    reference to /crypto/orders/buy/verify/ to confirm payment server-side
    before the buy executes. Falls back to manual bank transfer if
    Flutterwave isn't configured at all.

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

        # Pay instantly from the NGN wallet if the balance covers it.
        if _debit_ngn_wallet_if_sufficient(request.user, order.total_ngn):
            _log(order, 'paid_from_wallet', {'amount': str(order.total_ngn)})
            order.status = CryptoOrder.Status.PAYMENT_RECEIVED
            order.save(update_fields=['status', 'updated_at'])
            _execute_buy_after_payment(order)

        return Response(_buy_response(order), status=201)


class CryptoBuyVerifyView(APIView):
    """
    Confirm a Flutterwave in-app charge for a buy order, then execute it.

    Called by the client immediately after flutterwave_standard's SDK
    reports a successful charge. Verifies server-side with Flutterwave
    (status + amount) before crediting anything — mirrors
    wallet.views.WalletViewSet.verify_payment. The order's own reference
    doubles as the Flutterwave tx_ref.

    POST body: { reference }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        reference = str(request.data.get('reference', '')).strip()
        if not reference:
            return Response({'error': 'reference is required.'}, status=400)

        try:
            order = CryptoOrder.objects.get(
                reference=reference,
                user=request.user,
                order_type=CryptoOrder.OrderType.BUY,
            )
        except CryptoOrder.DoesNotExist:
            return Response({'error': 'Order not found.'}, status=404)

        if order.status == CryptoOrder.Status.COMPLETED:
            return Response(_buy_response(order))

        if order.status not in (
            CryptoOrder.Status.PENDING_PAYMENT,
            CryptoOrder.Status.PAYMENT_RECEIVED,
        ):
            return Response(
                {'error': f'Order cannot be verified from status "{order.status}".'},
                status=400,
            )

        from wallet.flutterwave import FlutterwaveError, verify_transaction

        try:
            result = verify_transaction(reference)
        except FlutterwaveError as exc:
            return Response({'error': str(exc)}, status=502)

        flw_data = result.get('data', {})
        flw_status = str(flw_data.get('status', ''))

        if result.get('status') != 'success' or flw_status != 'successful':
            _log(order, 'flw_verify_failed', {'flw_status': flw_status})
            return Response({'error': 'Payment not confirmed by Flutterwave.'}, status=400)

        try:
            flw_amount = Decimal(str(flw_data.get('amount', 0)))
        except InvalidOperation:
            flw_amount = Decimal('0')

        if flw_amount < order.total_ngn:
            _log(order, 'flw_amount_mismatch', {
                'expected': str(order.total_ngn), 'received': str(flw_amount),
            })
            return Response({'error': 'Payment amount mismatch.'}, status=400)

        claimed = _claim_flw_payment(order, str(flw_data.get('id', '')))
        if claimed is None:
            # Webhook (or an earlier verify call) already took this order.
            order.refresh_from_db()
            return Response(_buy_response(order))
        order = claimed
        _log(order, 'flw_payment_confirmed', {'amount': str(flw_amount)})

        _execute_buy_after_payment(order, refund_on_failure=False)

        return Response(_buy_response(order))


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
        # Re-check a few orders Quidax may have settled without the webhook
        # reaching us. Only reads Quidax state; finalizing is idempotent.
        if getattr(settings, 'QUIDAX_SECRET_KEY', ''):
            stuck = CryptoOrder.objects.filter(
                user=request.user,
                status=CryptoOrder.Status.PROCESSING,
                updated_at__lt=timezone.now() - timedelta(seconds=30),
            ).exclude(quidax_order_id='').order_by('-created_at')[:5]
            for order in stuck:
                try:
                    _sync_quidax_order(order)
                except QuidaxError as exc:
                    logger.warning('Order sync failed for %s: %s', order.reference, exc)

        orders = CryptoOrder.objects.filter(user=request.user).select_related('quote')[:50]
        return Response({'orders': [_order_dict(o) for o in orders]})


def _quidax_signature_valid(secret, sig_header, raw_body):
    """
    Verify Quidax's `t=<ts>,s=<sig>` header (docs.quidax.io/docs/introduction).

    Quidax computes HMAC-SHA256 over f"{ts}.{JSON.stringify(body)}". The raw
    body is normally byte-identical to that, but we also try a compact
    re-serialization in case whitespace differs in transit.
    """
    try:
        ts_part, sig_part = sig_header.split(',', 1)
        timestamp = ts_part.split('=', 1)[1].strip()
        signature = sig_part.split('=', 1)[1].strip()
    except (ValueError, IndexError):
        return False
    if not timestamp or not signature:
        return False

    candidates = [raw_body]
    try:
        compact = json.dumps(
            json.loads(raw_body), separators=(',', ':'), ensure_ascii=False,
        ).encode()
        if compact != raw_body:
            candidates.append(compact)
    except (ValueError, UnicodeDecodeError):
        pass

    for body in candidates:
        expected = hmac.new(
            secret.encode(),
            timestamp.encode() + b'.' + body,
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(signature.lower(), expected):
            return True
    return False


class CryptoWebhookView(APIView):
    """
    Receives Quidax webhook events.

    Quidax signs each request in the `quidax-signature` header as
    `t=<timestamp>,s=<hex HMAC-SHA256 of "<timestamp>.<json body>">`.
    Set QUIDAX_WEBHOOK_SECRET in Railway to the secret you configure in the
    Quidax merchant dashboard.

    Handled events:
      deposit.successful  → credit user's internal CryptoWallet
      order.done          → finalize buy/sell orders left in PROCESSING
      withdraw.successful → mark a CryptoWithdrawal COMPLETED
      withdraw.rejected   → mark a CryptoWithdrawal REJECTED, refund the wallet
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        # ── Signature verification ─────────────────────────────────────────
        secret = getattr(settings, 'QUIDAX_WEBHOOK_SECRET', '')
        if secret:
            sig_header = request.headers.get('Quidax-Signature', '')
            if not _quidax_signature_valid(secret, sig_header, request.body):
                logger.warning(
                    'Quidax webhook signature mismatch (header present=%s)',
                    bool(sig_header),
                )
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
        elif event in ('order.done', 'order.completed'):
            self._handle_order_done(data)
        elif event == 'withdraw.successful':
            self._handle_withdraw_successful(data)
        elif event == 'withdraw.rejected':
            self._handle_withdraw_rejected(data)
        # Unknown events are silently accepted (return 200) to prevent Quidax retries

        return Response({'received': True})

    def _handle_deposit(self, data: dict):
        """
        Credit the user's internal CryptoWallet when a deposit is confirmed.

        data fields from Quidax:
          id        — the deposit's id (dedupe key for re-delivered webhooks)
          user_id   — the Quidax sub-account UID (maps to accounts.User.quidax_user_id)
          amount    — deposit amount as string
          currency  — e.g. 'btc'
          network   — e.g. 'trc20' (may be absent)
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()

        quidax_uid = str(data.get('user', {}).get('id', '') or data.get('user_id', ''))
        if not quidax_uid:
            logger.warning('Deposit webhook: no user id in payload')
            return

        try:
            user = User.objects.get(quidax_user_id=quidax_uid)
        except User.DoesNotExist:
            logger.warning('Deposit webhook: no user with quidax_user_id=%s', quidax_uid)
            return

        _record_deposit(user, data, via='webhook')

    def _handle_order_done(self, data: dict):
        """Finalize Axira orders once Quidax confirms execution."""
        quidax_order_id = str(data.get('id', ''))
        if not quidax_order_id:
            return

        try:
            order = CryptoOrder.objects.get(quidax_order_id=quidax_order_id)
        except CryptoOrder.DoesNotExist:
            logger.info('Order webhook: no Axira order for quidax_order_id=%s', quidax_order_id)
            return

        _finalize_quidax_order(order)

    def _find_withdrawal(self, data: dict):
        quidax_id = str(data.get('id', ''))
        reference = str(data.get('reference', ''))

        withdrawal = None
        if quidax_id:
            withdrawal = CryptoWithdrawal.objects.filter(quidax_withdrawal_id=quidax_id).first()
        if not withdrawal and reference:
            withdrawal = CryptoWithdrawal.objects.filter(reference=reference).first()
        if not withdrawal:
            logger.info(
                'Withdraw webhook: no Axira withdrawal for id=%s reference=%s',
                quidax_id, reference,
            )
        return withdrawal

    def _handle_withdraw_successful(self, data: dict):
        """Mark a withdrawal COMPLETED once Quidax confirms on-chain settlement."""
        withdrawal = self._find_withdrawal(data)
        if withdrawal:
            _complete_withdrawal(withdrawal.pk, _quidax_tx_id(data), data.get('fee'))

    def _handle_withdraw_rejected(self, data: dict):
        """Mark a withdrawal REJECTED and refund the wallet."""
        withdrawal = self._find_withdrawal(data)
        if withdrawal:
            _reject_withdrawal(
                withdrawal.pk,
                str(data.get('reason') or data.get('message') or 'Rejected by Quidax.'),
            )


# ── Execution helpers (called synchronously for now) ─────────────────────────

def _finalize_quidax_order(order: CryptoOrder):
    """
    Credit wallets once Quidax confirms a market order (sync response or webhook).
    Safe to call multiple times — no-ops if the order is already completed.
    """
    if order.status == CryptoOrder.Status.COMPLETED:
        return

    with transaction.atomic():
        order = CryptoOrder.objects.select_for_update().get(pk=order.pk)
        if order.status == CryptoOrder.Status.COMPLETED:
            return

        if order.order_type == CryptoOrder.OrderType.BUY:
            _credit_wallet(order.user, order.coin, order.coin_amount)
            order.status = CryptoOrder.Status.COMPLETED
            _log(order, 'order_completed', {
                'coin_amount': str(order.coin_amount),
                'coin': order.coin,
                'via': 'quidax_done',
            })
        elif order.order_type == CryptoOrder.OrderType.SELL:
            _deduct_reserved(order.user, order.coin, order.coin_amount)
            _credit_ngn_wallet(order.user, order.total_ngn)
            order.status = CryptoOrder.Status.COMPLETED
            _log(order, 'order_completed', {
                'payout_ngn': str(order.total_ngn),
                'via': 'quidax_done',
            })
        else:
            return

        order.save(update_fields=['status', 'updated_at'])


_QUIDAX_DONE_STATES = ('done', 'completed', 'filled', 'success')

# How long a sell placed before the user had the coin waits for their deposit.
WAITING_DEPOSIT_TTL = timedelta(hours=24)


def _quidax_order_state(result: dict) -> str:
    return str(result.get('status') or result.get('state') or '').lower()


def _sync_quidax_order(order: CryptoOrder) -> None:
    """
    Pull the order's current state from Quidax — the fallback for when the
    order.done webhook never arrives, which otherwise leaves the order (and
    the user's reserved balance) stuck in PROCESSING forever.
    """
    if order.status != CryptoOrder.Status.PROCESSING or not order.quidax_order_id:
        return

    result = get_instant_order(order.quidax_order_id)
    state = _quidax_order_state(result)

    if state in _QUIDAX_DONE_STATES:
        _finalize_quidax_order(order)
        return

    if state in ('cancel', 'cancelled', 'rejected') and order.order_type == CryptoOrder.OrderType.SELL:
        executed = result.get('executed_volume') or {}
        executed = executed.get('amount', '0') if isinstance(executed, dict) else executed
        try:
            nothing_sold = Decimal(str(executed or '0')) == 0
        except InvalidOperation:
            nothing_sold = False
        # Partial fills are left for an admin — only auto-unwind clean misses.
        if nothing_sold:
            with transaction.atomic():
                locked = CryptoOrder.objects.select_for_update().get(pk=order.pk)
                if locked.status != CryptoOrder.Status.PROCESSING:
                    return
                _release_reserved(locked.user, locked.coin, locked.coin_amount)
                locked.status = CryptoOrder.Status.FAILED
                locked.save(update_fields=['status', 'updated_at'])
            _log(order, 'quidax_order_cancelled', {'state': state})


def _fulfil_waiting_sells(user, coin: str, max_age: timedelta = WAITING_DEPOSIT_TTL) -> None:
    """
    A sell placed before the user held the coin is saved as WAITING_DEPOSIT
    and the user is told to send crypto to their deposit address. When that
    deposit lands, execute the newest waiting order the balance now covers.

    The order is re-priced at the live rate first: its quote was only valid
    for seconds and the deposit can take much longer to confirm. Waiting
    orders older than max_age are expired so they stop showing as in-progress
    — any coin that arrives later just stays in the wallet to sell manually.
    """
    waiting = CryptoOrder.objects.filter(
        user=user, coin=coin,
        order_type=CryptoOrder.OrderType.SELL,
        status=CryptoOrder.Status.WAITING_DEPOSIT,
    )
    cutoff = timezone.now() - max_age

    for order in waiting.filter(created_at__gte=cutoff).order_by('-created_at'):
        if not _reserve_balance(user, coin, order.coin_amount):
            continue

        try:
            rate_ngn = _fetch_live_rate(coin)
        except ValueError:
            _release_reserved(user, coin, order.coin_amount)
            logger.warning('Waiting sell %s: no live rate, left waiting', order.reference)
            return

        ngn_value = (order.coin_amount * rate_ngn).quantize(Decimal('0.01'))
        fee_ngn = _compute_fee(_get_or_create_fee('sell'), ngn_value)
        old_payout = order.total_ngn

        order.rate_ngn = rate_ngn
        order.fee_ngn = fee_ngn
        order.total_ngn = max(ngn_value - fee_ngn, Decimal('0'))
        # Out of WAITING_DEPOSIT before executing, so a later deposit can't
        # reserve this order a second time if execution is queued for admin.
        order.status = CryptoOrder.Status.DEPOSIT_CONFIRMED
        order.save(update_fields=['rate_ngn', 'fee_ngn', 'total_ngn', 'status', 'updated_at'])
        _log(order, 'deposit_matched', {
            'coin_amount': str(order.coin_amount),
            'quoted_payout_ngn': str(old_payout),
            'repriced_payout_ngn': str(order.total_ngn),
        })

        _execute_sell(order, order.coin_amount)
        break

    expired = waiting.filter(created_at__lt=cutoff).update(
        status=CryptoOrder.Status.EXPIRED,
        note='No deposit received in time.',
        updated_at=timezone.now(),
    )
    if expired:
        logger.info('Expired %d waiting %s sell(s) for %s', expired, coin, user.email)


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
        quidax_status = _quidax_order_state(result)

        order.quidax_order_id = quidax_id

        order.save(update_fields=['quidax_order_id', 'updated_at'])

        if quidax_status in _QUIDAX_DONE_STATES:
            _finalize_quidax_order(order)
        else:
            order.status = CryptoOrder.Status.PROCESSING
            order.save(update_fields=['status', 'updated_at'])

    except QuidaxError as exc:
        error_msg = str(exc)

        logger.error('Quidax sell failed for %s: %s', order.reference, error_msg)
        _log(order, 'quidax_error', {
            'error': error_msg,
            'raw_exception': str(exc),
        })

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

        # Leg 2: buy to_coin with the NGN proceeds from leg 1 minus the swap
        # fee — Quidax market buys are NGN-denominated, not crypto-denominated.
        net_ngn = order.rate_ngn * coin_amount - order.fee_ngn
        ngn_volume = _ngn_volume_str(net_ngn)
        _log(order, 'quidax_buy_sent', {
            'coin': order.to_coin,
            'expected_amount': str(order.to_coin_amount),
            'ngn_volume': ngn_volume,
        })
        buy_result = create_instant_order(
            side='buy',
            market=SUPPORTED_COINS[order.to_coin],
            volume=ngn_volume,
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
        # Extract real error message from Quidax
        error_msg = str(exc)
        if hasattr(exc, 'response') and exc.response is not None:
            try:
                body = exc.response.json()
                error_msg = body.get('message') or body.get('error') or exc.response.text or str(exc)
            except Exception:
                error_msg = getattr(exc, 'message', str(exc))

        logger.error('Quidax swap failed for %s: %s', order.reference, error_msg)
        _log(order, 'quidax_error', {
            'error': error_msg,
            'raw_exception': str(exc),
        })

        _release_reserved(order.user, order.coin, coin_amount)
        order.status = CryptoOrder.Status.FAILED
        order.save(update_fields=['status', 'updated_at'])


def _execute_buy_after_payment(order: CryptoOrder, refund_on_failure: bool = True):
    """
    Called once payment is confirmed (wallet debit, Flutterwave verify, or
    webhook). Buys on Quidax and credits the crypto wallet. Guarded against
    double-execution since more than one of those paths can fire for the
    same order.

    refund_on_failure controls what happens if the Quidax buy itself fails
    after payment was confirmed:
      - True  (wallet-balance buys): safe to auto-refund — it's purely an
        internal ledger reversal, no external money was ever involved.
      - False (Flutterwave buys): real external money already moved to the
        merchant account, so failures are left for manual review instead of
        being auto-resolved by crediting the internal wallet.
    """
    if order.status == CryptoOrder.Status.COMPLETED:
        return

    if not getattr(settings, 'QUIDAX_SECRET_KEY', ''):
        logger.info('No Quidax key — buy order %s queued for manual execution', order.reference)
        return

    coin_amount = order.coin_amount
    ngn_volume = _ngn_volume_str(order.rate_ngn * coin_amount)
    _log(order, 'quidax_buy_sent', {
        'coin': order.coin, 'amount': str(coin_amount), 'ngn_volume': ngn_volume,
    })

    try:
        result = create_instant_order(
            side='buy',
            market=SUPPORTED_COINS[order.coin],
            volume=ngn_volume,
        )
        quidax_id = str(result.get('id', ''))
        quidax_status = _quidax_order_state(result)

        order.quidax_order_id = quidax_id

        order.save(update_fields=['quidax_order_id', 'updated_at'])

        if quidax_status in _QUIDAX_DONE_STATES:
            _finalize_quidax_order(order)
        else:
            order.status = CryptoOrder.Status.PROCESSING
            order.save(update_fields=['status', 'updated_at'])

    except QuidaxError as exc:
        error_msg = str(exc)

        logger.error('Quidax buy failed for %s: %s', order.reference, error_msg)
        _log(order, 'quidax_error', {
            'error': error_msg,
            'raw_exception': str(exc),
        })

        if refund_on_failure:
            _credit_ngn_wallet(order.user, order.total_ngn)
            _log(order, 'refunded_after_failure', {'amount': str(order.total_ngn)})
        else:
            _log(order, 'not_refunded_after_failure', {'amount': str(order.total_ngn)})

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
    needs_payment = (
        order.status == CryptoOrder.Status.PENDING_PAYMENT
        and bool(getattr(settings, 'FLUTTERWAVE_PUBLIC_KEY', ''))
    )
    resp = {
        'reference': order.reference,
        'coin': order.coin,
        'coin_amount': str(order.coin_amount),
        'rate_ngn': str(order.rate_ngn),
        'fee_ngn': str(order.fee_ngn),
        'total_ngn': str(order.total_ngn),
        'status': order.status,
        'needs_payment': needs_payment,
        'flutterwave': None,
        'bank_details': None,
    }
    if needs_payment:
        user = order.user
        resp['flutterwave'] = {
            'public_key': settings.FLUTTERWAVE_PUBLIC_KEY,
            'tx_ref': order.reference,
            'amount': str(order.total_ngn),
            'customer': {
                'email': user.email,
                'phone': getattr(user, 'phone', ''),
                'name': getattr(user, 'full_name', ''),
            },
        }
    elif order.status == CryptoOrder.Status.PENDING_PAYMENT:
        # Flutterwave isn't configured at all — manual bank-transfer fallback.
        resp['bank_details'] = _AXIRA_BANK
    elif order.status == CryptoOrder.Status.FAILED:
        # Raw Quidax errors describe Axira's account, so map them to
        # customer-facing text; the raw error stays in the order log.
        resp['error_detail'] = _order_error_detail(order, 'purchase')

        resp['refunded'] = order.logs.filter(event='refunded_after_failure').exists()
        resp['message'] = f"Purchase failed. {resp.get('error_detail', '')}"
    return resp


def _sell_response(order: CryptoOrder, deposit_address: str = '') -> dict:
    resp = {
        'reference': order.reference,
        'coin': order.coin,
        'coin_amount': str(order.coin_amount),
        'rate_ngn': str(order.rate_ngn),
        'fee_ngn': str(order.fee_ngn),
        'payout_ngn': str(order.total_ngn),
        'deposit_address': deposit_address,
        'status': order.status,
    }
    if order.status == CryptoOrder.Status.FAILED:
        resp['error_detail'] = _order_error_detail(order, 'sale')
    return resp


def _swap_response(order: CryptoOrder) -> dict:
    resp = {
        'reference': order.reference,
        'from_coin': order.coin,
        'to_coin': order.to_coin,
        'coin_amount': str(order.coin_amount),
        'to_coin_amount': str(order.to_coin_amount),
        'fee_ngn': str(order.fee_ngn),
        'status': order.status,
    }
    if order.status == CryptoOrder.Status.FAILED:
        resp['error_detail'] = _order_error_detail(order, 'swap')
    return resp
