from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import get_random_string
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import PasswordResetSession, SignupSession
from .serializers import (
    CompleteSignupSerializer,
    ForgotPasswordSerializer,
    ProfilePhotoUploadSerializer,
    ResendOtpSerializer,
    ResetPasswordSerializer,
    SignInSerializer,
    StartSignupSerializer,
    UpdateProfileSerializer,
    UserSerializer,
    VerifyOtpSerializer,
    VerifyResetOtpSerializer,
    normalize_phone,
)

User = get_user_model()


def tokens_for_user(user):
    refresh = RefreshToken.for_user(user)

    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


class AuthViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    def get_permissions(self):
        if self.action in {"me", "update_profile", "update_profile_photo"}:
            return [IsAuthenticated()]

        return [AllowAny()]

    @action(detail=False, methods=["post"], url_path="start-signup")
    def start_signup(self, request):
        serializer = StartSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session = serializer.save()

        print(f"AXIRA OTP for {session.phone}: {session.raw_otp}")

        return Response(
            {
                "message": "OTP sent successfully.",
                "session_id": str(session.id),
                "phone": session.phone,
                "expires_in": 300,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="verify-otp")
    def verify_otp(self, request):
        serializer = VerifyOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session = get_object_or_404(
            SignupSession,
            id=serializer.validated_data["session_id"],
        )

        if session.verified_at:
            return Response(
                {
                    "message": "OTP already verified.",
                    "verified": True,
                    "next": "complete_signup",
                }
            )

        if session.is_expired():
            raise serializers.ValidationError(
                {"detail": "OTP expired. Please request a new code."}
            )

        if session.otp_attempts >= 5:
            raise serializers.ValidationError(
                {"detail": "Too many incorrect OTP attempts."}
            )

        otp = serializer.validated_data["otp"]

        if check_password(otp, session.otp_hash):
            session.verified_at = timezone.now()
            session.save()

            return Response(
                {
                    "message": "OTP verified successfully.",
                    "verified": True,
                    "next": "complete_signup",
                }
            )

        session.otp_attempts += 1
        session.save(update_fields=["otp_attempts", "updated_at"])

        raise serializers.ValidationError({"detail": "Invalid OTP."})

    @action(detail=False, methods=["post"], url_path="resend-otp")
    def resend_otp(self, request):
        serializer = ResendOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session = get_object_or_404(
            SignupSession,
            id=serializer.validated_data["session_id"],
        )

        if session.completed_at:
            raise serializers.ValidationError(
                {"detail": "This signup session has already been completed."}
            )

        otp = get_random_string(4, allowed_chars="0123456789")

        session.otp_hash = make_password(otp)
        session.otp_expires_at = timezone.now() + timedelta(minutes=5)
        session.otp_attempts = 0
        session.save()

        session.raw_otp = otp

        print(f"AXIRA new OTP for {session.phone}: {session.raw_otp}")

        return Response(
            {
                "message": "OTP resent successfully.",
                "session_id": str(session.id),
                "expires_in": 300,
            }
        )

    @action(detail=False, methods=["post"], url_path="complete-signup")
    def complete_signup(self, request):
        serializer = CompleteSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session = get_object_or_404(
            SignupSession,
            id=serializer.validated_data["session_id"],
        )

        if not session.verified_at:
            raise serializers.ValidationError(
                {"session_id": "Verify OTP before completing signup."}
            )

        if session.completed_at:
            raise serializers.ValidationError(
                {"session_id": "This signup session has already been completed."}
            )

        if session.created_at < timezone.now() - timedelta(minutes=15):
            raise serializers.ValidationError(
                {"session_id": "Signup session expired. Please start again."}
            )

        try:
            user = User.objects.create_user(
                email=session.email,
                phone=session.phone,
                full_name=serializer.validated_data["full_name"],
                password=serializer.validated_data["password"],
                transaction_pin_hash=make_password(
                    serializer.validated_data["transaction_pin"]
                ),
            )
        except IntegrityError:
            raise serializers.ValidationError(
                {"detail": "Email or phone number is already in use."}
            )

        session.completed_at = timezone.now()
        session.save()

        return Response(
            {
                "message": "Account created successfully.",
                "user": UserSerializer(user, context={"request": request}).data,
                **tokens_for_user(user),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="sign-in")
    def sign_in(self, request):
        serializer = SignInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data.get("email")
        phone = serializer.validated_data.get("phone")
        password = serializer.validated_data["password"]

        if email:
            user = User.objects.filter(email__iexact=email).first()
        else:
            user = User.objects.filter(phone=normalize_phone(phone)).first()

        if user is None or not user.check_password(password):
            raise AuthenticationFailed("Invalid email/phone or password.")

        if not user.is_active:
            raise AuthenticationFailed("This account is inactive.")

        return Response(
            {
                "message": "Signed in successfully.",
                "user": UserSerializer(user, context={"request": request}).data,
                **tokens_for_user(user),
            }
        )

    @action(detail=False, methods=["get"])
    def me(self, request):
        return Response(
            {
                "user": UserSerializer(request.user, context={"request": request}).data,
            }
        )

    @action(detail=False, methods=["patch"], url_path="update-profile")
    def update_profile(self, request):
        serializer = UpdateProfileSerializer(
            data=request.data,
            instance=request.user,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        return Response(
            {
                "message": "Profile updated successfully.",
                "user": UserSerializer(user, context={"request": request}).data,
            }
        )

    @action(detail=False, methods=["post"], url_path="forgot-password")
    def forgot_password(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        otp = get_random_string(4, allowed_chars="0123456789")

        session = PasswordResetSession.objects.create(
            identifier=identifier,
            otp_hash=make_password(otp),
            otp_expires_at=timezone.now() + timedelta(minutes=5),
        )

        print(f"AXIRA Password Reset OTP for {identifier}: {otp}")

        return Response(
            {
                "message": "OTP sent to your registered contact.",
                "session_id": str(session.id),
                "expires_in": 300,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="verify-reset-otp")
    def verify_reset_otp(self, request):
        serializer = VerifyResetOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session = get_object_or_404(
            PasswordResetSession,
            id=serializer.validated_data["session_id"],
        )

        if session.used_at:
            raise serializers.ValidationError(
                {"detail": "This reset session has already been used."}
            )

        if session.is_expired():
            raise serializers.ValidationError(
                {"detail": "OTP expired. Please request a new one."}
            )

        if session.otp_attempts >= 5:
            raise serializers.ValidationError(
                {"detail": "Too many incorrect attempts. Please request a new code."}
            )

        otp = serializer.validated_data["otp"]

        if check_password(otp, session.otp_hash):
            session.verified_at = timezone.now()
            session.save(update_fields=["verified_at"])
            return Response({"message": "OTP verified.", "verified": True})

        session.otp_attempts += 1
        session.save(update_fields=["otp_attempts"])
        raise serializers.ValidationError({"detail": "Invalid OTP."})

    @action(detail=False, methods=["post"], url_path="reset-password")
    def reset_password(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        session = get_object_or_404(
            PasswordResetSession,
            id=serializer.validated_data["session_id"],
        )

        if not session.verified_at:
            raise serializers.ValidationError({"detail": "OTP not verified."})

        if session.used_at:
            raise serializers.ValidationError(
                {"detail": "This reset session has already been used."}
            )

        if session.is_expired():
            raise serializers.ValidationError(
                {"detail": "Session expired. Please start again."}
            )

        identifier = session.identifier
        if "@" in identifier:
            user = User.objects.filter(email__iexact=identifier).first()
        else:
            user = User.objects.filter(phone=identifier).first()

        if not user:
            raise serializers.ValidationError({"detail": "Account not found."})

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])

        session.used_at = timezone.now()
        session.save(update_fields=["used_at"])

        return Response({"message": "Password reset successfully."})

    @action(detail=False, methods=["post"], url_path="update-profile-photo")
    def update_profile_photo(self, request):
        serializer = ProfilePhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        request.user.profile_photo = serializer.validated_data["profile_photo"]
        request.user.save(update_fields=["profile_photo", "updated_at"])

        return Response(
            {
                "message": "Profile photo updated successfully.",
                "user": UserSerializer(request.user, context={"request": request}).data,
            }
        )
