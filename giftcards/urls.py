from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import GiftCardViewSet

router = DefaultRouter()
router.register(r'giftcards', GiftCardViewSet, basename='giftcards')

urlpatterns = [
    path('', include(router.urls)),
]
