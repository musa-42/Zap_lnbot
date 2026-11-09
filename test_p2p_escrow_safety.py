"""Offline financial-safety regression tests; no production DB, SDK or network."""
import asyncio
import importlib.util
import os
import tempfile
import unittest
import sys
import types
import socket
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

# Isolate before importing even the P2P module; never load the real SDK or bot.
_sandbox = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
os.environ['HOME'] = _sandbox.name
os.environ['TMPDIR'] = _sandbox.name
sdk_stub = types.ModuleType('breez_sdk_spark')
sdk_stub.EventListener = type('EventListener', (), {})
spec = importlib.util.spec_from_file_location('p2p_under_test', os.path.join(os.path.dirname(__file__), 'zap_p2p.py'))
p = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'breez_sdk_spark': sdk_stub}):
    spec.loader.exec_module(p)
p.DB_PATH = os.path.join(_sandbox.name, 'unused.db')
p.ESCROW_SEED_PATH = os.path.join(_sandbox.name, 'no-seed')
p.ESCROW_STORAGE_DIR = os.path.join(_sandbox.name, 'no-wallet')

class Safety(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        p.DB_PATH = os.path.join(self.temp.name, 'test.db')
        p._db = None
        p.init_db()
        p._mem_wiz.clear()
        self.patches = []
        self.network_block = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden in P2P tests'))
        self.network_block.start()
        for name in ('notify', 'audit', 'update_room', 'reply_to_room', 'channel_status', 'channel_repost', 'admin_case', 'sweep_fee_to_owner'):
            mock = patch.object(p, name, new_callable=AsyncMock)
            self.patches.append(mock); mock.start()
        self.bot = patch.object(p, '_bot', return_value=SimpleNamespace(client=None))
        self.bot.start()
        self.sdk_block = patch.object(p, 'escrow_sdk', new_callable=AsyncMock, side_effect=RuntimeError('offline SDK blocked'))
        self.sdk_block.start()
        self.address = patch.object(p, 'spark_address_of', new_callable=AsyncMock, return_value='mock-address')
        self.address.start()
        self.send = patch.object(p, 'send_sats_from_escrow', new_callable=AsyncMock, return_value=('PENDING','send-id',None))
        self.mock_send = self.send.start()
        self.lang = patch.object(p, 'lang_of', return_value='en'); self.lang.start()
        self.btn = patch.object(p, '_btn', return_value='button'); self.btn.start()

    async def asyncTearDown(self):
        self.btn.stop(); self.lang.stop(); self.send.stop(); self.address.stop(); self.sdk_block.stop(); self.bot.stop()
        for x in self.patches: x.stop()
        self.network_block.stop()
        p._db.close(); p._db = None
        self.temp.cleanup()

    def trade(self, stage, **fields):
        order = p._exec("INSERT INTO orders (status,created) VALUES ('taken',0)").lastrowid
        vals = dict(order_id=order,buyer=10,seller=20,sats=1000,fee=5,stage=stage,created=1,updated=1,lock_started=p.time.time(),escrow_verified=1,safety_version=1)
        vals.update(fields)
        return p._exec('INSERT INTO trades ('+','.join(vals)+') VALUES ('+','.join('?' for _ in vals)+')', tuple(vals.values())).lastrowid

    async def test_same_amount_receive_never_credited_or_refunded(self):
        a = self.trade(p.ST_LOCKING,lock_sent=1)
        b = self.trade(p.ST_CANCELED,lock_sent=1)
        await p._handle_escrow_receive(1005,'unrelated')
        self.assertEqual(p.get_trade(a)['stage'],p.ST_LOCKING)
        self.assertEqual(p.get_trade(b)['stage'],p.ST_CANCELED)
        self.mock_send.assert_not_awaited()

    async def test_pending_receive_not_credited(self):
        a = self.trade(p.ST_LOCKING,lock_sent=1,lock_pay_id='same')
        await p.on_escrow_payment('RECEIVE',1005,'same','PENDING')
        self.assertEqual(p.get_trade(a)['stage'],p.ST_LOCKING)

    async def test_unauthorized_cancel_dispute_and_dispute_payout(self):
        a = self.trade(p.ST_LOCKED)
        await p.request_cancel(None, 99, a)
        await p.open_dispute(None, 99, a, 'evil')
        self.assertEqual(p.get_trade(a)['stage'],p.ST_LOCKED)
        b = self.trade(p.ST_PAYOUT)
        await p.open_dispute(None, 10, b, 'late')
        self.assertEqual(p.get_trade(b)['stage'],p.ST_PAYOUT)
        self.mock_send.assert_not_awaited()

    async def test_refund_pending_is_not_resent(self):
        a = self.trade(p.ST_LOCKED)
        await p.start_refund(None,a)
        await p.start_refund(None,a)
        await p.admin_refund(None,a)
        self.assertEqual(p.get_trade(a)['stage'],p.ST_REFUND)
        self.assertEqual(self.mock_send.await_count,1)

    async def test_payout_unknown_result_never_retried(self):
        a = self.trade(p.ST_FIAT)
        self.mock_send.return_value = (False,'','timeout')
        await p.do_release(None,20,a)
        await p.admin_release(None,a)
        self.assertEqual(p.get_trade(a)['stage'],p.ST_PAYOUT)
        self.assertEqual(self.mock_send.await_count,1)

    async def test_cancel_lock_sent_does_not_cancel(self):
        a = self.trade(p.ST_LOCKING,lock_sent=1)
        await p.request_cancel(None,10,a)
        self.assertEqual(p.get_trade(a)['stage'],p.ST_LOCKING)

    async def test_racing_payout_refund_has_one_send(self):
        a = self.trade(p.ST_FIAT,cancel_seller=1,cancel_buyer=1)
        await asyncio.gather(p.do_release(None,20,a), p.start_refund(None,a))
        self.assertEqual(self.mock_send.await_count,1)

    async def test_legacy_unverified_trades_are_held(self):
        for stage in (p.ST_LOCKED, p.ST_FIAT, p.ST_DISPUTED):
            a = self.trade(stage, escrow_verified=0)
            await p.do_fiat(None,10,a)
            await p.do_release(None,20,a)
            await p.start_refund(None,a)
            await p.admin_release(None,a)
            await p.admin_refund(None,a)
            self.assertEqual(p.get_trade(a)['stage'],stage)
        self.mock_send.assert_not_awaited()

    async def test_stray_exact_id_not_refunded(self):
        a = self.trade(p.ST_CANCELED,lock_sent=1,lock_pay_id='same')
        await p._handle_escrow_receive(1005,'same')
        self.assertEqual(p.get_trade(a)['stage'],p.ST_CANCELED)
        self.mock_send.assert_not_awaited()

    async def test_refund_unknown_without_id_is_not_resent(self):
        a = self.trade(p.ST_LOCKED)
        self.mock_send.return_value = (False,'','timeout')
        await p.start_refund(None,a)
        await p.start_refund(None,a)
        await p.admin_refund(None,a)
        self.assertEqual(self.mock_send.await_count,1)
        self.assertEqual(p.get_trade(a)['stage'],p.ST_REFUND)

    async def test_outgoing_helper_pending_is_not_true(self):
        self.send.stop()
        class SDK:
            async def prepare_send_payment(self, **kw): return None
            async def send_payment(self, **kw):
                return SimpleNamespace(payment=SimpleNamespace(id='id',status='PENDING'))
        bot=SimpleNamespace(PrepareSendPaymentRequest=lambda **kw: kw,
            PaymentRequest=SimpleNamespace(INPUT=lambda **kw: kw), SendPaymentRequest=lambda **kw: kw)
        async def direct(coro): return await coro
        try:
            with patch.object(p,'_bot',return_value=bot), patch.object(p,'offloop',side_effect=direct), patch.object(p,'escrow_sdk',new_callable=AsyncMock,return_value=SDK()):
                result = await p.send_sats_from_escrow('mock',100,'test')
            self.assertEqual(result,('PENDING','id',None))
        finally:
            self.send.start()

    async def test_locked_cancel_votes_reset_on_fiat(self):
        a=self.trade(p.ST_LOCKED,cancel_seller=1,updated=p.time.time())
        await p.do_fiat(None,10,a)
        self.assertEqual(p.get_trade(a)['cancel_seller'],0)
        await p.request_cancel(None,10,a)
        self.assertEqual(p.get_trade(a)['stage'],p.ST_FIAT)
        self.mock_send.assert_not_awaited()

    async def test_stale_timeout_cannot_refund_fiat_stage(self):
        a=self.trade(p.ST_FIAT,updated=1)
        await p.start_refund(None,a,expected_stage=p.ST_LOCKED,timeout_before=p.time.time())
        self.assertEqual(p.get_trade(a)['stage'],p.ST_FIAT)
        self.mock_send.assert_not_awaited()

    async def test_payout_and_refund_pending_not_completed(self):
        a=self.trade(p.ST_FIAT)
        await p.do_release(None,20,a)
        await p.on_escrow_payment('SEND',1000,'send-id','PENDING')
        self.assertEqual(p.get_trade(a)['stage'],p.ST_PAYOUT)
        b=self.trade(p.ST_LOCKED)
        await p.start_refund(None,b)
        await p.on_escrow_payment('SEND',1005,'send-id','PENDING')
        self.assertEqual(p.get_trade(b)['stage'],p.ST_REFUND)

    async def test_token_is_not_sats(self):
        self.assertFalse(p._payment_is_sats(SimpleNamespace(method='TOKEN', details=None)))
        self.assertFalse(p._payment_is_sats(SimpleNamespace(method=None, details=None)))
        self.assertTrue(p._payment_is_sats(SimpleNamespace(method='PaymentMethod.SPARK', details=None)))

    async def test_empty_send_id_is_never_reconciled(self):
        a=self.trade(p.ST_PAYOUT,payout_id='')
        await p.on_escrow_payment('SEND',1000,'','COMPLETED')
        self.assertEqual(p.get_trade(a)['stage'],p.ST_PAYOUT)

    async def test_legacy_schema_migrates_with_conservative_defaults(self):
        a=self.trade(p.ST_LOCKED)
        p._db.execute('ALTER TABLE trades DROP COLUMN escrow_verified')
        p._db.execute('ALTER TABLE trades DROP COLUMN fee_safe')
        p.init_db()
        row=p.get_trade(a)
        self.assertEqual(row['escrow_verified'],0)
        self.assertEqual(row['fee_safe'],0)

    async def test_completed_lock_verifies_own_send(self):
        a=self.trade(p.ST_LOCKING,escrow_verified=0)
        class SDK:
            async def prepare_send_payment(self, **kw): return 'prep'
            async def send_payment(self, **kw):
                return SimpleNamespace(payment=SimpleNamespace(status='COMPLETED',id='seller-payment'))
        bot=SimpleNamespace(client=None,get_wallet=AsyncMock(return_value=SDK()),get_balance=AsyncMock(return_value=10000),
            PrepareSendPaymentRequest=lambda **kw: kw,PaymentRequest=SimpleNamespace(INPUT=lambda **kw: kw),SendPaymentRequest=lambda **kw: kw)
        async def direct(coro): return await coro
        with patch.object(p,'_bot',return_value=bot), patch.object(p,'offloop',side_effect=direct), patch.object(p,'escrow_address',new_callable=AsyncMock,return_value='escrow'):
            await p.do_lock(None,20,a)
            await asyncio.sleep(0)
        row=p.get_trade(a)
        self.assertEqual(row['stage'],p.ST_LOCKED)
        self.assertEqual(row['escrow_verified'],1)
        self.assertEqual(row['lock_pay_id'],'seller-payment')

    async def test_fee_send_is_disabled(self):
        self.patches[-1].stop()
        calls=0
        class SDK:
            async def receive_payment(self, **kw): return SimpleNamespace(payment_request='owner')
            async def prepare_send_payment(self, **kw): return None
            async def send_payment(self, **kw):
                nonlocal calls
                calls += 1
                return SimpleNamespace(payment=SimpleNamespace(status='PENDING'))
        sdk=SDK()
        bot=SimpleNamespace(client=None, get_wallet=AsyncMock(return_value=sdk),
            ReceivePaymentRequest=lambda **kw: kw, ReceivePaymentMethod=SimpleNamespace(SPARK_ADDRESS=lambda: None),
            PrepareSendPaymentRequest=lambda **kw: kw, PaymentRequest=SimpleNamespace(INPUT=lambda **kw: kw),
            SendPaymentRequest=lambda **kw: kw)
        async def direct(coro): return await coro
        a=self.trade(p.ST_DONE,fee_safe=1)
        try:
            with patch.object(p,'_bot',return_value=bot), patch.object(p,'admin_id',return_value=123), patch.object(p,'offloop',side_effect=direct), patch.object(p,'escrow_sdk',new_callable=AsyncMock,return_value=sdk):
                await p.sweep_fee_to_owner(p.get_trade(a))
                await p.sweep_fee_to_owner(p.get_trade(a))
            self.assertEqual(calls,0)
            self.assertEqual(p.get_trade(a)['fee_paid'],0)
            self.assertEqual(p.get_trade(a)['fee_tries'],0)
        finally:
            self.patches[-1].start()

    async def test_duplicate_lock_is_claimed_once_and_pending_not_locked(self):
        a = self.trade(p.ST_LOCKING)
        started = asyncio.Event(); release = asyncio.Event()
        sdk_calls = 0
        class SDK:
            async def prepare_send_payment(self, **kw): return 'prepared'
            async def send_payment(self, **kw):
                nonlocal sdk_calls
                sdk_calls += 1
                started.set(); await release.wait()
                return SimpleNamespace(payment=SimpleNamespace(status='PENDING',id='sender-id'))
        sdk=SDK()
        bot=SimpleNamespace(get_wallet=AsyncMock(return_value=sdk), get_balance=AsyncMock(return_value=10000),
            PrepareSendPaymentRequest=lambda **kw: kw, PaymentRequest=SimpleNamespace(INPUT=lambda **kw: kw), SendPaymentRequest=lambda **kw: kw)
        async def direct(coro): return await coro
        with patch.object(p,'_bot',return_value=bot), patch.object(p,'offloop',side_effect=direct), patch.object(p,'escrow_address',new_callable=AsyncMock,return_value='escrow'):
            first=asyncio.create_task(p.do_lock(None,20,a)); await started.wait()
            second=asyncio.create_task(p.do_lock(None,20,a)); await asyncio.sleep(0)
            release.set(); await asyncio.gather(first, second)
            self.assertEqual(sdk_calls, 1)
        self.assertEqual(p.get_trade(a)['stage'],p.ST_LOCKING)
        self.assertEqual(p.get_trade(a)['lock_sent'],1)
        self.assertEqual(p.get_trade(a)['lock_pay_id'],'sender-id')

if __name__ == '__main__': unittest.main()
