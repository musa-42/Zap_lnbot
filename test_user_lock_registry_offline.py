"""Item 8: per-user asyncio lock registry — accessor unit test.

user_lock(uid) must return a stable per-user Lock, distinct across users.
"""
import ast
import asyncio
import copy
from pathlib import Path
import unittest

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())


def extract(name, ns):
    node = copy.deepcopy(next(n for n in TREE.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns[name]


class TestUserLockRegistry(unittest.TestCase):
    def _ns(self):
        ns = {'asyncio': asyncio, '_user_locks': {}}
        extract('user_lock', ns)
        return ns

    def test_same_user_gets_same_lock(self):
        ns = self._ns()
        self.assertIs(ns['user_lock'](1), ns['user_lock'](1))

    def test_different_users_get_distinct_locks(self):
        ns = self._ns()
        a, b = ns['user_lock'](1), ns['user_lock'](2)
        self.assertIsNot(a, b)

    def test_lock_is_an_asyncio_lock(self):
        ns = self._ns()
        self.assertIsInstance(ns['user_lock'](7), asyncio.Lock)

    def test_lock_actually_serializes(self):
        ns = self._ns()
        lock = ns['user_lock'](1)
        order = []

        async def worker(tag):
            async with lock:
                order.append(f'{tag}-in')
                await asyncio.sleep(0)
                order.append(f'{tag}-out')

        async def main():
            await asyncio.gather(worker('a'), worker('b'))
        asyncio.run(main())
        self.assertEqual(order, ['a-in', 'a-out', 'b-in', 'b-out'])


if __name__ == '__main__':
    unittest.main()
