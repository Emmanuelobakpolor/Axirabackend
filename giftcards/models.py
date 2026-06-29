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
