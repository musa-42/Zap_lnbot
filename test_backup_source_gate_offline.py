"""Item 6: BACKUP_INCLUDE_SOURCE gate — offline.

Default ON: backup_to_telegram keeps sending everything it sends today
(databases + source files). BACKUP_INCLUDE_SOURCE=0 stops only the source
sends; database backups are never gated.
"""
import ast
import asyncio
import copy
import logging
import os
from pathlib import Path
import tempfile
from datetime import datetime
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
TREE = ast.parse(SOURCE.read_text())


class TestBackupSourceGate(unittest.IsolatedAsyncioTestCase):
    def _gate_ast(self):
        for node in TREE.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'BACKUP_INCLUDE_SOURCE':
                return node.value
        return None

    def test_gate_defaults_on(self):
        value = self._gate_ast()
        self.assertIsNotNone(value)
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(eval(compile(ast.Expression(value), str(SOURCE), 'eval'), {'os': os}))
        with patch.dict(os.environ, {'BACKUP_INCLUDE_SOURCE': '0'}, clear=True):
            self.assertFalse(eval(compile(ast.Expression(value), str(SOURCE), 'eval'), {'os': os}))
        with patch.dict(os.environ, {'BACKUP_INCLUDE_SOURCE': '1'}, clear=True):
            self.assertTrue(eval(compile(ast.Expression(value), str(SOURCE), 'eval'), {'os': os}))

    async def _run_one_backup_pass(self, include_source):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / 'db.sqlite').write_bytes(b'db')
        (d / 'p2p.db').write_bytes(b'p2p')
        for name in ('zap_p2p.py', 'zap_i18n.py', 'zap_tokens.py'):
            (d / name).write_text('# src')
        main_src = d / 'zap_wallet_bot_v32.py'
        main_src.write_text('# main')

        sent = []

        class FakeClient:
            async def send_file(self, chat, path, caption=None):
                sent.append(os.path.basename(path))

        sleeps = {'n': 0}

        async def fake_sleep(secs):
            sleeps['n'] += 1
            if sleeps['n'] == 1:      # initial 60s wait
                return
            await asyncio.Event().wait()   # interval wait: block forever

        def snap(src, dst):
            Path(dst).write_bytes(b'snap')

        node = copy.deepcopy(next(n for n in TREE.body
                                  if isinstance(n, ast.AsyncFunctionDef) and n.name == 'backup_to_telegram'))
        ns = {'asyncio': type('A', (), {'sleep': staticmethod(fake_sleep)}),
              'os': os, 'logging': logging, 'datetime': datetime,
              'DB_PATH': str(d / 'db.sqlite'), 'SOURCE_FILE_PATH': str(main_src),
              '_snapshot_sqlite': snap, 'client': FakeClient(),
              'BACKUP_CHANNEL_ID': -100, 'BACKUP_INTERVAL_MINUTES': 30,
              'BACKUP_INCLUDE_SOURCE': include_source}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
        task = asyncio.create_task(ns['backup_to_telegram']())
        for _ in range(500):
            if sleeps['n'] >= 2:
                break
            await asyncio.sleep(0.01)
        self.assertGreaterEqual(sleeps['n'], 2, 'backup pass never reached the interval wait')
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return sent

    async def test_default_on_sends_databases_and_all_sources(self):
        sent = await self._run_one_backup_pass(include_source=True)
        self.assertIn('zap_wallet_bot_v32.py', sent)
        self.assertIn('zap_p2p.py', sent)
        self.assertIn('zap_i18n.py', sent)
        self.assertIn('zap_tokens.py', sent)
        self.assertEqual(len([s for s in sent if s.startswith('db-bot-backup')]), 1)
        self.assertEqual(len([s for s in sent if s.startswith('p2p-db-backup')]), 1)

    async def test_gate_off_stops_only_source_sends(self):
        sent = await self._run_one_backup_pass(include_source=False)
        self.assertEqual(len([s for s in sent if s.startswith('db-bot-backup')]), 1)
        self.assertEqual(len([s for s in sent if s.startswith('p2p-db-backup')]), 1)
        self.assertEqual([s for s in sent if s.endswith('.py')], [])


if __name__ == '__main__':
    unittest.main()
