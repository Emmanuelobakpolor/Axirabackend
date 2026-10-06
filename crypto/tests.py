import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .models import CryptoDeposit, CryptoWallet

User = get_user_model()


@override_settings(QUIDAX_WEBHOOK_SECRET='', QUIDAX_SECRET_KEY='')
class DepositWebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='depositor@example.com',
            phone='+2348000000000',
            full_name='Test Depositor',
            password='x',
        )
        User.objects.filter(pk=self.user.pk).update(quidax_user_id='qx-sub-1')

    def _send(self, data):
        return self.client.post(
            '/api/crypto/webhook/quidax/',
            data=json.dumps({'event': 'deposit.successful', 'data': data}),
            content_type='application/json',
        )

    def _deposit(self, **overrides):
        data = {
            'id': 'dep-123',
            'user': {'id': 'qx-sub-1'},
            'amount': '0.00002002',
            'currency': 'btc',
            'txid': 'abc',
        }
        data.update(overrides)
        return data

    def _available(self):
        wallet = CryptoWallet.objects.filter(user=self.user, coin='BTC').first()
        return wallet.available if wallet else Decimal('0')

    def test_redelivered_webhook_credits_once(self):
        self.assertEqual(self._send(self._deposit()).status_code, 200)
        self.assertEqual(self._send(self._deposit()).status_code, 200)

        self.assertEqual(self._available(), Decimal('0.00002002'))
        self.assertEqual(CryptoDeposit.objects.count(), 1)

    def test_distinct_deposits_both_credit(self):
        self._send(self._deposit())
        self._send(self._deposit(id='dep-456'))

        self.assertEqual(self._available(), Decimal('0.00004004'))

    def test_deposit_without_id_is_not_credited(self):
        self.assertEqual(self._send(self._deposit(id='')).status_code, 200)

        self.assertEqual(self._available(), Decimal('0'))
        self.assertFalse(CryptoDeposit.objects.exists())
