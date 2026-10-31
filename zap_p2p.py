"""P2P exchange section for Zap_lnBot — owner + test users only (P2P_TEST_USERS).

Design (owner-approved v3):
- Escrow: dedicated wallet, seed at /root/mzap/.p2p_escrow_seed (chmod 600),
  storage ./.p2p_escrow. Seller locks sats+fee with a zero-fee internal spark
  send; release pays the buyer's in-bot wallet directly; fee (0.5%) sweeps to
  the owner (DONATE_USER_ID). All wallet plumbing reuses the bot's own
  get_wallet/prepare_payment/send_payment/create_invoice — no duplicate code.
- Arrival detection: the escrow wallet's OWN SDK event listener
  (PAYMENT_SUCCEEDED), same mechanism as the bot's user notifications, plus a
  one-shot scan after restart for events missed while the bot was down.
- UI built fresh for this section (no reuse of past P2P UIs): P2P home,
  market carousel, 3-step order wizard, ONE live trade-room message per party
  with a 3-node status bar and a single contextual action, anonymous in-room
  chat. Short numeric order ids (#12). Every money action is two-tap.
- Data: separate p2p.db (WAL). Bot DB untouched. Timeouts ride the bot's
  existing cleanup_pending_zaps sweeper via p2p.sweep().
- Agreement gate: first entry shows TOS; buyer responsibility for manual
  price review is explicit. Stored once per user.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

from breez_sdk_spark import EventListener
from html import escape as _h


async def offloop(coro):
    """Run a slow wallet/escrow SDK coroutine on a worker-thread loop so
    the main loop (buttons, chat) never freezes during sends."""
    loop = asyncio.new_event_loop()
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: loop.run_until_complete(coro))
    finally:
        loop.close()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'p2p.db')
ESCROW_SEED_PATH = '/root/mzap/.p2p_escrow_seed'
ESCROW_STORAGE_DIR = './.p2p_escrow'

FEE_BPS = 50                      # 0.5% — seller pays
LOCK_WAIT_SECS = 30 * 60          # taken, seller never locked -> back to market
FIAT_TIMEOUT_SECS = 2 * 3600      # locked, buyer silent -> warning
RELEASE_TIMEOUT_SECS = 24 * 3600  # fiat confirmed, seller silent -> auto dispute
FEE_MAX_TRIES = 5

# trade stages
ST_LOCKING = 'locking'
ST_LOCKED = 'locked'
ST_FIAT = 'fiat_sent'
ST_PAYOUT = 'payout'
ST_REFUND = 'refunding'
ST_DONE = 'done'
ST_REFUNDED = 'refunded'
ST_DISPUTED = 'disputed'
ST_CANCELED = 'canceled'

_db_lock = threading.RLock()
_db = None
_escrow = None
_escrow_lock = asyncio.Lock()
_last_panel = {}   # uid -> (chat_id, msg_id) last buttoned p2p panel
P2P_LIST_CHANNEL = -1004462652956   # «Zap P2P list» public order channel
_mem_wiz = {}       # in-process fallback
_db_wiz_ready = False


def _wiz_table():
    global _db_wiz_ready
    try:
        _exec("CREATE TABLE IF NOT EXISTS wiz (uid INTEGER PRIMARY KEY, data TEXT)")
        _db_wiz_ready = True
    except Exception:
        _db_wiz_ready = False
    return _db_wiz_ready


class _WizShelf(dict):
    """dict wrapper persisted to p2p.db so restarts don't kill in-progress orders."""

    def __getitem__(self, k):
        if k in _mem_wiz:
            return _mem_wiz[k]      # live object — mutations persist in-process
        if _wiz_table():
            row = _rows('SELECT data FROM wiz WHERE uid=?', (k,))
            if row:
                v = json.loads(row[0]['data'])
                _mem_wiz[k] = v     # adopt as live object for this process
                return v
        return _mem_wiz.get(k)

    def __contains__(self, k):
        if k in _mem_wiz:
            return True
        if isinstance(k, int) and _wiz_table():
            return bool(_rows('SELECT uid FROM wiz WHERE uid=?', (k,)))
        return False

    def __setitem__(self, k, v):
        _mem_wiz[k] = v
        # string keys (dsp:/adm: short-lived states) stay in-process only —
        # the wiz table's uid column is INTEGER and rejects them
        if _wiz_table() and isinstance(k, int):
            _exec('INSERT INTO wiz (uid, data) VALUES (?, ?) '
                  'ON CONFLICT(uid) DO UPDATE SET data=excluded.data',
                  (k, json.dumps(v)))

    def __delitem__(self, k):
        _mem_wiz.pop(k, None)
        if _wiz_table():
            _exec('DELETE FROM wiz WHERE uid=?', (k,))

    def get(self, k, default=None):
        try:
            return self.__getitem__(k)
        except KeyError:
            return default

    def pop(self, k, default=None):
        v = self.get(k, default)
        if v is not None or k in self:
            self.__delitem__(k)
        return v

    def __contains__(self, k):
        return self.get(k) is not None

    def keys(self):
        base = list(_mem_wiz.keys())
        if _wiz_table():
            base += [r['uid'] for r in _rows('SELECT uid FROM wiz')]
        return list(dict.fromkeys(base))


_wiz = _WizShelf()


# ==================== storage ====================

def _connect():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False,
                              isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.execute('PRAGMA journal_mode=WAL')
        _db.execute('PRAGMA busy_timeout=5000')
    return _db


def init_db():
    with _db_lock:
        db = _connect()
        db.executescript('''
CREATE TABLE IF NOT EXISTS agreed (
    uid INTEGER PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    side TEXT, maker INTEGER, sats INTEGER,
    price TEXT, method TEXT,
    status TEXT DEFAULT 'open',
    created REAL, taken_at REAL, ttl_secs REAL, fiat_code TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER, buyer INTEGER, seller INTEGER,
    sats INTEGER, fee INTEGER, price TEXT, method TEXT,
    stage TEXT, created REAL, updated REAL,
    lock_started REAL, fiat_at REAL, warned INTEGER DEFAULT 0,
    cancel_seller INTEGER DEFAULT 0, cancel_buyer INTEGER DEFAULT 0,
    panels TEXT DEFAULT '{}',
    payout_id TEXT, payout_tries INTEGER DEFAULT 0,
    refund_id TEXT, refund_tries INTEGER DEFAULT 0,
    fee_paid INTEGER DEFAULT 0, fee_tries INTEGER DEFAULT 0,
    dispute_reason TEXT,
    chat_open INTEGER DEFAULT 0, chat_off_seller INTEGER DEFAULT 0,
    chat_off_buyer INTEGER DEFAULT 0, premium REAL);
''')
        db.executescript('''
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER, rater INTEGER, ratee INTEGER, stars INTEGER, at REAL);
CREATE TABLE IF NOT EXISTS banned (
    uid INTEGER PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS p2p_admins (
    uid INTEGER PRIMARY KEY, added_by INTEGER, at REAL);
CREATE TABLE IF NOT EXISTS chat_msgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tid INTEGER, uid INTEGER, text TEXT, at REAL);
''')
        cols = {r[1] for r in db.execute('PRAGMA table_info(orders)').fetchall()}
        if 'ttl_secs' not in cols:
            db.execute('ALTER TABLE orders ADD COLUMN ttl_secs REAL')
        ocols2 = {r[1] for r in db.execute('PRAGMA table_info(orders)').fetchall()}
        for col, typ in (('price_mode', 'TEXT'), ('fiat_amount', 'REAL'),
                         ('premium', 'REAL'), ('channel_msg', 'INTEGER'),
                         ('fiat_code', 'TEXT')):
            if col not in ocols2:
                db.execute(f'ALTER TABLE orders ADD COLUMN {col} {typ}')
        tcols = {r[1] for r in db.execute('PRAGMA table_info(trades)').fetchall()}
        for col in ('chat_open', 'chat_off_seller', 'chat_off_buyer'):
            if col not in tcols:
                db.execute(f'ALTER TABLE trades ADD COLUMN {col} INTEGER DEFAULT 0')
        for col, typ in (('fiat_final', 'REAL'), ('rate_used', 'REAL'),
                         ('premium', 'REAL')):
            if col not in tcols:
                db.execute(f'ALTER TABLE trades ADD COLUMN {col} {typ}')
        db.commit()


def _rows(sql, args=()):
    with _db_lock:
        cur = _connect().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def _one(sql, args=()):
    rows = _rows(sql, args)
    return rows[0] if rows else None


def _exec(sql, args=()):
    with _db_lock:
        db = _connect()
        cur = db.execute(sql, args)
        db.commit()
        return cur


def has_agreed(uid):
    return _one('SELECT 1 FROM agreed WHERE uid=?', (uid,)) is not None


def set_agreed(uid):
    _exec('INSERT OR REPLACE INTO agreed (uid, at) VALUES (?,?)', (uid, time.time()))


# ==================== users / gating ====================

def _bot():
    """The RUNNING bot module.

    The service executes zap_wallet_bot_v31.py as __main__; a plain
    `import zap_wallet_bot_v31` here creates a SECOND module instance with
    cipher_suite=None (seeds fail to decrypt → "invalid word count: 1") and an
    empty active_user_connections. Always resolve the live __main__ first.
    """
    import sys
    m = sys.modules.get('__main__')
    if m is not None and hasattr(m, 'get_wallet'):
        return m
    import zap_wallet_bot_v31
    return zap_wallet_bot_v31


def p2p_public():
    """Public when P2P_TEST_USERS is unset, empty, or 'all'."""
    raw = (os.getenv('P2P_TEST_USERS') or '').strip().lower()
    return raw in ('', 'all')


def test_users():
    raw = os.getenv('P2P_TEST_USERS', '')
    out = set()
    for part in raw.split(','):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def enabled_for(uid):
    if is_banned(uid):
        return False
    if p2p_public():
        return True
    return uid in test_users()


def admin_id():
    try:
        return int(os.getenv('DONATE_USER_ID', '0'))
    except (TypeError, ValueError):
        return 0


def is_p2p_admin(uid):
    """Owner (DONATE_USER_ID) or a user added from the admin panel."""
    if uid and uid == admin_id():
        return True
    try:
        return bool(_one('SELECT uid FROM p2p_admins WHERE uid=?', (uid,)))
    except Exception:
        return False


def p2p_admin_ids():
    ids = [r['uid'] for r in _rows('SELECT uid FROM p2p_admins ORDER BY uid')]
    owner = admin_id()
    if owner and owner not in ids:
        ids.insert(0, owner)
    return ids


def is_banned(uid):
    try:
        return bool(_one('SELECT uid FROM banned WHERE uid=?', (uid,)))
    except Exception:
        return False


def ban_user(uid, reason=''):
    _exec('INSERT OR REPLACE INTO banned (uid, reason, at) VALUES (?,?,?)',
          (uid, (reason or '')[:200], time.time()))


def unban_user(uid):
    _exec('DELETE FROM banned WHERE uid=?', (uid,))


# ==================== texts ====================

