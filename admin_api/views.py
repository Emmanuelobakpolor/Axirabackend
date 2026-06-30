from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from giftcards.models import GiftCardBuy, GiftCardSale
from wallet.models import Transaction, Wallet


def _fmt(value):
    return str(value) if value is not None else '0.00'


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
        deposits_today_ngn = completed_deposits.filter(
            created_at__date=today
        ).aggregate(t=Sum('amount'))['t'] or 0
        deposits_today_count = completed_deposits.filter(created_at__date=today).count()

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
