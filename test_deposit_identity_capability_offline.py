"""Offline SDK capability check: receive quotes cannot identify later receipts."""
import ast
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DepositIdentityCapability(unittest.TestCase):
    def test_receive_quote_has_no_provider_order_or_quote_identity(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HOME': home}, clear=True), \
                patch.object(socket.socket, 'connect', side_effect=AssertionError('network blocked')), \
                patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network blocked')):
            import breez_sdk_spark as sdk
            quote_fields = set(sdk.CrossChainReceiveInfo.__annotations__)
            response_fields = set(sdk.ReceivePaymentResponse.__annotations__)
            self.assertFalse({'order_id', 'quote_id', 'provider_context'} & quote_fields)
            self.assertFalse({'order_id', 'quote_id', 'provider_context'} & response_fields)
            self.assertIn('deposit_address', quote_fields)
            import inspect
            tree = ast.parse(inspect.getsource(sdk.ConversionInfo))
            info = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ConversionInfo')
            orchestra = next(n for n in info.body if isinstance(n, ast.ClassDef) and n.name == 'ORCHESTRA')
            fields = {n.target.id for n in orchestra.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            self.assertTrue({'order_id', 'quote_id', 'recipient_address', 'asset_amount_in'} <= fields)
            self.assertNotIn('deposit_address', fields)

    def test_pauses_and_gates_remain_in_place(self):
        source = Path(__file__).with_name('zap_wallet_bot_v32.py').read_text(encoding='utf-8')
        # Deposit-fee tracking stays disabled (SDK cannot bind deposit identity);
        # group /zap was reverted to the classic flow (owner order), so the
        # durable-claim gate assertions no longer apply.
        self.assertIn("await event.answer(t(user_id, 'deposit_tracking_paused'), alert=True)", source)
        self.assertIn('pending_zap_confirmations', source)
        self.assertNotIn("await event.reply(t(event.sender_id, 'financial_paused'))", source)


if __name__ == '__main__':
    unittest.main()