TXT = {
 'fa': {


  'home_title': '🤝 معاوضه ساتوشی',
  'home_body': 'اینجا می‌تونی ساتوشی‌هات رو مستقیم با آدم‌ها معاوضه کنی — بدون واسطه‌ی صرافی.\n\n🔑 چطوریه؟\n• فروشنده سفارش میده و ساتوشی‌هاش رو تو قفل ربات می‌ذاره\n• خریدار پول (تومان، دلار، هر روشی که توافق کردید) رو مستقیم به فروشنده می‌ده\n• فروشنده تأیید می‌کنه و ساتوشی‌ها آزاد می‌شن و میرن ولت خریدار\n\n🛡 اگه اختلاف پیش بیاد، ادمین وسط میاد و تصمیم می‌گیره.',
  'home_fee': '💸 کارمزد: فقط ۰.۵٪ — از فروشنده کسر می‌شه',
  'tos_title': '🤝 توافق‌نامه و شرایط استفاده',
  'tos_body': 'قبل از استفاده از این بخش، این شرایط رو بخون و بپذیر:\n\n1️⃣ معاوضه بین تو و طرف مقابلته. ربات فقط نگه‌دارنده‌ی ساتوشی‌هاست و ضامن قیمت نیست.\n\n2️⃣ خریدار موظفه قبل از واریز، قیمت و بهای این معامله رو خودش دستی بررسی و مقایسه کنه. ربات هیچ نقشی در تعیین قیمت نداره و قیمت ممکنه با بازار فرق داشته باشه.\n\n3️⃣ پرداخت پولی (فیات یا هر روش دیگه) بین خودتونه؛ ربات اون رو تأیید یا تضمین نمی‌کنه.\n\n4️⃣ ساتوشی‌ها تا وقتی فروشنده «دریافت کردم» نزده، تو قفل ربات می‌مونه.\n\n5️⃣ تو اختلاف، تصمیم نهایی با ادمینه.\n\n6️⃣ برای هر معاوضه‌ی موفق ۰.۵٪ کارمزد از فروشنده کسر می‌شه.',
  'market_hint': 'بین سفارش‌ها بگرد 👇 — با ⬅️ ➡️ جلو و عقب برو. سفارش طرف مقابل رو که برداری، معامله شروع می‌شه.',
  'guide_text': '❓ راهنمای معاوضه P2P\n\n📌 می‌خوای بفروشی؟\n1️⃣ «سفارش جدید» رو بزن و مقدار، قیمت و مهلت رو مشخص کن\n2️⃣ خریدار سفارشت رو برداره، ساتوشی + کارمزد ۰.۵٪ رو قفل کن\n3️⃣ خریدار پول رو بهت می‌رسونه و «واریز کردم» می‌زنه\n4️⃣ پول رو که دیدی، «دریافت کردم» بزن — ساتوشی‌ها میرن ولت خریدار\n\n📌 می‌خوای بخری؟\n1️⃣ تو بازار یه سفارش فروش بردار\n2️⃣ فروشنده قفل کرد؟ شماره حسابش رو ازش بگیر و واریز کن\n3️⃣ «واریز کردم» بزن — بعد از تأیید فروشنده، ساتوشی‌ها میاد ولتت\n\n💡 نکته‌ها:\n• تو اتاق معامله می‌تونید بی‌اسم به هم پیام بدید\n• اگه معامله رو نخواستی ادامه بدی، «لغو» دوطرفه می‌شه\n• اختلافی پیش اومد؟ ⚖️ اختلاف رو بزن تا ادمین رسیدگی کنه\n• 💱 ارز معامله (تومان، دلار و…) از ⚙️ تنظیمات ← «ارز مبدا» عوض میشه — نرخ لحظه‌ای هم از همون ارز خونده میشه',
  'wiz_side': 'بیا سفارشت رو بسازیم 👇\n\nمی‌خوای ساتوشی بفروشی یا بخری؟',
  'wiz_amount': '💰 چقدر ساتوشی می‌خوای معاوضه کنی؟\n\nفقط عدد بفرست. مثال: 50000',
  'wiz_price': '🏷 قیمتت رو چطور می‌فروشی/می‌خری؟\n\nهر واحدی راحته — خودت بنویس.\nمثال:\n• 6500000 تومان\n• 5.3$ برای هر 10k sats',
  'wiz_method': '💳 آخر، توضیحات سفارش — روش پرداخت یا هر نکته‌ای که طرف مقابل باید بدونه (اختیاری).\n\nمثال: «فقط کارت به کارت بانک ملی» — یا خالی بفرست',
  'wiz_bad_amount': '⚠️ این عدد رو نشناستم. لطفاً فقط یه عدد ساتوشی بفرست (مثلاً 50000):',
  'wiz_review': '✍️ سفارشت آماده‌ست. یه نگاه بنداز:',
  'publish': '✅ ثبت و انتشار در بازار',
  'published': '✅ سفارشت تو بازار منتشر شد.',
  'order_none': '🎯 هنوز هیچ سفارشی تو بازار نیست.\nاولین نفر باش — «سفارش جدید» بزن.',
  'take_confirm': '🤝 این سفارش رو برمی‌داری؟\n\nبا برداشتن، معامله بین شما شروع می‌شه:\n• فروشنده باید ساتوشی‌ها رو قفل کنه\n• تو هم تا آخر معامله همراهی کنی\n\nادامه می‌دی؟',
  'taken_ok': '🤝 معاوضه شروع شد!\n\nاتاق معامله برات باز شد — از همین‌جا جلو ببر.',
  'busy_trade': '🤝 یه معاوضه‌ی بازی داری!\n\nتا این معاوضه تموم نشه (یا بسته بشه) نمی‌تونی سفارش جدیدی برداری. از «معاوضه‌های در جریان» ادامه‌ش بده.',
  'order_gone': '😌 دیر رسیدی — این سفارش همین الان برداشته شد.',
  'my_empty': 'هنوز هیچ سفارشی نساختی.\nبا «➕ سفارش جدید» اولین سفارشت رو بزن.',
  'mine_open': '🟢 تو بازار',
  'mine_taken': '🤝 درگیر معامله',
  'mine_done': '✅ انجام شد',
  'mine_canceled': '⛔️ لغو شد',
  'mine_expired': '⏳ مهلتش تموم شد',
  'my_canceled_ok': '⛔️ سفارشت لغو شد و از بازار برداشته شد.',
  'you_are_seller': '💸 تو فروشنده‌ای',
  'you_are_buyer': '🛒 تو خریداری',
  'fee_l': '💸 کارمزد ۰.۵٪ (از فروشنده)',
  'method_l': '💳 توضیحات',
  'st_locking': '⏳ منتظریم فروشنده ساتوشی‌ها رو قفل کنه…\n\n🔐 ساتوشی‌ها تا آخر معاوضه تو نگه‌داری ربات قفل می‌مونه.\n⏰ اگه فروشنده تا ۳۰ دقیقه قفل نکنه، سفارش خودکار برمی‌گرده بازار.',
  'st_lock_me': 'برای شروع معاوضه، اول ساتوشی‌ها + کارمزد رو قفل کن.\n\n🔒 تا وقتی قفل نشه خریدار کاری نمی‌کنه و معاوضه جلو نمی‌ره.',
  'lock_sure': '🔒 قراره {amt} sats از ولتت به قفل ربات بره.\n\nاین پول تا آخر معاوضه قفل می‌مونه:\n• معاوضه خوب پیش بره → میره ولت خریدار\n• لغو دوطرفه یا اختلاف → برمی‌گرده ولت تو\n\nمطمئنی؟',
  'st_locking_progress': '⏳ دارم ساتوشی‌ها رو قفل می‌کنم… چند لحظه صبر کن.',
  'st_locked': '🔒 ساتوشی‌ها قفل شد!',
  'locked_buyer': '💳 حالا نوبت توئه:\n1️⃣ از فروشنده شماره کارت/حساب رو بگیر (همین‌جا تو چت)\n2️⃣ مبلغ رو با همون روشی که توافق کردید واریز کن\n3️⃣ برگرد و «✅ واریز کردم» رو بزن',
  'locked_seller': '💳 الان شماره کارت/حسابت رو بفرست تا خریدار پول رو واریز کنه (همین‌جا تو چت معاوضه).\n\n💤 بعدش منتظر بمون — وقتی «واریز کردم» زد بهت خبر میدم.',
  'fiat_sure': '✅ تأیید می‌کنی پول رو واریز کردی؟\n\nاین یه اعلام رسمیه:\n• فروشنده با دیدنش آزادسازی می‌کنه\n• اگه دروغ باشه، پرونده میره دست ادمین\n\nواریز کردی؟',
  'fiat_yes': '✅ آره، واریز کردم',
  'st_fiat': '💸 خریدار واریزش رو تأیید کرد.',
  'st_fiat_buyer': '⏳ منتظر بمون تا فروشنده ساتوشی‌ها رو آزاد کنه — به‌محض آزادسازی ساتوشی‌ها میره ولتت.\n⏰ اگه فروشنده تا ۲۴ ساعت آزاد نکنه، پرونده خودکار به ادمین می‌ره.',
  'fiat_seller': '💰 حالا چک کن ببین پول واقعاً رسید.\n\nاگه رسید → «دریافت کردم» بزن تا ساتوشی‌ها بره ولت خریدار.\nاگه نرسیده → ⚖️ اختلاف بزن.',
  'rel_btn': '✅ پول رسید — آزاد کن',
  'rel_sure': '✅ تأیید می‌کنی پول رو گرفتی؟\n\n⚠️ این کار برگشتی نداره:\nساتوشی‌ها همون لحظه آزاد می‌شن و میرن ولت خریدار.\n\nمطمئنی؟',
  'rel_yes': '✅ آره، آزادش کن',
  'st_payout': '⏳ دارم ساتوشی‌ها رو آزاد می‌کنم و می‌فرستم ولت خریدار…',
  'st_done': '🎉 معاوضه تموم شد!\n\n💰 ساتوشی‌ها رفتن ولت خریدار\n\nممنون که مستقیم معامله کردی 🙏',
  'st_refunded': '↩️ معاوضه لغو شد — ساتوشی‌ها برگشت ولت فروشنده. جان سالم به در بردی 🙏',
  'cx_btn': '🔄 لغو معاوضه',
  'cx_sure': '🔄 می‌خوای معاوضه رو لغو کنی؟\n\nلغو فقط وقتی انجام می‌شه که طرف مقابل هم تأیید کنه (پس پول کسی گیر نمی‌کنه):\n• اونم تأیید کنه → ساتوشی‌ها برمی‌گرده ولت فروشنده\n• تأیید نکنه → معاوضه سر جاش می‌مونه\n\nدرخواست بفرستم؟',
  'cx_yes': '🔄 آره، درخواست لغو',
  'cx_req': '🔄 طرف مقابل درخواست لغو معاوضه داده.\n\nاگه تو هم موافقی، «🔄 لغو معاوضه» رو بزن تا ساتوشی‌ها برگشت ولت فروشنده بره.\nاگه مخالفی، کاری نکن — یا ⚖️ اختلاف بزن.',
  'dispute_sure': '⚖️ می‌خوای اختلاف باز کنی؟\n\nیعنی:\n• پرونده‌ات میره دست ادمین\n• تا تصمیم ادمین، معاوضه می‌ایسته\n• ادمین می‌تونه ساتوشی‌ها رو به تو برسونه یا برگشت بزنه\n\nبازش کنم؟',
  'dispute_yes': '⚖️ آره، اختلاف باز کن',
  'dispute_open': '⚖️ پرونده‌ی اختلافت ثبت شد.\n\nادمین بررسی می‌کنه و تصمیم نهایی رو می‌گیره. تا اون موقع معاوضه ایستاده.',
  'dispute_not_locked': '⚖️ هنوز ساتوشی‌ها قفل نشدن — تا وقتی فروشنده قفل نکرده چیزی برای اختلاف وجود نداره. اگه مشکلی داری، معاوضه رو لغو کن یا با طرف مقابل از طریق چت صحبت کن.',
  'dispute_note': '⚖️ بگو دقیقاً مشکل چیه — توضیحت رو بنویس تا ضمیمه‌ی پرونده بشه:\n\n(اگه نمی‌خوای توضیح بدی، فقط یه چیزی بفرست تا پرونده ثبت شه)',
  'lock_fail_bal': '⚠️ موجودیت کافی نیست!\n\nبرای این معاوضه باید {need} sats داشته باشی (مقدار سفارش + کارمزد ۰.۵٪).\nاول ولتت رو شارژ کن و دوباره امتحان کن.',
  'lock_fail': '⚠️ قفل کردن انجام نشد: {err}\n\nیه بار دیگه امتحان کن. اگه نشد به ادمین خبر بده.',
  'no_action': '⏳ الان نوبت تو نیست — منتظر بمون تا طرف مقابل کارش رو انجام بده.',
  'active_title': '🔄 معاوضه‌های در جریان',
  'active_none': 'هنوز معاوضه‌ای در جریان نداری.',
  'ping_fiat': '💸 خریدار «واریز کردم» رو زد — پول رو چک کن و «دریافت کردم» رو بزن.',
  'ping_release': '🎉 فروشنده آزاد کرد! ساتوشی‌ها داره میره ولتت.',
  'cx_solo': '⛔️ معاوضه لغو شد (قبل از قفل — هیچ پولی قفل نبود). سفارش هم از بازار برداشته شد.',
  'busy_locking': '⏳ دارم قفل می‌کنم… یه چند ثانیه صبر کن.',
  'busy_fiating': '⏳ ثبت «واریز کردم»…',
  'room_close': '🗑 بستن اتاق معامله',
  'room_closed': '🗑 اتاق معاوضه بسته شد. ساتوشی‌ها تو ولتته — برای معامله بعدی «🤝 P2P» رو بزن.',
  'rate_ask': '⭐ این معاوضه چطور بود؟ به طرف مقابل امتیاز بده:',
  'rate_done': '⭐ امتیازت ثبت شد — ممنون!',
  'price_mode': '🏷 قیمت رو چطور می‌خوای تعیین کنی؟',
  'price_fixed': '📌 قیمت ثابت',
  'price_float': '📈 قیمت لحظه‌ای (نرخ ربات)',
  'wiz_fiat_amount': '💰 چند تومان می‌خوای؟\n\nفقط عدد بفرست. مثال: 5000000',
  'wiz_bad_fiat': '⚠️ عدد معتبر نیست. فقط عدد تومانی بفرست. مثال: 5000000',
  'wiz_premium': '📈 چند درصد پریمیوم/تخفیف؟\n\n'
                 'یه دکمه بزن، یا عدد با علامت بفرست: ‎+2%‎ یعنی ۲٪ گروتر از نرخ، ‎-1%‎ یعنی ۱٪ ارزون‌تر.\n'
                 '0 یعنی همون نرخ.',
  'wiz_bad_premium': '⚠️ عدد معتبر نیست. مثل ‎+2%‎ یا -1 یا 0 بفرست.',
  'float_preview': '📈 نرخ لحظه‌ای از نرخ خود ربات خونده میشه.',
  'float_no_rate': '⚠️ الان نرخ در دسترس نیست — چند دقیقه بعد دوباره امتحان کن یا قیمت ثابت بذار.',
  'unit_ask': '📏 می‌خوای چطور مقدار معامله رو مشخص کنی؟',
  'unit_sats': '🪙 ساتوشی',
  'unit_toman': '💵 {fiat}',
  'wiz_sats_amount': '🪙 چند ساتوشی؟\n\nفقط عدد بفرست. مثال: 50000',
  'fixed_resolved': '✅ ثبت شد: {sats} sats در برابر {fiat} تومان ({prem}%)',
  'wiz_sats_price': '🪙 چند ساتوشی می‌خوای در برابر این مبلغ بفروشی؟\n\nفقط عدد بفرست. مثال: 50000',
  'wiz_bad_sats': '⚠️ عدد معتبر نیست. فقط عدد ساتوشی بفرست. مثال: 50000',
  'ch_join': '📢 کانال سفارش‌ها',
  'fiat_hint': '💱 ارز معامله از ⚙️ تنظیمات ← «ارز مبدا» قابل تغییره',
  'own_order_note': '📌 این سفارش مال خودته — از «🧾 سفارش‌های من» مدیریتش کن.',
  'fee_short': 'کارمزد',
  'float_tag': 'نرخ لحظه‌ای',
  'chat_btn': '💬 فعال‌سازی چت',
  'chat_off_btn': '💬 غیرفعال‌سازی چت',
  'chat_turned_on': '💬 چت فعال شد — پیام‌هات بی‌اسم میره برای طرف مقابل. هر وقت خواستی با «💬 غیرفعال‌سازی چت» ببندش.',
  'chat_turned_off': '🔇 چت قطع شد. اگه بعداً خواستی، دوباره «💬 چت» رو بزن.',
  'chat_off_notice': '🔇 چت توسط طرف مقابل غیرفعال شد. هرکدومتون می‌تونید دوباره فعالش کنید.',
  'chat_note': '💬 پیامت (بی‌اسم) میره برای طرف مقابل:',
  'partner_gone': '⏰ معاوضه‌ات لغو شد — فروشنده سر وقتش ساتوشی‌ها رو قفل نکرد.',
  'order_back_sell': '♻️ معاوضه به جایی نرسید و سفارشت دوباره برگشت بازار. منتظر خریدار جدید بمون.',
  'order_back_buy': '♻️ معاوضه به جایی نرسید و سفارشت دوباره برگشت بازار. منتظر فروشنده جدید بمون.',
  'lock_timeout_self': '⏰ معاوضه لغو شد — تا مهلت ۳۰ دقیقه‌ای ساتوشی‌ها قفل نشد.',
  'fiat_warn': '⏰ هشدار: ۲ ساعت از قفل شدن ساتوشی‌ها می‌گذره و خریدار هنوز «واریز کردم» نزده.\n\n• خریدار هنوز وقت داره واریز کنه\n• اگه هر دوتون می‌خوایید کنسل کنید، «🔄 لغو معاوضه» رو بزنید\n• اگه مشکلی پیش اومده، ⚖️ اختلاف باز کنین',
  'auto_dispute': '⚖️ ۲۴ ساعت گذشت و فروشنده هنوز آزادسازی نکرده.\n\nپرونده به‌صورت خودکار به ادمین رفت — به‌زودی بررسی و تصمیم می‌گیره.',
  'lock_btn': '🔒 قفل می‌کنم',
  'lock_yes': '🔒 بله، قفل کن',
  'fiat_btn': '✅ واریز کردم',
  'dispute_btn': '⚖️ اختلاف',
  'cancel': '❌ لغو',
  'skip': 'رد کردن',
  'market': 'بازار',
  'new_order': '➕ سفارش جدید',
  'my_orders': '🧾 سفارش‌های من',
  'guide': '❓ راهنما',
  'back_p2p': '« P2P',
  'back_menu': '« منو',
  'take': '🤝 بردار',
  'take_yes': '✅ آره، برمی‌دارم',
  'my_cancel': '❌ لغو',
  'side_sell': '💸 فروش',
  'side_buy': '🛒 خرید',
  'seller': 'فروشنده',
  'buyer': 'خریدار',
  'tos_ok': '✅ می‌پذیرم',
  'chat_off': '🔇 چت این معاوضه بسته شد.',
  'timeout_generic': '⏰ تایم‌اوت',
  'of': 'از',
  'room_title': '🤝 معاوضه #{id}',
  'price_l': '🏷 قیمت',
  'amount_l': '💰 مقدار',
  'st_canceled': '⛔️ این معاوضه لغو شد.',
 },
 'en': {
  'home_title': '🤝 P2P Exchange',
  'home_body': 'Trade your sats directly with people.\nThe bot only holds the funds; both sides set the price.',
  'home_fee': '💸 Fee: 0.5% from the seller',
  'tos_title': '🤝 P2P Exchange Agreement',
  'tos_body': 'By entering this section you accept:\n\n• The trade is between you and your counterparty; the bot only holds the sats\n• The BUYER must manually review the price and value of this deal — the bot plays NO role in pricing\n• The seller sets the price and it may differ from market\n• The bot is not responsible for fiat payment; proving it is on the two of you\n• Sats stay locked in the bot escrow until the seller taps "received"\n• In a dispute, the admin decides\n• A 0.5% fee is taken from the seller',
  'market_hint': 'Browse the orders 👇 — use ⬅️ ➡️. Take a counter-order and the trade starts.',
  'guide_text': '❓ Guide\n\n1️⃣ Seller posts an order and locks sats+fee\n2️⃣ Buyer sends the fiat transfer\n3️⃣ Seller confirms and sats go to the buyer wallet\n\n🔐 Funds stay in the bot lock until the end\n💬 Chat anonymously inside the trade room\n⚖️ Disputes are decided by the admin\n💱 Change your trade currency (toman, USD, …) in ⚙️ Settings — live rates follow it',
  'wiz_side': '💸 Sell or 🛒 buy?',
  'wiz_amount': '💰 How many sats? (number only)',
  'wiz_price': '🏷 Write your price — any unit you like.\ne.g. 6500000 toman  or  5.3$ per 10k sats',
  'wiz_method': '💳 Write notes — payment method or anything else (optional)',
  'wiz_bad_amount': '⚠️ Send a valid sats number:',
  'wiz_review': '✍️ Your order:',
  'publish': '✅ Post order',
  'published': '✅ Order posted to the market.',
  'order_none': '🎯 No orders on the market right now.',
  'take_confirm': '🤝 Take this order?\n\nTaking it starts the trade and you must carry it through to the lock.',
  'taken_ok': '🤝 Trade started!',
  'busy_trade': '🤝 You already have a trade in progress!\n\nFinish (or close) it before taking another order. Continue from "Trades in progress".',
  'order_gone': '⚠️ This order was just taken by someone else.',
  'my_empty': 'No orders yet.',
  'mine_open': 'open',
  'mine_taken': 'in trade',
  'mine_done': 'done',
  'mine_canceled': 'canceled',
  'mine_expired': 'expired',
  'my_canceled_ok': '⛔️ Order canceled.',
  'you_are_seller': "💸 You're the seller",
  'you_are_buyer': "🛒 You're the buyer",
  'fee_l': '💸 Fee (from seller)',
  'method_l': '💳 Method',
  'st_locking': '⏳ Waiting for the seller to lock the sats…',
  'st_lock_me': 'To start, lock sats + fee:',
  'lock_sure': '🔒 {amt} sats will go from your wallet to the bot lock. Sure?',
  'st_locking_progress': '⏳ Locking…',
  'st_locked': '🔒 Sats are locked!',
  'locked_buyer': 'Now send the fiat transfer, then tap "✅ Paid".',
  'locked_seller': 'The buyer is paying now. Wait.',
  'fiat_sure': '✅ Confirm you paid?\nThis is an official claim — the seller will release after it.',
  'fiat_yes': '✅ Yes, I paid',
  'st_fiat': '💸 The buyer confirmed the payment.',
  'st_fiat_buyer': "⏳ Wait for the seller to release — the sats go to your wallet as soon as he does.\n⏰ If he doesn't release within 24h, the case goes to the admin automatically.",
  'fiat_seller': 'Check the money arrived, then release.',
  'rel_btn': '✅ Received — release',
  'rel_sure': '✅ Confirm you received it?\nThe sats release to the buyer wallet. This cannot be undone!',
  'rel_yes': '✅ Yes, release',
  'st_payout': '⏳ Releasing sats to the buyer…',
  'st_done': '🎉 Trade complete!\nThe sats landed in the buyer wallet.',
  'st_refunded': '↩️ Trade canceled — sats returned to the seller.',
  'cx_btn': '🔄 Cancel',
  'cx_sure': '🔄 Send a cancel request?\nIf the counterparty confirms too, the funds return.',
  'cx_yes': '🔄 Yes, request cancel',
  'cx_req': '🔄 The counterparty requested a cancel.\nIf you agree, tap "🔄 Cancel" too.',
  'dispute_sure': '⚖️ Open a dispute?\nThe case goes to the admin and the trade pauses.',
  'dispute_yes': '⚖️ Yes, dispute',
  'dispute_open': '⚖️ Dispute filed. The admin will review and decide.',
  'dispute_not_locked': "⚖️ The sats aren't locked yet — nothing is in escrow to dispute. Cancel the trade or talk it out in chat.",
  'dispute_note': '⚖️ Write your explanation here to attach to the case (or just confirm):',
  'lock_fail_bal': '⚠️ Balance is below {need} sats (amount + fee). Top up first.',
  'lock_fail': '⚠️ Lock failed: {err}',
  'no_action': 'Not your turn now.',
  'active_title': '🔄 Trades in progress',
  'active_none': 'No trades in progress.',
  'ping_fiat': '💸 The buyer tapped "I paid" — check and tap "Released".',
  'ping_release': '🎉 The seller released! Sats are on the way to your wallet.',
  'cx_solo': '⛔️ Trade canceled (pre-lock — no sats were held). The order was removed from the market.',
  'busy_locking': '⏳ Locking… hang on a few seconds.',
  'busy_fiating': '⏳ Confirming payment…',
  'room_close': '🗑 Close trade room',
  'room_closed': '🗑 Trade room closed. Your sats are in your wallet — tap "🤝 P2P" for the next trade.',
  'rate_ask': '⭐ How was this trade? Rate the counterparty:',
  'rate_done': '⭐ Rating saved — thanks!',
  'price_mode': '🏷 How do you want to set the price?',
  'price_fixed': '📌 Fixed price',
  'price_float': "📈 Live rate (bot's rate)",
  'wiz_fiat_amount': '💰 How many Toman?\n\nSend just the number. Example: 5000000',
  'wiz_bad_fiat': '⚠️ Not a valid number. Send the Toman amount only. Example: 5000000',
  'wiz_premium': '📈 Premium/discount percent?\n\nTap a button, or send a signed number: +2 means 2% above the rate, -1 means 1% below. 0 = at rate.',
  'wiz_bad_premium': '⚠️ Not valid. Send like +2 or -1 or 0.',
  'float_preview': "📈 Live rate is read from the bot's own rate engine.",
  'float_no_rate': '⚠️ Rate unavailable right now — try again later or use a fixed price.',
  'unit_ask': '📏 How do you want to set the amount?',
  'unit_sats': '🪙 Sats',
  'unit_toman': '💵 {fiat}',
  'wiz_sats_amount': '🪙 How many sats?\n\nSend just the number. Example: 50000',
  'fixed_resolved': '✅ Set: {sats} sats for {fiat} ({prem}%)',
  'wiz_sats_price': '🪙 How many sats do you want for this amount?\n\nSend just the number. Example: 50000',
  'wiz_bad_sats': '⚠️ Not a valid number. Send the sats amount only. Example: 50000',
  'ch_join': '📢 Orders channel',
  'fiat_hint': '💱 Change the trade currency in ⚙️ Settings',
  'own_order_note': '📌 This is your own order — manage it from “My orders”.',
  'fee_short': 'fee',
  'float_tag': 'Live rate',
  'chat_btn': '💬 Turn chat ON',
  'chat_off_btn': '💬 Turn chat OFF',
  'chat_turned_on': '💬 Chat is ON — your messages go to the counterparty anonymously. Turn it OFF anytime.',
  'chat_turned_off': '🔇 Chat ended. Tap "💬 Chat" again anytime.',
  'chat_off_notice': '🔇 Chat was turned OFF by the counterparty. Either of you can turn it back ON.',
  'chat_note': '💬 Send a message (anonymous, goes to your counterparty):',
  'partner_gone': '⚠️ Trade canceled — the seller never locked the sats in time.',
  'order_back_sell': '♻️ The trade fell through and your order is back on the market. Wait for a new buyer.',
  'order_back_buy': '♻️ The trade fell through and your order is back on the market. Wait for a new seller.',
  'lock_timeout_self': '⏰ Trade canceled — the sats were never locked within the time limit.',
  'fiat_warn': 'Ⱄ The buyer hasn\'t confirmed payment for 2 hours.\nIf you both agree tap "🔄 Cancel", or wait / open a dispute.',
  'auto_dispute': '⚖️ 24h passed without release — case automatically sent to the admin.',
  'lock_btn': '🔒 Lock them',
  'lock_yes': '🔒 Yes, lock',
  'fiat_btn': '✅ I paid',
  'dispute_btn': '⚖️ Dispute',
  'cancel': '❌ Cancel',
  'skip': 'Skip',
  'market': 'Order',
  'new_order': '➕ New order',
  'my_orders': '🧾 My orders',
  'guide': '❓ Guide',
  'back_p2p': '« P2P',
  'back_menu': '« Menu',
  'take': '🤝 Take',
  'take_yes': '✅ Yes, take it',
  'my_cancel': 'Cancel',
  'side_sell': '💸 Sell',
  'side_buy': '🛒 Buy',
  'seller': 'Seller',
  'buyer': 'Buyer',
  'tos_ok': '✅ I accept',
  'chat_off': '🔇 This trade chat is closed.',
  'timeout_generic': '⏰ Timeout',
  'of': 'of',
  'room_title': '🤝 Trade #{id}',
  'price_l': '🏷 Price',
  'amount_l': '💰 Amount',
  'st_canceled': '⛔️ Trade canceled.',
 },
}


