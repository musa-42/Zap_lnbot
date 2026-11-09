"""Item 20 golden pins: deduped back-row helpers must stay byte-identical to
the inline literals they replaced, and the inline copies that remain (inside
message_handler, the frozen zap branches, and the exec-extracted callback
branches) stay pinned until item 26 can widen the env.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SRC = (ROOT / 'zap_wallet_bot_v32.py').read_text()

ROW_LITERALS = {
    'row1': '[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]',
    'row2': '[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]',
    'row3': '[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]',
}
# remaining inline copies after item 20 (message_handler + _batch_send_all +
# frozen tip_amount_/send_quick_ + exec-extracted confirm_payment_/ffio_pay_/
# tkwd_go branches). row1 29→30 (2026-11 hold rework): the donation hold
# branch passes one inline back-menu row to the recheck spawn — the helper
# is banned from message_handler (see test below), so the inline form is the
# correct one there.
REMAINING = {'row1': 30, 'row2': 17, 'row3': 12}


def _extract(name):
    tree = ast.parse(SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(fn)


class BackRowHelpers(unittest.TestCase):
    def _run(self, name, uid):
        code = _extract(name)
        env = {'t': lambda u, k, **kw: ('T', u, k),
               'Button': type('B', (), {'inline': staticmethod(lambda l, d: (l, d))})}
        exec(compile(ast.parse(code), '<helper>', 'exec'), env)
        return env[name](uid)

    def test_row_back_menu_identity(self):
        self.assertEqual(self._run('_row_back_menu', 7),
                         [[(('T', 7, 'btn_back_menu'), b'back_to_menu')]])

    def test_row_cancel_to_menu_identity(self):
        self.assertEqual(self._run('_row_cancel_to_menu', 7),
                         [[(('T', 7, 'btn_cancel'), b'back_to_menu')]])

    def test_row_cancel_settings_identity(self):
        self.assertEqual(self._run('_row_cancel_settings', 7),
                         [[(('T', 7, 'btn_cancel'), b'settings_back')]])

    def test_inline_literal_counts_pinned(self):
        for k, lit in ROW_LITERALS.items():
            self.assertEqual(SRC.count(lit), REMAINING[k],
                             f'{k} inline copies changed')

    def test_helper_call_counts(self):
        self.assertEqual(SRC.count('_row_back_menu(user_id)'), 14)
        self.assertEqual(SRC.count('_row_cancel_to_menu(user_id)'), 8)
        self.assertEqual(SRC.count('_row_cancel_settings(user_id)'), 6)

    def test_helpers_not_referenced_from_message_handler(self):
        """message_handler is exec-isolated with a fixed env (secret dispatch
        test) and must not gain any new global dependency."""
        tree = ast.parse(SRC)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        for helper in ('_row_back_menu', '_row_cancel_to_menu', '_row_cancel_settings'):
            self.assertNotIn(helper, names)

    def test_helpers_not_referenced_from_batch_send_all(self):
        """_batch_send_all is exec-extracted by the batch persistence tests."""
        tree = ast.parse(SRC)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == '_batch_send_all')
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        for helper in ('_row_back_menu', '_row_cancel_to_menu', '_row_cancel_settings'):
            self.assertNotIn(helper, names)

    def test_helpers_not_referenced_from_exec_extracted_branches(self):
        """confirm_payment_/ffio_pay_/tkwd_go branches are extracted and exec'd
        with fixed envs by the payment-lock and token-safety tests. The
        extractors keep the elif tail (orelse), but with matching data only
        the branch BODY executes — so the body must stay helper-free."""
        tree = ast.parse(SRC)
        cb = next(n for n in tree.body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callback_handler')
        for needle in ("data.startswith('confirm_payment_')",
                       "data.startswith('ffio_pay_')", "tkwd_go:"):
            branch = next(n for n in ast.walk(cb)
                          if isinstance(n, ast.If) and needle in ast.unparse(n.test))
            names = set()
            for stmt in branch.body:
                names |= {x.id for x in ast.walk(stmt) if isinstance(x, ast.Name)}
            for helper in ('_row_back_menu', '_row_cancel_to_menu', '_row_cancel_settings'):
                self.assertNotIn(helper, names, f'{helper} leaked into {needle}')


if __name__ == '__main__':
    unittest.main()
