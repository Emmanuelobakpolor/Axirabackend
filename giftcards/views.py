from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_transaction
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from wallet.models import Wallet

from .models import GiftCardBuy, GiftCardSale, GiftCardSaleImage
from .reloadly import ReloadlyError, get_brands, get_products, order_gift_card
from .serializers import BuyGiftCardSerializer, SellGiftCardSerializer


def _get_or_create_wallet(user):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


class GiftCardViewSet(GenericViewSet):
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def rate(self, request):
        return Response({'ngn_per_usd': str(settings.RELOADLY_NGN_PER_USD)})

    @action(detail=False, methods=['get'])
    def brands(self, request):
        country_code = request.query_params.get('country_code', 'US').strip().upper()
        try:
            result = get_brands(country_code)
        except ReloadlyError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({'brands': result})

    @action(detail=False, methods=['get'], url_path='debug-products')
    def debug_products(self, request):
        brand = request.query_params.get('brand', '').strip()
        country_code = request.query_params.get('country_code', 'US').strip().upper()
        if not brand:
            return Response({'error': 'brand is required.'})
        try:
            raw = get_products(brand, country_code)
        except ReloadlyError as e:
            return Response({'error': str(e)})
        return Response({'count': len(raw), 'raw': raw[:3]})

    @action(detail=False, methods=['get'])
    def products(self, request):
        brand = request.query_params.get('brand', '').strip()
        country_code = request.query_params.get('country_code', 'US').strip().upper()
        if not brand:
            return Response({'error': 'brand is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            raw = get_products(brand, country_code)
        except ReloadlyError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        rate = Decimal(str(settings.RELOADLY_NGN_PER_USD))
        products = []
        for p in raw:
            fixed = p.get('fixedRecipientDenominations') or []
            if fixed:
                for price in fixed:
                    usd = Decimal(str(price))
                    products.append({
                        'product_id': p['productId'],
                        'product_name': p.get('productName', brand),
                        'unit_price_usd': str(usd),
                        'unit_price_ngn': str((usd * rate).quantize(Decimal('0.01'))),
                        'open_range': False,
                    })
            else:
                min_usd = Decimal(str(p.get('minRecipientDenomination') or 1))
                max_usd = Decimal(str(p.get('maxRecipientDenomination') or min_usd))
                products.append({
                    'product_id': p['productId'],
                    'product_name': p.get('productName', brand),
                    'unit_price_usd': None,
                    'unit_price_ngn': None,
                    'open_range': True,
                    'min_usd': str(min_usd),
                    'max_usd': str(max_usd),
                })

        return Response({'products': products})

    @action(detail=False, methods=['post'])
    def buy(self, request):
        serializer = BuyGiftCardSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        rate = Decimal(str(settings.RELOADLY_NGN_PER_USD))
        amount_ngn = (d['unit_price_usd'] * rate).quantize(Decimal('0.01'))

        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(user=request.user)
            if wallet.ngn_balance < amount_ngn:
                return Response({'error': 'Insufficient balance.'}, status=status.HTTP_400_BAD_REQUEST)
            wallet.ngn_balance -= amount_ngn
            wallet.save(update_fields=['ngn_balance', 'updated_at'])

            order = GiftCardBuy.objects.create(
                user=request.user,
                brand=d['brand'],
                brand_asset=d.get('brand_asset', ''),
                country_code=d['country_code'],
                product_id=d['product_id'],
                unit_price_usd=d['unit_price_usd'],
                rate_ngn=rate,
                amount_ngn=amount_ngn,
                status=GiftCardBuy.Status.PENDING,
            )

        try:
            result = order_gift_card(
                product_id=d['product_id'],
                unit_price=float(d['unit_price_usd']),
                sender_name='Axira',
                recipient_email=request.user.email,
                custom_ref=order.reference,
            )
        except ReloadlyError as e:
            with db_transaction.atomic():
                refund_wallet = Wallet.objects.select_for_update().get(user=request.user)
                refund_wallet.ngn_balance += amount_ngn
                refund_wallet.save(update_fields=['ngn_balance', 'updated_at'])
                order.status = GiftCardBuy.Status.FAILED
                order.save(update_fields=['status', 'updated_at'])
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        cards = result.get('redeemedCards', [])
        redeem_code = cards[0].get('cardNumber', '') if cards else ''
        redeem_pin = cards[0].get('pinCode', '') if cards else ''
        product_name = result.get('product', {}).get('productName', d['brand'])

        order.status = GiftCardBuy.Status.COMPLETED
        order.reloadly_tx_id = str(result.get('transactionId', ''))
        order.product_name = product_name
        order.redeem_code = redeem_code
        order.redeem_pin = redeem_pin
        order.save(update_fields=['status', 'reloadly_tx_id', 'product_name', 'redeem_code', 'redeem_pin', 'updated_at'])

        return Response({
            'reference': order.reference,
            'product_name': product_name,
            'amount_usd': str(d['unit_price_usd']),
            'amount_ngn': str(amount_ngn),
            'redeem_code': redeem_code,
            'redeem_pin': redeem_pin,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def sell(self, request):
        serializer = SellGiftCardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        rate = Decimal(str(settings.RELOADLY_NGN_PER_USD))
        amount_ngn = (d['amount_usd'] * rate).quantize(Decimal('0.01'))

        sale = GiftCardSale.objects.create(
            user=request.user,
            brand=d['brand'],
            brand_asset=d.get('brand_asset', ''),
            country=d['country'],
            card_type=d['card_type'],
            amount_usd=d['amount_usd'],
            rate_ngn=rate,
            amount_ngn=amount_ngn,
        )

        for img_file in request.FILES.getlist('images'):
            GiftCardSaleImage.objects.create(sale=sale, image=img_file)

        return Response({
            'reference': sale.reference,
            'amount_usd': str(d['amount_usd']),
            'amount_ngn': str(amount_ngn),
            'status': sale.status,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def history(self, request):
        buys = GiftCardBuy.objects.filter(user=request.user)[:30]
        sales = GiftCardSale.objects.filter(user=request.user)[:30]

        items = []
        for b in buys:
            items.append({
                'id': b.reference,
                'type': 'buy',
                'brand': b.brand,
                'brand_asset': b.brand_asset,
                'amount_usd': str(b.unit_price_usd),
                'amount_ngn': str(b.amount_ngn),
                'status': b.status,
                'created_at': b.created_at.isoformat(),
                'redeem_code': b.redeem_code,
                'redeem_pin': b.redeem_pin,
                'product_name': b.product_name,
            })
        for s in sales:
            items.append({
                'id': s.reference,
                'type': 'sell',
                'brand': s.brand,
                'brand_asset': s.brand_asset,
                'amount_usd': str(s.amount_usd),
                'amount_ngn': str(s.amount_ngn),
                'status': s.status,
                'created_at': s.created_at.isoformat(),
            })

        items.sort(key=lambda x: x['created_at'], reverse=True)
        return Response({'items': items[:50]})
