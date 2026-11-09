"""Offline regression tests: payment-hold recheck + SDK read hang guard.

Covers the 2026-11 incident fixes without importing the live bot (functions
are extracted from zap_wallet_bot_v32.py via AST and run against stub module
globals):

- the read-only recheck resolves a held message to the SAME success text the
  live path uses (or 'payment_failed' / still-processing) and NEVER sends;
- payment_processing gate semantics: cleared on conclusive results, kept
  while the outcome is unresolved (money safety: no duplicate sends);
- the fire-and-forget task is referenced until it finishes (no GC);
- asyncio.wait_for hang guard raises a clean TimeoutError on a stuck call
  and passes results through untouched;
- get_fiat_rates logs at WARNING when a cycle produces nothing;
- no v32 code path renders the banned 'payment_hold' text anymore, and
  send_payment itself stays free of hard timeouts.
"""
import ast
import asyncio
import datetime as _dt
import logging
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from breez_sdk_spark import PaymentStatus

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'zap_wallet_bot_v32.py'
SRC_TEXT = SOURCE.read_text(encoding='utf-8')
_TREE = ast.parse(SRC_TEXT)

RECHECK_FNS = ('_payment_recheck_task', '_payment_recheck_gate_cleanup',
               '_safe_edit_message', '_spawn_payment_recheck',
               '_payment_success_text', '_payment_success_buttons',
               '_lnurl_success_text', '_ffio_success_text',
               '_ffio_success_buttons', '_donation_success_text',
               'sdk_read', 'is_payment_successful')


