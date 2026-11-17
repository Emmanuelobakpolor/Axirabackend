import uuid

from django.conf import settings
from django.db import models


class AppNotification(models.Model):
    class TargetScope(models.TextChoices):
        ALL_USERS = 'all_users', 'All users'
        SPECIFIC_USERS = 'specific_users', 'Specific users'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_notifications',
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    message_type = models.CharField(max_length=50, default='general')
    target_scope = models.CharField(
        max_length=20,
        choices=TargetScope.choices,
        default=TargetScope.ALL_USERS,
    )
    audience = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='notifications',
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} [{self.target_scope}]'


class UserDeviceToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='fcm_tokens',
    )
    token = models.TextField(unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'token')
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.user.email} :: {self.token[:20]}'
