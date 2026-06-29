from decimal import Decimal

from django.contrib.auth.hashers import check_password
from rest_framework import serializers


class BuyGiftCardSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    unit_price_usd = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    brand = serializers.CharField(max_length=100)
    brand_asset = serializers.CharField(max_length=200, required=False, default='')
    country_code = serializers.CharField(max_length=2)
    pin = serializers.CharField(min_length=4, max_length=4)

    def validate(self, attrs):
        user = self.context['request'].user
        if not user.transaction_pin_hash:
            raise serializers.ValidationError(
                {'pin': 'Transaction PIN not set. Please set your PIN in profile settings first.'}
            )
        if not check_password(attrs['pin'], user.transaction_pin_hash):
            raise serializers.ValidationError({'pin': 'Incorrect PIN.'})
        return attrs


class SellGiftCardSerializer(serializers.Serializer):
    brand = serializers.CharField(max_length=100)
    brand_asset = serializers.CharField(max_length=200, required=False, default='')
    country = serializers.CharField(max_length=100)
    card_type = serializers.CharField(max_length=50)
    amount_usd = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('1.00'))
