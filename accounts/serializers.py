import re
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers

from .email_otp import generate_otp
from .models import SignupSession, User

# Dial codes for every country offered in the signup country picker
# (lib/screens/auth/create_account_screen.dart). Sorted longest-first so a
# 3-digit code like 254 isn't shadowed by a 1-digit prefix match.
_DIAL_CODES = sorted(
    {
        "234", "233", "254", "27", "20", "255", "256", "250", "221", "225",
        "237", "260", "263", "251", "232", "231", "44", "1", "49", "33",
        "39", "34", "31", "971", "966", "91", "86", "55", "61",
    },
    key=len,
    reverse=True,
)

MAX_SUBSCRIBER_DIGITS = 10


def normalize_phone(value):
    raw = (value or "").strip()
    digits = re.sub(r"\D", "", raw)

    if not raw.startswith("+") and digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]

    dial_code = next((d for d in _DIAL_CODES if digits.startswith(d)), None)
    if not dial_code:
        raise serializers.ValidationError(
            "Phone must start with a valid country code, for example +2348123456780."
        )

    subscriber = digits[len(dial_code):]
    if not (7 <= len(subscriber) <= MAX_SUBSCRIBER_DIGITS):
        raise serializers.ValidationError(
            f"Phone number must be at most {MAX_SUBSCRIBER_DIGITS} digits "
            "after the country code."
        )

    return f"+{digits}"


class StartSignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone = serializers.CharField()

    def validate_email(self, value):
        return value.lower().strip()

    def validate_phone(self, value):
        return normalize_phone(value)

    def validate(self, attrs):
        email = attrs["email"]
        phone = attrs["phone"]

        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                {"email": "An account with this email already exists."}
            )

        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError(
                {"phone": "An account with this phone number already exists."}
            )

        return attrs

    def create(self, validated_data):
        otp = generate_otp()

        session = SignupSession.objects.create(
            email=validated_data["email"],
            phone=validated_data["phone"],
            otp_hash=make_password(otp),
            otp_expires_at=timezone.now() + timedelta(minutes=5),
        )

        session.raw_otp = otp

        return session


class VerifyOtpSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    otp = serializers.CharField(max_length=4)

    def validate_otp(self, value):
        if not re.fullmatch(r"\d{4}", value):
            raise serializers.ValidationError("OTP must be exactly 4 digits.")

        return value


class ResendOtpSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()


class CompleteSignupSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    full_name = serializers.CharField(min_length=2, max_length=150)
    password = serializers.CharField(write_only=True, min_length=8)
    transaction_pin = serializers.CharField(write_only=True, max_length=4)

    def validate_full_name(self, value):
        value = value.strip()

        if len(value) < 2:
            raise serializers.ValidationError("Full name must be at least 2 characters.")

        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_transaction_pin(self, value):
        if not re.fullmatch(r"\d{4}", value):
            raise serializers.ValidationError("Transaction PIN must be exactly 4 digits.")

        return value


class UpdateProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(required=False, min_length=2, max_length=150)
    phone = serializers.CharField(required=False)

    class Meta:
        model = User
        fields = [
            "full_name",
            "phone",
        ]

    def validate_full_name(self, value):
        value = value.strip()

        if len(value) < 2:
            raise serializers.ValidationError("Full name must be at least 2 characters.")

        return value

    def validate_phone(self, value):
        return normalize_phone(value)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                {"detail": "Provide full_name or phone to update."}
            )

        phone = attrs.get("phone")
        if phone:
            exists = User.objects.exclude(pk=self.instance.pk).filter(phone=phone)
            if exists.exists():
                raise serializers.ValidationError(
                    {"phone": "This phone number is already in use."}
                )

        return attrs


class ProfilePhotoUploadSerializer(serializers.Serializer):
    profile_photo = serializers.ImageField()

    def validate_profile_photo(self, value):
        if value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("Profile photo must be 5MB or smaller.")

        extension = value.name.split(".")[-1].lower()

        if extension not in {"jpg", "jpeg", "png", "webp"}:
            raise serializers.ValidationError(
                "Profile photo must be a JPG, PNG, or WebP image."
            )

        return value


class ForgotPasswordSerializer(serializers.Serializer):
    identifier = serializers.CharField()

    def validate_identifier(self, value):
        value = value.strip()
        if "@" in value:
            user = User.objects.filter(email__iexact=value).first()
            if not user:
                raise serializers.ValidationError("No account found with this email.")
            return value.lower()
        else:
            try:
                phone = normalize_phone(value)
            except serializers.ValidationError:
                raise serializers.ValidationError(
                    "Enter a valid email address or phone number."
                )
            user = User.objects.filter(phone=phone).first()
            if not user:
                raise serializers.ValidationError(
                    "No account found with this phone number."
                )
            return phone


class VerifyResetOtpSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    otp = serializers.CharField(max_length=4)

    def validate_otp(self, value):
        if not re.fullmatch(r"\d{4}", value):
            raise serializers.ValidationError("OTP must be exactly 4 digits.")
        return value


class ResetPasswordSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_new_password(self, value):
        validate_password(value)
        return value


class SignInSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)
    phone = serializers.CharField(required=False)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError(
                {"detail": "Provide either email or phone."}
            )

        if attrs.get("email"):
            attrs["email"] = attrs["email"].lower().strip()

        if attrs.get("phone"):
            attrs["phone"] = normalize_phone(attrs["phone"])

        return attrs


class UserSerializer(serializers.ModelSerializer):
    profile_photo = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone",
            "profile_photo",
            "full_name",
            "is_staff",
            "created_at",
        ]

    def get_profile_photo(self, obj):
        if not obj.profile_photo:
            return None

        url = obj.profile_photo.url
        # Cloudinary returns a full https:// URL; local storage returns a path.
        if url.startswith("http"):
            return url

        request = self.context.get("request")
        if request is not None:
            return request.build_absolute_uri(url)

        return url
