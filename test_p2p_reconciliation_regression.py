"""Offline regression cases: imports the isolated P2P SQLite/SDK fixture."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_escrow_safety as base

p = base.p

class ReconciliationRegression(base.Safety):
    async def test_immediate_success_with_conflicting_evidence_is_held(self):
        tid = self.trade(p.ST_FIAT)
        p.record_escrow_event('SEND', 999, 'duplicate', 'COMPLETED')
        self.mock_send.return_value = (True, 'duplicate', None)
        await p.do_release(None, 20, tid)
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_immediate_success_reused_id_is_held(self):
        old = self.trade(p.ST_DONE, payout_id='same', fee_safe=1)
        tid = self.trade(p.ST_FIAT)
        p.record_escrow_event('SEND', 1000, 'same', 'COMPLETED')
        self.mock_send.return_value = (True, 'same', None)
        await p.do_release(None, 20, tid)
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)
        self.assertEqual(p.get_trade(old)['fee_safe'], 0)
        self.assertEqual(p._one("SELECT conflict FROM escrow_events WHERE payment_id='same'")['conflict'], 1)

    async def test_completed_response_without_observation_stays_held(self):
        tid = self.trade(p.ST_FIAT)
        self.mock_send.return_value = (True, 'new-id', None)
        await p.do_release(None, 20, tid)
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_all_refund_and_admin_success_paths_reject_conflicts(self):
        cases = ((p.ST_LOCKED, False, False), (p.ST_DISPUTED, True, False), (p.ST_DISPUTED, True, True))
        for number, (stage, admin, payout) in enumerate(cases):
            pid = 'bad-' + str(number)
            tid = self.trade(stage)
            p.record_escrow_event('RECEIVE', 1, pid, 'COMPLETED')
            self.mock_send.return_value = (True, pid, None)
            if not admin:
                await p.start_refund(None, tid)
            elif payout:
                await p.admin_release(None, tid)
            else:
                await p.admin_refund(None, tid)
            self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT if payout else p.ST_REFUND)

    async def test_history_conflict_precedes_replay(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='id')
        p.record_escrow_event('SEND', 1000, 'id', 'COMPLETED')
        payment = SimpleNamespace(payment_type='SEND', status='FAILED', amount='1000', id='id', method='SPARK', details=None)
        class SDK:
            async def list_payments(self, request):
                return SimpleNamespace(payments=[payment] if request['offset'] == 0 else [])
        bot = SimpleNamespace(client=None, ListPaymentsRequest=lambda **kw: kw)
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=SDK()):
            await p.startup_scan()
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_replay_offline_and_short_pages(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='old')
        p.record_escrow_event('SEND', 1000, 'old', 'COMPLETED')
        await p.startup_scan()
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)
        payments = [SimpleNamespace(payment_type='RECEIVE', status='COMPLETED', amount='1', id=str(i), method='SPARK', details=None) for i in range(25)]
        class SDK:
            async def list_payments(self, request):
                return SimpleNamespace(payments=payments[request['offset']:request['offset'] + 10])
        bot = SimpleNamespace(client=None, ListPaymentsRequest=lambda **kw: kw)
        with patch.object(p, '_bot', return_value=bot), patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=SDK()):
            await p.startup_scan()
        self.assertEqual(len(p._rows("SELECT * FROM escrow_events WHERE direction='RECEIVE'")), 25)

    async def test_alert_replayed_after_crash_and_failure_transition(self):
        p.record_escrow_event('RECEIVE', 1, 'r', 'COMPLETED')
        p._db.close(); p._db = None; p.init_db()
        p.notify.reset_mock()
        await p.on_escrow_payment('RECEIVE', 1, 'r', 'COMPLETED')
        self.assertEqual(p.notify.await_count, 1)
        await p.on_escrow_payment('SEND', 1000, 's', 'PENDING')
        p.notify.reset_mock()
        await p.on_escrow_payment('SEND', 1000, 's', 'FAILED')
        self.assertEqual(p.notify.await_count, 1)

    async def test_alert_failure_retries_and_ack_survives_restart(self):
        p.notify.return_value = None
        await p.on_escrow_payment('RECEIVE', 1, 'r', 'COMPLETED')
        self.assertIsNone(p._one('SELECT sent_at FROM escrow_alerts')['sent_at'])
        p._db.close(); p._db = None; p.init_db()
        p.notify.return_value = object()
        p.notify.reset_mock()
        await p.replay_escrow_journal()
        self.assertEqual(p.notify.await_count, 1)
        self.assertIsNotNone(p._one('SELECT sent_at FROM escrow_alerts')['sent_at'])
        p._db.close(); p._db = None; p.init_db()
        p.notify.reset_mock()
        await p.replay_escrow_journal()
        self.assertEqual(p.notify.await_count, 0)

    async def test_late_contradiction_retained_and_disables_fee(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='s')
        await p.on_escrow_payment('SEND', 1000, 's', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)
        await p.on_escrow_payment('SEND', 1000, 's', 'FAILED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)
        self.assertEqual(p.get_trade(tid)['fee_safe'], 0)
        self.assertEqual(len(p._rows('SELECT * FROM escrow_observations')), 2)
        self.assertEqual(p._one('SELECT conflict FROM escrow_events')['conflict'], 1)

    async def test_matching_refund_is_held(self):
        tid = self.trade(p.ST_REFUND, refund_id='refund')
        await asyncio.gather(p.on_escrow_payment('SEND', 1005, 'refund', 'COMPLETED'),
                             p.on_escrow_payment('SEND', 1005, 'refund', 'COMPLETED'))
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_REFUND)
        self.assertEqual(p.audit.await_count, 0)

    async def test_review_history_preserved_and_current_review_invalidated(self):
        eid, _ = p.record_escrow_event('RECEIVE', 1, 'r', 'COMPLETED')
        with patch.object(p, 'is_p2p_admin', return_value=True):
            self.assertTrue(p.review_escrow_event(42, eid, 'original'))
        p.record_escrow_event('RECEIVE', 2, 'r', 'COMPLETED')
        row = p._one('SELECT * FROM escrow_events WHERE id=?', (eid,))
        self.assertEqual(row['conflict'], 1)
        self.assertIsNone(row['reviewed_by'])
        self.assertEqual(p._one('SELECT note FROM escrow_reviews WHERE event_id=?', (eid,))['note'], 'original')

    async def test_listener_records_failure(self):
        class PAYMENT_FAILED:
            payment = SimpleNamespace(payment_type='SEND', status='FAILED', amount='1', id='f', method='SPARK', details=None)
        bot = SimpleNamespace(client=None, main_event_loop=asyncio.get_running_loop())
        with patch.object(p, '_bot', return_value=bot):
            await p.EscrowListener().on_event(PAYMENT_FAILED())
            await asyncio.sleep(0.01)
        self.assertEqual(p._one("SELECT status FROM escrow_events WHERE payment_id='f'")['status'], 'FAILED')

    async def test_interleaved_conflict_before_finish_is_held(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='race')
        p.record_escrow_event('SEND', 1000, 'race', 'COMPLETED')
        original = p.finish_payout
        async def inject(tid):
            p.record_escrow_event('SEND', 999, 'race', 'COMPLETED')
            await original(tid)
        with patch.object(p, 'finish_payout', side_effect=inject):
            await p.reconcile_escrow_send('race')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_threads_never_complete_conflicted_record(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='race')
        p.record_escrow_event('SEND', 1000, 'race', 'COMPLETED')
        barrier = threading.Barrier(2)
        def add_conflict():
            barrier.wait()
            p.record_escrow_event('SEND', 999, 'race', 'COMPLETED')
        with p._db_lock:
            worker = threading.Thread(target=add_conflict)
            worker.start(); barrier.wait()
        worker.join()
        await asyncio.gather(p.reconcile_escrow_send('race'), p.reconcile_escrow_send('race'))
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_replay_with_bot_unavailable_stays_held(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='offline')
        p.record_escrow_event('SEND', 1000, 'offline', 'COMPLETED')
        p.record_escrow_event('RECEIVE', 1, 'receipt', 'COMPLETED')
        with patch.object(p, '_bot', side_effect=RuntimeError('bot unavailable')):
            try:
                await p.replay_escrow_journal()
            except RuntimeError:
                pass  # notification/UI after committed transition cannot undo it
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)
        self.assertIsNone(p._one('SELECT sent_at FROM escrow_alerts')['sent_at'])
