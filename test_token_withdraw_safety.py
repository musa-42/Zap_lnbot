"""Offline regression tests; never import the bot module or touch its database."""
import ast
import asyncio
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
import logging
import sys
import breez_sdk_spark as bss
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('isolated_zap_tokens', ROOT / 'zap_tokens.py')
zt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zt)


def bot_functions(*names):
    tree = ast.parse((ROOT / 'zap_wallet_bot_v32.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names), [n.name for n in nodes]
    for node in nodes:
        node.decorator_list = []
    env = {'__builtins__': __builtins__, 'sqlite3': sqlite3, 'json': json, 'time_module': __import__('time'),
           'os': __import__('os'), 'secrets': __import__('secrets'),
           'get_user_lang': lambda uid: 'en'}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<isolated-bot-functions>', 'exec'), env)
    return env


def open_test_gate(db):
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS token_withdraw_migration (id INTEGER PRIMARY KEY, actor INTEGER, audited REAL)')
        conn.execute('INSERT OR IGNORE INTO token_withdraw_migration VALUES (1, 1, 0)')


class TokenTests(unittest.IsolatedAsyncioTestCase):
    def test_nonfinite_amounts_rejected(self):
        for amount in ('nan', 'inf', '-inf'):
            self.assertIsNone(zt.parse_human_amount(amount))

    async def test_unknown_route_does_not_fall_back(self):
        route = SimpleNamespace(asset='USDT', chain='base', decimals=6)
        sdk = SimpleNamespace(get_cross_chain_routes=AsyncMock(return_value=[route]),
                              prepare_send_payment=AsyncMock(), send_payment=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'no_routes'):
            await zt.prepare_withdraw(sdk, 'USDT', 'tron', 'address', 1000000, object())
        sdk.prepare_send_payment.assert_not_awaited()
        sdk.send_payment.assert_not_awaited()

    async def test_quote_sdk_fields_and_precise_units(self):
        env = bot_functions('token_wd_quote_totals')
        env.update(logging=logging, cross_chain_address_details=lambda d:d,
                   usd_to_sats=lambda value, rate:int(value * rate),
                   get_usd_rate=AsyncMock(return_value=100))
        route = SimpleNamespace(asset='USDT', chain='tron', decimals=6, provider=bss.CrossChainProvider.ORCHESTRA,
                                chain_id=None, contract_address='contract',
                                accepted_assets=[SimpleNamespace(asset=bss.SparkAsset.TOKEN(zt.USDB_TOKEN_ID))])
        method = SimpleNamespace(amount_in=2100000, fee_amount=100000,
                                 estimated_out=2000000, source_transfer_fee_sats=7,
                                 asset_amount_in=2100000, route=route, recipient_address='Tfake',
                                 fee_mode=bss.CrossChainFeeMode.FEES_INCLUDED)
        sdk = SimpleNamespace(get_cross_chain_routes=AsyncMock(return_value=[route]),
                              prepare_send_payment=AsyncMock(return_value=SimpleNamespace(payment_method=method,
                                  amount=2100000, token_identifier=zt.USDB_TOKEN_ID, conversion_estimate=None,
                                  fee_policy=bss.FeePolicy.FEES_INCLUDED)),
                              send_payment=AsyncMock(), receive_payment=AsyncMock())
        old = sys.modules.get('zap_tokens')
        sys.modules['zap_tokens'] = zt
        try:
            fee, total, quote = await env['token_wd_quote_totals'](sdk, 'USDT', 'tron', 'Tfake', 2, 3, object())
        finally:
            if old is None: del sys.modules['zap_tokens']
            else: sys.modules['zap_tokens'] = old
        self.assertEqual(fee, '0.1')
        self.assertIsNone(total)
        self.assertEqual((quote[8], quote[10]), (2000000, 7))
        sdk.send_payment.assert_not_awaited()
        sdk.receive_payment.assert_not_awaited()

    async def test_quote_never_converts_when_preparation_insufficient(self):
        route = SimpleNamespace(asset='USDT', chain='tron', decimals=6,
                                accepted_assets=[SimpleNamespace(asset=bss.SparkAsset.TOKEN(zt.USDB_TOKEN_ID))])
        sdk = SimpleNamespace(get_cross_chain_routes=AsyncMock(return_value=[route]),
                              get_info=AsyncMock(return_value=SimpleNamespace(token_balances={})),
                              prepare_send_payment=AsyncMock(side_effect=RuntimeError('insufficient funds')),
                              receive_payment=AsyncMock(), send_payment=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'insufficient'):
            await zt.prepare_withdraw(sdk, 'USDT', 'tron', 'T-address', 1000000, object())
        sdk.send_payment.assert_not_awaited()
        sdk.receive_payment.assert_not_awaited()

    async def test_send_exception_never_retries_or_converts(self):
        sdk = SimpleNamespace(send_payment=AsyncMock(side_effect=RuntimeError('insufficient funds: outcome unknown')),
                              get_info=AsyncMock(), receive_payment=AsyncMock(), prepare_send_payment=AsyncMock())
        with self.assertRaises(RuntimeError):
            await zt.execute_withdraw(sdk, object(), 100, object())
        self.assertEqual(sdk.send_payment.await_count, 1)
        sdk.receive_payment.assert_not_awaited()
        sdk.prepare_send_payment.assert_not_awaited()

    async def test_durable_claim_blocks_racing_and_ambiguous(self):
        env = bot_functions('token_withdraw_claim', 'token_withdraw_prepare_send', 'token_withdraw_finish')
        with tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch') as tmp:
            env['DB_PATH'] = str(pathlib.Path(tmp) / 'isolated.sqlite')
            open_test_gate(env['DB_PATH'])
            claim, finish = env['token_withdraw_claim'], env['token_withdraw_finish']
            results = await asyncio.gather(*(asyncio.to_thread(claim, 42, 'nonce') for _ in range(12)))
            self.assertEqual(sum(results), 1)
            env['token_withdraw_prepare_send'](42, 'nonce', 'q')
            finish(42, 'nonce', 'unknown', '')
            self.assertFalse(claim(42, 'different'))
            self.assertFalse(claim(42, 'nonce'))

    def test_restart_and_legacy_statuses_are_held(self):
        with tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch') as tmp:
            db = str(pathlib.Path(tmp) / 'isolated.sqlite')
            open_test_gate(db)
            for i, status in enumerate(('claimed', 'sending', 'pending', 'unknown', 'completed', 'failed', 'future')):
                first = bot_functions('token_withdraw_claim', 'token_withdraw_finish')
                first['DB_PATH'] = db
                self.assertTrue(first['token_withdraw_claim'](i, f'n{i}'))
                with sqlite3.connect(db) as conn:
                    conn.execute('UPDATE token_withdraw_attempts SET status=? WHERE nonce=?', (status, f'n{i}'))
                restarted = bot_functions('token_withdraw_claim')
                restarted['DB_PATH'] = db
                self.assertFalse(restarted['token_withdraw_claim'](i, f'new{i}'), status)
            self.assertTrue(first['token_withdraw_claim'](99, 'safe'))
            first['token_withdraw_finish'](99, 'safe', 'safe_failed', '')
            self.assertTrue(restarted['token_withdraw_claim'](99, 'retry'))

    async def test_migration_and_reconciliation_after_crash(self):
        from test_token_quote_identity import fixture
        names = ('token_withdraw_claim', 'token_withdraw_prepare_send', 'token_withdraw_admin',
                 'token_withdraw_migration_ack', 'token_withdraw_register_legacy', 'token_withdraw_reconcile', 'token_withdraw_notice')
        env = bot_functions(*names)
        env.update(logging=logging, get_wallet=AsyncMock(), close_wallet=AsyncMock(), client=SimpleNamespace(send_message=AsyncMock()))
        env['os'] = SimpleNamespace(getenv=lambda key, default='': '7' if key == 'DONATE_USER_ID' else default)
        with tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch') as tmp:
            env['DB_PATH'] = str(pathlib.Path(tmp) / 'test.sqlite')
            self.assertFalse(env['token_withdraw_claim'](42, 'n'))
            with self.assertRaises(PermissionError):
                env['token_withdraw_migration_ack'](8)
            legacy = env['token_withdraw_register_legacy'](7, 43)
            env['token_withdraw_migration_ack'](7)
            env['token_withdraw_migration_ack'](7)
            self.assertFalse(env['token_withdraw_claim'](43, 'new'))
            self.assertEqual(await env['token_withdraw_reconcile'](7, 43, legacy), 'manual_review')
            route, prep = fixture()
            quote = zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)
            self.assertTrue(env['token_withdraw_claim'](42, 'n', {'quote':quote}))
            env['token_withdraw_prepare_send'](42, 'n', 'quote-1')
            with sqlite3.connect(env['DB_PATH']) as conn:
                self.assertEqual(json.loads(conn.execute('SELECT details FROM token_withdraw_attempts WHERE nonce=?', ('n',)).fetchone()[0])['quote_id'], 'quote-1')
            ci = bss.ConversionInfo.ORCHESTRA('order', 'quote-1', 'SECRET_DO_NOT_PERSIST', route.chain,
                route.chain_id, route.asset, 'Ttest', quote[7], quote[8], None, None,
                bss.ConversionStatus.PENDING, None, None, None, None, route.decimals, route.contract_address)
            payment = SimpleNamespace(id='pay-1', payment_type=bss.PaymentType.SEND,
                status=bss.PaymentStatus.COMPLETED, details=SimpleNamespace(conversion_info=ci))
            sdk = SimpleNamespace(list_payments=AsyncMock(return_value=SimpleNamespace(payments=[payment])), get_payment=AsyncMock())
            env['get_wallet'].return_value = sdk
            old = sys.modules.get('zap_tokens'); sys.modules['zap_tokens'] = zt
            try:
                with self.assertRaises(PermissionError):
                    await env['token_withdraw_reconcile'](8, 42, 'n')
                self.assertEqual(await env['token_withdraw_reconcile'](7, 42, 'n'), 'manual_review')
                ci.status = bss.ConversionStatus.COMPLETED
                ci.external_tx_hash = 'tx'
                ci.delivered_amount = quote[8]
                self.assertEqual(await env['token_withdraw_reconcile'](7, 42, 'n'), 'manual_review')
                self.assertFalse(env['token_withdraw_claim'](42, 'n2'))
                with sqlite3.connect(env['DB_PATH']) as conn:
                    self.assertEqual(conn.execute('SELECT status FROM token_withdraw_attempts WHERE nonce=?', ('n',)).fetchone()[0], 'sending')
                self.assertNotIn(b'SECRET_DO_NOT_PERSIST', pathlib.Path(env['DB_PATH']).read_bytes())
            finally:
                if old is None: del sys.modules['zap_tokens']
                else: sys.modules['zap_tokens'] = old

    async def test_refund_and_wrong_identity_fail_closed(self):
        from test_token_quote_identity import fixture
        route, prep = fixture()
        quote = zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)
        ci = bss.ConversionInfo.ORCHESTRA('order', 'quote-1', 'private', route.chain, route.chain_id,
            route.asset, 'Ttest', quote[7], quote[8], None, None, bss.ConversionStatus.REFUND_NEEDED,
            None, None, None, None, route.decimals, route.contract_address)
        from test_token_critical_regression import payment as sdk_payment
        payment, quote = sdk_payment()
        ci = payment.details.conversion_info
        ci.status = bss.ConversionStatus.REFUND_NEEDED
        ci.external_tx_hash = None
        self.assertEqual(zt.withdrawal_evidence(payment, 'quote-1', quote), 'refund_needed')
        ci.status = bss.ConversionStatus.REFUNDED
        self.assertEqual(zt.withdrawal_evidence(payment, 'quote-1', quote), 'refunded')
        self.assertEqual(zt.withdrawal_evidence(payment, 'other-quote', quote), 'unknown')
        payment.payment_type = bss.PaymentType.RECEIVE
        self.assertEqual(zt.withdrawal_evidence(payment, 'quote-1', quote), 'unknown')
        payment.payment_type = bss.PaymentType.SEND
        ci.recipient_address = 'other'
        self.assertEqual(zt.withdrawal_evidence(payment, 'quote-1', quote), 'unknown')
        ci.recipient_address = 'Ttest'
        sdk = SimpleNamespace(list_payments=AsyncMock(return_value=SimpleNamespace(payments=[payment, payment])))
        self.assertIsNone(await zt.find_withdrawal_payment(sdk, 'quote-1'))
        sdk.get_payment = AsyncMock(return_value=SimpleNamespace(payment=payment))
        self.assertIsNone(await zt.find_withdrawal_payment(sdk, 'quote-1', 'p1'))
        self.assertIsNone(await zt.find_withdrawal_payment(sdk, 'quote-1', 'wrong'))
        sdk.get_payment.assert_not_awaited()

    def test_quote_id_extracted_before_send(self):
        context = bss.CrossChainProviderContext.ORCHESTRA('quote-1', 'deposit', 2)
        self.assertEqual(zt.withdraw_quote_id(SimpleNamespace(payment_method=SimpleNamespace(provider_context=context))), 'quote-1')
        with self.assertRaises(ValueError):
            zt.withdraw_quote_id(SimpleNamespace(payment_method=SimpleNamespace(provider_context=object())))

    async def test_status_command_only_shows_own_holds(self):
        env = bot_functions('token_status_handler', 'token_withdraw_claim', 'token_withdraw_notice')
        with tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch') as tmp:
            env['DB_PATH'] = str(pathlib.Path(tmp) / 'db')
            open_test_gate(env['DB_PATH'])
            self.assertTrue(env['token_withdraw_claim'](10, 'owner-ref'))
            self.assertTrue(env['token_withdraw_claim'](11, 'other-ref'))
            event = SimpleNamespace(sender_id=10, respond=AsyncMock())
            await env['token_status_handler'](event)
            msg = event.respond.await_args.args[0]
            self.assertIn('owner-ref', msg)
            self.assertNotIn('other-ref', msg)

    async def test_owner_command_rejects_unauthorized_actor(self):
        env = bot_functions('token_audit_handler')
        env['token_withdraw_admin'] = lambda uid: uid == 7
        event = SimpleNamespace(sender_id=8, raw_text='/token_audit attest_all_history_audited', respond=AsyncMock())
        await env['token_audit_handler'](event)
        event.respond.assert_not_awaited()

    def test_old_confirmation_callback_requires_nonce_and_claim(self):
        source = (ROOT / 'zap_wallet_bot_v32.py').read_text()
        self.assertTrue('token_withdraw_claim(' in source)
        self.assertTrue('tkwd_go:' in source)
        self.assertFalse('b"tkwd_go"' in source)

    async def test_actual_confirmation_path_statuses_and_replay(self):
        import breez_sdk_spark as bss
        tree = ast.parse((ROOT / 'zap_wallet_bot_v32.py').read_text())
        callback = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'callback_handler')
        original_try = next(n for n in callback.body if isinstance(n, ast.Try))
        branch = next(n for n in original_try.body if isinstance(n, ast.If) and
                      'tkwd_go:' in ast.unparse(n.test))
        fn = ast.AsyncFunctionDef(name='isolated_callback', args=ast.arguments(posonlyargs=[],
            args=[ast.arg(arg='event'), ast.arg(arg='user_id'), ast.arg(arg='data')],
            kwonlyargs=[], kw_defaults=[], defaults=[]), body=[branch], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        for outcome in ('completed', 'pending', 'failed', 'unknown', 'exception', 'changed', 'provider', 'contract'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(dir='/root/.hermes/cache/scratch') as tmp:
                env = bot_functions('token_withdraw_claim', 'token_withdraw_prepare_send', 'token_withdraw_finish', 'token_withdraw_notice', 'token_withdraw_auth_binding')
                env.update(DB_PATH=str(pathlib.Path(tmp) / 'test.sqlite'), logging=logging,
                    PaymentStatus=bss.PaymentStatus, has_send_password=lambda uid: False, user_steps={42: 'token_wd_confirm'},
                    user_data={42: {'tk_asset':'USDT', 'tk_chain':'tron', 'tk_address':'Tfake',
                                    'tk_amount':2.0, 'tk_dest_details':{}, 'tk_confirm_nonce':'abc',
                                    'tk_quote':('USDT', 'tron', 'Tfake', 2.0, 6, True, 2000000, 500, 2000000, 10)}},
                    t=lambda uid, key, **kwargs: key,
                    Button=SimpleNamespace(inline=lambda *a: a),
                    cross_chain_address_details=lambda details: object(),
                    get_wallet=AsyncMock(return_value=object()),
                    client=SimpleNamespace(send_message=AsyncMock()), sweep_service_fee_sats=AsyncMock())
                open_test_gate(env['DB_PATH'])
                from test_token_quote_identity import fixture
                confirmed_route, confirmed_prep = fixture()
                env['user_data'][42]['tk_quote'] = zt.withdraw_quote(confirmed_prep, confirmed_route, 'Ttest', zt.USDB_TOKEN_ID)
                env['user_data'][42]['tk_address'] = 'Ttest'
                route, prep = fixture()
                if outcome == 'provider': route.provider = bss.CrossChainProvider.BOLTZ
                if outcome == 'contract': route.contract_address = 'different'
                if outcome == 'changed': prep.payment_method.fee_amount += 1
                spark_asset = SimpleNamespace(is_TOKEN=lambda: True)
                entry = {'asset':'USDT','chain':'tron','decimals':6, 'pair':route}
                fake = SimpleNamespace(get_routes=AsyncMock(return_value=[]), pick_routes=lambda routes, asset:[entry],
                    to_base_units=lambda n, d:int(n*10**d),
                    USDB_TOKEN_ID=zt.USDB_TOKEN_ID,
                    withdraw_quote=zt.withdraw_quote,
                    withdraw_quote_id=lambda prep:'quote-1',
                    withdraw_source_outcome=zt.withdraw_source_outcome,
                    prepare_withdraw=AsyncMock(return_value=(prep,entry,spark_asset,2100000,object())),
                    from_base_units=lambda raw, dec:raw/10**dec, fmt_amount=str)
                statuses = {'completed':bss.PaymentStatus.COMPLETED, 'pending':bss.PaymentStatus.PENDING,
                            'failed':bss.PaymentStatus.FAILED, 'unknown':None}
                fake.execute_withdraw = AsyncMock(side_effect=RuntimeError('timeout') if outcome == 'exception' else None,
                    return_value=SimpleNamespace(status=statuses.get(outcome), id='payment-id',
                        details=SimpleNamespace(conversion_info=SimpleNamespace(status=bss.ConversionStatus.PENDING))))
                event = SimpleNamespace(is_private=True, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
                exec(compile(module, '<isolated-callback>', 'exec'), env)
                old = sys.modules.get('zap_tokens')
                sys.modules['zap_tokens'] = fake
                try:
                    await asyncio.gather(*(env['isolated_callback'](event, 42, 'tkwd_go:abc') for _ in range(2)))
                finally:
                    if old is None:
                        del sys.modules['zap_tokens']
                    else:
                        sys.modules['zap_tokens'] = old
                self.assertEqual(fake.execute_withdraw.await_count, 0 if outcome in ('changed', 'provider', 'contract') else 1)
                with sqlite3.connect(env['DB_PATH']) as conn:
                    saved = conn.execute('SELECT status FROM token_withdraw_attempts').fetchone()[0]
                expected = {'exception':'sending', 'changed':'safe_failed', 'provider':'safe_failed', 'contract':'safe_failed', 'completed':'pending', 'pending':'pending',
                            'failed':'unknown', 'unknown':'unknown'}[outcome]
                self.assertEqual(saved, expected)
                if outcome not in ('changed', 'provider', 'contract'):
                    sent = env['client'].send_message.await_args.args[1]
                    if outcome in ('completed', 'pending'): self.assertIn('not confirmed', sent)
                    else: self.assertIn('unknown', sent)


if __name__ == '__main__':
    unittest.main()
