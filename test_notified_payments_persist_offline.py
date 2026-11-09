"""Item 13: deposit-notification idempotency survives restarts — offline.

notified_payments used to be memory-only, so every bot restart re-notified
deposits seen in the previous process. The kv store now mirrors the map
(best-effort) and the boot path restores it, dropping expired/malformed
entries. Offline: helpers are AST-extracted; a fake kv store stands in for
KeyValueSqlite. Also pins the three wiring sites structurally.
"""
import ast
import copy
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
import unittest

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
SRC = SOURCE.read_text()
TREE = ast.parse(SRC)

NS = {'datetime': datetime, 'logging': logging, 'threading': threading,
      'notified_payments': {}, 'notified_payments_lock': threading.Lock()}
for _name in ('_load_notified_payments', '_save_notified_payments'):
    _node = copy.deepcopy(next(n for n in TREE.body
                               if isinstance(n, ast.FunctionDef) and n.name == _name))
    _node.decorator_list = []
    exec(compile(ast.Module(body=[_node], type_ignores=[]), str(SOURCE), 'exec'), NS)

load, save = NS['_load_notified_payments'], NS['_save_notified_payments']


class FakeStore:
    def __init__(self, initial=None, fail=False):
        self.data = dict(initial or {})
        self.fail = fail

    def get(self, key):
        if self.fail:
            raise RuntimeError('boom')
        return self.data.get(key)

    def set(self, key, value):
        if self.fail:
            raise RuntimeError('boom')
        self.data[key] = value


class NotifiedPaymentsPersistence(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        now = datetime.now()
        NS['notified_payments'] = {
            'p1': now - timedelta(seconds=10),
            'p2': now - timedelta(hours=2),
        }
        store = FakeStore()
        save(store)
        # persisted payload is ISO strings
        self.assertEqual(store.data['notified_payments']['p1'],
                         NS['notified_payments']['p1'].isoformat())
        # restart: fresh in-memory map, restore from the store
        NS['notified_payments'] = {}
        NS['notified_payments'] = load(store, now)
        self.assertEqual(set(NS['notified_payments']), {'p1'})

    def test_restart_suppresses_duplicate_notification(self):
        now = datetime.now()
        store = FakeStore({'notified_payments': {'pay-9': now.isoformat()}})
        NS['notified_payments'] = load(store, now)
        # this is the exact membership check the SDK event handler performs
        self.assertIn('pay-9', NS['notified_payments'])

    def test_malformed_and_missing_entries(self):
        now = datetime.now()
        store = FakeStore({'notified_payments': {'bad': 'not-a-date', None: 'x',
                                                 'ok': now.isoformat()}})
        self.assertEqual(set(load(store, now)), {'ok'})
        self.assertEqual(load(FakeStore(), now), {})

    def test_save_failure_is_best_effort(self):
        NS['notified_payments'] = {'p': datetime.now()}
        save(FakeStore(fail=True))  # must not raise

    def test_wiring_sites_pinned(self):
        # mark site: _save call inside the dedup With-block that marks the id
        withs = [n for n in ast.walk(TREE) if isinstance(n, ast.With)]
        mark_block = [w for w in withs
                      if any(isinstance(s, ast.Assign) and ast.unparse(s) ==
                             'notified_payments[payment_id] = datetime.now()'
                             for s in w.body)]
        self.assertEqual(len(mark_block), 1)
        self.assertTrue(any(isinstance(s, ast.Expr) and '_save_notified_payments(db)' in ast.unparse(s)
                            for s in mark_block[0].body))
        # cleanup site: _save call inside the cleanup With-block
        cleanup_block = [w for w in withs
                         if any(isinstance(s, ast.Assign) and 'old_payments' in ast.unparse(s)
                                for s in w.body)]
        self.assertEqual(len(cleanup_block), 1)
        self.assertTrue(any(isinstance(s, ast.Expr) and '_save_notified_payments(db)' in ast.unparse(s)
                            for s in ast.walk(cleanup_block[0])))
        # boot site: module-level restore
        boot = [n for n in TREE.body if isinstance(n, ast.Expr) and
                ast.unparse(n.value) == 'notified_payments.update(_load_notified_payments(db, datetime.now()))']
        self.assertEqual(len(boot), 1)


if __name__ == '__main__':
    unittest.main()