def lang_of(uid):
    try:
        from zap_i18n import get_user_lang
        return 'fa' if get_user_lang(uid) == 'fa' else 'en'
    except Exception:
        return 'en'


def T(uid, key, **kw):
    lang = lang_of(uid)
    txt = TXT.get(lang) or TXT['en']
    s = txt.get(key) or TXT['en'].get(key) or key
    if kw:
        try:
            s = s.format(**kw)
        except Exception:
            pass
    return s


# ==================== helpers ====================

FA_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def parse_sats(text):
    if not text:
        return None
    t = text.strip().translate(FA_DIGITS)
    t = t.replace(',', '').replace('_', '').replace(' ', '')
    if not t.isdigit():
        return None
    n = int(t)
    return n if n > 0 else None


def fmt(n):
    return f'{n:,}'


def fee_for(sats):
    return max(1, (sats * FEE_BPS + 9999) // 10000)  # ceil to >=1 sat


def side_word(uid, side):
    return T(uid, 'side_sell') if side == 'sell' else T(uid, 'side_buy')


# ==================== escrow wallet ====================

class EscrowListener(EventListener):
    """Escrow wallet's own SDK listener — PAYMENT_SUCCEEDED (RECEIVE) = a lock
    landed; (SEND) = a payout/refund finished."""

    async def on_event(self, event):
        try:
            b = _bot()
            if 'PAYMENT_SUCCEEDED' not in type(event).__name__:
                return
            payment = getattr(event, 'payment', None)
            if payment is None:
                return
            ptype = getattr(payment, 'payment_type', None)
            amount = getattr(payment, 'amount_sats', getattr(payment, 'amount', 0)) or 0
            pid = getattr(payment, 'id', '')
            status = str(getattr(payment, 'status', ''))
            logging.info(f'p2p escrow event: {type(event).__name__} {ptype} {amount}s {status}')
            fut = asyncio.run_coroutine_threadsafe(
                on_escrow_payment(str(ptype), amount, pid, status),
                b.main_event_loop)
            fut.result(timeout=30)
        except Exception as e:
            logging.error(f'p2p escrow listener error: {e}')


async def escrow_sdk():
    global _escrow
    async with _escrow_lock:
        if _escrow is not None:
            return _escrow
        if not os.path.exists(ESCROW_SEED_PATH):
            raise RuntimeError('p2p escrow seed missing')
        b = _bot()
        mnemonic = open(ESCROW_SEED_PATH).read().strip()
        seed = b.Seed.MNEMONIC(mnemonic=mnemonic, passphrase=None)
        sdk = await b.connect(request=b.ConnectRequest(
            config=b.config, seed=seed, storage_dir=ESCROW_STORAGE_DIR))
        await sdk.add_event_listener(listener=EscrowListener())
        _escrow = sdk
        logging.info('p2p escrow wallet connected (listener attached)')
        return sdk


async def escrow_address():
    sdk = await escrow_sdk()
    resp = await sdk.receive_payment(request=_bot().ReceivePaymentRequest(
        payment_method=_bot().ReceivePaymentMethod.SPARK_ADDRESS()))
    return resp.payment_request


_spark_addr = {}   # uid -> spark address (static per wallet; skips ~30s re-derive)


async def spark_address_of(uid):
    """Current spark address of a bot user's wallet (for payouts).
    Spark addresses are static/reusable — cache per process."""
    if uid in _spark_addr:
        return _spark_addr[uid]

    async def _impl():
        b = _bot()
        sdk = await b.get_wallet(uid)
        resp = await sdk.receive_payment(request=b.ReceivePaymentRequest(
            payment_method=b.ReceivePaymentMethod.SPARK_ADDRESS()))
        return resp.payment_request

    addr = await offloop(_impl())   # ~30s re-derive: never on the main loop
    if addr:
        _spark_addr[uid] = addr
    return addr


async def send_sats_from_escrow(dest_address, sats, log_tag):
    """Zero-fee internal spark send from the escrow wallet. Returns (ok, payment_id, err).
    Runs off-loop — a slow send must never freeze buttons/chat."""
    async def _impl():
        b = _bot()
        sdk = await escrow_sdk()
        prep = await sdk.prepare_send_payment(request=b.PrepareSendPaymentRequest(
            payment_request=b.PaymentRequest.INPUT(input=dest_address),
            amount=int(sats)))
        resp = await sdk.send_payment(request=b.SendPaymentRequest(prepare_response=prep))
        payment = getattr(resp, 'payment', None)
        pid = getattr(payment, 'id', '') or ''
        status = str(getattr(payment, 'status', ''))
        logging.info(f'p2p escrow send [{log_tag}]: {sats}s -> {status} ({pid})')
        if 'COMPLETED' in status:
            return True, pid, None
        return 'PENDING' in status, pid, None if 'PENDING' in status else status
    try:
        return await offloop(_impl())
    except Exception as e:
        logging.error(f'p2p escrow send [{log_tag}] failed: {e}')
        return False, '', str(e)


async def sweep_fee_to_owner(trade):
    """0.5% fee: escrow sends the fee straight to the owner's wallet via a
    zero-fee SPARK address send (same zap method the bot uses for fee sweeps —
    no invoice, no LN routing)."""
    b = _bot()
    owner = admin_id()
    if not owner or trade['fee_paid']:
        return
    try:
        async def _impl():
            owner_sdk = await b.get_wallet(owner)
            addr_resp = await owner_sdk.receive_payment(request=b.ReceivePaymentRequest(
                payment_method=b.ReceivePaymentMethod.SPARK_ADDRESS()))
            owner_address = addr_resp.payment_request
            sdk = await escrow_sdk()
            preped = await sdk.prepare_send_payment(request=b.PrepareSendPaymentRequest(
                payment_request=b.PaymentRequest.INPUT(input=owner_address),
                amount=int(trade['fee'])))
            resp = await sdk.send_payment(request=b.SendPaymentRequest(
                prepare_response=preped))
            return str(getattr(getattr(resp, 'payment', None), 'status', ''))
        status = await offloop(_impl())
        if 'COMPLETED' in status or 'PENDING' in status:
            _exec('UPDATE trades SET fee_paid=1 WHERE id=?', (trade['id'],))
            logging.info(f'p2p fee swept via spark for trade #{trade["id"]}: '
                         f'{trade["fee"]}s ({status})')
            try:
                await b.client.send_message(
                    owner,
                    f'💰 **کارمزد P2P دریافت شد**\n\n'
                    f'💵 {trade["fee"]:,} sats\n'
                    f'🤝 معاوضه #{trade["id"]}')
            except Exception as ne:
                logging.error(f'p2p fee notify failed: {ne}')
        else:
            raise RuntimeError(f'fee spark send status: {status}')
    except Exception as e:
        tries = (trade.get('fee_tries') or 0) + 1
        _exec('UPDATE trades SET fee_tries=? WHERE id=?', (tries, trade['id']))
        logging.error(f'p2p fee sweep failed (try {tries}) trade #{trade["id"]}: {e}')


# ==================== escrow reconciliation ====================

async def on_escrow_payment(ptype, amount, pid, status):
    """Called from the escrow listener (and the startup scan)."""
    if 'RECEIVE' in ptype:
        row = _one("SELECT * FROM trades WHERE stage=? ORDER BY created ASC", (ST_LOCKING,))
        if row and amount and amount == row['sats'] + row['fee']:
            await mark_locked(row['id'])
        elif row:
            logging.info(f'p2p escrow RECEIVE {amount}s matches no pending lock '
                         f'(want {row["sats"] + row["fee"]}s) — ignored')
    elif 'SEND' in ptype:
        for col, done_stage, apply in (
                ('payout_id', ST_PAYOUT, 'payout'),
                ('refund_id', ST_REFUND, 'refund')):
            row = _one(f'SELECT * FROM trades WHERE {col}=? AND stage IN (?,?)',
                       (pid, ST_PAYOUT if col == 'payout_id' else ST_REFUND,
                        ST_PAYOUT if col == 'payout_id' else ST_REFUND))
            if row:
                if 'COMPLETED' in status:
                    if apply == 'payout':
                        await finish_payout(row['id'])
                    else:
                        await finish_refund(row['id'])


async def startup_scan():
    """One-shot after restart: resolve locks/payouts that landed while down."""
    try:
        since = int(time.time()) - 48 * 3600
        sdk = await escrow_sdk()
        resp = await sdk.list_payments(request=_bot().ListPaymentsRequest(
            from_timestamp=since, limit=100))
        payments = getattr(resp, 'payments', []) or []
        for p in payments:
            ptype = str(getattr(p, 'payment_type', ''))
            status = str(getattr(p, 'status', ''))
            amount = getattr(p, 'amount_sats', getattr(p, 'amount', 0)) or 0
            pid = getattr(p, 'id', '')
            if 'COMPLETED' not in status:
                continue
            if 'RECEIVE' in ptype:
                row = _one("SELECT * FROM trades WHERE stage=? ORDER BY created ASC", (ST_LOCKING,))
                if row and amount == row['sats'] + row['fee']:
                    await mark_locked(row['id'])
            elif 'SEND' in ptype:
                hit = _one('SELECT * FROM trades WHERE (payout_id=? AND stage=?) '
                           'OR (refund_id=? AND stage=?)',
                           (pid, ST_PAYOUT, pid, ST_REFUND))
                if hit:
                    if hit['payout_id'] == pid and hit['stage'] == ST_PAYOUT:
                        await finish_payout(hit['id'])
                    elif hit['refund_id'] == pid and hit['stage'] == ST_REFUND:
                        await finish_refund(hit['id'])
    except Exception as e:
        logging.error(f'p2p startup scan failed: {e}')


# ==================== UI renderers ====================

async def _del_pin_note(client, uid, panel_id):
    """Remove the pin service note Telegram posts right after a pin."""
    try:
        from telethon.tl.types import MessageActionPinMessage
        ms = await client.get_messages(uid, ids=list(range(panel_id + 1, panel_id + 5)))
        for m in ms:
            if m and getattr(m, 'action', None) is not None \
                    and isinstance(m.action, MessageActionPinMessage):
                await client.delete_messages(uid, m)
                return
    except Exception:
        pass


async def _strip_panel(client, uid):
    """Delete the previous p2p panel message so the chat stays uncluttered."""
    panel = _last_panel.pop(uid, None)
    if panel:
        try:
            await client.delete_messages(panel[0], panel[1])
        except Exception:
            pass
        await _del_pin_note(client, panel[0], panel[1])


async def _send_panel(client, uid, text, buttons):
    await _strip_panel(client, uid)
    msg = await client.send_message(uid, text, buttons=buttons)
    _last_panel[uid] = (msg.chat_id, msg.id)
    # pin the trade room so it stays on top in the user's PV
    # (its «pinned» service note is cleaned up when the panel goes away)
    try:
        from telethon.tl.functions.messages import UpdatePinnedMessageRequest
        await client(UpdatePinnedMessageRequest(peer=uid, id=msg.id, silent=True))
    except Exception as e:
        logging.debug(f'p2p pin panel {uid}: {e}')
    return msg


def _btn(label, data):
    from telethon import Button
    return Button.inline(label, data.encode() if isinstance(data, str) else data)


def room_text(uid, tr, stage_note=None):
    fa = lang_of(uid) == 'fa'
    role = T(uid, 'you_are_seller') if uid == tr['seller'] else T(uid, 'you_are_buyer')
    method = f"\n{T(uid, 'method_l')}: {tr['method']}" if tr.get('method') else ''
    head = T(uid, 'room_title', id=tr['id'])
    fee_line = (f' (+{fmt(tr["fee"])} {T(uid, "fee_short")})'
                if uid == tr['seller'] else '')
    amount_line = (f'🪙 {fmt(tr["sats"])} ساتوشی{fee_line}' if fa
                   else f'🪙 {fmt(tr["sats"])} sats{fee_line}')
    deal_line = f'🏷 {tr["price"]}'
    o = (_one('SELECT side, fiat_amount, fiat_code FROM orders WHERE id=?',
              (tr['order_id'],)) if tr.get('order_id') else None)
    if o and o.get('fiat_amount'):
        _fw = fiat_word(uid, o['fiat_code'] or 'IRT')
        _amt = f'🏷 {int(o["fiat_amount"]):,} {_fw}'
        if fa:
            deal_line = f'{"فروش" if o["side"] == "sell" else "خرید"} به اندازه\n{_amt}'
        else:
            deal_line = f'{"Selling" if o["side"] == "sell" else "Buying"} for\n{_amt}'
    rate_line = ''
    if tr.get('rate_used'):
        fw = 'تومان' if tr['rate_used'] >= 1_000_000 else 'USD'
        _src = 'yadio.io' if fw == 'تومان' else 'bot'
        _p = tr.get('premium')
        _ptag = f' ({_p:+g}%)' if _p else ''
        rate_line = (f'\n📈 نرخ: {_src}{_ptag}' if fa
                     else f'\n📈 Rate: {_src}{_ptag}')
    lines = (
        f'{head}\n'
        f'━━━━━━━━━━━━━━\n\n'
        f'{role}\n'
        f'{amount_line}\n'
        f'{deal_line}{rate_line}{method}\n\n'
    )
    note = stage_note if stage_note is not None else stage_note_for(uid, tr)
    text = lines + note
    if fa:
        text = '\n'.join('\u200f' + ln if ln.strip() else ln
                         for ln in text.split('\n'))
    return text


def stage_note_for(uid, tr):
    st = tr['stage']
    if st == ST_LOCKING:
        return T(uid, 'st_locking')
    if st == ST_LOCKED:
        return T(uid, 'st_locked') + '\n' + (
            T(uid, 'locked_buyer') if uid == tr['buyer'] else T(uid, 'locked_seller'))
    if st == ST_FIAT:
        return T(uid, 'st_fiat') + '\n' + (
            T(uid, 'fiat_seller') if uid == tr['seller'] else T(uid, 'st_fiat_buyer'))
    if st == ST_PAYOUT:
        return T(uid, 'st_payout')
    if st == ST_REFUND:
        return T(uid, 'st_payout')
    if st == ST_DONE:
        return T(uid, 'st_done')
    if st == ST_REFUNDED:
        return T(uid, 'st_refunded')
    if st == ST_DISPUTED:
        return T(uid, 'dispute_open')
    if st == ST_CANCELED:
        return T(uid, 'st_canceled')
    return ''


def room_buttons(uid, tr, confirm=None):
    """One contextual action per moment + cancel/dispute. confirm = pending two-tap."""
    st = tr['stage']
    rows = []
    if confirm == 'lock' and uid == tr['seller']:
        rows.append([_btn(T(uid, 'lock_yes').format(amt=fmt(tr['sats'] + tr['fee'])),
                          f'p2p:lockgo:{tr["id"]}'),
                     _btn('«', f'p2p:room:{tr["id"]}')])
    elif confirm == 'fiat' and uid == tr['buyer']:
        rows.append([_btn(T(uid, 'fiat_yes'), f'p2p:fiatgo:{tr["id"]}'),
                     _btn('«', f'p2p:room:{tr["id"]}')])
    elif confirm == 'rel' and uid == tr['seller']:
        rows.append([_btn(T(uid, 'rel_yes'), f'p2p:relgo:{tr["id"]}'),
                     _btn('«', f'p2p:room:{tr["id"]}')])
    elif confirm == 'cx':
        rows.append([_btn(T(uid, 'cx_yes'), f'p2p:cxgo:{tr["id"]}'),
                     _btn('«', f'p2p:room:{tr["id"]}')])
    elif confirm == 'dispute':
        rows.append([_btn(T(uid, 'dispute_yes'), f'p2p:dspgo:{tr["id"]}'),
                     _btn('«', f'p2p:room:{tr["id"]}')])
    else:
        if st == ST_LOCKING and uid == tr['seller']:
            rows.append([_btn(T(uid, 'lock_btn'), f'p2p:lock:{tr["id"]}')])
        elif st == ST_LOCKED and uid == tr['buyer']:
            rows.append([_btn(T(uid, 'fiat_btn'), f'p2p:fiat:{tr["id"]}')])
        elif st == ST_FIAT and uid == tr['seller']:
            rows.append([_btn(T(uid, 'rel_btn'), f'p2p:rel:{tr["id"]}')])
        if st in (ST_LOCKING, ST_LOCKED, ST_FIAT, ST_PAYOUT, ST_REFUND, ST_DONE, ST_REFUNDED):
            if tr.get('chat_open'):
                rows.append([_btn(T(uid, 'chat_off_btn'), f'p2p:chat:{tr["id"]}')])
            else:
                rows.append([_btn(T(uid, 'chat_btn'), f'p2p:chat:{tr["id"]}')])
        # dispute available in every live stage (incl. payout/refund)
        if st in (ST_LOCKING, ST_LOCKED, ST_FIAT, ST_PAYOUT, ST_REFUND):
            if st in (ST_LOCKING, ST_LOCKED, ST_FIAT):
                rows.append([_btn(T(uid, 'cx_btn'), f'p2p:cx:{tr["id"]}'),
                             _btn(T(uid, 'dispute_btn'), f'p2p:dsp:{tr["id"]}')])
            else:
                rows.append([_btn(T(uid, 'dispute_btn'), f'p2p:dsp:{tr["id"]}')])
    if st == ST_DISPUTED and not confirm:
        rows.append([_btn(T(uid, 'dispute_btn'), f'p2p:dsp:{tr["id"]}')])
    rows.append([_btn(T(uid, 'back_p2p'), 'p2p:home')])
    return rows


async def update_room(client, tid, note=None, buttons=True):
    """Refresh BOTH parties' live room messages."""
    tr = get_trade(tid)
    if not tr:
        return
    panels = json.loads(tr.get('panels') or '{}')
    for uid_s, chat_msg in panels.items():
        uid = int(uid_s)
        chat_id, msg_id = chat_msg
        try:
            await client.edit_message(
                chat_id, msg_id, room_text(uid, tr, note),
                buttons=room_buttons(uid, tr) if buttons else None)
        except Exception as e:
            logging.debug(f'p2p room edit {uid}: {e}')


async def channel_status(oid, status_line, delete=False):
    """Edit the channel listing with a status line; delete=True removes it."""
    try:
        o = get_order(oid)
        if not o or not o.get('channel_msg'):
            return
        b = _bot()
        if delete:
            await b.client.delete_messages(P2P_LIST_CHANNEL, o['channel_msg'])
            _exec('UPDATE orders SET channel_msg=NULL WHERE id=?', (oid,))
            return
        card = order_card(0, o)
        await b.client.edit_message(P2P_LIST_CHANNEL, o['channel_msg'],
                                    f'{status_line}\n\n{card}',
                                    buttons=None, parse_mode='html')
    except Exception as e:
        logging.debug(f'p2p channel status {oid}: {e}')


async def channel_repost(oid):
    """Post the order card to the listing channel again (timeout put it
    back on the market) and remember the new post id."""
    try:
        o = get_order(oid)
        if not o or o.get('status') != 'open':
            return
        b = _bot()
        card = order_card(o['maker'], o)
        card = '\n'.join(l for l in card.split('\n')
                         if 'این سفارش مال خودته' not in l
                         and 'مال خودته' not in l)
        if o['side'] == 'sell':
            label = '🛒 خرید ساتوشی' if lang_of(o['maker']) == 'fa' else '🛒 Buy sats'
        else:
            label = '💸 فروش ساتوشی' if lang_of(o['maker']) == 'fa' else '💸 Sell sats'
        url = f'https://t.me/Zap_lnBot?start=p2p_{oid}'
        from telethon import Button as _BU
        btn = [[_BU.url(label, url)]]
        post = await b.client.send_message(P2P_LIST_CHANNEL, card, buttons=btn,
                                           parse_mode='html')
        _exec('UPDATE orders SET channel_msg=? WHERE id=?', (post.id, oid))
    except Exception as e:
        logging.warning(f'p2p channel repost failed #{oid}: {e}')


async def audit(client, tid, event, extra=''):
    """Append-only trade/order audit line to the bot's backup channel.
    tid=0 → order-level event (details then come via `extra`)."""
    try:
        b = _bot()
        tr = get_trade(tid) if tid else None
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if tr:
            text = (f'🤝 P2P #{tid} · {event}\n'
                    f'💰 {tr["sats"]}+{tr["fee"]}s · 🏷 {tr["price"]}\n'
                    f'👤 buyer {user_link(tr["buyer"])} · seller {user_link(tr["seller"])}\n'
                    f'📌 stage: {tr["stage"]}\n'
                    f'🕒 {ts}{chr(10) + extra if extra else ""}')
        else:
            text = f'🤝 P2P · {event}\n🕒 {ts}{chr(10) + extra if extra else ""}'
        await b.client.send_message(int(os.getenv('BACKUP_CHANNEL_ID', '0') or 0), text, parse_mode='html')
    except Exception as e:
        logging.warning(f'p2p audit failed #{tid}: {e}')


async def notify(client, uid, text, buttons=None):
    try:
        return await client.send_message(uid, text, buttons=buttons)
    except Exception as e:
        logging.error(f'p2p notify {uid}: {e}')
        return None


async def reply_to_room(client, tid, uid, text, buttons=None):
    """Reply to a party's live room message so the notice lands in context."""
    tr = get_trade(tid)
    if not tr:
        return await notify(client, uid, text, buttons)
    panels = json.loads(tr.get('panels') or '{}')
    panel = panels.get(str(uid))
    if panel:
        try:
            return await client.send_message(uid, text, reply_to=panel[1],
                                             buttons=buttons)
        except Exception as e:
            logging.debug(f'p2p reply-to-room {uid}: {e}')
    return await notify(client, uid, text, buttons)


# ==================== market / orders UI ====================

def market_orders(filt):
    if filt in ('sell', 'buy'):
        return _rows("SELECT * FROM orders WHERE status='open' AND side=? ORDER BY id DESC", (filt,))
    return _rows("SELECT * FROM orders WHERE status='open' ORDER BY id DESC")


async def bot_fiat_rate_for(code):
    """Rate for a SPECIFIC fiat code (fiat per 1 BTC). Used at take time so
    the rate always matches the ORDER's currency, not the taker's settings —
    a toman order resolved with a taker's USD rate once minted 12.5B sats."""
    try:
        b = _bot()
        rates = await b.get_fiat_rates()
        code = (code or 'IRT').upper()
        rate = rates.get(code)
        if not rate and code == 'IRR':
            rate = rates.get('IRT')
        if rate and rate > 0:
            return float(rate), code
    except Exception as e:
        logging.warning(f'p2p bot_fiat_rate_for failed: {e}')
    return None, None


async def bot_fiat_rate(uid):
    """Rate for the USER'S SAVED fiat currency (settings) — fiat per 1 BTC.
    Falls back IRT → USD. None if unavailable."""
    try:
        b = _bot()
        rates = await b.get_fiat_rates()
        try:
            fiat = b.get_user_fiat(uid) or 'USD'
        except Exception:
            fiat = 'USD'
        rate = rates.get(fiat) or rates.get('IRT') or rates.get('IRR') or rates.get('USD')
        if rate and rate > 0:
            # rates dict is FIAT-per-1-BTC (e.g. IRT=22e9) — use as-is
            return float(rate), fiat
    except Exception as e:
        logging.warning(f'p2p bot_fiat_rate failed: {e}')
    return None, None


def fiat_word(uid, fiat):
    """User-facing name of the currency."""
    return 'تومان' if (fiat or '').upper() in ('IRT', 'IRR') else (fiat or 'USD')


def float_sats(fiat_amount, premium, rate):
    """sats for a floating order at take time. fiat_amount in Toman,
    premium percent (-… to +…), rate = fiat per 1 BTC."""
    if not rate or rate <= 0:
        return None
    eff = rate * (1 + (premium or 0) / 100.0)
    return int((fiat_amount / eff) * 100_000_000)


def rating_stats(uid):
    """(avg_stars, count) from unique trades only; (None, 0) if none."""
    rows = _rows('SELECT AVG(stars) AS a, COUNT(*) AS c FROM ratings WHERE ratee=?', (uid,))
    if not rows or not rows[0]['c']:
        return None, 0
    return round(rows[0]['a'] or 0, 1), rows[0]['c']


async def save_rating(client, rater, ratee, tid, stars):
    """Store one rating per (trade, rater). Returns False if duplicate."""
    exists = _one('SELECT id FROM ratings WHERE trade_id=? AND rater=?', (tid, rater))
    if exists:
        return False
    _exec('INSERT INTO ratings (trade_id, rater, ratee, stars, at) VALUES (?,?,?,?,?)',
          (tid, rater, ratee, stars, time.time()))
    key = (tid, rater)
    loc = _rate_msgs.pop(key, None)
    if loc:
        try:
            tg = getattr(client, 'client', client)   # bot wrapper → telethon
            # silent confirm: edit the ask-message only — no new message
            await tg.edit_message(loc[0], loc[1], '⭐ ثبت شد',
                                  buttons=None)
            return True
        except Exception as e:
            logging.debug(f'p2p rate edit {rater}: {e}')
    return True


def maker_stats(maker_id):
    """(completed trades, days since first order) for a maker."""
    done = _one("SELECT COUNT(*) AS c FROM trades WHERE (buyer=? OR seller=?) "
                "AND stage='done'", (maker_id, maker_id))
    first = _one("SELECT MIN(created) AS t FROM orders WHERE maker=?", (maker_id,))
    days = 0
    if first and first['t']:
        days = max(1, int((time.time() - first['t']) // 86400) + 1)
    return (done['c'] if done else 0), days


_rate_cache = {'rate': None, 'code': 'IRT', 'ts': 0}


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            return ex.submit(asyncio.run, coro).result(timeout=15)
    return asyncio.run(coro)


FIAT_FLAGS = {'IRT': '🇮🇷', 'IRR': '🇮🇷', 'USD': '🇺🇸', 'EUR': '🇪🇺',
              'TRY': '🇹🇷', 'AED': '🇦🇪', 'GBP': '🇬🇧', 'RUB': '🇷🇺'}


def order_card(uid, o, pos=None, total=None):
    fa = lang_of(uid) == 'fa'
    is_float = o.get('price_mode') == 'float'
    prem = o.get('premium') or 0
    code = (o.get('fiat_code') or 'IRT').upper()
    fw = fiat_word(uid, code)
    flag = FIAT_FLAGS.get(code, '💱')
    src_name = 'yadio.io' if code in ('IRT', 'IRR') else 'bot'

    # headline: «💸 فروش ساتوشی» / «🛒 خرید ساتوشی»
    if o['side'] == 'sell':
        side = ('💸 فروش ساتوشی' if fa else '💸 Selling sats')
    else:
        side = ('🛒 خرید ساتوشی' if fa else '🛒 Buying sats')

    prem_tag = f' {prem:+g}%' if prem else ''
    if is_float and o.get('fiat_amount'):
        # fiat-based: fiat amount fixed; sats at market rate
        amount = f'💵 {int(o["fiat_amount"]):,} {fw} {flag}'.strip()
        price = (f'📈 قیمت بازار ({src_name}){prem_tag}'
                 if fa else f'📈 Market rate ({src_name}){prem_tag}')
    elif is_float:
        # sats-based: sats exact; price at market
        amount = (f'🪙 {fmt(o["sats"])} ساتوشی' if fa
                  else f'🪙 {fmt(o["sats"])} sats')
        price = (f'📈 قیمت بازار ({src_name}){prem_tag}'
                 if fa else f'📈 Market rate ({src_name}){prem_tag}')
    else:
        amount = (f'🪙 {fmt(o["sats"])} ساتوشی' if fa
                  else f'🪙 {fmt(o["sats"])} sats')
        price = (f'🏷 به قیمت {o["price"]} {flag}'.strip()
                 if fa else f'🏷 At {o["price"]}')

    method = f'\n{T(uid, "method_l")}: {o["method"]}' if o.get('method') else ''
    stats = f'\n{maker_stats_line(uid, o["maker"])}'
    own = '\n📌 این سفارش مال خودته' if o['maker'] == uid else ''
    page = f'  ·  {pos + 1}/{total}' if pos is not None else ''
    head = f'📢 {T(uid, "market")}{page}'

    card = (f'{head}\n'
            f'━━━━━━━━━━━━━━\n'
            f'{side}\n'
            f'{amount}\n'
            f'{price}{method}\n'
            f'━━━━━━━━━━━━━━{stats}{own}')
    if fa:
        # force RTL rendering line by line
        card = '\n'.join('\u200f' + ln if ln.strip() else ln
                         for ln in card.split('\n'))
    return card


def maker_stats_line(uid, maker_id):
    avg, n = rating_stats(maker_id)
    if n:
        word = 'معامله' if lang_of(uid) == 'fa' else 'trades'
        return f'⭐ {avg} ({n} {word})'
    trades, days = maker_stats(maker_id)
    if lang_of(uid) == 'fa':
        return f'👤 کاربر در {days} روز {trades} معامله موفق داشته'
    return f'👤 {trades} successful trades in {days} days'


def market_buttons(uid, orders, idx, filt):
    is_own = bool(orders) and orders[idx]['maker'] == uid
    rows = []
    if orders:
        rows.append([_btn(T(uid, 'take'), f'p2p:take:{orders[idx]["id"]}')])
    flabels = [('همه', 'all'), ('💸 فروش', 'sell'), ('🛒 خرید', 'buy')] \
        if lang_of(uid) == 'fa' else [('All', 'all'), ('💸 Sell', 'sell'), ('🛒 Buy', 'buy')]
    rows.append([_btn(f'« {t} »' if f == filt else t, f'p2p:mf:{f}') for t, f in flabels])
    if len(orders) > 1:
        rows.append([_btn('⬅️', f'p2p:mprev:{idx}:{filt}'),
                     _btn('➡️', f'p2p:mnext:{idx}:{filt}')])
    rows.append([_btn(T(uid, 'back_p2p'), 'p2p:home')])
    return rows


async def show_market(client, uid, filt='all', idx=0, edit_event=None, uid_view=None):
    """Market carousel. filt='mine' shows only the user's own open orders."""
    if filt == 'mine':
        orders = _rows("SELECT * FROM orders WHERE status='open' AND maker=? ORDER BY id DESC", (uid,))
    else:
        orders = market_orders(filt)
    if not orders:
        text = f'🌐 {T(uid, "market")}\n\n{T(uid, "order_none")}'
        buttons = [[_btn(T(uid, 'new_order'), 'p2p:new')],
                   [_btn(T(uid, 'back_p2p'), 'p2p:home')]]
    else:
        idx = max(0, min(idx, len(orders) - 1))
        text = order_card(uid, orders[idx], idx, len(orders))
        buttons = market_buttons(uid, orders, idx, filt)
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons)
    else:
        await _send_panel(client, uid, text, buttons)


# ==================== wizard ====================

def _prem_buttons(uid):
    """Preset premium/discount picker for the order wizard."""
    row1 = [_btn('-2%', 'p2p:prem:-2'), _btn('-1%', 'p2p:prem:-1'), _btn('0%', 'p2p:prem:0')]
    row2 = [_btn('+1%', 'p2p:prem:1'), _btn('+2%', 'p2p:prem:2'), _btn('+3%', 'p2p:prem:3')]
    custom = _btn('✍️ دستی' if lang_of(uid) == 'fa' else '✍️ Custom', 'p2p:prem:custom')
    return [row1, row2, [custom]]


async def _wiz_premium_done(client, uid, prem):
    """Shared tail of the wizard premium step (button or typed).
    Returns the optional fixed_resolved notice, or None when the live rate
    is unavailable (wizard stays on the premium step to retry)."""
    w = _wiz.get(uid) or {}
    w['premium'] = prem
    if w.get('price_mode') == 'float':
        w['step'] = 'method'
        return ''   # rate is read at take time
    if w.get('unit') == 'sats' or not w.get('fiat_amount'):
        w['step'] = 'method'
        return ''
    # toman+fixed: resolve sats NOW from rate + premium
    rate, rate_fiat = await bot_fiat_rate(uid)
    if not rate:
        return None   # step stays 'premium' so the retry lands here again
    w['step'] = 'method'
    w['fiat_code'] = rate_fiat
    w['sats'] = float_sats(w['fiat_amount'], prem, rate)
    w['fee'] = fee_for(w['sats'])
    return T(uid, 'fixed_resolved',
             sats=fmt(w['sats']), fiat=f'{int(w["fiat_amount"]):,}',
             prem=(f'+{prem:g}' if prem >= 0 else f'{prem:g}'))


async def wiz_start(client, uid, edit_event=None):
    _wiz[uid] = {'step': 'side', '_ts': time.time()}
    text = f'➕ {T(uid, "new_order")}\n\n{T(uid, "wiz_side")}'
    buttons = [[_btn('💸 ' + T(uid, 'seller'), 'p2p:side:sell'),
                _btn('🛒 ' + T(uid, 'buyer'), 'p2p:side:buy')],
               [_btn(T(uid, 'cancel'), 'p2p:wcancel')]]
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons)
    else:
        await _send_panel(client, uid, text, buttons)


async def wiz_text(client, event, uid, text):
    """Returns True when the step consumed the text."""
    if uid in _wiz and isinstance(_wiz[uid], dict):
        _wiz[uid]['_ts'] = time.time()
    w = _wiz.get(uid)
    if not w:
        return False
    step = w.get('step')
    if step == 'fiat_amount' and (w.get('unit') == 'toman' and not w.get('sats')):
        # sell-by-toman first step: store the toman amount, resolve sats from rate
        try:
            amt = float(text.replace(',', '').strip())
            if amt <= 0:
                raise ValueError
        except Exception:
            _r, _f = await bot_fiat_rate(uid)
            _fw = fiat_word(uid, _f)
            await event.respond(T(uid, 'wiz_bad_fiat')
                                .replace('تومان', _fw).replace('Toman', _fw))
            return True
        w['fiat_amount'] = amt
        rate, rate_fiat = await bot_fiat_rate(uid)
        if not rate:
            await event.respond(T(uid, 'float_no_rate'))
            return True
        w['fiat_code'] = rate_fiat
        w['sats'] = float_sats(amt, 0, rate)
        w['fee'] = fee_for(w['sats'])
        w['step'] = 'pmode'
        await event.respond(T(uid, 'price_mode'), buttons=[
            [_btn(T(uid, 'price_fixed'), 'p2p:pmode:fixed')],
            [_btn(T(uid, 'price_float'), 'p2p:pmode:float')],
            [_btn(T(uid, 'cancel'), 'p2p:wcancel')]])
        return True
    if step == 'amount':
        n = parse_sats(text)
        if not n:
            await event.respond(T(uid, 'wiz_bad_amount'))
            return True
        w['sats'] = n
        w['fee'] = fee_for(n)
        w['step'] = 'pmode'
        await event.respond(T(uid, 'price_mode'), buttons=[
            [_btn(T(uid, 'price_fixed'), 'p2p:pmode:fixed')],
            [_btn(T(uid, 'price_float'), 'p2p:pmode:float')],
            [_btn(T(uid, 'cancel'), 'p2p:wcancel')]])
        return True
    if step == 'sats_price':
        n = parse_sats(text)
        if not n:
            await event.respond(T(uid, 'wiz_bad_sats'))
            return True
        w['sats'] = n
        w['fee'] = fee_for(n)
        w['step'] = 'method'
        await event.respond(T(uid, 'wiz_method'),
                            buttons=[[_btn(T(uid, 'skip'), 'p2p:skipmethod')]])
        return True
    if step == 'price':
        if len(text) > 120:
            text = text[:120]
        w['price'] = text
        w['price_mode'] = 'fixed'
        w['step'] = 'method'
        await event.respond(T(uid, 'wiz_method'),
                            buttons=[[_btn(T(uid, 'skip'), 'p2p:skipmethod')]])
        return True
    if step == 'fiat_amount':
        try:
            amt = float(text.replace(',', '').strip())
            if amt <= 0:
                raise ValueError
        except Exception:
            await event.respond(T(uid, 'wiz_bad_fiat'))
            return True
        w['fiat_amount'] = amt
        w['step'] = 'premium'
        await event.respond(T(uid, 'wiz_premium'), buttons=_prem_buttons(uid))
        return True
    if step == 'premium':
        try:
            prem = float(text.replace('%', '').strip())
        except Exception:
            await event.respond(T(uid, 'wiz_bad_premium'))
            return True
        notice = await _wiz_premium_done(client, uid, prem)
        if notice is None:
            await event.respond(T(uid, 'float_no_rate'))
            return True
        if notice:
            await event.respond(notice)
        await event.respond(T(uid, 'wiz_method'),
                            buttons=[[_btn(T(uid, 'skip'), 'p2p:skipmethod')]])
        return True
    if step == 'method':
        w['method'] = text[:80] if text else ''
        w['step'] = 'ttl'
        # expiry picker
        if lang_of(uid) == 'fa':
            t_text = '⏳ سفارش تا کی تو بازار بمونه؟'
            t_labels = [r[0] for r in ORDER_TTLS]
        else:
            t_text = '⏳ How long should the order stay on the market?'
            t_labels = [r[1] for r in ORDER_TTLS]
        buttons = [[_btn(t_labels[0], f'p2p:ttl:{ORDER_TTLS[0][2]}'),
                    _btn(t_labels[1], f'p2p:ttl:{ORDER_TTLS[1][2]}')],
                   [_btn(t_labels[2], f'p2p:ttl:{ORDER_TTLS[2][2]}')],
                   [_btn(T(uid, 'cancel'), 'p2p:wcancel')]]
        await event.respond(t_text, buttons=buttons)
        return True
    return False


ORDER_TTLS = [
    ('12 ساعت', '12h', 12 * 3600),
    ('24 ساعت', '24h', 24 * 3600),
    ('2 روز', '2 days', 2 * 86400),
]


async def wiz_review(client, event, uid):
    w = _wiz.get(uid) or {}
    fa = lang_of(uid) == 'fa'
    method = f'\n{T(uid, "method_l")}: {w.get("method")}' if w.get('method') else ''
    ttl_row = next((r for r in ORDER_TTLS if r[2] == w.get('ttl_secs')), None)
    if fa:
        ttl_line = f'\n⏳ مهلت: {ttl_row[0]}' if ttl_row else ''
    else:
        ttl_line = f'\n⏳ Expires in: {ttl_row[1]}' if ttl_row else ''

    mode = w.get('price_mode') or 'fixed'
    prem = w.get('premium') or 0
    code = (w.get('fiat_code') or 'IRT').upper()
    fw = fiat_word(uid, code)
    flag = FIAT_FLAGS.get(code, '💱')
    src_name = 'yadio.io' if code in ('IRT', 'IRR') else 'bot'
    prem_tag = f' {prem:+g}%' if prem else ''
    if w.get('side') == 'sell':
        side = ('💸 فروش ساتوشی' if fa else '💸 Selling sats')
    else:
        side = ('🛒 خرید ساتوشی' if fa else '🛒 Buying sats')

    if mode == 'float' and w.get('unit') == 'toman' and w.get('fiat_amount'):
        amount = f'💵 {int(w["fiat_amount"]):,} {fw} {flag}'.strip()
        price = (f'📈 قیمت بازار ({src_name}){prem_tag}'
                 if fa else f'📈 Market rate ({src_name}){prem_tag}')
    elif mode == 'float':
        amount = f'🪙 {fmt(w["sats"])} ساتوشی' if fa else f'🪙 {fmt(w["sats"])} sats'
        price = (f'📈 قیمت بازار ({src_name}){prem_tag}'
                 if fa else f'📈 Market rate ({src_name}){prem_tag}')
    else:
        amount = f'🪙 {fmt(w["sats"])} ساتوشی' if fa else f'🪙 {fmt(w["sats"])} sats'
        price = (f'🏷 به قیمت {w.get("price")} {flag}'.strip()
                 if fa else f'🏷 At {w.get("price") or "—"}')

    text = (f'{T(uid, "wiz_review")}\n\n'
            f'━━━━━━━━━━━━━━\n'
            f'{side}\n'
            f'{amount}\n'
            f'{price}{method}\n'
            f'━━━━━━━━━━━━━━{ttl_line}')
    if fa:
        text = '\n'.join('\u200f' + ln if ln.strip() else ln
                         for ln in text.split('\n'))
    buttons = [[_btn(T(uid, 'publish'), 'p2p:publish')],
               [_btn(T(uid, 'cancel'), 'p2p:wcancel')]]
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)


