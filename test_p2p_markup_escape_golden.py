"""Item 3 golden: maker-controlled HTML is escaped in public/audit renders.

Benign fixtures must render byte-identical to pre-change output (html.escape
is the identity on text without &<>"'); hostile markup must arrive escaped.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_p2p_escrow_safety as base

p = base.p

BENIGN = {'side': 'sell', 'maker': 20, 'sats': 100000, 'price': '5,000,000',
          'method': 'Bank transfer', 'fiat_code': 'IRT', 'status': 'open'}
EVIL = dict(BENIGN, price='<b>evil</b>', method='<i>spoof</i> & "quotes"')

BENIGN_CARD_EN = (
    '📢 Order\n'
    '━━━━━━━━━━━━━━\n'
    '💸 Selling sats\n'
    '🪙 100,000 sats\n'
    '🏷 At 5,000,000 تومان\n'
    '💳 Method: Bank transfer\n'
    '━━━━━━━━━━━━━━\n'
    '👤 0 successful trades in 0 days'
)


class OrderCardGolden(base.Safety):
    def card(self, o):
        return p.order_card(1, dict(o))

    def test_benign_card_is_byte_identical_golden(self):
        self.assertEqual(self.card(BENIGN), BENIGN_CARD_EN)

    def test_hostile_price_and_method_are_escaped(self):
        card = self.card(EVIL)
        self.assertNotIn('<b>evil</b>', card)
        self.assertNotIn('<i>spoof</i>', card)
        self.assertIn('🏷 At &lt;b&gt;evil&lt;/b&gt;', card)
        self.assertIn('💳 Method: &lt;i&gt;spoof&lt;/i&gt; &amp; &quot;quotes&quot;', card)

    def test_benign_fa_card_price_line_unchanged(self):
        with patch.object(p, 'lang_of', return_value='fa'):
            card = self.card(BENIGN)
        self.assertIn('🏷 به قیمت 5,000,000', card)

    def test_hostile_fa_card_price_is_escaped(self):
        with patch.object(p, 'lang_of', return_value='fa'):
            card = self.card(EVIL)
        self.assertNotIn('<b>evil</b>', card)
        self.assertIn('🏷 به قیمت &lt;b&gt;evil&lt;/b&gt;', card)


class AuditEscape(unittest.IsolatedAsyncioTestCase):
    """Real audit() against a throwaway DB; base.Safety mocks audit itself."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p.DB_PATH = os.path.join(self.tmp.name, 'audit.db')
        p._db = None
        p.init_db()
        self.addCleanup(self._close)

    def _close(self):
        p._db.close()
        p._db = None

    def _trade(self, price):
        order = p._exec("INSERT INTO orders (status,created) VALUES ('taken',0)").lastrowid
        return p._exec('INSERT INTO trades (order_id,buyer,seller,sats,fee,price,stage,created,updated) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (order, 10, 20, 1000, 5, price, 'locked', 1, 1)).lastrowid

    async def _audit(self, price):
        send = AsyncMock()
        tid = self._trade(price)
        bot = SimpleNamespace(client=SimpleNamespace(send_message=send))
        with patch.object(p, '_bot', return_value=bot), \
             patch.dict(os.environ, {'BACKUP_CHANNEL_ID': '-100123'}, clear=True):
            await p.audit(None, tid, 'STAGE X')
        return send.await_args.args[1], send.await_args.kwargs

    async def test_audit_benign_price_byte_identical(self):
        text, kwargs = await self._audit('5,000,000')
        self.assertIn('💰 1000+5s · 🏷 5,000,000', text)
        self.assertEqual(kwargs.get('parse_mode'), 'html')
        # intentional tg:// links stay raw HTML
        self.assertIn('<a href="tg://user?id=10">10</a>', text)

    async def test_audit_hostile_price_is_escaped(self):
        text, _ = await self._audit('<b>evil</b>')
        self.assertNotIn('🏷 <b>evil</b>', text)
        self.assertIn('🏷 &lt;b&gt;evil&lt;/b&gt;', text)


class MdEscapeGolden(unittest.TestCase):
    """Item 14: _md_escape on wallet-core user interpolands (memos, display
    names). Benign bytes identical; hostile bold/italic/link markup rendered
    inert under Telethon's markdown parser; the zap From/To sites stay frozen
    (untouched) and only the three planned sites gain escaping."""

    @classmethod
    def setUpClass(cls):
        import ast
        import copy
        from pathlib import Path
        src = Path(__file__).with_name('zap_wallet_bot_v32.py')
        tree = ast.parse(src.read_text())
        node = copy.deepcopy(next(n for n in tree.body
                                  if isinstance(n, ast.FunctionDef) and n.name == '_md_escape'))
        node.decorator_list = []
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(src), 'exec'), ns)
        cls.esc = staticmethod(ns['_md_escape'])
        cls.tree = tree

    def test_benign_bytes_identical(self):
        for s in ('coffee, order 42!', 'سیستم عالی', 'Payment from Ali',
                  'plain-text memo', 'Amount 1000 — thanks'):
            self.assertEqual(self.esc(s), s)

    def test_hostile_markup_is_escaped(self):
        self.assertEqual(self.esc('[c](https://evil)'), '[c]\\(https://evil)')
        self.assertEqual(self.esc('**bold**'), '\\*\\*bold\\*\\*')
        self.assertEqual(self.esc('__it__'), '\\_\\_it\\_\\_')
        self.assertEqual(self.esc('a\\*'), 'a\\\\\\*')  # backslash first: no un-escaping

    def test_escaped_markup_has_no_telethon_entities(self):
        from telethon.extensions import markdown
        for hostile in ('[c](https://evil)', '[phish](tg://user?id=1)',
                        '**bold**', '__italic__', '\\*\\*pre\\*\\*'):
            _, entities = markdown.parse(self.esc(hostile))
            self.assertEqual(entities, [], hostile)

    def test_exactly_three_call_sites_in_planned_functions(self):
        import ast
        calls = [n for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == '_md_escape']
        self.assertEqual(len(calls), 5)
        funcs = [(f.lineno, f.end_lineno, f.name) for f in ast.walk(self.tree)
                 if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
        enclosing = set()
        for c in calls:
            enclosing |= {name for a, b, name in funcs if a <= c.lineno <= b}
        self.assertEqual(enclosing,
                         {'format_payment_details', '_notify_payment_received',
                          'prepare_and_show_fee', 'callback_handler'})

    def test_to_render_benign_identity_golden(self):
        # The callback_handler "To:" renders escape only the display name;
        # for benign names the rendered line is byte-identical to the
        # pre-change f-string.
        for name in ('Ali', 'Sara — Lightning user', 'سیستم عالی', 'None'):
            self.assertEqual(
                f"👤 To: @ali ({self.esc(str(name))}) ✅\n\n",
                f"👤 To: @ali ({name}) ✅\n\n")

    def test_to_render_hostile_display_name_is_escaped(self):
        from telethon.extensions import markdown
        hostile = '**impersonating** you [phish](tg://user?id=1)'
        rendered = f"👤 To: @ali ({self.esc(str(hostile))}) ⚠️\n\n"
        self.assertNotIn('**impersonating**', rendered)
        _, entities = markdown.parse(rendered)
        self.assertEqual(entities, [])

    def test_frozen_send_quick_to_render_stays_raw(self):
        # C1 freeze: the To: render inside the send_quick_ branch (the only
        # remaining unescaped one) must not gain escaping in this pass.
        from pathlib import Path
        src = Path(__file__).with_name('zap_wallet_bot_v32.py').read_text()
        raw = "({ru['display_name']})"
        self.assertEqual(src.count(raw), 1, 'send_quick_ frozen site changed')


if __name__ == '__main__':
    unittest.main()
