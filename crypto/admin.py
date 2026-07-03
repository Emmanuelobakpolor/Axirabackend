from django.contrib import admin

from .models import (
    CryptoDepositAddress,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoOrderLog,
    CryptoQuote,
    CryptoWallet,
    CryptoWithdrawal,
    CryptoWithdrawalLog,
)


@admin.register(CryptoFeeSettings)
class CryptoFeeSettingsAdmin(admin.ModelAdmin):
    list_display = ('fee_type', 'flat_usd', 'percent', 'is_active', 'updated_at')
    list_editable = ('flat_usd', 'percent', 'is_active')


@admin.register(CryptoWallet)
class CryptoWalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'coin', 'available', 'reserved', 'total', 'updated_at')
    list_filter = ('coin',)
    search_fields = ('user__email', 'user__full_name')
    readonly_fields = ('updated_at',)
    ordering = ('user', 'coin')


@admin.register(CryptoDepositAddress)
class CryptoDepositAddressAdmin(admin.ModelAdmin):
    list_display = ('user', 'coin', 'network', 'address', 'created_at')
    list_filter = ('coin', 'network')
    search_fields = ('user__email', 'address')
    readonly_fields = ('created_at',)


@admin.register(CryptoQuote)
class CryptoQuoteAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'quote_type', 'coin', 'coin_amount', 'rate_ngn', 'fee_ngn', 'expires_at', 'used_at')
    list_filter = ('quote_type', 'coin')
    search_fields = ('user__email',)
    readonly_fields = ('id', 'created_at', 'used_at', 'expires_at')
    ordering = ('-created_at',)


class CryptoOrderLogInline(admin.TabularInline):
    model = CryptoOrderLog
    extra = 0
    readonly_fields = ('event', 'detail', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CryptoOrder)
class CryptoOrderAdmin(admin.ModelAdmin):
    list_display = ('reference', 'user', 'order_type', 'coin', 'coin_amount', 'total_ngn', 'status', 'created_at')
    list_filter = ('order_type', 'status', 'coin')
    search_fields = ('reference', 'user__email', 'user__full_name')
    readonly_fields = ('id', 'reference', 'idempotency_key', 'quote', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    inlines = [CryptoOrderLogInline]

    actions = ['approve_payment', 'confirm_sell_deposit', 'retry_failed_quidax_buy']

    @admin.action(description='Mark payment received → trigger buy execution')
    def approve_payment(self, request, queryset):
        from .views import _execute_buy_after_payment
        updated = 0
        for order in queryset.filter(
            order_type=CryptoOrder.OrderType.BUY,
            status=CryptoOrder.Status.PENDING_PAYMENT,
        ):
            order.status = CryptoOrder.Status.PAYMENT_RECEIVED
            order.save(update_fields=['status', 'updated_at'])
            from .views import _log
            _log(order, 'payment_approved_by_admin', {'admin': request.user.email})
            _execute_buy_after_payment(order)
            updated += 1
        self.message_user(request, f'{updated} order(s) approved.')

    @admin.action(description='Confirm sell deposit received → execute sell')
    def confirm_sell_deposit(self, request, queryset):
        updated = 0
        for order in queryset.filter(
            order_type=CryptoOrder.OrderType.SELL,
            status=CryptoOrder.Status.WAITING_DEPOSIT,
        ):
            order.status = CryptoOrder.Status.DEPOSIT_CONFIRMED
            order.save(update_fields=['status', 'updated_at'])
            from .views import _log, _execute_sell
            _log(order, 'deposit_confirmed_by_admin', {'admin': request.user.email})
            _execute_sell(order, order.coin_amount)
            updated += 1
        self.message_user(request, f'{updated} sell order(s) confirmed.')

    @admin.action(description='Retry Quidax execution for failed buy orders')
    def retry_failed_quidax_buy(self, request, queryset):
        from .views import _debit_ngn_wallet_if_sufficient, _execute_buy_after_payment, _log

        updated = 0
        skipped = 0
        for order in queryset.filter(
            order_type=CryptoOrder.OrderType.BUY,
            status=CryptoOrder.Status.FAILED,
        ):
            was_wallet = order.logs.filter(event='paid_from_wallet').exists()
            was_refunded = order.logs.filter(event='refunded_after_failure').exists()
            if was_wallet and was_refunded:
                if not _debit_ngn_wallet_if_sufficient(order.user, order.total_ngn):
                    skipped += 1
                    continue

            order.status = CryptoOrder.Status.PAYMENT_RECEIVED
            order.save(update_fields=['status', 'updated_at'])
            _log(order, 'quidax_retry_by_admin', {'admin': request.user.email})
            _execute_buy_after_payment(
                order,
                refund_on_failure=not bool(order.flw_transaction_id),
            )
            updated += 1

        msg = f'{updated} failed buy order(s) retried on Quidax.'
        if skipped:
            msg += f' {skipped} skipped (insufficient NGN wallet balance to re-debit).'
        self.message_user(request, msg)


class CryptoWithdrawalLogInline(admin.TabularInline):
    model = CryptoWithdrawalLog
    extra = 0
    readonly_fields = ('event', 'detail', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CryptoWithdrawal)
class CryptoWithdrawalAdmin(admin.ModelAdmin):
    """
    Withdrawals execute instantly with no approval step, so this is mainly
    for visibility — plus a manual safety valve (below) for the rare case
    where Quidax's withdraw.successful/rejected webhook never arrives and a
    withdrawal is stuck in PROCESSING after being confirmed on Quidax's own
    dashboard.
    """
    list_display = ('reference', 'user', 'coin', 'network', 'amount', 'fee', 'status', 'created_at')
    list_filter = ('status', 'coin', 'network')
    search_fields = ('reference', 'address', 'user__email', 'user__full_name', 'quidax_withdrawal_id')
    readonly_fields = ('id', 'reference', 'idempotency_key', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    inlines = [CryptoWithdrawalLogInline]

    actions = ['mark_completed', 'mark_rejected_and_refund']

    @admin.action(description='Mark selected as completed (use only if confirmed on Quidax dashboard)')
    def mark_completed(self, request, queryset):
        from .views import _wlog

        updated = 0
        for w in queryset.exclude(status=CryptoWithdrawal.Status.COMPLETED):
            w.status = CryptoWithdrawal.Status.COMPLETED
            w.save(update_fields=['status', 'updated_at'])
            _wlog(w, 'withdrawal_completed_by_admin', {'admin': request.user.email})
            updated += 1
        self.message_user(request, f'{updated} withdrawal(s) marked completed.')

    @admin.action(description='Mark selected as rejected and refund wallet')
    def mark_rejected_and_refund(self, request, queryset):
        from .views import _credit_wallet, _wlog

        updated = 0
        for w in queryset.exclude(
            status__in=(CryptoWithdrawal.Status.COMPLETED, CryptoWithdrawal.Status.REJECTED),
        ):
            w.status = CryptoWithdrawal.Status.REJECTED
            w.save(update_fields=['status', 'updated_at'])
            _credit_wallet(w.user, w.coin, w.amount)
            _wlog(w, 'withdrawal_rejected_refunded_by_admin', {'admin': request.user.email})
            updated += 1
        self.message_user(request, f'{updated} withdrawal(s) marked rejected and refunded.')
