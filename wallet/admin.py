from django.contrib import admin
from django.db import transaction as db_transaction

from .models import Transaction, Wallet


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ['user', 'ngn_balance', 'updated_at']
    search_fields = ['user__email', 'user__full_name']
    readonly_fields = ['updated_at']


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['reference', 'user', 'tx_type', 'amount', 'status', 'created_at']
    list_filter = ['tx_type', 'status']
    search_fields = ['user__email', 'reference']
    readonly_fields = ['id', 'reference', 'created_at', 'updated_at']
    actions = ['approve_deposits', 'fail_transactions']

    @admin.action(description='Approve selected deposits (credit wallet)')
    def approve_deposits(self, request, queryset):
        approved = 0
        for tx in queryset.filter(tx_type=Transaction.TxType.DEPOSIT, status=Transaction.Status.PENDING):
            with db_transaction.atomic():
                wallet, _ = Wallet.objects.select_for_update().get_or_create(user=tx.user)
                wallet.ngn_balance += tx.amount
                wallet.save(update_fields=['ngn_balance', 'updated_at'])
                tx.status = Transaction.Status.COMPLETED
                tx.save(update_fields=['status', 'updated_at'])
                approved += 1
        self.message_user(request, f'{approved} deposit(s) approved and wallets credited.')

    @admin.action(description='Mark selected as failed')
    def fail_transactions(self, request, queryset):
        # For failed withdrawals, refund the balance
        refunded = 0
        for tx in queryset.filter(status=Transaction.Status.PENDING):
            with db_transaction.atomic():
                if tx.tx_type == Transaction.TxType.WITHDRAWAL:
                    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=tx.user)
                    wallet.ngn_balance += tx.amount
                    wallet.save(update_fields=['ngn_balance', 'updated_at'])
                    refunded += 1
                tx.status = Transaction.Status.FAILED
                tx.save(update_fields=['status', 'updated_at'])
        self.message_user(request, f'Transactions marked failed. {refunded} withdrawal(s) refunded.')
