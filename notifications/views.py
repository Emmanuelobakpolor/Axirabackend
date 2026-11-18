import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AppNotification, UserDeviceToken
from .serializers import BroadcastNotificationSerializer, UserDeviceTokenSerializer

User = get_user_model()


class UserDeviceTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = UserDeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['token']

        obj, created = UserDeviceToken.objects.get_or_create(
            user=request.user,
            token=token,
        )

        if not created:
            obj.is_active = True
            obj.save(update_fields=['is_active', 'updated_at'])

        return Response({'message': 'Device token saved.'}, status=status.HTTP_200_OK)

    def delete(self, request):
        token = request.data.get('token')
        if not token:
            return Response({'error': 'token is required.'}, status=status.HTTP_400_BAD_REQUEST)

        UserDeviceToken.objects.filter(user=request.user, token=token).delete()
        return Response({'message': 'Device token removed.'})


class UserNotificationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = AppNotification.objects.filter(
            is_active=True,
        ).filter(
            Q(target_scope=AppNotification.TargetScope.ALL_USERS)
            | Q(audience=request.user)
        ).order_by('-created_at')

        data = [
            {
                'id': str(n.id),
                'title': n.title,
                'body': n.body,
                'type': n.message_type,
                'created_at': n.created_at.isoformat(),
            }
            for n in notifications
        ]
        return Response({'notifications': data})


class BroadcastNotificationView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = BroadcastNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notification = serializer.save(sender=request.user)

        target_users = User.objects.filter(is_active=True)
        if notification.target_scope == AppNotification.TargetScope.SPECIFIC_USERS:
            target_users = notification.audience.filter(is_active=True)

        target_user_ids = list(target_users.values_list('id', flat=True))

        tokens = list(
            UserDeviceToken.objects.filter(
                user_id__in=target_user_ids,
                is_active=True,
            ).values_list('token', flat=True).distinct()
        )

        push_status = 'not_configured'
        if tokens:
            import requests

            payload = {
                'registration_ids': tokens,
                'notification': {
                    'title': notification.title,
                    'body': notification.body,
                },
                'data': {
                    'type': notification.message_type,
                    'title': notification.title,
                    'body': notification.body,
                    'broadcast': 'true',
                },
            }

            server_key = getattr(settings, 'FCM_SERVER_KEY', '')
            if server_key:
                headers = {'Authorization': f'key={server_key}', 'Content-Type': 'application/json'}
                try:
                    push_response = requests.post(
                        'https://fcm.googleapis.com/fcm/send',
                        data=json.dumps(payload),
                        headers=headers,
                        timeout=20,
                    )
                    push_status = 'sent' if push_response.status_code < 300 else f'failed_{push_response.status_code}'
                except requests.RequestException:
                    push_status = 'request_failed'
            else:
                push_status = 'no_server_key'
        else:
            push_status = 'no_device_tokens'

        return Response(
            {
                'message': 'Broadcast sent.',
                'target_users': len(target_user_ids),
                'notification_id': str(notification.id),
                'device_tokens': len(tokens),
                'push_status': push_status,
            },
            status=status.HTTP_201_CREATED,
        )
