"""Item 15: payment_processing coordination under the per-user lock — offline.

The check-and-set of the payment flag sat far apart across awaits (notably
ffio_pay_'s `await event.answer()` between check and set), so two concurrent
callbacks could both pass the check and double-enter the pay path. Both
pay-path entry branches (confirm_payment_ and ffio_pay_) now run their whole
body under user_lock(uid); send_quick_ / onchain_speed_ are prepare-only
screens whose pay entry is confirm_payment_ itself.

Offline race simulation: branches are AST-extracted like the token tests;
a blocking send_payment pins the first callback mid-payment (inside the
lock) while a second callback for the same user is dispatched.
"""
import ast
import asyncio
import copy
import logging
import threading
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())


def _defs(*names):
    nodes = [copy.deepcopy(n) for n in TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names), [n.name for n in nodes]
    for node in nodes:
        node.decorator_list = []
    return nodes


def extract_branch_fn(needle):
    """Wrap one callback_handler elif branch in `async def run(event, user_id, data)`."""
    cb = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callback_handler')
    branch = next(n for n in ast.walk(cb)
                  if isinstance(n, ast.If) and needle in ast.unparse(n.test))
    branch = copy.deepcopy(branch)
    fn = ast.AsyncFunctionDef(
        name='run',
        args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=n) for n in ('event', 'user_id', 'data')],
                           kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[branch], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    def _make(env):
        exec(compile(module, '<branch>', 'exec'), env)
        return env['run']
    return _make, branch


def base_env():
    locks = {}
    def user_lock(uid):
        return locks.setdefault(uid, asyncio.Lock())
    env = {
        'user_lock': user_lock,
        'user_data': {}, 'user_steps': {},
        'has_send_password': lambda uid: False,
        'get_password_hint': lambda uid: '',
        't': lambda uid, key, **kw: key,
        'Button': NS(inline=lambda *a, **kw: a),
        'SendPaymentOptions': NS(BOLT11_INVOICE=lambda **kw: None),
        'PAYMENT_COMPLETION_TIMEOUT_SECS': 10,
        'is_payment_successful': lambda p: (True, None),
        'format_amount_with_usd': AsyncMock(return_value='100 sats'),
        'track_volume': lambda *a, **k: None,
        'show_tip_prompt': AsyncMock(),
        'pending_sender_info': {}, 'pending_sender_info_lock': threading.Lock(),
        'datetime': __import__('datetime').datetime,
        'logging': logging, 'client': NS(get_entity=AsyncMock()),
    }
    # hold-rework seams (2026-11): the branches render success via the real
    # shared text builders; the recheck spawn is stubbed (its behavior has
    # its own test file — test_payment_hold_recheck_offline).
    names = ('_payment_success_text', '_payment_success_buttons',
             '_lnurl_success_text', '_ffio_success_text',
             '_ffio_success_buttons', '_donation_success_text')
    nodes = [copy.deepcopy(n) for n in TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names), [n.name for n in nodes]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 '<helpers>', 'exec'), env)
    async def _noop_recheck(**kwargs):
        return None
    env['_spawn_payment_recheck'] = lambda **kwargs: asyncio.ensure_future(_noop_recheck())
    return env


async def drive_until(coro_pred, tries=200):
    for _ in range(tries):
        if coro_pred():
            return True
        await asyncio.sleep(0)
    return coro_pred()


class ConfirmPaymentRace(unittest.IsolatedAsyncioTestCase):
    async def test_double_callback_cannot_double_enter_pay_path(self):
        make, _ = extract_branch_fn("data.startswith('confirm_payment_')")
        env = base_env()
        env['user_data'][1] = {'prepare_response': 'pr', 'payment_type': 'bolt11',
                               'amount': 100, 'fee': 0, 'options': None}
        release = asyncio.Event()

        async def slow_send(*a, **k):
            await release.wait()
            return NS(id='pay-1', amount=100)

        env['send_payment'] = AsyncMock(side_effect=slow_send)
        run = make(env)
        ev1, ev2 = NS(answer=AsyncMock(), edit=AsyncMock()), NS(answer=AsyncMock(), edit=AsyncMock())

        t1 = asyncio.create_task(run(ev1, 1, 'confirm_payment_yes'))
        self.assertTrue(await drive_until(lambda: env['send_payment'].await_count == 1))
        # first callback is pinned mid-payment holding the user lock; a second
        # concurrent callback for the same user must not double-enter
        t2 = asyncio.create_task(run(ev2, 1, 'confirm_payment_yes'))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(env['send_payment'].await_count, 1)
        self.assertFalse(t2.done())

        release.set()
        await asyncio.gather(t1, t2)
        self.assertEqual(env['send_payment'].await_count, 1)
        # first completed the payment; second found the session gone
        self.assertIn('Payment Successful', ''.join(str(c.args[0]) for c in ev1.edit.await_args_list))
        ev2.answer.assert_any_call('❌ Payment data expired')
        self.assertNotIn(1, env['user_data'])


class FfioPayRace(unittest.IsolatedAsyncioTestCase):
    async def test_double_callback_answers_already_in_progress(self):
        make, _ = extract_branch_fn("data.startswith('ffio_pay_')")
        env = base_env()
        env['pending_ffio_orders'] = {1: {'id': 'o1', 'payment_processing': False,
                                          'prepare_response': 'pr', 'password_verified': True,
                                          'to_ccy': 'USDT'}}
        release = asyncio.Event()

        async def slow_send(*a, **k):
            await release.wait()
            return NS(id='pay-1', amount=100)

        env['send_payment'] = AsyncMock(side_effect=slow_send)
        run = make(env)
        ev1, ev2 = NS(answer=AsyncMock(), edit=AsyncMock()), NS(answer=AsyncMock(), edit=AsyncMock())

        t1 = asyncio.create_task(run(ev1, 1, 'ffio_pay_o1'))
        self.assertTrue(await drive_until(lambda: env['send_payment'].await_count == 1))
        t2 = asyncio.create_task(run(ev2, 1, 'ffio_pay_o1'))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(env['send_payment'].await_count, 1)
        self.assertFalse(t2.done())

        release.set()
        await asyncio.gather(t1, t2)
        self.assertEqual(env['send_payment'].await_count, 1)
        # ffio never resets the flag post-send: second tap hits the guard
        ev2.answer.assert_any_call('⏳ Payment already in progress...')


class StructurePin(unittest.TestCase):
    def test_both_pay_entries_locked_and_prepare_screens_not(self):
        cb = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callback_handler')
        def branch(needle):
            return next(n for n in ast.walk(cb)
                        if isinstance(n, ast.If) and needle in ast.unparse(n.test))
        for needle in ("data.startswith('confirm_payment_')", "data.startswith('ffio_pay_')"):
            body = branch(needle).body
            self.assertIsInstance(body[0], ast.AsyncWith, needle)
            self.assertIn('user_lock', ast.unparse(body[0].items[0]))
        for needle in ("data.startswith('send_quick_')", "data.startswith('onchain_speed_')"):
            src = ast.unparse(branch(needle))
            self.assertNotIn('user_lock', src, needle)
        # exactly two user_lock acquisitions in the whole handler
        uses = [n for n in ast.walk(cb) if isinstance(n, ast.AsyncWith)
                and 'user_lock' in ast.unparse(n)]
        self.assertEqual(len(uses), 2)


if __name__ == '__main__':
    unittest.main()
