from django.urls import path

from .views import BroadcastNotificationView, UserDeviceTokenView, UserNotificationsView

urlpatterns = [
    path('notifications/device-token/', UserDeviceTokenView.as_view(), name='device_token'),
    path('notifications/me/', UserNotificationsView.as_view(), name='user_notifications'),
    path('admin/notifications/broadcast/', BroadcastNotificationView.as_view(), name='broadcast_notification'),
]
