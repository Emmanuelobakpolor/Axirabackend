from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from crypto.admin import CryptoWithdrawalAdmin
from crypto.models import CryptoWallet, CryptoWithdrawal

AMT = Decimal('0.00002001')


class WithdrawalAdminActionsTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin_user = User.objects.create_user(
            email='a@x.com', phone='+2348000000001', full_name='A', password='pw12345678', is_staff=True)
        self.user = User.objects.create_user(
            email='u@x.com', phone='+2348000000002', full_name='U', password='pw12345678')
        # The real stuck state: reserved, never accepted by Quidax.
        self.wallet = CryptoWallet.objects.create(user=self.user, coin='BTC', available=Decimal('0.00003'), reserved=AMT)
        self.ma = CryptoWithdrawalAdmin(CryptoWithdrawal, AdminSite())

    def _req(self):
        r = RequestFactory().post('/')
        r.user = self.admin_user
        r.session = {}
        r._messages = FallbackStorage(r)
        return r

    def _w(self, **kw):
        return CryptoWithdrawal.objects.create(user=self.user, coin='BTC', address='1Fp2', amount=AMT, **kw)

    def _bal(self):
        self.wallet.refresh_from_db()
        return self.wallet.available, self.wallet.reserved

    def test_reject_never_sent_releases_reserved(self):
        w = self._w()
        self.ma.mark_rejected_and_refund(self._req(), CryptoWithdrawal.objects.filter(pk=w.pk))
        self.assertEqual(self._bal(), (Decimal('0.00005001'), Decimal('0')))
        # running it again changes nothing
        self.ma.mark_rejected_and_refund(self._req(), CryptoWithdrawal.objects.filter(pk=w.pk))
        self.assertEqual(self._bal(), (Decimal('0.00005001'), Decimal('0')))

    def test_complete_never_sent_clears_reserved(self):
        w = self._w()
        self.ma.mark_completed(self._req(), CryptoWithdrawal.objects.filter(pk=w.pk))
        self.assertEqual(self._bal(), (Decimal('0.00003'), Decimal('0')))

    def test_reject_accepted_by_quidax_credits_available(self):
        self.wallet.reserved = 0
        self.wallet.save()
        w = self._w(quidax_withdrawal_id='q1')
        self.ma.mark_rejected_and_refund(self._req(), CryptoWithdrawal.objects.filter(pk=w.pk))
        self.assertEqual(self._bal(), (Decimal('0.00005001'), Decimal('0')))

    def test_reject_failed_does_not_double_refund(self):
        self.wallet.reserved = 0
        self.wallet.save()
        w = self._w(status=CryptoWithdrawal.Status.FAILED)
        self.ma.mark_rejected_and_refund(self._req(), CryptoWithdrawal.objects.filter(pk=w.pk))
        self.assertEqual(self._bal(), (Decimal('0.00003'), Decimal('0')))

    def test_dashboard_endpoint_refund(self):
        from rest_framework.test import APIClient
        w = self._w()
        c = APIClient()
        c.force_authenticate(self.admin_user)
        r = c.post(f'/api/admin/crypto/withdrawals/{w.reference}/', {'action': 'refund'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'rejected')
        self.assertEqual(self._bal(), (Decimal('0.00005001'), Decimal('0')))
        # second click refused, balance unchanged
        r = c.post(f'/api/admin/crypto/withdrawals/{w.reference}/', {'action': 'refund'}, format='json')
        self.assertEqual(r.status_code, 400)
        r = c.post(f'/api/admin/crypto/withdrawals/{w.reference}/', {'action': 'complete'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._bal(), (Decimal('0.00005001'), Decimal('0')))

    def test_dashboard_endpoint_requires_staff(self):
        from rest_framework.test import APIClient
        w = self._w()
        c = APIClient()
        c.force_authenticate(self.user)
        r = c.post(f'/api/admin/crypto/withdrawals/{w.reference}/', {'action': 'refund'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._bal(), (Decimal('0.00003'), AMT))
