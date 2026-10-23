from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from crypto.models import CryptoDeposit, CryptoDepositAddress, CryptoWallet

DEPOSITS = [
    {'id': 'dep-1', 'currency': 'btc', 'amount': '0.0005', 'status': 'accepted', 'txid': 'abc'},
    {'id': 'dep-2', 'currency': 'btc', 'amount': '0.0002', 'status': 'submitted', 'txid': 'def'},
    {'id': 'dep-3', 'currency': 'btc', 'amount': '0.0009', 'status': 'rejected'},
]


class DepositSyncTest(TestCase):
    """Deposits whose webhook never arrived get credited by polling Quidax."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email='u@x.com', phone='+2348000000002', full_name='U',
            password='pw12345678', quidax_user_id='qx-1')
        CryptoDepositAddress.objects.create(user=self.user, coin='BTC', network='', address='bc1q')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _get(self):
        with mock.patch('crypto.views.list_deposits', return_value=DEPOSITS) as m:
            res = self.client.get('/api/crypto/deposits/')
        return res, m

    def test_confirmed_deposit_credited_and_pending_listed(self):
        res, m = self._get()
        self.assertEqual(res.status_code, 200)
        m.assert_called_once_with('qx-1', 'BTC')
        self.assertEqual(
            CryptoWallet.objects.get(user=self.user, coin='BTC').available, Decimal('0.0005'))
        rows = {d['reference']: d['status'] for d in res.json()['deposits']}
        self.assertEqual(rows, {'dep-1': 'completed', 'dep-2': 'pending'})

    def test_repeat_sync_and_webhook_do_not_double_credit(self):
        self._get()
        cache.clear()
        self._get()
        self.client.post(
            '/api/crypto/webhook/quidax/',
            {'event': 'deposit.successful', 'data': {**DEPOSITS[0], 'user': {'id': 'qx-1'}}},
            format='json')
        self.assertEqual(CryptoDeposit.objects.count(), 1)
        self.assertEqual(
            CryptoWallet.objects.get(user=self.user, coin='BTC').available, Decimal('0.0005'))

    def test_sync_is_throttled(self):
        self._get()
        _, m = self._get()
        m.assert_not_called()
