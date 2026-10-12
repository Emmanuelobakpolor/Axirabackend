from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from crypto.models import CryptoOrder, CryptoWallet


class RejectReleasesReserveTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(email='a@x.com', phone='+2348000000001', full_name='Admin', password='pw12345678', is_staff=True)
        self.user = User.objects.create_user(email='u@x.com', phone='+2348000000002', full_name='User', password='pw12345678')
        self.wallet = CryptoWallet.objects.create(
            user=self.user, coin='BTC', available=Decimal('0.00003'), reserved=Decimal('0.00002001'))
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _order(self, **kw):
        base = dict(user=self.user, order_type='sell', coin='BTC', coin_amount=Decimal('0.00002001'),
                    rate_ngn=1, fee_ngn=0, total_ngn=1, status='processing')
        base.update(kw)
        return CryptoOrder.objects.create(**base)

    def _reject(self, o):
        return self.client.post(f'/api/admin/crypto/orders/{o.reference}/', {'action': 'reject'}, format='json')

    def test_reject_releases(self):
        o = self._order()
        self.assertEqual(self._reject(o).status_code, 200)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.reserved, 0)
        self.assertEqual(self.wallet.available, Decimal('0.00005001'))
        # second reject is refused, no double release
        self.assertEqual(self._reject(o).status_code, 400)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available, Decimal('0.00005001'))

    def test_reject_refused_when_sent_to_quidax(self):
        o = self._order(quidax_order_id='999')
        self.assertEqual(self._reject(o).status_code, 400)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.reserved, Decimal('0.00002001'))

    def test_waiting_deposit_does_not_release(self):
        o = self._order(status='waiting_deposit')
        self.assertEqual(self._reject(o).status_code, 200)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.reserved, Decimal('0.00002001'))

    def test_user_detail_breakdown(self):
        o = self._order()
        r = self.client.get(f'/api/admin/users/{self.user.id}/')
        cw = r.json()['crypto_wallets'][0]
        self.assertEqual(cw['holds'][0]['reference'], o.reference)
        self.assertEqual(Decimal(cw['unexplained']), 0)
