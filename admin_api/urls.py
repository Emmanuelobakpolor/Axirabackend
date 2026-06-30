from django.urls import path

from .views import AdminOverviewView, AdminTransactionsView, AdminUserDetailView, AdminUsersView

urlpatterns = [
    path('admin/overview/', AdminOverviewView.as_view()),
    path('admin/users/', AdminUsersView.as_view()),
    path('admin/users/<str:user_id>/', AdminUserDetailView.as_view()),
    path('admin/transactions/', AdminTransactionsView.as_view()),
]
