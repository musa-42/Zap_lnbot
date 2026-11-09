"""
Zap Wallet Bot — i18n (internationalization) engine.

Usage in bot code:
    from zap_i18n import t, get_user_lang, set_user_lang, detect_lang_code, init_i18n, LANG_NAMES, SUPPORTED_LANGS

    text = t(user_id, "main_menu_title")            # plain translated string
    text = t(user_id, "balance_line", amount="1,000 sats")  # with {placeholders}
    btn  = t(user_id, "btn_send")                   # button labels

Adding a language: append a dict to TRANSLATIONS with the same keys.
Missing keys fall back to English, then to the key itself.
RTL languages (fa, ar, ur, he) are flagged in RTL_LANGS for message layout.
"""
import json
import logging

# ===================== Language registry =====================
LANG_ALIASES = {
    'en': 'en', 'fa': 'fa', 'pes': 'fa',  # pes = Persian (Telegram code)
    'ru': 'ru', 'zh': 'zh', 'zh-hans': 'zh', 'zh-hant': 'zh',
    'ar': 'ar', 'es': 'es', 'tr': 'tr', 'de': 'de', 'fr': 'fr',
    'pt': 'pt', 'hi': 'hi', 'id': 'id', 'ja': 'ja', 'ko': 'ko',
    'it': 'it', 'ur': 'ur', 'vi': 'vi', 'uk': 'uk', 'bn': 'bn',
}
SUPPORTED_LANGS = ['en', 'fa', 'ru', 'zh', 'ar', 'es', 'tr', 'de', 'fr', 'pt', 'hi', 'id']
RTL_LANGS = {'fa', 'ar', 'ur', 'he'}
DEFAULT_LANG = 'en'

LANG_NAMES = {
    'en': 'English', 'fa': 'فارسی', 'ru': 'Русский', 'zh': '中文',
    'ar': 'العربية', 'es': 'Español', 'tr': 'Türkçe', 'de': 'Deutsch',
    'fr': 'Français', 'pt': 'Português', 'hi': 'हिन्दी', 'id': 'Indonesia',
}

