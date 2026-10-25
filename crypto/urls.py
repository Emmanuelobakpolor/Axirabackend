from django.urls import path

from .views import (
    CryptoBuyOrderView,
    CryptoBuyVerifyView,
    CryptoDepositAddressView,
    CryptoDepositsView,
    CryptoFeesView,
    CryptoLimitsView,
    CryptoOrdersView,
    CryptoPricesView,
    CryptoQuoteView,
    CryptoSellOrderView,
    CryptoSwapOrderView,
    CryptoUploadProofView,
    CryptoWalletView,
    CryptoWebhookView,
    CryptoWithdrawalDetailView,
    CryptoWithdrawalsView,
    CryptoWithdrawView,
)

urlpatterns = [
    # Public-ish data
    path('crypto/prices/', CryptoPricesView.as_view()),
    path('crypto/fees/', CryptoFeesView.as_view()),
    path('crypto/limits/', CryptoLimitsView.as_view()),

    # Quote — must call before placing any order
    path('crypto/quote/', CryptoQuoteView.as_view()),

    # User wallets and addresses
    path('crypto/wallets/', CryptoWalletView.as_view()),
    path('crypto/address/<str:coin>/', CryptoDepositAddressView.as_view()),
    path('crypto/deposits/', CryptoDepositsView.as_view()),

    # Orders
    path('crypto/orders/', CryptoOrdersView.as_view()),
    path('crypto/orders/buy/', CryptoBuyOrderView.as_view()),
    path('crypto/orders/buy/verify/', CryptoBuyVerifyView.as_view()),
    path('crypto/orders/sell/', CryptoSellOrderView.as_view()),
    path('crypto/orders/swap/', CryptoSwapOrderView.as_view()),
    path('crypto/orders/<str:reference>/proof/', CryptoUploadProofView.as_view()),

    # Withdrawals (send crypto to an external address)
    path('crypto/withdraw/', CryptoWithdrawView.as_view()),
    path('crypto/withdrawals/', CryptoWithdrawalsView.as_view()),
    path('crypto/withdrawals/<str:reference>/', CryptoWithdrawalDetailView.as_view()),

    # Webhook (no auth — HMAC verified internally)
    path('crypto/webhook/quidax/', CryptoWebhookView.as_view()),
]
