"""Offline regressions for adversarial escrow review; uses the isolated fixture."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_admin_adjudication as base
p = base.p
REAL_SEND = p.send_sats_from_escrow

class AdversarialRegression(base.AdminAdjudication):
    async def test_legacy_user_paths_never_send(self):
        for action in ('release', 'cancel'):
            tid = self.trade(p.ST_FIAT, safety_version=0)
            if action == 'release':
                await p.do_release(None, 20, tid)
            else:
                await p.request_cancel(None, 10, tid)
                await p.request_cancel(None, 20, tid)
            self.assertEqual(p.get_trade(tid)['stage'], p.ST_FIAT)
        self.mock_send.assert_not_awaited()

    async def test_conflict_invalidation_and_send_race(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='race-proof')
        p.record_escrow_event('SEND', 1000, 'race-proof', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'settled')
        async def conflict(_):
            p.record_escrow_event('SEND', 999, 'race-proof', 'COMPLETED')
            return 'address'
        with patch.object(p, 'admin_id', return_value=77), patch.object(p, 'spark_address_of', side_effect=conflict):
            await p.admin_send_fee(None, 77, tid)
        self.assertEqual(p.get_trade(tid)['stage'], 'invalidated')
        self.mock_send.assert_not_awaited()

    async def test_conflict_during_fee_sdk_preparation_holds_without_send(self):
        tid = self.trade(p.ST_DONE, payout_id='prepared-race')
        p.record_escrow_event('SEND', 1000, 'prepared-race', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'settled')
        entered, proceed, sent = threading.Event(), threading.Event(), []
        class SDK:
            async def prepare_send_payment(self, request):
                entered.set()
                if not proceed.wait(8):
                    raise RuntimeError('test barrier timeout')
                return object()
            async def send_payment(self, request):
                sent.append(True)
                return SimpleNamespace(payment=SimpleNamespace(id='fee-id', status='PENDING'))
        class Request:
            def __init__(self, **kw): pass
        bot = SimpleNamespace(PaymentRequest=SimpleNamespace(INPUT=lambda **kw: object()),
                              PrepareSendPaymentRequest=Request, SendPaymentRequest=Request)
        with patch.object(p, 'admin_id', return_value=77), patch.object(p, '_bot', return_value=bot), \
             patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=SDK()), \
             patch.object(p, 'send_sats_from_escrow', REAL_SEND):
            task = asyncio.create_task(p.admin_send_fee(None, 77, tid))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                p.record_escrow_event('SEND', 999, 'prepared-race', 'COMPLETED')
                self.assertEqual(p.get_trade(tid)['stage'], p.ST_INVALIDATED)
            finally:
                proceed.set()
            result = await task
        self.assertEqual(sent, [])
        self.assertIn('held', result)
        self.assertEqual(p.get_trade(tid)['fee_tries'], 1)
        self.assertIsNone(p._one('SELECT payment_id FROM escrow_fee_attempts WHERE trade_id=?', (tid,))['payment_id'])
        self.assertEqual(len(p._rows("SELECT * FROM escrow_trade_notices WHERE trade_id=? AND kind='invalidated'", (tid,))), 5)

    async def test_fee_conflict_before_binding_invalidates_and_queues_notices(self):
        tid = self.trade(p.ST_DONE, payout_id='early-payout')
        p.record_escrow_event('SEND', 1000, 'early-payout', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'settled')
        async def send(*args, **kwargs):
            p.record_escrow_event('SEND', 5, 'early-fee', 'COMPLETED')
            p.record_escrow_event('SEND', 6, 'early-fee', 'COMPLETED')
            return ('PENDING', 'early-fee', None)
        with patch.object(p, 'admin_id', return_value=77), patch.object(p, 'send_sats_from_escrow', side_effect=send):
            result = await p.admin_send_fee(None, 77, tid)
        self.assertIn('held', result)
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_INVALIDATED)
        self.assertEqual(p.get_trade(tid)['fee_paid'], 0)
        self.assertEqual(p._one('SELECT payment_id FROM escrow_fee_attempts WHERE trade_id=?', (tid,))['payment_id'], 'early-fee')
        self.assertEqual(len(p._rows("SELECT * FROM escrow_trade_notices WHERE trade_id=? AND kind='invalidated'", (tid,))), 5)
        self.assertEqual(p.admin_adjudicate(77, tid, 'fee'), 'insufficient evidence')

    async def test_done_recovery_and_hold_completeness(self):
        legacy = self.trade(p.ST_DISPUTED, safety_version=0, escrow_verified=1)
        done = self.trade(p.ST_DONE, payout_id='old-done')
        p.record_escrow_event('SEND', 1000, 'old-done', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, done, 'payout'), 'settled')
        p.record_escrow_event('SEND', 999, 'old-done', 'COMPLETED')
        event = SimpleNamespace(sender_id=77, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.object(p, '_btn', side_effect=lambda text,data: (text,data)):
            await p.on_callback(None,event,'p2p:admholds:0')
        found = [item[1] for row in event.edit.await_args.kwargs['buttons'] for item in row]
        self.assertIn(f'p2p:admhold:{legacy}', found)
        self.assertIn(f'p2p:admhold:{done}', found)

    async def test_token_actor_action_and_replay(self):
        tid = self.trade(p.ST_DISPUTED, safety_version=0)
        event = SimpleNamespace(sender_id=77, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.object(p, '_btn', side_effect=lambda text,data: (text,data)):
            await p.on_callback(None, event, f'p2p:admrel:{tid}')
            callback = event.edit.await_args.kwargs['buttons'][0][0][1]
            event.sender_id = 78
            await p.on_callback(None, event, callback)
            event.sender_id = 77
            await p.on_callback(None, event, callback.replace(':release:', ':refund:'))
            self.mock_send.assert_not_awaited()
            await p.on_callback(None, event, callback)
            await p.on_callback(None, event, callback)
        self.assertEqual(self.mock_send.await_count, 1)

    async def test_fee_callback_nonce_bound_and_one_use(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='fee-nonce')
        p.record_escrow_event('SEND', 1000, 'fee-nonce', 'COMPLETED')
        p.admin_adjudicate(77, tid, 'payout')
        event = SimpleNamespace(sender_id=77, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.object(p, '_btn', side_effect=lambda text,data: (text,data)), patch.object(p, 'admin_id', return_value=77):
            await p.on_callback(None,event,f'p2p:feeconfirm:{tid}')
            callback = event.edit.await_args.kwargs['buttons'][0][0][1]
            event.sender_id = 78
            await p.on_callback(None,event,callback)
            self.mock_send.assert_not_awaited()
            event.sender_id = 77
            await p.on_callback(None,event,callback)
            await p.on_callback(None,event,callback)
        self.assertEqual(self.mock_send.await_count,1)

    async def test_notifications_and_conflict_replay(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='notify')
        p.record_escrow_event('SEND',1000,'notify','COMPLETED')
        event = SimpleNamespace(sender_id=77, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.object(p,'admin_id',return_value=77):
            await p.on_callback(None,event,f'p2p:adjudicate:payout:{tid}')
            self.assertEqual({call.args[1] for call in p.notify.await_args_list}, {10,20,77})
            self.assertTrue(p.update_room.await_count)
            self.assertTrue(p.channel_status.await_count)
            await p.on_escrow_payment('SEND',999,'notify','COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_INVALIDATED)
        self.assertEqual(len(p._rows("SELECT * FROM escrow_trade_notices WHERE trade_id=? AND kind='invalidated'",(tid,))), 5)

    async def test_late_conflict_old_done_and_failed_notice_retry(self):
        tid = self.trade(p.ST_DONE, payout_id='old-conflict', fee=0)
        p.record_escrow_event('SEND',1000,'old-conflict','COMPLETED')
        p.record_escrow_event('SEND',999,'old-conflict','COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'],p.ST_INVALIDATED)
        with patch.object(p,'admin_id',return_value=77), patch.object(p,'notify',new_callable=AsyncMock,return_value=None), patch.object(p,'update_room',new_callable=AsyncMock,return_value=False), patch.object(p,'channel_status',new_callable=AsyncMock,return_value=False):
            await p.deliver_trade_notices(None,tid)
        self.assertEqual(len(p._rows('SELECT * FROM escrow_trade_notices WHERE trade_id=? AND sent_at IS NULL',(tid,))),5)
        with patch.object(p,'admin_id',return_value=77), patch.object(p,'notify',new_callable=AsyncMock,return_value=True), patch.object(p,'update_room',new_callable=AsyncMock,return_value=True), patch.object(p,'channel_status',new_callable=AsyncMock,return_value=True):
            await p.deliver_trade_notices(None,tid)
        self.assertEqual(len(p._rows('SELECT * FROM escrow_trade_notices WHERE trade_id=? AND sent_at IS NULL',(tid,))),0)

    async def test_fee_blocks_legacy_and_missing_journal(self):
        for version, remove in ((0,False),(1,True)):
            tid = self.trade(p.ST_PAYOUT,payout_id='proof-'+str(version),safety_version=version)
            p.record_escrow_event('SEND',1000,'proof-'+str(version),'COMPLETED')
            self.assertEqual(p.admin_adjudicate(77,tid,'payout'),'settled')
            if remove:
                p._exec('DELETE FROM escrow_observations WHERE event_id=(SELECT id FROM escrow_events WHERE payment_id=?)',('proof-1',))
            with patch.object(p,'admin_id',return_value=77):
                await p.admin_send_fee(None,77,tid)
        self.mock_send.assert_not_awaited()

    async def test_existing_conflict_migration_is_idempotent(self):
        tid = self.trade(p.ST_DONE,payout_id='migration-conflict',fee=0)
        p.record_escrow_event('SEND',1000,'migration-conflict','COMPLETED')
        p._exec("UPDATE escrow_events SET conflict=1 WHERE payment_id='migration-conflict'")
        p._db.close(); p._db = None
        p.init_db(); p.init_db()
        self.assertEqual(p.get_trade(tid)['stage'],p.ST_INVALIDATED)
        self.assertEqual(len(p._rows('SELECT * FROM escrow_trade_notices WHERE trade_id=?',(tid,))),5)

    async def test_confirmation_expiry_and_translation_keys(self):
        tid = self.trade(p.ST_DISPUTED,safety_version=0)
        nonce = p.issue_escrow_confirmation(77,tid,'legacy_release')
        p._exec('UPDATE escrow_confirmations SET expires=0 WHERE nonce=?',(nonce,))
        self.assertFalse(p.consume_escrow_confirmation(77,tid,'legacy_release',nonce))
        for key in ('holds_title','event_detail','legacy_warning','confirm_fee_text','trade_notice_invalidated','result_insufficient_evidence'):
            for lang in ('fa','en'):
                self.assertIn(key,p.TXT[lang])

    async def test_direct_callbacks_rejected(self):
        legacy = self.trade(p.ST_DISPUTED, safety_version=0)
        done = self.trade(p.ST_PAYOUT, payout_id='direct')
        p.record_escrow_event('SEND', 1000, 'direct', 'COMPLETED')
        p.admin_adjudicate(77, done, 'payout')
        event = SimpleNamespace(sender_id=77, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        await p.on_callback(None,event,f'p2p:legacyack:release:{legacy}')
        await p.on_callback(None,event,f'p2p:feesend:{done}')
        self.mock_send.assert_not_awaited()
