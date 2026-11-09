"""Offline SDK-shaped token withdrawal regressions; no live bot imports."""
import unittest
import ast
import logging
import pathlib
import sys
import tempfile
from unittest.mock import Mock, patch
from test_token_withdraw_safety import bot_functions, open_test_gate
from test_p2p_secret_dispatch import handler_env, event as message_event
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import breez_sdk_spark as b
import zap_tokens as z
from test_token_quote_identity import fixture


def payment():
    route, prep = fixture()
    quote = z.withdraw_quote(prep, route, 'Ttest', z.USDB_TOKEN_ID)
    ci = b.ConversionInfo.ORCHESTRA('order', 'quote-1', 'SECRET', route.chain,
        route.chain_id, route.asset, 'Ttest', quote[7], quote[8], quote[8], 'external',
        b.ConversionStatus.COMPLETED, None, None, None, None,
        route.decimals, route.contract_address)
    metadata = b.TokenMetadata(identifier=z.USDB_TOKEN_ID, issuer_public_key='issuer',
        name='USDB', ticker='USDB', decimals=6, max_supply=1, is_freezable=False)
    details = b.PaymentDetails.TOKEN(metadata, 'tx', b.TokenTransactionType.TRANSFER, None, ci)
    p = b.Payment(id='p1', payment_type=b.PaymentType.SEND, status=b.PaymentStatus.COMPLETED,
        amount=quote[3], fees=0, timestamp=1, method=b.PaymentMethod.TOKEN,
        details=details, conversion_details=None)
    return p, quote


class Critical(unittest.IsolatedAsyncioTestCase):
    def test_source_token_and_debit(self):
        p, quote = payment()
        self.assertEqual(z.withdrawal_evidence(p, 'quote-1', quote), 'delivered')
        p.amount += 1
        self.assertEqual(z.withdrawal_evidence(p, 'quote-1', quote), 'unknown')
        p.amount -= 1
        p.details.metadata.identifier = 'other'
        self.assertEqual(z.withdrawal_evidence(p, 'quote-1', quote), 'unknown')

    async def test_known_id_does_not_skip_duplicate_scan(self):
        p, _ = payment()
        p2, _ = payment(); p2.id = 'p2'
        sdk = NS(get_payment=AsyncMock(return_value=NS(payment=p)),
                 list_payments=AsyncMock(return_value=NS(payments=[p, p2])))
        self.assertIsNone(await z.find_withdrawal_payment(sdk, 'quote-1', 'p1'))

    async def test_short_history_without_completeness_proof_is_not_exhaustive(self):
        p, _ = payment()
        sdk = NS(list_payments=AsyncMock(return_value=NS(payments=[p])))
        self.assertIsNone(await z.find_withdrawal_payment(sdk, 'quote-1'))

class Auth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch')
        self.addCleanup(self.tmp.cleanup)
        env = bot_functions('token_withdraw_claim', 'token_withdraw_prepare_send',
            'token_withdraw_finish', 'token_withdraw_notice', 'token_withdraw_auth_binding')
        env.update(DB_PATH=str(pathlib.Path(self.tmp.name) / 'db'), logging=logging,
            user_steps={1:'token_wd_confirm'}, user_data={1:{'tk_confirm_nonce':'nonce',
            'tk_asset':'USDT', 'tk_chain':'tron', 'tk_address':'Ttest', 'tk_amount':2,
            'tk_dest_details':{}, 'tk_quote':['quote', 2]}},
            has_send_password=lambda uid: True, get_user_send_password_hash=lambda uid:'hash',
            t=lambda uid,key,**kw:key, Button=NS(inline=lambda *a:a))
        open_test_gate(env['DB_PATH'])
        tree = ast.parse((pathlib.Path(__file__).parent / 'zap_wallet_bot_v32.py').read_text())
        cb = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callback_handler')
        branch = next(n for n in next(n for n in cb.body if isinstance(n, ast.Try)).body
                      if isinstance(n, ast.If) and 'tkwd_go:' in ast.unparse(n.test))
        fn = ast.AsyncFunctionDef(name='run', args=ast.arguments(posonlyargs=[],
            args=[ast.arg(arg='event'),ast.arg(arg='user_id'),ast.arg(arg='data')],
            kwonlyargs=[],kw_defaults=[],defaults=[]), body=[branch], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])), '<callback>', 'exec'), env)
        self.env = env
        self.ev = NS(is_private=True, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())

    async def callback(self):
        await self.env['run'](self.ev, 1, 'tkwd_go:nonce')

    async def test_private_password_and_no_relay(self):
        await self.callback()
        self.assertEqual(self.env['user_steps'][1], 'verify_send_password_for_token')
        env = handler_env('verify_send_password_for_token')
        env.update(user_data=self.env['user_data'], user_steps=self.env['user_steps'],
            token_withdraw_auth_binding=self.env['token_withdraw_auth_binding'],
            has_send_password=lambda uid: True, get_user_send_password_hash=lambda uid:'hash')
        env['verify_send_password'] = Mock(return_value=True)
        p2p = NS(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
        ev = message_event('secret')
        with patch.dict(sys.modules, {'zap_p2p':p2p}):
            await env['message_handler'](ev)
        p2p.on_text.assert_not_awaited(); p2p.on_media.assert_not_awaited()
        env['delete_password_message'].assert_awaited_once_with(ev)
        self.assertEqual(env['user_steps'][1], 'token_wd_confirm')
        self.assertIn('tkwd_go:nonce', repr(ev.respond.await_args.kwargs['buttons']))
        self.assertEqual(env['user_data'][1]['tk_auth_verified'],
                         env['token_withdraw_auth_binding'](1, env['user_data'][1]))
        # A destination mutation invalidates the verified capability before claim.
        env['user_data'][1]['tk_address'] = 'attacker'
        await self.callback()
        self.assertEqual(env['user_steps'][1], 'verify_send_password_for_token')

    async def test_group_and_replay_fail(self):
        self.ev.is_private = False
        await self.callback()
        self.assertEqual(self.env['user_steps'][1], 'token_wd_confirm')
        self.ev.is_private = True
        await self.callback()
        await self.callback()
        self.assertEqual(self.ev.edit.await_count, 1)

    async def test_wrong_password_and_media_never_relay(self):
        await self.callback()
        env = handler_env('verify_send_password_for_token')
        env.update(user_data=self.env['user_data'], user_steps=self.env['user_steps'],
            token_withdraw_auth_binding=self.env['token_withdraw_auth_binding'],
            has_send_password=lambda uid: True, get_user_send_password_hash=lambda uid:'hash')
        p2p = NS(on_text=AsyncMock(return_value=True), on_media=AsyncMock(return_value=True))
        with patch.dict(sys.modules, {'zap_p2p':p2p}):
            await env['message_handler'](message_event('secret', media=True))
            await env['message_handler'](message_event('wrong'))
            await env['message_handler'](message_event('retry secret'))
        p2p.on_text.assert_not_awaited(); p2p.on_media.assert_not_awaited()
        self.assertNotIn('tk_auth_verified', env['user_data'][1])
        self.assertEqual(env['user_steps'][1], 'verify_send_password_for_token')

if __name__ == '__main__': unittest.main()
