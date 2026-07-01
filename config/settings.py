import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-change-this-in-production")

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS", "localhost,127.0.0.1,10.0.2.2,0.0.0.0"
).split(",")

# ── Apps ──────────────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "cloudinary_storage",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "accounts",
    "wallet",
    "giftcards",
    "crypto",
    "admin_api",
]

# ── Third-party API keys (never hardcoded) ────────────────────────────────────
FLUTTERWAVE_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "")
FLUTTERWAVE_PUBLIC_KEY = os.environ.get("FLW_PUBLIC_KEY", "")
FLUTTERWAVE_WEBHOOK_HASH = os.environ.get("FLW_WEBHOOK_HASH", "")

RELOADLY_CLIENT_ID = os.environ.get("RELOADLY_CLIENT_ID", "")
RELOADLY_CLIENT_SECRET = os.environ.get("RELOADLY_CLIENT_SECRET", "")
RELOADLY_SANDBOX = os.environ.get("RELOADLY_SANDBOX", "true").lower() == "true"
RELOADLY_NGN_PER_USD = float(os.environ.get("RELOADLY_NGN_PER_USD", "1700"))

# ── Quidax (crypto buy/sell/swap) ─────────────────────────────────────────────
QUIDAX_SECRET_KEY = os.environ.get("QUIDAX_SECRET_KEY", "")
QUIDAX_USER_ID = os.environ.get("QUIDAX_USER_ID", "me")  # 'me' = master account

# NGN/USD rate for converting flat USD fees → NGN
NGN_PER_USD = float(os.environ.get("NGN_PER_USD", "1600"))

# Axira's bank account (displayed to users for buy orders)
AXIRA_BANK_NAME = os.environ.get("AXIRA_BANK_NAME", "")
AXIRA_ACCOUNT_NUMBER = os.environ.get("AXIRA_ACCOUNT_NUMBER", "")
AXIRA_ACCOUNT_NAME = os.environ.get("AXIRA_ACCOUNT_NAME", "")

# Axira's crypto deposit addresses (for sell orders — users send crypto here)
AXIRA_BTC_ADDRESS = os.environ.get("AXIRA_BTC_ADDRESS", "")
AXIRA_ETH_ADDRESS = os.environ.get("AXIRA_ETH_ADDRESS", "")
AXIRA_USDT_ADDRESS = os.environ.get("AXIRA_USDT_ADDRESS", "")
AXIRA_SOL_ADDRESS = os.environ.get("AXIRA_SOL_ADDRESS", "")
AXIRA_BNB_ADDRESS = os.environ.get("AXIRA_BNB_ADDRESS", "")
AXIRA_XRP_ADDRESS = os.environ.get("AXIRA_XRP_ADDRESS", "")
AXIRA_USDC_ADDRESS = os.environ.get("AXIRA_USDC_ADDRESS", "")

# Email — SendGrid SMTP (OTP verification)
# Locally (no SENDGRID_API_KEY): prints to console via ConsoleEmailBackend.
# In production: set SENDGRID_API_KEY and DEFAULT_FROM_EMAIL in Railway env vars.
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@axira.com")

if SENDGRID_API_KEY:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.sendgrid.net"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = "apikey"
    EMAIL_HOST_PASSWORD = SENDGRID_API_KEY
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ── Middleware ─────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ── Database ──────────────────────────────────────────────────────────────────
# Railway sets DATABASE_URL automatically when you add a Postgres service.
# Locally it falls back to SQLite.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

# ── Password validation ────────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Internationalisation ───────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── Static & media files ───────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Cloudinary — all ImageField / FileField uploads go here when configured.
# Locally (no CLOUDINARY_CLOUD_NAME in .env) falls back to local media storage.
if os.environ.get("CLOUDINARY_CLOUD_NAME"):
    CLOUDINARY_STORAGE = {
        "CLOUD_NAME": os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
        "API_KEY": os.environ.get("CLOUDINARY_API_KEY", ""),
        "API_SECRET": os.environ.get("CLOUDINARY_API_SECRET", ""),
    }
    DEFAULT_FILE_STORAGE = "cloudinary_storage.storage.MediaCloudinaryStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

# ── CORS ──────────────────────────────────────────────────────────────────────
# For a mobile Flutter app CORS is not enforced by the device, but Django admin
# needs it for local dev. In production add your Railway URL to CORS_ALLOWED_ORIGINS.
_cors_origins = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080",
)
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(",") if o.strip()]

_csrf_origins = os.environ.get(
    "CSRF_TRUSTED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080",
)
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]

# ── DRF ───────────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}
