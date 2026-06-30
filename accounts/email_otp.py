import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils.crypto import get_random_string

logger = logging.getLogger(__name__)

# When SENDGRID_API_KEY is not configured, every OTP is this fixed value
# so the app works during development/testing without an email provider.
_DEV_OTP = "1234"


def generate_otp() -> str:
    """Returns a real random 4-digit OTP in production, or _DEV_OTP in dev."""
    if not settings.SENDGRID_API_KEY:
        return _DEV_OTP
    return get_random_string(4, allowed_chars="0123456789")


def send_otp_email(email: str, otp: str) -> bool:
    """
    Send a one-time password via email (SendGrid in production,
    console output locally when SENDGRID_API_KEY is not set).
    Returns True on success, False on failure.
    """
    subject = "Your Axira Verification Code"
    message = (
        f"Your Axira verification code is: {otp}\n\n"
        "This code is valid for 5 minutes.\n"
        "Do not share this code with anyone."
    )
    html_message = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;background:#f9fafb;border-radius:12px;">
      <h2 style="color:#0E24A0;margin-bottom:8px;">Axira</h2>
      <p style="color:#374151;font-size:15px;">Your verification code is:</p>
      <div style="background:#ffffff;border:1px solid #E5E7EB;border-radius:10px;padding:24px;text-align:center;margin:24px 0;">
        <span style="font-size:36px;font-weight:bold;letter-spacing:8px;color:#111827;">{otp}</span>
      </div>
      <p style="color:#6B7280;font-size:13px;">Valid for <strong>5 minutes</strong>. Do not share this code with anyone.</p>
      <p style="color:#9CA3AF;font-size:12px;margin-top:24px;">If you did not request this code, ignore this email.</p>
    </div>
    """
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        return True
    except Exception as exc:
        logger.error("OTP email failed for %s: %s", email, exc)
        return False
