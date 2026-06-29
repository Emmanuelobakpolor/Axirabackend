import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def send_otp_sms(phone: str, otp: str) -> bool:
    """
    Send a one-time password via Twilio SMS.
    Returns True on success, False on failure (logs the error).
    Falls back to console print when TWILIO_ACCOUNT_SID is not configured
    (local development).
    """
    if not settings.TWILIO_ACCOUNT_SID:
        # Local dev fallback — print to console
        print(f"[AXIRA DEV] OTP for {phone}: {otp}")
        return True

    try:
        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=(
                f"Your Axira verification code is: {otp}\n"
                "Valid for 5 minutes. Do not share this code."
            ),
            from_=settings.TWILIO_PHONE_NUMBER,
            to=phone,
        )
        return True

    except Exception as exc:
        logger.error("Twilio SMS failed for %s: %s", phone, exc)
        return False
