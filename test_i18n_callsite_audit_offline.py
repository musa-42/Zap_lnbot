"""Item 12: t()/T() call-site placeholder audit — test-only.

For every literal-key t(uid, 'key', ...) call in zap_wallet_bot_v32.py and
T(uid, 'key', ...) call in zap_p2p.py:

- the key must exist in the EN pack (t()/T() otherwise render the raw key),
- the call must pass every {placeholder} the pack string declares (a missing
  kwarg renders raw braces — t() swallows the KeyError), and
- it must not pass kwargs the string does not declare, beyond the pinned
  known-extras below (format() silently ignores them today).

A chained `.format(...)` on the t()/T() result credits its keyword names
(p2p's `T(uid, 'unit_toman').format(fiat=...)` pattern). Dynamic-key calls
and `**spread` sites are skipped. Sites with a non-literal second argument
are skipped. No t() behavior change in this item. Gate for items 21-24.
"""
import ast
import string
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


def placeholders(s):
    return {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}


def _exec_assign(src_text, target_name, filename):
    ns = {}
    for node in ast.parse(src_text).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(tg, ast.Name) and tg.id == target_name for tg in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), filename, 'exec'), ns)
    return ns[target_name]


def en_packs():
    ns = {}
    exec(compile(ast.parse((ROOT / 'zap_i18n.py').read_text()), 'zap_i18n.py', 'exec'), ns)
    i18n = ns['TRANSLATIONS']['en']
    p2p = _exec_assign((ROOT / 'zap_p2p.py').read_text(), 'TXT', 'zap_p2p.py')['en']
    return i18n, p2p


# kwargs currently passed although the pack string declares no such
# placeholder; format() ignores them. Pinned so NEW extras fail loudly.
KNOWN_EXTRA_KWARGS = {
    'token_chain_pick': frozenset({'asset'}),
    'token_wd_confirm': frozenset({'fee', 'net', 'sats', 'svc_fee', 'svc_sats'}),
    'token_recv_ready': frozenset({'fee', 'fee_sats'}),
    'token_wd_amount': frozenset({'asset'}),
}


def audit(source_path, func_name, en_pack):
    """Yield (lineno, key, problem) for every resolvable call site."""
    tree = ast.parse(source_path.read_text())
    # credit chained `.format(...)` kwargs: map inner call id -> outer kwargs
    chained = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'format' and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == func_name):
            chained[id(node.func.value)] = {kw.arg for kw in node.keywords if kw.arg}
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == func_name):
            continue
        if len(node.args) != 2:
            problems.append((node.lineno, '<shape>',
                             f'expected (uid, key) positional args, got {len(node.args)}'))
            continue
        key_node = node.args[1]
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue  # dynamic key: unresolvable statically
        key = key_node.value
        if not isinstance(en_pack.get(key), str):
            problems.append((node.lineno, key, 'key missing from EN pack'))
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue  # **spread: kwargs unknowable statically
        given_direct = {kw.arg for kw in node.keywords}
        given_all = given_direct | chained.get(id(node), set())
        declared = placeholders(en_pack[key])
        if declared - given_all:
            problems.append((node.lineno, key, f'missing kwargs {sorted(declared - given_all)}'))
        # extras: direct kwargs only — a chained `.format(extra=...)` on the
        # returned string (e.g. lock_yes's dead `amt=`) is outside T()'s
        # contract and tolerated.
        extras = given_direct - declared
        if extras - KNOWN_EXTRA_KWARGS.get(key, frozenset()):
            problems.append((node.lineno, key, f'unexpected kwargs {sorted(extras)}'))
    return problems


class CallSiteAudit(unittest.TestCase):
    def test_t_callsites_pass_declared_placeholders(self):
        i18n_en, _ = en_packs()
        problems = audit(ROOT / 'zap_wallet_bot_v32.py', 't', i18n_en)
        self.assertEqual(problems, [])

    def test_T_callsites_pass_declared_placeholders(self):
        _, p2p_en = en_packs()
        problems = audit(ROOT / 'zap_p2p.py', 'T', p2p_en)
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
