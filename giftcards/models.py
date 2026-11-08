import uuid

from django.conf import settings
from django.db import models


class GiftCardBuy(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='gc_purchases')
    brand = models.CharField(max_length=100)
    brand_asset = models.CharField(max_length=200, blank=True)
    country_code = models.CharField(max_length=2)
    product_id = models.IntegerField()
    product_name = models.CharField(max_length=200, blank=True)
    unit_price_usd = models.DecimalField(max_digits=10, decimal_places=2)
    rate_ngn = models.DecimalField(max_digits=10, decimal_places=2)
    amount_ngn = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=40, unique=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reloadly_tx_id = models.CharField(max_length=100, blank=True)
    redeem_code = models.CharField(max_length=500, blank=True)
    redeem_pin = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = 'GCBUY-' + uuid.uuid4().hex[:14].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Buy {self.brand} ${self.unit_price_usd} [{self.status}] – {self.user.email}"


class GiftCardSale(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        DECLINED = 'declined', 'Declined'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='gc_sales')
    brand = models.CharField(max_length=100)
    brand_asset = models.CharField(max_length=200, blank=True)
    country = models.CharField(max_length=100)
    card_type = models.CharField(max_length=50)
    amount_usd = models.DecimalField(max_digits=10, decimal_places=2)
    rate_ngn = models.DecimalField(max_digits=10, decimal_places=2)
    amount_ngn = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=40, unique=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = 'GCSALE-' + uuid.uuid4().hex[:13].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Sell {self.brand} ${self.amount_usd} [{self.status}] – {self.user.email}"


class GiftCardSaleImage(models.Model):
    sale = models.ForeignKey(GiftCardSale, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='giftcard_sales/')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Image for {self.sale.reference}"


# Countries offered when setting sell rates. Codes are what's stored; names
# are what the app and admin dashboard show.
SELL_RATE_COUNTRIES = {
    'US': 'United States',
    'CA': 'Canada',
    'GB': 'United Kingdom',
    'AU': 'Australia',
    'NZ': 'New Zealand',
    'DE': 'Germany',
    'FR': 'France',
    'IT': 'Italy',
    'ES': 'Spain',
    'NL': 'Netherlands',
    'IE': 'Ireland',
    'CH': 'Switzerland',
}


class GiftCardSellRate(models.Model):
    """
    What Axira pays, in NGN per $1 of card value, when a customer sells a
    gift card. Rates vary by brand, country, card type and value band
    (e.g. Amazon US e-code $100–$500 can pay more than $25–$99), so there
    is one row per combination. A sale is only accepted if it matches an
    active row, and it's paid at that row's rate.
    """

    class CardType(models.TextChoices):
        PHYSICAL = 'physical', 'Physical Card'
        ECODE = 'ecode', 'E-Code'

    brand = models.CharField(max_length=100)
    logo_url = models.CharField(max_length=300, blank=True)
    country = models.CharField(max_length=2)
    card_type = models.CharField(max_length=20, choices=CardType.choices)
    min_usd = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    max_usd = models.DecimalField(max_digits=10, decimal_places=2, default=1000)
    rate_ngn = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['brand', 'country', 'card_type', 'min_usd']

    @property
    def country_name(self):
        return SELL_RATE_COUNTRIES.get(self.country, self.country)

    @classmethod
    def find(cls, brand, country, card_type, amount_usd):
        """The active rate for this card, or None if Axira isn't buying it."""
        return (
            cls.objects.filter(
                brand__iexact=brand,
                country=country,
                card_type=card_type,
                is_active=True,
                min_usd__lte=amount_usd,
                max_usd__gte=amount_usd,
            )
            .order_by('-min_usd')
            .first()
        )

    def __str__(self):
        return (
            f"{self.brand} {self.country} {self.get_card_type_display()} "
            f"${self.min_usd}–${self.max_usd} @ ₦{self.rate_ngn}"
        )
