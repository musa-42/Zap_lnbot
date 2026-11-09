"""Offline regression tests for missing proof and disabled fee movement."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_escrow_safety as base

p = base.p


class FailClosed(base.Safety):
    async def test_failed_scan_and_legacy_observations_never_settle(self):
        for verified, version in ((0, 0), (1, 1)):
            for stage, column, amount in ((p.ST_PAYOUT, 'payout_id', 1000),
                                          (p.ST_REFUND, 'refund_id', 1005)):
                pid = f'{verified}-{stage}'
                tid = self.trade(stage, escrow_verified=verified, safety_version=version, **{column: pid})
                p.record_escrow_event('SEND', amount, pid, 'COMPLETED')
                await p.startup_scan()
                await p.on_escrow_payment('SEND', amount, pid, 'COMPLETED')
                self.assertEqual(p.get_trade(tid)['stage'], stage)
                self.assertIsNone(p._complete_escrow_trade(tid, stage == p.ST_PAYOUT))

    async def test_empty_scan_is_not_completeness_proof(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='s')
        p.record_escrow_event('SEND', 1000, 's', 'COMPLETED')
        sdk = SimpleNamespace(list_payments=AsyncMock(return_value=SimpleNamespace(payments=[])))
        bot = SimpleNamespace(client=None, ListPaymentsRequest=lambda **kw: kw)
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=sdk):
            await p.startup_scan()
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_fee_does_not_call_wallet_or_change_state(self):
        self.patches[-1].stop()
        try:
            tid = self.trade(p.ST_DONE, fee_safe=1)
            with patch.object(p, 'admin_id', return_value=123):
                await p.sweep_fee_to_owner(p.get_trade(tid))
            self.assertEqual(p.get_trade(tid)['fee_paid'], 0)
            self.assertEqual(p.get_trade(tid)['fee_tries'], 0)
            p.escrow_sdk.assert_not_awaited()
        finally:
            self.patches[-1].start()

    async def test_replay_continues_and_outbox_retries(self):
        p.record_escrow_event('RECEIVE', 1, 'receipt', 'COMPLETED')
        for pid in ('a', 'b'):
            self.trade(p.ST_PAYOUT, payout_id=pid)
            p.record_escrow_event('SEND', 1000, pid, 'COMPLETED')
        calls = []
        async def reconcile(pid):
            calls.append(pid)
            if pid == 'a':
                raise RuntimeError('temporary failure')
        with patch.object(p, 'reconcile_escrow_send', side_effect=reconcile):
            p.notify.return_value = object()
            await p.replay_escrow_journal()
        self.assertEqual(calls, ['a', 'b'])
        self.assertIsNotNone(p._one('SELECT sent_at FROM escrow_alerts')['sent_at'])

    async def test_lock_malformed_completed_status_is_not_verified(self):
        tid = self.trade(p.ST_LOCKING, escrow_verified=0)
        sdk = SimpleNamespace(prepare_send_payment=AsyncMock(return_value='prepared'),
            send_payment=AsyncMock(return_value=SimpleNamespace(payment=SimpleNamespace(status='NOT_COMPLETED', id='x'))))
        bot = SimpleNamespace(client=None, get_wallet=AsyncMock(return_value=sdk),
            get_balance=AsyncMock(return_value=10000), PrepareSendPaymentRequest=lambda **kw: kw,
            PaymentRequest=SimpleNamespace(INPUT=lambda **kw: kw), SendPaymentRequest=lambda **kw: kw)
        async def direct(coro):
            return await coro
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'offloop', side_effect=direct), \
             patch.object(p, 'escrow_address', new_callable=AsyncMock, return_value='escrow'):
            await p.do_lock(None, 20, tid)
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(tid)['escrow_verified'], 0)
