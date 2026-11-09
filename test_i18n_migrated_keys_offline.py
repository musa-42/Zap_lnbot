"""Item 22 golden pins: wallet-core literals migrated to t() keys.

Owner decision #8: items 22-24 migrate hardcoded strings with byte-identical
values in ALL language packs (translations are a separate UI/UX pass). These
pins hold that contract:

- every migrated key exists in every pack with the exact EN bytes,
- v32 references every migrated key, and
- the raw inline literals are gone from v32 — except the copies inside the
  C1-frozen /zap + tip functions, which stay inline and are count-pinned.

UI/UX pass update (decision #8 lifted): the FA pack now carries real Persian
for the item-22 keys and the item-24 token codes; EN carries human wording for
the codes; other packs dropped the code keys (EN fallback). Pins follow that
layout.
"""
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent

MIGRATED = {
    'hist_loading': '📜 Loading history...',
    'insuff_after_fee_reserve': '❌ Insufficient balance after fee reserve.\n\n💰 Balance: **{balance} sats**',
    'insuff_withdraw': 'Insufficient balance for withdrawal.',
    'insuff_after_fees': 'Insufficient balance after fees',
    'insuff_generic': 'Insufficient balance.',
    'amount_gt0': 'Amount must be greater than 0',
    'amount_gt0_wd': '❌ Amount must be greater than 0\n\nTry again or cancel:',
    'err_no_sender': '❌ Could not identify sender.',
    'balance_anon_text': '💰 **Your Balance:** {balance}',
    'balance_check_prompt': '💰 **Check Your Balance**\n\nClick the button below to see your wallet balance.\n⚠️ Only you can see your balance.',
    'balance_reveal': '💰 Your Balance: {balance}',
    'dep_min_note': '⬇️ حداقل واریز: **{amount} دلار**{sats}',
    'dep_max_note': '⬆️ حداکثر واریز: **{amount} دلار**{sats}',
}

# UI/UX pass: FA translations of the English item-22 keys (dep_* were already
# Persian in every pack and stay pinned in MIGRATED above).
MIGRATED_FA = {
    'hist_loading': '📜 در حال آوردن تاریخچه...',
    'insuff_after_fee_reserve': '❌ بعد از کارمزد و ذخیره، موجودی کافی نمی‌مونه.\n\n💰 موجودی: **{balance} ساتوشی**',
    'insuff_withdraw': 'موجودی برای برداشت کافی نیست.',
    'insuff_after_fees': 'موجودی بعد از کارمزدها کافی نیست.',
    'insuff_generic': 'موجودی کافی نیست.',
    'amount_gt0': 'مبلغ باید بیشتر از صفر باشه',
    'amount_gt0_wd': '❌ مبلغ باید بیشتر از صفر باشه\n\nدوباره امتحان کن یا لغو کن:',
    'err_no_sender': '❌ نتونستم فرستنده رو شناسایی کنم.',
    'balance_anon_text': '💰 **موجودی تو:** {balance}',
    'balance_check_prompt': '💰 **چک موجودی**\n\nدکمه زیر رو بزن تا موجودی کیف پولت رو ببینی.\n⚠️ فقط خودت می‌تونی موجودیت رو ببینی.',
    'balance_reveal': '💰 موجودی تو: {balance}',
}

V32 = (ROOT / 'zap_wallet_bot_v32.py').read_text()


def packs():
    ns = {}
    exec(compile((ROOT / 'zap_i18n.py').read_text(), 'zap_i18n.py', 'exec'), ns)
    return ns['TRANSLATIONS']


class MigratedKeysGolden(unittest.TestCase):
    def test_keys_pinned_per_pack(self):
        for lang, pack in packs().items():
            expected = MIGRATED_FA if lang == 'fa' else MIGRATED
            for key, val in expected.items():
                self.assertEqual(pack.get(key), val, f'{lang}:{key}')

    def test_v32_references_every_migrated_key(self):
        for key in MIGRATED:
            self.assertIn(f'"{key}"', V32, key)

    def test_raw_literals_gone_from_v32(self):
        gone = [
            '"Amount must be greater than 0"',
            'Insufficient balance after fee reserve',
            '"Insufficient balance for withdrawal."',
            '"Insufficient balance after fees"',
            '"Insufficient balance."',
            'Loading history',
            'Check Your Balance',
            'Your Balance: ',
            'حداقل واریز',
            'حداکثر واریز',
        ]
        for lit in gone:
            self.assertNotIn(lit, V32, lit)

    def test_frozen_copies_stay_inline(self):
        # tip_handler is C1-frozen: its own guards keep their inline literals.
        self.assertEqual(V32.count('await event.reply("❌ Could not identify sender.")'), 1)
        self.assertEqual(V32.count('await event.reply("❌ Amount must be greater than 0")'), 1)


# UI/UX pass: zap_tokens still raises the same codes (TOK_RAISE_CODES), but
# packs now carry human wording — EN below, FA in TOK_ERR_KEYS_FA; every other
# pack dropped the keys and falls back to EN.
TOK_RAISE_CODES = {
    'tok_no_routes': 'no_routes',
    'tok_conversion_unconfirmed': 'conversion not confirmed',
    'tok_invalid_amount': 'invalid amount',
    'tok_route_no_usdb': 'route does not accept USDB',
    'tok_unknown_provider': 'unknown provider',
    'tok_route_identity': 'incomplete route identity',
    'tok_route_changed': 'prepared route or destination changed',
    'tok_unexpected_funding': 'unexpected funding or conversion',
    'tok_invalid_quote': 'invalid quote amounts',
    'tok_unsupported_provider': 'unsupported withdrawal provider',
    'tok_missing_correlation': 'missing quote correlation',
}

