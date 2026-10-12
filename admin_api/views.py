from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.serializers import UserSerializer, UpdateProfileSerializer
from crypto.models import CryptoFeeSettings, CryptoOrder, CryptoWallet, CryptoWithdrawal
from crypto.quidax import QuidaxError, create_instant_order
from crypto.views import (
    SUPPORTED_COINS,
    _finalize_quidax_order,
    _log,
    _ngn_volume_str,
    _order_dict,
    _release_reserved,
)
from giftcards.models import GiftCardBuy, GiftCardSale
from wallet.models import Transaction, Wallet


def _fmt(value):
    return str(value) if value is not None else '0.00'


def _crypto_wallets_breakdown(user):
    """
    Each coin wallet with the open orders/withdrawals currently holding its
    reserved balance. The wallet only stores a reserved total, so the holds
    are reconstructed from the statuses that reserve coin without settling:
      - sell in PROCESSING / DEPOSIT_CONFIRMED (Quidax not done yet, or
        queued for admin because no Quidax key was set)
      - swap in PROCESSING
      - withdrawal in PROCESSING that Quidax never accepted (an accepted one
        is deducted from reserved immediately)
    Anything left over is reported as unexplained — reserved coin with no
    open order behind it, which needs a manual fix.
    """
    holds_by_coin = {}

    open_orders = CryptoOrder.objects.filter(user=user).filter(
        Q(order_type=CryptoOrder.OrderType.SELL,
          status__in=[CryptoOrder.Status.PROCESSING, CryptoOrder.Status.DEPOSIT_CONFIRMED])
        | Q(order_type=CryptoOrder.OrderType.SWAP, status=CryptoOrder.Status.PROCESSING)
    )
    for o in open_orders:
        holds_by_coin.setdefault(o.coin, []).append({
            'kind': o.order_type,
            'reference': o.reference,
            'status': o.status,
            'amount': str(o.coin_amount),
            'quidax_id': o.quidax_order_id,
            'created_at': o.created_at.isoformat(),
        })

    stuck_withdrawals = CryptoWithdrawal.objects.filter(
        user=user, status=CryptoWithdrawal.Status.PROCESSING, quidax_withdrawal_id='',
    )
    for w in stuck_withdrawals:
        holds_by_coin.setdefault(w.coin, []).append({
            'kind': 'withdrawal',
            'reference': w.reference,
            'status': w.status,
            'amount': str(w.amount),
            'quidax_id': '',
            'created_at': w.created_at.isoformat(),
        })

    result = []
    for wallet in CryptoWallet.objects.filter(user=user).order_by('coin'):
        holds = holds_by_coin.get(wallet.coin, [])
        if wallet.available == 0 and wallet.reserved == 0 and not holds:
            continue
        held = sum((Decimal(h['amount']) for h in holds), Decimal('0'))
        result.append({
            'coin': wallet.coin,
            'available': str(wallet.available),
            'reserved': str(wallet.reserved),
            'total': str(wallet.total),
            'holds': holds,
            'unexplained': str(wallet.reserved - held),
        })
    return result


def _wallet_tx_dict(tx):
    return {
        'id': str(tx.id),
        'reference': tx.reference,
        'type': tx.tx_type,
        'user': tx.user.full_name,
        'email': tx.user.email,
        'amount_ngn': _fmt(tx.amount),
        'status': tx.status,
        'created_at': tx.created_at.isoformat(),
        'bank_name': tx.bank_name or '',
        'account_number': tx.account_number or '',
        'account_name': tx.account_name or '',
    }


def _gc_buy_dict(gc):
    return {
        'id': gc.reference,
        'reference': gc.reference,
        'type': 'giftcard_buy',
        'user': gc.user.full_name,
        'email': gc.user.email,
        'amount_ngn': _fmt(gc.amount_ngn),
        'status': gc.status,
        'created_at': gc.created_at.isoformat(),
        'brand': gc.brand,
        'unit_price_usd': _fmt(gc.unit_price_usd),
    }


def _gc_sale_dict(gc):
    return {
        'id': gc.reference,
        'reference': gc.reference,
        'type': 'giftcard_sell',
        'user': gc.user.full_name,
        'email': gc.user.email,
        'amount_ngn': _fmt(gc.amount_ngn),
        'status': gc.status,
        'created_at': gc.created_at.isoformat(),
        'brand': gc.brand,
        'amount_usd': _fmt(gc.amount_usd),
    }