async def wiz_publish(client, uid):
    w = _wiz.pop(uid, None)
    if not w or 'sats' not in w:
        return None
    if w.get('price_mode') == 'float' and 'fiat_amount' not in w             and not (w.get('unit') == 'sats' and w.get('sats')):
        return None
    if w.get('price_mode') != 'float' and 'price' not in w \
            and not (w.get('unit') == 'toman' and w.get('fiat_amount')):
        return None
    mode = w.get('price_mode') or 'fixed'
    price_disp = w.get('price') or ''
    fw = fiat_word(uid, w.get('fiat_code') or 'IRT')
    if w.get('unit') == 'toman' and w.get('fiat_amount') and mode != 'float':
        price_disp = f'{int(w["fiat_amount"]):,} {fw}'
    if mode == 'float' and w.get('unit') == 'sats':
        price_disp = f'{T(uid, "float_tag")} · {fmt(w["sats"])} sats'
    elif mode == 'float':
        prem = w.get('premium') or 0
        price_disp = (f'{int(w.get("fiat_amount") or 0):,} {fw} · '
                      f'{T(uid, "float_tag")} ({prem:+g}%)')
    elif w.get('unit') == 'toman' and w.get('fiat_amount'):
        # fiat manual price: price is the toman amount (sats shown in مقدار)
        price_disp = f'{int(w["fiat_amount"]):,} {fw}'
    cur = _exec(
        'INSERT INTO orders (side, maker, sats, price, method, status, created, ttl_secs, '
        'price_mode, fiat_amount, premium, fiat_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (w['side'], uid, w['sats'], price_disp, w.get('method') or '', 'open',
         time.time(), w.get('ttl_secs'), mode,
         w.get('fiat_amount'),
         w.get('premium') if mode == 'float' else None,
         w.get('fiat_code')))
    oid = cur.lastrowid
    await audit(client, 0, 'ORDER PUBLISHED',
                f'🆔 order #{oid} · side {w["side"]} · maker {uid}')
    # public listing post in the channel
    try:
        b = _bot()
        o = get_order(oid)
        card = order_card(uid, o)          # maker's language
        card = '\n'.join(l for l in card.split('\n')
                         if 'این سفارش مال خودته' not in l
                         and 'مال خودته' not in l)
        if o['side'] == 'sell':
            label = '🛒 خرید ساتوشی' if lang_of(uid) == 'fa' else '🛒 Buy sats'
        else:
            label = '💸 فروش ساتوشی' if lang_of(uid) == 'fa' else '💸 Sell sats'
        url = f'https://t.me/Zap_lnBot?start=p2p_{oid}'
        from telethon import Button as _BU
        btn = [[_BU.url(label, url)]]
        post = await b.client.send_message(P2P_LIST_CHANNEL, card, buttons=btn,
                                           parse_mode='html')
        _exec('UPDATE orders SET channel_msg=? WHERE id=?', (post.id, oid))
    except Exception as e:
        logging.warning(f'p2p channel post failed #{oid}: {e}')
    return oid


