from django.apps import AppConfig


class CryptoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crypto'

    def ready(self):
        import crypto.signals  # noqa: F401 — registers post_save signal
