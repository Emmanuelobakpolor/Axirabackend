"""
Repairs crypto orders stuck showing "Processing":

  - WAITING_DEPOSIT sells whose deposit was credited before the deposit
    webhook knew how to settle them (executed at the live rate, or expired
    once older than --max-age-hours).
  - PROCESSING orders whose Quidax order.done webhook never arrived.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Settle waiting-deposit sells and re-sync PROCESSING orders with Quidax."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            help="Only reconcile this specific user's orders.",
        )
        parser.add_argument(
            "--max-age-hours",
            type=int,
            default=24,
            help="Waiting-deposit sells older than this are expired instead of executed.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "QUIDAX_SECRET_KEY", ""):
            self.stderr.write(self.style.ERROR(
                "QUIDAX_SECRET_KEY is not set — cannot reconcile against Quidax."
            ))
            return

        from crypto.models import CryptoOrder
        from crypto.quidax import QuidaxError
        from crypto.views import _fulfil_waiting_sells, _sync_quidax_order

        orders = CryptoOrder.objects.select_related("user")
        if options.get("email"):
            orders = orders.filter(user__email=options["email"])

        max_age = timedelta(hours=options["max_age_hours"])
        waiting = (
            orders.filter(
                order_type=CryptoOrder.OrderType.SELL,
                status=CryptoOrder.Status.WAITING_DEPOSIT,
            )
            .values_list("user_id", "coin")
            .distinct()
        )
        for user_id, coin in waiting:
            user = User.objects.get(pk=user_id)
            try:
                _fulfil_waiting_sells(user, coin, max_age=max_age)
                self.stdout.write(f"{user.email}: settled waiting {coin} sells")
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{user.email} {coin}: {exc}"))

        processing = orders.filter(status=CryptoOrder.Status.PROCESSING).exclude(quidax_order_id="")
        for order in processing:
            try:
                _sync_quidax_order(order)
                order.refresh_from_db()
                self.stdout.write(f"{order.reference}: {order.status}")
            except QuidaxError as exc:
                self.stderr.write(self.style.ERROR(f"{order.reference}: {exc}"))

        self.stdout.write(self.style.SUCCESS("Done."))
