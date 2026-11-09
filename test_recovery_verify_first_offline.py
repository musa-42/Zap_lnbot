"""Item 7a: recovery_input must verify the new phrase BEFORE destroying the
old wallet — offline. Wrong-but-valid 12-word phrase → old dir intact, DB seed
unchanged, same error text; only a loadable phrase proceeds to the swap.
Also pins 7b/7c zap_tokens guards.
"""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())

GOOD = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'


def recovery_branch(ns):
    fn = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
    node = next(n for n in ast.walk(fn)
                if isinstance(n, ast.If) and ast.unparse(n.test) == "current_step == 'recovery_input'")
    f = ast.AsyncFunctionDef(name='branch', args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg='event')], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=copy.deepcopy(node.body), decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[f], type_ignores=[])), str(SOURCE), 'exec'), ns)
    return ns['branch']


class FakeShutil:
    def __init__(self, exists_paths):
        self.rmtrees = []
        self.os = SimpleNamespace(path=SimpleNamespace(exists=lambda p: p in exists_paths))

    def rmtree(self, path, **kwargs):
        self.rmtrees.append(path)


class FakeDB:
    def __init__(self, initial):
        self.kv = dict(initial)
        self.sets = []

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.sets.append((key, value))
        self.kv[key] = value


class VerifyFirst(unittest.IsolatedAsyncioTestCase):
    def _env(self, *, check_ok, connect_fails=False, old_dir_exists=True, old_seed='ENC-old'):
        sh = FakeShutil({'./.1'} if old_dir_exists else set())
        db = FakeDB({'1': old_seed})
        sdk = SimpleNamespace(disconnect=AsyncMock(), sync_wallet=AsyncMock())
        connect = AsyncMock(side_effect=RuntimeError('unloadable') if connect_fails
                            else None, return_value=sdk)
        ev = SimpleNamespace(respond=AsyncMock())
        ns = {
            'text': GOOD, 'user_id': 1, 'event': ev,
            'mnemo': SimpleNamespace(check=lambda w: check_ok),
            'Button': SimpleNamespace(inline=lambda label, data: (label, data)),
            't': lambda uid, key: key,
            'active_user_connections': {1: {'sdk': SimpleNamespace(disconnect=AsyncMock())}},
            'os': sh.os, 'shutil': sh, 'logging': __import__('logging'),
            'connect': connect,
            'ConnectRequest': lambda **kw: kw,
            'Seed': SimpleNamespace(MNEMONIC=lambda mnemonic, passphrase: ('seed', mnemonic)),
            'config': object(),
            'encrypt_seed': lambda s: 'ENC:' + s,
            'db': db, 'user_steps': {1: 'recovery_input'},
            'get_wallet': AsyncMock(return_value=sdk),
            'SyncWalletRequest': lambda **kw: kw,
        }
        return recovery_branch(ns), ns, ev, sh, db, connect

    def _rmtree_old(self, sh):
        return [p for p in sh.rmtrees if p == './.1']

    async def test_bad_mnemonic_leaves_everything_intact(self):
        branch, ns, ev, sh, db, connect = self._env(check_ok=False)
        await branch(ev)
        self.assertIn('Invalid recovery phrase. Please check your words.', str(ev.respond.await_args.args[0]))
        self.assertEqual(self._rmtree_old(sh), [])
        self.assertEqual(db.sets, [])
        self.assertEqual(ns['user_steps'], {1: 'recovery_input'})
        connect.assert_not_awaited()

    async def test_unloadable_phrase_keeps_old_wallet_and_seed(self):
        branch, ns, ev, sh, db, connect = self._env(check_ok=True, connect_fails=True)
        await branch(ev)
        self.assertIn('Invalid recovery phrase. Please check your words.', str(ev.respond.await_args.args[0]))
        # old wallet dir untouched, DB seed untouched, step unchanged
        self.assertEqual(self._rmtree_old(sh), [])
        self.assertEqual(db.sets, [])
        self.assertEqual(db.get('1'), 'ENC-old')
        self.assertEqual(ns['user_steps'], {1: 'recovery_input'})
        # the throwaway verify dir is cleaned up either way
        self.assertIn('./.1-recovery-verify', sh.rmtrees)

    async def test_loadable_phrase_proceeds_to_swap(self):
        branch, ns, ev, sh, db, connect = self._env(check_ok=True, connect_fails=False)
        await branch(ev)
        self.assertEqual(self._rmtree_old(sh), ['./.1'])
        self.assertEqual(db.sets, [('1', 'ENC:' + GOOD)])
        self.assertNotIn(1, ns['user_steps'])
        self.assertIn('Wallet Recovered!', str(ev.respond.await_args.args[0]))
        ns['get_wallet'].assert_awaited_once_with(1)
        # verify connection is opened against the temp dir and closed again
        req = connect.await_args.kwargs['request']
        self.assertEqual(req['storage_dir'], './.1-recovery-verify')


class TokenGuards(unittest.TestCase):
    def test_to_base_units_rejects_zero_negative_and_dust(self):
        import zap_tokens as zt
        for bad in (0, -1, 0.0000001, float('inf'), float('nan')):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    zt.to_base_units(bad, 6)
        self.assertEqual(zt.to_base_units(1.5, 6), 1500000)

    def test_prepare_withdraw_rejects_nonpositive_amount(self):
        import zap_tokens as zt
        sdk = SimpleNamespace()
        for bad in (0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    import asyncio
                    asyncio.run(zt.prepare_withdraw(sdk, 'USDB', 'tron', 'addr', bad))

    def test_convert_raises_when_credit_never_lands(self):
        import asyncio
        import zap_tokens as zt

        class SDK:
            async def receive_payment(self, request=None):
                return SimpleNamespace(payment_request='addr')

            async def prepare_send_payment(self, request=None):
                return object()

            async def send_payment(self, request=None):
                return SimpleNamespace(payment=SimpleNamespace(status='COMPLETED'))

        async def run():
            with unittest.mock.patch.object(zt, 'usdb_raw_balance', new=AsyncMock(return_value=0)), \
                 unittest.mock.patch.object(zt.asyncio, 'sleep', new=AsyncMock()):
                return await zt.convert_sats_to_usdb(SDK(), 1_000_000)
        with self.assertRaises(RuntimeError):
            asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
