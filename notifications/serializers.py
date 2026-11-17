from rest_framework import serializers

from accounts.models import User
from .models import AppNotification, UserDeviceToken


class UserDeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDeviceToken
        fields = ['token', 'is_active']

    def validate_token(self, value):
        return value.strip()


class BroadcastNotificationSerializer(serializers.ModelSerializer):
    user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        default=list,
    )

    class Meta:
        model = AppNotification
        fields = ['title', 'body', 'message_type', 'target_scope', 'user_ids']

    def validate(self, attrs):
        target_scope = attrs.get('target_scope')
        user_ids = attrs.get('user_ids', [])

        if target_scope == AppNotification.TargetScope.SPECIFIC_USERS and not user_ids:
            raise serializers.ValidationError(
                {'user_ids': 'Select at least one user when sending to specific users.'}
            )

        if target_scope == AppNotification.TargetScope.ALL_USERS:
            attrs['user_ids'] = []

        return attrs

    def create(self, validated_data):
        user_ids = validated_data.pop('user_ids', [])
        notification = AppNotification.objects.create(**validated_data)
        if user_ids:
            selected_users = User.objects.filter(id__in=user_ids)
            notification.audience.set(selected_users)
        return notification
