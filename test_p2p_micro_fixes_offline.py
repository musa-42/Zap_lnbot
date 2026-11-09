"""Item 18 offline pins: float_sats truncation golden + _WizShelf.__contains__ semantics.

float_sats is settlement-adjacent: its int() truncation and premium math are
pinned as-is (the quirks are the spec). The shelf keeps the effective
value-based __contains__ (the removed first definition was dead — shadowed
by the later def in the same class body).
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

_sandbox = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
os.environ['HOME'] = _sandbox.name
sdk_stub = types.ModuleType('breez_sdk_spark')
sdk_stub.EventListener = type('EventListener', (), {})
spec = importlib.util.spec_from_file_location('p2p_micro', os.path.join(os.path.dirname(__file__), 'zap_p2p.py'))
p = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'breez_sdk_spark': sdk_stub}):
    spec.loader.exec_module(p)


class FloatSatsGolden(unittest.TestCase):
    """Pin CURRENT truncation/rounding semantics; no numeric change permitted."""

    def test_exact_grid(self):
        # 10,000,000 T at parity premium, 1e12 IRT/BTC → 1,000 sats
        self.assertEqual(p.float_sats(10_000_000, 0, 1_000_000_000_000), 1_000)
        # +5% premium → fewer sats (952.380… truncates to 952)
        self.assertEqual(p.float_sats(10_000_000, 5, 1_000_000_000_000), 952)
        # -5% premium → more sats (1052.631… truncates to 1052)
        self.assertEqual(p.float_sats(10_000_000, -5, 1_000_000_000_000), 1_052)
        # fractional sats TRUNCATE toward zero (int(), not round)
        self.assertEqual(p.float_sats(1, 0, 100_000_000), 1)          # 1e-8 sat → 1
        self.assertEqual(p.float_sats(3, 0, 400_000_000), 0)          # 0.75 sat → 0
        # None premium counts as 0
        self.assertEqual(p.float_sats(10_000_000, None, 1_000_000_000_000), 1_000)

    def test_invalid_rate_is_none(self):
        self.assertIsNone(p.float_sats(1000, 0, 0))
        self.assertIsNone(p.float_sats(1000, 0, None))
        self.assertIsNone(p.float_sats(1000, 0, -5))


class WizShelfContains(unittest.TestCase):
    def setUp(self):
        p.DB_PATH = os.path.join(_sandbox.name, 'micro.db')
        p._db = None
        p.init_db()
        p._mem_wiz.clear()
        self.addCleanup(p._mem_wiz.clear)

    def test_effective_contains_is_value_based(self):
        p._wiz[7] = {'stage': 'x'}
        self.assertIn(7, p._wiz)
        # a stored falsy-but-not-None value still counts (value-based check);
        # a stored None does not — this is the pinned effective semantics of
        # the surviving (second) __contains__ definition
        p._wiz[8] = {}
        self.assertIn(8, p._wiz)
        p._wiz[9] = None
        self.assertNotIn(9, p._wiz)

    def test_missing_key_not_contained(self):
        self.assertNotIn(12345, p._wiz)

    def test_exactly_one_contains_def(self):
        import ast
        src = os.path.join(os.path.dirname(__file__), 'zap_p2p.py')
        tree = ast.parse(open(src).read())
        shelf = next(n for n in tree.body
                     if isinstance(n, ast.ClassDef) and n.name == '_WizShelf')
        contains = [n for n in shelf.body
                    if isinstance(n, ast.FunctionDef) and n.name == '__contains__']
        self.assertEqual(len(contains), 1)
        # and it is the value-based one
        self.assertIn('get', ast.unparse(contains[0].body[0]))


if __name__ == '__main__':
    unittest.main()
