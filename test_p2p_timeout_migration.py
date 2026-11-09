"""Offline regression tests for timeout disputes and conservative migration."""
import asyncio
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import test_p2p_escrow_safety as base

p = base.p

class TimeoutMigration(base.Safety):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sdk_patch = patch.object(p, 'escrow_sdk', new_callable=AsyncMock, side_effect=RuntimeError('offline'))
        self.sdk_patch.start()

    async def asyncTearDown(self):
        self.sdk_patch.stop()
        await super().asyncTearDown()

    async def test_expired_seller_cancel_opens_dispute_never_refunds(self):
        a = self.trade(p.ST_LOCKED, updated=1)
        await p.request_cancel(None, 20, a)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_DISPUTED)
        self.mock_send.assert_not_awaited()
        p.admin_case.assert_awaited()

    async def test_expired_sweep_opens_dispute_never_refunds(self):
        a = self.trade(p.ST_LOCKED, updated=1)
        await p.sweep(None)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_DISPUTED)
        self.mock_send.assert_not_awaited()

    async def test_old_locking_never_resent_or_canceled_after_migration(self):
        a = self.trade(p.ST_LOCKING, lock_sent=0)
        p._db.execute('ALTER TABLE trades DROP COLUMN safety_version') if 'safety_version' in p.get_trade(a) else None
        p.init_db()
        await p.do_lock(None, 20, a)
        await p.request_cancel(None, 10, a)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(a)['lock_sent'], 1)
        self.assertIn('safe reconciliation', p.stage_note_for(10, p.get_trade(a)))

    async def test_pre_flag_schema_and_interrupted_migration_are_held(self):
        a = self.trade(p.ST_LOCKING, lock_sent=0)
        for column in ('safety_version', 'locked_at', 'lock_sent', 'lock_pay_id', 'escrow_verified', 'fee_safe'):
            p._db.execute('ALTER TABLE trades DROP COLUMN ' + column)
        p.init_db()
        self.assertEqual(p.get_trade(a)['lock_sent'], 1)
        p.init_db()
        self.assertEqual(p.get_trade(a)['lock_sent'], 1)
        await p.request_cancel(None, 10, a)
        await p.sweep(None)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        self.mock_send.assert_not_awaited()

    async def test_fresh_trade_is_not_misidentified_on_restart(self):
        oid = p._exec("INSERT INTO orders (side,maker,sats,status,created) VALUES ('sell',20,1000,'open',1)").lastrowid
        with patch.object(p, '_send_panel', new_callable=AsyncMock, return_value=SimpleNamespace(chat_id=1,id=1)):
            a = await p.take_order(None, 10, oid)
        p.init_db()
        self.assertEqual(p.get_trade(a)['lock_sent'], 0)
        await p.request_cancel(None, 10, a)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_CANCELED)

    async def test_lock_claim_rejects_expired_deadline(self):
        a = self.trade(p.ST_LOCKING, lock_started=1)
        b = SimpleNamespace(get_wallet=AsyncMock())
        with patch.object(p, '_bot', return_value=b):
            await p.do_lock(None, 20, a)
        b.get_wallet.assert_not_awaited()
        self.assertEqual(p.get_trade(a)['lock_sent'], 0)

    async def test_stale_timeout_cannot_override_fiat_confirmation(self):
        a = self.trade(p.ST_LOCKED, updated=1)
        stale = p.get_trade(a)
        await p.do_fiat(None, 10, a)
        await p.timeout_dispute(None, stale, p.time.time(), p.FIAT_TIMEOUT_SECS)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_FIAT)
        self.mock_send.assert_not_awaited()

    async def test_fiat_and_dispute_race_has_one_winner(self):
        a = self.trade(p.ST_LOCKED, updated=1)
        tr = p.get_trade(a)
        await asyncio.gather(p.do_fiat(None, 10, a),
                             p.timeout_dispute(None, tr, p.time.time(), p.FIAT_TIMEOUT_SECS))
        self.assertIn(p.get_trade(a)['stage'], (p.ST_FIAT, p.ST_DISPUTED))
        self.mock_send.assert_not_awaited()
        if p.get_trade(a)['stage'] == p.ST_FIAT:
            p.admin_case.assert_not_awaited()
        else:
            p.admin_case.assert_awaited_once()

    async def test_lock_claim_and_sweep_race_no_double_outcome(self):
        a = self.trade(p.ST_LOCKING, lock_started=p.time.time()-p.LOCK_WAIT_SECS-1)
        b = SimpleNamespace(get_wallet=AsyncMock())
        with patch.object(p, '_bot', return_value=b):
            await asyncio.gather(p.do_lock(None, 20, a), p.sweep(None))
        self.assertEqual(p.get_trade(a)['stage'], p.ST_CANCELED)
        b.get_wallet.assert_not_awaited()

    async def test_unverified_receipt_never_becomes_trade_identity(self):
        a = self.trade(p.ST_LOCKING, lock_sent=1, lock_pay_id='identical')
        b = self.trade(p.ST_LOCKING, lock_sent=1, lock_pay_id='other')
        await p.on_escrow_payment('RECEIVE', 1005, 'identical', 'COMPLETED')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(b)['stage'], p.ST_LOCKING)
        self.mock_send.assert_not_awaited()

    async def test_deadline_is_stable_after_cancel_vote(self):
        a = self.trade(p.ST_LOCKED, updated=p.time.time()-p.FIAT_TIMEOUT_SECS-1)
        await p.request_cancel(None, 10, a)
        await p.request_cancel(None, 20, a)
        self.assertEqual(p.get_trade(a)['stage'], p.ST_DISPUTED)
        self.mock_send.assert_not_awaited()
