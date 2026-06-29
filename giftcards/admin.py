from django.contrib import admin
from django.db import transaction as db_transaction

from wallet.models import Wallet

from .models import GiftCardBuy, GiftCardSale, GiftCardSaleImage


@admin.register(GiftCardBuy)
class GiftCardBuyAdmin(admin.ModelAdmin):
    list_display = ['reference', 'user', 'brand', 'unit_price_usd', 'amount_ngn', 'status', 'created_at']
    list_filter = ['status', 'brand']
    readonly_fields = ['reference', 'reloadly_tx_id', 'redeem_code', 'redeem_pin', 'created_at', 'updated_at']
    search_fields = ['user__email', 'reference', 'brand']


class GiftCardSaleImageInline(admin.TabularInline):
    model = GiftCardSaleImage
    extra = 0
    readonly_fields = ['image', 'created_at']


@admin.register(GiftCardSale)
class GiftCardSaleAdmin(admin.ModelAdmin):
    list_display = ['reference', 'user', 'brand', 'amount_usd', 'amount_ngn', 'status', 'created_at']
    list_filter = ['status', 'brand']
    readonly_fields = ['reference', 'rate_ngn', 'amount_ngn', 'created_at', 'updated_at']
    search_fields = ['user__email', 'reference', 'brand']
    inlines = [GiftCardSaleImageInline]
    actions = ['approve_sales', 'decline_sales']

    @admin.action(description='Approve selected sales (credit NGN balance)')
    def approve_sales(self, request, queryset):
        credited = 0
        for sale in queryset.filter(status=GiftCardSale.Status.PENDING):
            with db_transaction.atomic():
                wallet, _ = Wallet.objects.get_or_create(user=sale.user)
                wallet = Wallet.objects.select_for_update().get(user=sale.user)
                wallet.ngn_balance += sale.amount_ngn
                wallet.save(update_fields=['ngn_balance', 'updated_at'])
                sale.status = GiftCardSale.Status.APPROVED
                sale.save(update_fields=['status', 'updated_at'])
            credited += 1
        self.message_user(request, f"Approved {credited} sale(s) and credited NGN balances.")

    @admin.action(description='Decline selected sales')
    def decline_sales(self, request, queryset):
        updated = queryset.filter(status=GiftCardSale.Status.PENDING).update(
            status=GiftCardSale.Status.DECLINED
        )
        self.message_user(request, f"Declined {updated} sale(s).")
