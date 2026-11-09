"""Item 11: money-math golden pin — the quirks ARE the spec.

Pins the current numeric outputs of the wallet's float money math on a fixed
grid: usd_to_sats float truncation, sats_to_usd round(..., 2),
parse_amount_input int(float(...)) flooring, the ffio send-all fee reserve
(max(10, int(balance * 0.01))), the calc_input int(round(...)) sites, and the
float-BTC conversions (amount_sats / SATS_PER_BTC) sent to ff.io.

Any change to these semantics — including "cleanup" to Decimal — must fail
here and update the goldens deliberately. Numeric outputs must not change.
Offline: extracts the helpers via AST and never loads the bot module.
"""
import ast
import copy
import re
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
SRC = SOURCE.read_text()
TREE = ast.parse(SRC)

RATE = 97432.17  # USD per BTC grid rate


def extract(names, ns):
    nodes = [copy.deepcopy(n) for n in TREE.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names), [n.name for n in nodes]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class MoneyMathGolden(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = extract({'sats_to_usd', 'usd_to_sats', 'parse_amount_input'},
                         {'SATS_PER_BTC': 100_000_000, 're': re,
                          'get_usd_rate': AsyncMock(return_value=RATE)})

    def test_usd_to_sats_golden(self):
        f = self.ns['usd_to_sats']
        # (usd, rate, expected_sats) — int() truncates the float product;
        # artifacts like 0.29*1e8 landing on 290.0 exactly are part of the spec.
        grid = [
            (0.01, 100000, 10),
            (0.29, 100000, 290),
            (10, 100000, 10000),
            (50, RATE, 51317),
            (100, RATE, 102635),
            (1234.56, RATE, 1267096),
            (1000000, RATE, 1026355052),
            (3, 14142, 21213),
            (7, 70000.07, 9999),
            (0, 100000, 0),
            (100, None, None),
            (100, 0, None),
        ]
        for usd, rate, expected in grid:
            with self.subTest(usd=usd, rate=rate):
                self.assertEqual(f(usd, rate), expected)

    def test_sats_to_usd_golden(self):
        f = self.ns['sats_to_usd']
        # (sats, rate, expected_usd) — round(usd, 2) with banker's rounding on
        # exact float halves; None for falsy rates.
        grid = [
            (1, 100000, 0.0),
            (29, 100000, 0.03),
            (1000, 100000, 1.0),
            (12345, RATE, 12.03),
            (123456, RATE, 120.29),
            (100000000, 100000, 100000.0),
            (999999999, RATE, 974321.7),
            (297, 0.5, 0.0),
            (100, None, None),
            (100, 0, None),
        ]
        for sats, rate, expected in grid:
            with self.subTest(sats=sats, rate=rate):
                self.assertEqual(f(sats, rate), expected)

    async def test_parse_amount_input_golden(self):
        f = self.ns['parse_amount_input']
        # Plain sats text: int(text) when digit-only, else int(float(text)) —
        # silently floors fractions, accepts scientific notation, and maps
        # inf/nan/invalid to (None, False, None) via the bare except.
        plain = [
            ('1000', (1000, False, None)),
            ('5000.9', (5000, False, None)),   # floored
            ('0.999', (0, False, None)),       # floored to zero
            ('12.5', (12, False, None)),
            ('1e3', (1000, False, None)),      # float() accepts it
            ('abc', (None, False, None)),
            ('inf', (None, False, None)),      # int(inf) raises -> except
            ('nan', (None, False, None)),      # int(nan) raises -> except
        ]
        for text, expected in plain:
            with self.subTest(text=text):
                self.assertEqual(await f(text), expected)
        # USD text with an explicit rate: no rate fetch happens.
        usd = [
            ('$50', RATE, (51317, True, 50.0)),
            ('50$', RATE, (51317, True, 50.0)),
            ('$0.29', 100000, (290, True, 0.29)),
        ]
        for text, rate, expected in usd:
            with self.subTest(text=text, rate=rate):
                self.assertEqual(await f(text, rate), expected)
                self.ns['get_usd_rate'].assert_not_awaited()
        # Falsy rate argument triggers the get_usd_rate() fetch path.
        self.assertEqual(await f('$5', 0), (5131, True, 5.0))
        self.ns['get_usd_rate'].assert_awaited_once()

    async def test_parse_amount_input_unfetchable_rate(self):
        ns = extract({'usd_to_sats', 'parse_amount_input'},
                     {'SATS_PER_BTC': 100_000_000, 're': re,
                      'get_usd_rate': AsyncMock(return_value=None)})
        self.assertEqual(await ns['parse_amount_input']('$5', 0), (None, True, 5.0))
        self.assertEqual(await ns['parse_amount_input']('$5'), (None, True, 5.0))

    def test_inline_money_statements_pinned(self):
        """Structural pin: the inline money-math statements must stay byte-
        equivalent (ast.unparse), so any semantic change fails loudly."""
        assigns = {}
        for node in ast.walk(TREE):
            if isinstance(node, ast.Assign):
                assigns.setdefault(ast.unparse(node), 0)
                assigns[ast.unparse(node)] += 1
        expected = {
            # ffio send-all fee reserve: 1% of balance, min 10 sats
            'fee_reserve = max(10, int(balance * 0.01))': 1,
            # calc_input conversions (btc / fiat / sats units)
            'sats = int(round(value * SATS_PER_BTC))': 1,
            'sats = int(round(value / fiat_rate * SATS_PER_BTC))': 1,
            'sats = int(round(value))': 1,
            # float BTC handed to ff.io pricing — float artifacts are the spec
            'amount_btc = amount_sats / SATS_PER_BTC': 3,
        }
        for stmt, count in expected.items():
            self.assertEqual(assigns.get(stmt, 0), count, stmt)


if __name__ == '__main__':
    unittest.main()