class AdminProfileView(APIView):
    """Admin's own profile — view and update."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response({'user': UserSerializer(request.user, context={'request': request}).data})

    def patch(self, request):
        serializer = UpdateProfileSerializer(
            data=request.data, instance=request.user, partial=True
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({'user': UserSerializer(user, context={'request': request}).data})


class AdminOverviewView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        today = timezone.now().date()

        total_users = User.objects.filter(is_staff=False).count()

        completed_deposits = Transaction.objects.filter(
            tx_type=Transaction.TxType.DEPOSIT,
            status=Transaction.Status.COMPLETED,
        )
        completed_withdrawals = Transaction.objects.filter(
            tx_type=Transaction.TxType.WITHDRAWAL,
            status=Transaction.Status.COMPLETED,
        )

        total_deposits_ngn = completed_deposits.aggregate(t=Sum('amount'))['t'] or 0
        total_withdrawals_ngn = completed_withdrawals.aggregate(t=Sum('amount'))['t'] or 0

        deposits_today_qs = Transaction.objects.filter(
            tx_type=Transaction.TxType.DEPOSIT,
            created_at__date=today,
        ).exclude(status=Transaction.Status.FAILED)
        deposits_today_ngn = deposits_today_qs.aggregate(t=Sum('amount'))['t'] or 0
        deposits_today_count = deposits_today_qs.count()

        # Combined recent transactions (wallet + giftcard)
        wallet_txns = list(
            Transaction.objects.select_related('user').order_by('-created_at')[:20]
        )
        gc_buys = list(
            GiftCardBuy.objects.select_related('user').order_by('-created_at')[:10]
        )
        gc_sales = list(
            GiftCardSale.objects.select_related('user').order_by('-created_at')[:10]
        )

        combined = (
            [_wallet_tx_dict(t) for t in wallet_txns]
            + [_gc_buy_dict(g) for g in gc_buys]
            + [_gc_sale_dict(g) for g in gc_sales]
        )
        combined.sort(key=lambda x: x['created_at'], reverse=True)

        return Response({
            'total_users': total_users,
            'total_deposits_ngn': _fmt(total_deposits_ngn),
            'deposits_today_ngn': _fmt(deposits_today_ngn),
            'deposits_today_count': deposits_today_count,
            'total_withdrawals_ngn': _fmt(total_withdrawals_ngn),
            'recent_transactions': combined[:15],
        })


class AdminUsersView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = User.objects.filter(is_staff=False).prefetch_related('wallet').order_by('-created_at')

        total = qs.count()
        active = qs.filter(is_active=True).count()
        blacklisted = qs.filter(is_active=False).count()

        users = []
        for u in qs:
            try:
                balance = str(u.wallet.ngn_balance)
            except Wallet.DoesNotExist:
                balance = '0.00'

            users.append({
                'id': str(u.id),
                'full_name': u.full_name,
                'email': u.email,
                'phone': u.phone,
                'ngn_balance': balance,
                'is_active': u.is_active,
                'created_at': u.created_at.isoformat(),
                'profile_photo': u.profile_photo.url if u.profile_photo else '',
            })

        return Response({
            'total': total,
            'active': active,
            'blacklisted': blacklisted,
            'users': users,
        })


class AdminUserDetailView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, user_id):
        try:
            u = User.objects.get(id=user_id, is_staff=False)
        except User.DoesNotExist:
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            balance = str(u.wallet.ngn_balance)
        except Wallet.DoesNotExist:
            balance = '0.00'

        wallet_txns = Transaction.objects.filter(user=u).order_by('-created_at')[:20]
        gc_buys = GiftCardBuy.objects.filter(user=u).order_by('-created_at')[:10]
        gc_sales = GiftCardSale.objects.filter(user=u).order_by('-created_at')[:10]

        combined = (
            [_wallet_tx_dict(t) for t in wallet_txns]
            + [_gc_buy_dict(g) for g in gc_buys]
            + [_gc_sale_dict(g) for g in gc_sales]
        )
        combined.sort(key=lambda x: x['created_at'], reverse=True)

        return Response({
            'id': str(u.id),
            'full_name': u.full_name,
            'email': u.email,
            'phone': u.phone,
            'ngn_balance': balance,
            'is_active': u.is_active,
            'created_at': u.created_at.isoformat(),
            'profile_photo': u.profile_photo.url if u.profile_photo else '',
            'transactions': combined[:20],
            'crypto_wallets': _crypto_wallets_breakdown(u),
        })

    def post(self, request, user_id):
        """Toggle user active/blacklisted status."""
        try:
            u = User.objects.get(id=user_id, is_staff=False)
        except User.DoesNotExist:
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        u.is_active = not u.is_active
        u.save(update_fields=['is_active'])
        return Response({'is_active': u.is_active})

    def delete(self, request, user_id):
        try:
            u = User.objects.get(id=user_id, is_staff=False)
        except User.DoesNotExist:
            return Response({'error': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        if u.profile_photo:
            u.profile_photo.delete(save=False)

        u.delete()
        return Response({'message': 'User deleted successfully.'})


class AdminTransactionsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        tx_type = request.query_params.get('type', '').lower()

        deposit_qs = Transaction.objects.filter(
            tx_type=Transaction.TxType.DEPOSIT,
            status=Transaction.Status.COMPLETED,
        )
        withdrawal_qs = Transaction.objects.filter(
            tx_type=Transaction.TxType.WITHDRAWAL,
            status=Transaction.Status.COMPLETED,
        )
        gc_buy_qs = GiftCardBuy.objects.filter(status=GiftCardBuy.Status.COMPLETED)
        gc_sale_qs = GiftCardSale.objects.all()

        stats = {
            'deposits': {
                'count': deposit_qs.count(),
                'total_ngn': _fmt(deposit_qs.aggregate(t=Sum('amount'))['t'] or 0),
            },
            'withdrawals': {
                'count': withdrawal_qs.count(),
                'total_ngn': _fmt(withdrawal_qs.aggregate(t=Sum('amount'))['t'] or 0),
            },
            'giftcard_buys': {
                'count': gc_buy_qs.count(),
                'total_ngn': _fmt(gc_buy_qs.aggregate(t=Sum('amount_ngn'))['t'] or 0),
            },
            'giftcard_sales': {
                'count': gc_sale_qs.count(),
                'pending': gc_sale_qs.filter(status='pending').count(),
            },
        }

        # Build combined list filtered by type
        wallet_txns = list(
            Transaction.objects.select_related('user').order_by('-created_at')[:100]
        )
        gc_buys = list(
            GiftCardBuy.objects.select_related('user').order_by('-created_at')[:50]
        )
        gc_sales = list(
            GiftCardSale.objects.select_related('user').order_by('-created_at')[:50]
        )

        combined = []
        if tx_type in ('', 'deposit'):
            combined += [_wallet_tx_dict(t) for t in wallet_txns if t.tx_type == 'deposit']
        if tx_type in ('', 'withdrawal'):
            combined += [_wallet_tx_dict(t) for t in wallet_txns if t.tx_type == 'withdrawal']
        if tx_type in ('', 'giftcard'):
            combined += [_gc_buy_dict(g) for g in gc_buys]
            combined += [_gc_sale_dict(g) for g in gc_sales]

        combined.sort(key=lambda x: x['created_at'], reverse=True)

        return Response({
            'stats': stats,
            'transactions': combined[:100],
        })


# ── Crypto Fee Settings ───────────────────────────────────────────────────────

class AdminFeesView(APIView):
    """List all crypto fee settings. Auto-creates missing fee types on first access."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        for ft in ('buy', 'sell', 'swap'):
            CryptoFeeSettings.objects.get_or_create(
                fee_type=ft,
                defaults={'flat_usd': Decimal('0'), 'percent': Decimal('0')},
            )
        fees = CryptoFeeSettings.objects.all().order_by('fee_type')
        return Response({
            'fees': [
                {
                    'id': f.id,
                    'fee_type': f.fee_type,
                    'flat_usd': str(f.flat_usd),
                    'percent': str(f.percent),
                    'is_active': f.is_active,
                }
                for f in fees
            ]
        })


