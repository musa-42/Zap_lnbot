"""Durable batch per-item persistence + send-password gate — offline regression.

Exercises extracted batch functions and the password message-branch with a
temporary SQLite DB, mocked SDK/network, and no import of the live bot module.
"""
import ast
import contextlib
import copy
import logging
import os
from pathlib import Path
import secrets
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())


def extract(name, ns, keep_decorators=False):
    node = copy.deepcopy(next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    if not keep_decorators:
        node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns[name]


def message_branch(test, ns):
    fn = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
    node = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == test)
    f = ast.AsyncFunctionDef(name='branch', args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='event')], kwonlyargs=[], kw_defaults=[], defaults=[]), body=copy.deepcopy(node.body), decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[f], type_ignores=[])), str(SOURCE), 'exec'), ns)
    return ns['branch']


class Event:
    """Callback-query-ish event: edit returns a progress object with edit()."""

    def __init__(self, user=1):
        self.sender_id = self.chat_id = user
        self.answers = []
        self.edits = []
        self.responds = []

    async def answer(self, text='', **kwargs):
        self.answers.append(text)

    async def edit(self, text='', **kwargs):
        self.edits.append((text, kwargs))
        return self

    async def respond(self, text='', **kwargs):
        self.responds.append((text, kwargs))


BATCH_FUNCS = [
    '_batch_db', '_batch_fingerprint', '_batch_add', '_batch_rows', '_batch_claim',
    '_batch_finish', '_batch_clear', '_batch_abandon', '_batch_recover_orphans',
    '_batch_send_item', '_batch_send_all',
]


class TestBatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'HOME': self.tmp.name, 'DONATE_USER_ID': '55'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        for name in ('connect', 'connect_ex'):
            p = patch.object(socket.socket, name, side_effect=AssertionError('network blocked'))
            p.start(); self.addCleanup(p.stop)
        self.hash = None
        self.send_payment = AsyncMock(return_value=SimpleNamespace(id='pay-1', status='COMPLETED'))
        self.lnurl_pay = AsyncMock(return_value=SimpleNamespace(id='pay-ln', status='COMPLETED'))
        self.ns = {
            'contextlib': contextlib, 'os': os, 'logging': logging, 'secrets': secrets,
            'hashlib': __import__('hashlib'),
            'DB_PATH': str(Path(self.tmp.name) / 'db.sqlite'),
            't': lambda uid, key, **kw: key,
            'Button': SimpleNamespace(inline=lambda label, data: (label, data)),
            'user_data': {}, 'user_steps': {},
            'send_payment': self.send_payment,
            'lnurl_pay': self.lnurl_pay,
            'is_payment_successful': lambda p: (
                p.status == 'COMPLETED', None if p.status == 'COMPLETED'
                else ('failed' if p.status == 'FAILED' else 'pending')),
            'has_send_password': lambda uid: self.hash is not None,
            'get_user_send_password_hash': lambda uid: self.hash,
            'verify_send_password': lambda uid, password: password == 'correct',
            'delete_password_message': AsyncMock(),
            'user_id': 1,
            '_admin_alert': AsyncMock(),
        }
        for name in BATCH_FUNCS:
            self.ns[name] = extract(name, self.ns, keep_decorators=name.endswith('_db'))
        self.password = message_branch("current_step == 'verify_send_password_for_batch'", self.ns)

    # ---- helpers -------------------------------------------------------
    def add_item(self, user=1, batch='b1', dest='lnbc1invoice', amount=1000, kind='invoice'):
        fp = self.ns['_batch_fingerprint'](kind, amount, dest)
        row_id = self.ns['_batch_add'](batch, user, dest, amount, fp)
        entry = {'dest': dest, 'amount': amount, 'kind': kind, 'fee': 0,
                 'prepare_response': SimpleNamespace(prepared=True),
                 'options': 'opts', 'row_id': row_id, 'fingerprint': fp}
        return row_id, entry

    def arm(self, user=1, batch='b1', entries=None):
        self.ns['user_data'][user] = {'batch_id': batch, 'batch_items': entries or []}

    def rows(self, batch='b1', user=1):
        return {r[0]: r for r in self.ns['_batch_rows'](batch, user)}

    async def test_password_enforced_before_any_send(self):
        self.hash = 'password-hash'
        row_id, entry = self.add_item()
        self.arm(entries=[entry])
        e = Event()
        await self.ns['_batch_send_all'](e, 1)
        self.assertEqual(self.send_payment.await_count, 0)          # blocked
        self.assertEqual(self.ns['user_steps'][1], 'verify_send_password_for_batch')
        self.assertTrue(any('batch_password_required' in str(t) for t, _ in e.responds))
        # wrong password -> retry, still blocked
        pe = Event()
        self.ns['text'] = 'wrong'
        await self.password(pe)
        self.assertTrue(any('batch_password_retry' in str(t) for t, _ in pe.responds))
        self.assertEqual(self.send_payment.await_count, 0)
        # correct password binds the hash, then sending proceeds exactly once
        self.ns['text'] = 'correct'
        await self.password(pe)
        self.assertEqual(self.ns['user_data'][1]['batch_auth'], ['b1', 'password-hash'])
        self.assertTrue(any('batch_password_verified' in str(t) for t, _ in pe.responds))
        e2 = Event()
        await self.ns['_batch_send_all'](e2, 1)
        self.assertEqual(self.send_payment.await_count, 1)
        self.assertEqual(self.rows()[row_id][4], 'sent')
        await self.ns['_batch_send_all'](Event(), 1)                # replay: no resend
        self.assertEqual(self.send_payment.await_count, 1)

    async def test_failed_item_retry_allowed_then_sent(self):
        r1, e1 = self.add_item(dest='lnbc-a')
        r2, e2 = self.add_item(dest='lnbc-b', amount=2000)
        self.arm(entries=[e1, e2])
        self.send_payment.side_effect = [
            SimpleNamespace(id='f1', status='FAILED'),
            SimpleNamespace(id='ok2', status='COMPLETED'),
        ]
        await self.ns['_batch_send_all'](Event(), 1)
        rows = self.rows()
        self.assertEqual(rows[r1][4], 'failed')      # conclusive failure: retry allowed
        self.assertEqual(rows[r2][4], 'sent')
        self.send_payment.side_effect = None
        await self.ns['_batch_send_all'](Event(), 1)  # retry only the failed one
        self.assertEqual(self.send_payment.await_count, 3)
        self.assertEqual(self.rows()[r1][4], 'sent')
        await self.ns['_batch_send_all'](Event(), 1)  # all sent: no more sends
        self.assertEqual(self.send_payment.await_count, 3)

    async def test_unknown_item_never_retried(self):
        row_id, entry = self.add_item()
        self.arm(entries=[entry])
        self.send_payment.return_value = SimpleNamespace(id='p1', status='PENDING')
        await self.ns['_batch_send_all'](Event(), 1)
        rows = self.rows()
        self.assertEqual(rows[row_id][4], 'unknown')          # ambiguous -> held
        self.assertTrue(rows[row_id][5].startswith('claim-'))  # claim token retained
        await self.ns['_batch_send_all'](Event(), 1)          # retry forbidden
        self.assertEqual(self.send_payment.await_count, 1)
        self.assertEqual(self.rows()[row_id][4], 'unknown')

    async def test_crash_after_claim_never_resends(self):
        row_id, entry = self.add_item()
        self.arm(entries=[entry])
        # simulate crash residue: claim token persisted, send never confirmed
        with self.ns['_batch_db']() as con:
            con.execute('UPDATE batch_items SET payment_id=? WHERE id=?', ('claim-crash', row_id))
        await self.ns['_batch_send_all'](Event(), 1)
        self.assertEqual(self.send_payment.await_count, 0)     # never re-sent blind
        self.assertEqual(self.rows()[row_id][4], 'unknown')
        self.assertTrue(self.ns['_admin_alert'].await_count >= 1)

    async def test_route_fingerprint_mismatch_held(self):
        row_id, entry = self.add_item()
        entry['fingerprint'] = 'tampered'                      # session/route mismatch
        self.arm(entries=[entry])
        await self.ns['_batch_send_all'](Event(), 1)
        self.assertEqual(self.send_payment.await_count, 0)
        self.assertEqual(self.rows()[row_id][4], 'unknown')
        # a lost in-memory route (restart) is held too
        r2, e2 = self.add_item(dest='lnbc-c')
        self.arm(entries=[entry])   # memory no longer knows r2's confirmed route
        await self.ns['_batch_send_all'](Event(), 1)
        self.assertEqual(self.send_payment.await_count, 0)
        self.assertEqual(self.rows()[r2][4], 'unknown')

    async def test_lnurl_route_uses_addtime_prepare(self):
        row_id, entry = self.add_item(dest='user@domain.com', kind='lightning-address')
        self.arm(entries=[entry])
        await self.ns['_batch_send_all'](Event(), 1)
        self.assertEqual(self.send_payment.await_count, 0)     # route NOT re-resolved
        self.assertEqual(self.lnurl_pay.await_count, 1)
        self.assertEqual(self.rows()[row_id][4], 'sent')

    async def test_queue_clear_and_abandon(self):
        row_id, entry = self.add_item()
        self.assertEqual(len(self.rows()), 1)
        self.ns['_batch_clear'](1)                              # queue_clear clears batch_items
        self.assertEqual(self.rows(), {})
        r2, _ = self.add_item(batch='b2', dest='lnbc-d')
        self.ns['_batch_abandon'](1)                            # new batch: orphans held
        self.assertEqual(self.rows('b2')[r2][4], 'unknown')
        r3, _ = self.add_item(batch='b3', dest='lnbc-e')
        self.assertEqual(self.ns['_batch_recover_orphans'](), 1)  # boot: held, never re-sent
        self.assertEqual(self.rows('b3')[r3][4], 'unknown')


if __name__ == '__main__':
    unittest.main()