# ==================== data access ====================

def get_trade(tid):
    return _one('SELECT * FROM trades WHERE id=?', (tid,))


def get_order(oid):
    return _one('SELECT * FROM orders WHERE id=?', (oid,))


def active_trade_of(uid):
    rows = _rows(
        'SELECT * FROM trades WHERE (buyer=? OR seller=?) AND stage IN (?,?,?,?,?,?,?,?) '
        'ORDER BY id DESC LIMIT 1',
        (uid, uid, ST_LOCKING, ST_LOCKED, ST_FIAT, ST_PAYOUT, ST_DISPUTED,
         ST_REFUND, ST_DONE, ST_REFUNDED))
    return rows[0] if rows else None


# ==================== engine actions ====================

def live_trade_of(uid):
    """An actually-in-flight trade (blocks taking new orders)."""
    rows = _rows(
        'SELECT * FROM trades WHERE (buyer=? OR seller=?) AND stage IN (?,?,?,?,?,?) '
        'ORDER BY id DESC LIMIT 1',
        (uid, uid, ST_LOCKING, ST_LOCKED, ST_FIAT, ST_PAYOUT, ST_REFUND, ST_DISPUTED))
    return rows[0] if rows else None


async def take_order(client, uid, oid):
    """Transactional take: first taker wins; creates the trade."""
    if live_trade_of(uid):
        await notify(client, uid, T(uid, 'busy_trade'))
        return None
    # floating price: resolve sats NOW from the bot's own rate (before claiming)
    row0 = get_order(oid)
    rate_used = None
    if row0 and (row0.get('price_mode') == 'float'):
        # resolve in the ORDER's currency — never the taker's saved fiat
        rate_used, _rate_fiat = await bot_fiat_rate_for(row0.get('fiat_code') or 'IRT')
        if not rate_used:
            await notify(client, uid, T(uid, 'float_no_rate'))
            return None
    with _db_lock:
        db = _connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            if row is None or row['status'] != 'open':
                db.rollback()
                return None
            db.execute("UPDATE orders SET status='taken', taken_at=? WHERE id=?",
                       (time.time(), oid))
            side = row['side']
            buyer, seller = (row['maker'], uid) if side == 'buy' else (uid, row['maker'])
            sats = row['sats']
            fiat_final = None
            if row['price_mode'] == 'float' and rate_used:
                if row['fiat_amount']:
                    # toman-based float: sats follow the rate
                    sats = float_sats(row['fiat_amount'], row['premium'], rate_used)
                    fiat_final = row['fiat_amount']
                else:
                    # sats-based float: fiat value follows the rate (+premium)
                    _prem = row['premium'] or 0
                    fiat_final = round(sats / 100_000_000 * rate_used
                                       * (1 + _prem / 100.0), 2)
            cur = db.execute(
                'INSERT INTO trades (order_id, buyer, seller, sats, fee, price, method, '
                'stage, created, updated, lock_started, fiat_final, rate_used, premium) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (oid, buyer, seller, sats, fee_for(sats), row['price'],
                 row['method'], ST_LOCKING, time.time(), time.time(), time.time(),
                 fiat_final, rate_used, row['premium']))
            tid = cur.lastrowid
            db.commit()
        except Exception:
            db.rollback()
            raise
    await audit(client, tid, 'TAKEN', f'🆔 order #{oid} · taker {uid}')
    await channel_status(oid, '🤝 برداشته شد', delete=True)
    tr = get_trade(tid)
    # open a live room panel for both parties
    for party in (buyer, seller):
        try:
            msg = await _send_panel(client, party, room_text(party, tr),
                                    room_buttons(party, tr))
            panels = json.loads(get_trade(tid)['panels'] or '{}')
            panels[str(party)] = [msg.chat_id, msg.id]
            _exec('UPDATE trades SET panels=? WHERE id=?', (json.dumps(panels), tid))
        except Exception as e:
            logging.error(f'p2p room open {party}: {e}')
    return tid


async def do_lock(client, uid, tid):
    """Seller locks sats+fee: internal wallet -> escrow spark send."""
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_LOCKING or uid != tr['seller']:
        await notify(client, uid, T(uid, 'no_action'))
        return
    b = _bot()
    need = tr['sats'] + tr['fee']

    async def _lock_sdk():
        # runs on a worker loop — slow SDK calls never freeze buttons/chat
        sdk = await b.get_wallet(uid)
        bal = int(await b.get_balance(uid))
        if bal < need:
            try:
                info = await sdk.get_info(request=b.GetInfoRequest(ensure_synced=True))
                bal = int(info.balance_sats)
            except Exception:
                bal = int(await b.get_balance(uid))
            if bal < need:
                return None
        dest = await escrow_address()
        prep = await sdk.prepare_send_payment(request=b.PrepareSendPaymentRequest(
            payment_request=b.PaymentRequest.INPUT(input=dest),
            amount=need))
        resp = await sdk.send_payment(request=b.SendPaymentRequest(prepare_response=prep))
        return str(getattr(getattr(resp, 'payment', None), 'status', ''))

    try:
        status = await offloop(_lock_sdk())
        if status is None:
            await notify(client, uid, T(uid, 'lock_fail_bal', need=fmt(need)))
            return
        logging.info(f'p2p lock send trade #{tid}: {need}s -> {status}')
        if 'COMPLETED' in status or 'PENDING' in status:
            await mark_locked(tid)
        else:
            await update_room(client, tid, note=T(uid, 'st_locking_progress'))
    except Exception as e:
        logging.error(f'p2p lock trade #{tid}: {e}')
        await notify(client, uid, T(uid, 'lock_fail', err=str(e)[:120]))