class AdminFeeDetailView(APIView):
    """Update a single fee setting."""
    permission_classes = [IsAdminUser]

    def patch(self, request, fee_id):
        try:
            fee = CryptoFeeSettings.objects.get(id=fee_id)
        except CryptoFeeSettings.DoesNotExist:
            return Response({'error': 'Fee not found.'}, status=status.HTTP_404_NOT_FOUND)

        fields = []
        for field in ('flat_usd', 'percent'):
            val = request.data.get(field)
            if val is not None:
                try:
                    setattr(fee, field, Decimal(str(val)))
                    fields.append(field)
                except (InvalidOperation, ValueError):
                    return Response({'error': f'Invalid value for {field}.'}, status=400)

        is_active = request.data.get('is_active')
        if is_active is not None:
            fee.is_active = bool(is_active)
            fields.append('is_active')

        if fields:
            fee.save(update_fields=fields + ['updated_at'])

        return Response({
            'id': fee.id,
            'fee_type': fee.fee_type,
            'flat_usd': str(fee.flat_usd),
            'percent': str(fee.percent),
            'is_active': fee.is_active,
        })


# ── Admin Crypto Orders ───────────────────────────────────────────────────────

class AdminCryptoOrdersView(APIView):
    """List all crypto orders with summary stats."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        order_type = request.query_params.get('type', '').lower()
        order_status = request.query_params.get('status', '').lower()

        qs = CryptoOrder.objects.select_related('user').order_by('-created_at')
        if order_type in ('buy', 'sell', 'swap'):
            qs = qs.filter(order_type=order_type)
        if order_status:
            qs = qs.filter(status=order_status)

        orders = qs[:100]

        stats = {
            'total': CryptoOrder.objects.count(),
            # Waiting on the user or on an admin, before reaching Quidax.
            'pending': CryptoOrder.objects.filter(status__in=[
                CryptoOrder.Status.PENDING_PAYMENT,
                CryptoOrder.Status.PAYMENT_RECEIVED,
                CryptoOrder.Status.WAITING_DEPOSIT,
                CryptoOrder.Status.DEPOSIT_CONFIRMED,
            ]).count(),
            'processing': CryptoOrder.objects.filter(status=CryptoOrder.Status.PROCESSING).count(),
            'completed': CryptoOrder.objects.filter(status=CryptoOrder.Status.COMPLETED).count(),
            'failed': CryptoOrder.objects.filter(status=CryptoOrder.Status.FAILED).count(),
        }

        data = []
        for o in orders:
            d = _order_dict(o)
            d['user'] = o.user.full_name
            d['email'] = o.user.email
            d['user_id'] = str(o.user.id)
            d['has_proof'] = bool(o.payment_proof)
            d['proof_url'] = request.build_absolute_uri(o.payment_proof.url) if o.payment_proof else ''
            d['quidax_order_id'] = o.quidax_order_id
            d['note'] = o.note
            data.append(d)

        return Response({'stats': stats, 'orders': data})


class AdminCryptoWithdrawalsView(APIView):
    """List crypto sends (withdrawals to external wallets) with summary stats."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        w_status = request.query_params.get('status', '').lower()

        qs = CryptoWithdrawal.objects.select_related('user').order_by('-created_at')
        if w_status:
            qs = qs.filter(status=w_status)

        stats = {
            'total': CryptoWithdrawal.objects.count(),
            'processing': CryptoWithdrawal.objects.filter(status=CryptoWithdrawal.Status.PROCESSING).count(),
            'completed': CryptoWithdrawal.objects.filter(status=CryptoWithdrawal.Status.COMPLETED).count(),
            'failed': CryptoWithdrawal.objects.filter(
                status__in=[CryptoWithdrawal.Status.FAILED, CryptoWithdrawal.Status.REJECTED],
            ).count(),
        }

        data = []
        for w in qs[:100]:
            data.append({
                'reference': w.reference,
                'user': w.user.full_name,
                'email': w.user.email,
                'user_id': str(w.user.id),
                'coin': w.coin,
                'network': w.network,
                'address': w.address,
                'amount': str(w.amount),
                'fee': str(w.fee),
                'status': w.status,
                'tx_id': w.tx_id,
                'quidax_withdrawal_id': w.quidax_withdrawal_id,
                'note': w.note,
                'created_at': w.created_at.isoformat(),
            })

        return Response({'stats': stats, 'withdrawals': data})