TOK_ERR_KEYS = {
    'tok_no_routes': 'No route available right now. Try again in a few minutes.',
    'tok_conversion_unconfirmed': 'Conversion not confirmed yet. Check your balance before retrying.',
    'tok_invalid_amount': 'Invalid amount. Send a number like 10 or 10.5.',
    'tok_route_no_usdb': 'This route does not accept USDB. Pick another network.',
    'tok_unknown_provider': 'Unknown provider on this route. Try again or pick another network.',
    'tok_route_identity': 'Route details incomplete. Try again.',
    'tok_route_changed': 'Route or destination changed while preparing. Start again.',
    'tok_unexpected_funding': 'Unexpected funding or conversion in the quote. Start again.',
    'tok_invalid_quote': 'Quote amounts are invalid. Start again.',
    'tok_unsupported_provider': 'This withdrawal provider is not supported. Start again.',
    'tok_missing_correlation': 'Quote reference missing. Start again.',
}

TOK_ERR_KEYS_FA = {
    'tok_no_routes': 'فعلاً مسیری در دسترس نیست. چند دقیقه دیگه دوباره امتحان کن.',
    'tok_conversion_unconfirmed': 'تبدیل هنوز تأیید نشده. قبل از تلاش دوباره موجودیت رو چک کن.',
    'tok_invalid_amount': 'مبلغ نامعتبره. یه عدد مثل 10 یا 10.5 بفرست.',
    'tok_route_no_usdb': 'این مسیر USDB قبول نمی‌کنه. یه شبکه دیگه انتخاب کن.',
    'tok_unknown_provider': 'ارائه‌دهنده این مسیر شناسایی نشد. دوباره امتحان کن یا شبکه دیگه‌ای انتخاب کن.',
    'tok_route_identity': 'اطلاعات مسیر ناقصه. دوباره امتحان کن.',
    'tok_route_changed': 'مسیر یا مقصد موقع آماده‌سازی تغییر کرد. از اول شروع کن.',
    'tok_unexpected_funding': 'در پیش‌فاکتور، تأمین یا تبدیل غیرمنتظره‌ای هست. از اول شروع کن.',
    'tok_invalid_quote': 'مبالغ پیش‌فاکتور معتبر نیست. از اول شروع کن.',
    'tok_unsupported_provider': 'این ارائه‌دهنده برداشت پشتیبانی نمی‌شه. از اول شروع کن.',
    'tok_missing_correlation': 'شناسه پیش‌فاکتور پیدا نشد. از اول شروع کن.',
}


def _zap_tokens():
    """Load zap_tokens offline (breez stubbed), like the p2p micro test."""
    sandbox = os.environ.get('TMPDIR', '/tmp')
    old = os.environ.get('HOME')
    os.environ['HOME'] = sandbox
    try:
        sdk_stub = types.ModuleType('breez_sdk_spark')
        sdk_stub.EventListener = type('EventListener', (), {})
        spec = importlib.util.spec_from_file_location(
            'zt_migrated', ROOT / 'zap_tokens.py')
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'breez_sdk_spark': sdk_stub}):
            spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            del os.environ['HOME']
        else:
            os.environ['HOME'] = old


class TokenErrKeysGolden(unittest.TestCase):
    def test_tok_keys_pinned_per_pack(self):
        for lang, pack in packs().items():
            if lang == 'en':
                expected = TOK_ERR_KEYS
                for key, val in expected.items():
                    self.assertEqual(pack.get(key), val, f'{lang}:{key}')
            elif lang == 'fa':
                for key, val in TOK_ERR_KEYS_FA.items():
                    self.assertEqual(pack.get(key), val, f'{lang}:{key}')
            else:
                for key in TOK_ERR_KEYS:
                    self.assertNotIn(key, pack, f'{lang}:{key} (must fall back to EN)')

    def test_user_err_text_identity(self):
        zt = _zap_tokens()
        # known codes render as the EN human wording (user 7 = default lang);
        # unknown errors pass through unchanged
        for key, code in TOK_RAISE_CODES.items():
            self.assertEqual(zt.user_err_text(7, RuntimeError(code)), TOK_ERR_KEYS[key], code)
        self.assertEqual(zt.user_err_text(7, RuntimeError('timeout')), 'timeout')
        self.assertEqual(zt.user_err_text(7, ValueError('sdk boom')), 'sdk boom')

    def test_wizard_render_sites_use_user_err_text(self):
        # the five non-extracted wizard branches route through user_err_text;
        # the exec-isolated tkwd_go/message_handler sites keep error=str(e)
        self.assertEqual(
            V32.count('error=zt.user_err_text(user_id, e)'), 5)

    def test_every_raise_code_is_mapped(self):
        import ast
        tree = ast.parse((ROOT / 'zap_tokens.py').read_text())
        codes = {n.exc.args[0].value for n in ast.walk(tree)
                 if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and n.exc.args
                 and isinstance(n.exc.args[0], ast.Constant)
                 and isinstance(n.exc.args[0].value, str)}
        self.assertEqual(codes, set(TOK_RAISE_CODES.values()))


if __name__ == '__main__':
    unittest.main()