def _extract(*names):
    nodes = [n for n in _TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    found = {n.name for n in nodes}
    assert not set(names) - found, f'functions not found at module top: {set(names) - found}'
    return ast.Module(body=nodes, type_ignores=[])


class _FakeButton:
    @staticmethod
    def inline(text, data):
        return ('btn', text, data)


def _make_recheck_namespace():
    calls = {'t': [], 'volume': [], 'edits': [], 'polls': []}
    sends = []

    class FakeClient:
        async def edit_message(self, chat_id, message_id, text, buttons=None):
            calls['edits'].append({'chat': chat_id, 'msg': message_id,
                                   'text': text, 'buttons': buttons})

    async def never_send(*a, **k):
        sends.append((a, k))
        raise AssertionError('the recheck must never send, retry or refund')

    async def get_payment_by_id(user_id, payment_id):
        calls['polls'].append((user_id, payment_id))
        return calls.get('poll_result')

    def track_volume(direction, sats):
        calls['volume'].append((direction, sats))

    def t(user_id, key, **kwargs):
        calls['t'].append((key, kwargs))
        rendered = '|'.join(f'{k}={v}' for k, v in kwargs.items())
        return f'<{key}>' + (f'#{rendered}' if rendered else '')

    lock = asyncio.Lock()
    ns = {
        'asyncio': asyncio,
        'logging': logging,
        'Button': _FakeButton,
        't': t,
        'client': FakeClient(),
        'user_lock': lambda uid: lock,
        'user_data': {},
        'user_steps': {},
        'get_payment_by_id': get_payment_by_id,
        'track_volume': track_volume,
        'send_payment': never_send,
        'lnurl_pay': never_send,
        'execute_withdraw': never_send,
        '_PAYMENT_RECHECK_TASKS': set(),
        'SDK_READ_TIMEOUT_SECS': 45,
        'SDK_DISCONNECT_TIMEOUT_SECS': 15,
        # fast clock so the 120s window runs in milliseconds
        'PAYMENT_RECHECK_INTERVAL_SECS': 0.01,
        'PAYMENT_RECHECK_TOTAL_SECS': 0.05,
        'PaymentStatus': PaymentStatus,
        '_calls': calls,
        '_sends': sends,
    }
    exec(compile(_extract(*RECHECK_FNS), str(SOURCE), 'exec'), ns)
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


class RecheckBehavior(OfflineHygieneMixin, unittest.IsolatedAsyncioTestCase):
    def _held_state(self, ns):
        prep = object()
        ns['user_data'][7] = {'payment_processing': True, 'prepare_response': prep}
        ns['user_steps'][7] = 'awaiting_amount'
        return prep

    async def test_resolves_to_success_text_and_clears_gate(self):
        ns = _make_recheck_namespace()
        prep = self._held_state(ns)
        ns['_calls']['poll_result'] = SimpleNamespace(status=PaymentStatus.COMPLETED, id='pid1')
        await ns['_payment_recheck_task'](
            user_id=7, chat_id=7, message_id=42, payment_id='pid1',
            success_text=ns['_payment_success_text']('500 sats', '1 sat', 'pid1'),
            success_buttons=ns['_payment_success_buttons'](7, 'pid1'),
            amount=500, fee=3, prepare_response=prep, manage_gate=True)
        edits = ns['_calls']['edits']
        self.assertEqual(len(edits), 1)
        self.assertEqual(
            edits[0]['text'],
            '✅ **Payment Successful!**\n\n💰 Amount: 500 sats\n⚡ Fee: 1 sat\n🆔 ID: `pid1`')
        self.assertIsNotNone(edits[0]['buttons'])  # same buttons as the live path
        self.assertEqual(ns['_calls']['volume'], [('out', 503)])  # volume tracked like the live path
        ud = ns['user_data'][7]
        self.assertIs(ud['payment_processing'], False)   # gate reopened: outcome conclusive
        self.assertNotIn('prepare_response', ud)          # this payment's stash dropped
        self.assertNotIn(7, ns['user_steps'])
        self.assertEqual(ns['_sends'], [])                # never sent

    async def test_failed_poll_edits_payment_failed_and_clears_gate(self):
        ns = _make_recheck_namespace()
        prep = self._held_state(ns)
        ns['_calls']['poll_result'] = SimpleNamespace(status=PaymentStatus.FAILED, id='pid1')
        await ns['_payment_recheck_task'](
            user_id=7, chat_id=7, message_id=42, payment_id='pid1',
            success_text='unused', amount=500, fee=0,
            prepare_response=prep, manage_gate=True)
        self.assertEqual(ns['_calls']['edits'][0]['text'], '<payment_failed>')
        self.assertIsNone(ns['_calls']['edits'][0]['buttons'])
        self.assertEqual(ns['_calls']['volume'], [])
        self.assertIs(ns['user_data'][7]['payment_processing'], False)
        self.assertNotIn('prepare_response', ns['user_data'][7])

    async def test_still_unknown_keeps_gate_and_shows_payment_id(self):
        ns = _make_recheck_namespace()
        prep = self._held_state(ns)
        ns['_calls']['poll_result'] = SimpleNamespace(status=PaymentStatus.PENDING, id='pid1')
        await ns['_payment_recheck_task'](
            user_id=7, chat_id=7, message_id=42, payment_id='pid1',
            success_text='unused', amount=500, fee=0,
            prepare_response=prep, manage_gate=True)
        edits = ns['_calls']['edits']
        self.assertEqual(len(edits), 1)  # exactly one terminal edit
        self.assertEqual(edits[0]['text'], '<payment_still_processing>#payment_id=pid1')
        self.assertIsNone(edits[0]['buttons'])
        # money safety: outcome unresolved → duplicate-send gate STAYS set
        ud = ns['user_data'][7]
        self.assertIs(ud['payment_processing'], True)
        self.assertIs(ud['prepare_response'], prep)
        self.assertEqual(ns['_sends'], [])

    async def test_no_payment_id_runs_timer_only(self):
        ns = _make_recheck_namespace()
        self._held_state(ns)
        await ns['_payment_recheck_task'](
            user_id=7, chat_id=7, message_id=42, payment_id=None,
            success_text='unused')
        self.assertEqual(ns['_calls']['polls'], [])  # nothing identifiable → no polling
        self.assertEqual(ns['_calls']['edits'][-1]['text'], '<payment_still_processing>#payment_id=N/A')

    async def test_poll_failure_logs_warning_and_ages_out(self):
        ns = _make_recheck_namespace()
        self._held_state(ns)

        async def boom(uid, pid):
            raise RuntimeError('sdk wedged')

        ns['get_payment_by_id'] = boom
        with self.assertLogs(level='WARNING') as lg:
            await ns['_payment_recheck_task'](
                user_id=7, chat_id=7, message_id=42, payment_id='pid1', success_text='unused')
        self.assertTrue(any('poll error' in line for line in lg.output))
        self.assertEqual(ns['_calls']['edits'][-1]['text'], '<payment_still_processing>#payment_id=pid1')

    async def test_unresolvable_poll_logs_warning(self):
        ns = _make_recheck_namespace()
        self._held_state(ns)
        ns['_calls']['poll_result'] = None  # payment never found
        with self.assertLogs(level='WARNING') as lg:
            await ns['_payment_recheck_task'](
                user_id=7, chat_id=7, message_id=42, payment_id='pid1', success_text='unused')
        self.assertTrue(any('poll unresolved' in line for line in lg.output))

    async def test_deleted_message_never_crashes_and_gate_still_clears(self):
        ns = _make_recheck_namespace()
        prep = self._held_state(ns)
        ns['_calls']['poll_result'] = SimpleNamespace(status=PaymentStatus.COMPLETED, id='pid1')

        class DeadClient:
            async def edit_message(self, *a, **k):
                raise RuntimeError('message deleted')

        ns['client'] = DeadClient()
        with self.assertLogs(level='WARNING'):
            await ns['_payment_recheck_task'](
                user_id=7, chat_id=7, message_id=42, payment_id='pid1',
                success_text='✅', amount=None, fee=0,
                prepare_response=prep, manage_gate=True)  # must simply return
        self.assertIs(ns['user_data'][7]['payment_processing'], False)

    async def test_newer_flow_survives_gate_cleanup(self):
        ns = _make_recheck_namespace()
        old_prep = self._held_state(ns)
        ns['_calls']['poll_result'] = SimpleNamespace(status=PaymentStatus.COMPLETED, id='pid1')
        # while gated, the user built a NEW send flow (different prepare object)
        new_prep = object()
        ns['user_data'][7]['prepare_response'] = new_prep
        ns['user_steps'][7] = 'awaiting_password'
        await ns['_payment_recheck_task'](
            user_id=7, chat_id=7, message_id=42, payment_id='pid1',
            success_text='✅', amount=None, fee=0,
            prepare_response=old_prep, manage_gate=True)
        ud = ns['user_data'][7]
        self.assertIs(ud['prepare_response'], new_prep)  # new flow untouched
        self.assertIs(ud['payment_processing'], False)
        self.assertEqual(ns['user_steps'][7], 'awaiting_password')

    async def test_spawn_keeps_hard_reference_until_done(self):
        ns = _make_recheck_namespace()
        task = ns['_spawn_payment_recheck'](
            user_id=7, chat_id=7, message_id=42, payment_id=None, success_text='x')
        self.assertIn(task, ns['_PAYMENT_RECHECK_TASKS'])  # not GC-able mid-poll
        await task
        self.assertNotIn(task, ns['_PAYMENT_RECHECK_TASKS'])


class SdkReadHangGuard(OfflineHygieneMixin, unittest.IsolatedAsyncioTestCase):
    async def test_timeout_raises_clean_error_on_stuck_call(self):
        ns = _make_recheck_namespace()
        stuck = asyncio.Event()

        async def never_returns():
            await stuck.wait()

        with self.assertLogs(level='ERROR') as lg:
            with self.assertRaises(TimeoutError) as cm:
                await ns['sdk_read'](never_returns(), 'unit probe', timeout_secs=0.05)
        self.assertIn('timed out', str(cm.exception))
        self.assertTrue(any('unit probe' in line for line in lg.output))

    async def test_result_passthrough(self):
        ns = _make_recheck_namespace()

        async def ok():
            return 'value'

        self.assertEqual(await ns['sdk_read'](ok(), 'unit probe', timeout_secs=1), 'value')


class FiatRatesVisibility(OfflineHygieneMixin, unittest.IsolatedAsyncioTestCase):
    def _make_fiat_namespace(self, users, yadio):
        class DB:
            def get(self, key):
                return users if key == 'users' else None

        async def get_yadio_rates():
            return yadio

        async def no_sdk(*a, **k):
            raise AssertionError('SDK must not be reached in this scenario')

        ns = {
            'logging': logging,
            'datetime': _dt.datetime,
            'db': DB(),
            'get_wallet': no_sdk,
            'close_wallet': no_sdk,
            'get_yadio_rates': get_yadio_rates,
            'fiat_rates_cache': {'rates': {}, 'last_update': None, 'update_interval': 300},
            'SDK_READ_TIMEOUT_SECS': 45,
            'SDK_DISCONNECT_TIMEOUT_SECS': 15,
        }
        exec(compile(_extract('get_fiat_rates', 'sdk_read'), str(SOURCE), 'exec'), ns)
        return ns

    async def test_empty_cycle_logs_warning(self):
        ns = self._make_fiat_namespace(users=[], yadio={})
        with self.assertLogs(level='WARNING') as lg:
            rates = await ns['get_fiat_rates']()
        self.assertEqual(rates, {})
        self.assertTrue(any('produced no rates' in line for line in lg.output))

    async def test_fresh_cycle_does_not_warn(self):
        ns = self._make_fiat_namespace(users=[], yadio={'USD': 100000})
        with self.assertNoLogs(level='WARNING'):
            rates = await ns['get_fiat_rates']()
        self.assertEqual(rates['USD'], 100000)
        self.assertEqual(ns['fiat_rates_cache']['rates']['USD'], 100000)


class StaticGuards(unittest.TestCase):
    """Source-level money-safety pins."""

    def _fn(self, name):
        return next(n for n in _TREE.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)

    def test_recheck_functions_never_send_or_refund(self):
        banned = {'send_payment', 'lnurl_pay', 'execute_withdraw',
                  'receive_payment', 'refund'}
        for name in ('_payment_recheck_task', '_payment_recheck_gate_cleanup',
                     '_safe_edit_message'):
            node = self._fn(name)
            called = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    if isinstance(fn, ast.Name):
                        called.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        called.add(fn.attr)
            self.assertEqual(called & banned, set(),
                             f'{name} must not call {sorted(called & banned)}')

    def test_send_payment_has_no_hard_timeout(self):
        # A slow-but-successful send must never be cancelled into a
        # retryable-looking failure; unknown outcomes go to the recheck path.
        src = ast.unparse(self._fn('send_payment'))
        self.assertNotIn('sdk_read', src)
        self.assertNotIn('wait_for', src)

    def test_v32_never_renders_payment_hold(self):
        # Owner order: the banned user-facing text must not appear in any
        # string constant (comments may mention it historically).
        for node in ast.walk(_TREE):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn('payment_hold', node.value)

    def test_success_text_builders_are_byte_identical_to_live_path(self):
        ns = _make_recheck_namespace()
        self.assertEqual(
            ns['_payment_success_text']('1 sats', '2 sats', 'abc'),
            '✅ **Payment Successful!**\n\n💰 Amount: 1 sats\n⚡ Fee: 2 sats\n🆔 ID: `abc`')
        self.assertEqual(
            ns['_lnurl_success_text']('1 sats', '2 sats'),
            '✅ **Payment Successful!**\n\n💰 Amount: 1 sats\n⚡ Fee: 2 sats\nType: LNURL-Pay')
        self.assertEqual(
            ns['_ffio_success_text']('o1', 'TRX'),
            "✅ **Lightning payment sent to ff.io!**\n\n🆔 Order: `o1`\n"
            '📥 You will receive **TRX** at your address.\n\n'
            "⏳ Exchange is processing. You'll be notified when done.")
        self.assertEqual(
            ns['_donation_success_text']('1 sats', '2 sats'),
            '**Donation Successful!**\n\nAmount: 1 sats\nFee: 2 sats\n\n'
            'Thank you for your support!\nYour donation helps keep this bot running!')


class NewI18nKeys(unittest.TestCase):
    def test_keys_present_in_en_and_fa_with_placeholder_parity(self):
        ns = {}
        exec(compile(ast.parse((ROOT / 'zap_i18n.py').read_text(encoding='utf-8')),
                     'zap_i18n.py', 'exec'), ns)
        for pack in ('EN', 'FA'):
            self.assertIn('payment_processing_status', ns[pack])
            self.assertIn('payment_still_processing', ns[pack])
            self.assertIn('{payment_id}', ns[pack]['payment_still_processing'])
        self.assertNotIn('{', ns['EN']['payment_processing_status'])


if __name__ == '__main__':
    unittest.main()