class AdminCryptoOrderActionView(APIView):
    """
    Approve or reject a crypto order.
    POST { action: 'approve' | 'reject', note: '' }
    """
    permission_classes = [IsAdminUser]

    def post(self, request, reference):
        try:
            order = CryptoOrder.objects.get(reference=reference)
        except CryptoOrder.DoesNotExist:
            return Response({'error': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get('action', '').lower()
        note = request.data.get('note', '')

        REJECTABLE = (
            CryptoOrder.Status.PENDING_PAYMENT,
            CryptoOrder.Status.PAYMENT_RECEIVED,
            CryptoOrder.Status.WAITING_DEPOSIT,
            CryptoOrder.Status.DEPOSIT_CONFIRMED,
            CryptoOrder.Status.PROCESSING,
        )

        if action == 'reject':
            with transaction.atomic():
                # Lock so a double-click can't release the reserve twice.
                order = CryptoOrder.objects.select_for_update().get(pk=order.pk)
                if order.status not in REJECTABLE:
                    return Response({'error': 'Order cannot be rejected at this stage.'}, status=400)

                # Sells/swaps past WAITING_DEPOSIT have the user's coin moved
                # into reserved — rejecting must hand it back to available.
                holds_reserve = (
                    order.order_type == CryptoOrder.OrderType.SELL
                    and order.status in (CryptoOrder.Status.PROCESSING, CryptoOrder.Status.DEPOSIT_CONFIRMED)
                ) or (
                    order.order_type == CryptoOrder.OrderType.SWAP
                    and order.status == CryptoOrder.Status.PROCESSING
                )
                if holds_reserve and order.quidax_order_id:
                    # Quidax may already have sold the coin — releasing it
                    # here would let the user spend it twice.
                    return Response({
                        'error': (
                            f'Order was already sent to Quidax (#{order.quidax_order_id}). '
                            'Run reconcile_crypto_orders to settle it from Quidax instead of rejecting.'
                        ),
                    }, status=400)

                if holds_reserve:
                    _release_reserved(order.user, order.coin, order.coin_amount)

                order.status = CryptoOrder.Status.FAILED
                order.note = note or 'Rejected by admin.'
                order.save(update_fields=['status', 'note', 'updated_at'])
                _log(order, 'admin_rejected', {
                    'note': order.note,
                    'released_reserved': str(order.coin_amount) if holds_reserve else '0',
                })
            return Response({'status': order.status, 'reference': reference})

        if action == 'approve':
            if order.status not in REJECTABLE:
                return Response({'error': 'Order cannot be approved at this stage.'}, status=400)

            # Execute on Quidax if key is configured
            from django.conf import settings as django_settings
            if getattr(django_settings, 'QUIDAX_SECRET_KEY', ''):
                market = SUPPORTED_COINS.get(order.coin, '')
                if not market:
                    return Response({'error': f'No Quidax market for {order.coin}.'}, status=400)

                if order.order_type == CryptoOrder.OrderType.BUY:
                    side = 'buy'
                elif order.order_type == CryptoOrder.OrderType.SELL:
                    side = 'sell'
                else:
                    side = 'sell'  # swap: sell leg already attempted at order creation

                # Quidax market buys are NGN-denominated, sells are crypto-denominated.
                volume = (
                    _ngn_volume_str(order.rate_ngn * order.coin_amount)
                    if side == 'buy'
                    else str(order.coin_amount)
                )

                try:
                    result = create_instant_order(
                        side=side,
                        market=market,
                        volume=volume,
                    )
                    order.quidax_order_id = str(result.get('id', ''))
                    order.note = note or 'Approved by admin.'
                    order.save(update_fields=['note', 'quidax_order_id', 'updated_at'])

                    quidax_status = str(result.get('status', '')).lower()
                    if quidax_status in ('done', 'completed', 'filled', 'success'):
                        _finalize_quidax_order(order)
                    else:
                        order.status = CryptoOrder.Status.PROCESSING
                        order.save(update_fields=['status', 'updated_at'])
                except QuidaxError as e:
                    return Response(
                        {'error': f'Quidax execution failed: {e}'},
                        status=status.HTTP_502_BAD_GATEWAY,
                    )
            else:
                order.status = CryptoOrder.Status.COMPLETED
                order.note = note or 'Approved by admin.'
                order.save(update_fields=['status', 'note', 'updated_at'])

            return Response({'status': order.status, 'reference': reference})

        return Response({'error': 'action must be "approve" or "reject".'}, status=400)
