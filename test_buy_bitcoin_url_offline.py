"""Offline regression tests: buy_bitcoin_url request shape (SDK 0.27.x).

The SDK upgrade changed BuyBitcoinRequest.MOONPAY to take
``delivery: Optional[MoonpayDelivery], redirect_url: Optional[str]`` —
the old ``locked_amount_sat=...`` kwarg now raises TypeError at call time.

The function is extracted from zap_wallet_bot_v32.py via AST (house pattern:
the live bot module is never imported) and run against a mocked sdk wallet:

- locked_amount_sat=N  -> delivery=MoonpayDelivery.BITCOIN(amount_sat=N)
- locked_amount_sat unset -> delivery=None
- redirect_url stays None and the response URL is returned untouched;
- no locked_amount_sat kwarg reaches the request constructor (the real
  0.27.x class rejects it, so the old call form cannot slip through).
"""
import ast
import logging
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from breez_sdk_spark import BuyBitcoinRequest, MoonpayDelivery

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'zap_wallet_bot_v32.py'


def _extract_buy_bitcoin_url():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body
             if isinstance(n, ast.AsyncFunctionDef) and n.name == 'buy_bitcoin_url']
    assert nodes, 'buy_bitcoin_url not found at module top of zap_wallet_bot_v32.py'
    return ast.Module(body=nodes, type_ignores=[])


def _make_namespace():
    """Stub globals + a mocked sdk wallet recording the request it receives."""
    opened, closed, requests = [], [], []

    class FakeSdk:
        async def buy_bitcoin(self, request=None):
            requests.append(request)
            return SimpleNamespace(url='https://buy.moonpay.test/xyz')

    async def get_wallet(user_id):
        opened.append(user_id)
        return FakeSdk()

    async def close_wallet(sdk):
        closed.append(sdk)

    ns = {
        'logging': logging,
        'get_wallet': get_wallet,
        'close_wallet': close_wallet,
        'BuyBitcoinRequest': BuyBitcoinRequest,
        'MoonpayDelivery': MoonpayDelivery,
        '_opened': opened,
        '_closed': closed,
        '_requests': requests,
    }
    exec(compile(_extract_buy_bitcoin_url(), str(SOURCE), 'exec'), ns)
    return ns


class OfflineHygieneMixin:
    """House pattern: no HOME writes, no network from extracted code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'HOME': self.tmp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for meth in ('connect', 'connect_ex'):
            p = patch.object(socket.socket, meth, side_effect=AssertionError('network blocked'))
            p.start()
            self.addCleanup(p.stop)


class BuyBitcoinUrlShape(OfflineHygieneMixin, unittest.IsolatedAsyncioTestCase):
    async def test_locked_amount_becomes_bitcoin_delivery(self):
        ns = _make_namespace()
        url = await ns['buy_bitcoin_url'](user_id=7, locked_amount_sat=50_000)
        self.assertEqual(url, 'https://buy.moonpay.test/xyz')
        self.assertEqual(len(ns['_requests']), 1)
        req = ns['_requests'][0]
        self.assertIsInstance(req.delivery, MoonpayDelivery.BITCOIN)
        self.assertEqual(req.delivery.amount_sat, 50_000)
        self.assertIsNone(req.redirect_url)

    async def test_no_locked_amount_means_no_delivery(self):
        ns = _make_namespace()
        url = await ns['buy_bitcoin_url'](user_id=7)
        self.assertEqual(url, 'https://buy.moonpay.test/xyz')
        req = ns['_requests'][0]
        self.assertIsNone(req.delivery)
        self.assertIsNone(req.redirect_url)

    async def test_wallet_opened_and_closed_once(self):
        ns = _make_namespace()
        await ns['buy_bitcoin_url'](user_id=7, locked_amount_sat=1_000)
        self.assertEqual(ns['_opened'], [7])
        self.assertEqual(len(ns['_closed']), 1)

    async def test_sdk_rejects_locked_amount_kwarg(self):
        # Guard for the regression: the installed 0.27.x MOONPAY constructor
        # must not silently accept the old locked_amount_sat kwarg, otherwise
        # the shape assertions above could pass with the legacy call form.
        with self.assertRaises(TypeError):
            BuyBitcoinRequest.MOONPAY(locked_amount_sat=50_000, redirect_url=None)


if __name__ == '__main__':
    unittest.main()
