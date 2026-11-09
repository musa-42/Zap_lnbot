"""Item 9: shared constants block + donate_user_id() helper — offline.

Pins: exact constant values, per-call env read for the owner id, and that no
bare 100_000_000 / '// 1000' / completion_timeout_secs=10 literals remain
outside the frozen zap/tip flow and the exec-pinned tip_custom_amount branch.
"""
import ast
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
SRC = SOURCE.read_text()
TREE = ast.parse(SRC)

FROZEN_FUNCS = {'tip_handler', 'process_zap_full', 'process_zap_payment', 'show_tip_prompt'}
FROZEN_BRANCHES = ('zap_cancel_', 'tip_amount_', 'send_quick_')
PINNED_BRANCHES = ("current_step == 'tip_custom_amount'",)


def extract(name, ns):
    node = copy.deepcopy(next(n for n in TREE.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns[name]


def find_path(node, line):
    start = getattr(node, 'lineno', None)
    end = getattr(node, 'end_lineno', None)
    if start is not None and not (start <= line <= end):
        return None
    best = [node] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.If)) else []
    for child in ast.iter_child_nodes(node):
        r = find_path(child, line)
        if r is not None:
            return best + r
    return best if best else None


def enclosing_unit(line):
    p = find_path(TREE, line) or []
    fn = next((n.name for n in p if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    branch = ast.unparse(p[-1].test) if p and isinstance(p[-1], ast.If) else None
    return fn, branch


def bare_literal_sites():
    lines = SRC.splitlines()
    definition_lines = set()
    for n in TREE.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id in ('SATS_PER_BTC', 'MSAT_PER_SAT', 'PAYMENT_COMPLETION_TIMEOUT_SECS'):
            definition_lines.add(n.lineno)
    sites = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Constant):
            if node.lineno in definition_lines:
                continue
            line = lines[node.lineno - 1]
            if node.value == 100_000_000:
                sites.append((node.lineno, 'SATS'))
            elif node.value == 1000 and '// 1000' in line:
                sites.append((node.lineno, 'MSAT'))
            elif node.value == 10 and 'completion_timeout_secs=10' in line:
                sites.append((node.lineno, 'TIMEOUT'))
    return sites


class TestSharedConstants(unittest.TestCase):
    def test_constants_exist_with_exact_values(self):
        assigns = {n.targets[0].id: n.value for n in TREE.body
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Name)
                   and n.targets[0].id in ('SATS_PER_BTC', 'MSAT_PER_SAT', 'PAYMENT_COMPLETION_TIMEOUT_SECS')}
        self.assertEqual({k: ast.literal_eval(v) for k, v in assigns.items()},
                         {'SATS_PER_BTC': 100_000_000, 'MSAT_PER_SAT': 1000,
                          'PAYMENT_COMPLETION_TIMEOUT_SECS': 10})

    def test_donate_user_id_reads_env_per_call(self):
        ns = {'os': os}
        fn = extract('donate_user_id', ns)
        with patch.dict(os.environ, {'DONATE_USER_ID': '77'}, clear=True):
            self.assertEqual(fn(), 77)
            self.assertEqual(fn(), 77)
        with patch.dict(os.environ, {'DONATE_USER_ID': 'garbage'}, clear=True):
            self.assertEqual(fn(), 0)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fn(), 0)

    def test_no_bare_literals_outside_frozen_code(self):
        offenders = []
        for lineno, kind in bare_literal_sites():
            fn, branch = enclosing_unit(lineno)
            frozen = (fn in FROZEN_FUNCS
                      or (branch and any(f"'{p}'" in branch for p in FROZEN_BRANCHES))
                      or (branch in PINNED_BRANCHES))
            if not frozen:
                offenders.append((lineno, kind, fn, branch))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