async def mark_locked(tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_LOCKING:
        return
    _exec('UPDATE trades SET stage=?, updated=?, lock_started=COALESCE(lock_started, ?) '
          'WHERE id=? AND stage=?',
          (ST_LOCKED, time.time(), time.time(), tid, ST_LOCKING))
    b = _bot()
    for party in (tr['buyer'], tr['seller']):
        await reply_to_room(b.client, tid, party, f'🔒 {T(party, "st_locked")}')
    await update_room(b.client, tid)
    await audit(b.client, tid, 'LOCKED (escrow funded)')
    logging.info(f'p2p trade #{tid} LOCKED')
    # warm the buyer's spark address so the payout later doesn't stall
    asyncio.get_event_loop().create_task(spark_address_of(tr['buyer']))


async def do_fiat(client, uid, tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_LOCKED or uid != tr['buyer']:
        await notify(client, uid, T(uid, 'no_action'))
        return
    _exec('UPDATE trades SET stage=?, fiat_at=?, updated=? WHERE id=?',
          (ST_FIAT, time.time(), time.time(), tid))
    await update_room(client, tid)
    await reply_to_room(client, tid, tr['seller'], T(tr['seller'], 'ping_fiat'))
    await audit(client, tid, 'FIAT CONFIRMED (buyer pressed paid)')
    logging.info(f'p2p trade #{tid} fiat confirmed by buyer')


async def do_release(client, uid, tid):
    """Seller releases: escrow pays buyer's wallet; fee sweeps to owner."""
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_FIAT or uid != tr['seller']:
        await notify(client, uid, T(uid, 'no_action'))
        return
    await update_room(client, tid, note=T(uid, 'st_payout'), buttons=False)
    try:
        dest = await spark_address_of(tr['buyer'])
    except Exception as e:
        logging.error(f'p2p payout: buyer wallet unreachable #{tid}: {e}')
        await update_room(client, tid)
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: buyer wallet unreachable — payout parked')
        return
    ok, pid, err = await send_sats_from_escrow(dest, tr['sats'], f'payout#{tid}')
    if ok == 'PENDING' or (ok is True):
        _exec('UPDATE trades SET stage=?, payout_id=?, updated=? WHERE id=?',
              (ST_PAYOUT, pid, time.time(), tid))
        await reply_to_room(client, tid, tr['buyer'], T(tr['buyer'], 'ping_release'),
                            buttons=[[_btn(T(tr['buyer'], 'room_close'), f'p2p:close:{tid}')]])
        if ok is True:
            await finish_payout(tid)
    else:
        logging.error(f'p2p payout send failed #{tid}: {err}')
        _spark_addr.pop(tr['buyer'], None)
        await update_room(client, tid)
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: payout failed ({str(err)[:80]})')


_rate_msgs = {}   # (tid, uid) -> (chat_id, msg_id)


async def finish_payout(tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_PAYOUT:
        return
    _exec("UPDATE trades SET stage='done', updated=? WHERE id=? AND stage=?",
          (time.time(), tid, ST_PAYOUT))
    _exec("UPDATE orders SET status='done' WHERE id=?", (tr['order_id'],))
    b = _bot()
    await update_room(b.client, tid)
    other = {tr['buyer']: tr['seller'], tr['seller']: tr['buyer']}
    for party in (tr['buyer'], tr['seller']):
        ratee = other[party]
        stars = [[_btn('⭐' * i, f'p2p:rate:{tid}:{ratee}:{i}')] for i in (1, 2, 3, 4, 5)]
        rate_txt = f'{T(party, "st_done")}\n\n{T(party, "rate_ask")}'
        msg = await reply_to_room(b.client, tid, party, rate_txt, buttons=stars)
        if msg:
            _rate_msgs[(tid, party)] = (msg.chat_id, msg.id)
    tr2 = get_trade(tid)
    # sweep in background — never hold DONE/rating on a wallet call
    asyncio.get_event_loop().create_task(sweep_fee_to_owner(tr2))
    await audit(b.client, tid, 'DONE (payout completed)')
    logging.info(f'p2p trade #{tid} DONE')


async def request_cancel(client, uid, tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] not in (ST_LOCKING, ST_LOCKED, ST_FIAT):
        await notify(client, uid, T(uid, 'no_action'))
        return
    other = tr['buyer'] if uid == tr['seller'] else tr['seller']
    if tr['stage'] == ST_LOCKING:
        # pre-lock: nothing in escrow — one-sided cancel is safe
        _exec("UPDATE trades SET stage=?, updated=? WHERE id=?", (ST_CANCELED, time.time(), tid))
        _exec("UPDATE orders SET status='canceled' WHERE id=?", (tr['order_id'],))
        await audit(client, tid, f'CANCELED (one-sided pre-lock) by {uid}')
        await channel_status(tr['order_id'], '⛔️ لغو شد', delete=True)
        await notify(client, uid, T(uid, 'cx_solo'))
        await notify(client, other, T(other, 'cx_solo'))
        await update_room(client, tid)
        return
    col = 'cancel_seller' if uid == tr['seller'] else 'cancel_buyer'
    _exec(f'UPDATE trades SET {col}=1, updated=? WHERE id=?', (time.time(), tid))
    tr = get_trade(tid)
    if tr['cancel_seller'] and tr['cancel_buyer']:
        await start_refund(client, tid)
    else:
        await notify(client, other, T(other, 'cx_req'))
        await update_room(client, tid)


async def start_refund(client, tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] not in (ST_LOCKING, ST_LOCKED, ST_FIAT, ST_REFUND):
        return
    if tr['stage'] == ST_LOCKING:
        # nothing confirmed locked yet — just cancel; escrow listener refunds
        # any in-flight lock that lands (matched amount, no active lock left)
        _exec("UPDATE trades SET stage=?, updated=? WHERE id=?",
              (ST_CANCELED, time.time(), tid))
        _exec("UPDATE orders SET status='canceled' WHERE id=?", (tr['order_id'],))
        await audit(client, tid, 'CANCELED (mutual)')
        await channel_status(tr['order_id'], '⛔️ لغو شد', delete=True)
        await update_room(client, tid)
        return
    try:
        dest = await spark_address_of(tr['seller'])
    except Exception as e:
        logging.error(f'p2p refund: seller wallet unreachable #{tid}: {e}')
        _exec("UPDATE trades SET stage='refunding', updated=? WHERE id=?", (time.time(), tid))
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: refund parked (seller wallet unreachable)')
        return
    ok, pid, err = await send_sats_from_escrow(dest, tr['sats'] + tr['fee'], f'refund#{tid}')
    if ok == 'PENDING' or (ok is True):
        _exec('UPDATE trades SET stage=?, refund_id=?, updated=? WHERE id=?',
              (ST_REFUND, pid, time.time(), tid))
        _exec("UPDATE orders SET status='canceled' WHERE id=?", (tr['order_id'],))
        await channel_status(tr['order_id'], '↩️ برگشت خورد')
        if ok is True:
            await finish_refund(tid)
    else:
        _exec("UPDATE trades SET stage='refunding', updated=? WHERE id=?", (time.time(), tid))
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: refund failed ({str(err)[:80]})')


async def finish_refund(tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_REFUND:
        return
    _exec("UPDATE trades SET stage='refunded', updated=? WHERE id=? AND stage=?",
          (time.time(), tid, ST_REFUND))
    b = _bot()
    await update_room(b.client, tid)
    await audit(b.client, tid, 'REFUNDED (sats returned to seller)')
    other = {tr['buyer']: tr['seller'], tr['seller']: tr['buyer']}
    for party in (tr['buyer'], tr['seller']):
        ratee = other[party]
        stars = [[_btn('⭐' * i, f'p2p:rate:{tid}:{ratee}:{i}')] for i in (1, 2, 3, 4, 5)]
        msg = await reply_to_room(b.client, tid, party,
                                  f'{T(party, "st_refunded")}\n\n{T(party, "rate_ask")}',
                                  buttons=stars)
        if msg:
            _rate_msgs[(tid, party)] = (msg.chat_id, msg.id)
    logging.info(f'p2p trade #{tid} REFUNDED')


async def open_dispute(client, uid, tid, reason=None):
    tr = get_trade(tid)
    # dispute only makes sense once the escrow is actually funded (lock paid)
    if not tr or tr['stage'] not in (ST_LOCKED, ST_FIAT, ST_PAYOUT, ST_DISPUTED):
        await notify(client, uid, T(uid, 'dispute_not_locked'))
        return
    prev = (tr.get('dispute_reason') or '').strip()
    merged = f'{prev}\n—\n{reason}' if prev and reason else (reason or prev or '')
    _exec("UPDATE trades SET stage='disputed', dispute_reason=?, updated=? WHERE id=?",
          (merged, time.time(), tid))
    await audit(client, tid, f'DISPUTE FILED by {uid}', f'📝 {(merged or "")[:200]}')
    await update_room(client, tid)
    await admin_case(client, tid, auto=False)


def user_link(uid):
    """Clickable tg://user?id= link for a numeric Telegram id."""
    return f'<a href="tg://user?id={uid}">{uid}</a>'


async def username_line(uid):
    """@username of a user, or None + a nudge was sent asking them to set one."""
    b = _bot()
    try:
        entity = await b.client.get_entity(uid)
        un = getattr(entity, 'username', None)
        if un:
            return f'@{un}'
        # no username: nudge once per trade party
        try:
            await b.client.send_message(uid, '⚠️ برای استفاده از بخش P2P یه یوزرنیم تلگرام ست کن (تنظیمات تلگرام → Username).')
        except Exception:
            pass
    except Exception as e:
        logging.error(f'p2p username lookup {uid}: {e}')
    return '—'


async def admin_case(client, tid, auto=False):
    tr = get_trade(tid)
    if not tr:
        return
    admins = p2p_admin_ids()
    if not admins:
        return
    buyer_un = await username_line(tr['buyer'])
    seller_un = await username_line(tr['seller'])
    st = 'قفل' if tr['stage'] in (ST_LOCKED,) else tr['stage']
    reason = f'\n📝 {_h(tr["dispute_reason"])}' if tr.get('dispute_reason') else ''
    auto_line = '\n⏰ خودکار (۲۴ ساعت)' if auto else ''
    text = (f'⚖️ پرونده p2p #{tid}{auto_line}\n\n'
            f'🛒 خریدار: {_h(buyer_un)} · {user_link(tr["buyer"])}\n\n'
            f'💸 فروشنده: {_h(seller_un)} · {user_link(tr["seller"])}\n\n'
            f'💰 {fmt(tr["sats"])} sats (+{fmt(tr["fee"])} کارمزد)\n'
            f'🏷 {_h(tr["price"])}\n'
            f'📌 مرحله: {_h(st)}{reason}')
    buttons = [[_btn('✅ آزادسازی', f'p2p:admrel:{tid}'),
                _btn('↩️ برگشت', f'p2p:admref:{tid}')],
               [_btn('💬 خلاصه چت', f'p2p:admchat:{tid}')]]
    b = _bot()
    for adm in admins:
        try:
            await b.client.send_message(adm, text, buttons=buttons, parse_mode='html')
        except Exception as e:
            logging.error(f'p2p case notify {adm}: {e}')
    logging.info(f'p2p dispute case #{tid} -> {len(admins)} admin(s) (auto={auto})')


async def admin_release(client, tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] not in (ST_DISPUTED, ST_FIAT, ST_LOCKED, ST_PAYOUT):
        await notify(client, admin_id(), T(admin_id(), 'no_action'))
        return
    # force path: pay the buyer from escrow regardless of whose turn it is
    await update_room(client, tid, note=T(tr['buyer'], 'st_payout'), buttons=False)
    try:
        dest = await spark_address_of(tr['buyer'])
    except Exception as e:
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: buyer wallet unreachable: {str(e)[:80]}')
        return
    ok, pid, err = await send_sats_from_escrow(dest, tr['sats'], f'adminpayout#{tid}')
    if ok == 'PENDING' or (ok is True):
        _exec('UPDATE trades SET stage=?, payout_id=?, updated=? WHERE id=?',
              (ST_PAYOUT, pid, time.time(), tid))
        if ok is True:
            await finish_payout(tid)
    else:
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: admin payout failed ({str(err)[:80]})')


async def admin_refund(client, tid):
    tr = get_trade(tid)
    if not tr:
        return
    if tr['stage'] in (ST_DONE, ST_REFUNDED, ST_CANCELED):
        await notify(client, admin_id(), T(admin_id(), 'no_action'))
        return
    if tr['stage'] == ST_DISPUTED:
        # start_refund skips disputed — force the refund here (seller gets sats+fee)
        await update_room(client, tid, buttons=False)
        try:
            dest = await spark_address_of(tr['seller'])
        except Exception as e:
            await notify(client, admin_id(), f'⚠️ p2p #{tid}: seller wallet unreachable: {str(e)[:80]}')
            return
        ok, pid, err = await send_sats_from_escrow(dest, tr['sats'] + tr['fee'], f'adminrefund#{tid}')
        if ok == 'PENDING' or (ok is True):
            _exec('UPDATE trades SET stage=?, refund_id=?, updated=? WHERE id=?',
                  (ST_REFUND, pid, time.time(), tid))
            _exec("UPDATE orders SET status='canceled' WHERE id=?", (tr['order_id'],))
            await channel_status(tr['order_id'], '↩️ برگشت خورد')
            if ok is True:
                await finish_refund(tid)
            return
        _exec("UPDATE trades SET stage='refunding', updated=? WHERE id=?", (time.time(), tid))
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: refund failed ({str(err)[:80]})')
        return
    await start_refund(client, tid)


# ==================== sweep (rides the bot's existing sweeper) ====================

async def sweep(client):
    now = time.time()
    # -2) auto-close chats of finished trades 2h after completion
    _exec("UPDATE trades SET chat_open=0 WHERE chat_open=1 AND stage IN (?,?,?) AND updated < ?",
          (ST_DONE, ST_REFUNDED, ST_CANCELED, time.time() - 2 * 3600))

    # -1) abandoned wizards (user pressed new-order then walked away)
    for wuid in list(_wiz.keys()):
        w = _wiz.get(wuid)
        if isinstance(w, dict) and time.time() - w.get('_ts', 0) > 900:
            _wiz.pop(wuid, None)

    # 0) expired open orders (user-picked TTL) -> close them
    for o in _rows("SELECT * FROM orders WHERE status='open' AND ttl_secs IS NOT NULL"):
        if now - o['created'] > o['ttl_secs']:
            _exec("UPDATE orders SET status='expired' WHERE id=? AND status='open'", (o['id'],))
            await audit(client, 0, 'ORDER EXPIRED', f'🆔 order #{o["id"]} · maker {o["maker"]}')
            await channel_status(o['id'], '⏳ منقضی شد', delete=True)
            await notify(client, o['maker'], f'⏳ سفارش #{o["id"]} مهلتش تموم شد و از بازار برداشته شد.')
    # 1) taken but never locked
    for tr in _rows('SELECT * FROM trades WHERE stage=?', (ST_LOCKING,)):
        if now - (tr['lock_started'] or tr['created']) > LOCK_WAIT_SECS:
            _exec("UPDATE trades SET stage='canceled', updated=? WHERE id=? AND stage=?",
                  (now, tr['id'], ST_LOCKING))
            _exec("UPDATE orders SET status='open', taken_at=NULL WHERE id=? AND status='taken'",
                  (tr['order_id'],))
            await channel_repost(tr['order_id'])
            o = _one('SELECT maker, side FROM orders WHERE id=?', (tr['order_id'],))
            maker = o['maker'] if o else tr['seller']
            if maker == tr['seller']:
                # sell order: maker flaked, taker buyer waited in vain
                await notify(client, maker, T(maker, 'order_back_sell'))
                await notify(client, tr['buyer'], T(tr['buyer'], 'partner_gone'))
            else:
                # buy order: taker seller flaked, maker buyer waited in vain
                await notify(client, maker, T(maker, 'order_back_buy'))
                await notify(client, tr['seller'], T(tr['seller'], 'lock_timeout_self'))
    # 2) locked, buyer silent
    for tr in _rows('SELECT * FROM trades WHERE stage=? AND warned=0', (ST_LOCKED,)):
        if now - tr['updated'] > FIAT_TIMEOUT_SECS:
            _exec('UPDATE trades SET warned=1 WHERE id=?', (tr['id'],))
            for party in (tr['buyer'], tr['seller']):
                await notify(client, party, T(party, 'fiat_warn'))
    # 3) fiat confirmed, seller silent -> auto dispute
    for tr in _rows('SELECT * FROM trades WHERE stage=?', (ST_FIAT,)):
        if tr['fiat_at'] and now - tr['fiat_at'] > RELEASE_TIMEOUT_SECS:
            _exec("UPDATE trades SET stage='disputed', updated=? WHERE id=? AND stage=?",
                  (now, tr['id'], ST_FIAT))
            await update_room(client, tr['id'])
            for party in (tr['buyer'], tr['seller']):
                await notify(client, party, T(party, 'auto_dispute'))
            await admin_case(client, tr['id'], auto=True)
    # 4) payout/refund in flight — check escrow ledger (send-side bookkeeping)
    try:
        async def _lp():
            sdk = await escrow_sdk()
            return await sdk.list_payments(request=_bot().ListPaymentsRequest(limit=50))
        resp = await offloop(_lp())
        by_id = {getattr(p, 'id', ''): p for p in (getattr(resp, 'payments', []) or [])}
        for tr in _rows('SELECT * FROM trades WHERE stage IN (?,?)', (ST_PAYOUT, ST_REFUND)):
            pid = tr['payout_id'] if tr['stage'] == ST_PAYOUT else tr['refund_id']
            p = by_id.get(pid or '')
            if p is None:
                continue
            status = str(getattr(p, 'status', ''))
            if 'COMPLETED' in status:
                if tr['stage'] == ST_PAYOUT:
                    await finish_payout(tr['id'])
                else:
                    await finish_refund(tr['id'])
            elif 'FAILED' in status:
                tries = (tr['payout_tries'] if tr['stage'] == ST_PAYOUT else tr['refund_tries']) + 1
                col = 'payout_tries' if tr['stage'] == ST_PAYOUT else 'refund_tries'
                _exec(f'UPDATE trades SET {col}=? WHERE id=?', (tries, tr['id']))
                if tr['stage'] == ST_PAYOUT and tries <= 2:
                    await admin_release(client, tr['id'])  # retry once via force path
                else:
                    await notify(client, admin_id(),
                                 f'⚠️ p2p #{tr["id"]}: {"payout" if tr["stage"] == ST_PAYOUT else "refund"} '
                                 f'FAILED on network — manual action needed')
    except Exception as e:
        logging.debug(f'p2p sweep ledger check: {e}')
    # 5) retry unpaid fees (best-effort, capped)
    for tr in _rows('SELECT * FROM trades WHERE stage=? AND fee_paid=0 AND fee_tries<?',
                    (ST_DONE, FEE_MAX_TRIES)):
        await sweep_fee_to_owner(tr)


# ==================== boot ====================

async def boot(client):
    init_db()
    if not enabled_for_all():
        logging.info('p2p: no test users configured — module idle')
        return
    if not os.path.exists(ESCROW_SEED_PATH):
        logging.warning('p2p: escrow seed missing — section disabled')
        return
    try:
        await escrow_sdk()
        await startup_scan()
        logging.info('p2p section booted')
    except Exception as e:
        logging.error(f'p2p boot failed: {e}')


def enabled_for_all():
    return bool(test_users())


# ==================== admin panel ====================
def p2p_stats():
    def c(sql):
        r = _one(sql)
        return (r.get('c') if isinstance(r, dict) else 0) or 0
    fees = (_one('SELECT COALESCE(SUM(fee),0) AS s FROM trades WHERE fee_paid=1') or {}).get('s') or 0
    return {
        'trades': c('SELECT COUNT(*) AS c FROM trades'),
        'done': c("SELECT COUNT(*) AS c FROM trades WHERE stage IN ('done','refunded')"),
        'users': c('SELECT COUNT(DISTINCT u) AS c FROM '
                   '(SELECT buyer AS u FROM trades UNION SELECT seller FROM trades)'),
        'fees': fees,
        'open_orders': c("SELECT COUNT(*) AS c FROM orders WHERE status='open'"),
        'disputes': c("SELECT COUNT(*) AS c FROM trades WHERE stage='disputed'"),
        'banned': c('SELECT COUNT(*) AS c FROM banned'),
        'admins': len(p2p_admin_ids()),
    }


def chat_summary(tid, limit=30, trunc=200):
    rows = _rows('SELECT * FROM chat_msgs WHERE tid=? ORDER BY id DESC LIMIT ?', (tid, limit))
    if not rows:
        return '💬 بین طرفین پیامی رد و بدل نشده (یا چت خاموش بوده).'
    rows = list(reversed(rows))
    tr = get_trade(tid)
    seller = tr['seller'] if tr else None
    lines = [f'📋 چت معاوضه #{tid} — {len(rows)} پیام اخیر:']
    for r in rows:
        who = '💸 فروشنده' if r['uid'] == seller else '🛒 خریدار'
        t = r['text'] or ''
        if len(t) > trunc:
            t = t[:trunc] + '…'
        lines.append(f'{who}: {t}')
    return '\n'.join(lines)


async def admin_panel(client, uid, edit_event=None):
    st = p2p_stats()
    text = ('🛡 پنل ادمین P2P\n\n'
            '📊 آمار:\n'
            f'• معاوضه‌ها: {st["trades"]} (تموم‌شده: {st["done"]})\n'
            f'• کاربرای معامله‌کننده: {st["users"]}\n'
            f'• کارمزدهای جمع‌شده: {fmt(st["fees"])} sats\n'
            f'• سفارش‌های باز بازار: {st["open_orders"]}\n'
            f'• اختلاف‌های باز: {st["disputes"]}\n'
            f'• بن‌شده‌ها: {st["banned"]} · ادمین‌ها: {st["admins"]}')
    buttons = [
        [_btn('⚖️ اختلاف‌های باز', 'p2p:adm:disputes')],
        [_btn('🚫 بن کاربر', 'p2p:adm:ban'), _btn('📜 بن‌شده‌ها', 'p2p:adm:banlist')],
        [_btn('👥 ادمین‌های داوری', 'p2p:adm:admins')],
        [_btn(T(uid, 'back_p2p'), 'p2p:home')],
    ]
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons)
    else:
        await _send_panel(client, uid, text, buttons)


async def admin_disputes(client, uid, edit_event=None):
    rows = _rows("SELECT * FROM trades WHERE stage='disputed' ORDER BY updated DESC LIMIT 20")
    if not rows:
        text, buttons = '✅ اختلاف بازی وجود نداره.', [[_btn('« پنل', 'p2p:adm')]]
    else:
        text = f'⚖️ اختلاف‌های باز ({len(rows)}):'
        buttons = [[_btn(f'#{r["id"]} · {fmt(r["sats"])}s', f'p2p:admcase:{r["id"]}')] for r in rows]
        buttons.append([_btn('« پنل', 'p2p:adm')])
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons)
    else:
        await _send_panel(client, uid, text, buttons)


async def admin_banlist(client, uid, edit_event=None):
    rows = _rows('SELECT * FROM banned ORDER BY at DESC LIMIT 30')
    if not rows:
        text, buttons = '🚫 هیچ‌کی بن نشده.', [[_btn('« پنل', 'p2p:adm')]]
    else:
        text = f'🚫 بن‌شده‌ها ({len(rows)}):'
        buttons = []
        for r in rows:
            reason = f' — {r["reason"]}' if r['reason'] else ''
            text += f'\n• {r["uid"]}{reason}'
            buttons.append([_btn(f'♻️ رفع بن {r["uid"]}', f'p2p:adm:unban:{r["uid"]}')])
        buttons.append([_btn('« پنل', 'p2p:adm')])
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons)
    else:
        await _send_panel(client, uid, text, buttons)


async def admin_admins(client, uid, edit_event=None):
    ids = p2p_admin_ids()
    text = '👥 ادمین‌های داوری:\n' + '\n'.join(
        f'• {i}' + (' (مالک)' if i == admin_id() else '') for i in ids)
    buttons = []
    if uid == admin_id():
        buttons.append([_btn('➕ افزودن ادمین', 'p2p:adm:admadd')])
        for i in ids:
            if i != admin_id():
                buttons.append([_btn(f'❌ حذف {i}', f'p2p:adm:admdel:{i}')])
    buttons.append([_btn('« پنل', 'p2p:adm')])
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons)
    else:
        await _send_panel(client, uid, text, buttons)


