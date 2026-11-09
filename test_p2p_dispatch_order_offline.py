"""Item 25: on_callback dispatch order regression pin.

The pilot split moved leaf branches (_cb_rate, _cb_close) into top-level
helpers and deduped the admin prelude into _admin_gate, all WITHOUT
reordering the dispatch chain. This pins the exact ordered sequence of
top-level dispatch conditions so any future branch move/reorder that changes
routing order fails loudly (prefix order matters: p2p:adm:unban must stay
behind p2p:adm, etc.).
"""
import ast
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent / 'zap_p2p.py').read_text()


def dispatch_tests():
    tree = ast.parse(SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == 'on_callback')
    return [ast.unparse(s.test) for s in fn.body if isinstance(s, ast.If)]


DISPATCH = [
    'is_banned(uid) and (not admin)',
    'not admin and (not enabled_for(uid))',
    "data == 'p2p' or data == 'p2p:home'",
    "data == 'p2p:agree'",
    "data == 'p2p:exit'",
    "data.startswith('p2p:chat:')",
    "data.startswith('p2p:pmode:')",
    "data.startswith('p2p:rate:')",
    "data.startswith('p2p:close:')",
    "data == 'p2p:active'",
    "data == 'p2p:guide'",
    "data == 'p2p:market'",
    "data.startswith('p2p:mf:') or data.startswith('p2p:mprev:') or data.startswith('p2p:mnext:')",
    "data == 'p2p:new'",
    "data == 'p2p:wcancel'",
    "data.startswith('p2p:side:')",
    "data.startswith('p2p:unit:')",
    "data.startswith('p2p:prem:')",
    "data == 'p2p:skipmethod'",
    "data.startswith('p2p:ttl:')",
    "data == 'p2p:publish'",
    "data == 'p2p:mine'",
    "data.startswith('p2p:ocancel:')",
    "data.startswith('p2p:take:')",
    "data.startswith('p2p:takego:')",
    "data.startswith('p2p:room:')",
    "data.startswith('p2p:lock:')",
    "data.startswith('p2p:lockgo:')",
    "data.startswith('p2p:fiat:')",
    "data.startswith('p2p:fiatgo:')",
    "data.startswith('p2p:rel:')",
    "data.startswith('p2p:relgo:')",
    "data.startswith('p2p:cx:')",
    "data.startswith('p2p:cxgo:')",
    "data.startswith('p2p:dsp:')",
    "data.startswith('p2p:dspgo:')",
    "data.startswith('p2p:admholds:')",
    "data.startswith('p2p:admhold:')",
    "data.startswith('p2p:adjudicate:')",
    "data.startswith('p2p:feeconfirm:')",
    "data.startswith('p2p:feesend:')",
    "data.startswith('p2p:admevents:')",
    "data.startswith('p2p:admevent:')",
    "data.startswith('p2p:admreview:')",
    "data.startswith('p2p:admcase:')",
    "data.startswith('p2p:admchat:')",
    "data.startswith('p2p:adm:unban:')",
    "data.startswith('p2p:adm:admdel:')",
    "data == 'p2p:adm'",
    "data == 'p2p:adm:disputes'",
    "data == 'p2p:adm:ban'",
    "data == 'p2p:adm:banlist'",
    "data == 'p2p:adm:admins'",
    "data == 'p2p:adm:admadd'",
    "data.startswith('p2p:adm:banok:')",
    "data.startswith('p2p:adm:cxlive:')",
    "data.startswith('p2p:legacyack:')",
    "data.startswith('p2p:admrel:')",
    "data.startswith('p2p:admref:')",
]


class DispatchOrder(unittest.TestCase):
    def test_dispatch_order_pinned(self):
        self.assertEqual(dispatch_tests(), DISPATCH)

    def test_admin_gate_dedup_replaced_all_copies(self):
        self.assertEqual(SRC.count('if await _admin_gate(event, uid):'), 21)
        # only the handler-top `admin = is_p2p_admin(uid)` prelude remains
        tree = ast.parse(SRC)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == 'on_callback')
        self.assertEqual(ast.unparse(fn).count('is_p2p_admin('), 1)

    def test_leaf_helpers_exist_and_are_called_in_place(self):
        self.assertIn("data.startswith('p2p:rate:')", SRC)
        self.assertEqual(SRC.count('await _cb_rate(client, event, uid, data)'), 1)
        self.assertEqual(SRC.count('await _cb_close(client, event, uid, data)'), 1)


if __name__ == '__main__':
    unittest.main()
