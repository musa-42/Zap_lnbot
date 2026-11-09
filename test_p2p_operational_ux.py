"""Offline P2P policy, media and durable wizard regression tests."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_p2p_escrow_safety as base
import test_p2p_secret_dispatch as dispatch

p = base.p


class Operational(base.Safety):
    async def test_boot_and_user_gates_agree(self):
        for raw, permitted, active in [(None, True, True), ('', True, True), (' ALL ', True, True),
                                       ('1,2', True, True), ('2', False, True),
                                       ('garbage', False, False), ('all,garbage', False, False)]:
            with self.subTest(raw=raw):
                env = {} if raw is None else {'P2P_TEST_USERS': raw}
                with patch.dict(os.environ, env, clear=True), patch.object(p.os.path, 'exists', return_value=True), \
                     patch.object(p, 'escrow_sdk', new_callable=AsyncMock) as sdk, \
                     patch.object(p, 'startup_scan', new_callable=AsyncMock) as scan:
                    self.assertEqual(p.enabled_for(1), permitted)
                    await p.boot(None)
                    self.assertEqual(sdk.await_count, int(active))
                    self.assertEqual(scan.await_count, int(active))

    async def test_photo_adapter_and_inactive_gate(self):
        ev = dispatch.event('', True)
        client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
        tid = self.trade(p.ST_LOCKED, chat_open=1, buyer=1, seller=2)
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
            self.assertTrue(await p.on_media(client, ev))
            client.send_file.assert_awaited_once_with(2, ev.photo, caption=f'💬 #{tid}')
            ev.sender_id = 3
            self.assertFalse(await p.on_media(client, ev))
        self.assertEqual(client.send_file.await_count, 1)

    async def test_photo_requires_allowed_user_open_chat_and_supported_stage(self):
        ev = dispatch.event('', True)
        client = SimpleNamespace(send_file=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
            self.assertFalse(await p.on_media(client, ev))
            tid = self.trade(p.ST_LOCKED, chat_open=0, buyer=1, seller=2)
            self.assertFalse(await p.on_media(client, ev))
            p._exec('UPDATE trades SET chat_open=1, stage=? WHERE id=?', (p.ST_CANCELED, tid))
            self.assertFalse(await p.on_media(client, ev))
            p._exec('UPDATE trades SET stage=? WHERE id=?', (p.ST_LOCKED, tid))
            p._exec('INSERT INTO banned (uid) VALUES (?)', (1,))
            self.assertFalse(await p.on_media(client, ev))
            p._exec('DELETE FROM banned WHERE uid=?', (1,))
            self.assertTrue(await p.on_media(client, ev))
        client.send_file.assert_awaited_once()

    async def test_boot_with_missing_seed_does_not_connect(self):
        with patch.dict(os.environ, {'P2P_TEST_USERS': 'all'}, clear=True), \
             patch.object(p.os.path, 'exists', return_value=False), \
             patch.object(p, 'escrow_sdk', new_callable=AsyncMock) as sdk, \
             patch.object(p, 'startup_scan', new_callable=AsyncMock) as scan:
            await p.boot(None)
        sdk.assert_not_awaited()
        scan.assert_not_awaited()

    async def test_real_handler_sensitive_media_and_active_photo(self):
        self.trade(p.ST_LOCKED, chat_open=1, buyer=1, seller=2)
        for step in dispatch.SENSITIVE:
            env = dispatch.handler_env(step)
            ev = dispatch.event('', True)
            env['client'].send_file = AsyncMock()
            with patch.dict('sys.modules', {'zap_p2p': p}), patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
                await env['message_handler'](ev)
            env['client'].send_file.assert_not_awaited()
            env['client'].send_message.assert_not_awaited()
        env = dispatch.handler_env(None)
        env['client'].send_file = AsyncMock()
        with patch.dict('sys.modules', {'zap_p2p': p}), patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
            await env['message_handler'](dispatch.event('', True))
        env['client'].send_file.assert_awaited_once()

    async def test_non_photo_media_is_not_relayed_as_empty_text(self):
        self.trade(p.ST_LOCKED, chat_open=1, buyer=1, seller=2)
        ev = dispatch.event('', False)
        ev.document = object()
        client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
            self.assertFalse(await p.on_media(client, ev))
            self.assertFalse(await p.on_text(client, ev, ''))
        client.send_file.assert_not_awaited()
        client.send_message.assert_not_awaited()

    async def test_wizard_each_step_survives_cache_clear(self):
        ev = SimpleNamespace(sender_id=1, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True), \
             patch.object(p, 'bot_fiat_rate', new_callable=AsyncMock, return_value=(100000, 'IRT')):
            await p.wiz_start(None, 1, edit_event=ev)
            async def callback(data, expected):
                await p.on_callback(None, ev, data)
                p._mem_wiz.clear()
                self.assertEqual(p._wiz.get(1)['step'], expected)
            await callback('p2p:side:sell', 'unit')
            await callback('p2p:unit:sats', 'amount')
            self.assertTrue(await p.on_text(None, ev, '50000'))
            p._mem_wiz.clear()
            self.assertEqual(p._wiz.get(1)['step'], 'pmode')
            await callback('p2p:pmode:float', 'premium')
            await callback('p2p:prem:1', 'method')
            await callback('p2p:skipmethod', 'ttl')
            await callback('p2p:ttl:43200', 'ttl')
            self.assertEqual(p._wiz.get(1)['ttl_secs'], 43200)

    async def test_pmode_and_units_both_branches_persist(self):
        ev = SimpleNamespace(sender_id=1, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True), \
             patch.object(p, 'bot_fiat_rate', new_callable=AsyncMock, return_value=(100000, 'IRT')):
            for initial, action, expected in [({'step': 'unit'}, 'p2p:unit:toman', 'fiat_amount'),
                    ({'step': 'pmode', 'unit': 'toman', 'fiat_amount': 500}, 'p2p:pmode:fixed', 'sats_price'),
                    ({'step': 'pmode', 'unit': 'sats'}, 'p2p:pmode:fixed', 'price'),
                    ({'step': 'pmode', 'unit': 'toman', 'fiat_amount': 500}, 'p2p:pmode:float', 'premium'),
                    ({'step': 'pmode'}, 'p2p:pmode:float', 'fiat_amount')]:
                with self.subTest(action=action, initial=initial):
                    p._wiz[1] = initial
                    await p.on_callback(None, ev, action)
                    p._mem_wiz.clear()
                    self.assertEqual(p._wiz[1]['step'], expected)

    async def test_wizard_survives_database_reopen(self):
        ev = SimpleNamespace(sender_id=1, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True), \
             patch.object(p, 'bot_fiat_rate', new_callable=AsyncMock, return_value=(100000, 'IRT')):
            await p.wiz_start(None, 1, edit_event=ev)
            await p.on_callback(None, ev, 'p2p:side:sell')
            await p.on_callback(None, ev, 'p2p:unit:sats')
            await p.on_text(None, ev, '50000')
            p._db.close()
            p._db = None
            p._mem_wiz.clear()
            p.init_db()
            self.assertEqual((p._wiz[1]['step'], p._wiz[1]['sats']), ('pmode', 50000))

    async def test_cancel_during_rate_lookup_does_not_resurrect(self):
        ev = SimpleNamespace(sender_id=1, respond=AsyncMock())
        p._wiz[1] = {'step': 'fiat_amount', 'unit': 'toman'}
        async def rate(*args):
            p._wiz.pop(1, None)
            return (100000, 'IRT')
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True), \
             patch.object(p, 'bot_fiat_rate', side_effect=rate):
            await p.on_text(None, ev, '500')
        p._mem_wiz.clear()
        self.assertIsNone(p._wiz.get(1))

    async def test_cancel_during_text_never_resurrects_wizard(self):
        p._wiz[1] = {'step': 'amount'}
        ev = SimpleNamespace(sender_id=1, respond=AsyncMock())
        async def cancel(*args, **kwargs):
            p._wiz.pop(1, None)
        ev.respond.side_effect = cancel
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
            await p.on_text(None, ev, '50000')
        p._mem_wiz.clear()
        self.assertIsNone(p._wiz.get(1))

    async def test_text_steps_persist_even_when_response_fails(self):
        ev = SimpleNamespace(sender_id=1, respond=AsyncMock(side_effect=RuntimeError('offline')))
        cases = [({'step': 'amount'}, '50000', 'pmode'),
                 ({'step': 'sats_price'}, '50000', 'method'),
                 ({'step': 'price'}, 'fair price', 'method'),
                 ({'step': 'fiat_amount', 'unit': 'sats'}, '500', 'premium'),
                 ({'step': 'method'}, 'bank', 'ttl')]
        with patch.dict(os.environ, {'P2P_TEST_USERS': '1'}, clear=True):
            for initial, text, expected in cases:
                with self.subTest(initial=initial):
                    p._wiz[1] = initial
                    with self.assertRaises(RuntimeError):
                        await p.on_text(None, ev, text)
                    p._mem_wiz.clear()
                    self.assertEqual(p._wiz[1]['step'], expected)

if __name__ == '__main__':
    unittest.main()