async def admin_case_view(client, uid, tid, edit_event=None):
    tr = get_trade(tid)
    if not tr:
        text, buttons = 'پرونده پیدا نشد.', [[_btn('« پنل', 'p2p:adm')]]
    else:
        buyer_un = await username_line(tr['buyer'])
        seller_un = await username_line(tr['seller'])
        reason = f'\n📝 {_h(tr["dispute_reason"])}' if tr.get('dispute_reason') else ''
        text = (f'⚖️ پرونده #{tid}\n\n'
                f'🛒 خریدار: {_h(buyer_un)} · {user_link(tr["buyer"])}\n'
                f'💸 فروشنده: {_h(seller_un)} · {user_link(tr["seller"])}\n\n'
                f'💰 {fmt(tr["sats"])} sats (+{fmt(tr["fee"])} کارمزد)\n'
                f'🏷 {_h(tr["price"])}\n'
                f'📌 مرحله: {_h(tr["stage"])}{reason}\n\n'
                + _h(chat_summary(tid)))
        buttons = [[_btn('✅ آزادسازی', f'p2p:admrel:{tid}'),
                    _btn('↩️ برگشت', f'p2p:admref:{tid}')],
                   [_btn('💬 چت کامل', f'p2p:admchat:{tid}'),
                    _btn('« پنل', 'p2p:adm')]]
    if edit_event is not None:
        await edit_event.edit(text, buttons=buttons, parse_mode='html')
    else:
        await _send_panel(client, uid, text, buttons)


# ==================== routing: callbacks ====================

