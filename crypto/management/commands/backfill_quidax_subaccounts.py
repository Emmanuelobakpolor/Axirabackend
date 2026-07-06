"""
Backfills Quidax sub-accounts for users whose signup either happened before
QUIDAX_SECRET_KEY was configured, or whose sub-account creation failed at
signup time (see crypto/signals.py).
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Create missing Quidax sub-accounts for users with no quidax_user_id."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            help="Only backfill this specific user's email instead of all affected users.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "QUIDAX_SECRET_KEY", ""):
            self.stderr.write(self.style.ERROR(
                "QUIDAX_SECRET_KEY is not set — cannot create sub-accounts."
            ))
            return

        from crypto.quidax import QuidaxError, create_sub_account

        qs = User.objects.filter(quidax_user_id="")
        if options.get("email"):
            qs = qs.filter(email=options["email"])

        if not qs.exists():
            self.stdout.write("No users need a Quidax sub-account.")
            return

        for user in qs:
            name_parts = user.full_name.strip().split(None, 1)
            first_name = name_parts[0] if name_parts else user.email
            last_name = name_parts[1] if len(name_parts) > 1 else first_name

            try:
                result = create_sub_account(
                    email=user.email,
                    first_name=first_name,
                    last_name=last_name,
                )
                quidax_uid = str(result.get("id") or result.get("uid") or "")
                if not quidax_uid:
                    self.stderr.write(self.style.ERROR(
                        f"{user.email}: Quidax response missing id: {result}"
                    ))
                    continue

                User.objects.filter(pk=user.pk).update(quidax_user_id=quidax_uid)
                self.stdout.write(self.style.SUCCESS(
                    f"{user.email}: linked to Quidax sub-account {quidax_uid}"
                ))
            except QuidaxError as exc:
                self.stderr.write(self.style.ERROR(f"{user.email}: {exc}"))
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{user.email}: unexpected error: {exc}"))
