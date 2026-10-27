from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from crypto import views
from crypto.models import CryptoWallet, CryptoWithdrawal

ADDRESS = 'bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq'


@override_settings(QUIDAX_SECRET_KEY='test-key')
class WithdrawNetworkFeeTest(TestCase):
    """Quidax's on-chain fee is deducted from what the customer sends."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email='f@x.com', phone='+2348000000004', full_name='F',
            password='pw12345678', quidax_user_id='qx-f')
        CryptoWallet.objects.create(user=self.user, coin='BTC', available=Decimal('0.001'))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        patches = [
            mock.patch.object(views, 'CRYPTO_WITHDRAW_NETWORK_FEES', {'BTC': Decimal('0.0001')}),
            mock.patch.object(views, 'CRYPTO_WITHDRAW_MIN_AMOUNTS', {'BTC': Decimal('0.00015')}),
            mock.patch('crypto.views.get_all_tickers', return_value={}),
            mock.patch('crypto.views._resolve_price_ngn', return_value=Decimal('110000000')),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _send(self, amount, withdraw=None):
        withdraw = withdraw or mock.Mock(return_value={'id': 'wd-9', 'fee': '0.0001'})
        with mock.patch('crypto.views.create_withdrawal', withdraw):
            res = self.client.post('/api/crypto/withdraw/', {
                'coin': 'BTC', 'network': 'BTC', 'address': ADDRESS, 'amount': amount,
            }, format='json')
        return res, withdraw

    def _available(self):
        return CryptoWallet.objects.get(user=self.user, coin='BTC').available

    def test_fee_deducted_from_amount_sent(self):
        res, withdraw = self._send('0.0005')
        self.assertEqual(res.status_code, 201, res.json())
        # Quidax is asked to send the amount minus the fee...
        self.assertEqual(Decimal(withdraw.call_args.kwargs['amount']), Decimal('0.0004'))
        # ...and the customer is charged the full amount they entered.
        self.assertEqual(self._available(), Decimal('0.0005'))
        body = res.json()
        self.assertEqual(Decimal(body['network_fee']), Decimal('0.0001'))
        self.assertEqual(Decimal(body['receive_amount']), Decimal('0.0004'))

    def test_minimum_applies_after_the_fee(self):
        # 0.0002 - 0.0001 fee = 0.0001 received, below Quidax's 0.00015 minimum.
        res, withdraw = self._send('0.0002')
        self.assertEqual(res.status_code, 400)
        self.assertIn('0.00025', res.json()['error'])
        withdraw.assert_not_called()
        self.assertEqual(self._available(), Decimal('0.001'))

    def test_amount_not_above_fee_rejected(self):
        res, withdraw = self._send('0.0001')
        self.assertEqual(res.status_code, 400)
        withdraw.assert_not_called()

    def test_quidax_rejection_refunds_full_amount(self):
        res, _ = self._send('0.0005')
        w = CryptoWithdrawal.objects.get()
        views._reject_withdrawal(w.pk, 'rejected', via='test')
        self.assertEqual(self._available(), Decimal('0.001'))

    def test_last_charged_fee_used_when_higher_than_configured(self):
        self._send('0.0005', mock.Mock(return_value={'id': 'wd-1', 'fee': '0.00015'}))
        res, withdraw = self._send('0.0005')
        self.assertEqual(res.status_code, 201, res.json())
        self.assertEqual(Decimal(withdraw.call_args.kwargs['amount']), Decimal('0.00035'))
