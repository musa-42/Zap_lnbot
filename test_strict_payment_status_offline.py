"""Offline regression tests: extract the pure status helper, never load the bot."""
import ast
import copy
from pathlib import Path
import os
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from breez_sdk_spark import PaymentStatus

SOURCE = Path(__file__).with_name('zap_wallet_bot_v32.py')
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
node = copy.deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                          and n.name == 'is_payment_successful'))
namespace = {'PaymentStatus': PaymentStatus}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
classify = namespace['is_payment_successful']


class StrictStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'HOME': self.tmp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.net = patch.object(socket.socket, 'connect', side_effect=AssertionError('network blocked'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.net_ex = patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network blocked'))
        self.net_ex.start()
        self.addCleanup(self.net_ex.stop)

    def test_completed_only(self):
        self.assertEqual(classify(SimpleNamespace(status=PaymentStatus.COMPLETED)), (True, None))
        for status in (PaymentStatus.PENDING, PaymentStatus.FAILED, None, 'COMPLETED',
                       'PENDING', 'INCOMPLETE', 'NOT_COMPLETED', 'SUCCEEDED'):
            with self.subTest(status=status):
                self.assertFalse(classify(SimpleNamespace(status=status))[0])
        self.assertFalse(classify(None)[0])

    def test_pending_unknown_and_failed_are_distinct(self):
        pending = classify(SimpleNamespace(status=PaymentStatus.PENDING))[1]
        failed = classify(SimpleNamespace(status=PaymentStatus.FAILED))[1]
        unknown = classify(SimpleNamespace(status=None))[1]
        self.assertEqual((pending, failed, unknown), ('pending', 'failed', 'unknown'))

    def test_conflicting_htlc_failure_does_not_count_as_success(self):
        payment = SimpleNamespace(status=PaymentStatus.COMPLETED,
                                  details=SimpleNamespace(htlc_details=SimpleNamespace(status='RETURNED')))
        self.assertEqual(classify(payment), (False, 'unknown'))


if __name__ == '__main__':
    unittest.main()
