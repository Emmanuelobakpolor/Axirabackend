from decimal import Decimal

from django.contrib.auth.hashers import check_password
from rest_framework import serializers

from .models import SELL_RATE_COUNTRIES, GiftCardSellRate


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
    country = serializers.CharField(max_length=2)
    card_type = serializers.ChoiceField(choices=GiftCardSellRate.CardType.choices)
    amount_usd = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('1.00'))

    def validate_country(self, value):
        return value.strip().upper()

    def validate(self, attrs):
        rate = GiftCardSellRate.find(
            attrs['brand'], attrs['country'], attrs['card_type'], attrs['amount_usd'],
        )
        if rate is None:
            raise serializers.ValidationError(
                {'detail': "We're not buying this card at this value right now."}
            )
        attrs['sell_rate'] = rate
        return attrs


class GiftCardSellRateSerializer(serializers.ModelSerializer):
    country_name = serializers.CharField(read_only=True)
    card_type_label = serializers.CharField(source='get_card_type_display', read_only=True)

    class Meta:
        model = GiftCardSellRate
        fields = [
            'id', 'brand', 'logo_url', 'country', 'country_name', 'card_type',
            'card_type_label', 'min_usd', 'max_usd', 'rate_ngn', 'is_active',
            'updated_at',
        ]
        read_only_fields = ['id', 'updated_at']
        # Required so the overlap check below always has a full value band.
        extra_kwargs = {'min_usd': {'required': True}, 'max_usd': {'required': True}}

    def validate_brand(self, value):
        return value.strip()

    def validate_country(self, value):
        value = value.strip().upper()
        if value not in SELL_RATE_COUNTRIES:
            raise serializers.ValidationError('Unsupported country.')
        return value

    def validate_rate_ngn(self, value):
        if value <= 0:
            raise serializers.ValidationError('Rate must be greater than zero.')
        return value

    def validate(self, attrs):
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field) if self.instance else None

        min_usd, max_usd = current('min_usd'), current('max_usd')
        if min_usd is not None and max_usd is not None:
            if min_usd < 1:
                raise serializers.ValidationError({'min_usd': 'Minimum must be at least $1.'})
            if min_usd > max_usd:
                raise serializers.ValidationError(
                    {'max_usd': 'Maximum must be at least the minimum.'}
                )

        # Two active rows covering the same card value would make the payout
        # ambiguous, so value bands for one brand/country/type can't overlap.
        if current('is_active') is not False:
            clash = GiftCardSellRate.objects.filter(
                brand__iexact=current('brand'),
                country=current('country'),
                card_type=current('card_type'),
                is_active=True,
                min_usd__lte=max_usd,
                max_usd__gte=min_usd,
            )
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            other = clash.first()
            if other:
                raise serializers.ValidationError({
                    'detail': (
                        f'Overlaps the existing ${other.min_usd}–${other.max_usd} '
                        f'rate for this card. Adjust the value range.'
                    )
                })
        return attrs
