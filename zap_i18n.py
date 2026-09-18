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
    'btn_emergency_exit': '🚨 Emergency Exit',
    'btn_advanced': '🔧 Advanced',
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
        'Need help? @your_bot'
    ),

    # ---- send ----
    'send_title': '📤 **Send**',
    'send_prompt': 'Paste an invoice, LNURL, lightning address, @username, or bc1 address.\nAmounts like `$5` work too.',
    'send_confirm': 'Confirm sending **{amount}**?',
    'send_done': '✅ Sent!',
    'send_amount_prompt': 'Send the amount in sats (or `$5`):\n\n💡 Balance: **{balance}**',

    # ---- receive ----
    'receive_title': '📥 **Receive**\n\nChoose a receive method:',
    'btn_ln_invoice': '⚡ Lightning Invoice',
    'btn_onchain': '₿ Onchain Address',
    'btn_exchange': '🔄 Exchange (ff.io)',

    # ---- history ----
    'history_title': '📜 **History** — page {page}',
    'history_empty': 'Nothing here yet. Send or receive your first sats!',
    'btn_prev': '⬅️ Previous',
    'btn_next': 'Next ➡️',

    # ---- donate ----
    'donate_title': '💝 **Support This Bot**\n\nThank you for considering to support!\n\nYou can donate in two ways:\n\n1️⃣ **Zap directly to this bot:**\n   Just send a payment to the bot!\n\n2️⃣ **Send to developer:**\n   `your_wallet@example.com`\n\nEvery zap helps keep this bot running! ⚡',
    'btn_zap_now': '⚡ Zap Now',
    'donate_amount_title': '💝 **Donate Amount**\n\nHow many sats would you like to donate?\n\nSuggested amounts:\n• 1,000 sats (☕ Coffee)\n• 5,000 sats (🍕 Pizza)\n• 10,000 sats (❤️ Supporter)\n• 21,000 sats (🚀 Champion!)\n➡️ Send the amount (sats or $USD):',
    'btn_cancel_donate': '« Cancel',

    # ---- common ----
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ Session expired. Start again.',
    'insufficient': '❌ Insufficient balance!',
    'loading': '⏳ Loading...',
    'synced': '✅ Synced.\n💰 Balance: **{balance} sats**',
    'buy_title': '🟧 **Buy Bitcoin**\n\n{url}',
    'fees_loading': '⛽ Loading...',
    'buy_title': '🟧 **Buy Bitcoin**\n\n{url}',
    'fees_loading': '⛽ Loading...',
    'sync_loading': '🔄 Syncing...',
    # ---- receive v2 (user-friendly) ----
    'receive_title': '📥 **Receive**',
    'your_ln_address': 'Your Lightning Address',
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
    'qr_decoded': '✅ QR read successfully!',
    'amount_label': 'Amount',
    # ---- history ----
    'hist_loading': '📜 Loading history...',
    'hist_title': '📜 **History** — page {page}',
    'hist_empty_title': '📜 **History**',
    'hist_empty': 'Nothing here yet.\nSend or receive your first sats!',
    'hist_received': 'Received',
    'hist_sent': 'Sent',
    'hist_details': 'Details',
    'btn_prev': '⬅️ Previous',
    'btn_next': 'Next ➡️',
    'btn_scan_payment': '🔍 Scan Payment',
    'hist_error': '❌ Error loading history:\n{error}',
    # ---- ff.io exchange ----
    'ff_loading': 'Loading currencies...',
    'ff_load_fail': 'Failed to load currencies.',
    'ff_recv_title': '🔄 **Receive via Exchange (ff.io)**\n\nSelect the coin you want to deposit:\nYou send crypto → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Send via Exchange (ff.io)**\n\nBalance: {balance}\n\nSelect the coin you want to receive:\nLightning BTC → ff.io → your crypto',
    'ff_terms': '⚠️ _The exchange service is provided by FixedFloat. Creating an order confirms your agreement with the [FixedFloat rules](https://ff.io/terms-of-service)._'
}