# ===================== Translations =====================
EN = {
    'financial_paused': 'Payments here are paused for safety. Contact support for reconciliation.',
    'deposit_tracking_paused': 'Deposit tracking is temporarily unavailable; check your wallet history.',
    'deposit_fee_paused': 'Service fee collection is temporarily paused.',
    'payment_hold': '⏳ Payment status is pending or unknown. Do not retry; contact support for reconciliation.',
    # v32 hold rework (owner order): the live send path never shows
    # 'payment_hold' anymore — pending/unknown renders as processing and a
    # read-only background recheck resolves the message. Key kept: zap
    # wallet v31 still renders it.
    'payment_processing_status': "⏳ Payment is processing… I'll update this message in a moment.",
    'payment_still_processing': "⏳ Payment is still processing. Do not retry; if it has not arrived within 10 minutes, contact support.\n\n🆔 ID: `{payment_id}`",
    'payment_failed': '❌ Payment failed.',
    'batch_password_required': 'Enter your send password in this private chat to send this batch.',
    'batch_password_retry': 'Incorrect password. Try again.',
    'batch_password_failed': 'This batch is no longer available. Start Batch Pay again.',
    'batch_password_verified': '✅ Password accepted. Tap Send All to send the batch.',
    'batch_hold': '⏳ One or more batch payments are pending or unknown. Do not retry them — they are held and support will reconcile.',
    'token_wd_password_retry': '❌ Incorrect password. Try again or cancel.',
    # ---- main menu ----
    'app_title': '⚡ **ZAP Wallet**',
    'btn_send': '📤 Send',
    'btn_receive': '📥 Receive',
    'btn_history': '📜 History',
    'btn_settings': '⚙️ Settings',
    'btn_help': '❓ Help',
    'btn_donate': '💝 Donate',
    'btn_refresh': '🔄 Refresh',
    'btn_back_menu': '« Menu',
    'btn_back_settings': '« Settings',
    'btn_back_tools': '« Tools',
    'btn_cancel': '« Cancel',
    'btn_tools': '🧰 Tools',
    'btn_confirm': '✅ Confirm',
    'btn_wd_all': '💰 Withdraw everything',
    'btn_try_again': '🔄 Try Again',
    'btn_skip': '⏭ Skip',
    'balance_line': '💰 Balance: {amount}',
    'backup_seed_hint': '🛡 Back up your seed in ⚙️ Settings',

    # ---- tools ----
    'tools_title': '🧰 **Tools**\n\nPick a tool:',
    'btn_calculator': '🧮 Calculator',
    'btn_fees': '⛽ Fees',
    'btn_batch_pay': '📢 Batch Pay',
    'btn_contacts': '📒 Contacts',
    'btn_buy_bitcoin': '🟧 Buy Bitcoin',
    'btn_sync': '🔄 Sync',
    'btn_sign_msg': '✍️ Sign Message',
    'btn_language': '🌐 Language',

    # ---- settings ----
    'settings_title': '⚙️ **Settings**',
    'btn_backup_seed': '🔐 Backup Seed',
    'btn_restore': '🔄 Restore Wallet',
    'btn_deposits': '📦 Unclaimed Deposits',
    'btn_ln_address': '⚡ Lightning Address',
    'btn_change_fiat': '💱 Fiat Currency',
    'btn_send_password': '🔒 Send Password',
    'btn_private_mode': '👻 Private Mode',
    'select_language': '🌐 **Language**\n\nCurrent: {current}',
    'lang_set': '✅ Language: {lang}',

    # ---- help ----
    'help_text': (
        '⚡ **ZAP Wallet — Guide**\n\n'
        '**🚀 Start**\n'
        '1. `/start` — open your wallet\n'
        '2. Back up your seed phrase in ⚙️ Settings\n'
        '3. Send & receive Bitcoin\n\n'
        '**📤 Send**\n'
        'Paste any invoice / address / LNURL here — the bot detects it and opens Send. Dollar amounts work too: `$5`\n\n'
        '**📥 Receive**\n'
        '• ⚡ Lightning Invoice\n'
        '• ₿ Onchain Address\n'
        '• 🔄 Exchange via ff.io (any crypto ⇄ Lightning)\n\n'
        '**⚡ Zap (in groups)**\n'
        '• Reply to a message: `/zap 1000`\n'
        '• Mention someone: `/zap 1000 @user`\n'
        '• With note & dollars: `/zap $5 @user Nice!`\n\n'
        '**📦 Unclaimed Deposits**\n'
        'Settings → Unclaimed Deposits → claim or refund.\n\n'
        '**🧰 Tools**\n'
        '`/contacts` address book • `/buy` buy Bitcoin\n'
        '`/fees` network fees • `/sync` refresh\n'
        '`/signmsg` / `/verifymsg` prove ownership\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **Backup your seed phrase — no seed, no wallet!**\n\n'
        'Need help? @zap_ln'
    ),

    # ---- send ----
    'send_title': '📤 **Send**',

    # ---- receive ----
    'receive_title': '📥 **Receive**\n\nChoose a receive method:',
    'btn_ln_invoice': '⚡ Lightning Invoice',
    'btn_onchain': '₿ Onchain Address',
    'btn_exchange': '🔄 Exchange (ff.io)',
    # ---- USDT/USDC (Spark cross-chain) ----
    'btn_token_usd': '💵 USDT / USDC',
    'token_deposit_title': '💵 **Deposit USDT/USDC**\n\nWhich network are you sending from?',
    'token_withdraw_title': '💵 **Withdraw {asset}**\n\nSend the destination address ({chain}):',
    'token_wd_amount': '💰 Enter the amount of USDB to spend.\n\nAvailable: **{balance}**\nBTC is not converted; the quote determines the debit and estimated delivery.',
    'token_wd_min': '⬇️ Minimum withdrawal: **{amount} USD**',
    'token_wd_min_err': '⬇️ Minimum is **{amount} USD** — send a larger amount:',
    'token_wd_private_only': 'Open the bot in private to withdraw.',
    'token_wd_password_prompt': '🔒 Enter your send password to authorize this withdrawal.',
    'token_wd_password_failed': '❌ Password incorrect or withdrawal expired. Start again.',
    'token_wd_password_verified': '✅ Password verified. Confirm this withdrawal to continue.',
    'token_wd_confirm': '💸 **Withdraw {asset}**\n\n💰 Estimated delivery: **{amount} {asset}**\n💳 Source debit: **{input_amount} USDB**\n⛽ Estimated network/provider fee: {network_fee} {asset}\n⚡ Source transfer fee budget: {transfer_fee} sats\n📍 To: `{address}`\n📡 {chain}\n\nDelivery is not guaranteed until confirmed. Confirm?',
    'token_recv_ready': '✅ **{asset} deposit — {chain}**\n\n💰 Send exactly: **{amount} {asset}**\n⚡ You get: ≈ {sats}\n\n📍 Address:\n`{address}`\n\n⚠️ {asset} on {chain} only — anything else is lost',
    'token_no_routes': '❌ No route available right now. Try again later.',
    'token_parse_fail': '❌ Not a valid {chain} address. Send it again:',
    'token_amount_bad': '❌ Send a valid amount like `10` or `10.5`',
    'token_amount_high': '❌ Too much. Balance: **{balance}** — send a smaller amount:',
    'token_wd_started': '⏳ Withdrawing… this can take a few minutes',
    'token_wd_done': '✅ **Withdrawal sent**\n\n💰 {amount} {asset} → `{address}`\n📡 {chain}\n🔖 TXID: `{tx}`',
    'token_busy': '⏳ Preparing…',
    'token_dep_amount': '💰 How much **{asset}** on {chain}? (e.g. `10`)',
    'token_withdraw_pick': '💵 **Withdraw USDT/USDC**\n\nPick the token:',
    'token_chain_pick': '📡 Which network?',
    'btn_track': '🔍 Track deposit',
    'btn_prev': '⬅️ Previous',
    'btn_next': 'Next ➡️',

    # ---- donate ----
    'donate_title': '💝 **Support This Bot**\n\nThank you for considering to support!\n\nYou can donate in two ways:\n\n1️⃣ **Zap directly to this bot:**\n   Just send a payment to the bot!\n\n2️⃣ **Send to developer:**\n   `you@wallet.domain`\n\nEvery zap helps keep this bot running! ⚡',
    'btn_zap_now': '⚡ Zap Now',
    'btn_back_contacts': '« Contacts',
    'btn_cancel_x': '❌ Cancel',
    'btn_confirm_wd': '✅ Confirm & Withdraw',
    'btn_confirm_send': '✅ Confirm & Send',
    'btn_create_order': '✅ Create Order',
    'btn_confirm_claim': '✅ Confirm Claim',
    'btn_claim_ln': '⚡ Claim to Lightning',
    'btn_send_all': '💸 Send All',
    'btn_show_name': '👤 Show My Name',
    'btn_anon': '👻 Send Anonymous',
    'btn_rate_float': '📈 Float Rate (market)',
    'btn_rate_fixed': '🔒 Fixed Rate (guaranteed)',
    'btn_exit_menu': '« Exit Menu',
    'btn_scan_another': '🔍 Scan Another',
    'btn_pay_details': '🔍 Payment Details',
    'btn_check_status': '🔍 Check Status',
    'btn_cancel_reset': '❌ Cancel Reset',
    'btn_fee_fast': '🚀 Fast',
    'btn_fee_medium': '🚗 Medium',
    'btn_fee_slow': '🐢 Slow',
    'btn_fee_fast_r': '🚀 Fast — {rate} sat/vB',
    'btn_fee_medium_r': '🚗 Medium — {rate} sat/vB',
    'btn_fee_slow_r': '🐢 Slow — {rate} sat/vB',
    'btn_cancel_all': '🗑 Cancel All',
    'btn_calc_another': '🧮 Another',
    'btn_add_another': '➕ Add Another',
    'btn_remove': '➖ Remove',
    'btn_rm_pass': '🗑️ Remove Password',
    'btn_chpass': '✏️ Change Password',
    'btn_hint': '💡 Set/Change Hint',
    'btn_forgot': '❓ Forgot Password?',
    'btn_req_reset': '🔓 Request Reset (3 days)',
    'btn_show_bal': '👁️ Show My Balance',
    'btn_later': 'Maybe later',
    'btn_export_state': '💾 Export State',
    'btn_export_bk': '💾 Export State backup',
    'btn_quote_exit': '🚨 Quote my exit',
    'btn_refund_btc': '↩️ Refund to BTC Address',
    'btn_reg_url': '➕ Register URL',
    'btn_try_coin': '« Try Different Coin',
    'btn_view_contacts': '📒 View Contacts',
    'btn_chaddr': '✏️ Change Address',
    'btn_regaddr': '✏️ Register Address',
    'btn_del_yes': '🗑️ Yes, delete it',
    'btn_del_addr': '🗑️ Delete Address',
    'btn_custom_fee': '✏️ Custom Fee',
    'btn_custom': '✏️ Custom',
    'btn_custom_amount': 'Custom amount',
    'btn_send_all_n': '🚀 Send All ({n})',
    'btn_claim_n': '⚡ Claim #{i} ({amt})',

    'donate_amount_title': '💝 **Donate Amount**\n\nHow many sats would you like to donate?\n\nSuggested amounts:\n• 1,000 sats (☕ Coffee)\n• 5,000 sats (🍕 Pizza)\n• 10,000 sats (❤️ Supporter)\n• 21,000 sats (🚀 Champion!)\n➡️ Send the amount (sats or $USD):',

    # ---- common ----
    'err_generic': '❌ {error}',
    'session_expired': '❌ Session expired. Please start again.',
    'pay_data_missing': '❌ Payment details expired — start the payment again.',
    'no_balance_wd': '❌ No sats to withdraw — receive some first.',
    'wd_process_fail': '❌ Unable to process the withdrawal. Please start again.',
    'order_refresh_expired': '❌ Order expired — create a new order.',
    'synced': '✅ Synced.\n💰 Balance: **{balance} sats**',
    # ---- receive v2 (user-friendly) ----
    'your_ln_address': 'Your Lightning Address',
    'your_spark_address': 'Your Spark Address',
    'invoice_no_amount_title': '📥 **Your Invoice** (no amount set)',
    'btn_set_amount': '💵 Set Amount',
    'btn_set_memo': '💬 Set Memo',
    'btn_register_ln': '➕ Register Lightning Address',
    'btn_back': '« Back',
    'ln_no_address': '⚡ **No Lightning Address yet**\n\nRegister one to receive payments easily.',
    'onchain_title': '₿ **Your Onchain Address**',
    'network_fee': 'Network fee',
    'onchain_note': '💡 This address changes each time — old ones stay valid forever.',
    'your_onchain_address': 'Your Onchain Address',
    'memo_label': 'Memo',
    'invoice_title': '📥 **Your Invoice**',
    'available_line': '💰 Available: {amount}',
    'send_how': 'How do you want to pay?',
    'btn_ln_btc': '⚡ Lightning / BTC',
    'send_paste_any': 'Paste an invoice, address, or @username — or send a QR photo 📷',
    'send_qr_hint': 'You can also send a QR code photo and I will read it.',
    'qr_not_found': 'Could not read a QR code in that image. Try another photo or paste the text.',
    'amount_label': 'Amount',
    # ---- history ----
    'hist_title': '📜 **History** — page {page}',
    'hist_empty_title': '📜 **History**',
    'hist_empty': 'Nothing here yet.\nSend or receive your first sats!',
    'btn_scan_payment': '🔍 Scan Payment',
    'hist_error': '❌ Error loading history:\n{error}',
    # ---- ff.io exchange ----
    'ff_loading': 'Loading currencies...',
    'ff_load_fail': 'Failed to load currencies.',
    'ff_recv_title': '🔄 **Receive via Exchange (ff.io)**\n\nSelect the coin you want to deposit:\nYou send crypto → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Send via Exchange (ff.io)**\n\nBalance: {balance}\n\nSelect the coin you want to receive:\nLightning BTC → ff.io → your crypto',
    'ff_terms': '⚠️ _The exchange service is provided by FixedFloat. Creating an order confirms your agreement with the [FixedFloat rules](https://ff.io/terms-of-service)._',
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
    # item 24 codes — human wording in the UI/UX pass (owner decision #8 lifted);
    # rendered inside err_generic ('❌ {error}')
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

# Full translations for major languages; partial packs fall back to EN per key.
FA = {
    'financial_paused': 'این پرداخت‌ها فعلاً برای حفظ امنیت متوقف شده‌اند. برای بررسی به پشتیبانی پیام بده.',
    'deposit_tracking_paused': 'پیگیری واریز فعلاً در دسترس نیست؛ تاریخچه کیف پولت را بررسی کن.',
    'deposit_fee_paused': 'دریافت کارمزد سرویس فعلاً متوقف است.',
    'payment_hold': '⏳ وضعیت پرداخت در انتظار یا نامشخصه. دوباره ارسال نکن؛ برای بررسی با پشتیبانی تماس بگیر.',
    'payment_processing_status': '⏳ پرداخت در حال پردازش است… چند لحظه صبر کن؛ همین‌جا خبر می‌دم.',
    'payment_still_processing': '⏳ پرداخت هنوز در حال پردازش است. دوباره تلاش نکن؛ اگر تا ۱۰ دقیقه دیگر به مقصد نرسید به پشتیبانی پیام بده.\n\n🆔 شناسه: `{payment_id}`',
    'payment_failed': '❌ پرداخت ناموفق بود.',
    'batch_password_required': 'رمز ارسال رو در همین گفتگوی خصوصی وارد کن تا این دسته ارسال شود.',
    'batch_password_retry': 'رمز اشتباهه. دوباره تلاش کن.',
    'batch_password_failed': 'این دسته دیگر در دسترس نیست. ارسال گروهی را از نو شروع کن.',
    'batch_password_verified': '✅ رمز پذیرفته شد. برای ارسال دسته دکمه ارسال همه را بزن.',
    'batch_hold': '⏳ یک یا چند پرداخت دسته‌ای در انتظار یا نامشخص است. دوباره ارسال نکن؛ نگه داشته شده و پشتیبانی بررسی می‌کند.',
    'token_wd_password_retry': '❌ رمز اشتباه است. دوباره تلاش کن یا انصراف بده.',
    'app_title': '⚡ **کیف پول زپ**',
    'btn_send': '📤 ارسال', 'btn_receive': '📥 دریافت', 'btn_history': '📜 تاریخچه',
    'btn_settings': '⚙️ تنظیمات', 'btn_help': '❓ راهنما', 'btn_donate': '💝 حمایت',
    'btn_refresh': '🔄 بروزرسانی', 'btn_back_menu': '« منو', 'btn_back_settings': '« تنظیمات',
    'btn_back_tools': '« ابزارها', 'btn_cancel': '« لغو', 'btn_tools': '🧰 ابزارها',
    'btn_confirm': '✅ تایید', 'btn_try_again': '🔄 تلاش دوباره', 'btn_skip': '⏭ رد کردن',
    'btn_wd_all': '💰 برداشت کل موجودی',
    'balance_line': '💰 موجودی:\n{amount}',
    'backup_seed_hint': '🛡 عبارت بازیابی رو در ⚙️ تنظیمات بکاپ بگیر',
    'tools_title': '🧰 **ابزارها**\n\nیک ابزار انتخاب کن:',
    'btn_calculator': '🧮 ماشین حساب', 'btn_fees': '⛽ کارمزد شبکه',
    'btn_batch_pay': '📢 پرداخت گروهی', 'btn_contacts': '📒 مخاطبین',
    'btn_buy_bitcoin': '🟧 خرید بیت‌کوین', 'btn_sync': '🔄 همگام‌سازی',
    'btn_sign_msg': '✍️ امضای پیام', 'btn_language': '🌐 زبان',
    'settings_title': '⚙️ **تنظیمات**',
    'btn_backup_seed': '🔐 بکاپ عبارت بازیابی', 'btn_restore': '🔄 بازیابی کیف پول',
    'btn_deposits': '📦 واریزی‌های ادعا نشده', 'btn_ln_address': '⚡ آدرس لایتنینگ',
    'btn_change_fiat': '💱 ارز پایه', 'btn_send_password': '🔒 رمز ارسال',
    'btn_private_mode': '👻 حالت خصوصی', 
    'select_language': '🌐 **زبان**\n\nفعلی: {current}',
    'lang_set': '✅ زبان: {lang}',
    'help_text': (
        '⚡ **کیف پول زپ — راهنما**\n\n'
        '**🚀 شروع**\n'
        '1. `/start` — باز کردن کیف پول\n'
        '2. بکاپ عبارت بازیابی در ⚙️ تنظیمات\n'
        '3. ارسال و دریافت بیت‌کوین\n\n'
        '**📤 ارسال**\n'
        'فاکتور، LNURL، آدرس لایتنینگ، @یوزرنیم یا آدرس bc1 رو پیست کن — ربات خودش تشخیص میده. مبلغ دلاری مثل `$5` هم قبوله.\n\n'
        '**📥 دریافت**\n'
        '• ⚡ فاکتور لایتنینگ\n'
        '• ₿ آدرس آنچین\n'
        '• 🔄 صرافی ff.io (هر کریپتو ⇄ لایتنینگ)\n\n'
        '**⚡ زپ (تو گروه‌ها)**\n'
        '• روی پیام کسی ریپلای کن: `/zap 1000`\n'
        '• منشن کن: `/zap 1000 @user`\n'
        '• با یادداشت و دلار: `/zap $5 @user آفرین!`\n\n'
        '**📦 واریزی‌های ادعا نشده**\n'
        'تنظیمات → واریزی‌های ادعا نشده → claim یا استرداد.\n\n'
        '**🧰 ابزارها**\n'
        '`/contacts` دفترچه آدرس • `/buy` خرید بیت‌کوین\n'
        '`/fees` کارمزد شبکه • `/sync` بروزرسانی\n'
        '`/signmsg` / `/verifymsg` اثبات مالکیت\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **عبارت بازیابی رو بکاپ بگیر — بدون seed کیف پولی نیست!**\n\n'
        'سوال داری؟ @zap_ln'
    ),
    'send_title': '📤 **ارسال**',
    'receive_title': '📥 **دریافت**\n\nروش دریافت رو انتخاب کن:',
    'btn_ln_invoice': '⚡ فاکتور لایتنینگ', 'btn_onchain': '₿ آدرس آنچین',
    'btn_exchange': '🔄 صرافی (ff.io)',
    # ---- USDT/USDC (Spark cross-chain) ----
    'btn_token_usd': '💵 تتر / یو‌اس‌دی‌سی',
    'token_deposit_title': '💵 **واریز تتر/یو‌اس‌دی‌سی**\n\nاز کدوم شبکه می‌خوای بفرستی؟',
    'token_withdraw_title': '💵 **برداشت {asset}**\n\nآدرس مقصد رو بفرست (شبکه {chain}):',
    'token_wd_amount': '💰 مبلغ USDB برای برداشت رو وارد کن.\n\nموجودی قابل استفاده: **{balance}**\nبیت‌کوین تبدیل نمی‌شه؛ مبلغ کسر و دریافتی تخمینی در پیش‌فاکتور مشخص می‌شن.',
    'token_wd_min': '⬇️ حداقل برداشت: **{amount} دلار**',
    'token_wd_min_err': '⬇️ حداقل برداشت **{amount} دلار**ه — عدد بزرگ‌تری بفرست:',
    'token_wd_private_only': 'برای برداشت، ربات را در چت خصوصی باز کن.',
    'token_wd_password_prompt': '🔒 رمز ارسال را برای تأیید این برداشت وارد کن.',
    'token_wd_password_failed': '❌ رمز اشتباه است یا برداشت منقضی شده. دوباره شروع کن.',
    'token_wd_password_verified': '✅ رمز تأیید شد. برای ادامه برداشت را تأیید کن.',
    'token_wd_confirm': '💸 **برداشت {asset}**\n\n💰 دریافتی تخمینی: **{amount} {asset}**\n💳 کسر از موجودی: **{input_amount} USDB**\n⛽ کارمزد تخمینی شبکه/ارائه‌دهنده: {network_fee} {asset}\n⚡ سقف کارمزد انتقال مبدأ: {transfer_fee} سات\n📍 به: `{address}`\n📡 شبکه {chain}\n\nرسیدن مبلغ تا زمان تأیید تضمین نیست. تأیید می‌کنی؟',
    'token_recv_ready': '✅ **واریز {asset} — شبکه {chain}**\n\n💰 بفرست: دقیقاً **{amount} {asset}**\n⚡ می‌رسه: ≈ {sats}\n\n📍 آدرس:\n`{address}`\n\n⚠️ فقط {asset} روی {chain} — چیز دیگه‌ای بفرستی پول می‌سوزه',
    'token_no_routes': '❌ فعلاً مسیری در دسترس نیست. یه کم بعداً امتحان کن.',
    'token_parse_fail': '❌ آدرس {chain} معتبر نیست. دوباره بفرست:',
    'token_amount_bad': '❌ یه عدد درست بفرست، مثل `10` یا `10.5`',
    'token_amount_high': '❌ بیشتر از موجودیه. موجودی: **{balance}** — عدد کمتری بفرست:',
    'token_wd_started': '⏳ در حال برداشت… چند دقیقه طول می‌کشه',
    'token_wd_done': '✅ **برداشت شد**\n\n💰 {amount} {asset} → `{address}`\n📡 {chain}\n🔖 TXID: `{tx}`',
    'btn_track': '🔍 پیگیری واریز',
    'token_busy': '⏳ دارم آماده می‌کنم…',
    'token_dep_amount': '💰 چند **{asset}** روی {chain} واریز می‌کنی؟ (مثلاً `10`)',
    'token_withdraw_pick': '💵 **برداشت تتر/یو‌اس‌دی‌سی**\n\nکدوم توکن؟',
    'token_chain_pick': '📡 کدوم شبکه؟',
    'btn_prev': '⬅️ قبلی', 'btn_next': 'بعدی ➡️',
    'donate_title': '💝 **حمایت از ربات**\n\nممنون که به حمایت فکر کردی!\n\nدو راه داری:\n\n1️⃣ **زپ مستقیم به ربات:**\n   فقط یه پرداخت به ربات بفرست!\n\n2️⃣ **واریز به توسعه‌دهنده:**\n   `you@wallet.domain`\n\nهر زپ به ادامه کار ربات کمک میکنه! ⚡',
    'btn_zap_now': '⚡ زپ کن',
    'btn_back_contacts': '« مخاطبین',
    'btn_cancel_x': '❌ لغو',
    'btn_confirm_wd': '✅ تأیید و برداشت',
    'btn_confirm_send': '✅ تأیید و ارسال',
    'btn_create_order': '✅ ساخت سفارش',
    'btn_confirm_claim': '✅ تأیید و دریافت',
    'btn_claim_ln': '⚡ انتقال به لایتنینگ',
    'btn_send_all': '💸 ارسال همه',
    'btn_show_name': '👤 با اسم خودم',
    'btn_anon': '👻 ناشناس بفرست',
    'btn_rate_float': '📈 نرخ شناور (بازار)',
    'btn_rate_fixed': '🔒 نرخ ثابت (تضمینی)',
    'btn_exit_menu': '« خروج از منو',
    'btn_scan_another': '🔍 اسکن یکی دیگه',
    'btn_pay_details': '🔍 جزئیات پرداخت',
    'btn_check_status': '🔍 چک وضعیت',
    'btn_cancel_reset': '❌ لغو ریست',
    'btn_fee_fast': '🚀 سریع',
    'btn_fee_medium': '🚗 متوسط',
    'btn_fee_slow': '🐢 آروم',
    'btn_fee_fast_r': '🚀 سریع — {rate} sat/vB',
    'btn_fee_medium_r': '🚗 متوسط — {rate} sat/vB',
    'btn_fee_slow_r': '🐢 آروم — {rate} sat/vB',
    'btn_cancel_all': '🗑 لغو همه',
    'btn_calc_another': '🧮 یکی دیگه',
    'btn_add_another': '➕ یکی دیگه اضافه کن',
    'btn_remove': '➖ حذف',
    'btn_rm_pass': '🗑️ حذف رمز',
    'btn_chpass': '✏️ تغییر رمز',
    'btn_hint': '💡 تعیین/تغییر یادآور',
    'btn_forgot': '❓ رمز یادت رفته؟',
    'btn_req_reset': '🔓 درخواست ریست (۳ روز)',
    'btn_show_bal': '👁️ موجودی رو نشون بده',
    'btn_later': 'شاید بعداً',
    'btn_export_state': '💾 خروجی State',
    'btn_export_bk': '💾 بکاپ State',
    'btn_quote_exit': '🚨 استعلام خروج',
    'btn_refund_btc': '↩️ برگشت به آدرس BTC',
    'btn_reg_url': '➕ ثبت URL',
    'btn_try_coin': '« سکه‌ی دیگه',
    'btn_view_contacts': '📒 دیدن مخاطبین',
    'btn_chaddr': '✏️ تغییر آدرس',
    'btn_regaddr': '✏️ ثبت آدرس',
    'btn_del_yes': '🗑️ آره، پاکش کن',
    'btn_del_addr': '🗑️ حذف آدرس',
    'btn_custom_fee': '✏️ کارمزد دلخواه',
    'btn_custom': '✏️ دلخواه',
    'btn_custom_amount': 'مبلغ دلخواه',
    'btn_send_all_n': '🚀 ارسال همه ({n})',
    'btn_claim_n': '⚡ برداشت #{i} ({amt})',

    'donate_amount_title': '💝 **مبلغ حمایت**\n\nچند ساتوشی میخوای حمایت کنی؟\n\nمبلغ‌های پیشنهادی:\n• 1,000 ساتوشی (☕ قهوه)\n• 5,000 ساتوشی (🍕 پیتزا)\n• 10,000 ساتوشی (❤️ حامی)\n• 21,000 ساتوشی (🚀 قهرمان!)\n➡️ مبلغ رو بفرست (ساتوشی یا دلار):',
    'err_generic': '❌ {error}',
    'session_expired': '❌ جلسه منقضی شد. دوباره شروع کن.',
    'pay_data_missing': '❌ اطلاعات پرداخت منقضی شده — پرداخت رو از اول شروع کن.',
    'no_balance_wd': '❌ ساتوشی برای برداشت نداری — اول یه چیزی دریافت کن.',
    'wd_process_fail': '❌ برداشت پردازش نشد. دوباره از اول شروع کن.',
    'order_refresh_expired': '❌ سفارش منقضی شده — یه سفارش جدید بساز.',
    'synced': '✅ همگام شد.\n💰 موجودی: **{balance} ساتوشی**',
    # ---- receive v2 ----
    'your_ln_address': 'آدرس لایتنینگ شما',
    'your_spark_address': 'آدرس اسپارک شما',
    'invoice_no_amount_title': '📥 **فاکتور شما** (بدون مبلغ)',
    'btn_set_amount': '💵 تعیین مبلغ',
    'btn_set_memo': '💬 تعیین یادداشت',
    'btn_register_ln': '➕ ثبت آدرس لایتنینگ',
    'btn_back': '« برگشت',
    'ln_no_address': '⚡ **هنوز آدرس لایتنینگ نداری**\n\nبرای دریافت راحت، یکی ثبت کن.',
    'onchain_title': '₿ **آدرس آنچین شما**',
    'network_fee': 'کارمزد شبکه',
    'onchain_note': '💡 این آدرس هر بار عوض میشه — آدرس‌های قبلی همیشه معتبرن.',
    'your_onchain_address': 'آدرس آنچین شما',
    'memo_label': 'یادداشت',
    'invoice_title': '📥 **فاکتور شما**',
    'available_line': '💰 موجودی:\n{amount}',
    'send_how': 'چطور میخوای پرداخت کنی؟',
    'btn_ln_btc': '⚡ لایتنینگ / بیت‌کوین',
    'send_paste_any': 'فاکتور، آدرس یا @یوزرنیم رو پیست کن — یا عکس QR بفرست 📷',
    'send_qr_hint': 'می‌تونی عکس کد QR هم بفرستی تا برات بخونم.',
    'qr_not_found': 'نتونستم کد QR توی این عکس بخونم. عکس دیگه‌ای بفرست یا متن رو پیست کن.',
    'amount_label': 'مبلغ',
    # ---- history ----
    'hist_title': '📜 **تاریخچه** — صفحه {page}',
    'hist_empty_title': '📜 **تاریخچه**',
    'hist_empty': 'هنوز چیزی اینجا نیست.\nاولین ساتوشی‌هات رو بفرست یا دریافت کن!',
    'btn_scan_payment': '🔍 بررسی پرداخت',
    'hist_error': '❌ خطا در بارگذاری تاریخچه:\n{error}',
    # ---- ff.io exchange ----
    'ff_loading': 'در حال بارگذاری ارزها...',
    'ff_load_fail': 'بارگذاری ارزها ناموفق بود.',
    'ff_recv_title': '🔄 **دریافت با صرافی (ff.io)**\n\nارزی که میخوای واریز کنی رو انتخاب کن:\nتو ارسال میکنی ← ff.io ← بیت‌کوین لایتنینگ',
    'ff_send_title': '🔄 **ارسال با صرافی (ff.io)**\n\nموجودی: {balance}\n\nارزی که میخوای دریافت کنی رو انتخاب کن:\nبیت‌کوین لایتنینگ ← ff.io ← ارز تو',
    'ff_terms': '⚠️ _سرویس صرافی توسط FixedFloat ارائه میشه. ساخت سفارش یعنی قبول [قوانین FixedFloat](https://ff.io/terms-of-service)._',
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
    'dep_min_note': '⬇️ حداقل واریز: **{amount} دلار**{sats}',
    'dep_max_note': '⬆️ حداکثر واریز: **{amount} دلار**{sats}',
    # item 24 codes — fa migrated in the UI/UX pass (owner decision #8 lifted)
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

RU = {
    'app_title': '⚡ **ZAP Кошелёк**',
    'btn_send': '📤 Отправить', 'btn_receive': '📥 Получить', 'btn_history': '📜 История',
    'btn_settings': '⚙️ Настройки', 'btn_help': '❓ Помощь', 'btn_donate': '💝 Поддержать',
    'btn_refresh': '🔄 Обновить', 'btn_back_menu': '« Меню', 'btn_back_settings': '« Настройки',
    'btn_back_tools': '« Инструменты', 'btn_cancel': '« Отмена', 'btn_tools': '🧰 Инструменты',
    'btn_confirm': '✅ Подтвердить', 'btn_try_again': '🔄 Ещё раз', 'btn_skip': '⏭ Пропустить',
    'balance_line': '💰 Баланс: {amount}',
    'backup_seed_hint': '🛡 Сделайте резервную копию seed в ⚙️ Настройках',
    'tools_title': '🧰 **Инструменты**\n\nВыберите инструмент:',
    'btn_calculator': '🧮 Калькулятор', 'btn_fees': '⛽ Комиссии',
    'btn_batch_pay': '📢 Групповая оплата', 'btn_contacts': '📒 Контакты',
    'btn_buy_bitcoin': '🟧 Купить Bitcoin', 'btn_sync': '🔄 Синхронизация',
    'btn_sign_msg': '✍️ Подписать сообщение', 'btn_language': '🌐 Язык',
    'settings_title': '⚙️ **Настройки**',
    'btn_backup_seed': '🔐 Резервная копия seed', 'btn_restore': '🔄 Восстановление',
    'btn_deposits': '📦 Невостребованные депозиты', 'btn_ln_address': '⚡ Lightning адрес',
    'btn_change_fiat': '💱 Валюта', 'btn_send_password': '🔒 Пароль отправки',
    'btn_private_mode': '👻 Приватный режим', 
    'select_language': '🌐 **Язык**\n\nТекущий: {current}',
    'lang_set': '✅ Язык: {lang}',
    'help_text': (
        '⚡ **ZAP Кошелёк — Руководство**\n\n'
        '**🚀 Начало**\n'
        '1. `/start` — открыть кошелёк\n'
        '2. Сделайте резервную копию seed в ⚙️ Настройках\n'
        '3. Отправляйте и получайте Bitcoin\n\n'
        '**📤 Отправить**\n'
        'Вставьте инвойс, LNURL, lightning адрес, @username или bc1 адрес — бот определит автоматически. Суммы вроде `$5` тоже работают.\n\n'
        '**📥 Получить**\n'
        '• ⚡ Lightning инвойс\n'
        '• ₿ Onchain адрес\n'
        '• 🔄 Обмен через ff.io (любая крипта ⇄ Lightning)\n\n'
        '**⚡ Зап (в группах)**\n'
        '• Ответьте на сообщение: `/zap 1000`\n'
        '• Упомяните: `/zap 1000 @user`\n'
        '• С заметкой и долларами: `/zap $5 @user Молодец!`\n\n'
        '**📦 Невостребованные депозиты**\n'
        'Настройки → Невостребованные депозиты → получить или вернуть.\n\n'
        '**🧰 Инструменты**\n'
        '`/contacts` адресная книга • `/buy` купить Bitcoin\n'
        '`/fees` комиссии сети • `/sync` обновить\n'
        '`/signmsg` / `/verifymsg` подтверждение владения\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **Сделайте копию seed — без seed нет кошелька!**\n\n'
        'Нужна помощь? @zap_ln'
    ),
    'send_title': '📤 **Отправить**',
    'receive_title': '📥 **Получить**\n\nВыберите способ получения:',
    'btn_ln_invoice': '⚡ Lightning инвойс', 'btn_onchain': '₿ Onchain адрес',
    'btn_exchange': '🔄 Обмен (ff.io)',
    'btn_prev': '⬅️ Назад', 'btn_next': 'Вперёд ➡️',
    'donate_title': '💝 **Поддержать бота**\n\nСпасибо, что думаете о поддержке!\n\nДва способа:\n\n1️⃣ **Зап напрямую боту:**\n   Просто отправьте платёж боту!\n\n2️⃣ **Разработчику:**\n   `you@wallet.domain`\n\nКаждый зап помогает боту работать! ⚡',
    'btn_zap_now': '⚡ Запнуть',
    'donate_amount_title': '💝 **Сумма доната**\n\nСколько сатоши хотите отправить?\n\nРекомендуемые суммы:\n• 1,000 сатоши (☕ Кофе)\n• 5,000 сатоши (🍕 Пицца)\n• 10,000 сатоши (❤️ Поддержка)\n• 21,000 сатоши (🚀 Чемпион!)\n➡️ Отправьте сумму (сатоши или $USD):',
    'err_generic': '❌ {error}',
    'synced': '✅ Синхронизировано.\n💰 Баланс: **{balance} сатоши**',
    # ---- receive v2 ----
    'your_ln_address': 'Ваш Lightning адрес',
    'invoice_no_amount_title': '📥 **Ваш инвойс** (без суммы)',
    'btn_set_amount': '💵 Указать сумму',
    'btn_set_memo': '💬 Указать примечание',
    'btn_register_ln': '➕ Зарегистрировать Lightning адрес',
    'btn_back': '« Назад',
    'ln_no_address': '⚡ **Lightning адреса пока нет**\n\nЗарегистрируйте для удобного получения.',
    'onchain_title': '₿ **Ваш onchain адрес**',
    'network_fee': 'Комиссия сети',
    'onchain_note': '💡 Адрес меняется каждый раз — старые остаются действительными навсегда.',
    'your_onchain_address': 'Ваш onchain адрес',
    'memo_label': 'Примечание',
    'invoice_title': '📥 **Ваш инвойс**',
    'available_line': '💰 Доступно: {amount}',
    'send_how': 'Как хотите оплатить?',
    'btn_ln_btc': '⚡ Lightning / BTC',
    'send_paste_any': 'Вставьте инвойс, адрес или @username — или отправьте фото QR-кода 📷',
    'send_qr_hint': 'Можно также отправить фото QR-кода — я его прочитаю.',
    'qr_not_found': 'Не удалось прочитать QR-код на изображении. Отправьте другое фото или вставьте текст.',
    'amount_label': 'Сумма',
    # ---- history ----
    'hist_title': '📜 **История** — стр. {page}',
    'hist_empty_title': '📜 **История**',
    'hist_empty': 'Здесь пока пусто.\nОтправьте или получите первые сатоши!',
    'btn_scan_payment': '🔍 Проверить платёж',
    'hist_error': '❌ Ошибка загрузки истории:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'Загрузка валют...',
    'ff_load_fail': 'Не удалось загрузить валюты.',
    'ff_recv_title': '🔄 **Получить через обменник (ff.io)**\n\nВыберите монету для депозита:\nВы отправляете крипту → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Отправить через обменник (ff.io)**\n\nБаланс: {balance}\n\nВыберите монету для получения:\nLightning BTC → ff.io → ваша крипта',
    'ff_terms': '⚠️ _Сервис обмена предоставляет FixedFloat. Создание ордера означает согласие с [правилами FixedFloat](https://ff.io/terms-of-service)._',
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

ZH = {
    'app_title': '⚡ **ZAP 钱包**',
    'btn_send': '📤 发送', 'btn_receive': '📥 接收', 'btn_history': '📜 历史',
    'btn_settings': '⚙️ 设置', 'btn_help': '❓ 帮助', 'btn_donate': '💝 捐赠',
    'btn_refresh': '🔄 刷新', 'btn_back_menu': '« 菜单', 'btn_back_settings': '« 设置',
    'btn_back_tools': '« 工具', 'btn_cancel': '« 取消', 'btn_tools': '🧰 工具',
    'btn_confirm': '✅ 确认', 'btn_try_again': '🔄 重试', 'btn_skip': '⏭ 跳过',
    'balance_line': '💰 余额: {amount}',
    'backup_seed_hint': '🛡 请在 ⚙️ 设置中备份您的助记词',
    'tools_title': '🧰 **工具**\n\n请选择工具:',
    'btn_calculator': '🧮 计算器', 'btn_fees': '⛽ 手续费',
    'btn_batch_pay': '📢 批量支付', 'btn_contacts': '📒 联系人',
    'btn_buy_bitcoin': '🟧 购买比特币', 'btn_sync': '🔄 同步',
    'btn_sign_msg': '✍️ 签名消息', 'btn_language': '🌐 语言',
    'settings_title': '⚙️ **设置**',
    'btn_backup_seed': '🔐 备份助记词', 'btn_restore': '🔄 恢复钱包',
    'btn_deposits': '📦 未认领存款', 'btn_ln_address': '⚡ 闪电地址',
    'btn_change_fiat': '💱 法定货币', 'btn_send_password': '🔒 发送密码',
    'btn_private_mode': '👻 隐私模式', 
    'select_language': '🌐 **语言**\n\n当前: {current}',
    'lang_set': '✅ 语言: {lang}',
    'help_text': (
        '⚡ **ZAP 钱包 — 指南**\n\n'
        '**🚀 开始**\n'
        '1. `/start` — 打开钱包\n'
        '2. 在 ⚙️ 设置中备份助记词\n'
        '3. 发送和接收比特币\n\n'
        '**📤 发送**\n'
        '粘贴发票、LNURL、闪电地址、@用户名或 bc1 地址 — 机器人会自动识别。也支持 `$5` 这样的金额。\n\n'
        '**📥 接收**\n'
        '• ⚡ 闪电发票\n'
        '• ₿ 链上地址\n'
        '• 🔄 通过 ff.io 兑换 (任意加密货币 ⇄ 闪电网络)\n\n'
        '**⚡ ZAP (群组中)**\n'
        '• 回复某人的消息: `/zap 1000`\n'
        '• 提及某人: `/zap 1000 @user`\n'
        '• 带备注和美元: `/zap $5 @user 干得好!`\n\n'
        '**📦 未认领存款**\n'
        '设置 → 未认领存款 → 认领或退还。\n\n'
        '**🧰 工具**\n'
        '`/contacts` 联系人 • `/buy` 购买比特币\n'
        '`/fees` 网络手续费 • `/sync` 刷新\n'
        '`/signmsg` / `/verifymsg` 所有权证明\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **备份您的助记词 — 没有助记词就没有钱包!**\n\n'
        '需要帮助? @zap_ln'
    ),
    'send_title': '📤 **发送**',
    'receive_title': '📥 **接收**\n\n选择接收方式:',
    'btn_ln_invoice': '⚡ 闪电发票', 'btn_onchain': '₿ 链上地址',
    'btn_exchange': '🔄 兑换 (ff.io)',
    'btn_prev': '⬅️ 上一页', 'btn_next': '下一页 ➡️',
    'donate_title': '💝 **支持本机器人**\n\n感谢您的支持！\n\n两种捐赠方式：\n\n1️⃣ **直接给机器人发 ZAP：**\n   直接给机器人发送付款即可！\n\n2️⃣ **发送给开发者：**\n   `you@wallet.domain`\n\n每个 ZAP 都能帮助机器人运行！⚡',
    'btn_zap_now': '⚡ 立即 ZAP',
    'donate_amount_title': '💝 **捐赠金额**\n\n您想捐赠多少聪？\n\n建议金额：\n• 1,000 聪 (☕ 咖啡)\n• 5,000 聪 (🍕 披萨)\n• 10,000 聪 (❤️ 支持者)\n• 21,000 聪 (🚀 冠军！)\n➡️ 发送金额 (聪或美元):',
    'err_generic': '❌ {error}',
    'synced': '✅ 已同步。\n💰 余额: **{balance} 聪**',
    # ---- receive v2 ----
    'your_ln_address': '您的闪电地址',
    'invoice_no_amount_title': '📥 **您的发票** (未设金额)',
    'btn_set_amount': '💵 设置金额',
    'btn_set_memo': '💬 设置备注',
    'btn_register_ln': '➕ 注册闪电地址',
    'btn_back': '« 返回',
    'ln_no_address': '⚡ **还没有闪电地址**\n\n注册一个以便轻松收款。',
    'onchain_title': '₿ **您的链上地址**',
    'network_fee': '网络费用',
    'onchain_note': '💡 此地址每次都会更改 — 旧地址永久有效。',
    'your_onchain_address': '您的链上地址',
    'memo_label': '备注',
    'invoice_title': '📥 **您的发票**',
    'available_line': '💰 可用: {amount}',
    'send_how': '您想如何付款？',
    'btn_ln_btc': '⚡ 闪电 / 比特币',
    'send_paste_any': '粘贴发票、地址或@用户名 — 或发送二维码照片 📷',
    'send_qr_hint': '也可以发送二维码照片，我来识别。',
    'qr_not_found': '无法读取该图片中的二维码。请换一张照片或直接粘贴文本。',
    'amount_label': '金额',
    # ---- history ----
    'hist_title': '📜 **历史** — 第 {page} 页',
    'hist_empty_title': '📜 **历史**',
    'hist_empty': '这里还是空的。\n发送或接收您的第一聪吧！',
    'btn_scan_payment': '🔍 查询付款',
    'hist_error': '❌ 加载历史出错：\n{error}',
    # ---- ff.io ----
    'ff_loading': '正在加载币种...',
    'ff_load_fail': '加载币种失败。',
    'ff_recv_title': '🔄 **通过交易所接收 (ff.io)**\n\n选择要存入的币种：\n您发送加密货币 → ff.io → 闪电比特币',
    'ff_send_title': '🔄 **通过交易所发送 (ff.io)**\n\n余额: {balance}\n\n选择要接收的币种：\n闪电比特币 → ff.io → 您的加密货币',
    'ff_terms': '⚠️ _兑换服务由 FixedFloat 提供。创建订单即表示您同意 [FixedFloat 条款](https://ff.io/terms-of-service)。_',
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

AR = {
    'app_title': '⚡ **محفظة ZAP**',
    'btn_send': '📤 إرسال', 'btn_receive': '📥 استلام', 'btn_history': '📜 السجل',
    'btn_settings': '⚙️ الإعدادات', 'btn_help': '❓ مساعدة', 'btn_donate': '💝 تبرع',
    'btn_refresh': '🔄 تحديث', 'btn_back_menu': '« القائمة', 'btn_back_settings': '« الإعدادات',
    'btn_back_tools': '« أدوات', 'btn_cancel': '« إلغاء', 'btn_tools': '🧰 أدوات',
    'btn_confirm': '✅ تأكيد', 'btn_try_again': '🔄 حاول مجدداً', 'btn_skip': '⏭ تخطي',
    'balance_line': '💰 الرصيد:\n{amount}',
    'backup_seed_hint': '🛡 احفظ عبارة الاسترداد في ⚙️ الإعدادات',
    'tools_title': '🧰 **الأدوات**\n\nاختر أداة:',
    'btn_calculator': '🧮 آلة حاسبة', 'btn_fees': '⛽ الرسوم',
    'btn_batch_pay': '📢 دفعة مجموعة', 'btn_contacts': '📒 جهات الاتصال',
    'btn_buy_bitcoin': '🟧 شراء بيتكوين', 'btn_sync': '🔄 مزامنة',
    'btn_sign_msg': '✍️ توقيع رسالة', 'btn_language': '🌐 اللغة',
    'settings_title': '⚙️ **الإعدادات**',
    'btn_backup_seed': '🔐 نسخ عبارة الاسترداد', 'btn_restore': '🔄 استعادة المحفظة',
    'btn_deposits': '📦 ودائع غير مطالب بها', 'btn_ln_address': '⚡ عنوان برق',
    'btn_change_fiat': '💱 العملة', 'btn_send_password': '🔒 كلمة مرور الإرسال',
    'btn_private_mode': '👻 الوضع الخاص', 
    'select_language': '🌐 **اللغة**\n\nالحالية: {current}',
    'lang_set': '✅ اللغة: {lang}',
    'help_text': (
        '⚡ **محفظة ZAP — الدليل**\n\n'
        '**🚀 البداية**\n'
        '1. `/start` — فتح المحفظة\n'
        '2. احفظ عبارة الاسترداد في ⚙️ الإعدادات\n'
        '3. أرسل واستلم البيتكوين\n\n'
        '**📤 إرسال**\n'
        'الصق فاتورة أو LNURL أو عنوان برق أو @مستخدم أو عنوان bc1 — يتعرف عليها البوت تلقائياً. المبالغ مثل `$5` مدعومة أيضاً.\n\n'
        '**📥 استلام**\n'
        '• ⚡ فاتورة برق\n'
        '• ₿ عنوان على السلسلة\n'
        '• 🔄 صرف عبر ff.io (أي عملة ⇄ برق)\n\n'
        '**⚡ زاب (في المجموعات)**\n'
        '• رد على رسالة أحدهم: `/zap 1000`\n'
        '• أشر إلى أحدهم: `/zap 1000 @user`\n'
        '• مع ملاحظة ودولار: `/zap $5 @user أحسنت!`\n\n'
        '**📦 ودائع غير مطالب بها**\n'
        'الإعدادات → ودائع غير مطالب بها → مطالبة أو استرداد.\n\n'
        '**🧰 أدوات**\n'
        '`/contacts` دفتر العناوين • `/buy` شراء بيتكوين\n'
        '`/fees` رسوم الشبكة • `/sync` تحديث\n'
        '`/signmsg` / `/verifymsg` إثبات الملكية\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **احفظ عبارة الاسترداد — بدون عبارة لا محفظة!**\n\n'
        'تحتاج مساعدة؟ @zap_ln'
    ),
    'send_title': '📤 **إرسال**',
    'receive_title': '📥 **استلام**\n\nاختر طريقة الاستلام:',
    'btn_ln_invoice': '⚡ فاتورة برق', 'btn_onchain': '₿ عنوان على السلسلة',
    'btn_exchange': '🔄 صرف (ff.io)',
    'btn_prev': '⬅️ السابق', 'btn_next': 'التالي ➡️',
    'donate_title': '💝 **دعم البوت**\n\nشكراً لتفكيرك في الدعم!\n\nطريقتان للتبرع:\n\n1️⃣ **زاب مباشرة للبوت:**\n   أرسل الدفعة للبوت مباشرة!\n\n2️⃣ **إلى المطور:**\n   `you@wallet.domain`\n\nكل زاب يساعد في استمرار البوت! ⚡',
    'btn_zap_now': '⚡ زاب الآن',
    'donate_amount_title': '💝 **مبلغ التبرع**\n\nكم ساتوشي تريد التبرع؟\n\nمبالغ مقترحة:\n• 1,000 ساتوشي (☕ قهوة)\n• 5,000 ساتوشي (🍕 بيتزا)\n• 10,000 ساتوشي (❤️ داعم)\n• 21,000 ساتوشي (🚀 بطل!)\n➡️ أرسل المبلغ (ساتوشي أو دولار):',
    'err_generic': '❌ {error}',
    'synced': '✅ تمت المزامنة.\n💰 الرصيد: **{balance} ساتوشي**',
    # ---- receive v2 ----
    'your_ln_address': 'عنوان البرق الخاص بك',
    'invoice_no_amount_title': '📥 **فاتورتك** (بدون مبلغ)',
    'btn_set_amount': '💵 تحديد المبلغ',
    'btn_set_memo': '💬 تحديد ملاحظة',
    'btn_register_ln': '➕ تسجيل عنوان برق',
    'btn_back': '« رجوع',
    'ln_no_address': '⚡ **لا يوجد عنوان برق بعد**\n\nسجّل واحداً للاستلام بسهولة.',
    'onchain_title': '₿ **عنوانك على السلسلة**',
    'network_fee': 'رسوم الشبكة',
    'onchain_note': '💡 يتغير هذا العنوان في كل مرة — العناوين القديمة تبقى صالحة للأبد.',
    'your_onchain_address': 'عنوانك على السلسلة',
    'memo_label': 'ملاحظة',
    'invoice_title': '📥 **فاتورتك**',
    'available_line': '💰 المتاح:\n{amount}',
    'send_how': 'كيف تريد الدفع؟',
    'btn_ln_btc': '⚡ برق / بيتكوين',
    'send_paste_any': 'الصق فاتورة أو عنوان أو @مستخدم — أو أرسل صورة رمز QR 📷',
    'send_qr_hint': 'يمكنك أيضاً إرسال صورة رمز QR وسأقرأها لك.',
    'qr_not_found': 'لم أتمكن من قراءة رمز QR في الصورة. أرسل صورة أخرى أو الصق النص.',
    'amount_label': 'المبلغ',
    # ---- history ----
    'hist_title': '📜 **السجل** — صفحة {page}',
    'hist_empty_title': '📜 **السجل**',
    'hist_empty': 'لا شيء هنا بعد.\nأرسل أو استلم أول ساتوشي!',
    'btn_scan_payment': '🔍 فحص الدفعة',
    'hist_error': '❌ خطأ في تحميل السجل:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'جار تحميل العملات...',
    'ff_load_fail': 'فشل تحميل العملات.',
    'ff_recv_title': '🔄 **استلام عبر الصرافة (ff.io)**\n\nاختر العملة للإيداع:\nترسل العملة ← ff.io ← بيتكوين برق',
    'ff_send_title': '🔄 **إرسال عبر الصرافة (ff.io)**\n\nالرصيد: {balance}\n\nاختر العملة للاستلام:\nبيتكوين برق ← ff.io ← عملتك',
    'ff_terms': '⚠️ _خدمة الصرافة مقدمة من FixedFloat. إنشاء طلب يعني موافقتك على [شروط FixedFloat](https://ff.io/terms-of-service)._',
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

ES = {
    'app_title': '⚡ **Monedero ZAP**',
    'btn_send': '📤 Enviar', 'btn_receive': '📥 Recibir', 'btn_history': '📜 Historial',
    'btn_settings': '⚙️ Ajustes', 'btn_help': '❓ Ayuda', 'btn_donate': '💝 Donar',
    'btn_refresh': '🔄 Actualizar', 'btn_back_menu': '« Menú', 'btn_back_settings': '« Ajustes',
    'btn_back_tools': '« Herramientas', 'btn_cancel': '« Cancelar', 'btn_tools': '🧰 Herramientas',
    'btn_confirm': '✅ Confirmar', 'btn_try_again': '🔄 Reintentar', 'btn_skip': '⏭ Omitir',
    'balance_line': '💰 Saldo: {amount}',
    'backup_seed_hint': '🛡 Respalda tu semilla en ⚙️ Ajustes',
    'tools_title': '🧰 **Herramientas**\n\nElige una herramienta:',
    'btn_calculator': '🧮 Calculadora', 'btn_fees': '⛽ Tarifas',
    'btn_batch_pay': '📢 Pago por lotes', 'btn_contacts': '📒 Contactos',
    'btn_buy_bitcoin': '🟧 Comprar Bitcoin', 'btn_sync': '🔄 Sincronizar',
    'btn_sign_msg': '✍️ Firmar mensaje', 'btn_language': '🌐 Idioma',
    'settings_title': '⚙️ **Ajustes**',
    'btn_backup_seed': '🔐 Respaldar semilla', 'btn_restore': '🔄 Restaurar monedero',
    'btn_deposits': '📦 Depósitos sin reclamar', 'btn_ln_address': '⚡ Dirección Lightning',
    'btn_change_fiat': '💵 Moneda fiat', 'btn_send_password': '🔒 Contraseña de envío',
    'btn_private_mode': '👻 Modo privado', 
    'select_language': '🌐 **Idioma**\n\nActual: {current}',
    'lang_set': '✅ Idioma: {lang}',
    'help_text': (
        '⚡ **Monedero ZAP — Guía**\n\n'
        '**🚀 Empezar**\n'
        '1. `/start` — abrir el monedero\n'
        '2. Respalda tu semilla en ⚙️ Ajustes\n'
        '3. Envía y recibe Bitcoin\n\n'
        '**📤 Enviar**\n'
        'Pega una factura, LNURL, dirección Lightning, @usuario o dirección bc1 — el bot la detecta solo. Montos como `$5` también funcionan.\n\n'
        '**📥 Recibir**\n'
        '• ⚡ Factura Lightning\n'
        '• ₿ Dirección on-chain\n'
        '• 🔄 Intercambio vía ff.io (cualquier crypto ⇄ Lightning)\n\n'
        '**⚡ Zap (en grupos)**\n'
        '• Responde a un mensaje: `/zap 1000`\n'
        '• Menciona a alguien: `/zap 1000 @user`\n'
        '• Con nota y dólares: `/zap $5 @user ¡Bien hecho!`\n\n'
        '**📦 Depósitos sin reclamar**\n'
        'Ajustes → Depósitos sin reclamar → reclamar o reembolsar.\n\n'
        '**🧰 Herramientas**\n'
        '`/contacts` libreta de direcciones • `/buy` comprar Bitcoin\n'
        '`/fees` tarifas de red • `/sync` actualizar\n'
        '`/signmsg` / `/verifymsg` probar propiedad\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **¡Respalda tu semilla — sin semilla no hay monedero!**\n\n'
        '¿Necesitas ayuda? @zap_ln'
    ),
    'send_title': '📤 **Enviar**',
    'receive_title': '📥 **Recibir**\n\nElige un método:',
    'btn_ln_invoice': '⚡ Factura Lightning', 'btn_onchain': '₿ Dirección on-chain',
    'btn_exchange': '🔄 Intercambio (ff.io)',
    'btn_prev': '⬅️ Anterior', 'btn_next': 'Siguiente ➡️',
    'donate_title': '💝 **Apoya este bot**\n\n¡Gracias por considerar apoyar!\n\nDos formas de donar:\n\n1️⃣ **Zap directo al bot:**\n   ¡Solo envía un pago al bot!\n\n2️⃣ **Al desarrollador:**\n   `you@wallet.domain`\n\n¡Cada zap ayuda a mantener el bot! ⚡',
    'btn_zap_now': '⚡ Zap ahora',
    'donate_amount_title': '💝 **Monto de donación**\n\n¿Cuántos sats quieres donar?\n\nMontos sugeridos:\n• 1,000 sats (☕ Café)\n• 5,000 sats (🍕 Pizza)\n• 10,000 sats (❤️ Seguidor)\n• 21,000 sats (🚀 ¡Campeón!)\n➡️ Envía el monto (sats o $USD):',
    'err_generic': '❌ {error}',
    'synced': '✅ Sincronizado.\n💰 Saldo: **{balance} sats**',
    # ---- receive v2 ----
    'your_ln_address': 'Tu dirección Lightning',
    'invoice_no_amount_title': '📥 **Tu factura** (sin monto)',
    'btn_set_amount': '💵 Fijar monto',
    'btn_set_memo': '💬 Fijar nota',
    'btn_register_ln': '➕ Registrar dirección Lightning',
    'btn_back': '« Atrás',
    'ln_no_address': '⚡ **Aún no tienes dirección Lightning**\n\nRegistra una para recibir fácil.',
    'onchain_title': '₿ **Tu dirección on-chain**',
    'network_fee': 'Tarifa de red',
    'onchain_note': '💡 Esta dirección cambia cada vez — las antiguas siguen válidas para siempre.',
    'your_onchain_address': 'Tu dirección on-chain',
    'memo_label': 'Nota',
    'invoice_title': '📥 **Tu factura**',
    'available_line': '💰 Disponible: {amount}',
    'send_how': '¿Cómo quieres pagar?',
    'btn_ln_btc': '⚡ Lightning / BTC',
    'send_paste_any': 'Pega una factura, dirección o @usuario — o envía una foto del código QR 📷',
    'send_qr_hint': 'También puedes enviar una foto del código QR y la leo.',
    'qr_not_found': 'No pude leer un código QR en esa imagen. Prueba otra foto o pega el texto.',
    'amount_label': 'Monto',
    # ---- history ----
    'hist_title': '📜 **Historial** — pág. {page}',
    'hist_empty_title': '📜 **Historial**',
    'hist_empty': 'Nada aquí todavía.\n¡Envía o recibe tus primeros sats!',
    'btn_scan_payment': '🔍 Ver pago',
    'hist_error': '❌ Error al cargar el historial:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'Cargando monedas...',
    'ff_load_fail': 'Error al cargar monedas.',
    'ff_recv_title': '🔄 **Recibir vía exchange (ff.io)**\n\nElige la moneda a depositar:\nEnvías crypto → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Enviar vía exchange (ff.io)**\n\nSaldo: {balance}\n\nElige la moneda a recibir:\nLightning BTC → ff.io → tu crypto',
    'ff_terms': '⚠️ _El servicio de exchange lo provee FixedFloat. Crear una orden implica aceptar las [reglas de FixedFloat](https://ff.io/terms-of-service)._',
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

TR = {
    'app_title': '⚡ **ZAP Cüzdan**',
    'btn_send': '📤 Gönder', 'btn_receive': '📥 Al', 'btn_history': '📜 Geçmiş',
    'btn_settings': '⚙️ Ayarlar', 'btn_help': '❓ Yardım', 'btn_donate': '💝 Bağış',
    'btn_refresh': '🔄 Yenile', 'btn_back_menu': '« Menü', 'btn_back_settings': '« Ayarlar',
    'btn_back_tools': '« Araçlar', 'btn_cancel': '« İptal', 'btn_tools': '🧰 Araçlar',
    'btn_confirm': '✅ Onayla', 'btn_try_again': '🔄 Tekrar dene', 'btn_skip': '⏭ Atla',
    'balance_line': '💰 Bakiye: {amount}',
    'backup_seed_hint': "🛡 Seed ifrenizi ⚙️ Ayarlar'dan yedekleyin",
    'tools_title': '🧰 **Araçlar**\n\nBir araç seçin:',
    'btn_calculator': '🧮 Hesap makinesi', 'btn_fees': '⛽ Ücretler',
    'btn_batch_pay': '📢 Toplu ödeme', 'btn_contacts': '📒 Kişiler',
    'btn_buy_bitcoin': '🟧 Bitcoin satın al', 'btn_sync': '🔄 Senkronize',
    'btn_sign_msg': '✍️ Mesaj imzala', 'btn_language': '🌐 Dil',
    'settings_title': '⚙️ **Ayarlar**',
    'btn_backup_seed': '🔐 Seed yedekle', 'btn_restore': '🔄 Cüzdanı geri yükle',
    'btn_deposits': '📦 Talep edilmemiş mevduat', 'btn_ln_address': '⚡ Lightning adresi',
    'btn_change_fiat': '💵 Fiat para birimi', 'btn_send_password': '🔒 Gönderim şifresi',
    'btn_private_mode': '👻 Gizli mod', 
    'select_language': '🌐 **Dil**\n\nMevcut: {current}',
    'lang_set': '✅ Dil: {lang}',
    'help_text': (
        '⚡ **ZAP Cüzdan — Kılavuz**\n\n'
        '**🚀 Başlangıç**\n'
        '1. `/start` — cüzdanı aç\n'
        "2. Seed ifrenizi ⚙️ Ayarlar'dan yedekleyin\n"
        '3. Bitcoin gönderin ve alın\n\n'
        '**📤 Gönder**\n'
        'Fatura, LNURL, Lightning adresi, @kullanıcı veya bc1 adresi yapıştırın — bot otomatik algılar. `$5` gibi tutarlar da çalışır.\n\n'
        '**📥 Al**\n'
        '• ⚡ Lightning faturası\n'
        '• ₿ Zincir üstü adres\n'
        '• 🔄 ff.io ile takas (her kripto ⇄ Lightning)\n\n'
        '**⚡ Zap (gruplarda)**\n'
        "• Birine cevap verin: `/zap 1000`\n"
        "• Birini etiketleyin: `/zap 1000 @user`\n"
        "• Not ve dolarla: `/zap $5 @user Harika!`\n\n"
        '**📦 Talep edilmemiş mevduat**\n'
        'Ayarlar → Talep edilmemiş mevduat → talep et veya iade et.\n\n'
        '**🧰 Araçlar**\n'
        '`/contacts` adres defteri • `/buy` Bitcoin satın al\n'
        '`/fees` ağ ücretleri • `/sync` yenile\n'
        '`/signmsg` / `/verifymsg` sahiplik kanıtı\n\n'
        '━━━━━━━━━━━━━━━\n'
        '💡 **Seed ifrenizi yedekleyin — seed yoksa cüzdan yok!**\n\n'
        'Yardım mı lazım? @zap_ln'
    ),
    'send_title': '📤 **Gönder**',
    'receive_title': '📥 **Al**\n\nBir yöntem seçin:',
    'btn_ln_invoice': '⚡ Lightning faturası', 'btn_onchain': '₿ Zincir üstü adres',
    'btn_exchange': '🔄 Takas (ff.io)',
    'btn_prev': '⬅️ Önceki', 'btn_next': 'Sonraki ➡️',
    'donate_title': '💝 **Bu botu destekle**\n\nDesteklemeyi düşündüğünüz için teşekkürler!\n\nİki bağış yolu:\n\n1️⃣ **Doğrudan bota zap:**\n   Bota bir ödeme gönderin!\n\n2️⃣ **Geliştiriciye:**\n   `you@wallet.domain`\n\nHer zap botun çalışmasına yardım eder! ⚡',
    'btn_zap_now': '⚡ Şimdi zapla',
    'donate_amount_title': '💝 **Bağış miktarı**\n\nKaç satoshi bağışlamak istersiniz?\n\nÖnerilen miktarlar:\n• 1,000 satoshi (☕ Kahve)\n• 5,000 satoshi (🍕 Pizza)\n• 10,000 satoshi (❤️ Destekçi)\n• 21,000 satoshi (🚀 Şampiyon!)\n➡️ Tutarı gönderin (satoshi veya dolar):',
    'err_generic': '❌ {error}',
    'synced': '✅ Senkronize edildi.\n💰 Bakiye: **{balance} satoshi**',
    # ---- receive v2 ----
    'your_ln_address': 'Lightning adresiniz',
    'invoice_no_amount_title': '📥 **Faturanız** (tutar yok)',
    'btn_set_amount': '💵 Tutar ayarla',
    'btn_set_memo': '💬 Not ayarla',
    'btn_register_ln': '➕ Lightning adresi kaydet',
    'btn_back': '« Geri',
    'ln_no_address': '⚡ **Henüz Lightning adresiniz yok**\n\nKolay alım için bir tane kaydedin.',
    'onchain_title': '₿ **Zincir üstü adresiniz**',
    'network_fee': 'Ağ ücreti',
    'onchain_note': '💡 Bu adres her seferinde değişir — eskileri sonsuza kadar geçerlidir.',
    'your_onchain_address': 'Zincir üstü adresiniz',
    'memo_label': 'Not',
    'invoice_title': '📥 **Faturanız**',
    'available_line': '💰 Mevcut: {amount}',
    'send_how': 'Nasıl ödemek istersiniz?',
    'btn_ln_btc': '⚡ Lightning / BTC',
    'send_paste_any': 'Fatura, adres veya @kullanıcı adı yapıştırın — veya QR kod fotoğrafı gönderin 📷',
    'send_qr_hint': "QR kod fotoğrafı da gönderebilirsiniz, ben okurum.",
    'qr_not_found': 'Bu görselde QR kodu okuyamadım. Başka bir fotoğraf deneyin veya metni yapıştırın.',
    'amount_label': 'Tutar',
    # ---- history ----
    'hist_title': '📜 **Geçmiş** — sayfa {page}',
    'hist_empty_title': '📜 **Geçmiş**',
    'hist_empty': "Burada henüz bir şey yok.\nİlk satoshi'nizi gönderin veya alın!",
    'btn_scan_payment': '🔍 Ödemeyi sorgula',
    'hist_error': '❌ Geçmiş yüklenirken hata:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'Para birimleri yükleniyor...',
    'ff_load_fail': 'Para birimleri yüklenemedi.',
    'ff_recv_title': '🔄 **Takas ile al (ff.io)**\n\nYatırmak istediğiniz coini seçin:\nCoin gönderirsiniz → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Takas ile gönder (ff.io)**\n\nBakiye: {balance}\n\nAlmak istediğiniz coini seçin:\nLightning BTC → ff.io → coininiz',
    'ff_terms': '⚠️ _Takas servisi FixedFloat tarafından sağlanır. Sipariş oluşturmak [FixedFloat kurallarını](https://ff.io/terms-of-service) kabul etmek anlamına gelir._',
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

# Partial packs — common buttons only; rest falls back to English.
DE = {
    'btn_send': '📤 Senden', 'btn_receive': '📥 Empfangen', 'btn_history': '📜 Verlauf',
    'btn_settings': '⚙️ Einstellungen', 'btn_help': '❓ Hilfe', 'btn_donate': '💝 Spenden',
    'btn_back_menu': '« Menü', 'btn_back_settings': '« Einstellungen', 'btn_cancel': '« Abbrechen',
    'btn_back_tools': '« Werkzeuge', 'btn_tools': '🧰 Werkzeuge', 'btn_confirm': '✅ Bestätigen',
    'btn_language': '🌐 Sprache', 'balance_line': '💰 Guthaben: {amount}',
    'btn_buy_bitcoin': '🟧 Bitcoin kaufen', 
    'settings_title': '⚙️ **Einstellungen**',
    'tools_title': '🧰 **Werkzeuge**\n\nWerkzeug wählen:', 'btn_calculator': '🧮 Rechner',
    'btn_fees': '⛽ Gebühren', 'btn_batch_pay': '📢 Sammelzahlung', 'btn_contacts': '📒 Kontakte',
    'btn_sync': '🔄 Synchronisieren', 'btn_sign_msg': '✍️ Nachricht signieren',
    'select_language': '🌐 **Sprache**\n\nAktuell: {current}', 'lang_set': '✅ Sprache: {lang}',
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
FR = {
    'btn_send': '📤 Envoyer', 'btn_receive': '📥 Recevoir', 'btn_history': '📜 Historique',
    'btn_settings': '⚙️ Réglages', 'btn_help': '❓ Aide', 'btn_donate': '💝 Faire un don',
    'btn_back_menu': '« Menu', 'btn_back_settings': '« Réglages', 'btn_cancel': '« Annuler',
    'btn_back_tools': '« Outils', 'btn_tools': '🧰 Outils', 'btn_confirm': '✅ Confirmer',
    'btn_language': '🌐 Langue', 'balance_line': '💰 Solde : {amount}',
    'btn_buy_bitcoin': '🟧 Acheter du Bitcoin', 
    'settings_title': '⚙️ **Réglages**',
    'tools_title': '🧰 **Outils**\n\nChoisissez un outil :', 'btn_calculator': '🧮 Calculatrice',
    'btn_fees': '⛽ Frais', 'btn_batch_pay': '📢 Paiement groupé', 'btn_contacts': '📒 Contacts',
    'btn_sync': '🔄 Synchroniser', 'btn_sign_msg': '✍️ Signer un message',
    'select_language': '🌐 **Langue**\n\nActuel : {current}', 'lang_set': '✅ Langue : {lang}',
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
PT = {
    'btn_send': '📤 Enviar', 'btn_receive': '📥 Receber', 'btn_history': '📜 Histórico',
    'btn_settings': '⚙️ Configurações', 'btn_help': '❓ Ajuda', 'btn_donate': '💝 Doar',
    'btn_back_menu': '« Menu', 'btn_back_settings': '« Configurações', 'btn_cancel': '« Cancelar',
    'btn_back_tools': '« Ferramentas', 'btn_tools': '🧰 Ferramentas', 'btn_confirm': '✅ Confirmar',
    'btn_language': '🌐 Idioma', 'balance_line': '💰 Saldo: {amount}',
    'btn_buy_bitcoin': '🟧 Comprar Bitcoin', 
    'settings_title': '⚙️ **Configurações**',
    'tools_title': '🧰 **Ferramentas**\n\nEscolha uma ferramenta:', 'btn_calculator': '🧮 Calculadora',
    'btn_fees': '⛽ Taxas', 'btn_batch_pay': '📢 Pagamento em lote', 'btn_contacts': '📒 Contatos',
    'btn_sync': '🔄 Sincronizar', 'btn_sign_msg': '✍️ Assinar mensagem',
    'select_language': '🌐 **Idioma**\n\nAtual: {current}', 'lang_set': '✅ Idioma: {lang}',
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
HI = {
    'btn_send': '📤 भेजें', 'btn_receive': '📥 प्राप्त करें', 'btn_history': '📜 इतिहास',
    'btn_settings': '⚙️ सेटिंग्स', 'btn_help': '❓ सहायता', 'btn_donate': '💝 दान करें',
    'btn_back_menu': '« मेनू', 'btn_back_settings': '« सेटिंग्स', 'btn_cancel': '« रद्द करें',
    'btn_back_tools': '« उपकरण', 'btn_tools': '🧰 उपकरण', 'btn_confirm': '✅ पुष्टि करें',
    'btn_language': '🌐 भाषा', 'balance_line': '💰 शेष: {amount}',
    'btn_buy_bitcoin': '🟧 बिटकॉइन खरीदें', 
    'settings_title': '⚙️ **सेटिंग्स**',
    'tools_title': '🧰 **उपकरण**\n\nएक उपकरण चुनें:', 'btn_calculator': '🧮 कैलकुलेटर',
    'btn_fees': '⛽ शुल्क', 'btn_batch_pay': '📢 बैच भुगतान', 'btn_contacts': '📒 संपर्क',
    'btn_sync': '🔄 सिंक', 'btn_sign_msg': '✍️ संदेश साइन करें',
    'select_language': '🌐 **भाषा**\n\nवर्तमान: {current}', 'lang_set': '✅ भाषा: {lang}',
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
ID = {
    'btn_send': '📤 Kirim', 'btn_receive': '📥 Terima', 'btn_history': '📜 Riwayat',
    'btn_settings': '⚙️ Pengaturan', 'btn_help': '❓ Bantuan', 'btn_donate': '💝 Donasi',
    'btn_back_menu': '« Menu', 'btn_back_settings': '« Pengaturan', 'btn_cancel': '« Batal',
    'btn_back_tools': '« Alat', 'btn_tools': '🧰 Alat', 'btn_confirm': '✅ Konfirmasi',
    'btn_language': '🌐 Bahasa', 'balance_line': '💰 Saldo: {amount}',
    'btn_buy_bitcoin': '🟧 Beli Bitcoin', 
    'settings_title': '⚙️ **Pengaturan**',
    'tools_title': '🧰 **Alat**\n\nPilih alat:', 'btn_calculator': '🧮 Kalkulator',
    'btn_fees': '⛽ Biaya', 'btn_batch_pay': '📢 Bayar massal', 'btn_contacts': '📒 Kontak',
    'btn_sync': '🔄 Sinkron', 'btn_sign_msg': '✍️ Tanda tangani pesan',
    'select_language': '🌐 **Bahasa**\n\nSaat ini: {current}', 'lang_set': '✅ Bahasa: {lang}',
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

TRANSLATIONS = {
    'en': EN, 'fa': FA, 'ru': RU, 'zh': ZH, 'ar': AR, 'es': ES,
    'tr': TR, 'de': DE, 'fr': FR, 'pt': PT, 'hi': HI, 'id': ID,
}


# ===================== Persistence (wired to the bot's KeyValueSqlite db) =====================
_db = None  # set via init_i18n(db)


def init_i18n(db):
    """Wire the i18n store to the bot's KeyValueSqlite instance."""
    global _db
    _db = db


def _lang_map():
    return _db.get('user_lang') if _db is not None else {}


def get_user_lang(user_id):
    """Stored language for user, else English."""
    m = _lang_map() or {}
    return m.get(str(user_id), DEFAULT_LANG)


def set_user_lang(user_id, lang):
    if lang not in TRANSLATIONS:
        lang = DEFAULT_LANG
    m = dict(_lang_map() or {})
    m[str(user_id)] = lang
    if _db is not None:
        _db.set('user_lang', m)
    return lang


def detect_lang_code(telegram_lang_code):
    """Map a Telegram client language code to a supported language."""
    if not telegram_lang_code:
        return None
    return LANG_ALIASES.get(telegram_lang_code.lower().strip())


def t(user_id, key, **kwargs):
    """Translate a key for a user, with {placeholder} formatting."""
    lang = get_user_lang(user_id)
    pack = TRANSLATIONS.get(lang) or {}
    s = pack.get(key) or EN.get(key) or key
    if kwargs:
        try:
            s = s.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return s


def tb(user_id, key):
    """Callback-data for language buttons: `setlang:<code>`."""
    return f"setlang:{get_user_lang(user_id)}"
