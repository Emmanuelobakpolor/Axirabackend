"""
Moves coins sitting in users' Quidax deposit sub-accounts into the master
account, where sells and withdrawals are executed.

Deposits are swept automatically as they're credited; this command catches
anything that sweep missed or failed on (e.g. below Quidax's minimum at the
time), and backfills coins deposited before automatic sweeping existed.
Safe to run repeatedly — e.g. from a Railway cron every 15 minutes.

    python manage.py sweep_quidax_balances            # every user, every coin
    python manage.py sweep_quidax_balances --email a@b.com --coin BTC
    python manage.py sweep_quidax_balances --dry-run  # show balances only
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Sweep coins from users' Quidax sub-accounts into the master account."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Only sweep this user's sub-account.")
        parser.add_argument("--coin", help="Only sweep this coin, e.g. BTC.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List sub-account balances without moving anything.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "QUIDAX_SECRET_KEY", ""):
            self.stderr.write(self.style.ERROR(
                "QUIDAX_SECRET_KEY is not set — cannot sweep."
            ))
            return

        from decimal import Decimal, InvalidOperation

        from crypto.quidax import QuidaxError, get_master_uid, get_wallets
        from crypto.views import SUPPORTED_COINS, sweep_to_master

        master_uid = get_master_uid()
        self.stdout.write(f"Master Quidax uid: {master_uid or '(unresolved)'}")
        if not master_uid:
            self.stderr.write(self.style.ERROR("Could not resolve the master uid — aborting."))
            return

        users = User.objects.exclude(quidax_user_id__isnull=True).exclude(quidax_user_id="")
        if options["email"]:
            users = users.filter(email=options["email"])
        only_coin = (options["coin"] or "").upper()

        sent = failed = 0
        for user in users:
            try:
                wallets = get_wallets(user.quidax_user_id) or []
            except QuidaxError as exc:
                self.stderr.write(f"{user.email}: could not read wallets: {exc}")
                continue

            for w in wallets:
                coin = str(w.get("currency", "")).upper()
                if coin not in SUPPORTED_COINS or (only_coin and coin != only_coin):
                    continue
                try:
                    balance = Decimal(str(w.get("balance") or "0"))
                except InvalidOperation:
                    continue
                if balance <= 0:
                    continue

                if options["dry_run"]:
                    self.stdout.write(f"{user.email}: {balance} {coin} in sub-account")
                    continue

                try:
                    sweep = sweep_to_master(user, coin, via="command")
                except QuidaxError as exc:
                    self.stderr.write(f"{user.email}: {coin} sweep errored: {exc}")
                    failed += 1
                    continue
                if sweep is None:
                    continue
                if sweep.status == sweep.Status.SENT:
                    sent += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"{user.email}: moved {sweep.amount} {coin} ({sweep.reference})"
                    ))
                else:
                    failed += 1
                    self.stderr.write(f"{user.email}: {coin} sweep failed: {sweep.error}")

        if not options["dry_run"]:
            self.stdout.write(f"Done — {sent} sent, {failed} failed.")
