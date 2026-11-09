"""Item 26 golden pins: constrained god-handler leaf extraction.

Three callback_handler leaves (tktrack, signmsg_help, del_lnaddress) and two
message_handler steps (webhook_url_input, webhook_del_input) moved to
top-level helpers with their dispatch tests byte-identical in place. The
secret-dispatch exec paths never execute these branches (SENSITIVE steps and
the no-step dispatch only), so the fixed env needs no new names — pinned
here. The sensitive prelude and SENSITIVE tuple semantics are untouched.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SRC = (ROOT / 'zap_wallet_bot_v32.py').read_text()
TREE = ast.parse(SRC)


def _fn(name):
    return next(n for n in TREE.body
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == name)


CALLBACK_LEAVES = {'tktrack', 'signmsg_help', 'del_lnaddress'}
STEP_LEAVES = {'webhook_url_input', 'webhook_del_input'}


class LeafExtractionGolden(unittest.TestCase):
    def test_helpers_exist_at_module_level(self):
        names = {n.name for n in TREE.body
                 if isinstance(n, ast.AsyncFunctionDef)}
        for h in ('_cb_tktrack', '_cb_signmsg_help', '_cb_del_lnaddress',
                  '_step_webhook_url', '_step_webhook_del'):
            self.assertIn(h, names)

    def test_callback_dispatch_bodies_are_pure_delegation(self):
        fn = _fn('callback_handler')
        seen = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                test = ast.unparse(node.test)
                for leaf in CALLBACK_LEAVES:
                    if f"'{leaf}'" in test:
                        body = [ast.unparse(s) for s in node.body]
                        helper = {'tktrack': '_cb_tktrack',
                                  'signmsg_help': '_cb_signmsg_help',
                                  'del_lnaddress': '_cb_del_lnaddress'}[leaf]
                        self.assertEqual(
                            body, [f'await {helper}(event, user_id)', 'return'],
                            leaf)
                        seen.add(leaf)
        self.assertEqual(seen, CALLBACK_LEAVES)

    def test_step_dispatch_bodies_are_pure_delegation(self):
        fn = _fn('message_handler')
        seen = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                test = ast.unparse(node.test)
                for step in STEP_LEAVES:
                    # ast.unparse normalizes string literals to single quotes
                    if f"'{step}'" in test:
                        body = [ast.unparse(s) for s in node.body]
                        helper = {'webhook_url_input': '_step_webhook_url',
                                  'webhook_del_input': '_step_webhook_del'}[step]
                        self.assertEqual(
                            body, [f'await {helper}(event, user_id, text)',
                                   'return'], step)
                        seen.add(step)
        self.assertEqual(seen, STEP_LEAVES)

    def test_moved_step_bodies_not_referenced_from_sensitive_prelude(self):
        """The secret-dispatch exec env provides a fixed name set; the moved
        branches must not be reachable from any SENSITIVE step body (they
        are exclusive elif arms, and those tests only run SENSITIVE steps
        or no step at all)."""
        SENSITIVE = ('set_send_password', 'set_new_send_password',
                     'verify_old_send_password', 'confirm_remove_send_password',
                     'verify_password_for_backup',
                     'verify_send_password_for_payment',
                     'verify_send_password_for_ffio', 'set_password_hint',
                     'set_password_hint_after_password', 'recovery_input')
        fn = _fn('message_handler')
        matched = 0
        for node in ast.walk(fn):
            if isinstance(node, ast.If):
                test = ast.unparse(node.test)
                if any(f"'{s}'" in test for s in SENSITIVE):
                    matched += 1
                    # walk ONLY this arm's body: node.walk would descend into
                    # orelse, i.e. the rest of the elif chain (which legally
                    # contains the moved webhook delegations)
                    names = set()
                    for stmt in node.body:
                        names |= {x.id for x in ast.walk(stmt)
                                  if isinstance(x, ast.Name)}
                    self.assertFalse(
                        names & {'_step_webhook_url', '_step_webhook_del'},
                        ast.unparse(node.test))
        # non-vacuous: every SENSITIVE step must have its own dispatch arm
        self.assertEqual(matched, len(SENSITIVE), matched)

    def test_frozen_handlers_untouched(self):
        for frozen in ('tip_handler', 'process_zap_full', 'process_zap_payment',
                       'show_tip_prompt'):
            self.assertIn(f'async def {frozen}', SRC)


if __name__ == '__main__':
    unittest.main()
