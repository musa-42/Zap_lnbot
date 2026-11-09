"""Offline tests for the P2P admin panel UI: state-aware buttons, fa/en labels,
pagination, legacy path, invalidated display and non-admin rejection.
No production DB, SDK, session or network (see test_p2p_escrow_safety)."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_escrow_safety as base

p = base.p

ADMIN = 77
NON_ADMIN = 55

ROUTES = [
    'p2p:adm', 'p2p:adm:disputes', 'p2p:adm:ban', 'p2p:adm:banlist',
    'p2p:adm:admins', 'p2p:adm:admadd', 'p2p:adm:admdel:88', 'p2p:adm:unban:66',
    'p2p:adm:banok:66', 'p2p:adm:cxlive:1',
    'p2p:admholds:0', 'p2p:admhold:1', 'p2p:adjudicate:payout:1',
    'p2p:feeconfirm:1', 'p2p:feesend:1:deadbeef', 'p2p:admevents:0',
    'p2p:admevent:1', 'p2p:admreview:1', 'p2p:admcase:1', 'p2p:admchat:1',
    'p2p:admrel:1', 'p2p:admref:1', 'p2p:legacyack:release:1:deadbeef',
]


def btn_patch():
    return patch.object(p, '_btn', side_effect=lambda text, data: (text, data))


def flat(buttons):
    return [b[1] for row in (buttons or []) for b in row]


class AdminPanel(base.Safety):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.admin_gate = patch.object(p, 'is_p2p_admin', return_value=True)
        self.admin_gate.start()
        self.owner = patch.object(p, 'admin_id', return_value=ADMIN)
        self.owner.start()

    async def asyncTearDown(self):
        self.owner.stop()
        self.admin_gate.stop()
        await super().asyncTearDown()

    def event(self, sender=ADMIN):
        return SimpleNamespace(sender_id=sender, answer=AsyncMock(),
                               edit=AsyncMock(), respond=AsyncMock())

    def callback(self, data, sender=ADMIN):
        return self._callback(data, sender)

    async def _callback(self, data, sender=ADMIN):
        ev = self.event(sender)
        with btn_patch():
            await p.on_callback(None, ev, data)
        return ev

    # ---------- single entry + section rendering ----------

    async def test_panel_renders_all_sections_with_counts(self):
        self.trade(p.ST_PAYOUT, payout_id='pid-a')
        p.record_escrow_event('RECEIVE', 1, 'unattr', 'COMPLETED')
        self.trade(p.ST_DISPUTED)
        ev = await self.callback('p2p:adm')
        ev.answer.assert_awaited()
        data = flat(ev.edit.await_args.kwargs['buttons'])
        for route in ('p2p:adm:disputes', 'p2p:admevents:0', 'p2p:admholds:0',
                      'p2p:adm:ban', 'p2p:adm:banlist', 'p2p:adm:admins'):
            self.assertIn(route, data)
        pairs = [b for row in ev.edit.await_args.kwargs['buttons'] for b in row]
        labels = [b[0] for b in pairs]
        self.assertTrue(any('Held operations (1)' in lbl for lbl in labels), labels)
        self.assertTrue(any('Review escrow events (1)' in lbl for lbl in labels), labels)
        self.assertTrue(any('Open disputes (1)' in lbl for lbl in labels), labels)

    async def test_home_shows_single_admin_entry_for_admin(self):
        with patch.dict(os.environ, {'P2P_TEST_USERS': str(ADMIN)}):
            p.set_agreed(ADMIN)
            ev = await self.callback('p2p:home')
        data = flat(ev.edit.await_args.kwargs['buttons'])
        self.assertEqual(data.count('p2p:adm'), 1)

    async def test_every_admin_route_answers(self):
        # a disputed trade + an event so list/detail routes have data
        tid = self.trade(p.ST_DISPUTED)
        eid, _ = p.record_escrow_event('RECEIVE', 1, 'rx-1', 'COMPLETED')
        p.ban_user(66, 'test')
        routes = [r.replace(':1', f':{tid}') if r.endswith(':1') else r for r in ROUTES]
        routes = [r.replace('admevent:%d' % tid, 'admevent:%d' % eid) for r in routes]
        routes = [r.replace('admreview:%d' % tid, 'admreview:%d' % eid) for r in routes]
        routes = [r.replace('admhold:%d' % tid, 'admhold:%d' % tid) for r in routes]
        for route in routes:
            with self.subTest(route=route):
                ev = await self.callback(route)
                self.assertTrue(ev.answer.await_count >= 1, route)

    # ---------- non-admin rejection ----------

    async def test_non_admin_rejected_on_every_admin_route(self):
        self.trade(p.ST_DISPUTED)
        with patch.object(p, 'is_p2p_admin', return_value=False), \
             patch.dict(os.environ, {'P2P_TEST_USERS': str(NON_ADMIN)}):
            for route in ROUTES:
                with self.subTest(route=route):
                    ev = self.callback  # noqa: F841 — explicit event below
                    e = SimpleNamespace(sender_id=NON_ADMIN, answer=AsyncMock(),
                                        edit=AsyncMock(), respond=AsyncMock())
                    with btn_patch():
                        await p.on_callback(None, e, route)
                    e.edit.assert_not_awaited()
                    args, kwargs = e.answer.await_args
                    self.assertTrue(kwargs.get('alert') or (args and args[1] is True))

    async def test_banned_non_admin_blocked_at_entry(self):
        p.ban_user(NON_ADMIN, 'x')
        with patch.object(p, 'is_p2p_admin', return_value=False), \
             patch.dict(os.environ, {'P2P_TEST_USERS': str(NON_ADMIN)}):
            e = SimpleNamespace(sender_id=NON_ADMIN, answer=AsyncMock(), edit=AsyncMock())
            await p.on_callback(None, e, 'p2p:adm')
        e.edit.assert_not_awaited()
        self.assertTrue(e.answer.await_args.kwargs.get('alert'))

    # ---------- pagination ----------

    async def test_holds_pagination(self):
        for _ in range(12):
            self.trade(p.ST_PAYOUT)
        ev = await self.callback('p2p:admholds:0')
        data = flat(ev.edit.await_args.kwargs['buttons'])
        items = [d for d in data if d.startswith('p2p:admhold:')]
        self.assertEqual(len(items), 10)
        nxt = [d for d in data if d.startswith('p2p:admholds:') and d != 'p2p:admholds:0']
        self.assertEqual(len(nxt), 1)
        ev2 = await self.callback(nxt[0])
        data2 = flat(ev2.edit.await_args.kwargs['buttons'])
        self.assertEqual(len([d for d in data2 if d.startswith('p2p:admhold:')]), 2)
        self.assertFalse([d for d in data2 if d.startswith('p2p:admholds:')])

    async def test_events_pagination(self):
        for i in range(12):
            p.record_escrow_event('RECEIVE', 1, f'rx-{i}', 'COMPLETED')
        ev = await self.callback('p2p:admevents:0')
        data = flat(ev.edit.await_args.kwargs['buttons'])
        self.assertEqual(len([d for d in data if d.startswith('p2p:admevent:')]), 10)
        nxt = [d for d in data if d.startswith('p2p:admevents:') and d != 'p2p:admevents:0'][0]
        ev2 = await self.callback(nxt)
        data2 = flat(ev2.edit.await_args.kwargs['buttons'])
        self.assertEqual(len([d for d in data2 if d.startswith('p2p:admevent:')]), 2)
        self.assertFalse([d for d in data2 if d.startswith('p2p:admevents:')])

    # ---------- state-aware hold buttons ----------

    async def hold_buttons(self, tid):
        ev = await self.callback(f'p2p:admhold:{tid}')
        return flat(ev.edit.await_args.kwargs['buttons'])

    async def test_settle_hidden_without_bound_payment_id(self):
        tid = self.trade(p.ST_PAYOUT)  # payout stage, no payout_id
        data = await self.hold_buttons(tid)
        self.assertNotIn(f'p2p:adjudicate:payout:{tid}', data)
        self.assertNotIn(f'p2p:feeconfirm:{tid}', data)

    async def test_settle_shown_with_bound_evidence_and_hidden_after(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='pay-ev')
        p.record_escrow_event('SEND', 1000, 'pay-ev', 'COMPLETED')
        data = await self.hold_buttons(tid)
        self.assertIn(f'p2p:adjudicate:payout:{tid}', data)
        self.assertEqual(p.admin_adjudicate(ADMIN, tid, 'payout'), 'settled')
        data = await self.hold_buttons(tid)
        self.assertNotIn(f'p2p:adjudicate:payout:{tid}', data)

    async def test_refund_settle_only_with_bound_refund_id(self):
        tid = self.trade(p.ST_REFUND, refund_id='ref-ev')
        p.record_escrow_event('SEND', 1005, 'ref-ev', 'COMPLETED')
        data = await self.hold_buttons(tid)
        self.assertIn(f'p2p:adjudicate:refund:{tid}', data)
        tid2 = self.trade(p.ST_REFUND)  # no refund id -> nothing to adjudicate
        data2 = await self.hold_buttons(tid2)
        self.assertNotIn(f'p2p:adjudicate:refund:{tid2}', data2)

    async def test_fee_button_requires_adjudicated_payout_and_fresh_state(self):
        # done trade, payout never adjudicated -> fee send impossible, button hidden
        tid = self.trade(p.ST_PAYOUT, payout_id='pay-f1')
        p.record_escrow_event('SEND', 1000, 'pay-f1', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(ADMIN, tid, 'payout'), 'settled')
        data = await self.hold_buttons(tid)
        self.assertIn(f'p2p:feeconfirm:{tid}', data)
        # fee_tries > 0 (attempt claimed) -> hidden
        p._exec('UPDATE trades SET fee_tries=1 WHERE id=?', (tid,))
        self.assertNotIn(f'p2p:feeconfirm:{tid}', await self.hold_buttons(tid))
        # fee already paid -> hidden (and trade leaves the holds list entirely)
        p._exec('UPDATE trades SET fee_tries=0, fee_paid=1 WHERE id=?', (tid,))
        self.assertNotIn(f'p2p:feeconfirm:{tid}', await self.hold_buttons(tid))
        # bound fee attempt -> fee SETTLEMENT button instead of send
        p._exec('UPDATE trades SET fee_paid=0, fee_tries=1 WHERE id=?', (tid,))
        p._exec('INSERT INTO escrow_fee_attempts(trade_id,admin_uid,claimed_at,amount,payment_id,bound_at) '
                'VALUES (?,?,?,?,?,?)', (tid, ADMIN, p.time.time(), 5, 'fee-pid', p.time.time()))
        data = await self.hold_buttons(tid)
        self.assertNotIn(f'p2p:feeconfirm:{tid}', data)
        self.assertIn(f'p2p:adjudicate:fee:{tid}', data)

    async def test_done_without_payout_adjudication_hides_fee_button(self):
        tid = self.trade(p.ST_DONE, payout_id='never-proved')
        data = await self.hold_buttons(tid)
        self.assertNotIn(f'p2p:feeconfirm:{tid}', data)
        self.assertNotIn(f'p2p:adjudicate:fee:{tid}', data)

    async def test_invalidated_trade_shows_conflict_evidence_no_actions(self):
        tid = self.trade(p.ST_DONE, payout_id='conf-pid')
        p.record_escrow_event('SEND', 1000, 'conf-pid', 'COMPLETED')
        p.record_escrow_event('SEND', 999, 'conf-pid', 'COMPLETED')  # conflict
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_INVALIDATED)
        ev = await self.callback(f'p2p:admhold:{tid}')
        text = ev.edit.await_args.args[0]
        self.assertIn('conf-pid', text)          # conflicting evidence displayed
        data = flat(ev.edit.await_args.kwargs['buttons'])
        self.assertFalse([d for d in data if d.startswith('p2p:adjudicate:')])
        self.assertNotIn(f'p2p:feeconfirm:{tid}', data)

    # ---------- legacy path ----------

    async def test_legacy_hold_links_to_case_and_ack_flow(self):
        tid = self.trade(p.ST_DISPUTED, safety_version=0)
        data = await self.hold_buttons(tid)
        self.assertIn(f'p2p:admcase:{tid}', data)
        # case view offers release/refund; release routes through legacy ack
        ev = await self.callback(f'p2p:admcase:{tid}')
        self.assertIn(f'p2p:admrel:{tid}', flat(ev.edit.await_args.kwargs['buttons']))
        ev2 = await self.callback(f'p2p:admrel:{tid}')
        confirm = flat(ev2.edit.await_args.kwargs['buttons'])
        ack = [d for d in confirm if d.startswith('p2p:legacyack:release:')]
        self.assertEqual(len(ack), 1)
        ev3 = await self.callback(ack[0])
        ev3.respond.assert_awaited()
        self.assertEqual(self.mock_send.await_count, 1)
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_PAYOUT)  # completion stays evidence-bound
        self.assertEqual(len(p._rows('SELECT * FROM escrow_admin_actions WHERE trade_id=?', (tid,))), 1)

    async def test_legacy_ack_rejects_bad_nonce(self):
        tid = self.trade(p.ST_DISPUTED, safety_version=0)
        ev = await self.callback(f'p2p:legacyack:release:{tid}:not-a-real-nonce')
        ev.edit.assert_not_awaited()
        self.assertTrue(ev.answer.await_args.kwargs.get('alert'))
        self.assertEqual(p.get_trade(tid)['stage'], p.ST_DISPUTED)

    # ---------- fee confirmation tokens ----------

    async def test_feeconfirm_gates_and_one_use_token(self):
        tid = self.trade(p.ST_PAYOUT, payout_id='pay-tok')
        p.record_escrow_event('SEND', 1000, 'pay-tok', 'COMPLETED')
        self.assertEqual(p.admin_adjudicate(ADMIN, tid, 'payout'), 'settled')
        # already-attempted trade is rejected at the route too
        p._exec('UPDATE trades SET fee_tries=1 WHERE id=?', (tid,))
        ev = await self.callback(f'p2p:feeconfirm:{tid}')
        self.assertTrue(ev.answer.await_args.kwargs.get('alert'))
        ev.edit.assert_not_awaited()
        # fresh trade: confirmation screen with one-use nonce
        p._exec('UPDATE trades SET fee_tries=0 WHERE id=?', (tid,))
        ev2 = await self.callback(f'p2p:feeconfirm:{tid}')
        send = [d for d in flat(ev2.edit.await_args.kwargs['buttons'])
                if d.startswith('p2p:feesend:')]
        self.assertEqual(len(send), 1)
        # forged nonce refused; real one consumed exactly once
        ev3 = await self.callback(f'p2p:feesend:{tid}:forged')
        self.assertTrue(ev3.answer.await_args.kwargs.get('alert'))
        ev4 = await self.callback(send[0])
        ev4.respond.assert_awaited()
        ev5 = await self.callback(send[0])  # replay
        self.assertTrue(ev5.answer.await_args.kwargs.get('alert'))
        self.assertEqual(self.mock_send.await_count, 1)

    # ---------- events review ----------

    async def test_event_detail_and_evidence_review_flow(self):
        eid, _ = p.record_escrow_event('RECEIVE', 7, 'rev-pid', 'COMPLETED')
        ev = await self.callback(f'p2p:admevent:{eid}')
        self.assertIn('rev-pid', ev.edit.await_args.args[0])
        await self.callback(f'p2p:admreview:{eid}')
        ev_text = SimpleNamespace(sender_id=ADMIN, respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': str(ADMIN)}):
            self.assertTrue(await p.on_text(None, ev_text, 'external proof: bank receipt'))
        ev_text.respond.assert_awaited()
        row = p._one('SELECT * FROM escrow_events WHERE id=?', (eid,))
        self.assertEqual(row['review_note'], 'external proof: bank receipt')

    # ---------- ban / admin management flows ----------

    async def test_ban_flow_prompt_confirm_and_notify(self):
        await self.callback('p2p:adm:ban')
        ev = SimpleNamespace(sender_id=ADMIN, respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': str(ADMIN)}), btn_patch():
            self.assertTrue(await p.on_text(None, ev, '66 scammer'))
        kb = flat(ev.respond.await_args.kwargs['buttons'])
        self.assertIn('p2p:adm:banok:66', kb)
        await self.callback('p2p:adm:banok:66')
        self.assertTrue(p.is_banned(66))
        await self.callback('p2p:adm:unban:66')
        self.assertFalse(p.is_banned(66))

    async def test_ban_confirm_rejects_mismatched_target(self):
        p._wiz[f'adm:{ADMIN}'] = {'m': 'ban2', 'target': 66, 'reason': '', 'ts': p.time.time()}
        ev = await self.callback('p2p:adm:banok:99')
        self.assertTrue(ev.answer.await_args.kwargs.get('alert'))
        self.assertFalse(p.is_banned(99))
        self.assertFalse(p.is_banned(66))

    async def test_admadd_owner_only_and_add(self):
        with patch.object(p, 'is_p2p_admin', return_value=False), \
             patch.dict(os.environ, {'P2P_TEST_USERS': str(NON_ADMIN)}):
            ev = await self.callback('p2p:adm:admadd', sender=NON_ADMIN)
        self.assertTrue(ev.answer.await_args.kwargs.get('alert'))
        await self.callback('p2p:adm:admadd')
        ev = SimpleNamespace(sender_id=ADMIN, respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': str(ADMIN)}):
            self.assertTrue(await p.on_text(None, ev, '888'))
        self.assertIn(888, p.p2p_admin_ids())
        await self.callback('p2p:adm:admdel:888')
        self.assertNotIn(888, p.p2p_admin_ids())

    # ---------- labels: fa/en parity and distinct settle labels ----------

    def test_txt_fa_en_parity_and_distinct_labels(self):
        self.assertEqual(set(p.TXT['fa']), set(p.TXT['en']))
        for key in ('mark_settled_payout', 'mark_settled_refund', 'mark_settled_fee'):
            self.assertIn(key, p.TXT['fa'])
        labels = {p.TXT['en'][k] for k in
                  ('mark_settled_payout', 'mark_settled_refund', 'mark_settled_fee')}
        self.assertEqual(len(labels), 3)
        for key in ('admin_panel_btn', 'adm_panel_title', 'holds_panel', 'events_panel',
                    'send_fee', 'hold_open_case', 'hold_conflict_title'):
            self.assertTrue(p.TXT['fa'].get(key) and p.TXT['en'].get(key), key)


if __name__ == '__main__':
    unittest.main()
