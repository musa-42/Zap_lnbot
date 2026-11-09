"""Item 1: _admin_alert must exist as a top-level def and never raise — offline.

AST check that the def exists at module top level (previously a latent
NameError on the batch funds-hold paths), plus a behavioral test executing
the extracted function with a fake client.
"""
import ast
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())


def extract(name, ns):
    node = copy.deepcopy(next(n for n in TREE.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns[name]


class TestAdminAlert(unittest.IsolatedAsyncioTestCase):
    def _env(self, client):
        ns = {'os': os, 'logging': __import__('logging'), 'client': client}
        extract('donate_user_id', ns)   # _admin_alert resolves the owner per call
        return ns

    def test_top_level_def_exists(self):
        names = [n.name for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertIn('_admin_alert', names)

    async def test_admin_alert_dms_owner(self):
        client = SimpleNamespace(send_message=AsyncMock())
        fn = extract('_admin_alert', self._env(client))
        with patch.dict(os.environ, {'DONATE_USER_ID': '55'}, clear=True):
            await fn('held batch item')
        client.send_message.assert_awaited_once_with(55, 'held batch item')

    async def test_admin_alert_swallows_send_errors(self):
        client = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('boom')))
        fn = extract('_admin_alert', self._env(client))
        with patch.dict(os.environ, {'DONATE_USER_ID': '55'}, clear=True):
            await fn('again')  # must not raise

    async def test_admin_alert_no_owner_configured_is_silent(self):
        client = SimpleNamespace(send_message=AsyncMock())
        fn = extract('_admin_alert', self._env(client))
        with patch.dict(os.environ, {'DONATE_USER_ID': '0'}, clear=True):
            await fn('nobody home')
        client.send_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
