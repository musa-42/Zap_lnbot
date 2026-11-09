"""Item 5: FFIO credentials read from env — offline. The public copy ships
empty-string defaults; defaults must be byte-identical; env must win.
"""
import ast
import hashlib
import hmac
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())

CURRENT = {
    'FFIO_API_KEY': '',
    'FFIO_API_SECRET': '',
    'FFIO_REFCODE': '',
}


def seam_assignments():
    out = {}
    for node in TREE.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in CURRENT:
                out[node.targets[0].id] = node.value
    return out


def evaluate(value_node, env=None):
    if env is None:
        patcher = patch.dict(os.environ, {}, clear=True)
        env = patcher.start()
        try:
            return eval(compile(ast.Expression(value_node), str(SOURCE), 'eval'), {'os': os})
        finally:
            patcher.stop()
    with patch.dict(os.environ, env, clear=True):
        return eval(compile(ast.Expression(value_node), str(SOURCE), 'eval'), {'os': os})


class TestFFIOEnvSeam(unittest.TestCase):
    def test_credentials_use_getenv_with_current_fallback(self):
        assigns = seam_assignments()
        self.assertEqual(set(assigns), set(CURRENT))
        for name, value in assigns.items():
            self.assertIsInstance(value, ast.Call, name)
            self.assertEqual(ast.unparse(value.func), 'os.getenv', name)
            self.assertEqual(ast.literal_eval(value.args[0]), name)
            self.assertEqual(ast.literal_eval(value.args[1]), CURRENT[name], name)

    def test_defaults_are_byte_identical_when_env_unset(self):
        for name, value in seam_assignments().items():
            self.assertEqual(evaluate(value), CURRENT[name], name)

    def test_env_override_wins(self):
        env = {'FFIO_API_KEY': 'rotated-key', 'FFIO_API_SECRET': 'rotated-secret', 'FFIO_REFCODE': 'rr'}
        for name, value in seam_assignments().items():
            self.assertEqual(evaluate(value, env), env[name], name)

    def test_ffio_sign_uses_configured_secret(self):
        node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'ffio_sign')
        ns = {'hmac': hmac, 'hashlib': hashlib, 'FFIO_API_SECRET': 'rotated-secret'}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
        sig = ns['ffio_sign']('{"a":1}')
        expected = hmac.new(b'rotated-secret', b'{"a":1}', hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)


if __name__ == '__main__':
    unittest.main()
