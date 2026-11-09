"""Offline tests for explicit evidence-bound administrative recovery."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_escrow_safety as base
p = base.p


class AdminAdjudication(base.Safety):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.admin = patch.object(p, 'is_p2p_admin', return_value=True)
        self.admin.start()

    async def asyncTearDown(self):
        self.admin.stop()
        await super().asyncTearDown()

    async def test_payout_and_refund_evidence_and_idempotency(self):
        for stage, column, amount, outcome, kind in ((p.ST_PAYOUT,'payout_id',1000,p.ST_DONE,'payout'),(p.ST_REFUND,'refund_id',1005,p.ST_REFUNDED,'refund')):
            tid = self.trade(stage, **{column: kind+'-id'})
            p.record_escrow_event('SEND',amount,kind+'-id','COMPLETED')
            self.assertEqual(p.admin_adjudicate(77,tid,kind), 'settled')
            self.assertEqual(p.admin_adjudicate(77,tid,kind), 'already settled')
            self.assertEqual(p.get_trade(tid)['stage'],outcome)
            self.assertEqual(len(p._rows('SELECT * FROM escrow_adjudications WHERE trade_id=?',(tid,))),1)

    async def test_mismatch_and_conflict_held(self):
        for n, direction, amount, pid in ((1,'RECEIVE',1000,'id1'),(2,'SEND',999,'id2'),(3,'SEND',1000,'wrong')):
            tid = self.trade(p.ST_PAYOUT,payout_id=f'id{n}')
            p.record_escrow_event(direction,amount,pid,'COMPLETED')
            self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'insufficient evidence')
            self.assertEqual(p.get_trade(tid)['stage'],p.ST_PAYOUT)

    async def test_fee_claim_crash_and_recovery(self):
        tid=self.trade(p.ST_PAYOUT,payout_id='payout-fee-id')
        p.record_escrow_event('SEND',1000,'payout-fee-id','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'settled')
        with patch.object(p,'admin_id',return_value=77):
            await p.admin_send_fee(None,77,tid)
            await p.admin_send_fee(None,77,tid)
        self.assertEqual(self.mock_send.await_count,1)
        self.assertEqual(p.get_trade(tid)['fee_paid'],0)
        self.assertEqual(p.admin_adjudicate(77,tid,'fee'),'insufficient evidence')
        p.record_escrow_event('SEND',5,'send-id','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'fee'),'settled')
        self.assertEqual(p.admin_adjudicate(77,tid,'fee'),'already settled')
        self.assertEqual(p.get_trade(tid)['fee_paid'],1)

    async def test_fee_crash_after_claim_never_resends(self):
        tid=self.trade(p.ST_PAYOUT,payout_id='crash-payout')
        p.record_escrow_event('SEND',1000,'crash-payout','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'settled')
        with patch.object(p,'admin_id',return_value=77), patch.object(p,'send_sats_from_escrow',new_callable=AsyncMock,side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await p.admin_send_fee(None,77,tid)
        p._db.close(); p._db = None
        p.init_db()
        self.assertEqual(await p.admin_send_fee(None,77,tid),'held or already attempted')
        self.assertEqual(p.admin_adjudicate(77,tid,'fee'),'insufficient evidence')
        self.mock_send.assert_not_awaited()

    async def test_conflict_reused_id_and_concurrent_adjudication(self):
        tid = self.trade(p.ST_PAYOUT,payout_id='conflict')
        p.record_escrow_event('SEND',1000,'conflict','COMPLETED')
        p.record_escrow_event('SEND',999,'conflict','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'insufficient evidence')
        tid2 = self.trade(p.ST_PAYOUT,payout_id='reuse')
        self.trade(p.ST_REFUND,refund_id='reuse')
        p.record_escrow_event('SEND',1000,'reuse','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid2,'payout'),'insufficient evidence')
        tid3 = self.trade(p.ST_PAYOUT,payout_id='race')
        p.record_escrow_event('SEND',1000,'race','COMPLETED')
        results = await asyncio.gather(asyncio.to_thread(p.admin_adjudicate,77,tid3,'payout'), asyncio.to_thread(p.admin_adjudicate,77,tid3,'payout'))
        self.assertCountEqual(results,['settled','already settled'])
        self.assertEqual(len(p._rows('SELECT * FROM escrow_adjudications WHERE trade_id=?',(tid3,))),1)

    async def test_callback_legacy_ack_and_admin_allowlist(self):
        tid=self.trade(p.ST_DISPUTED,safety_version=0)
        event=SimpleNamespace(sender_id=77,answer=AsyncMock(),edit=AsyncMock(),respond=AsyncMock())
        with patch.dict(p.os.environ,{'P2P_TEST_USERS':'invalid'}), patch.object(p, '_btn', side_effect=lambda text,data: (text,data)):
            await p.on_callback(None,event,f'p2p:admrel:{tid}')
            self.mock_send.assert_not_awaited()
            callback = event.edit.await_args.kwargs['buttons'][0][0][1]
            await p.on_callback(None,event,callback)
        self.assertEqual(self.mock_send.await_count,1)
        self.assertEqual(len(p._rows('SELECT * FROM escrow_admin_actions WHERE trade_id=?',(tid,))),1)

    async def test_fee_evidence_mismatch_and_parallel_claim(self):
        tid=self.trade(p.ST_PAYOUT,payout_id='parallel-payout')
        p.record_escrow_event('SEND',1000,'parallel-payout','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'settled')
        with patch.object(p,'admin_id',return_value=77):
            await asyncio.gather(p.admin_send_fee(None,77,tid),p.admin_send_fee(None,77,tid))
        self.assertEqual(self.mock_send.await_count,1)
        p.record_escrow_event('RECEIVE',5,'send-id','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'fee'),'insufficient evidence')
        self.assertEqual(p.get_trade(tid)['fee_paid'],0)

    async def test_fee_recovery_after_id_bound(self):
        tid=self.trade(p.ST_PAYOUT,payout_id='recovery-payout')
        p.record_escrow_event('SEND',1000,'recovery-payout','COMPLETED')
        self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'settled')
        with patch.object(p,'admin_id',return_value=77):
            await p.admin_send_fee(None,77,tid)
        p.record_escrow_event('SEND',5,'send-id','COMPLETED')
        p._db.close(); p._db = None
        p.init_db()
        self.assertEqual(p.admin_adjudicate(77,tid,'fee'),'settled')
        self.assertEqual(p.get_trade(tid)['fee_paid'],1)

    async def test_fee_blocked_without_adjudicated_payout(self):
        tid=self.trade(p.ST_DONE)
        self.assertEqual(await p.admin_send_fee(None,77,tid),'held or already attempted')
        self.mock_send.assert_not_awaited()

    async def test_legacy_gate(self):
        for func in (p.admin_release,p.admin_refund):
            tid=self.trade(p.ST_DISPUTED,safety_version=0)
            await func(None,tid)
            self.assertEqual(p.get_trade(tid)['stage'],p.ST_DISPUTED)
        self.mock_send.assert_not_awaited()

    async def test_holds_and_allowlist_callback(self):
        tid=self.trade(p.ST_DONE,fee_tries=0,fee_paid=0)
        event=SimpleNamespace(sender_id=77,answer=AsyncMock(),edit=AsyncMock())
        with patch.dict(p.os.environ,{'P2P_TEST_USERS':'invalid'}), patch.object(p,'_btn',side_effect=lambda text,data: (text,data)):
            await p.on_callback(None,event,'p2p:admholds:0')
        buttons=event.edit.await_args.kwargs['buttons']
        self.assertTrue(any(f'p2p:admhold:{tid}' == item[1] for row in buttons for item in row))
