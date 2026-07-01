"""
Django signals for the crypto app.
Creates a Quidax sub-account automatically when a new Axira user registers.
If creation fails, the user still registers — a background retry or admin
action can provision the sub-account later.
"""
import logging

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

User = get_user_model()


@receiver(post_save, sender=User)
def provision_quidax_sub_account(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.quidax_user_id:
        # Already provisioned (e.g. admin-created user with explicit UID)
        return

    try:
        from django.conf import settings
        if not getattr(settings, 'QUIDAX_SECRET_KEY', ''):
            return  # No Quidax key configured — skip silently

        from .quidax import QuidaxError, create_sub_account

        name_parts = instance.full_name.strip().split(None, 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else first_name

        result = create_sub_account(
            email=instance.email,
            first_name=first_name,
            last_name=last_name,
        )
        quidax_uid = str(result.get('id') or result.get('uid') or '')
        if quidax_uid:
            User.objects.filter(pk=instance.pk).update(quidax_user_id=quidax_uid)
            logger.info('Quidax sub-account created for %s → uid=%s', instance.email, quidax_uid)
        else:
            logger.warning('Quidax sub-account response missing id for %s: %s', instance.email, result)

    except Exception as exc:
        # Never block registration due to Quidax being unavailable
        logger.error('Quidax sub-account creation failed for %s: %s', instance.email, exc)
