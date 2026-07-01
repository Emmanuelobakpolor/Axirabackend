import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class CryptoFeeSettings(models.Model):
    class FeeType(models.TextChoices):
        BUY = 'buy', 'Buy'
        SELL = 'sell', 'Sell'
        SWAP = 'swap', 'Swap'

    fee_type = models.CharField(max_length=10, choices=FeeType.choices, unique=True)
    flat_usd = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Crypto Fee Setting'
        verbose_name_plural = 'Crypto Fee Settings'

    def __str__(self):
        return f"{self.fee_type} fee – ${self.flat_usd} flat + {self.percent}%"


class CryptoWallet(models.Model):
    """
    Internal ledger — the authoritative source of truth for each user's crypto balances.
    Quidax wallets are never queried for balance; only this table is.

    available = spendable balance
    reserved  = locked for a pending sell or swap order
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crypto_wallets',
    )
    coin = models.CharField(max_length=20)
    available = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal('0'))
    reserved = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal('0'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'coin')
        indexes = [models.Index(fields=['user', 'coin'])]

    @property
    def total(self):
        return self.available + self.reserved

    def __str__(self):
        return f"{self.user.email} {self.coin}: avail={self.available} res={self.reserved}"


class CryptoDepositAddress(models.Model):
    """
    Per-user, per-coin deposit address sourced from the user's Quidax sub-account.
    Generated on first request and cached here. Never share addresses between users.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crypto_addresses',
    )
    coin = models.CharField(max_length=20)
    network = models.CharField(max_length=30, blank=True)  # e.g. 'TRC20', 'ERC20'
    address = models.CharField(max_length=200)
    quidax_ref = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'coin', 'network')
        indexes = [
            models.Index(fields=['user', 'coin']),
            models.Index(fields=['address']),
        ]

    def __str__(self):
        net = f'/{self.network}' if self.network else ''
        return f"{self.user.email} {self.coin}{net}: {self.address[:20]}..."


class CryptoQuote(models.Model):
    """
    A price-locked quote valid for 60 seconds.
    Must be consumed (used_at set) before it expires.
    An expired or already-used quote is rejected at order creation.
    """
    class QuoteType(models.TextChoices):
        BUY = 'buy', 'Buy'
        SELL = 'sell', 'Sell'
        SWAP = 'swap', 'Swap'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crypto_quotes',
    )
    quote_type = models.CharField(max_length=10, choices=QuoteType.choices)
    coin = models.CharField(max_length=20)
    to_coin = models.CharField(max_length=20, blank=True)
    coin_amount = models.DecimalField(max_digits=20, decimal_places=8)
    rate_ngn = models.DecimalField(max_digits=20, decimal_places=2)
    to_rate_ngn = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('0'))
    fee_ngn = models.DecimalField(max_digits=14, decimal_places=2)
    total_ngn = models.DecimalField(max_digits=14, decimal_places=2)
    to_coin_amount = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal('0'))
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['expires_at']),
        ]

    def is_expired(self):
        return timezone.now() > self.expires_at

    def is_used(self):
        return self.used_at is not None

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=['used_at'])

    def __str__(self):
        return f"{self.quote_type} {self.coin_amount} {self.coin} [{self.id}]"


class CryptoOrder(models.Model):
    class OrderType(models.TextChoices):
        BUY = 'buy', 'Buy'
        SELL = 'sell', 'Sell'
        SWAP = 'swap', 'Swap'

    class Status(models.TextChoices):
        # Buy lifecycle
        PENDING_PAYMENT = 'pending_payment', 'Pending Payment'
        PAYMENT_RECEIVED = 'payment_received', 'Payment Received'
        # Sell lifecycle
        WAITING_DEPOSIT = 'waiting_deposit', 'Waiting for Deposit'
        DEPOSIT_CONFIRMED = 'deposit_confirmed', 'Deposit Confirmed'
        # Shared
        PROCESSING = 'processing', 'Processing on Quidax'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='crypto_orders',
    )
    quote = models.OneToOneField(
        CryptoQuote,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='order',
    )
    order_type = models.CharField(max_length=10, choices=OrderType.choices)
    coin = models.CharField(max_length=20)
    to_coin = models.CharField(max_length=20, blank=True)
    coin_amount = models.DecimalField(max_digits=20, decimal_places=8)
    to_coin_amount = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal('0'))
    rate_ngn = models.DecimalField(max_digits=20, decimal_places=2)
    fee_ngn = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    # Buy: total NGN user pays. Sell: NGN payout. Swap: NGN value of from_coin.
    total_ngn = models.DecimalField(max_digits=14, decimal_places=2)
    payment_proof = models.ImageField(upload_to='crypto_proofs/', null=True, blank=True)
    quidax_order_id = models.CharField(max_length=100, blank=True)
    reference = models.CharField(max_length=40, unique=True, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)
    note = models.TextField(blank=True)
    status = models.CharField(
        max_length=25, choices=Status.choices, default=Status.PENDING_PAYMENT
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status']),
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"CRY{uuid.uuid4().hex[:16].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_type} {self.coin_amount} {self.coin} [{self.status}] – {self.user.email}"


class CryptoOrderLog(models.Model):
    """Immutable audit trail for every significant event on an order."""
    order = models.ForeignKey(CryptoOrder, on_delete=models.CASCADE, related_name='logs')
    event = models.CharField(max_length=60)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    @classmethod
    def log(cls, order, event, detail=None):
        cls.objects.create(order=order, event=event, detail=detail or {})

    def __str__(self):
        return f"[{self.order.reference}] {self.event}"
