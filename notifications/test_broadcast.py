from django.contrib.auth import get_user_model
from django.test import TestCase

from .serializers import BroadcastNotificationSerializer


class BroadcastNotificationSerializerTests(TestCase):
    def test_accepts_integer_user_ids_for_specific_users(self):
        user = get_user_model().objects.create_user(
            email='receiver@example.com',
            phone='08000000001',
            full_name='Receiver',
        )

        serializer = BroadcastNotificationSerializer(data={
            'title': 'Test broadcast',
            'body': 'Hello',
            'message_type': 'general',
            'target_scope': 'specific_users',
            'user_ids': [str(user.id)],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        notification = serializer.save(sender=None)
        self.assertEqual(list(notification.audience.values_list('id', flat=True)), [user.id])
