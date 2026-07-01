from django.contrib import admin

from .models import (
    CryptoDepositAddress,
    CryptoFeeSettings,
    CryptoOrder,
    CryptoOrderLog,
    CryptoQuote,
    CryptoWallet,
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

    actions = ['approve_payment', 'confirm_sell_deposit']

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
