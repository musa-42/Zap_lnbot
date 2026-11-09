"""Offline tests of extracted production code; never import the live bot.

Batch persistence is covered by test_batch_persistence_offline.py (it
exercises the real implementation). This file keeps the flow guards
that still exist: strict payment status, and smoke checks that the
classic (pre-claim-system) zap/tip flow is wired in — tip_handler,
process_zap_full/process_zap_payment background tasks, the ⚡ reaction,
the zap_cancel_ button branch and the direct-send private tip path.
The old flow has no durable claim system, so these are existence /
render checks only (AST extraction, no live DB)."""
import ast
import asyncio
import contextlib
import logging
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'), filename=str(SOURCE))


def extract(name, ns):
    import copy
    node = copy.deepcopy(next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns[name]


def branch(function, expression, ns):
    import copy
    fn = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == function)
    found = [n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == expression]
    assert len(found) == 1, (expression, len(found))
    node = ast.AsyncFunctionDef(name='run', args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='event')], kwonlyargs=[], kw_defaults=[], defaults=[]), body=copy.deepcopy(found[0].body), decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(SOURCE), 'exec'), ns)
    return ns['run']


class Event:
    def __init__(self, data='', sender=1, chat=99):
        self.data = data.encode()
        self.sender_id = sender
        self.chat_id = chat
        self.message_id = 1
        self.id = 1
        self.is_private = False
        self.answers = []
        self.edits = []
    async def answer(self, text='', **kw): self.answers.append(text)
    async def edit(self, text='', **kw): self.edits.append(text); return self
    async def respond(self, text='', **kw): self.edits.append(text); return self
    async def reply(self, text='', **kw): self.edits.append(text); return self
    async def get_sender(self): return SimpleNamespace(first_name='fixture')


# Symbols of the reverted durable-claim tip system — none may come back.
REVERTED_SYMBOLS = ('_tip_open', '_tip_execute', '_tip_callback', '_zap_callback',
                    'zap_react_target', 'verify_send_password_for_tip', 'tip_claims',
                    '_tip_db', '_tip_get', '_tip_confirmation', '_tip_respond')

OLD_ZAP_FUNCS = ('tip_handler', 'add_zap_reaction', 'process_zap_full',
                 'remove_cancel_button_after_delay', 'process_zap_payment',
                 'cleanup_pending_zaps')


class FinancialSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HOME': self.tmp.name, 'DONATE_USER_ID': '55'}, clear=True)
        self.env.start()
        self.net = patch.object(socket.socket, 'connect', side_effect=AssertionError('network blocked'))
        self.net.start()
        self.net_ex = patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network blocked'))
        self.net_ex.start()
        self.addCleanup(self.net_ex.stop)
        self.addCleanup(self.net.stop)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.client = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), send_file=AsyncMock())
        self.ns = {'os': os, 'asyncio': asyncio, 'logging': logging,
                   'client': self.client,
                   'user_data': {}, 'user_steps': {},
                   'mark_user_active': AsyncMock(), 't': lambda uid, key, **kw: key,
                   'send_payment': AsyncMock(return_value=SimpleNamespace(status='PAID')),
                   'prepare_payment': AsyncMock(return_value=('prepared', 0, 0)),
                   'create_invoice': AsyncMock(return_value='fixture'), 'init': AsyncMock(),
                   'SendPaymentOptions': SimpleNamespace(BOLT11_INVOICE=lambda **kw: None),
                   'is_payment_successful': lambda p: (True, None),
                   'format_amount_with_usd': AsyncMock(return_value='100 sats'),
                   'parse_amount_input': AsyncMock(return_value=(100, False, None)),
                   'Button': SimpleNamespace(inline=lambda *args, **kw: args)}

    async def test_pending_and_unknown_status_are_not_success(self):
        from breez_sdk_spark import PaymentStatus
        f = extract('is_payment_successful', {'PaymentStatus': PaymentStatus})
        self.assertEqual(f(SimpleNamespace(status=PaymentStatus.PENDING)), (False, 'pending'))
        self.assertEqual(f(SimpleNamespace(status=None)), (False, 'unknown'))

    async def test_old_zap_flow_is_wired_in(self):
        # The classic zap/tip system must exist as top-level handlers.
        names = {n.name for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in OLD_ZAP_FUNCS:
            self.assertIn(name, names, f'{name} missing from bot source')
        source = SOURCE.read_text(encoding='utf-8')
        # pending_zap_confirmations registry + periodic cleanup task
        self.assertIn('pending_zap_confirmations = {}', source)
        self.assertIn('asyncio.create_task(cleanup_pending_zaps())', source)
        # callback_handler answers the zap_cancel_ button
        cb = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callback_handler')
        cb_src = ast.unparse(cb)
        self.assertIn("'zap_cancel_'", cb_src)
        self.assertIn('pending_zap_confirmations', cb_src)
        # tip_handler stores a pending confirmation and launches process_zap_full
        th_src = ast.unparse(next(
            n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'tip_handler'))
        self.assertIn('pending_zap_confirmations', th_src)
        self.assertIn('process_zap_full', th_src)
        self.assertIn('zap_cancel_', th_src)

    async def test_private_tip_custom_amount_sends_directly(self):
        # Old private tip flow: parsed amount goes straight to a donate send,
        # no claim/confirmation round-trip.
        run = branch('message_handler', "current_step == 'tip_custom_amount'", self.ns)
        self.ns['text'] = '100'
        self.ns['user_id'] = 1
        event = Event(sender=1)
        await run(event)
        self.ns['send_payment'].assert_awaited_once()
        self.ns['prepare_payment'].assert_awaited_once()
        self.assertNotIn(1, self.ns['user_steps'])
        self.assertTrue(any('Tip sent' in str(e) for e in event.edits) or
                        self.client.edit_message.await_count == 1)

    async def test_reverted_tip_system_symbols_are_gone(self):
        source = SOURCE.read_text(encoding='utf-8')
        for sym in REVERTED_SYMBOLS:
            self.assertNotIn(sym, source, f'reverted symbol {sym} still referenced')


if __name__ == '__main__':
    unittest.main()
