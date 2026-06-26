from django.contrib import admin

from .models import SignupSession, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "phone",
        "full_name",
        "is_staff",
        "is_active",
        "created_at",
    )

    search_fields = (
        "email",
        "phone",
        "full_name",
    )

    list_filter = (
        "is_staff",
        "is_active",
        "is_superuser",
    )


@admin.register(SignupSession)
class SignupSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "email",
        "phone",
        "otp_attempts",
        "verified_at",
        "completed_at",
        "created_at",
    )

    search_fields = (
        "email",
        "phone",
    )

    readonly_fields = (
        "otp_hash",
        "otp_expires_at",
        "otp_attempts",
        "verified_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
