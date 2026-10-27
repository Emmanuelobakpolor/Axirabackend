from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient


class AdminSweepEndpointTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            email='a@x.com', phone='+2348000000009', full_name='A',
            password='pw12345678', is_staff=True)
        self.client = APIClient()

    def _post(self, body):
        with mock.patch('admin_api.views.call_command', create=True), \
             mock.patch('django.core.management.call_command') as cc:
            res = self.client.post('/api/admin/crypto/sweep/', body, format='json')
        return res, cc

    def test_requires_admin(self):
        user = get_user_model().objects.create_user(
            email='u@x.com', phone='+2348000000008', full_name='U', password='pw12345678')
        self.client.force_authenticate(user)
        res, cc = self._post({})
        self.assertEqual(res.status_code, 403)
        cc.assert_not_called()

    def test_defaults_to_dry_run(self):
        self.client.force_authenticate(self.admin)
        res, cc = self._post({})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['dry_run'])
        self.assertTrue(cc.call_args.kwargs['dry_run'])

    def test_real_run_with_filters(self):
        self.client.force_authenticate(self.admin)
        res, cc = self._post({'dry_run': False, 'email': 'e@x.com', 'coin': 'btc'})
        kw = cc.call_args.kwargs
        self.assertFalse(kw['dry_run'])
        self.assertEqual((kw['email'], kw['coin']), ('e@x.com', 'BTC'))