# Full translations for major languages; partial packs fall back to EN per key.
FA = {
    'app_title': '⚡ **کیف پول زپ**',
    'btn_send': '📤 ارسال', 'btn_receive': '📥 دریافت', 'btn_history': '📜 تاریخچه',
    'btn_settings': '⚙️ تنظیمات', 'btn_help': '❓ راهنما', 'btn_donate': '💝 حمایت',
    'btn_refresh': '🔄 بروزرسانی', 'btn_back_menu': '« منو', 'btn_back_settings': '« تنظیمات',
    'btn_back_tools': '« ابزارها', 'btn_cancel': '« لغو', 'btn_tools': '🧰 ابزارها',
    'btn_confirm': '✅ تایید', 'btn_try_again': '🔄 تلاش دوباره', 'btn_skip': '⏭ رد کردن',
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
    'btn_private_mode': '👻 حالت خصوصی', 'btn_emergency_exit': '🚨 خروج اضطراری',
    'btn_advanced': '🔧 پیشرفته',
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
        'سوال داری؟ @your_bot'
    ),
    'send_title': '📤 **ارسال**',
    'send_prompt': 'فاکتور، LNURL، آدرس لایتنینگ، @یوزرنیم یا آدرس bc1 رو پیست کن.\nمبلغ دلاری مثل `$5` هم قبوله.',
    'send_confirm': 'ارسال **{amount}** تایید بشه؟',
    'send_done': '✅ ارسال شد!',
    'send_amount_prompt': 'مبلغ به ساتوشی بفرست (یا `$5`):\n\n💡 موجودی: **{balance}**',
    'receive_title': '📥 **دریافت**\n\nروش دریافت رو انتخاب کن:',
    'btn_ln_invoice': '⚡ فاکتور لایتنینگ', 'btn_onchain': '₿ آدرس آنچین',
    'btn_exchange': '🔄 صرافی (ff.io)',
    'history_title': '📜 **تاریخچه** — صفحه {page}',
    'history_empty': 'هنوز چیزی اینجا نیست. اولین ساتوشی‌هات رو بفرست یا دریافت کن!',
    'btn_prev': '⬅️ قبلی', 'btn_next': 'بعدی ➡️',
    'donate_title': '💝 **حمایت از ربات**\n\nممنون که به حمایت فکر کردی!\n\nدو راه داری:\n\n1️⃣ **زپ مستقیم به ربات:**\n   فقط یه پرداخت به ربات بفرست!\n\n2️⃣ **واریز به توسعه‌دهنده:**\n   `your_wallet@example.com`\n\nهر زپ به ادامه کار ربات کمک میکنه! ⚡',
    'btn_zap_now': '⚡ زپ کن',
    'donate_amount_title': '💝 **مبلغ حمایت**\n\nچند ساتوشی میخوای حمایت کنی؟\n\nمبلغ‌های پیشنهادی:\n• 1,000 ساتوشی (☕ قهوه)\n• 5,000 ساتوشی (🍕 پیتزا)\n• 10,000 ساتوشی (❤️ حامی)\n• 21,000 ساتوشی (🚀 قهرمان!)\n➡️ مبلغ رو بفرست (ساتوشی یا دلار):',
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ نشست منقضی شد. دوباره شروع کن.',
    'insufficient': '❌ موجودی کافی نیست!',
    'loading': '⏳ در حال بارگذاری...',
    'synced': '✅ همگام شد.\n💰 موجودی: **{balance} ساتوشی**',
    'buy_title': '🟧 **خرید بیت‌کوین**\n\n{url}',
    'fees_loading': '⛽ در حال بارگذاری...',
    'buy_title': '🟧 **خرید بیت‌کوین**\n\n{url}',
    'fees_loading': '⛽ در حال بارگذاری...',
    'sync_loading': '🔄 در حال همگام‌سازی...',
    # ---- receive v2 ----
    'receive_title': '📥 **دریافت**',
    'your_ln_address': 'آدرس لایتنینگ شما',
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
    'qr_decoded': '✅ کد QR با موفقیت خونده شد!',
    'amount_label': 'مبلغ',
    # ---- history ----
    'hist_loading': '📜 در حال بارگذاری تاریخچه...',
    'hist_title': '📜 **تاریخچه** — صفحه {page}',
    'hist_empty_title': '📜 **تاریخچه**',
    'hist_empty': 'هنوز چیزی اینجا نیست.\nاولین ساتوشی‌هات رو بفرست یا دریافت کن!',
    'hist_received': 'دریافت شد',
    'hist_sent': 'ارسال شد',
    'hist_details': 'جزئیات',
    'btn_prev': '⬅️ قبلی',
    'btn_next': 'بعدی ➡️',
    'btn_scan_payment': '🔍 بررسی پرداخت',
    'hist_error': '❌ خطا در بارگذاری تاریخچه:\n{error}',
    # ---- ff.io exchange ----
    'ff_loading': 'در حال بارگذاری ارزها...',
    'ff_load_fail': 'بارگذاری ارزها ناموفق بود.',
    'ff_recv_title': '🔄 **دریافت با صرافی (ff.io)**\n\nارزی که میخوای واریز کنی رو انتخاب کن:\nتو ارسال میکنی ← ff.io ← بیت‌کوین لایتنینگ',
    'ff_send_title': '🔄 **ارسال با صرافی (ff.io)**\n\nموجودی: {balance}\n\nارزی که میخوای دریافت کنی رو انتخاب کن:\nبیت‌کوین لایتنینگ ← ff.io ← ارز تو',
    'ff_terms': '⚠️ _سرویس صرافی توسط FixedFloat ارائه میشه. ساخت سفارش یعنی قبول [قوانین FixedFloat](https://ff.io/terms-of-service)._'
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
    'btn_private_mode': '👻 Приватный режим', 'btn_emergency_exit': '🚨 Аварийный выход',
    'btn_advanced': '🔧 Расширенные',
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
        'Нужна помощь? @your_bot'
    ),
    'send_title': '📤 **Отправить**',
    'send_prompt': 'Вставьте инвойс, LNURL, lightning адрес, @username или bc1 адрес.\nСуммы вроде `$5` тоже работают.',
    'send_confirm': 'Отправить **{amount}**?',
    'send_done': '✅ Отправлено!',
    'send_amount_prompt': 'Отправьте сумму в сатоши (или `$5`):\n\n💡 Баланс: **{balance}**',
    'receive_title': '📥 **Получить**\n\nВыберите способ получения:',
    'btn_ln_invoice': '⚡ Lightning инвойс', 'btn_onchain': '₿ Onchain адрес',
    'btn_exchange': '🔄 Обмен (ff.io)',
    'history_title': '📜 **История** — стр. {page}',
    'history_empty': 'Здесь пока пусто. Отправьте или получите первые сатоши!',
    'btn_prev': '⬅️ Назад', 'btn_next': 'Вперёд ➡️',
    'donate_title': '💝 **Поддержать бота**\n\nСпасибо, что думаете о поддержке!\n\nДва способа:\n\n1️⃣ **Зап напрямую боту:**\n   Просто отправьте платёж боту!\n\n2️⃣ **Разработчику:**\n   `your_wallet@example.com`\n\nКаждый зап помогает боту работать! ⚡',
    'btn_zap_now': '⚡ Запнуть',
    'donate_amount_title': '💝 **Сумма доната**\n\nСколько сатоши хотите отправить?\n\nРекомендуемые суммы:\n• 1,000 сатоши (☕ Кофе)\n• 5,000 сатоши (🍕 Пицца)\n• 10,000 сатоши (❤️ Поддержка)\n• 21,000 сатоши (🚀 Чемпион!)\n➡️ Отправьте сумму (сатоши или $USD):',
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ Сессия истекла. Начните заново.',
    'insufficient': '❌ Недостаточно средств!',
    'loading': '⏳ Загрузка...',
    'synced': '✅ Синхронизировано.\n💰 Баланс: **{balance} сатоши**',
    'buy_title': '🟧 **Купить Bitcoin**\n\n{url}',
    'fees_loading': '⛽ Загрузка...',
    'buy_title': '🟧 **Купить Bitcoin**\n\n{url}',
    'fees_loading': '⛽ Загрузка...',
    'sync_loading': '🔄 Синхронизация...',
    # ---- receive v2 ----
    'receive_title': '📥 **Получить**',
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
    'qr_decoded': '✅ QR-код успешно прочитан!',
    'amount_label': 'Сумма',
    # ---- history ----
    'hist_loading': '📜 Загрузка истории...',
    'hist_title': '📜 **История** — стр. {page}',
    'hist_empty_title': '📜 **История**',
    'hist_empty': 'Здесь пока пусто.\nОтправьте или получите первые сатоши!',
    'hist_received': 'Получено',
    'hist_sent': 'Отправлено',
    'hist_details': 'Детали',
    'btn_prev': '⬅️ Назад',
    'btn_next': 'Вперёд ➡️',
    'btn_scan_payment': '🔍 Проверить платёж',
    'hist_error': '❌ Ошибка загрузки истории:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'Загрузка валют...',
    'ff_load_fail': 'Не удалось загрузить валюты.',
    'ff_recv_title': '🔄 **Получить через обменник (ff.io)**\n\nВыберите монету для депозита:\nВы отправляете крипту → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Отправить через обменник (ff.io)**\n\nБаланс: {balance}\n\nВыберите монету для получения:\nLightning BTC → ff.io → ваша крипта',
    'ff_terms': '⚠️ _Сервис обмена предоставляет FixedFloat. Создание ордера означает согласие с [правилами FixedFloat](https://ff.io/terms-of-service)._'
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
    'btn_private_mode': '👻 隐私模式', 'btn_emergency_exit': '🚨 紧急退出',
    'btn_advanced': '🔧 高级',
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
        '需要帮助? @your_bot'
    ),
    'send_title': '📤 **发送**',
    'send_prompt': '粘贴发票、LNURL、闪电地址、@用户名或 bc1 地址。\n也支持 `$5` 这样的金额。',
    'send_confirm': '确认发送 **{amount}**？',
    'send_done': '✅ 已发送！',
    'send_amount_prompt': '发送聪数量 (或 `$5`):\n\n💡 余额: **{balance}**',
    'receive_title': '📥 **接收**\n\n选择接收方式:',
    'btn_ln_invoice': '⚡ 闪电发票', 'btn_onchain': '₿ 链上地址',
    'btn_exchange': '🔄 兑换 (ff.io)',
    'history_title': '📜 **历史** — 第 {page} 页',
    'history_empty': '这里还是空的。发送或接收您的第一聪吧！',
    'btn_prev': '⬅️ 上一页', 'btn_next': '下一页 ➡️',
    'donate_title': '💝 **支持本机器人**\n\n感谢您的支持！\n\n两种捐赠方式：\n\n1️⃣ **直接给机器人发 ZAP：**\n   直接给机器人发送付款即可！\n\n2️⃣ **发送给开发者：**\n   `your_wallet@example.com`\n\n每个 ZAP 都能帮助机器人运行！⚡',
    'btn_zap_now': '⚡ 立即 ZAP',
    'donate_amount_title': '💝 **捐赠金额**\n\n您想捐赠多少聪？\n\n建议金额：\n• 1,000 聪 (☕ 咖啡)\n• 5,000 聪 (🍕 披萨)\n• 10,000 聪 (❤️ 支持者)\n• 21,000 聪 (🚀 冠军！)\n➡️ 发送金额 (聪或美元):',
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ 会话已过期。请重新开始。',
    'insufficient': '❌ 余额不足！',
    'loading': '⏳ 加载中...',
    'synced': '✅ 已同步。\n💰 余额: **{balance} 聪**',
    'buy_title': '🟧 **购买比特币**\n\n{url}',
    'fees_loading': '⛽ 加载中...',
    'buy_title': '🟧 **购买比特币**\n\n{url}',
    'fees_loading': '⛽ 加载中...',
    'sync_loading': '🔄 同步中...',
    # ---- receive v2 ----
    'receive_title': '📥 **接收**',
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
    'qr_decoded': '✅ 二维码识别成功！',
    'amount_label': '金额',
    # ---- history ----
    'hist_loading': '📜 正在加载历史...',
    'hist_title': '📜 **历史** — 第 {page} 页',
    'hist_empty_title': '📜 **历史**',
    'hist_empty': '这里还是空的。\n发送或接收您的第一聪吧！',
    'hist_received': '已接收',
    'hist_sent': '已发送',
    'hist_details': '详情',
    'btn_prev': '⬅️ 上一页',
    'btn_next': '下一页 ➡️',
    'btn_scan_payment': '🔍 查询付款',
    'hist_error': '❌ 加载历史出错：\n{error}',
    # ---- ff.io ----
    'ff_loading': '正在加载币种...',
    'ff_load_fail': '加载币种失败。',
    'ff_recv_title': '🔄 **通过交易所接收 (ff.io)**\n\n选择要存入的币种：\n您发送加密货币 → ff.io → 闪电比特币',
    'ff_send_title': '🔄 **通过交易所发送 (ff.io)**\n\n余额: {balance}\n\n选择要接收的币种：\n闪电比特币 → ff.io → 您的加密货币',
    'ff_terms': '⚠️ _兑换服务由 FixedFloat 提供。创建订单即表示您同意 [FixedFloat 条款](https://ff.io/terms-of-service)。_'
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
    'btn_private_mode': '👻 الوضع الخاص', 'btn_emergency_exit': '🚨 خروج طارئ',
    'btn_advanced': '🔧 متقدم',
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
        'تحتاج مساعدة؟ @your_bot'
    ),
    'send_title': '📤 **إرسال**',
    'send_prompt': 'الصق فاتورة أو LNURL أو عنوان برق أو @مستخدم أو عنوان bc1.\nالمبالغ مثل `$5` مدعومة أيضاً.',
    'send_confirm': 'تأكيد إرسال **{amount}**؟',
    'send_done': '✅ تم الإرسال!',
    'send_amount_prompt': 'أرسل المبلغ بالساتوشي (أو `$5`):\n\n💡 الرصيد: **{balance}**',
    'receive_title': '📥 **استلام**\n\nاختر طريقة الاستلام:',
    'btn_ln_invoice': '⚡ فاتورة برق', 'btn_onchain': '₿ عنوان على السلسلة',
    'btn_exchange': '🔄 صرف (ff.io)',
    'history_title': '📜 **السجل** — صفحة {page}',
    'history_empty': 'لا شيء هنا بعد. أرسل أو استلم أول ساتوشي!',
    'btn_prev': '⬅️ السابق', 'btn_next': 'التالي ➡️',
    'donate_title': '💝 **دعم البوت**\n\nشكراً لتفكيرك في الدعم!\n\nطريقتان للتبرع:\n\n1️⃣ **زاب مباشرة للبوت:**\n   أرسل الدفعة للبوت مباشرة!\n\n2️⃣ **إلى المطور:**\n   `your_wallet@example.com`\n\nكل زاب يساعد في استمرار البوت! ⚡',
    'btn_zap_now': '⚡ زاب الآن',
    'donate_amount_title': '💝 **مبلغ التبرع**\n\nكم ساتوشي تريد التبرع؟\n\nمبالغ مقترحة:\n• 1,000 ساتوشي (☕ قهوة)\n• 5,000 ساتوشي (🍕 بيتزا)\n• 10,000 ساتوشي (❤️ داعم)\n• 21,000 ساتوشي (🚀 بطل!)\n➡️ أرسل المبلغ (ساتوشي أو دولار):',
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ انتهت الجلسة. ابدأ من جديد.',
    'insufficient': '❌ الرصيد غير كافٍ!',
    'loading': '⏳ جار التحميل...',
    'synced': '✅ تمت المزامنة.\n💰 الرصيد: **{balance} ساتوشي**',
    'buy_title': '🟧 **شراء بيتكوين**\n\n{url}',
    'fees_loading': '⛽ جار التحميل...',
    'buy_title': '🟧 **شراء بيتكوين**\n\n{url}',
    'fees_loading': '⛽ جار التحميل...',
    'sync_loading': '🔄 جار المزامنة...',
    # ---- receive v2 ----
    'receive_title': '📥 **استلام**',
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
    'qr_decoded': '✅ تمت قراءة رمز QR بنجاح!',
    'amount_label': 'المبلغ',
    # ---- history ----
    'hist_loading': '📜 جار تحميل السجل...',
    'hist_title': '📜 **السجل** — صفحة {page}',
    'hist_empty_title': '📜 **السجل**',
    'hist_empty': 'لا شيء هنا بعد.\nأرسل أو استلم أول ساتوشي!',
    'hist_received': 'استلام',
    'hist_sent': 'إرسال',
    'hist_details': 'التفاصيل',
    'btn_prev': '⬅️ السابق',
    'btn_next': 'التالي ➡️',
    'btn_scan_payment': '🔍 فحص الدفعة',
    'hist_error': '❌ خطأ في تحميل السجل:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'جار تحميل العملات...',
    'ff_load_fail': 'فشل تحميل العملات.',
    'ff_recv_title': '🔄 **استلام عبر الصرافة (ff.io)**\n\nاختر العملة للإيداع:\nترسل العملة ← ff.io ← بيتكوين برق',
    'ff_send_title': '🔄 **إرسال عبر الصرافة (ff.io)**\n\nالرصيد: {balance}\n\nاختر العملة للاستلام:\nبيتكوين برق ← ff.io ← عملتك',
    'ff_terms': '⚠️ _خدمة الصرافة مقدمة من FixedFloat. إنشاء طلب يعني موافقتك على [شروط FixedFloat](https://ff.io/terms-of-service)._'
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
    'btn_private_mode': '👻 Modo privado', 'btn_emergency_exit': '🚨 Salida de emergencia',
    'btn_advanced': '🔧 Avanzado',
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
        '¿Necesitas ayuda? @your_bot'
    ),
    'send_title': '📤 **Enviar**',
    'send_prompt': 'Pega una factura, LNURL, dirección Lightning, @usuario o dirección bc1.\nMontos como `$5` también funcionan.',
    'send_confirm': '¿Confirmar envío de **{amount}**?',
    'send_done': '✅ ¡Enviado!',
    'send_amount_prompt': 'Envía el monto en sats (o `$5`):\n\n💡 Saldo: **{balance}**',
    'receive_title': '📥 **Recibir**\n\nElige un método:',
    'btn_ln_invoice': '⚡ Factura Lightning', 'btn_onchain': '₿ Dirección on-chain',
    'btn_exchange': '🔄 Intercambio (ff.io)',
    'history_title': '📜 **Historial** — pág. {page}',
    'history_empty': 'Nada aquí todavía. ¡Envía o recibe tus primeros sats!',
    'btn_prev': '⬅️ Anterior', 'btn_next': 'Siguiente ➡️',
    'donate_title': '💝 **Apoya este bot**\n\n¡Gracias por considerar apoyar!\n\nDos formas de donar:\n\n1️⃣ **Zap directo al bot:**\n   ¡Solo envía un pago al bot!\n\n2️⃣ **Al desarrollador:**\n   `your_wallet@example.com`\n\n¡Cada zap ayuda a mantener el bot! ⚡',
    'btn_zap_now': '⚡ Zap ahora',
    'donate_amount_title': '💝 **Monto de donación**\n\n¿Cuántos sats quieres donar?\n\nMontos sugeridos:\n• 1,000 sats (☕ Café)\n• 5,000 sats (🍕 Pizza)\n• 10,000 sats (❤️ Seguidor)\n• 21,000 sats (🚀 ¡Campeón!)\n➡️ Envía el monto (sats o $USD):',
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ Sesión expirada. Empieza de nuevo.',
    'insufficient': '❌ ¡Saldo insuficiente!',
    'loading': '⏳ Cargando...',
    'synced': '✅ Sincronizado.\n💰 Saldo: **{balance} sats**',
    'buy_title': '🟧 **Comprar Bitcoin**\n\n{url}',
    'fees_loading': '⛽ Cargando...',
    'buy_title': '🟧 **Comprar Bitcoin**\n\n{url}',
    'fees_loading': '⛽ Cargando...',
    'sync_loading': '🔄 Sincronizando...',
    # ---- receive v2 ----
    'receive_title': '📥 **Recibir**',
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
    'qr_decoded': '✅ ¡Código QR leído con éxito!',
    'amount_label': 'Monto',
    # ---- history ----
    'hist_loading': '📜 Cargando historial...',
    'hist_title': '📜 **Historial** — pág. {page}',
    'hist_empty_title': '📜 **Historial**',
    'hist_empty': 'Nada aquí todavía.\n¡Envía o recibe tus primeros sats!',
    'hist_received': 'Recibido',
    'hist_sent': 'Enviado',
    'hist_details': 'Detalles',
    'btn_prev': '⬅️ Anterior',
    'btn_next': 'Siguiente ➡️',
    'btn_scan_payment': '🔍 Ver pago',
    'hist_error': '❌ Error al cargar el historial:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'Cargando monedas...',
    'ff_load_fail': 'Error al cargar monedas.',
    'ff_recv_title': '🔄 **Recibir vía exchange (ff.io)**\n\nElige la moneda a depositar:\nEnvías crypto → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Enviar vía exchange (ff.io)**\n\nSaldo: {balance}\n\nElige la moneda a recibir:\nLightning BTC → ff.io → tu crypto',
    'ff_terms': '⚠️ _El servicio de exchange lo provee FixedFloat. Crear una orden implica aceptar las [reglas de FixedFloat](https://ff.io/terms-of-service)._'
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
    'btn_private_mode': '👻 Gizli mod', 'btn_emergency_exit': '🚨 Acil çıkış',
    'btn_advanced': '🔧 Gelişmiş',
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
        'Yardım mı lazım? @your_bot'
    ),
    'send_title': '📤 **Gönder**',
    'send_prompt': 'Fatura, LNURL, Lightning adresi, @kullanıcı veya bc1 adresi yapıştırın.\n`$5` gibi tutarlar da çalışır.',
    'send_confirm': '**{amount}** gönderimi onaylansın mı?',
    'send_done': '✅ Gönderildi!',
    'send_amount_prompt': 'Tutarı satoshi olarak gönderin (veya `$5`):\n\n💡 Bakiye: **{balance}**',
    'receive_title': '📥 **Al**\n\nBir yöntem seçin:',
    'btn_ln_invoice': '⚡ Lightning faturası', 'btn_onchain': '₿ Zincir üstü adres',
    'btn_exchange': '🔄 Takas (ff.io)',
    'history_title': '📜 **Geçmiş** — sayfa {page}',
    'history_empty': "Burada henüz bir şey yok. İlk satoshi'nizi gönderin veya alın!",
    'btn_prev': '⬅️ Önceki', 'btn_next': 'Sonraki ➡️',
    'donate_title': '💝 **Bu botu destekle**\n\nDesteklemeyi düşündüğünüz için teşekkürler!\n\nİki bağış yolu:\n\n1️⃣ **Doğrudan bota zap:**\n   Bota bir ödeme gönderin!\n\n2️⃣ **Geliştiriciye:**\n   `your_wallet@example.com`\n\nHer zap botun çalışmasına yardım eder! ⚡',
    'btn_zap_now': '⚡ Şimdi zapla',
    'donate_amount_title': '💝 **Bağış miktarı**\n\nKaç satoshi bağışlamak istersiniz?\n\nÖnerilen miktarlar:\n• 1,000 satoshi (☕ Kahve)\n• 5,000 satoshi (🍕 Pizza)\n• 10,000 satoshi (❤️ Destekçi)\n• 21,000 satoshi (🚀 Şampiyon!)\n➡️ Tutarı gönderin (satoshi veya dolar):',
    'err_generic': '❌ {error}',
    'err_session_lost': '❌ Oturum süresi doldu. Yeniden başlayın.',
    'insufficient': '❌ Yetersiz bakiye!',
    'loading': '⏳ Yükleniyor...',
    'synced': '✅ Senkronize edildi.\n💰 Bakiye: **{balance} satoshi**',
    'buy_title': '🟧 **Bitcoin satın al**\n\n{url}',
    'fees_loading': '⛽ Yükleniyor...',
    'buy_title': '🟧 **Bitcoin satın al**\n\n{url}',
    'fees_loading': '⛽ Yükleniyor...',
    'sync_loading': '🔄 Senkronize ediliyor...',
    # ---- receive v2 ----
    'receive_title': '📥 **Al**',
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
    'qr_decoded': '✅ QR kod başarıyla okundu!',
    'amount_label': 'Tutar',
    # ---- history ----
    'hist_loading': '📜 Geçmiş yükleniyor...',
    'hist_title': '📜 **Geçmiş** — sayfa {page}',
    'hist_empty_title': '📜 **Geçmiş**',
    'hist_empty': "Burada henüz bir şey yok.\nİlk satoshi'nizi gönderin veya alın!",
    'hist_received': 'Alındı',
    'hist_sent': 'Gönderildi',
    'hist_details': 'Detaylar',
    'btn_prev': '⬅️ Önceki',
    'btn_next': 'Sonraki ➡️',
    'btn_scan_payment': '🔍 Ödemeyi sorgula',
    'hist_error': '❌ Geçmiş yüklenirken hata:\n{error}',
    # ---- ff.io ----
    'ff_loading': 'Para birimleri yükleniyor...',
    'ff_load_fail': 'Para birimleri yüklenemedi.',
    'ff_recv_title': '🔄 **Takas ile al (ff.io)**\n\nYatırmak istediğiniz coini seçin:\nCoin gönderirsiniz → ff.io → Lightning BTC',
    'ff_send_title': '🔄 **Takas ile gönder (ff.io)**\n\nBakiye: {balance}\n\nAlmak istediğiniz coini seçin:\nLightning BTC → ff.io → coininiz',
    'ff_terms': '⚠️ _Takas servisi FixedFloat tarafından sağlanır. Sipariş oluşturmak [FixedFloat kurallarını](https://ff.io/terms-of-service) kabul etmek anlamına gelir._'
}