async def on_callback(client, event, data):
    uid = event.sender_id
    init_db()
    if is_banned(uid):
        await event.answer('🚫 دسترسی‌ات به P2P مسدود شده.', alert=True)
        return
    if not enabled_for(uid):
        await event.answer('⚠️ P2P در حال تست است.')
        return

    if data == 'p2p' or data == 'p2p:home':
        await event.answer()
        if not has_agreed(uid):
            text = f'{T(uid, "tos_title")}\n\n{T(uid, "tos_body")}'
            await event.edit(text, buttons=[[_btn(T(uid, 'tos_ok'), 'p2p:agree')]])
            return
        text = (f'{T(uid, "home_title")}\n\n{T(uid, "home_body")}\n'
                f'{T(uid, "home_fee")}')
        n = active_trade_count(uid)
        rows = [
            [_btn(T(uid, 'market'), 'p2p:market'), _btn(T(uid, 'new_order'), 'p2p:new')],
        ]
        if n:
            rows.append([_btn(f'{T(uid, "active_title")} ({n})', 'p2p:active')])
        rows.append([_btn(T(uid, 'my_orders'), 'p2p:mine'), _btn(T(uid, 'guide'), 'p2p:guide')])
        if is_p2p_admin(uid):
            rows.append([_btn('🛡 پنل ادمین', 'p2p:adm')])
        from telethon import Button as _B
        rows.append([_B.url(T(uid, 'ch_join'), 'https://t.me/your_channel')])
        rows.append([_btn(T(uid, 'back_menu'), 'p2p:exit')])
        await event.edit(text, buttons=rows)
        return

    if data == 'p2p:agree':
        await event.answer()
        set_agreed(uid)
        text = (f'{T(uid, "home_title")}\n\n{T(uid, "home_body")}\n'
                f'{T(uid, "home_fee")}')
        n = active_trade_count(uid)
        rows = [
            [_btn(T(uid, 'market'), 'p2p:market'), _btn(T(uid, 'new_order'), 'p2p:new')],
        ]
        if n:
            rows.append([_btn(f'{T(uid, "active_title")} ({n})', 'p2p:active')])
        rows.append([_btn(T(uid, 'my_orders'), 'p2p:mine'), _btn(T(uid, 'guide'), 'p2p:guide')])
        if is_p2p_admin(uid):
            rows.append([_btn('🛡 پنل ادمین', 'p2p:adm')])
        rows.append([_btn(T(uid, 'back_menu'), 'p2p:exit')])
        await event.edit(text, buttons=rows)
        return

    if data == 'p2p:exit':
        await event.answer()
        b = _bot()
        await b.show_main_menu(event, edit=True)
        return

    if data.startswith('p2p:chat:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr or uid not in (tr['buyer'], tr['seller']):
            return
        other = tr['buyer'] if uid == tr['seller'] else tr['seller']
        if tr.get('chat_open'):
            _exec('UPDATE trades SET chat_open=0 WHERE id=?', (tid,))
            await notify(client, uid, T(uid, 'chat_turned_off'))
            await notify(client, other, T(other, 'chat_off_notice'))
        else:
            _exec('UPDATE trades SET chat_open=1 WHERE id=?', (tid,))
            await notify(client, uid, T(uid, 'chat_turned_on'))
            await notify(client, other, T(other, 'chat_turned_on'))
        await update_room(client, tid)
        return

    if data.startswith('p2p:pmode:'):
        await event.answer()
        mode = data.split(':')[2]
        w = _wiz.get(uid) or {}
        w['price_mode'] = mode
        _wiz[uid] = w
        if mode == 'fixed' and w.get('unit') == 'toman' and w.get('fiat_amount'):
            # toman-seller manual price: ask how many SATS for his toman amount
            w['step'] = 'sats_price'
            await event.edit(T(uid, 'wiz_sats_price'))
        elif mode == 'fixed':
            w['step'] = 'price'
            await event.edit(T(uid, 'wiz_price'))
        elif mode == 'float' and w.get('unit') == 'sats':
            # sats + live rate: premium % adjusts the derived fiat value
            w['step'] = 'premium'
            await event.edit(T(uid, 'wiz_premium'), buttons=_prem_buttons(uid))
        elif mode == 'float' and w.get('unit') == 'toman' and w.get('fiat_amount'):
            # toman + live rate: premium % on top of the market rate
            w['step'] = 'premium'
            await event.edit(T(uid, 'wiz_premium'), buttons=_prem_buttons(uid))
        else:
            w['step'] = 'fiat_amount'
            _rate, _fiat = await bot_fiat_rate(uid)
            fw = fiat_word(uid, _fiat)
            txt = T(uid, 'wiz_fiat_amount').replace('تومان', fw).replace('Toman', fw)
            await event.edit(txt)
        return


    if data.startswith('p2p:rate:'):
        await event.answer()
        _, _, tid, ratee, stars = data.split(':')
        await save_rating(client, uid, int(ratee), int(tid), int(stars))
        return

    if data.startswith('p2p:close:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr or uid not in (tr['buyer'], tr['seller']):
            return
        # drop his panel (message + its pin) and kill his chat side
        panels = json.loads(tr.get('panels') or '{}')
        if str(uid) in panels:
            chat_id, msg_id = panels[str(uid)]
            try:
                await client.delete_messages(chat_id, msg_id)
            except Exception:
                pass
            await _del_pin_note(client, chat_id, msg_id)
            panels.pop(str(uid), None)
            _exec('UPDATE trades SET panels=? WHERE id=?', (json.dumps(panels), tid))
        col = 'chat_off_seller' if uid == tr['seller'] else 'chat_off_buyer'
        _exec(f'UPDATE trades SET {col}=1, chat_open=0 WHERE id=?', (tid,))
        await notify(client, uid, T(uid, 'room_closed'))
        return

    if data == 'p2p:active':
        # in-progress trades list -> tap one to jump back into its live room
        await event.answer()
        rows = _rows('SELECT * FROM trades WHERE (buyer=? OR seller=?) '
                     'AND stage IN (?,?,?,?,?,?) ORDER BY id DESC',
                     (uid, uid, ST_LOCKING, ST_LOCKED, ST_FIAT, ST_PAYOUT,
                      ST_REFUND, ST_DISPUTED))
        if not rows:
            return
        if len(rows) == 1:
            await _open_room(client, uid, rows[0]['id'])
            return
        # several in progress: list them as buttons
        btns = []
        for tr in rows:
            role = T(uid, 'side_sell') if uid == tr['seller'] else T(uid, 'side_buy')
            btns.append([_btn(f'#{tr["id"]} · {role} {fmt(tr["sats"])} sats',
                              f'p2p:room:{tr["id"]}')])
        btns.append([_btn(T(uid, 'back_p2p'), 'p2p:home')])
        await event.edit(f'🔄 {T(uid, "active_title")}', buttons=btns)
        return

    if data == 'p2p:guide':
        await event.answer()
        await event.edit(T(uid, 'guide_text'),
                         buttons=[[_btn(T(uid, 'back_p2p'), 'p2p:home')]])
        return

    if data == 'p2p:market':
        await event.answer()
        await show_market(client, uid, edit_event=event, uid_view=uid)
        return

    if data.startswith('p2p:mf:') or data.startswith('p2p:mprev:') or data.startswith('p2p:mnext:') \
:
        await event.answer()
        parts = data.split(':')
        if parts[1] == 'mf':
            filt = parts[2]
            await show_market(client, uid, filt=filt, idx=0, edit_event=event)
        else:
            idx = int(parts[2])
            filt = parts[3]
            step = -1 if parts[1] == 'mprev' else 1
            await show_market(client, uid, filt=filt, idx=idx + step, edit_event=event)
        return

    if data == 'p2p:new':
        await event.answer()
        await wiz_start(client, uid, edit_event=event)
        return

    if data == 'p2p:wcancel':
        await event.answer()
        _wiz.pop(uid, None)
        await on_callback(client, event, 'p2p:home')
        return

    if data.startswith('p2p:side:'):
        await event.answer()
        w = _wiz.get(uid)
        if not isinstance(w, dict):
            w = {}
        w['side'] = data.split(':', 2)[2]
        w['step'] = 'unit'
        _wiz[uid] = w
        _rate, _fiat = await bot_fiat_rate(uid)
        fw = fiat_word(uid, _fiat)
        await event.edit(f'{T(uid, "unit_ask")}\n\n{T(uid, "fiat_hint")}', buttons=[
            [_btn(T(uid, 'unit_sats'), 'p2p:unit:sats')],
            [_btn(T(uid, 'unit_toman').format(fiat=fw), 'p2p:unit:toman')],
            [_btn(T(uid, 'cancel'), 'p2p:wcancel')]])
        return

    if data.startswith('p2p:unit:'):
        await event.answer()
        unit = data.split(':')[2]
        w = _wiz.get(uid) or {}
        w['unit'] = unit
        if unit == 'sats':
            w['step'] = 'amount'
            txt = T(uid, 'wiz_amount')
        else:
            w['step'] = 'fiat_amount'
            _rate, _fiat = await bot_fiat_rate(uid)
            fw = fiat_word(uid, _fiat)
            txt = T(uid, 'wiz_fiat_amount').replace('تومان', fw).replace('Toman', fw)
        await event.edit(txt)
        return

    if data.startswith('p2p:prem:'):
        await event.answer()
        w = _wiz.get(uid) or {}
        if w.get('step') != 'premium':
            return
        val = data.split(':')[2]
        if val == 'custom':
            await event.edit(T(uid, 'wiz_premium'))
            return
        try:
            prem = float(val)
        except Exception:
            return
        notice = await _wiz_premium_done(client, uid, prem)
        if notice is None:
            await event.edit(T(uid, 'float_no_rate'))
            return
        await event.edit((notice + '\n\n' if notice else '') + T(uid, 'wiz_method'),
                         buttons=[[_btn(T(uid, 'skip'), 'p2p:skipmethod')]])
        return

    if data == 'p2p:skipmethod':
        await event.answer()
        w = _wiz.get(uid) or {}
        w['method'] = ''
        w['step'] = 'ttl'
        # expiry picker (same as the text path)
        if lang_of(uid) == 'fa':
            t_text = '⏳ سفارش تا کی تو بازار بمونه؟'
            t_labels = [r[0] for r in ORDER_TTLS]
        else:
            t_text = '⏳ How long should the order stay on the market?'
            t_labels = [r[1] for r in ORDER_TTLS]
        await event.edit(t_text, buttons=[
            [_btn(t_labels[0], f'p2p:ttl:{ORDER_TTLS[0][2]}'),
             _btn(t_labels[1], f'p2p:ttl:{ORDER_TTLS[1][2]}')],
            [_btn(t_labels[2], f'p2p:ttl:{ORDER_TTLS[2][2]}')],
            [_btn(T(uid, 'cancel'), 'p2p:wcancel')]])
        return

    if data.startswith('p2p:ttl:'):
        await event.answer()
        w = _wiz.get(uid) or {}
        w['ttl_secs'] = int(data.split(':')[2])
        _wiz[uid] = w
        await wiz_review(client, event, uid)
        return

    if data == 'p2p:publish':
        await event.answer()
        if uid in _wiz and _wiz[uid].get('published'):
            return
        oid = await wiz_publish(client, uid)
        if oid:
            if uid in _wiz:
                _wiz[uid]['published'] = True
                _wiz.pop(uid, None)
            await event.edit(f'{T(uid, "published")}  (#{oid})\n\n'
                             f'{order_card(uid, get_order(oid))}',
                             buttons=[[_btn(T(uid, 'back_p2p'), 'p2p:home')]])
        else:
            await event.answer('⚠️')
        return

    if data == 'p2p:mine':
        await event.answer()
        rows = _rows('SELECT * FROM orders WHERE maker=? ORDER BY id DESC LIMIT 15', (uid,))
        smap = {'open': 'mine_open', 'taken': 'mine_taken', 'done': 'mine_done',
                'canceled': 'mine_canceled', 'expired': 'mine_expired'}
        lines = []
        btns = []
        for o in rows:
            lines.append(f'#{o["id"]} · {side_word(uid, o["side"])} {fmt(o["sats"])} sats — '
                         f'{T(uid, smap.get(o["status"], "mine_open"))}')
            if o['status'] == 'open':
                btns.append([_btn(f'{T(uid, "my_cancel")} #{o["id"]}', f'p2p:ocancel:{o["id"]}')])
        text = (f'🧾 {T(uid, "my_orders")}\n\n' + ('\n'.join(lines) if lines else T(uid, 'my_empty')))
        btns.append([_btn(T(uid, 'back_p2p'), 'p2p:home')])
        await event.edit(text, buttons=btns)
        return

    if data.startswith('p2p:ocancel:'):
        oid = int(data.split(':')[2])
        o = get_order(oid)
        if o and o['maker'] == uid and o['status'] == 'open':
            _exec("UPDATE orders SET status='canceled' WHERE id=?", (oid,))
            await event.answer('⛔️')
            await channel_status(oid, '⛔️ لغو شد', delete=True)
            await on_callback(client, event, 'p2p:mine')
        else:
            await event.answer('⚠️')
        return

    if data.startswith('p2p:take:'):
        await event.answer()
        if live_trade_of(uid):
            await event.answer('⚠️')
            try:
                await event.edit(T(uid, 'busy_trade'), buttons=[
                    [_btn(f'{T(uid, "active_title")}', 'p2p:active')],
                    [_btn(T(uid, 'back_p2p'), 'p2p:home')]])
            except Exception:
                pass
            return
        oid = int(data.split(':')[2])
        o = get_order(oid)
        if not o or o['status'] != 'open':
            await event.edit(T(uid, 'order_gone'))
            return
        if o['maker'] == uid:
            await event.answer('⚠️')
            return
        method = f'\n💳 {T(uid, "method_l")}: {o["method"]}' if o.get('method') else ''
        text = (f'{T(uid, "take_confirm")}\n\n'
                f'{side_word(uid, o["side"])}\n'
                f'{T(uid, "amount_l")}: {fmt(o["sats"])} sats\n'
                f'{T(uid, "price_l")}: {o["price"]}{method}\n'
                f'{maker_stats_line(uid, o["maker"])}')
        await event.edit(text, buttons=[
            [_btn(T(uid, 'take_yes'), f'p2p:takego:{oid}'),
             _btn('«', 'p2p:market')]])
        return

    if data.startswith('p2p:takego:'):
        await event.answer()
        oid = int(data.split(':')[2])
        tid = await take_order(client, uid, oid)
        if tid is None:
            await event.edit(T(uid, 'order_gone'))
            return
        await event.answer('🤝')
        return

    if data.startswith('p2p:room:'):
        await event.answer()
        tid = int(data.split(':')[2])
        await _open_room(client, uid, tid)
        return

    if data.startswith('p2p:lock:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr:
            return
        await _edit_event_room(event, uid, tr, confirm='lock')
        return

    if data.startswith('p2p:lockgo:'):
        await event.answer()
        tid = int(data.split(':')[2])
        try:
            await event.edit(T(uid, 'busy_locking'), buttons=None)
        except Exception:
            pass
        asyncio.get_event_loop().create_task(do_lock(client, uid, tid))
        return

    if data.startswith('p2p:fiat:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr:
            return
        await _edit_event_room(event, uid, tr, confirm='fiat')
        return

    if data.startswith('p2p:fiatgo:'):
        await event.answer()
        tid = int(data.split(':')[2])
        try:
            await event.edit(T(uid, 'busy_fiating'), buttons=None)
        except Exception:
            pass
        await do_fiat(client, uid, tid)
        return

    if data.startswith('p2p:rel:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr:
            return
        await _edit_event_room(event, uid, tr, confirm='rel')
        return

    if data.startswith('p2p:relgo:'):
        await event.answer()
        tid = int(data.split(':')[2])
        try:
            await event.edit(T(uid, 'st_payout'), buttons=None)
        except Exception:
            pass
        asyncio.get_event_loop().create_task(do_release(client, uid, tid))
        return

    if data.startswith('p2p:cx:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr:
            return
        await _edit_event_room(event, uid, tr, confirm='cx')
        return

    if data.startswith('p2p:cxgo:'):
        await event.answer()
        await request_cancel(client, uid, int(data.split(':')[2]))
        return

    if data.startswith('p2p:dsp:'):
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr:
            return
        await _edit_event_room(event, uid, tr, confirm='dispute')
        return

    if data.startswith('p2p:dspgo:'):
        await event.answer()
        tid = int(data.split(':')[2])
        _wiz[f'dsp:{uid}'] = tid  # capture optional note as next text
        # in-place: the confirm row becomes the note prompt — no leftover buttons
        await event.edit(T(uid, 'dispute_note'),
                         buttons=[[_btn('«', f'p2p:room:{tid}')]])
        return

    # ---- پنل ادمین ----
    if data.startswith('p2p:admcase:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        await admin_case_view(client, uid, int(data.split(':')[2]), edit_event=event)
        return
    if data.startswith('p2p:admchat:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        tid = int(data.split(':')[2])
        await event.edit(chat_summary(tid, 60),
                         buttons=[[_btn('« پرونده', f'p2p:admcase:{tid}')]])
        return
    if data.startswith('p2p:adm:unban:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        tuid = int(data.split(':')[3])
        unban_user(tuid)
        try:
            await notify(client, tuid, '♻️ رفع بن شدی — به بخش P2P خوش برگشتی.')
        except Exception:
            pass
        await admin_banlist(client, uid, edit_event=event)
        return
    if data.startswith('p2p:adm:admdel:'):
        if uid != admin_id():
            await event.answer('⛔️ فقط مالک', alert=True); return
        await event.answer()
        _exec('DELETE FROM p2p_admins WHERE uid=?', (int(data.split(':')[3]),))
        await admin_admins(client, uid, edit_event=event)
        return
    if data == 'p2p:adm':
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        await admin_panel(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:disputes':
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        await admin_disputes(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:ban':
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        _wiz[f'adm:{uid}'] = {'m': 'ban', 'ts': time.time()}
        await event.edit('🚫 آیدی عددی کاربر رو بفرست (دلیل رو هم با فاصله بنویس):\n\nمثال: 123456789 کلاهبردار',
                         buttons=[[_btn('« پنل', 'p2p:adm')]])
        return
    if data == 'p2p:adm:banlist':
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        await admin_banlist(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:admins':
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        await admin_admins(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:admadd':
        if uid != admin_id():
            await event.answer('⛔️ فقط مالک می‌تونه ادمین اضافه کنه', alert=True); return
        await event.answer()
        _wiz[f'adm:{uid}'] = {'m': 'admadd', 'ts': time.time()}
        await event.edit('➕ آیدی عددی ادمین جدید رو بفرست:',
                         buttons=[[_btn('« پنل', 'p2p:adm')]])
        return
    if data.startswith('p2p:adm:banok:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        st = _wiz.get(f'adm:{uid}')
        if (not isinstance(st, dict) or st.get('m') != 'ban2'
                or time.time() - (st.get('ts') or 0) > 900):
            await event.answer('⏳ منقضی شد — دوباره از پنل بن کن.', alert=True); return
        target = int(data.split(':')[3])
        if target != st.get('target'):
            await event.answer('⛔️ ناسازگار — دوباره از پنل بن کن.', alert=True); return
        await event.answer()
        _wiz.pop(f'adm:{uid}', None)
        reason = st.get('reason', '')
        ban_user(target, reason)
        try:
            await notify(client, target, '🚫 دسترسی‌ات به بخش P2P مسدود شد.')
        except Exception:
            pass
        await audit(client, 0, 'P2P BAN', f'{target} — {reason or "—"} (by {uid})')
        await admin_banlist(client, uid, edit_event=event)
        return
    if data.startswith('p2p:adm:cxlive:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True); return
        await event.answer()
        tid = int(data.split(':')[3])
        tr = get_trade(tid)
        if tr and tr['stage'] == ST_LOCKING:
            _exec("UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=?",
                  (ST_CANCELED, time.time(), tid, ST_LOCKING))
            _exec("UPDATE orders SET status='open', taken_at=NULL WHERE id=? AND status='taken'",
                  (tr['order_id'],))
            for party in (tr['buyer'], tr['seller']):
                await notify(client, party, T(party, 'cx_solo'))
            await channel_status(tr['order_id'], '⛔️ لغو شد', delete=True)
            await update_room(client, tid)
            await audit(client, 0, 'P2P ADMIN CANCEL', f'trade #{tid} (by {uid})')
            await event.respond(f'✅ معاوضه #{tid} لغو شد و سفارش برگشت بازار.')
        else:
            await event.respond('⚠️ فقط معاوضه‌ی قفل‌نشده از پنل لغو می‌شه؛ پول‌دارها از پرونده‌ی اختلاف.')
        return
    if data.startswith('p2p:admrel:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True)
            return
        await event.answer()
        tid = int(data.split(':')[2])
        await admin_release(client, tid)
        await notify(client, uid, f'✅ p2p #{tid} آزادسازی شد')
        return

    if data.startswith('p2p:admref:'):
        if not is_p2p_admin(uid):
            await event.answer('⛔️ فقط ادمین', alert=True)
            return
        await event.answer()
        tid = int(data.split(':')[2])
        await admin_refund(client, tid)
        await notify(client, uid, f'↩️ p2p #{tid} برگشت خورد')
        return

    await event.answer()


async def _edit_event_room(event, uid, tr, confirm):
    await event.edit(room_text(uid, tr), buttons=room_buttons(uid, tr, confirm=confirm))


# ==================== routing: text (wizard + anonymous chat) ====================

async def on_text(client, event, text):
    """Returns True when the p2p section consumed this text."""
    uid = event.sender_id
    init_db()
    if not enabled_for(uid):
        return False

    # wizard capture
    w = _wiz.get(uid)
    if w and isinstance(w, dict) and w.get('step') in ('amount', 'price', 'sats_price', 'method', 'ttl', 'fiat_amount', 'premium', 'unit', 'pmode'):
        # ttl is button-only; free text in that step falls through to chat/wallet
        result = await wiz_text(client, event, uid, text)
        if result and isinstance(_wiz.get(uid), dict):
            _wiz[uid] = w          # persist mutations back to the shelf
        return result

    # dispute note capture
    key = f'dsp:{uid}'
    if key in _wiz:
        tid = _wiz.pop(key)
        await open_dispute(client, uid, tid, reason=text)
        await event.respond(T(uid, 'dispute_open'))
        return True

    # admin panel input capture (ban uid / add admin)
    akey = f'adm:{uid}'
    if akey in _wiz:
        st = _wiz.pop(akey)
        mode = st.get('m') if isinstance(st, dict) else None
        if not is_p2p_admin(uid) or not mode or time.time() - (st.get('ts') or 0) > 900:
            return True
        if mode == 'admadd' and uid != admin_id():
            await event.respond('⛔️ فقط مالک می‌تونه ادمین اضافه کنه.')
            return True
        parts = text.split(None, 1)
        try:
            target = int(parts[0].strip().lstrip('@'))
        except Exception:
            await event.respond('⚠️ آیدی عددی معتبر نیست. مثال: 123456789')
            _wiz[akey] = {'m': mode, 'ts': time.time()}
            return True
        reason = parts[1].strip() if len(parts) > 1 else ''
        if mode == 'ban':
            if target == uid:
                await event.respond('😅 خودت رو نمی‌شه بن کرد.')
                return True
            _wiz[akey] = {'m': 'ban2', 'target': target, 'reason': reason, 'ts': time.time()}
            lines = ['🚫 بن این کاربر رو تأیید می‌کنی؟', '', f'👤 {target}']
            if reason:
                lines.append(f'📝 {reason}')
            kb = [[_btn('✅ بن کن', f'p2p:adm:banok:{target}'), _btn('❌ انصراف', 'p2p:adm')]]
            lt = live_trade_of(target)
            if lt:
                lines.append(f'\n⚠️ معاوضه‌ی فعال داره: #{lt["id"]} ({lt["stage"]})')
                if lt['stage'] == ST_LOCKING:
                    lines.append('هنوز قفل نشده — می‌تونی لغوش کنی:')
                    kb.insert(1, [_btn(f'🗑 لغو معاوضه #{lt["id"]}', f'p2p:adm:cxlive:{lt["id"]}')])
                else:
                    lines.append('پول تو بازیه — بعد از بن، از پرونده‌ی اختلاف حلش کن.')
            await event.respond('\n'.join(lines), buttons=kb)
            return True
        elif mode == 'admadd':
            if is_banned(target):
                await event.respond('⛔️ این کاربر بنه — اول رفع بنش کن بعد ادمینش کن.')
                return True
            _exec('INSERT OR REPLACE INTO p2p_admins (uid, added_by, at) VALUES (?,?,?)',
                  (target, uid, time.time()))
            try:
                await notify(client, target, '🛡 تو ادمین داوری P2P شدی — گزینه‌ی «🛡 ادمین» تو بخش P2P برات فعاله.')
            except Exception:
                pass
            await event.respond(f'✅ {target} ادمین داوری شد.')
            await audit(client, 0, 'P2P ADMIN ADD', f'{target} (by {uid})')
        return True

    # anonymous trade chat — only when the chat toggle is ON for this trade
    tr = active_trade_of(uid)
    if tr and tr.get('chat_open') and tr['stage'] in (ST_LOCKING, ST_LOCKED, ST_FIAT, ST_DISPUTED, ST_PAYOUT, ST_REFUND, ST_DONE, ST_REFUNDED):
        # don't trap payment inputs — let the wallet's send flow take them
        try:
            b = _bot()
            if b._looks_like_payment_input(text):
                return False
        except Exception:
            pass
        other = tr['buyer'] if uid == tr['seller'] else tr['seller']
        try:
            _exec('INSERT INTO chat_msgs (tid, uid, text, at) VALUES (?,?,?,?)',
                  (tr['id'], uid, text[:500], time.time()))
        except Exception:
            pass
        try:
            await client.send_message(other, f'💬 #{tr["id"]}\n{text}')
            await event.respond('✅ ارسال شد')
        except Exception as e:
            logging.error(f'p2p chat relay {uid}: {e}')
            await event.respond('⚠️ ارسال نشد — دوباره امتحان کن')
        return True
    return False


async def on_photo(client, event):
    """Relay a photo through the anonymous trade chat (receipts etc.).
    Returns True when consumed."""
    uid = event.sender_id
    init_db()
    if not enabled_for(uid):
        return False
    tr = active_trade_of(uid)
    if not tr or not tr.get('chat_open') or tr['stage'] not in (ST_LOCKING, ST_LOCKED, ST_FIAT, ST_DISPUTED, ST_PAYOUT, ST_REFUND, ST_DONE, ST_REFUNDED):
        return False
    other = tr['buyer'] if uid == tr['seller'] else tr['seller']
    try:
        _exec('INSERT INTO chat_msgs (tid, uid, text, at) VALUES (?,?,?,?)',
              (tr['id'], uid, '[📷 عکس]', time.time()))
    except Exception:
        pass
    try:
        await client.send_file(other, event.photo, caption=f'💬 #{tr["id"]}')
        await event.respond('✅')
        return True
    except Exception as e:
        logging.error(f'p2p photo relay {uid}: {e}')
        try:
            await event.respond('⚠️')
        except Exception:
            pass
        return True


# ==================== main-menu hook ====================

async def _open_room(client, uid, tid):
    """Open (or re-open) the live trade room for one party.
    Always sends a FRESH message and deletes the stale one — keeps chat clean."""
    tr = get_trade(tid)
    if not tr or uid not in (tr['buyer'], tr['seller']):
        return
    panels = json.loads(tr.get('panels') or '{}')
    if str(uid) in panels:
        chat_id, msg_id = panels[str(uid)]
        try:
            await client.delete_messages(chat_id, msg_id)
        except Exception:
            pass
        await _del_pin_note(client, chat_id, msg_id)
    try:
        msg = await _send_panel(client, uid, room_text(uid, tr),
                                room_buttons(uid, tr))
        panels[str(uid)] = [msg.chat_id, msg.id]
        _exec('UPDATE trades SET panels=? WHERE id=?', (json.dumps(panels), tid))
    except Exception as e:
        logging.error(f'p2p room open {uid}: {e}')


def active_trade_count(uid):
    try:
        row = _one('SELECT COUNT(*) AS c FROM trades WHERE (buyer=? OR seller=?) '
                   'AND stage IN (?,?,?,?,?,?)',
                   (uid, uid, ST_LOCKING, ST_LOCKED, ST_FIAT, ST_PAYOUT,
                    ST_DISPUTED, ST_REFUND))
        return row['c'] if row else 0
    except Exception:
        return 0


def main_menu_button(uid):
    """Returns a button row for the bot's main menu, or None when hidden.
    Shows an in-progress badge that jumps straight back into the trade room."""
    if not enabled_for(uid):
        return None
    try:
        from zap_i18n import get_user_lang
        lang = get_user_lang(uid)
    except Exception:
        lang = 'en'
    n = active_trade_count(uid)
    if n > 0:
        badge = f'({n} در جریان)' if lang == 'fa' else f'({n} active)'
        return _btn(f'🤝 P2P {badge}', 'p2p')
    return _btn('🤝 P2P', 'p2p')

async def deep_link_take(client, uid, oid):
    """Channel deep link → same path as pressing Take inside the bot."""
    init_db()
    o = get_order(oid)
    if not o or o['status'] != 'open':
        await _send_panel(client, uid, T(uid, 'order_gone'),
                          [[_btn(T(uid, 'back_p2p'), 'p2p:home')]])
        return
    if o['maker'] == uid:
        await _send_panel(client, uid, T(uid, 'own_order_note'),
                          [[_btn(T(uid, 'back_p2p'), 'p2p:home')]])
        return
    if live_trade_of(uid):
        await _send_panel(client, uid, T(uid, 'busy_trade'),
                          [[_btn(T(uid, 'active_title'), 'p2p:active')],
                           [_btn(T(uid, 'back_p2p'), 'p2p:home')]])
        return
    method = f'\n{T(uid, "method_l")}: {o["method"]}' if o.get('method') else ''
    text = (f'{T(uid, "take_confirm")}\n\n'
            f'{side_word(uid, o["side"])}\n'
            f'{T(uid, "amount_l")}: {fmt(o["sats"])} sats\n'
            f'{T(uid, "price_l")}: {o["price"]}{method}\n'
            f'{maker_stats_line(uid, o["maker"])}')
    await _send_panel(client, uid, text, buttons=[
        [_btn(T(uid, 'take_yes'), f'p2p:takego:{oid}')],
        [_btn(T(uid, 'back_p2p'), 'p2p:home')]])
