import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, email, phone, full_name, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required.")

        email = self.normalize_email(email)

        user = self.model(
            email=email,
            phone=phone,
            full_name=full_name,
            **extra_fields,
        )

        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_superuser(self, email, phone, full_name, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")

        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(
            email=email,
            phone=phone,
            full_name=full_name,
            password=password,
            **extra_fields,
        )


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=150)
    profile_photo = models.ImageField(upload_to="profile_photos/", blank=True, null=True)

    transaction_pin_hash = models.CharField(max_length=128, blank=True)

    # Quidax sub-account UID — set automatically after registration
    quidax_user_id = models.CharField(max_length=100, blank=True, db_index=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["phone", "full_name"]

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
        ]

    def __str__(self):
        return self.email


class PasswordResetSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identifier = models.CharField(max_length=254)  # normalized email or phone
    otp_hash = models.CharField(max_length=128)
    otp_expires_at = models.DateTimeField()
    otp_attempts = models.PositiveSmallIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self):
        return timezone.now() > self.otp_expires_at

    def __str__(self):
        return f"Password reset for {self.identifier}"


class SignupSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField()
    phone = models.CharField(max_length=20)

    otp_hash = models.CharField(max_length=128)
    otp_expires_at = models.DateTimeField()
    otp_attempts = models.PositiveSmallIntegerField(default=0)

    verified_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["verified_at"]),
        ]

    def is_expired(self):
        return timezone.now() > self.otp_expires_at

    def __str__(self):
        return f"{self.phone} signup session"
