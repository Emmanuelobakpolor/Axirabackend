import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from .flutterwave import (
    FlutterwaveError,
    get_banks,
    initiate_transfer,
    resolve_account,
    verify_transaction,
)
from .models import Transaction, Wallet
from .serializers import (
    DepositNotifySerializer,
    InitiateDepositSerializer,
    TransactionSerializer,
    WithdrawalSerializer,
)


logger = logging.getLogger(__name__)


def _get_or_create_wallet(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def _mask_account(account_number):
    """Last 4 digits only — logs must not carry full bank account numbers."""
    acct = str(account_number or '')
    return f"******{acct[-4:]}" if len(acct) > 4 else '******'


class WalletViewSet(GenericViewSet):
    permission_classes = [IsAuthenticated]

    # ── Balance ───────────────────────────────────────────────────────────────

    @action(detail=False, methods=['get'])
    def balance(self, request):
        wallet = _get_or_create_wallet(request.user)
        return Response({'ngn_balance': str(wallet.ngn_balance)})

    # ── Initiate deposit (Flutterwave) ────────────────────────────────────────

    @action(detail=False, methods=['post'], url_path='initiate-deposit')
    def initiate_deposit(self, request):
        serializer = InitiateDepositSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        Transaction.objects.create(
            user=request.user,
            tx_type=Transaction.TxType.DEPOSIT,
            amount=d['amount'],
            status=Transaction.Status.PENDING,
            flw_tx_ref=d['tx_ref'],
        )

        user = request.user
        return Response({
            'tx_ref': d['tx_ref'],
            'amount': str(d['amount']),
            'public_key': settings.FLUTTERWAVE_PUBLIC_KEY,
            'customer': {
                'email': user.email,
                'phone': getattr(user, 'phone', ''),
                'name': getattr(user, 'full_name', ''),
            },
        }, status=status.HTTP_201_CREATED)

    # ── Verify payment after Flutterwave SDK completes ─────────────────────────

    @action(detail=False, methods=['post'], url_path='verify-payment')
    def verify_payment(self, request):
        tx_ref = request.data.get('tx_ref', '').strip()
        if not tx_ref:
            return Response({'error': 'tx_ref is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            tx = Transaction.objects.get(flw_tx_ref=tx_ref, user=request.user)
        except Transaction.DoesNotExist:
            return Response({'error': 'Transaction not found.'}, status=status.HTTP_404_NOT_FOUND)

        if tx.status == Transaction.Status.COMPLETED:
            wallet = _get_or_create_wallet(request.user)
            return Response({
                'message': 'Already credited.',
                'ngn_balance': str(wallet.ngn_balance),
            })

        try:
            result = verify_transaction(tx_ref)
        except FlutterwaveError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        flw_data = result.get('data', {})
        flw_status = flw_data.get('status', '')

        if result.get('status') != 'success' or flw_status != 'successful':
            tx.status = Transaction.Status.FAILED
            tx.save(update_fields=['status', 'updated_at'])
            return Response(
                {'error': 'Payment not confirmed by Flutterwave.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        flw_amount = Decimal(str(flw_data.get('amount', 0)))
        if flw_amount < tx.amount:
            tx.status = Transaction.Status.FAILED
            tx.save(update_fields=['status', 'updated_at'])
            return Response(
                {'error': 'Payment amount mismatch.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(user=request.user)
            wallet.ngn_balance += tx.amount
            wallet.save(update_fields=['ngn_balance', 'updated_at'])
            tx.status = Transaction.Status.COMPLETED
            tx.save(update_fields=['status', 'updated_at'])

        return Response({
            'message': 'Wallet credited successfully.',
            'ngn_balance': str(wallet.ngn_balance),
            'amount': str(tx.amount),
        })

    # ── Flutterwave webhook (unauthenticated) ──────────────────────────────────

    @action(
        detail=False,
        methods=['post'],
        url_path='flw-webhook',
        permission_classes=[AllowAny],
        authentication_classes=[],
    )
    def flw_webhook(self, request):
        verif_hash = request.headers.get('Verif-Hash', '')
        if verif_hash != settings.FLUTTERWAVE_WEBHOOK_HASH:
            logger.warning('FLW webhook rejected: signature mismatch')
            return Response({'error': 'Invalid signature.'}, status=status.HTTP_401_UNAUTHORIZED)

        event = request.data.get('event', '')
        data = request.data.get('data', {})
        logger.info('FLW webhook received: event=%s status=%s', event, data.get('status'))

        if event == 'charge.completed' and data.get('status') == 'successful':
            tx_ref = data.get('tx_ref', '')

            # Crypto buy orders have references starting with 'CRY' — route there
            if tx_ref.startswith('CRY'):
                from crypto.views import handle_flw_crypto_charge
                handle_flw_crypto_charge(data)
                return Response({'status': 'ok'})

            try:
                tx = Transaction.objects.get(flw_tx_ref=tx_ref)
            except Transaction.DoesNotExist:
                return Response({'status': 'ok'})

            if tx.status != Transaction.Status.COMPLETED:
                with db_transaction.atomic():
                    wallet = Wallet.objects.select_for_update().get(user=tx.user)
                    wallet.ngn_balance += tx.amount
                    wallet.save(update_fields=['ngn_balance', 'updated_at'])
                    tx.status = Transaction.Status.COMPLETED
                    tx.save(update_fields=['status', 'updated_at'])

        elif event == 'transfer.completed':
            ref = data.get('reference', '')
            transfer_status = data.get('status', '')
            # Flutterwave puts the human-readable rejection reason here — this is
            # the only place the real cause of an async failure ever appears.
            reason = (
                data.get('complete_message')
                or data.get('narration')
                or 'No reason supplied by Flutterwave.'
            )

            try:
                tx = Transaction.objects.get(reference=ref)
            except Transaction.DoesNotExist:
                logger.warning('Transfer webhook: no transaction with reference=%s', ref)
                return Response({'status': 'ok'})

            logger.info(
                'Transfer webhook: ref=%s status=%s current=%s reason=%s',
                ref, transfer_status, tx.status, reason,
            )

            if transfer_status == 'SUCCESSFUL':
                # Guard against Flutterwave's webhook retries.
                updated = Transaction.objects.filter(
                    pk=tx.pk, status=Transaction.Status.PENDING,
                ).update(
                    status=Transaction.Status.COMPLETED,
                    updated_at=timezone.now(),
                )
                if updated:
                    logger.info('Withdrawal COMPLETED: ref=%s amount=%s', ref, tx.amount)
                else:
                    logger.info(
                        'Transfer webhook ignored (already %s): ref=%s', tx.status, ref,
                    )

            elif transfer_status in ('FAILED', 'CANCELLED'):
                with db_transaction.atomic():
                    # Conditional update is the concurrency guard: only the first
                    # delivery flips PENDING→FAILED, so retries cannot refund twice.
                    updated = Transaction.objects.filter(
                        pk=tx.pk, status=Transaction.Status.PENDING,
                    ).update(
                        status=Transaction.Status.FAILED,
                        note=reason,
                        updated_at=timezone.now(),
                    )

                    if updated:
                        wallet = Wallet.objects.select_for_update().get(user=tx.user)
                        wallet.ngn_balance += tx.amount
                        wallet.save(update_fields=['ngn_balance', 'updated_at'])
                        logger.error(
                            'Withdrawal FAILED via webhook: ref=%s user=%s amount=%s '
                            'flw_status=%s reason=%s (wallet refunded)',
                            ref, tx.user_id, tx.amount, transfer_status, reason,
                        )
                    else:
                        logger.warning(
                            'Duplicate/late transfer failure webhook ignored: '
                            'ref=%s already %s — no refund applied',
                            ref, tx.status,
                        )

        return Response({'status': 'ok'})

    # ── Withdraw (initiates FLW bank transfer) ─────────────────────────────────

    @action(detail=False, methods=['post'])
    def withdraw(self, request):
        serializer = WithdrawalSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(user=request.user)
            if wallet.ngn_balance < d['amount']:
                return Response(
                    {'amount': ['Insufficient balance.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            wallet.ngn_balance -= d['amount']
            wallet.save(update_fields=['ngn_balance', 'updated_at'])

            tx = Transaction.objects.create(
                user=request.user,
                tx_type=Transaction.TxType.WITHDRAWAL,
                amount=d['amount'],
                status=Transaction.Status.PENDING,
                account_name=d['account_name'],
                bank_name=d['bank_name'],
                bank_code=d['bank_code'],
                account_number=d['account_number'],
            )

        logger.info(
            'Withdrawal initiating: ref=%s user=%s amount=%s bank=%s(%s) acct=%s',
            tx.reference, request.user.pk, d['amount'],
            d['bank_name'], d['bank_code'], _mask_account(d['account_number']),
        )

        try:
            result = initiate_transfer(
                amount=float(d['amount']),
                bank_code=d['bank_code'],
                account_number=d['account_number'],
                account_name=d['account_name'],
                reference=tx.reference,
            )
        except FlutterwaveError as e:
            with db_transaction.atomic():
                refund_wallet = Wallet.objects.select_for_update().get(user=request.user)
                refund_wallet.ngn_balance += d['amount']
                refund_wallet.save(update_fields=['ngn_balance', 'updated_at'])
                tx.status = Transaction.Status.FAILED
                tx.note = str(e)
                tx.save(update_fields=['status', 'note', 'updated_at'])
            logger.error(
                'Withdrawal FAILED at initiation: ref=%s user=%s amount=%s reason=%s '
                '(wallet refunded)',
                tx.reference, request.user.pk, d['amount'], e,
            )
            return Response(
                {'error': f'Transfer could not be initiated: {e}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        logger.info(
            'Withdrawal accepted by Flutterwave: ref=%s user=%s amount=%s flw_id=%s',
            tx.reference, request.user.pk, d['amount'],
            (result or {}).get('data', {}).get('id'),
        )

        return Response({
            'message': 'Withdrawal initiated. Processing within 24 hours.',
            'reference': tx.reference,
            'amount': str(d['amount']),
            'status': tx.status,
        }, status=status.HTTP_201_CREATED)

    # ── Resolve account (verifies account name before withdrawal) ──────────────

    @action(detail=False, methods=['get'], url_path='resolve-account')
    def resolve_account(self, request):
        account_number = request.query_params.get('account_number', '').strip()
        bank_code = request.query_params.get('bank_code', '').strip()

        if not account_number or not bank_code:
            return Response(
                {'error': 'account_number and bank_code are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = resolve_account(account_number, bank_code)
        except FlutterwaveError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'account_name': result.get('data', {}).get('account_name', '')})

    # ── Nigerian banks list ────────────────────────────────────────────────────

    @action(detail=False, methods=['get'])
    def banks(self, request):
        try:
            result = get_banks()
        except FlutterwaveError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        banks_list = [
            {'name': b['name'], 'code': b['code']}
            for b in result.get('data', [])
        ]
        return Response({'banks': banks_list})

    # ── Transaction history ────────────────────────────────────────────────────

    @action(detail=False, methods=['get'])
    def transactions(self, request):
        qs = Transaction.objects.filter(user=request.user)[:50]
        return Response(TransactionSerializer(qs, many=True).data)
