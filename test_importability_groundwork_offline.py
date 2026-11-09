"""Item 27 golden pins: importability groundwork — test-only.

- zap_p2p / zap_tokens(=zt) are imported at module top of zap_wallet_bot_v32.
- The remaining function-local zap imports are exactly the injection seams
  the offline exec-isolation tests patch via sys.modules (an import-anywhere
  in a function makes the name function-local, so sibling branches in the
  same function MUST keep their own imports — the tkwd_go lesson).
- get_balance is a pure str() wrapper over the typed get_balance_raw core;
  rendered bytes for existing callers are unchanged ('<sats>' / '0').
- zap_p2p and zap_tokens have assignment-only module bodies, so importing
  them twice in a fresh interpreter is side-effect free (double-import smoke
  via subprocess; the unittest process's sys.modules stays untouched).
"""
import ast
import logging
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SRC = (ROOT / 'zap_wallet_bot_v32.py').read_text()
TREE = ast.parse(SRC)


def _fn(name):
    return next(n for n in TREE.body
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == name)


def local_zap_imports():
    """(lineno, module, enclosing top-level function) for every
    function-local zap_p2p/zap_tokens import."""
    out = []
    for n in TREE.body:
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for sub in ast.walk(n):
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        if a.name in ('zap_p2p', 'zap_tokens'):
                            out.append((sub.lineno, a.name, n.name))
    return sorted(out)


class ModuleTopImports(unittest.TestCase):
    def test_zap_modules_imported_at_module_top(self):
        tops = set()
        for n in TREE.body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    tops.add((a.name, a.asname))
        self.assertIn(('zap_p2p', None), tops)
        self.assertIn(('zap_tokens', 'zt'), tops)

    def test_local_import_census_is_exactly_the_injection_seams(self):
        seams = local_zap_imports()
        self.assertEqual(
            [(m, f) for _, m, f in seams],
            [('zap_tokens', 'token_withdraw_reconcile'),
             ('zap_tokens', 'token_wd_quote_totals'),
             ('zap_tokens', 'callback_handler'),
             ('zap_tokens', 'callback_handler'),
             ('zap_tokens', 'callback_handler'),
             ('zap_tokens', 'callback_handler'),
             ('zap_tokens', 'callback_handler'),
             ('zap_p2p', 'message_handler'),
             ('zap_p2p', 'message_handler')],
            seams)

    def test_every_zt_reference_in_callback_handler_is_covered_by_an_import(self):
        """A zt reference in a dispatch branch whose arm never executes an
        import raises UnboundLocalError (zt is function-local). Every
        referencing arm must import first — except the pre-existing tkwd:
        except-render, which was equally unbound before item 27 and is
        pinned here as a known latent edge."""
        cb = _fn('callback_handler')
        arms = [n for n in next(t for t in cb.body if isinstance(t, ast.Try)).body
                if isinstance(n, ast.If)]
        for arm in arms:
            refs = [x for x in ast.walk(arm) if isinstance(x, ast.Name) and x.id == 'zt']
            imps = [x for x in ast.walk(arm) if isinstance(x, ast.Import)
                    and any(a.name == 'zap_tokens' for a in x.names)]
            test = ast.unparse(arm.test)
            if refs and not imps:
                # only the known pre-existing latent edge may remain
                self.assertIn("'tkwd:'", test, test)
                handler = [s for s in ast.walk(arm)
                           if isinstance(s, ast.ExceptHandler)][0]
                self.assertTrue(all(r.lineno >= handler.lineno for r in refs),
                                test)

    def test_message_handler_has_no_zap_tokens_import(self):
        """The three token step arms resolve zt via the module-top global;
        no import statement may remain there (it would re-localize zt and
        UnboundLocalError the sibling arms)."""
        for node in ast.walk(_fn('message_handler')):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotEqual(a.name, 'zap_tokens')


class GetBalanceSplit(unittest.TestCase):
    def test_get_balance_is_pure_str_wrapper(self):
        body = [ast.unparse(s) for s in _fn('get_balance').body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        self.assertEqual(body, ["return str(await get_balance_raw(user_id))"])

    def test_get_balance_raw_signature_and_paths(self):
        fn = _fn('get_balance_raw')
        self.assertEqual(fn.returns.id, 'int')
        rets = [ast.unparse(r.value) for n in ast.walk(fn)
                if isinstance(n, ast.Return) for r in [n]]
        self.assertIn('info.balance_sats', rets)
        # error fallback is the int 0 (get_balance renders it as "0")
        self.assertIn('0', rets)

    def test_exec_both_paths_bytes_unchanged(self):
        import breez_sdk_spark as bss
        calls = []
        ns = {}

        class Info:
            balance_sats = 1234

        class Sdk:
            async def get_info(self, request):
                calls.append(request.ensure_synced)
                return Info()

        async def get_wallet_ok(uid):
            return Sdk()

        async def get_wallet_boom(uid):
            raise RuntimeError('no wallet')

        async def close_wallet(sdk):
            calls.append('closed')

        for fn_name in ('get_balance_raw', 'get_balance'):
            node = _fn(fn_name)
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<gb>', 'exec'), ns)
        # sdk_read = hang-guard seam around the read-only get_info call
        # (2026-11); passthrough here — the guard itself has its own tests.
        ns.update(get_wallet=get_wallet_ok, close_wallet=close_wallet,
                  GetInfoRequest=bss.GetInfoRequest, logging=logging,
                  sdk_read=lambda aw, what, timeout_secs=45: aw)
        raw, bal = ns['get_balance_raw'], ns['get_balance']
        self.assertEqual(__import__('asyncio').run(bal(7)), '1234')
        self.assertEqual(__import__('asyncio').run(raw(7)), 1234)
        # error path → "0" / 0, wallet closed exactly when opened
        ns2 = {}
        for fn_name in ('get_balance_raw', 'get_balance'):
            node = _fn(fn_name)
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<gb>', 'exec'), ns2)
        ns2.update(get_wallet=get_wallet_boom, close_wallet=close_wallet,
                   GetInfoRequest=bss.GetInfoRequest, logging=logging,
                   sdk_read=lambda aw, what, timeout_secs=45: aw)
        self.assertEqual(__import__('asyncio').run(ns2['get_balance'](7)), '0')
        self.assertEqual(__import__('asyncio').run(ns2['get_balance_raw'](7)), 0)
        self.assertEqual(calls, [False, 'closed', False, 'closed'])


class DoubleImportSmoke(unittest.TestCase):
    def test_section_modules_import_twice_side_effect_free(self):
        root = str(ROOT)
        code = (
            "import importlib.util, sys\n"
            "for name in ('zap_p2p', 'zap_tokens'):\n"
            "    mods = []\n"
            "    for i in range(2):\n"
            "        spec = importlib.util.spec_from_file_location(\n"
            f"            f'{{name}}_fresh_{{i}}', f'{root}/{{name}}.py')\n"
            "        m = importlib.util.module_from_spec(spec)\n"
            "        spec.loader.exec_module(m)\n"
            "        mods.append(m)\n"
            "    assert mods[0] is not mods[1]\n"
            "print('DOUBLE-IMPORT-OK')\n"
        )
        r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                           text=True, cwd=root, timeout=120,
                           env={'PYTHONDONTWRITEBYTECODE': '1',
                                'PATH': '/usr/bin:/bin', 'HOME': root})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('DOUBLE-IMPORT-OK', r.stdout)


if __name__ == '__main__':
    unittest.main()
