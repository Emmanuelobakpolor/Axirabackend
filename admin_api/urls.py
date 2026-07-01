from django.urls import path

from .views import (
    AdminCryptoOrderActionView,
    AdminCryptoOrdersView,
    AdminFeeDetailView,
    AdminFeesView,
    AdminOverviewView,
    AdminProfileView,
    AdminTransactionsView,
    AdminUserDetailView,
    AdminUsersView,
)

urlpatterns = [
    # Admin's own profile
    path('admin/profile/', AdminProfileView.as_view()),

    path('admin/overview/', AdminOverviewView.as_view()),
    path('admin/users/', AdminUsersView.as_view()),
    path('admin/users/<str:user_id>/', AdminUserDetailView.as_view()),
    path('admin/transactions/', AdminTransactionsView.as_view()),

    # Crypto fee settings
    path('admin/fees/', AdminFeesView.as_view()),
    path('admin/fees/<int:fee_id>/', AdminFeeDetailView.as_view()),

    # Crypto order management
    path('admin/crypto/orders/', AdminCryptoOrdersView.as_view()),
    path('admin/crypto/orders/<str:reference>/', AdminCryptoOrderActionView.as_view()),
]
