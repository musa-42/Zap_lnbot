"""Offline recovery tests. SDK, database and wallet are isolated by base fixture."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_escrow_safety as base

p = base.p


class Recovery(base.Safety):
    async def test_receipts_survive_restart_and_deduplicate(self):
        a = self.trade(p.ST_LOCKING, lock_sent=1, escrow_verified=0)
        await p.on_escrow_payment('RECEIVE', 1005, 'r1', 'COMPLETED')
        await p.on_escrow_payment('RECEIVE', 1005, 'r1', 'COMPLETED')
        p._db.close(); p._db = None
        p.init_db()
        rows = p._rows('SELECT * FROM escrow_events')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payment_id'], 'r1')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        self.mock_send.assert_not_awaited()

    async def test_missing_id_and_conflicting_identity_fail_closed(self):
        await p.on_escrow_payment('RECEIVE', 1005, '', 'COMPLETED')
        await p.on_escrow_payment('RECEIVE', 1005, 'r1', 'COMPLETED')
        await p.on_escrow_payment('RECEIVE', 9999, 'r1', 'COMPLETED')
        self.assertEqual(p._one("SELECT conflict FROM escrow_events WHERE payment_id='r1'")['conflict'], 1)
        self.assertEqual(len(p._rows('SELECT * FROM escrow_events')), 2)
        self.assertEqual(len(p._rows("SELECT * FROM escrow_observations WHERE event_id=(SELECT id FROM escrow_events WHERE payment_id='r1')")), 2)
        self.mock_send.assert_not_awaited()

    async def test_early_send_is_held_after_id_is_stored(self):
        a = self.trade(p.ST_FIAT)
        async def send(*args):
            await p.on_escrow_payment('SEND', 1000, 'early', 'COMPLETED')
            return ('PENDING', 'early', None)
        self.mock_send.side_effect = send
        await p.do_release(None, 20, a)
        await p.on_escrow_payment('SEND', 1000, 'early', 'COMPLETED')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_PAYOUT)
        self.assertEqual(p.get_trade(a)['payout_id'], 'early')
        self.assertEqual(self.mock_send.await_count, 1)

    async def test_duplicate_send_id_or_wrong_amount_never_finishes(self):
        a = self.trade(p.ST_PAYOUT, payout_id='same')
        b = self.trade(p.ST_REFUND, refund_id='same')
        await p.on_escrow_payment('SEND', 1000, 'same', 'COMPLETED')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_PAYOUT)
        self.assertEqual(p.get_trade(b)['stage'], p.ST_REFUND)
        c = self.trade(p.ST_PAYOUT, payout_id='different')
        await p.on_escrow_payment('SEND', 999, 'different', 'COMPLETED')
        self.assertEqual(p.get_trade(c)['stage'], p.ST_PAYOUT)

    async def test_admin_notes_auth_idempotency_and_no_funds_movement(self):
        await p.on_escrow_payment('RECEIVE', 1005, 'r1', 'COMPLETED')
        eid = p._one("SELECT id FROM escrow_events WHERE payment_id='r1'")['id']
        with patch.object(p, 'is_p2p_admin', side_effect=lambda uid: uid == 42):
            self.assertFalse(p.review_escrow_event(99, eid, 'external evidence'))
            self.assertTrue(p.review_escrow_event(42, eid, 'external evidence'))
            self.assertFalse(p.review_escrow_event(42, eid, 'replacement'))
        row = p._one('SELECT * FROM escrow_events WHERE id=?', (eid,))
        self.assertEqual((row['reviewed_by'], row['review_note']), (42, 'external evidence'))
        self.mock_send.assert_not_awaited()

    async def test_seller_id_alone_does_not_prove_escrow_destination(self):
        a = self.trade(p.ST_LOCKING, lock_sent=1, escrow_verified=0, lock_pay_id='seller-id')
        payment = SimpleNamespace(id='seller-id', payment_type='SEND', status='COMPLETED', amount='1005', method='SPARK', details=None)
        seller = SimpleNamespace(get_payment=AsyncMock(return_value=SimpleNamespace(payment=payment)))
        bot = SimpleNamespace(client=None, get_wallet=AsyncMock(return_value=seller), GetPaymentRequest=lambda **kw: kw,
                              ListPaymentsRequest=lambda **kw: kw)
        escrow = SimpleNamespace(list_payments=AsyncMock(return_value=SimpleNamespace(payments=[])))
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=escrow):
            await p.startup_scan()
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        seller.get_payment.assert_not_awaited()
        self.mock_send.assert_not_awaited()

    async def test_duplicate_seller_ids_are_held(self):
        a = self.trade(p.ST_LOCKING, lock_sent=1, escrow_verified=0, lock_pay_id='dup')
        b = self.trade(p.ST_LOCKING, lock_sent=1, escrow_verified=0, lock_pay_id='dup')
        await p.startup_scan()
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(b)['stage'], p.ST_LOCKING)

    async def test_callback_and_text_enforce_admin_auth(self):
        await p.on_escrow_payment('RECEIVE', 1, 'r1', 'COMPLETED')
        eid = p._one("SELECT id FROM escrow_events WHERE payment_id='r1'")['id']
        event = SimpleNamespace(sender_id=99, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.object(p, 'enabled_for', return_value=True), patch.object(p, 'is_banned', return_value=False), patch.object(p, 'is_p2p_admin', side_effect=lambda uid: uid == 42):
            await p.on_callback(None, event, f'p2p:admreview:{eid}')
            event.answer.assert_awaited_once()
            self.assertNotIn('adm:99', p._mem_wiz)
            event.sender_id = 42
            await p.on_callback(None, event, f'p2p:admreview:{eid}')
            p._mem_wiz['adm:99'] = p._mem_wiz['adm:42']
            event.sender_id = 99
            await p.on_text(None, event, 'forged evidence')
            self.assertIsNone(p._one('SELECT reviewed_by FROM escrow_events WHERE id=?', (eid,))['reviewed_by'])
            event.sender_id = 42
            await p.on_text(None, event, 'checked wallet record')
            self.assertEqual(p._one('SELECT reviewed_by FROM escrow_events WHERE id=?', (eid,))['reviewed_by'], 42)

    async def test_restart_holds_journal_with_persisted_id(self):
        a = self.trade(p.ST_PAYOUT, payout_id='old')
        p.record_escrow_event('SEND', 1000, 'old', 'COMPLETED')
        p._db.close(); p._db = None
        p.init_db()
        escrow = SimpleNamespace(list_payments=AsyncMock(return_value=SimpleNamespace(payments=[])))
        bot = SimpleNamespace(client=None, ListPaymentsRequest=lambda **kw: kw)
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=escrow):
            await p.startup_scan()
            await p.startup_scan()
        self.assertEqual(p.get_trade(a)['stage'], p.ST_PAYOUT)
        self.mock_send.assert_not_awaited()

    async def test_conflicting_send_observation_blocks_replay(self):
        a = self.trade(p.ST_PAYOUT, payout_id='old')
        p.record_escrow_event('SEND', 1000, 'old', 'PENDING')
        p.record_escrow_event('SEND', 999, 'old', 'COMPLETED')
        await p.reconcile_escrow_send('old')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_PAYOUT)

    async def test_startup_scans_paginated_history(self):
        payments = [SimpleNamespace(payment_type='RECEIVE', status='COMPLETED', amount='1', id=str(i), method='SPARK', details=None) for i in range(125)]
        class SDK:
            async def list_payments(self, request):
                return SimpleNamespace(payments=payments[request['offset']:request['offset'] + request['limit']])
        bot = SimpleNamespace(client=None, ListPaymentsRequest=lambda **kw: kw)
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=SDK()):
            await p.startup_scan()
        self.assertEqual(len(p._rows('SELECT * FROM escrow_events')), 125)
