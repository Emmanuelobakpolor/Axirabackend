from decimal import Decimal

from rest_framework import serializers

from .models import Transaction


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            'id', 'tx_type', 'amount', 'status', 'reference',
            'bank_name', 'account_number', 'account_name',
            'note', 'created_at',
        ]
        read_only_fields = fields


class InitiateDepositSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal('100'),
    )
    tx_ref = serializers.CharField(max_length=100)

    def validate_tx_ref(self, value):
        if Transaction.objects.filter(flw_tx_ref=value).exists():
            raise serializers.ValidationError('Duplicate transaction reference.')
        return value


class DepositNotifySerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal('100'),
    )


class WithdrawalSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal('500'),
    )
    account_name = serializers.CharField(max_length=150)
    bank_name = serializers.CharField(max_length=100)
    bank_code = serializers.CharField(max_length=20)
    account_number = serializers.CharField(max_length=20)

    def validate_account_number(self, value):
        if not value.isdigit() or len(value) != 10:
            raise serializers.ValidationError(
                'Account number must be exactly 10 digits.'
            )
        return value

    def validate(self, attrs):
        user = self.context['request'].user
        wallet = getattr(user, 'wallet', None)
        if wallet is None or wallet.ngn_balance < attrs['amount']:
            raise serializers.ValidationError({'amount': 'Insufficient balance.'})
        return attrs
