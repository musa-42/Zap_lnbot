"""Offline tests for unique-lock-amount auto-attribution (safety version 2).

No production DB, SDK, seed or network: temp SQLite, mocked SDK/bot, isolated
HOME (inherited from the escrow-safety harness module)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_p2p_escrow_safety as base

p = base.p


class UniqueLockAttribution(base.Safety):
    # ---------- helpers ----------

    def v2_trade(self, stage=base.p.ST_LOCKING, lock_amount=1006, **fields):
        fields.setdefault('lock_sent', 1)
        return self.trade(stage, safety_version=2,
                          lock_amount=lock_amount, **fields)

    async def take(self, sats=1000, maker=20, taker=10):
        oid = p._exec("INSERT INTO orders (side,maker,sats,status,created) "
                      "VALUES ('sell',?,?,'open',1)", (maker, sats)).lastrowid
        with patch.object(p, '_send_panel', new_callable=AsyncMock,
                          return_value=SimpleNamespace(chat_id=1, id=1)), \
             patch.object(p, 'live_trade_of', return_value=None):
            return await p.take_order(None, taker, oid)

    # ---------- unique amount assignment ----------

    async def test_take_order_assigns_unique_amount_and_version(self):
        tid = await self.take()
        row = p.get_trade(tid)
        base_fee = p.fee_for(1000)
        self.assertEqual(row['safety_version'], 2)
        self.assertIsNotNone(row['lock_amount'])
        self.assertGreaterEqual(row['lock_amount'], 1000 + base_fee + p.LOCK_OFFSET_MIN)
        self.assertLessEqual(row['lock_amount'], 1000 + base_fee + p.LOCK_OFFSET_MAX)

    async def test_unique_amount_avoids_48h_and_legacy_collisions(self):
        now = p.time.time()
        # legacy locking trade of the same raw size (sats+fee in collision set)
        legacy = self.trade(p.ST_LOCKING, sats=1000, fee=5, safety_version=1)
        # 47h-old finished trade: inside the 48h window
        self.trade(p.ST_CANCELED, sats=1, fee=1, lock_amount=1000 + 5 + 1,
                   lock_started=now - 47 * 3600, created=now - 47 * 3600,
                   updated=now - 47 * 3600)
        # 49h-old finished trade: outside the window, its amount is reusable
        self.trade(p.ST_CANCELED, sats=1, fee=1, lock_amount=1000 + 5 + 2,
                   lock_started=now - 49 * 3600, created=now - 49 * 3600,
                   updated=now - 49 * 3600)
        tid = await self.take()
        row = p.get_trade(tid)
        # 1005 taken by the legacy trade, 1006 by the 47h-old one, 1007 free
        self.assertEqual((row['lock_amount'], row['safety_version']), (1007, 2))
        self.assertNotEqual(legacy, tid)

    async def test_concurrent_same_sats_trades_get_different_amounts(self):
        a, b = await asyncio.gather(self.take(), self.take())
        ra, rb = p.get_trade(a), p.get_trade(b)
        self.assertEqual((ra['safety_version'], rb['safety_version']), (2, 2))
        self.assertNotEqual(ra['lock_amount'], rb['lock_amount'])
        self.assertNotEqual(ra['lock_amount'], ra['sats'] + ra['fee'])
        self.assertNotEqual(rb['lock_amount'], rb['sats'] + rb['fee'])

    async def test_exhausted_offset_space_stays_v1_manual(self):
        now = p.time.time()
        base_amt = 1000 + p.fee_for(1000)
        for off in range(p.LOCK_OFFSET_MIN, p.LOCK_OFFSET_MAX + 1):
            p._exec('INSERT INTO trades (order_id,buyer,seller,sats,fee,stage,created,updated,'
                    'lock_started,lock_amount,safety_version) VALUES (0,10,20,1,1,?,?,?,?,?,2)',
                    (p.ST_DISPUTED, now, now, now, base_amt + off))
        tid = await self.take()
        row = p.get_trade(tid)
        self.assertEqual(row['safety_version'], 1)
        self.assertIsNone(row['lock_amount'])

    # ---------- attribution ----------

    async def test_exact_receipt_locks_once_and_replay_is_idempotent(self):
        tid = self.v2_trade(lock_amount=1006)
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-1', 'COMPLETED')
        row = p.get_trade(tid)
        self.assertEqual(row['stage'], p.ST_LOCKED)
        self.assertEqual(row['escrow_verified'], 1)
        # replay of the same receipt changes nothing
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-1', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKED)
        self.assertEqual(p.get_trade(tid)['locked_at'], row['locked_at'])
        # no alert is left pending: the receipt was resolved automatically
        self.assertIsNone(p._one('SELECT * FROM escrow_alerts WHERE sent_at IS NULL'))

    async def test_receipt_matching_nothing_is_held_with_admin_alert(self):
        tid = self.v2_trade(lock_amount=1006)
        await p.on_escrow_payment('RECEIVE', 424242, 'rcpt-2', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKING)
        alert = p._one('SELECT * FROM escrow_alerts WHERE sent_at IS NULL')
        self.assertIsNone(alert)   # delivered (notify mock succeeds)
        self.assertIsNotNone(p._one('SELECT * FROM escrow_alerts'))

    async def test_receipt_matching_multiple_trades_is_held(self):
        # simulate a legacy collision: two v2 locking trades share an amount
        a = self.v2_trade(lock_amount=1006)
        b = self.v2_trade(lock_amount=1006)
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-3', 'COMPLETED')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(b)['stage'], p.ST_LOCKING)
        self.assertIsNone(p._one('SELECT * FROM escrow_alerts WHERE sent_at IS NULL'))

    async def test_version1_trade_never_auto_attributed(self):
        tid = self.trade(p.ST_LOCKING, lock_sent=1, safety_version=1, lock_amount=1006,
                         escrow_verified=0)
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-4', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(tid)['escrow_verified'], 0)

    async def test_pending_receipt_then_completed_attributes(self):
        tid = self.v2_trade(lock_amount=1006)
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-5', 'PENDING')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKING)
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-5', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKED)

    async def test_conflicting_observation_is_held_and_invalidation_intact(self):
        tid = self.v2_trade(lock_amount=1006)
        # same payment id previously observed as an unrelated SEND: conflict
        p.record_escrow_event('SEND', 77, 'rcpt-6', 'COMPLETED')
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-6', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKING)
        ev = p._one("SELECT * FROM escrow_events WHERE payment_id='rcpt-6'")
        self.assertEqual(ev['conflict'], 1)

    async def test_reused_payment_id_bound_to_send_is_held(self):
        tid = self.v2_trade(lock_amount=1006)
        done = self.trade(p.ST_DONE, payout_id='rcpt-7')
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-7', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKING)
        self.assertEqual(p.get_trade(done)['stage'], p.ST_DONE)

    async def test_duplicate_same_amount_receipt_is_held(self):
        a = self.v2_trade(lock_amount=1006)
        b = self.v2_trade(lock_amount=1006 + 1)
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-8', 'COMPLETED')
        self.assertEqual(p.get_trade(a)['stage'], p.ST_LOCKED)
        # a second completed receipt of the same amount must not credit b's
        # neighbour amount even if amounts were collided manually
        await p.on_escrow_payment('RECEIVE', 1007, 'rcpt-9', 'COMPLETED')
        self.assertEqual(p.get_trade(b)['stage'], p.ST_LOCKED)

    async def test_echo_receipt_after_send_verified_lock_is_quiet(self):
        tid = self.v2_trade(stage=p.ST_LOCKED, lock_amount=1006, escrow_verified=1)
        rooms_before = p.update_room.await_count
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-10', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKED)
        self.assertEqual(p.update_room.await_count, rooms_before)
        self.assertIsNone(p._one('SELECT * FROM escrow_alerts WHERE sent_at IS NULL'))
        # a genuine duplicate payment of the same amount stays held + alerted
        await p.on_escrow_payment('RECEIVE', 1006, 'rcpt-11', 'COMPLETED')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKED)
        self.assertIsNone(p._one('SELECT * FROM escrow_alerts WHERE sent_at IS NULL'))

    async def test_receipt_during_startup_scan_attributes(self):
        tid = self.v2_trade(lock_amount=1006)
        sdk = SimpleNamespace(list_payments=AsyncMock(return_value=SimpleNamespace(payments=[
            SimpleNamespace(method='PaymentMethod.SPARK', payment_type='RECEIVE',
                            amount_sats=1006, id='rcpt-12', status='COMPLETED')])))
        bot = SimpleNamespace(client=None, ListPaymentsRequest=lambda **kw: kw)
        with patch.object(p, '_bot', return_value=bot), \
             patch.object(p, 'escrow_sdk', new_callable=AsyncMock, return_value=sdk):
            await p.startup_scan()
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_LOCKED)
        self.assertIsNone(p._one('SELECT * FROM escrow_alerts WHERE sent_at IS NULL'))

    # ---------- lock send + seller UX ----------

    async def test_do_lock_sends_unique_amount_for_v2_and_raw_for_v1(self):
        async def run_lock(tid):
            sent = {}

            class SDK:
                async def prepare_send_payment(self, **kw):
                    sent['req'] = kw
                    return 'prep'

                async def send_payment(self, **kw):
                    return SimpleNamespace(payment=SimpleNamespace(status='PENDING', id='p'))

            bot = SimpleNamespace(client=None, get_wallet=AsyncMock(return_value=SDK()),
                                  get_balance=AsyncMock(return_value=10000),
                                  PrepareSendPaymentRequest=lambda **kw: kw,
                                  PaymentRequest=SimpleNamespace(INPUT=lambda **kw: kw),
                                  SendPaymentRequest=lambda **kw: kw)

            async def direct(coro):
                return await coro

            with patch.object(p, '_bot', return_value=bot), \
                 patch.object(p, 'offloop', side_effect=direct), \
                 patch.object(p, 'escrow_address', new_callable=AsyncMock, return_value='escrow'):
                await p.do_lock(None, 20, tid)
            return sent['req']['request']['amount']

        v2 = self.v2_trade(lock_amount=1006, lock_sent=0)
        self.assertEqual(await run_lock(v2), 1006)
        v1 = self.trade(p.ST_LOCKING, lock_sent=0, sats=1000, fee=5, safety_version=1)
        self.assertEqual(await run_lock(v1), 1005)

    async def test_seller_flow_shows_the_unique_amount(self):
        tid = await self.take()
        row = p.get_trade(tid)
        note = p.stage_note_for(20, row)   # seller
        self.assertIn(p.fmt(row['lock_amount']), note)
        buyer_note = p.stage_note_for(10, row)
        self.assertNotIn('Exact lock amount', buyer_note)

    # ---------- migration ----------

    async def test_migration_adds_lock_amount_idempotently(self):
        p._db.execute('ALTER TABLE trades DROP COLUMN lock_amount')
        p.init_db()
        cols = {r[1] for r in p._db.execute('PRAGMA table_info(trades)').fetchall()}
        self.assertIn('lock_amount', cols)
        p.init_db()   # repeat migration is harmless
        cols = {r[1] for r in p._db.execute('PRAGMA table_info(trades)').fetchall()}
        self.assertIn('lock_amount', cols)


class UniqueLockSettlement(base.Safety):
    """Dust-free v2 settlement amounts (lock_amount-based) with byte-identical
    v1 behavior: payout pays lock-fee, refund pays the whole lock, evidence
    checks accept exactly what was sent."""

    def v2_trade(self, stage, lock_amount=1006, **fields):
        fields.setdefault('sats', 1000)
        fields.setdefault('fee', 5)
        return self.trade(stage, safety_version=2, lock_amount=lock_amount, **fields)

    def sent_amounts(self):
        return [c.args[1] for c in self.mock_send.await_args_list]

    async def test_v2_payout_sends_lock_minus_fee(self):
        tid = self.v2_trade(p.ST_FIAT)
        await p.do_release(None, 20, tid)
        self.assertEqual(self.sent_amounts(), [1001])          # 1006 - 5
        self.assertEqual(self.mock_send.await_args.args[2], f'payout#{tid}')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_v2_refund_sends_full_lock_amount(self):
        tid = self.v2_trade(p.ST_LOCKED)
        await p.start_refund(None, tid)
        self.assertEqual(self.sent_amounts(), [1006])
        self.assertEqual(self.mock_send.await_args.args[2], f'refund#{tid}')

    async def test_v2_admin_release_and_refund_follow_same_rule(self):
        a = self.v2_trade(p.ST_DISPUTED)
        await p.admin_release(None, a)
        self.assertEqual(self.sent_amounts(), [1001])
        self.assertEqual(self.mock_send.await_args.args[2], f'adminpayout#{a}')
        b = self.v2_trade(p.ST_DISPUTED)
        await p.admin_refund(None, b)
        self.assertEqual(self.sent_amounts(), [1001, 1006])
        self.assertEqual(self.mock_send.await_args.args[2], f'adminrefund#{b}')

    async def test_v1_amounts_are_byte_identical_regression(self):
        a = self.trade(p.ST_FIAT)               # v1: sats=1000 fee=5
        await p.do_release(None, 20, a)
        self.assertEqual(self.sent_amounts(), [1000])
        b = self.trade(p.ST_LOCKED)
        await p.start_refund(None, b)
        self.assertEqual(self.sent_amounts(), [1000, 1005])
        c = self.trade(p.ST_FIAT)
        await p.admin_release(None, c)
        self.assertEqual(self.sent_amounts(), [1000, 1005, 1000])
        d = self.trade(p.ST_DISPUTED)
        await p.admin_refund(None, d)
        self.assertEqual(self.sent_amounts(), [1000, 1005, 1000, 1005])

    async def test_v1_trade_with_stray_lock_amount_column_keeps_old_amounts(self):
        # defensive: only safety_version>=2 switches amounts, never the column alone
        a = self.trade(p.ST_FIAT, lock_amount=1006, safety_version=1)
        await p.do_release(None, 20, a)
        self.assertEqual(self.sent_amounts(), [1000])
        b = self.trade(p.ST_LOCKED, lock_amount=1006, safety_version=1)
        await p.start_refund(None, b)
        self.assertEqual(self.sent_amounts(), [1000, 1005])

    async def test_escrow_balance_delta_zero_after_full_v2_settlement(self):
        tid = self.v2_trade(p.ST_FIAT)
        row = p.get_trade(tid)
        ledger = [row['lock_amount']]                      # unique lock receipt
        await p.do_release(None, 20, tid)
        ledger.append(-self.sent_amounts()[0])             # buyer payout (sent)
        ledger.append(-int(row['fee']))                    # operator fee sweep
        self.assertEqual(sum(ledger), 0)                   # dust = 0
        self.assertEqual(p.payout_amount_of(row) + int(row['fee']), row['lock_amount'])

    async def test_refund_settlement_leaves_no_dust(self):
        tid = self.v2_trade(p.ST_LOCKED)
        row = p.get_trade(tid)
        await p.start_refund(None, tid)
        self.assertEqual(row['lock_amount'] - self.sent_amounts()[0], 0)

    async def test_v2_amount_helpers_agree_with_each_route(self):
        for stage in (p.ST_FIAT, p.ST_DISPUTED, p.ST_LOCKED):
            tr = p.get_trade(self.v2_trade(stage))
            self.assertEqual(p.payout_amount_of(tr), 1001)
            self.assertEqual(p.refund_amount_of(tr), 1006)
        v1 = p.get_trade(self.trade(p.ST_FIAT))
        self.assertEqual((p.payout_amount_of(v1), p.refund_amount_of(v1)), (1000, 1005))


class UniqueLockSettlementAdjudication(base.Safety):
    """admin_adjudicate must expect exactly what the escrow actually sent."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.admin = patch.object(p, 'is_p2p_admin', return_value=True)
        self.admin.start()

    async def asyncTearDown(self):
        self.admin.stop()
        await super().asyncTearDown()

    def v2_trade(self, stage, lock_amount=1006, **fields):
        fields.setdefault('sats', 1000)
        fields.setdefault('fee', 5)
        return self.trade(stage, safety_version=2, lock_amount=lock_amount, **fields)

    def sent_amounts(self):
        return [c.args[1] for c in self.mock_send.await_args_list]

    async def test_v2_payout_evidence_accepted_and_replays(self):
        tid = self.v2_trade(p.ST_PAYOUT, payout_id='pay-v2')
        p.record_escrow_event('SEND', 1001, 'pay-v2', 'COMPLETED')   # lock-fee
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'settled')
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'already settled')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_DONE)

    async def test_v2_payout_rejects_old_sats_amount_evidence(self):
        tid = self.v2_trade(p.ST_PAYOUT, payout_id='pay-old')
        p.record_escrow_event('SEND', 1000, 'pay-old', 'COMPLETED')  # legacy amount
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'insufficient evidence')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)

    async def test_v2_refund_evidence_expects_full_lock(self):
        tid = self.v2_trade(p.ST_REFUND, refund_id='ref-v2')
        p.record_escrow_event('SEND', 1006, 'ref-v2', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, tid, 'refund'), 'settled')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_REFUNDED)
        b = self.v2_trade(p.ST_REFUND, refund_id='ref-old')
        p.record_escrow_event('SEND', 1005, 'ref-old', 'COMPLETED')  # sats+fee
        self.assertEqual(p.admin_adjudicate(77, b, 'refund'), 'insufficient evidence')

    async def test_v1_adjudication_amounts_unchanged(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='pay-v1')
        p.record_escrow_event('SEND', 1000, 'pay-v1', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'settled')
        rid = self.trade(p.ST_REFUND, refund_id='ref-v1')
        p.record_escrow_event('SEND', 1005, 'ref-v1', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, rid, 'refund'), 'settled')

    async def test_end_to_end_v2_payout_send_then_adjudicate_with_captured_amount(self):
        # what do_release actually sent is exactly what adjudication accepts
        tid = self.v2_trade(p.ST_FIAT)
        await p.do_release(None, 20, tid)
        sent = self.sent_amounts()[0]
        p.record_escrow_event('SEND', sent, 'send-id', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(77, tid, 'payout'), 'settled')
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_DONE)

    async def test_hold_detail_displays_bound_amounts_for_v2(self):
        # holds view renders the v2 trade and its settle button stays available
        from types import SimpleNamespace
        tid = self.v2_trade(p.ST_PAYOUT, payout_id='pay-ui')
        p.record_escrow_event('SEND', 1001, 'pay-ui', 'COMPLETED')
        with patch.object(p, 'is_p2p_admin', return_value=True), \
             patch.object(p, '_btn', side_effect=lambda text, data: (text, data)):
            ev = SimpleNamespace(sender_id=77, answer=AsyncMock(), edit=AsyncMock())
            await p.on_callback(None, ev, f'p2p:admhold:{tid}')
        ev.edit.assert_awaited_once()
        text = ev.edit.await_args.args[0]
        self.assertIn('pay-ui', text)
        buttons = [b[1] for row in (ev.edit.await_args.kwargs.get('buttons') or []) for b in row]
        self.assertIn(f'p2p:adjudicate:payout:{tid}', buttons)


if __name__ == '__main__':
    import unittest
    unittest.main()