# Partial packs — common buttons only; rest falls back to English.
DE = {
    'btn_send': '📤 Senden', 'btn_receive': '📥 Empfangen', 'btn_history': '📜 Verlauf',
    'btn_settings': '⚙️ Einstellungen', 'btn_help': '❓ Hilfe', 'btn_donate': '💝 Spenden',
    'btn_back_menu': '« Menü', 'btn_back_settings': '« Einstellungen', 'btn_cancel': '« Abbrechen',
    'btn_back_tools': '« Werkzeuge', 'btn_tools': '🧰 Werkzeuge', 'btn_confirm': '✅ Bestätigen',
    'btn_language': '🌐 Sprache', 'balance_line': '💰 Guthaben: {amount}',
    'btn_buy_bitcoin': '🟧 Bitcoin kaufen', 'insufficient': '❌ Unzureichendes Guthaben!',
    'send_done': '✅ Gesendet!', 'settings_title': '⚙️ **Einstellungen**',
    'tools_title': '🧰 **Werkzeuge**\n\nWerkzeug wählen:', 'btn_calculator': '🧮 Rechner',
    'btn_fees': '⛽ Gebühren', 'btn_batch_pay': '📢 Sammelzahlung', 'btn_contacts': '📒 Kontakte',
    'btn_sync': '🔄 Synchronisieren', 'btn_sign_msg': '✍️ Nachricht signieren',
    'select_language': '🌐 **Sprache**\n\nAktuell: {current}', 'lang_set': '✅ Sprache: {lang}',
    'history_empty': 'Noch nichts hier. Sende oder empfange deine ersten Sats!',
    'loading': '⏳ Wird geladen...', 'err_session_lost': '❌ Sitzung abgelaufen. Nochmal beginnen.',
}
FR = {
    'btn_send': '📤 Envoyer', 'btn_receive': '📥 Recevoir', 'btn_history': '📜 Historique',
    'btn_settings': '⚙️ Réglages', 'btn_help': '❓ Aide', 'btn_donate': '💝 Faire un don',
    'btn_back_menu': '« Menu', 'btn_back_settings': '« Réglages', 'btn_cancel': '« Annuler',
    'btn_back_tools': '« Outils', 'btn_tools': '🧰 Outils', 'btn_confirm': '✅ Confirmer',
    'btn_language': '🌐 Langue', 'balance_line': '💰 Solde : {amount}',
    'btn_buy_bitcoin': '🟧 Acheter du Bitcoin', 'insufficient': '❌ Solde insuffisant !',
    'send_done': '✅ Envoyé !', 'settings_title': '⚙️ **Réglages**',
    'tools_title': '🧰 **Outils**\n\nChoisissez un outil :', 'btn_calculator': '🧮 Calculatrice',
    'btn_fees': '⛽ Frais', 'btn_batch_pay': '📢 Paiement groupé', 'btn_contacts': '📒 Contacts',
    'btn_sync': '🔄 Synchroniser', 'btn_sign_msg': '✍️ Signer un message',
    'select_language': '🌐 **Langue**\n\nActuel : {current}', 'lang_set': '✅ Langue : {lang}',
    'history_empty': "Rien ici pour l'instant. Envoyez ou recevez vos premiers sats !",
    'loading': '⏳ Chargement...', 'err_session_lost': '❌ Session expirée. Recommencez.',
}
PT = {
    'btn_send': '📤 Enviar', 'btn_receive': '📥 Receber', 'btn_history': '📜 Histórico',
    'btn_settings': '⚙️ Configurações', 'btn_help': '❓ Ajuda', 'btn_donate': '💝 Doar',
    'btn_back_menu': '« Menu', 'btn_back_settings': '« Configurações', 'btn_cancel': '« Cancelar',
    'btn_back_tools': '« Ferramentas', 'btn_tools': '🧰 Ferramentas', 'btn_confirm': '✅ Confirmar',
    'btn_language': '🌐 Idioma', 'balance_line': '💰 Saldo: {amount}',
    'btn_buy_bitcoin': '🟧 Comprar Bitcoin', 'insufficient': '❌ Saldo insuficiente!',
    'send_done': '✅ Enviado!', 'settings_title': '⚙️ **Configurações**',
    'tools_title': '🧰 **Ferramentas**\n\nEscolha uma ferramenta:', 'btn_calculator': '🧮 Calculadora',
    'btn_fees': '⛽ Taxas', 'btn_batch_pay': '📢 Pagamento em lote', 'btn_contacts': '📒 Contatos',
    'btn_sync': '🔄 Sincronizar', 'btn_sign_msg': '✍️ Assinar mensagem',
    'select_language': '🌐 **Idioma**\n\nAtual: {current}', 'lang_set': '✅ Idioma: {lang}',
    'history_empty': 'Nada aqui ainda. Envie ou receba seus primeiros sats!',
    'loading': '⏳ Carregando...', 'err_session_lost': '❌ Sessão expirada. Comece de novo.',
}
HI = {
    'btn_send': '📤 भेजें', 'btn_receive': '📥 प्राप्त करें', 'btn_history': '📜 इतिहास',
    'btn_settings': '⚙️ सेटिंग्स', 'btn_help': '❓ सहायता', 'btn_donate': '💝 दान करें',
    'btn_back_menu': '« मेनू', 'btn_back_settings': '« सेटिंग्स', 'btn_cancel': '« रद्द करें',
    'btn_back_tools': '« उपकरण', 'btn_tools': '🧰 उपकरण', 'btn_confirm': '✅ पुष्टि करें',
    'btn_language': '🌐 भाषा', 'balance_line': '💰 शेष: {amount}',
    'btn_buy_bitcoin': '🟧 बिटकॉइन खरीदें', 'insufficient': '❌ अपर्याप्त शेष!',
    'send_done': '✅ भेज दिया!', 'settings_title': '⚙️ **सेटिंग्स**',
    'tools_title': '🧰 **उपकरण**\n\nएक उपकरण चुनें:', 'btn_calculator': '🧮 कैलकुलेटर',
    'btn_fees': '⛽ शुल्क', 'btn_batch_pay': '📢 बैच भुगतान', 'btn_contacts': '📒 संपर्क',
    'btn_sync': '🔄 सिंक', 'btn_sign_msg': '✍️ संदेश साइन करें',
    'select_language': '🌐 **भाषा**\n\nवर्तमान: {current}', 'lang_set': '✅ भाषा: {lang}',
    'history_empty': 'यहाँ अभी कुछ नहीं है। अपने पहले सैट्स भेजें या प्राप्त करें!',
    'loading': '⏳ लोड हो रहा है...', 'err_session_lost': '❌ सत्र समाप्त। फिर से शुरू करें।',
}
ID = {
    'btn_send': '📤 Kirim', 'btn_receive': '📥 Terima', 'btn_history': '📜 Riwayat',
    'btn_settings': '⚙️ Pengaturan', 'btn_help': '❓ Bantuan', 'btn_donate': '💝 Donasi',
    'btn_back_menu': '« Menu', 'btn_back_settings': '« Pengaturan', 'btn_cancel': '« Batal',
    'btn_back_tools': '« Alat', 'btn_tools': '🧰 Alat', 'btn_confirm': '✅ Konfirmasi',
    'btn_language': '🌐 Bahasa', 'balance_line': '💰 Saldo: {amount}',
    'btn_buy_bitcoin': '🟧 Beli Bitcoin', 'insufficient': '❌ Saldo tidak cukup!',
    'send_done': '✅ Terkirim!', 'settings_title': '⚙️ **Pengaturan**',
    'tools_title': '🧰 **Alat**\n\nPilih alat:', 'btn_calculator': '🧮 Kalkulator',
    'btn_fees': '⛽ Biaya', 'btn_batch_pay': '📢 Bayar massal', 'btn_contacts': '📒 Kontak',
    'btn_sync': '🔄 Sinkron', 'btn_sign_msg': '✍️ Tanda tangani pesan',
    'select_language': '🌐 **Bahasa**\n\nSaat ini: {current}', 'lang_set': '✅ Bahasa: {lang}',
    'history_empty': 'Belum ada apa pun di sini. Kirim atau terima sats pertama Anda!',
    'loading': '⏳ Memuat...', 'err_session_lost': '❌ Sesi berakhir. Mulai lagi.',
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


def is_rtl(user_id):
    return get_user_lang(user_id) in RTL_LANGS


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
