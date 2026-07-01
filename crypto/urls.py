from django.urls import path

from .views import (
    CryptoBuyOrderView,
    CryptoDepositAddressView,
    CryptoFeesView,
    CryptoOrdersView,
    CryptoPricesView,
    CryptoQuoteView,
    CryptoSellOrderView,
    CryptoSwapOrderView,
    CryptoUploadProofView,
    CryptoWalletView,
    CryptoWebhookView,
)

urlpatterns = [
    # Public-ish data
    path('crypto/prices/', CryptoPricesView.as_view()),
    path('crypto/fees/', CryptoFeesView.as_view()),

    # Quote — must call before placing any order
    path('crypto/quote/', CryptoQuoteView.as_view()),

    # User wallets and addresses
    path('crypto/wallets/', CryptoWalletView.as_view()),
    path('crypto/address/<str:coin>/', CryptoDepositAddressView.as_view()),

    # Orders
    path('crypto/orders/', CryptoOrdersView.as_view()),
    path('crypto/orders/buy/', CryptoBuyOrderView.as_view()),
    path('crypto/orders/sell/', CryptoSellOrderView.as_view()),
    path('crypto/orders/swap/', CryptoSwapOrderView.as_view()),
    path('crypto/orders/<str:reference>/proof/', CryptoUploadProofView.as_view()),

    # Webhook (no auth — HMAC verified internally)
    path('crypto/webhook/quidax/', CryptoWebhookView.as_view()),
]
