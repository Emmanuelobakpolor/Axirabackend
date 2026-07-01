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
    get_ticker,
)

logger = logging.getLogger(__name__)

# ── Coin registry ─────────────────────────────────────────────────────────────
# Edit here to add/remove supported coins — Flutter never hardcodes this list.

SUPPORTED_COINS = {
    'BTC':  'btcngn',
    'ETH':  'ethngn',
    'USDT': 'usdtngn',
    'SOL':  'solngn',
    'BNB':  'bnbngn',
    'XRP':  'xrpngn',
    'USDC': 'usdcngn',
}

COIN_META = {
    'BTC':  {'name': 'Bitcoin',      'color': '#F7931A', 'letter': 'B'},
    'ETH':  {'name': 'Ethereum',     'color': '#627EEA', 'letter': 'E'},
    'USDT': {'name': 'Tether',       'color': '#26A17B', 'letter': 'T'},
    'SOL':  {'name': 'Solana',       'color': '#9945FF', 'letter': 'S'},
    'BNB':  {'name': 'Binance Coin', 'color': '#F3BA2F', 'letter': 'B'},
    'XRP':  {'name': 'Ripple',       'color': '#346AA9', 'letter': 'X'},
    'USDC': {'name': 'USDC Coin',    'color': '#2775CA', 'letter': 'U'},
}

QUOTE_TTL_SECONDS = 60

_NGN_PER_USD = Decimal(str(getattr(settings, 'NGN_PER_USD', '1600')))

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
    market = SUPPORTED_COINS[coin]
    try:
        data = get_ticker(market)
        rate = Decimal(str(data.get('ticker', {}).get('last', '0')))
        if rate <= 0:
            raise ValueError(f'Zero price returned for {coin}')
        return rate
    except (QuidaxError, InvalidOperation) as exc:
        raise ValueError(f'Unable to fetch live price for {coin}.') from exc


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

class CryptoPricesView(APIView):
    """Live prices + coin metadata. Flutter uses this for display only."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tickers = get_all_tickers()
        except QuidaxError as exc:
            logger.error('Quidax ticker fetch failed: %s', exc)
            return Response({'error': str(exc)}, status=502)

        coins, prices = [], {}
        for coin, market in SUPPORTED_COINS.items():
            price = tickers.get(market, {}).get('ticker', {}).get('last', '0')
            prices[coin] = price
            meta = COIN_META.get(coin, {})
            coins.append({
                'symbol': coin,
                'name': meta.get('name', coin),
                'color': meta.get('color', '#888888'),
                'letter': meta.get('letter', coin[0]),
                'price_ngn': price,
            })

        return Response({'prices': prices, 'coins': coins})


class CryptoFeesView(APIView):
    """Current fee config — Flutter uses for estimate display only."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        fees = {}
        for ft in ('buy', 'sell', 'swap'):
            f = _get_or_create_fee(ft)
            fees[ft] = {'flat_usd': str(f.flat_usd), 'percent': str(f.percent)}
        return Response({'fees': fees})


class CryptoQuoteView(APIView):
    """
    Request a price-locked quote valid for 60 seconds.

    POST body:
        type    : 'buy' | 'sell' | 'swap'
        coin    : 'BTC'
        amount  : '0.001'
        to_coin : 'ETH'  (swap only)

    The backend fetches the live Quidax price at this moment, computes the
    fee, and locks the result into a CryptoQuote row with a 60-second expiry.
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

    Flow after this endpoint:
      1. User makes bank transfer using reference as narration.
      2. User uploads proof of payment.
      3. Admin confirms payment → backend calls Quidax to buy → credits wallet.

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
        'bank_details': _AXIRA_BANK,
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
