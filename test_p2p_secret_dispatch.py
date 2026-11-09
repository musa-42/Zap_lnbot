"""Offline dispatch regressions: execute only the handler AST, never import the bot."""
import ast
import logging
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = pathlib.Path(__file__).resolve().parent
SENSITIVE = (
    'set_send_password', 'set_new_send_password', 'verify_old_send_password',
    'confirm_remove_send_password', 'verify_password_for_backup',
    'verify_send_password_for_payment', 'verify_send_password_for_ffio',
    'set_password_hint', 'set_password_hint_after_password', 'recovery_input',
)


def handler_env(step):
    tree = ast.parse((ROOT / 'zap_wallet_bot_v32.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
    fn.decorator_list = []
    env = dict(__builtins__=__builtins__, user_steps={1: step} if step else {},
               user_data={1: {}}, pending_ffio_orders={}, client=types.SimpleNamespace(
                   send_message=AsyncMock(), edit_message=AsyncMock()),
               mark_user_active=AsyncMock(), delete_password_message=AsyncMock(),
               set_password_hint=Mock(), remove_password_hint=Mock(), get_password_hint=Mock(return_value=None),
               set_user_send_password=Mock(),
               verify_send_password=Mock(return_value=False),
               Button=types.SimpleNamespace(inline=lambda label, data: (label, data)),
               t=lambda uid, key: key, logging=logging,
               _looks_like_payment_input=lambda text: False,
               show_main_menu=AsyncMock(), format_amount_with_usd=AsyncMock(return_value='0'))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<isolated-handler>', 'exec'), env)
    return env


def event(text='dummy input', media=False):
    return types.SimpleNamespace(sender_id=1, chat_id=1, text=text,
        photo=object() if media else None, document=None, audio=None, video=None, voice=None,
        respond=AsyncMock(), download_media=AsyncMock(), delete=AsyncMock())


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_inputs_never_reach_p2p_and_hit_wallet(self):
        for step in SENSITIVE:
            with self.subTest(step=step):
                env = handler_env(step)
                p2p = types.SimpleNamespace(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
                ev = event('x')
                with patch.dict(sys.modules, {'zap_p2p': p2p}):
                    await env['message_handler'](ev)
                p2p.on_text.assert_not_awaited()
                p2p.on_media.assert_not_awaited()
                self.assertTrue(ev.respond.await_count or env['client'].send_message.await_count
                                or env['client'].edit_message.await_count, step)
                if step.startswith('verify_') or step == 'confirm_remove_send_password':
                    env['verify_send_password'].assert_called_once_with(1, 'x')
                    env['delete_password_message'].assert_awaited_once_with(ev)
                elif step in ('set_password_hint', 'set_password_hint_after_password'):
                    env['set_password_hint'].assert_called_once_with(1, 'x')
                elif step == 'recovery_input':
                    self.assertIn('Invalid recovery phrase', ev.respond.await_args.args[0])

    async def test_valid_new_password_stays_in_wallet(self):
        for step in ('set_send_password', 'set_new_send_password'):
            with self.subTest(step=step):
                env = handler_env(step)
                p2p = types.SimpleNamespace(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
                ev = event('test password')
                with patch.dict(sys.modules, {'zap_p2p': p2p}):
                    await env['message_handler'](ev)
                env['set_user_send_password'].assert_called_once_with(1, 'test password')
                env['delete_password_message'].assert_awaited_once_with(ev)
                p2p.on_text.assert_not_awaited()
                self.assertEqual(env['user_steps'].get(1), 'set_password_hint_after_password' if step == 'set_send_password' else None)

    async def test_sensitive_media_never_relayed_or_consumed(self):
        for step in SENSITIVE:
            for caption in (None, '', 'dummy input'):
                with self.subTest(step=step, caption=caption):
                    env = handler_env(step)
                    p2p = types.SimpleNamespace(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
                    ev = event(caption, media=True)
                    with patch.dict(sys.modules, {'zap_p2p': p2p}):
                        await env['message_handler'](ev)
                    p2p.on_text.assert_not_awaited()
                    p2p.on_media.assert_not_awaited()
                    ev.download_media.assert_not_awaited()
                    self.assertEqual(env['user_steps'][1], step)
                    env['verify_send_password'].assert_not_called()

    async def test_sensitive_other_media_never_relayed(self):
        env = handler_env('verify_send_password_for_payment')
        ev = event('caption')
        ev.media = object()
        p2p = types.SimpleNamespace(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
        with patch.dict(sys.modules, {'zap_p2p': p2p}):
            await env['message_handler'](ev)
        p2p.on_text.assert_not_awaited()
        env['verify_send_password'].assert_not_called()
        self.assertEqual(env['user_steps'][1], 'verify_send_password_for_payment')

    async def test_normal_p2p_text_and_media_still_dispatch(self):
        for text, media, which in [('hello', False, 'on_text'), ('receipt', True, 'on_text'),
                                   ('', True, 'on_media')]:
            with self.subTest(text=text, media=media):
                env = handler_env(None)
                p2p = types.SimpleNamespace(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
                with patch.dict(sys.modules, {'zap_p2p': p2p}):
                    await env['message_handler'](event(text, media))
                getattr(p2p, which).assert_awaited_once()

    async def test_verified_password_still_requires_payment_confirmation(self):
        for step in ('verify_send_password_for_payment', 'verify_send_password_for_ffio'):
            with self.subTest(step=step):
                env = handler_env(step)
                env['verify_send_password'].return_value = True
                if step.endswith('ffio'):
                    env['user_data'][1]['pending_ffio_order_id'] = 'fake-order'
                    env['pending_ffio_orders'][1] = {'id': 'fake-order'}
                p2p = types.SimpleNamespace(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
                with patch.dict(sys.modules, {'zap_p2p': p2p}):
                    await env['message_handler'](event('dummy password'))
                p2p.on_text.assert_not_awaited()
                kwargs = env['client'].send_message.await_args.kwargs
                labels = repr(kwargs['buttons'])
                self.assertIn('ffio_pay_fake-order' if step.endswith('ffio') else 'confirm_payment_yes', labels)

    def test_media_without_text_passes_event_filter(self):
        tree = ast.parse((ROOT / 'zap_wallet_bot_v32.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'message_handler')
        call = fn.decorator_list[0].args[0]
        predicate = next(k.value for k in call.keywords if k.arg == 'func')
        predicate = eval(compile(ast.Expression(predicate), '<event-filter>', 'eval'))
        self.assertTrue(predicate(types.SimpleNamespace(is_private=True, text=None)))
        self.assertFalse(predicate(types.SimpleNamespace(is_private=True, text='/start')))
        self.assertFalse(predicate(types.SimpleNamespace(is_private=False, text='hello')))

if __name__ == '__main__':
    unittest.main()
