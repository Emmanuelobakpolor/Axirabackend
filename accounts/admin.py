from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import SignupSession, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "phone", "full_name", "is_staff", "is_active", "created_at")
    search_fields = ("email", "phone", "full_name")
    list_filter = ("is_staff", "is_active", "is_superuser")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("full_name", "phone", "profile_photo")}),
        (_("Axira"), {"fields": ("transaction_pin_hash", "quidax_user_id")}),
        (
            _("Permissions"),
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        (_("Important dates"), {"fields": ("last_login", "created_at", "updated_at")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "phone", "full_name", "password1", "password2"),
            },
        ),
    )

    readonly_fields = ("last_login", "created_at", "updated_at")

    # Use default username field name override
    USERNAME_FIELD = "email"


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

    search_fields = ("email", "phone")

    readonly_fields = (
        "otp_hash",
        "otp_expires_at",
        "otp_attempts",
        "verified_at",
        "completed_at",
        "created_at",
        "updated_at",
    )
