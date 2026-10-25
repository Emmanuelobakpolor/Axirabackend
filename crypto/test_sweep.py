from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from crypto.models import CryptoSweep, CryptoWallet
from crypto.quidax import QuidaxError

DEPOSIT = {'id': 'dep-1', 'currency': 'btc', 'amount': '0.0005', 'network': 'btc', 'txid': 'abc'}


@override_settings(QUIDAX_SECRET_KEY='test-key', QUIDAX_MASTER_UID='master-uid')
class DepositSweepTest(TestCase):
    """Credited deposits are moved from the user's sub-account to the master."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email='s@x.com', phone='+2348000000003', full_name='S',
            password='pw12345678', quidax_user_id='qx-sub')
        self.client = APIClient()

    def _webhook(self, wallet, withdraw=None):
        withdraw = withdraw or mock.Mock(return_value={'id': 'wd-1'})
        with mock.patch('crypto.views.get_wallet', return_value=wallet), \
                mock.patch('crypto.views.create_withdrawal', withdraw), \
                mock.patch('crypto.views._fulfil_waiting_sells'):
            self.client.post(
                '/api/crypto/webhook/quidax/',
                {'event': 'deposit.successful', 'data': {**DEPOSIT, 'user': {'id': 'qx-sub'}}},
                format='json')
        return withdraw

    def test_deposit_swept_to_master_as_internal_transfer(self):
        withdraw = self._webhook({'balance': '0.0005'})
        withdraw.assert_called_once()
        kwargs = withdraw.call_args.kwargs
        self.assertEqual(kwargs['uid'], 'qx-sub')          # from the sub-account
        self.assertEqual(kwargs['address'], 'master-uid')  # to the master's user id
        self.assertEqual(kwargs['amount'], '0.0005')
        self.assertEqual(kwargs['network'], 'btc')
        sweep = CryptoSweep.objects.get()
        self.assertEqual(sweep.status, CryptoSweep.Status.SENT)
        self.assertEqual(sweep.quidax_withdrawal_id, 'wd-1')
        # The customer's ledger balance is the deposit, untouched by the sweep.
        self.assertEqual(
            CryptoWallet.objects.get(user=self.user, coin='BTC').available, Decimal('0.0005'))

    def test_failed_sweep_is_recorded_and_keeps_customer_balance(self):
        withdraw = mock.Mock(side_effect=QuidaxError('Token withdrawal lower than the minimum'))
        self._webhook({'balance': '0.0005'}, withdraw)
        sweep = CryptoSweep.objects.get()
        self.assertEqual(sweep.status, CryptoSweep.Status.FAILED)
        self.assertIn('minimum', sweep.error)
        self.assertEqual(
            CryptoWallet.objects.get(user=self.user, coin='BTC').available, Decimal('0.0005'))

    def test_empty_sub_account_is_not_swept(self):
        withdraw = self._webhook({'balance': '0'})
        withdraw.assert_not_called()
        self.assertFalse(CryptoSweep.objects.exists())

    @override_settings(QUIDAX_MASTER_UID='qx-sub')
    def test_never_sweeps_to_itself(self):
        withdraw = self._webhook({'balance': '0.0005'})
        withdraw.assert_not_called()


class FriendlyErrorTest(TestCase):
    def test_insufficient_balance_is_never_shown_raw(self):
        from crypto.views import _friendly_order_error, _friendly_withdraw_error
        for text in (
            _friendly_withdraw_error('BTC', 'Insufficient account balance'),
            _friendly_order_error('sale', 'BTC', 'Insufficient account balance'),
            _friendly_order_error('purchase', 'BTC', 'insufficient balance'),
        ):
            self.assertNotIn('nsufficient', text)
            self.assertIn('our side', text)
