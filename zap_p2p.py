"""P2P exchange section for Zap_lnBot (public or P2P_TEST_USERS allowlist).

Design (owner-approved v3):
- Escrow: dedicated wallet, seed path from P2P_ESCROW_SEED_PATH (chmod 600),
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
import secrets
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
ESCROW_SEED_PATH = os.getenv('P2P_ESCROW_SEED_PATH') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.p2p_escrow_seed')
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
ST_INVALIDATED = 'invalidated'
ST_REFUNDED = 'refunded'
ST_DISPUTED = 'disputed'
ST_CANCELED = 'canceled'

_db_lock = threading.RLock()
_db = None
_escrow = None
_escrow_lock = asyncio.Lock()
_last_panel = {}   # uid -> (chat_id, msg_id) last buttoned p2p panel
P2P_LIST_CHANNEL = int(os.getenv('P2P_LIST_CHANNEL', '0') or 0)   # «Zap P2P list» public order channel
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
    fee_paid INTEGER DEFAULT 0, fee_tries INTEGER DEFAULT 0, fee_safe INTEGER DEFAULT 0,
    dispute_reason TEXT,
    chat_open INTEGER DEFAULT 0, chat_off_seller INTEGER DEFAULT 0,
    chat_off_buyer INTEGER DEFAULT 0, premium REAL,
    lock_sent INTEGER DEFAULT 0, lock_pay_id TEXT, escrow_verified INTEGER DEFAULT 0,
    safety_version INTEGER DEFAULT 0, locked_at REAL, lock_amount INTEGER);
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
CREATE TABLE IF NOT EXISTS escrow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id TEXT UNIQUE,
    direction TEXT NOT NULL, amount TEXT NOT NULL, status TEXT NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    reviewed_by INTEGER, reviewed_at REAL, review_note TEXT);
CREATE TABLE IF NOT EXISTS escrow_observations (
    event_id INTEGER NOT NULL, direction TEXT NOT NULL, amount TEXT NOT NULL,
    status TEXT NOT NULL, observed_at REAL NOT NULL,
    UNIQUE(event_id,direction,amount,status));
CREATE TABLE IF NOT EXISTS escrow_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
    direction TEXT NOT NULL, amount TEXT NOT NULL, status TEXT NOT NULL,
    sent_at REAL, UNIQUE(event_id,direction,amount,status));
CREATE TABLE IF NOT EXISTS escrow_reviews (
    event_id INTEGER NOT NULL, uid INTEGER NOT NULL, reviewed_at REAL NOT NULL,
    note TEXT NOT NULL, UNIQUE(event_id,reviewed_at));
CREATE TABLE IF NOT EXISTS escrow_adjudications (
    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, kind TEXT NOT NULL,
    event_id INTEGER NOT NULL UNIQUE, payment_id TEXT NOT NULL UNIQUE,
    direction TEXT NOT NULL, amount TEXT NOT NULL, status TEXT NOT NULL,
    admin_uid INTEGER NOT NULL, adjudicated_at REAL NOT NULL,
    UNIQUE(trade_id,kind));
CREATE TABLE IF NOT EXISTS escrow_admin_actions (
    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, kind TEXT NOT NULL,
    admin_uid INTEGER NOT NULL, acted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS escrow_fee_attempts (
    trade_id INTEGER PRIMARY KEY, admin_uid INTEGER NOT NULL, claimed_at REAL NOT NULL,
    amount INTEGER NOT NULL, payment_id TEXT, bound_at REAL);
CREATE TABLE IF NOT EXISTS escrow_confirmations (
    nonce TEXT PRIMARY KEY, uid INTEGER NOT NULL, trade_id INTEGER NOT NULL,
    action TEXT NOT NULL, expires REAL NOT NULL, consumed_at REAL);
CREATE TABLE IF NOT EXISTS escrow_trade_notices (
    trade_id INTEGER NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
    sent_at REAL, PRIMARY KEY(trade_id,kind,target));
''')
        # Import previous reviews and unresolved alerts without losing history.
        db.execute("INSERT OR IGNORE INTO escrow_reviews SELECT id,reviewed_by,reviewed_at,review_note FROM escrow_events WHERE reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL AND review_note IS NOT NULL")
        db.execute("INSERT OR IGNORE INTO escrow_alerts(event_id,direction,amount,status) SELECT o.event_id,o.direction,o.amount,o.status FROM escrow_observations o JOIN escrow_events e ON e.id=o.event_id WHERE o.direction='RECEIVE' OR o.status IN ('FAILED','ID_REUSED') OR e.conflict=1")
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
                         ('premium', 'REAL'),
                         ('lock_sent', 'INTEGER DEFAULT 0'), ('lock_pay_id', 'TEXT'),
                         ('fee_safe', 'INTEGER DEFAULT 0'), ('escrow_verified', 'INTEGER DEFAULT 0'),
                         ('safety_version', 'INTEGER DEFAULT 0'), ('locked_at', 'REAL'),
                         ('lock_amount', 'INTEGER')):
            if col not in tcols:
                db.execute(f'ALTER TABLE trades ADD COLUMN {col} {typ}')
        # Old locking rows may have sent money before lock_sent existed.  The
        # version is explicit on creation, so a crash/restart cannot reclassify
        # a new trade as old; repeating this migration is harmless.
        db.execute("UPDATE trades SET lock_sent=1 WHERE stage=? AND COALESCE(safety_version,0)=0", (ST_LOCKING,))
        db.execute("UPDATE trades SET locked_at=COALESCE(updated,created) WHERE stage=? AND locked_at IS NULL", (ST_LOCKED,))
        # Recover already-conflicted terminal records from older deployments.
        db.execute('BEGIN IMMEDIATE')
        try:
            for event in db.execute('SELECT id FROM escrow_events WHERE conflict=1').fetchall():
                _invalidate_event_trades(db, event['id'])
            db.commit()
        except Exception:
            db.rollback()
            raise


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

    The service executes zap_wallet_bot_v32.py as __main__; a plain
    `import zap_wallet_bot_v32` here creates a SECOND module instance with
    cipher_suite=None (seeds fail to decrypt → "invalid word count: 1") and an
    empty active_user_connections. Always resolve the live __main__ first.
    """
    import sys
    m = sys.modules.get('__main__')
    if m is not None and hasattr(m, 'get_wallet'):
        return m
    import zap_wallet_bot_v32
    return zap_wallet_bot_v32


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
  'holds_title': 'عملیات متوقف‌شده: سوابق کیف پول را بررسی کنید؛ نتیجه نامعلوم را دوباره ارسال نکنید.',
  'events_title': 'رویدادهای بدون انتساب یا متناقض؛ بررسی وجهی جابه‌جا نمی‌کند.',
  'event_missing': 'رویداد پیدا نشد.', 'event_detail': 'رویداد #{id}\nشناسه پرداخت: {pid}\n{direction} {amount} · {status}\nتناقض: {conflict}\nمشاهدات:\n{evidence}\nبررسی: {review}\nهویت اثبات نشده؛ بررسی به معنی انتساب یا انتقال وجه نیست.',
  'not_reviewed': 'بررسی نشده', 'record_evidence': 'ثبت مدرک', 'events_back': '« رویدادها',
  'event_review_prompt': 'مدرک را بدون اطلاعات محرمانه بفرستید. فقط بررسی ثبت می‌شود و پولی جابه‌جا نمی‌شود.',
  'event_review_saved': 'مدرک ثبت شد؛ رویداد بدون انتساب می‌ماند و پولی جابه‌جا نشده.',
  'event_review_failed': 'بررسی ثبت نشد: مدرک خالی، قبلاً بررسی‌شده یا دسترسی نامعتبر.',
  'holds_panel': 'عملیات متوقف', 'events_panel': 'بررسی رویدادهای اسکرو',
  'next': 'بعدی', 'admin_back': '« پنل', 'holds_back': '« موارد متوقف', 'cancel_admin': 'انصراف',
  'admin_only': 'فقط ادمین', 'trade_missing': 'معامله پیدا نشد.', 'unknown': 'نامعلوم',
  'hold_detail': 'معامله #{id} · {stage}\nخریدار: {buyer} · فروشنده: {seller}\nشناسه قفل: {lock}\nشناسه پرداخت: {payout}\nشناسه بازگشت: {refund}\nکارمزد: {fee} · تلاش‌ها: {tries} · شناسه: {fee_id}\nنتیجه نامعلوم را نگه دارید؛ نبود شناسه اثبات عدم پرداخت نیست.',
  'send_fee': 'ارسال کارمزد (یک تلاش)',
  'mark_settled_payout': 'ثبت تسویه پرداخت (نیازمند مدرک)',
  'mark_settled_refund': 'ثبت تسویه بازگشت (نیازمند مدرک)',
  'mark_settled_fee': 'ثبت تسویه کارمزد (نیازمند مدرک)',
  'hold_conflict_title': '\n⚠️ مدارک متناقض:\n{lines}',
  'hold_open_case': '⚖️ پرونده اختلاف',
  'confirm_fee_text': 'ارسال یک‌باره {fee} ساتوشی کارمزد به مالک را تأیید کنید. نتیجه نامعلوم قابل تکرار نیست.',
  'confirm_fee': 'تأیید ارسال کارمزد', 'confirmation_invalid': 'تأیید نامعتبر یا منقضی است.',
  'legacy_warning': 'معامله قدیمی است. قفل و ارسال‌های قبلی را مستقل بررسی کنید. تأیید ممکن است پول ارسال کند.',
  'legacy_release': 'تأیید پرداخت قدیمی', 'legacy_refund': 'تأیید بازگشت قدیمی',
  'legacy_none': 'عملیات قدیمی موجود نیست.', 'legacy_recorded': 'تأیید ثبت شد؛ وضعیت معامله را بررسی کنید.',
  'admin_check_status': 'ℹ️ p2p #{id}: وضعیت معامله را بررسی کنید؛ پرداخت‌های در انتظار یا نامعلوم نیازمند رسیدگی‌اند.',
  'result_settled': 'تسویه ثبت شد.', 'result_already_settled': 'قبلاً تسویه ثبت شده.', 'result_invalidated': 'معامله به دلیل تناقض نامعتبر شد.',
  'result_insufficient_evidence': 'مدرک کافی نیست.', 'result_not_authorized': 'دسترسی ندارید.', 'result_held': 'متوقف یا قبلاً تلاش شده؛ بررسی لازم است.',
  'admin_panel_btn': '🛡 پنل ادمین',
  'adm_panel_title': '🛡 پنل ادمین P2P\n\n📊 آمار:',
  'adm_st_trades': '• معاوضه‌ها: {trades} (تموم‌شده: {done})',
  'adm_st_users': '• کاربرای معامله‌کننده: {users}',
  'adm_st_fees': '• کارمزدهای جمع‌شده: {fees} ساتوشی',
  'adm_st_orders': '• سفارش‌های باز بازار: {orders}',
  'adm_st_disputes': '• اختلاف‌های باز: {disputes}',
  'adm_st_mod': '• بن‌شده‌ها: {banned} · ادمین‌ها: {admins}',
  'adm_attention_title': '🚨 نیازمند رسیدگی:',
  'adm_attn_disputes': '⚖️ {n} اختلاف باز',
  'adm_attn_holds': '🛑 {n} عملیات متوقف',
  'adm_attn_events': '🔍 {n} رویداد در انتظار بررسی',
  'adm_disputes_btn': '⚖️ اختلاف‌های باز', 'adm_disputes_empty': '✅ اختلاف بازی وجود نداره.',
  'adm_disputes_title': '⚖️ اختلاف‌های باز ({n}):',
  'adm_dispute_card': '#{id} · {sats} ساتوشی · {price}\n🛒 {buyer} / 💸 {seller} · ⏳ {age}',
  'adm_loading_case': '⏳ در حال باز کردن پرونده…',
  'age_mins': '{n} دقیقه', 'age_hours': '{n} ساعت', 'age_days': '{n} روز',
  'adm_ban_btn': '🚫 بن کاربر', 'adm_banlist_btn': '📜 بن‌شده‌ها',
  'adm_banlist_title': '🚫 بن‌شده‌ها ({n}):', 'adm_banlist_empty': '🚫 هیچ‌کی بن نشده.',
  'adm_unban_btn': '♻️ رفع بن {target}', 'adm_unban_notify': '♻️ رفع بن شدی — به بخش P2P خوش برگشتی.',
  'adm_ban_notify': '🚫 دسترسی‌ات به بخش P2P مسدود شد.',
  'adm_admins_btn': '👥 ادمین‌های داوری', 'adm_admins_title': '👥 ادمین‌های داوری:',
  'adm_owner_tag': ' (مالک)', 'adm_add_btn': '➕ افزودن ادمین', 'adm_del_btn': '❌ حذف {target}',
  'adm_owner_only': '⛔️ فقط مالک', 'adm_owner_only_add': '⛔️ فقط مالک می‌تونه ادمین اضافه کنه.',
  'adm_add_prompt': '➕ آیدی عددی ادمین جدید رو بفرست:', 'adm_added': '✅ {target} ادمین داوری شد.',
  'adm_added_notify': '🛡 تو ادمین داوری P2P شدی — گزینه‌ی «🛡 پنل ادمین» تو بخش P2P برات فعاله.',
  'adm_banned_target': '⛔️ این کاربر بنه — اول رفع بنش کن بعد ادمینش کن.',
  'adm_ban_prompt': '🚫 آیدی عددی کاربر رو بفرست (دلیل رو هم با فاصله بنویس):\n\nمثال: 123456789 کلاهبردار',
  'adm_invalid_uid': '⚠️ آیدی عددی معتبر نیست. مثال: 123456789',
  'adm_ban_self': '😅 خودت رو نمی‌شه بن کرد.',
  'adm_ban_confirm': '🚫 بن این کاربر رو تأیید می‌کنی؟', 'adm_ban_user': '👤 {target}',
  'adm_ban_reason': '📝 {reason}', 'adm_ban_yes': '✅ بن کن', 'adm_ban_no': '❌ انصراف',
  'adm_ban_active': '\n⚠️ معاوضه‌ی فعال داره: #{id} ({stage})',
  'adm_ban_locking_hint': 'هنوز قفل نشده — می‌تونی لغوش کنی:',
  'adm_ban_money_hint': 'پول تو بازیه — بعد از بن، از پرونده‌ی اختلاف حلش کن.',
  'adm_cx_btn': '🗑 لغو معاوضه #{id}',
  'adm_ban_expired': '⏳ منقضی شد — دوباره از پنل بن کن.',
  'adm_ban_mismatch': '⛔️ ناسازگار — دوباره از پنل بن کن.',
  'adm_cx_done': '✅ معاوضه #{id} لغو شد و سفارش برگشت بازار.',
  'adm_cx_inflight': '⚠️ قفل در جریانه؛ رسیدگی دستی لازم است.',
  'adm_cx_only_locking': '⚠️ فقط معاوضه‌ی قفل‌نشده از پنل لغو می‌شه؛ پول‌دارها از پرونده‌ی اختلاف.',
  'adm_case_missing': 'پرونده پیدا نشد.',
  'adm_case_text': '⚖️ پرونده p2p #{id}{auto}\n\n🛒 خریدار: {buyer}\n\n💸 فروشنده: {seller}\n\n💰 {sats} ساتوشی (+{fee} کارمزد)\n🏷 {price}\n⏳ سن: {age}\n📌 مرحله: {stage}{reason}',
  'adm_case_view_text': '⚖️ پرونده #{id}\n\n🛒 خریدار: {buyer}\n💸 فروشنده: {seller}\n\n💰 {sats} ساتوشی (+{fee} کارمزد)\n🏷 {price}\n⏳ سن: {age}\n📌 مرحله: {stage}{reason}\n\n{chat}',
  'adm_auto': '\n⏰ خودکار', 'adm_reason': '\n📝 {reason}',
  'adm_stage_locking': 'در حال قفل', 'adm_stage_locked': 'قفل',
  'adm_stage_fiat': 'اعلام واریز', 'adm_stage_disputed': 'اختلاف',
  'adm_stage_payout': 'در حال پرداخت', 'adm_stage_refunding': 'در حال بازگشت',
  'adm_stage_done': 'تمام‌شده', 'adm_stage_refunded': 'برگشت داده شد',
  'adm_stage_canceled': 'لغو شده', 'adm_stage_invalidated': 'نامعتبر',
  'adm_release_btn': '✅ آزادسازی', 'adm_refund_btn': '↩️ برگشت',
  'adm_chat_btn': '💬 خلاصه چت', 'adm_chat_full_btn': '💬 چت کامل', 'adm_case_back': '« پرونده',
  'adm_chat_title': '📋 چت معاوضه #{id} — {n} پیام اخیر:',
  'adm_chat_empty': '💬 بین طرفین پیامی رد و بدل نشده (یا چت خاموش بوده).',
  'adm_chat_seller': '💸 فروشنده', 'adm_chat_buyer': '🛒 خریدار',
  'adm_buyer_unreachable': '⚠️ p2p #{id}: ولت خریدار در دسترس نیست: {err}',
  'adm_seller_unreachable': '⚠️ p2p #{id}: ولت فروشنده در دسترس نیست: {err}',
  'adm_payout_failed': '⚠️ p2p #{id}: پرداخت ادمین ناموفق بود ({err})',
  'adm_refund_failed': '⚠️ p2p #{id}: بازگشت وجه ناموفق بود ({err})',
  'adm_alert_event': '⚠️ p2p: رویداد اسکرو #{event_id} {direction} {amount} {status} برای بررسی متوقف شد؛ انتساب یا بازگشت خودکار انجام نمی‌شود',
  'trade_notice_payout': 'معامله #{id}: پرداخت با بررسی مدیر ثبت شد.',
  'trade_notice_refund': 'معامله #{id}: بازگشت وجه با بررسی مدیر ثبت شد.',
  'trade_notice_invalidated': 'معامله #{id}: مدارک متناقض است؛ نتیجه نامعتبر و نیازمند بررسی است. پرداخت قبلی قابل برگشت خودکار نیست.',
  'trade_notice_settlement_held': 'معامله #{id}: سند تکمیل تسویه ناقص است؛ تسویه خودکار انجام نشد و نیازمند بررسی مدیر است.',


  'home_title': '🤝 معاوضه ساتوشی',
  'home_body': 'اینجا می‌تونی ساتوشی‌هات رو مستقیم با آدم‌ها معاوضه کنی — بدون واسطه‌ی صرافی.\n\n🔑 چطوریه؟\n• فروشنده سفارش میده و ساتوشی‌هاش رو تو قفل ربات می‌ذاره\n• خریدار پول (تومان، دلار، هر روشی که توافق کردید) رو مستقیم به فروشنده می‌ده\n• فروشنده تأیید می‌کنه و ساتوشی‌ها آزاد می‌شن و میرن ولت خریدار\n\n🛡 اگه اختلاف پیش بیاد، ادمین وسط میاد و تصمیم می‌گیره.',
  'home_fee': '💸 کارمزد: فقط ۰.۵٪ — از فروشنده کسر می‌شه',
  'tos_title': '🤝 توافق‌نامه و شرایط استفاده',
  'tos_body': 'قبل از استفاده از این بخش، این شرایط رو بخون و بپذیر:\n\n1️⃣ معاوضه بین تو و طرف مقابلته. ربات فقط نگه‌دارنده‌ی ساتوشی‌هاست و ضامن قیمت نیست.\n\n2️⃣ خریدار موظفه قبل از واریز، قیمت و بهای این معامله رو خودش دستی بررسی و مقایسه کنه. ربات هیچ نقشی در تعیین قیمت نداره و قیمت ممکنه با بازار فرق داشته باشه.\n\n3️⃣ پرداخت پولی (فیات یا هر روش دیگه) بین خودتونه؛ ربات اون رو تأیید یا تضمین نمی‌کنه.\n\n4️⃣ ساتوشی‌ها تا وقتی فروشنده «دریافت کردم» نزده، تو قفل ربات می‌مونه.\n\n5️⃣ تو اختلاف، تصمیم نهایی با ادمینه.\n\n6️⃣ برای هر معاوضه‌ی موفق ۰.۵٪ کارمزد از فروشنده کسر می‌شه.',
  'market_hint': 'بین سفارش‌ها بگرد 👇 — با ⬅️ ➡️ جلو و عقب برو. سفارش طرف مقابل رو که برداری، معامله شروع می‌شه.',
  'guide_text': '❓ راهنمای معاوضه P2P\n\n📌 می‌خوای بفروشی؟\n1️⃣ «سفارش جدید» رو بزن و مقدار، قیمت و مهلت رو مشخص کن\n2️⃣ خریدار سفارشت رو برداره، ساتوشی + کارمزد ۰.۵٪ رو قفل کن\n3️⃣ خریدار پول رو بهت می‌رسونه و «واریز کردم» می‌زنه\n4️⃣ پول رو که دیدی، «دریافت کردم» بزن — ساتوشی‌ها میرن ولت خریدار\n\n📌 می‌خوای بخری؟\n1️⃣ تو بازار یه سفارش فروش بردار\n2️⃣ فروشنده قفل کرد؟ شماره حسابش رو ازش بگیر و واریز کن\n3️⃣ «واریز کردم» بزن — بعد از تأیید فروشنده، ساتوشی‌ها میاد ولتت\n\n💡 نکته‌ها:\n• تو اتاق معامله می‌تونید بی‌اسم به هم پیام بدید\n• اگه معامله رو نخواستی ادامه بدی، «لغو» دوطرفه می‌شه\n• اختلافی پیش اومد؟ ⚖️ اختلاف رو بزن تا ادمین رسیدگی کنه\n• 💱 ارز معامله (تومان، دلار و…) از ⚙️ تنظیمات ← «ارز مبدا» عوض میشه — نرخ لحظه‌ای هم از همون ارز خونده میشه',
  'wiz_side': 'بیا سفارشت رو بسازیم 👇\n\nمی‌خوای ساتوشی بفروشی یا بخری؟',
  'wiz_amount': '💰 چقدر ساتوشی می‌خوای معاوضه کنی؟\n\nفقط عدد بفرست. مثال: 50000',
  'wiz_price': '🏷 قیمتت رو چطور می‌فروشی/می‌خری؟\n\nهر واحدی راحته — خودت بنویس.\nمثال:\n• 6500000 تومان\n• 5.3$ برای هر 10k ساتوشی',
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
  'lock_sure': '🔒 قراره {amt} ساتوشی از ولتت به قفل ربات بره.\n\nاین پول تا آخر معاوضه قفل می‌مونه:\n• معاوضه خوب پیش بره → میره ولت خریدار\n• لغو دوطرفه یا اختلاف → برمی‌گرده ولت تو\n\nمطمئنی؟',
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
  'lock_fail_bal': '⚠️ موجودیت کافی نیست!\n\nبرای این معاوضه باید {need} ساتوشی داشته باشی (مقدار سفارش + کارمزد ۰.۵٪).\nاول ولتت رو شارژ کن و دوباره امتحان کن.',
  'lock_fail': '⚠️ قفل کردن انجام نشد: {err}\n\nیه بار دیگه امتحان کن. اگه نشد به ادمین خبر بده.',
  'no_action': '⏳ الان نوبت تو نیست — منتظر بمون تا طرف مقابل کارش رو انجام بده.',
  'active_title': '🔄 معاوضه‌های در جریان',
  'active_none': 'هنوز معاوضه‌ای در جریان نداری.',
  'ping_fiat': '💸 خریدار «واریز کردم» رو زد — پول رو چک کن و «دریافت کردم» رو بزن.',
  'ping_release': '🎉 فروشنده آزاد کرد! ساتوشی‌ها داره میره ولتت.',
  'cx_solo': '⛔️ معاوضه لغو شد (قبل از قفل — هیچ پولی قفل نبود). سفارش هم از بازار برداشته شد.',
  'busy_locking': '⏳ دارم قفل می‌کنم… یه چند ثانیه صبر کن.',
  'lock_held': '⚠️ نتیجه قفل مشخص نیست یا مهلتش تمام شده؛ دوباره پرداخت نکن. ممکن است دارایی گیر کند و ابزار بررسی امن هنوز آماده نیست. با ادمین تماس بگیر.',
  'lock_amount_note': '🔐 مبلغ دقیق قفل: {amt} ساتوشی (مقدار + کارمزد + شناسه یکتا)؛ فقط و دقیقاً همین مبلغ قفل می‌شود.',
  'adm_lock_auto': '✅ p2p: قفل معامله #{id} با مبلغ یکتای {amount} ساتوشی به‌صورت خودکار تأیید شد.',
  'fiat_timeout_dispute': '⚖️ مهلت تأیید واریز گذشته؛ پرونده برای بررسی به ادمین رفت. پول خودکار برگشت نمی‌خورد.',
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
  'unit_sats_word': 'ساتوشی',
  'wiz_sats_amount': '🪙 چند ساتوشی؟\n\nفقط عدد بفرست. مثال: 50000',
  'fixed_resolved': '✅ ثبت شد: {sats} ساتوشی در برابر {fiat} تومان ({prem}%)',
  'wiz_sats_price': '🪙 چند ساتوشی می‌خوای در برابر این مبلغ بفروشی؟\n\nفقط عدد بفرست. مثال: 50000',
  'wiz_bad_sats': '⚠️ عدد معتبر نیست. فقط عدد ساتوشی بفرست. مثال: 50000',
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
  # ---- item 23 migrations (fa = today's fa bytes; en follows decision #8) ----
  'timer_lock_wait': '⏱ مهلت قفل ساتوشی: {m} دقیقه و {s} ثانیه باقی‌مانده',
  'timer_lock_expired': '⏰ مهلت قفل تمام شده',
  'timer_locked': '⏱ زمان گذشته از قفل: {em} دقیقه | مهلت واریز خریدار: {rm} دقیقه باقی‌مانده',
  'timer_locked_over': '⏰ مهلت گذشته ({em} دقیقه) — فروشنده می‌تواند درخواست بررسی اختلاف بدهد؛ بازگشت خودکار نیست',
  'timer_fiat': '⏱ زمان گذشته از واریز: {eh} ساعت | مهلت آزادسازی فروشنده: {rh} ساعت باقی‌مانده',
  'card_sell': '💸 فروش ساتوشی',
  'card_buy': '🛒 خرید ساتوشی',
  'rate_market': '📈 قیمت بازار ({src_name}){prem_tag}',
  'amount_sats': '🪙 {amount} ساتوشی',
  'amount_fiat': '💵 {amount} {fiat} {flag}',
  'chat_relay_msg': '💬 #{tid}\n{text}',
  'msg_sent': '✅ ارسال شد',
  'msg_fail': '⚠️ ارسال نشد — دوباره امتحان کن',
  'rate_saved': '⭐ ثبت شد',
  'username_nudge': '⚠️ برای استفاده از بخش P2P یه یوزرنیم تلگرام ست کن (تنظیمات تلگرام → Username).',
  'banned_answer': '🚫 دسترسی‌ات به P2P مسدود شده.',
  'testing_answer': '⚠️ P2P در حال تست است.',
  'expired_channel': '⏳ منقضی شد',
  'expired_notify': '⏳ سفارش #{id} مهلتش تموم شد و از بازار برداشته شد.',
  'refunded_channel': '↩️ برگشت خورد',
  'p2p_btn_active': '({n} در جریان)',
  'ttl_ask': '⏳ سفارش تا کی تو بازار بمونه؟',
  'ttl_12h': '12 ساعت',
  'ttl_24h': '24 ساعت',
  'ttl_2d': '2 روز',
  'ttl_line': '\n⏳ مهلت: {label}',
  # ---- UI/UX pass: card/room literals moved into the pack ----
  'timer_fiat_over': '⏰ مهلت آزادسازی گذشته ({eh} ساعت) — ساتوشی‌ها رو آزاد کن یا درخواست بررسی اختلاف بده',
  'fixed_price_line': '🏷 به قیمت {price} {fiat}',
  'mf_all': 'همه', 'mf_sell': '💸 فروش', 'mf_buy': '🛒 خرید',
  'card_own': '📌 این سفارش مال خودته',
  'take_sell_order': '🛒 خرید ساتوشی', 'take_buy_order': '💸 فروش ساتوشی',
  'stats_word': 'معامله',
  'stats_hist': '👤 کاربر در {days} روز {trades} معامله موفق داشته',
  'prem_custom': '✍️ دستی',
  'deal_sell_for': 'فروش به اندازه\n{amt}',
  'deal_buy_for': 'خرید به اندازه\n{amt}',
  'rate_line': '📈 نرخ: {src}{ptag}',
 },
 'en': {
  'holds_title': 'Held operations: investigate authoritative wallet records. Never resend unknown outcomes.',
  'events_title': 'Unattributed or conflicting events. Review records only; no funds will move.',
  'event_missing': 'Event not found.', 'event_detail': 'Event #{id}\nPayment ID: {pid}\n{direction} {amount} · {status}\nConflict: {conflict}\nObservations:\n{evidence}\nReview: {review}\nIdentity is not proven. Reviewing does not attribute or move funds.',
  'not_reviewed': 'not reviewed', 'record_evidence': 'Record evidence', 'events_back': '« Events',
  'event_review_prompt': 'Send an evidence note (no secrets). This records a review only; no attribution or payment.',
  'event_review_saved': 'Evidence recorded. Event remains unassigned; no funds moved.',
  'event_review_failed': 'Review not recorded: missing note, already reviewed, or unauthorized.',
  'holds_panel': 'Held operations', 'events_panel': 'Review escrow events',
  'next': 'Next', 'admin_back': '« Admin', 'holds_back': '« Holds', 'cancel_admin': 'Cancel',
  'admin_only': 'Admins only', 'trade_missing': 'Trade not found.', 'unknown': 'unknown',
  'hold_detail': 'Trade #{id} · {stage}\nBuyer: {buyer} · Seller: {seller}\nLock ID: {lock}\nPayout ID: {payout}\nRefund ID: {refund}\nFee: {fee} · Attempts: {tries} · Fee ID: {fee_id}\nHold unknown outcomes. A missing ID is not proof of non-payment.',
  'send_fee': 'Send fee (one attempt)',
  'confirm_fee_text': 'Confirm ONE fee send of {fee} sats to the owner. Unknown outcomes cannot be retried.',
  'confirm_fee': 'Confirm fee send', 'confirmation_invalid': 'Confirmation invalid or expired.',
  'legacy_warning': 'Legacy trade: independently verify the lock and prior sends. Confirming may send money.',
  'legacy_release': 'Acknowledge legacy payout', 'legacy_refund': 'Acknowledge legacy refund',
  'legacy_none': 'No legacy action available.', 'legacy_recorded': 'Acknowledgement recorded; check the trade status.',
  'admin_check_status': 'ℹ️ p2p #{id}: check the trade status; pending or uncertain payments require reconciliation.',
  'result_settled': 'Settlement recorded.', 'result_already_settled': 'Already settled.', 'result_invalidated': 'Trade invalidated by conflicting evidence.',
  'result_insufficient_evidence': 'Insufficient evidence.', 'result_not_authorized': 'Not authorized.', 'result_held': 'Held or already attempted; investigation required.',
  'admin_panel_btn': '🛡 Admin panel',
  'adm_panel_title': '🛡 P2P admin panel\n\n📊 Stats:',
  'adm_st_trades': '• Trades: {trades} (completed: {done})',
  'adm_st_users': '• Trading users: {users}',
  'adm_st_fees': '• Collected fees: {fees} sats',
  'adm_st_orders': '• Open market orders: {orders}',
  'adm_st_disputes': '• Open disputes: {disputes}',
  'adm_st_mod': '• Banned: {banned} · Admins: {admins}',
  'adm_attention_title': '🚨 Needs attention:',
  'adm_attn_disputes': '⚖️ {n} open dispute(s)',
  'adm_attn_holds': '🛑 {n} held operation(s)',
  'adm_attn_events': '🔍 {n} event(s) awaiting review',
  'adm_disputes_btn': '⚖️ Open disputes', 'adm_disputes_empty': '✅ No open disputes.',
  'adm_disputes_title': '⚖️ Open disputes ({n}):',
  'adm_dispute_card': '#{id} · {sats} sats · {price}\n🛒 {buyer} / 💸 {seller} · ⏳ {age}',
  'adm_loading_case': '⏳ Opening case…',
  'age_mins': '{n}m', 'age_hours': '{n}h', 'age_days': '{n}d',
  'adm_ban_btn': '🚫 Ban user', 'adm_banlist_btn': '📜 Banned',
  'adm_banlist_title': '🚫 Banned ({n}):', 'adm_banlist_empty': '🚫 Nobody is banned.',
  'adm_unban_btn': '♻️ Unban {target}', 'adm_unban_notify': '♻️ You have been unbanned — welcome back to P2P.',
  'adm_ban_notify': '🚫 Your access to the P2P section has been blocked.',
  'adm_admins_btn': '👥 Adjudication admins', 'adm_admins_title': '👥 Adjudication admins:',
  'adm_owner_tag': ' (owner)', 'adm_add_btn': '➕ Add admin', 'adm_del_btn': '❌ Remove {target}',
  'adm_owner_only': '⛔️ Owner only', 'adm_owner_only_add': '⛔️ Only the owner can add admins.',
  'adm_add_prompt': '➕ Send the numeric ID of the new admin:', 'adm_added': '✅ {target} is now an adjudication admin.',
  'adm_added_notify': '🛡 You are now a P2P adjudication admin — the «🛡 Admin panel» option in the P2P section is enabled for you.',
  'adm_banned_target': '⛔️ This user is banned — unban first, then promote.',
  'adm_ban_prompt': '🚫 Send the user\'s numeric ID (add a reason after a space):\n\nExample: 123456789 scammer',
  'adm_invalid_uid': '⚠️ Not a valid numeric ID. Example: 123456789',
  'adm_ban_self': '😅 You cannot ban yourself.',
  'adm_ban_confirm': '🚫 Confirm banning this user?', 'adm_ban_user': '👤 {target}',
  'adm_ban_reason': '📝 {reason}', 'adm_ban_yes': '✅ Ban', 'adm_ban_no': '❌ Cancel',
  'adm_ban_active': '\n⚠️ Has an active trade: #{id} ({stage})',
  'adm_ban_locking_hint': 'Not locked yet — you can cancel it:',
  'adm_ban_money_hint': 'Funds are in play — after banning, resolve it from the dispute case.',
  'adm_cx_btn': '🗑 Cancel trade #{id}',
  'adm_ban_expired': '⏳ Expired — ban again from the panel.',
  'adm_ban_mismatch': '⛔️ Mismatch — ban again from the panel.',
  'adm_cx_done': '✅ Trade #{id} canceled and the order returned to the market.',
  'adm_cx_inflight': '⚠️ Lock in progress; manual reconciliation required.',
  'adm_cx_only_locking': '⚠️ Only unlocked trades can be canceled from the panel; funded ones go through the dispute case.',
  'adm_case_missing': 'Case not found.',
  'adm_case_text': '⚖️ p2p case #{id}{auto}\n\n🛒 Buyer: {buyer}\n\n💸 Seller: {seller}\n\n💰 {sats} sats (+{fee} fee)\n🏷 {price}\n⏳ Age: {age}\n📌 Stage: {stage}{reason}',
  'adm_case_view_text': '⚖️ Case #{id}\n\n🛒 Buyer: {buyer}\n💸 Seller: {seller}\n\n💰 {sats} sats (+{fee} fee)\n🏷 {price}\n⏳ Age: {age}\n📌 Stage: {stage}{reason}\n\n{chat}',
  'adm_auto': '\n⏰ automatic', 'adm_reason': '\n📝 {reason}',
  'adm_stage_locking': 'locking', 'adm_stage_locked': 'locked',
  'adm_stage_fiat': 'fiat sent', 'adm_stage_disputed': 'disputed',
  'adm_stage_payout': 'paying out', 'adm_stage_refunding': 'refunding',
  'adm_stage_done': 'done', 'adm_stage_refunded': 'refunded',
  'adm_stage_canceled': 'canceled', 'adm_stage_invalidated': 'invalidated',
  'adm_release_btn': '✅ Release', 'adm_refund_btn': '↩️ Refund',
  'adm_chat_btn': '💬 Chat summary', 'adm_chat_full_btn': '💬 Full chat', 'adm_case_back': '« Case',
  'adm_chat_title': '📋 Trade #{id} chat — {n} recent messages:',
  'adm_chat_empty': '💬 No messages were exchanged (or chat was off).',
  'adm_chat_seller': '💸 Seller', 'adm_chat_buyer': '🛒 Buyer',
  'adm_buyer_unreachable': '⚠️ p2p #{id}: buyer wallet unreachable: {err}',
  'adm_seller_unreachable': '⚠️ p2p #{id}: seller wallet unreachable: {err}',
  'adm_payout_failed': '⚠️ p2p #{id}: admin payout failed ({err})',
  'adm_refund_failed': '⚠️ p2p #{id}: refund failed ({err})',
  'adm_alert_event': '⚠️ p2p: escrow event #{event_id} {direction} {amount} {status} held for review; no automatic attribution or refund',
  'mark_settled_payout': 'Mark payout settled (requires evidence)',
  'mark_settled_refund': 'Mark refund settled (requires evidence)',
  'mark_settled_fee': 'Mark fee settled (requires evidence)',
  'hold_conflict_title': '\n⚠️ Conflicting evidence:\n{lines}',
  'hold_open_case': '⚖️ Dispute case',
  'trade_notice_payout': 'Trade #{id}: payout settlement recorded by an administrator.',
  'trade_notice_refund': 'Trade #{id}: refund settlement recorded by an administrator.',
  'trade_notice_invalidated': 'Trade #{id}: conflicting evidence invalidated the outcome; investigation required. A prior payment cannot be automatically reversed.',
  'trade_notice_settlement_held': 'Trade #{id}: settlement evidence incomplete; automatic completion held for admin review.',
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
  'lock_held': '⚠️ Lock outcome unknown or deadline passed. Do not pay again; funds may remain stuck because safe reconciliation is not yet available. Contact an admin.',
  'lock_amount_note': '🔐 Exact lock amount: {amt} sats (amount + fee + unique tag); exactly this amount will be locked.',
  'adm_lock_auto': '✅ p2p: trade #{id} lock auto-verified by its unique amount of {amount} sats.',
  'fiat_timeout_dispute': '⚖️ Fiat confirmation timed out. The case was sent to an admin; there is no automatic refund.',
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
  'unit_sats_word': 'sats',
  'wiz_sats_amount': '🪙 How many sats?\n\nSend just the number. Example: 50000',
  'fixed_resolved': '✅ Set: {sats} sats for {fiat} ({prem}%)',
  'wiz_sats_price': '🪙 How many sats do you want for this amount?\n\nSend just the number. Example: 50000',
  'wiz_bad_sats': '⚠️ Not a valid number. Send the sats amount only. Example: 50000',
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
  # ---- item 23 migrations (single-literal sites: same bytes in both packs) ----
  'timer_lock_wait': '⏱ Lock timeout: {m}m {s}s remaining',
  'timer_lock_expired': '⏰ Lock timeout expired',
  'timer_locked': '⏱ Locked {em}m ago | Buyer fiat timeout in {rm}m',
  'timer_locked_over': '⏰ Deadline passed ({em}m) — seller may request a dispute; no automatic refund',
  'timer_fiat': '⏱ Time since fiat: {eh}h | Release timeout in {rh}h',
  'card_sell': '💸 Selling sats',
  'card_buy': '🛒 Buying sats',
  'rate_market': '📈 Market rate ({src_name}){prem_tag}',
  'amount_sats': '🪙 {amount} sats',
  'amount_fiat': '💵 {amount} {fiat} {flag}',
  'chat_relay_msg': '💬 #{tid}\n{text}',
  # item 23 migrations — en wording restored in the UI/UX pass (decision #8 lifted)
  'msg_sent': '✅ Sent',
  'msg_fail': '⚠️ Could not send — try again',
  'rate_saved': '⭐ Saved',
  'username_nudge': '⚠️ Set a Telegram username to use P2P (Telegram Settings → Username).',
  'banned_answer': '🚫 Your access to P2P is banned.',
  'testing_answer': '⚠️ P2P is in testing mode.',
  'expired_channel': '⏳ Expired',
  'expired_notify': '⏳ Order #{id} timed out and was removed from the market.',
  'refunded_channel': '↩️ Refunded',
  'p2p_btn_active': '({n} active)',
  'ttl_ask': '⏳ How long should the order stay on the market?',
  'ttl_12h': '12h',
  'ttl_24h': '24h',
  'ttl_2d': '2 days',
  'ttl_line': '\n⏳ Expires in: {label}',
  # ---- UI/UX pass: card/room literals moved into the pack ----
  'timer_fiat_over': '⏰ Release deadline passed ({eh}h) — release the sats or open a dispute',
  'fixed_price_line': '🏷 At {price} {fiat}',
  'mf_all': 'All', 'mf_sell': '💸 Sell', 'mf_buy': '🛒 Buy',
  'card_own': '📌 This is your own order',
  'take_sell_order': '🛒 Buy sats', 'take_buy_order': '💸 Sell sats',
  'stats_word': 'trades',
  'stats_hist': '👤 {trades} successful trades in {days} days',
  'prem_custom': '✍️ Custom',
  'deal_sell_for': 'Selling for\n{amt}',
  'deal_buy_for': 'Buying for\n{amt}',
  'rate_line': '📈 Rate: {src}{ptag}',
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


def sats_word(uid):
    """Localized bare unit word for sats amounts (fa: ساتوشی, en: sats)."""
    return T(uid, 'unit_sats_word')


# ==================== escrow wallet ====================

def _payment_is_sats(payment):
    # Generic amount also represents token base units; token/unknown must not
    # be processed as satoshi escrow payments.
    method = str(getattr(payment, 'method', '')).upper()
    return any(name in method for name in ('SPARK', 'LIGHTNING', 'DEPOSIT', 'WITHDRAW')) and 'TOKEN' not in method and 'TOKEN' not in type(getattr(payment, 'details', None)).__name__.upper()


class EscrowListener(EventListener):
    """Escrow wallet's own SDK listener — PAYMENT_SUCCEEDED (RECEIVE) = a lock
    landed; (SEND) = a payout/refund finished."""

    async def on_event(self, event):
        try:
            if not any(name in type(event).__name__ for name in ('PAYMENT_SUCCEEDED', 'PAYMENT_PENDING', 'PAYMENT_FAILED')):
                return
            payment = getattr(event, 'payment', None)
            if payment is None:
                return
            ptype = getattr(payment, 'payment_type', None)
            amount = getattr(payment, 'amount_sats', getattr(payment, 'amount', 0)) or 0
            pid = getattr(payment, 'id', '')
            status = str(getattr(payment, 'status', ''))
            if not _payment_is_sats(payment):
                return
            logging.info(f'p2p escrow event: {type(event).__name__} {ptype} {amount}s {status}')
            # Persist before scheduling: an early event must survive a restart.
            event_id, changed = record_escrow_event(str(ptype), amount, pid, status)
            b = _bot()
            asyncio.run_coroutine_threadsafe(
                on_escrow_payment(str(ptype), amount, pid, status, (event_id, changed)),
                b.main_event_loop)
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


async def send_sats_from_escrow(dest_address, sats, log_tag, gate=None):
    """Zero-fee internal spark send from the escrow wallet. Returns (ok, payment_id, err).
    Runs off-loop — a slow send must never freeze buttons/chat.
    ``gate``: optional callable returning False to abort right before the SDK send
    (e.g. trade invalidated by a conflicting event during preparation)."""
    async def _impl():
        b = _bot()
        sdk = await escrow_sdk()
        prep = await sdk.prepare_send_payment(request=b.PrepareSendPaymentRequest(
            payment_request=b.PaymentRequest.INPUT(input=dest_address),
            amount=int(sats)))
        try:
            if gate is not None and not gate():
                logging.warning(f'p2p escrow send [{log_tag}]: aborted by gate before send')
                return False, '', 'held: invalidated before send'
        except Exception as gate_err:
            logging.error(f'p2p escrow send [{log_tag}] gate check failed: {gate_err}')
            return False, '', 'held: gate check failed'
        resp = await sdk.send_payment(request=b.SendPaymentRequest(prepare_response=prep))
        payment = getattr(resp, 'payment', None)
        pid = getattr(payment, 'id', '') or ''
        status = str(getattr(payment, 'status', ''))
        logging.info(f'p2p escrow send [{log_tag}]: {sats}s -> {status} ({pid})')
        if _event_value(status) == 'COMPLETED':
            return True, pid, None
        if _event_value(status) == 'PENDING':
            return 'PENDING', pid, None
        return False, pid, status
    try:
        return await offloop(_impl())
    except Exception as e:
        logging.error(f'p2p escrow send [{log_tag}] failed: {e}')
        return False, '', str(e)


async def sweep_fee_to_owner(trade):
    # shortcut: fee sends disabled until durable attribution and recovery are proven.
    return


# ==================== escrow reconciliation ====================

def _match_lock_trade(amount, pid):
    # SDK payment IDs are documented as wallet payment IDs, not as a shared
    # sender/receiver transfer identity. Even an equal ID is not proof of origin.
    return None


async def _refund_stray_lock(row, amount, pid):
    # Attribution of an incoming Spark address payment is unproven. Never send
    # a guessed refund from the shared wallet; hold for manual investigation.
    await notify(_bot().client, admin_id(),
                 f'⚠️ p2p: unverified escrow receive {pid} ({amount} sats); manual resolution required')


async def _handle_escrow_receive(amount, pid):
    logging.warning(f'p2p escrow receive {pid} ({amount}s): identity unverified; held')
    await notify(_bot().client, admin_id(),
                 f'⚠️ p2p: escrow received {amount} sats ({pid}); sender identity unverified — manual resolution required')


def _event_value(value):
    return str(value or '').upper().rsplit('.', 1)[-1]


def _queue_trade_notices(db, tid, kind):
    for target in ('buyer', 'seller', 'room', 'channel', 'admin'):
        db.execute('INSERT OR IGNORE INTO escrow_trade_notices(trade_id,kind,target) VALUES (?,?,?)', (tid, kind, target))


def _invalidate_event_trades(db, event_id):
    rows = db.execute("SELECT DISTINCT t.id FROM trades t JOIN escrow_events e ON e.id=? LEFT JOIN escrow_adjudications a ON a.trade_id=t.id AND a.event_id=e.id LEFT JOIN escrow_fee_attempts f ON f.trade_id=t.id AND f.payment_id=e.payment_id WHERE t.stage IN (?,?) AND (a.event_id IS NOT NULL OR t.payout_id=e.payment_id OR t.refund_id=e.payment_id OR f.payment_id IS NOT NULL)", (event_id, ST_DONE, ST_REFUNDED)).fetchall()
    for row in rows:
        db.execute('UPDATE trades SET stage=?,fee_safe=0,updated=? WHERE id=?', (ST_INVALIDATED, time.time(), row['id']))
        _queue_trade_notices(db, row['id'], 'invalidated')


def record_escrow_event(ptype, amount, pid, status):
    """Store every distinct observation; unknown IDs never match a trade."""
    direction, status = _event_value(ptype), _event_value(status)
    pid = pid if isinstance(pid, str) and pid.strip() else None
    amount = str(amount)
    now = time.time()
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            previous = db.execute('SELECT * FROM escrow_events WHERE payment_id=?', (pid,)).fetchone() if pid else None
            if previous is None:
                cur = db.execute('INSERT INTO escrow_events (payment_id,direction,amount,status,conflict,first_seen,last_seen) VALUES (?,?,?,?,?,?,?)',
                                 (pid, direction, amount, status, int(pid is None), now, now))
                event_id, changed = cur.lastrowid, True
            else:
                conflict = (previous['direction'] != direction or previous['amount'] != amount or
                            (previous['status'] in ('COMPLETED', 'FAILED') and status in ('COMPLETED', 'FAILED') and previous['status'] != status))
                new_status = previous['status'] if previous['status'] in ('COMPLETED', 'FAILED') else status
                db.execute('UPDATE escrow_events SET status=?, conflict=MAX(conflict,?), last_seen=? WHERE id=?',
                           (new_status, int(conflict), now, previous['id']))
                event_id, changed = previous['id'], bool(conflict and not previous['conflict'])
            observation = db.execute('INSERT OR IGNORE INTO escrow_observations (event_id,direction,amount,status,observed_at) VALUES (?,?,?,?,?)',
                                     (event_id, direction, amount, status, now))
            event = db.execute('SELECT * FROM escrow_events WHERE id=?', (event_id,)).fetchone()
            if observation.rowcount and previous is not None:
                db.execute('UPDATE escrow_events SET reviewed_by=NULL, reviewed_at=NULL, review_note=NULL WHERE id=?', (event_id,))
            if observation.rowcount and (direction == 'RECEIVE' or status == 'FAILED' or event['conflict'] or pid is None):
                db.execute('INSERT OR IGNORE INTO escrow_alerts(event_id,direction,amount,status) VALUES (?,?,?,?)', (event_id, direction, amount, status))
            if event['conflict'] and pid:
                db.execute('UPDATE trades SET fee_safe=0 WHERE payout_id=?', (pid,))
                _invalidate_event_trades(db, event_id)
            db.commit()
            return event_id, changed
        except Exception:
            db.rollback()
            raise


def review_escrow_event(uid, event_id, note):
    """Record human evidence only; retain every superseded review."""
    if not is_p2p_admin(uid) or not isinstance(note, str) or not note.strip() or len(note) > 1000:
        return False
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            now = time.time()
            cur = db.execute('UPDATE escrow_events SET reviewed_by=?, reviewed_at=?, review_note=? WHERE id=? AND reviewed_by IS NULL', (uid, now, note.strip(), event_id))
            if cur.rowcount:
                db.execute('INSERT INTO escrow_reviews(event_id,uid,reviewed_at,note) VALUES (?,?,?,?)', (event_id, uid, now, note.strip()))
            db.commit()
            return cur.rowcount == 1
        except Exception:
            db.rollback()
            raise


def issue_escrow_confirmation(uid, tid, action):
    nonce = secrets.token_urlsafe(12)
    _exec('INSERT INTO escrow_confirmations(nonce,uid,trade_id,action,expires) VALUES (?,?,?,?,?)',
          (nonce, uid, tid, action, time.time() + 300))
    return nonce


def consume_escrow_confirmation(uid, tid, action, nonce):
    if not nonce or not is_p2p_admin(uid):
        return False
    return _exec('UPDATE escrow_confirmations SET consumed_at=? WHERE nonce=? AND uid=? AND trade_id=? AND action=? AND consumed_at IS NULL AND expires>?',
                 (time.time(), nonce, uid, tid, action, time.time())).rowcount == 1


def admin_adjudicate(uid, tid, kind):
    """An explicit human decision on a bound, conflict-free observation; never sends."""
    if not is_p2p_admin(uid) or kind not in ('payout', 'refund', 'fee'):
        return 'not authorized'
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            previous = db.execute('SELECT 1 FROM escrow_adjudications WHERE trade_id=? AND kind=?', (tid, kind)).fetchone()
            if previous:
                tr_previous = db.execute('SELECT stage FROM trades WHERE id=?', (tid,)).fetchone()
                db.commit()
                return 'invalidated' if tr_previous and tr_previous['stage'] == ST_INVALIDATED else 'already settled'
            tr = db.execute('SELECT * FROM trades WHERE id=?', (tid,)).fetchone()
            if not tr:
                db.commit()
                return 'insufficient evidence'
            if kind == 'fee':
                attempt = db.execute('SELECT * FROM escrow_fee_attempts WHERE trade_id=?', (tid,)).fetchone()
                valid = tr['stage'] == ST_DONE and not tr['fee_paid'] and attempt and attempt['amount'] == tr['fee']
                pid = attempt['payment_id'] if attempt else None
                amount = tr['fee']
            else:
                valid = tr['stage'] in ((ST_PAYOUT, ST_DONE) if kind == 'payout' else (ST_REFUND,))
                pid = tr['payout_id'] if kind == 'payout' else tr['refund_id']
                amount = payout_amount_of(tr) if kind == 'payout' else refund_amount_of(tr)
            event = db.execute('SELECT * FROM escrow_events WHERE payment_id=?', (pid,)).fetchone() if pid else None
            count = db.execute('SELECT (SELECT COUNT(*) FROM trades WHERE payout_id=?) + (SELECT COUNT(*) FROM trades WHERE refund_id=?) + (SELECT COUNT(*) FROM escrow_fee_attempts WHERE payment_id=?)', (pid, pid, pid)).fetchone()[0] if pid else 0
            observation = db.execute("SELECT 1 FROM escrow_observations WHERE event_id=? AND direction='SEND' AND amount=? AND status='COMPLETED'", (event['id'], str(amount))).fetchone() if event else None
            if not (valid and isinstance(pid, str) and pid.strip() and count == 1 and event and not event['conflict'] and event['direction'] == 'SEND' and event['amount'] == str(amount) and event['status'] == 'COMPLETED' and observation):
                db.commit()
                return 'insufficient evidence'
            now = time.time()
            db.execute('INSERT INTO escrow_adjudications(trade_id,kind,event_id,payment_id,direction,amount,status,admin_uid,adjudicated_at) VALUES (?,?,?,?,?,?,?,?,?)', (tid, kind, event['id'], pid, 'SEND', str(amount), 'COMPLETED', uid, now))
            if kind == 'fee':
                db.execute('UPDATE trades SET fee_paid=1,updated=? WHERE id=?', (now, tid))
            else:
                db.execute('UPDATE trades SET stage=?,updated=?,cancel_seller=0,cancel_buyer=0 WHERE id=?', (ST_DONE if kind == 'payout' else ST_REFUNDED, now, tid))
                _queue_trade_notices(db, tid, kind)
            db.commit()
            return 'settled'
        except sqlite3.IntegrityError:
            db.rollback()
            return 'insufficient evidence'
        except Exception:
            db.rollback()
            raise


def _fee_send_still_valid(tid):
    """Gate checked immediately before the SDK fee send: the trade must still be
    done, un-conflicted and hold an unbound fee attempt."""
    try:
        with _db_lock:
            db = _connect()
            db.execute('BEGIN IMMEDIATE')
            try:
                tr = db.execute('SELECT * FROM trades WHERE id=?', (tid,)).fetchone()
                attempt = db.execute('SELECT 1 FROM escrow_fee_attempts WHERE trade_id=? AND payment_id IS NULL', (tid,)).fetchone()
                conflict = db.execute('SELECT 1 FROM escrow_events WHERE payment_id=? AND conflict=1', (tr['payout_id'],)).fetchone() if tr and tr['payout_id'] else None
                ok = bool(tr and tr['stage'] == ST_DONE and attempt and not conflict and not tr['fee_paid'])
                db.commit()
                return ok
            except Exception:
                db.rollback()
                raise
    except Exception as e:
        logging.error(f'p2p fee gate check failed for trade {tid}: {e}')
        return False


def _bind_fee_payment(tid, pid):
    """Bind the fee payment ID; reconcile already-journaled conflicts at bind time
    (events may have arrived between the send and this binding)."""
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('UPDATE escrow_fee_attempts SET payment_id=?,bound_at=? WHERE trade_id=? AND payment_id IS NULL',
                       (pid, time.time(), tid))
            ev = db.execute('SELECT * FROM escrow_events WHERE payment_id=?', (pid,)).fetchone()
            if ev and ev['conflict']:
                db.execute('UPDATE trades SET stage=?,fee_safe=0,updated=? WHERE id=? AND stage=?',
                           (ST_INVALIDATED, time.time(), tid, ST_DONE))
                _queue_trade_notices(db, tid, 'invalidated')
            db.commit()
        except Exception:
            db.rollback()
            raise


async def admin_send_fee(client, uid, tid):
    """Resolve destination, atomically validate and claim once, then send."""
    if not is_p2p_admin(uid):
        return 'not authorized'
    # Resolve the destination before the transaction: no asynchronous wallet work
    # may occur between validation/claim and starting the send.
    try:
        owner = admin_id()
        if not owner:
            return 'held or already attempted'
        dest = await spark_address_of(owner)
    except Exception:
        logging.exception('p2p fee destination unavailable for trade %s', tid)
        return 'held or no send attempted'
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            tr = db.execute('SELECT * FROM trades WHERE id=?', (tid,)).fetchone()
            proof = db.execute("SELECT a.payment_id FROM escrow_adjudications a JOIN escrow_events e ON e.id=a.event_id WHERE a.trade_id=? AND a.kind='payout' AND e.conflict=0 AND e.direction='SEND' AND e.amount=a.amount AND e.status='COMPLETED' AND e.payment_id=a.payment_id AND EXISTS (SELECT 1 FROM escrow_observations o WHERE o.event_id=e.id AND o.direction='SEND' AND o.amount=e.amount AND o.status='COMPLETED')", (tid,)).fetchone()
            reuse = db.execute('SELECT (SELECT COUNT(*) FROM trades WHERE payout_id=?) + (SELECT COUNT(*) FROM trades WHERE refund_id=?) + (SELECT COUNT(*) FROM escrow_fee_attempts WHERE payment_id=?)', (proof['payment_id'], proof['payment_id'], proof['payment_id'])).fetchone()[0] if proof else 0
            if not tr or not proof or reuse != 1 or not tr['escrow_verified'] or (tr['safety_version'] or 0) < 1 or tr['stage'] != ST_DONE or tr['payout_id'] != proof['payment_id'] or tr['fee_paid'] or tr['fee_tries'] or not tr['fee'] or tr['fee'] < 0:
                db.commit()
                return 'held or already attempted'
            if db.execute('SELECT 1 FROM escrow_fee_attempts WHERE trade_id=?', (tid,)).fetchone():
                db.commit()
                return 'held or already attempted'
            db.execute('INSERT INTO escrow_fee_attempts(trade_id,admin_uid,claimed_at,amount) VALUES (?,?,?,?)', (tid, uid, time.time(), tr['fee']))
            db.execute('UPDATE trades SET fee_tries=fee_tries+1 WHERE id=?', (tid,))
            db.commit()
        except Exception:
            db.rollback()
            raise
    try:
        ok, pid, err = await send_sats_from_escrow(dest, tr['fee'], f'adminfee#{tid}', gate=lambda: _fee_send_still_valid(tid))
        if ok == 'PENDING' or ok is True:
            if isinstance(pid, str) and pid.strip():
                _bind_fee_payment(tid, pid)
                return 'held: adjudicate with completed evidence'
        return 'held: payment ID unavailable or outcome uncertain'
    except Exception:
        logging.exception('p2p fee attempt held for trade %s', tid)
        return 'held: outcome unknown'


def _complete_escrow_trade(tid, payout):
    """Settle a trade whose bound escrow send provably landed.

    Evidence-gated and fail-closed: the ONLY path to ST_DONE/ST_REFUNDED
    without an admin adjudication is a journaled escrow_events row for the
    trade's OWN bound payment id — direction SEND, status COMPLETED,
    conflict-free, amount exactly the version-aware expected amount.  Every
    check and the stage claim run inside ONE BEGIN IMMEDIATE transaction, so
    a concurrent settle/invalidated transition cannot interleave; a later
    contradictory observation still flips the settled trade to ST_INVALIDATED
    via _invalidate_event_trades.  Any evidence miss rolls back, warns and
    queues one durable admin notice — deferral is failure, never a guess."""
    tr = get_trade(tid)
    if not tr:
        return None
    expected_stage = ST_PAYOUT if payout else ST_REFUND
    column = 'payout_id' if payout else 'refund_id'
    pid = tr.get(column)
    if not isinstance(pid, str) or not pid.strip():
        return None   # nothing proven yet — no send id was ever bound
    expected_amount = payout_amount_of(tr) if payout else refund_amount_of(tr)
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            current = db.execute('SELECT * FROM trades WHERE id=?', (tid,)).fetchone()
            if not current or current['stage'] != expected_stage:
                db.commit()   # someone else settled or invalidated it first
                return None
            ev = db.execute('SELECT direction, amount, status, conflict FROM escrow_events WHERE payment_id=?', (pid,)).fetchone()
            reason = None
            if ev is None:
                reason = 'no journaled event'
            elif ev['direction'] != 'SEND':
                reason = f"direction {ev['direction']}"
            elif ev['status'] != 'COMPLETED':
                reason = f"status {ev['status']}"
            elif ev['conflict']:
                reason = 'conflicting evidence'
            else:
                try:
                    ok_amount = int(ev['amount']) == expected_amount
                except (TypeError, ValueError):
                    ok_amount = False
                if not ok_amount:
                    reason = f"amount {ev['amount']} != {expected_amount}"
            if reason:
                db.rollback()
                logging.warning(f'p2p settlement evidence insufficient #{tid} ({reason})')
                _exec('INSERT OR IGNORE INTO escrow_trade_notices(trade_id,kind,target) VALUES (?,?,?)',
                      (tid, 'settlement_held', 'admin'))
                return None
            cur = db.execute(f'UPDATE trades SET stage=?, updated=?, cancel_seller=0, cancel_buyer=0 '
                             f'WHERE id=? AND stage=? AND {column}=?',
                             (ST_DONE if payout else ST_REFUNDED, time.time(), tid, expected_stage, pid))
            if cur.rowcount != 1:
                db.rollback()
                return None
            db.commit()
        except Exception:
            db.rollback()
            raise
    logging.info(f'p2p trade #{tid} settled ({"payout" if payout else "refund"}) pid={pid}')
    return get_trade(tid)


def _bind_escrow_send(tid, pid, payout):
    """Persist even reused IDs as evidence, but mark them ambiguous atomically."""
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            tr = db.execute('SELECT * FROM trades WHERE id=?', (tid,)).fetchone()
            stage, column = (ST_PAYOUT, 'payout_id') if payout else (ST_REFUND, 'refund_id')
            if not tr or tr['stage'] != stage:
                db.commit()
                return
            db.execute(f'UPDATE trades SET {column}=?, updated=? WHERE id=? AND stage=?',
                       (pid, time.time(), tid, stage))
            if isinstance(pid, str) and pid.strip():
                count = db.execute('SELECT COUNT(*) FROM trades WHERE payout_id=? OR refund_id=?', (pid, pid)).fetchone()[0]
                opposite = tr['refund_id'] if payout else tr['payout_id']
                if count > 1 or opposite == pid:
                    now = time.time()
                    event = db.execute('SELECT id FROM escrow_events WHERE payment_id=?', (pid,)).fetchone()
                    amount = str(payout_amount_of(tr) if payout else refund_amount_of(tr))
                    if event is None:
                        event_id = db.execute('INSERT INTO escrow_events(payment_id,direction,amount,status,conflict,first_seen,last_seen) VALUES (?,?,?,?,1,?,?)',
                                              (pid, 'SEND', amount, 'ID_REUSED', now, now)).lastrowid
                    else:
                        event_id = event['id']
                        db.execute('UPDATE escrow_events SET conflict=1,last_seen=?,reviewed_by=NULL,reviewed_at=NULL,review_note=NULL WHERE id=?', (now, event_id))
                    db.execute('INSERT OR IGNORE INTO escrow_observations(event_id,direction,amount,status,observed_at) VALUES (?,?,?,?,?)',
                               (event_id, 'SEND', amount, 'ID_REUSED', now))
                    db.execute('INSERT OR IGNORE INTO escrow_alerts(event_id,direction,amount,status) VALUES (?,?,?,?)',
                               (event_id, 'SEND', amount, 'ID_REUSED'))
                    db.execute('UPDATE trades SET fee_safe=0 WHERE payout_id=?', (pid,))
                    _invalidate_event_trades(db, event_id)
            db.commit()
        except Exception:
            db.rollback()
            raise


async def reconcile_escrow_send(pid):
    if not isinstance(pid, str) or not pid.strip():
        return
    event = _one('SELECT id FROM escrow_events WHERE payment_id=?', (pid,))
    if event:
        await _deliver_escrow_alerts(event['id'])
    # The final decision is inside finish_*'s transaction, not this advisory lookup.
    for tr in _rows('SELECT id,stage FROM trades WHERE payout_id=? OR refund_id=?', (pid, pid)):
        if tr['stage'] == ST_PAYOUT:
            await finish_payout(tr['id'])
        elif tr['stage'] == ST_REFUND:
            await finish_refund(tr['id'])


async def _reconcile_bound_sends():
    """Boot/periodic recovery: re-run settlement for every trade holding a
    bound send id in an in-flight settle stage.  Idempotent — once settled
    the stage no longer matches and the call is a no-op; without full
    evidence the settle transaction holds the trade and queues an admin
    notice (deduplicated by the notices primary key)."""
    rows = _rows("SELECT payout_id, refund_id FROM trades "
                 "WHERE (stage=? AND COALESCE(payout_id,'')<>'') "
                 "OR (stage=? AND COALESCE(refund_id,'')<>'')", (ST_PAYOUT, ST_REFUND))
    for row in rows:
        for pid in (row['payout_id'], row['refund_id']):
            if isinstance(pid, str) and pid.strip():
                try:
                    await reconcile_escrow_send(pid)
                except Exception:
                    logging.exception('p2p bound-send reconcile failed for payment %s', pid)


async def _deliver_escrow_alerts(event_id=None):
    sql = 'SELECT * FROM escrow_alerts WHERE sent_at IS NULL'
    args = ()
    if event_id is not None:
        sql += ' AND event_id=?'
        args = (event_id,)
    for alert in _rows(sql + ' ORDER BY id', args):
        try:
            sent = await notify(_bot().client, admin_id(),
                T(admin_id(), 'adm_alert_event', event_id=alert["event_id"], direction=alert["direction"], amount=alert["amount"], status=alert["status"]))
        except Exception as e:
            logging.error(f'p2p escrow alert delivery failed: {e}')
            continue
        if sent is not None:
            _exec('UPDATE escrow_alerts SET sent_at=? WHERE id=? AND sent_at IS NULL', (time.time(), alert['id']))


async def deliver_trade_notices(client, tid=None):
    sql = 'SELECT * FROM escrow_trade_notices WHERE sent_at IS NULL'
    args = ()
    if tid is not None:
        sql += ' AND trade_id=?'
        args = (tid,)
    for row in _rows(sql + ' ORDER BY trade_id,kind,target', args):
        tr = get_trade(row['trade_id'])
        if not tr:
            continue
        target, kind = row['target'], row['kind']
        if tr['stage'] == ST_INVALIDATED and kind != 'invalidated':
            _exec('UPDATE escrow_trade_notices SET sent_at=? WHERE trade_id=? AND kind=? AND target=? AND sent_at IS NULL',
                  (time.time(), tr['id'], kind, target))
            continue
        try:
            if target in ('buyer', 'seller'):
                who = tr[target]
                result = await notify(client, who, T(who, 'trade_notice_' + kind, id=tr['id']))
                if result is None:
                    continue
            elif target == 'admin':
                who = admin_id()
                if not who or await notify(client, who, T(who, 'trade_notice_' + kind, id=tr['id'])) is None:
                    continue
            elif target == 'room':
                if await update_room(client, tr['id']) is False:
                    continue
            else:
                if await channel_status(tr['order_id'], T(tr['seller'], 'trade_notice_' + kind, id=tr['id'])) is False:
                    continue
            _exec('UPDATE escrow_trade_notices SET sent_at=? WHERE trade_id=? AND kind=? AND target=? AND sent_at IS NULL',
                  (time.time(), tr['id'], kind, target))
        except Exception:
            logging.exception('p2p trade notice failed for #%s', tr['id'])


async def replay_escrow_journal():
    for row in _rows("SELECT payment_id FROM escrow_events WHERE direction='SEND' AND status='COMPLETED' AND conflict=0"):
        try:
            await reconcile_escrow_send(row['payment_id'])
        except Exception:
            logging.exception('p2p journal replay failed for payment %s', row['payment_id'])
    await _deliver_escrow_alerts()
    try:
        await deliver_trade_notices(_bot().client)
    except Exception:
        logging.exception('p2p trade notice replay failed')


def _attribute_lock_receipt(event_id):
    """Exact-amount auto-attribution of an escrow receipt (safety version 2).

    One atomic transaction: validate the journaled event, prove the amount
    matches exactly ONE version>=2 trade in locking with lock_sent=1, then
    claim the transition with a conditional UPDATE (rowcount==1).  Returns
    ('locked', tid) when a trade was credited, ('echo', tid) when the single
    matching trade is already locked by its own verified send (first receipt
    only — resolved quietly), or None for every ambiguous case; None keeps
    the existing hold-and-notify path."""
    with _db_lock:
        db = _connect()
        db.execute('BEGIN IMMEDIATE')
        try:
            ev = db.execute('SELECT * FROM escrow_events WHERE id=?', (event_id,)).fetchone()
            if ev is None or ev['conflict'] or ev['direction'] != 'RECEIVE' \
                    or ev['status'] != 'COMPLETED' or not ev['payment_id']:
                db.commit()
                return None
            try:
                amount = int(str(ev['amount']))
            except (TypeError, ValueError):
                db.commit()
                return None
            if amount <= 0:
                db.commit()
                return None
            pid = ev['payment_id']
            # A payment ID already bound to any send or human decision can
            # never be reused to credit a lock.
            reuse = db.execute('SELECT (SELECT COUNT(*) FROM trades WHERE payout_id=?) '
                               '+ (SELECT COUNT(*) FROM trades WHERE refund_id=?) '
                               '+ (SELECT COUNT(*) FROM escrow_fee_attempts WHERE payment_id=?) '
                               '+ (SELECT COUNT(*) FROM escrow_adjudications WHERE payment_id=?)',
                               (pid, pid, pid, pid)).fetchone()[0]
            if reuse:
                db.commit()
                return None
            # A second completed receipt of the same amount proves nothing;
            # hold it for manual review instead of crediting either.
            dup = db.execute("SELECT COUNT(*) FROM escrow_events WHERE id<>? AND direction='RECEIVE' "
                             "AND amount=? AND status='COMPLETED'", (event_id, ev['amount'])).fetchone()[0]
            if dup:
                db.commit()
                return None
            locking = db.execute('SELECT id FROM trades WHERE stage=? AND COALESCE(lock_sent,0)=1 '
                                 'AND COALESCE(safety_version,0)>=2 AND lock_amount=?',
                                 (ST_LOCKING, amount)).fetchall()
            locked = db.execute('SELECT id FROM trades WHERE stage=? AND COALESCE(escrow_verified,0)=1 '
                                'AND COALESCE(lock_sent,0)=1 AND COALESCE(safety_version,0)>=2 AND lock_amount=?',
                                (ST_LOCKED, amount)).fetchall() if not locking else []
            if len(locking) == 1 and not locked:
                cur = db.execute('UPDATE trades SET stage=?, escrow_verified=1, updated=?, '
                                 'locked_at=COALESCE(locked_at,?), cancel_seller=0, cancel_buyer=0 '
                                 'WHERE id=? AND stage=? AND COALESCE(lock_sent,0)=1 '
                                 'AND COALESCE(safety_version,0)>=2 AND lock_amount=?',
                                 (ST_LOCKED, time.time(), time.time(), locking[0]['id'], ST_LOCKING, amount))
                if cur.rowcount == 1:
                    db.execute('UPDATE escrow_alerts SET sent_at=COALESCE(sent_at,?) WHERE event_id=? '
                               'AND sent_at IS NULL', (time.time(), event_id))
                    tid = locking[0]['id']
                    db.commit()
                    return ('locked', tid)
                db.commit()
                return None
            if len(locking) == 0 and len(locked) == 1:
                # expected counterpart of a lock already verified by the
                # seller's own send result; resolve quietly, no admin alert
                db.execute('UPDATE escrow_alerts SET sent_at=COALESCE(sent_at,?) WHERE event_id=? '
                           'AND sent_at IS NULL', (time.time(), event_id))
                tid = locked[0]['id']
                db.commit()
                return ('echo', tid)
            db.commit()
            return None   # zero or multiple candidates: hold
        except Exception:
            db.rollback()
            raise


async def _attribute_and_announce(event_id):
    """Run receipt attribution; announce an auto-lock like a send-verified lock."""
    try:
        result = _attribute_lock_receipt(event_id)
    except Exception:
        logging.exception('p2p receipt attribution failed')
        return
    if not result:
        return
    kind, tid = result
    tr = get_trade(tid)
    if not tr:
        return
    if kind == 'echo':
        logging.info(f'p2p receipt for trade #{tid} matches its verified lock; resolved quietly')
        return
    await _announce_locked(tr, 'LOCKED (auto-attributed unique lock amount)')
    try:
        await notify(_bot().client, admin_id(),
                     T(admin_id(), 'adm_lock_auto', id=tid, amount=fmt(tr['lock_amount'])))
    except Exception:
        logging.exception('p2p auto-lock admin notice failed')


async def on_escrow_payment(ptype, amount, pid, status, recorded=None):
    """Journal first; receipts are attributed only by exact unique lock amount."""
    event_id, _ = recorded or record_escrow_event(ptype, amount, pid, status)
    if _event_value(ptype) == 'RECEIVE':
        await _attribute_and_announce(event_id)
    await _deliver_escrow_alerts(event_id)
    try:
        await deliver_trade_notices(_bot().client)
    except Exception:
        logging.exception('p2p trade notice delivery failed')
    if _event_value(ptype) == 'SEND':
        await reconcile_escrow_send(pid)


async def startup_scan():
    """One-shot after restart: resolve locks/payouts that landed while down."""
    # Incoming addresses are shared; uncertain seller locks remain held.
    try:
        for tr in _rows('SELECT id FROM trades WHERE stage=? AND COALESCE(safety_version,0)=0', (ST_LOCKING,)):
            await notify(_bot().client, admin_id(),
                         f'⚠️ p2p #{tr["id"]}: legacy lock outcome unknown; held, do not resend; manual investigation required')
        sdk = await escrow_sdk()
        offset, limit = 0, 100
        # Best-effort observations only; this is not a proven stable snapshot.
        until = int(time.time())
        seen_pages = set()
        while True:
            resp = await sdk.list_payments(request=_bot().ListPaymentsRequest(
                to_timestamp=until, offset=offset, limit=limit, sort_ascending=True))
            payments = getattr(resp, 'payments', []) or []
            if not payments:
                break
            page = tuple(getattr(p, 'id', None) for p in payments)
            if page in seen_pages:
                raise RuntimeError('payment pagination repeated; history incomplete')
            seen_pages.add(page)
            for p in payments:
                if _payment_is_sats(p):
                    ev_id, _ = record_escrow_event(str(getattr(p, 'payment_type', '')),
                        getattr(p, 'amount_sats', getattr(p, 'amount', 0)) or 0,
                        getattr(p, 'id', ''), str(getattr(p, 'status', '')))
                    # receipts observed while offline attribute the same way
                    await _attribute_and_announce(ev_id)
            offset += len(payments)
    except Exception as e:
        logging.error(f'p2p startup scan failed: {e}')
        try:
            await notify(_bot().client, admin_id(), '⚠️ P2P payment history scan incomplete; held events require manual investigation.')
        except Exception:
            pass
    # Retry alerts even after failure; observations never authorize settlement.
    await _reconcile_bound_sends()
    await replay_escrow_journal()


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


async def _send_panel(client, uid, text, buttons, parse_mode=None):
    await _strip_panel(client, uid)
    kw = {'buttons': buttons}
    if parse_mode:
        kw['parse_mode'] = parse_mode
    msg = await client.send_message(uid, text, **kw)
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



def get_stage_timer_text(uid, tr):
    st = tr['stage']
    now = time.time()
    if st == ST_LOCKING:
        start = tr.get('lock_started') or tr.get('created') or now
        rem = LOCK_WAIT_SECS - (now - start)
        if rem > 0:
            m = int(rem // 60)
            s = int(rem % 60)
            return T(uid, 'timer_lock_wait', m=m, s=s)
        else:
            return T(uid, 'timer_lock_expired')
    elif st == ST_LOCKED:
        locked_at = tr.get('locked_at') or tr.get('updated') or tr.get('created') or now
        elapsed = int(now - locked_at)
        rem = FIAT_TIMEOUT_SECS - elapsed
        em = elapsed // 60
        if rem > 0:
            rm = rem // 60
            return T(uid, 'timer_locked', em=em, rm=rm)
        else:
            return T(uid, 'timer_locked_over', em=em)
    elif st == ST_FIAT:
        fiat_at = tr.get('fiat_at') or tr.get('updated') or now
        elapsed = int(now - fiat_at)
        rem = RELEASE_TIMEOUT_SECS - elapsed
        eh = elapsed // 3600
        if rem > 0:
            rh = rem // 3600
            return T(uid, 'timer_fiat', eh=eh, rh=rh)
        else:
            return T(uid, 'timer_fiat_over', eh=eh)
    return ''


def room_text(uid, tr, stage_note=None):

    fa = lang_of(uid) == 'fa'
    role = T(uid, 'you_are_seller') if uid == tr['seller'] else T(uid, 'you_are_buyer')
    method = f"\n{T(uid, 'method_l')}: {tr['method']}" if tr.get('method') else ''
    head = T(uid, 'room_title', id=tr['id'])
    fee_line = (f' (+{fmt(tr["fee"])} {T(uid, "fee_short")})'
                if uid == tr['seller'] else '')
    amount_line = T(uid, 'amount_sats', amount=fmt(tr['sats'])) + fee_line
    deal_line = f'🏷 {tr["price"]}'
    o = (_one('SELECT side, fiat_amount, fiat_code FROM orders WHERE id=?',
              (tr['order_id'],)) if tr.get('order_id') else None)
    if o and o.get('fiat_amount'):
        _fw = fiat_word(uid, o['fiat_code'] or 'IRT')
        _amt = f'🏷 {int(o["fiat_amount"]):,} {_fw}'
        deal_line = T(uid, 'deal_sell_for' if o['side'] == 'sell' else 'deal_buy_for',
                      amt=_amt)
    rate_line = ''
    if tr.get('rate_used'):
        fw = 'تومان' if tr['rate_used'] >= 1_000_000 else 'USD'
        _src = 'yadio.io' if fw == 'تومان' else 'bot'
        _p = tr.get('premium')
        _ptag = f' ({_p:+g}%)' if _p else ''
        rate_line = f'\n{T(uid, "rate_line", src=_src, ptag=_ptag)}'
    lines = (
        f'{head}\n'
        f'━━━━━━━━━━━━━━\n\n'
        f'{role}\n'
        f'{amount_line}\n'
        f'{deal_line}{rate_line}{method}\n\n'
    )
    timer_txt = get_stage_timer_text(uid, tr)
    timer_line = f'{timer_txt}\n\n' if timer_txt else ''
    note = stage_note if stage_note is not None else stage_note_for(uid, tr)
    text = lines + timer_line + note
    if fa:
        text = '\n'.join('\u200f' + ln if ln.strip() else ln
                         for ln in text.split('\n'))
    return text


def stage_note_for(uid, tr):
    st = tr['stage']
    if st == ST_LOCKING:
        if tr.get('lock_sent') or not tr.get('safety_version'):
            return T(uid, 'lock_held')
        la = lock_amount_of(tr)
        if la is not None and uid == tr['seller'] and int(tr.get('safety_version') or 0) >= 2:
            return T(uid, 'st_locking') + '\n' + T(uid, 'lock_amount_note', amt=fmt(la))
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
    if st == ST_INVALIDATED:
        return T(uid, 'trade_notice_invalidated', id=tr['id'])
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
        rows.append([_btn(T(uid, 'lock_yes').format(amt=fmt(lock_amount_of(tr) or (tr['sats'] + tr['fee']))),
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
    delivered = True
    for uid_s, chat_msg in panels.items():
        uid = int(uid_s)
        chat_id, msg_id = chat_msg
        try:
            await client.edit_message(
                chat_id, msg_id, room_text(uid, tr, note),
                buttons=room_buttons(uid, tr) if buttons else None)
        except Exception as e:
            delivered = False
            logging.debug(f'p2p room edit {uid}: {e}')
    return delivered


async def channel_status(oid, status_line, delete=False):
    """Edit the channel listing with a status line; delete=True removes it."""
    try:
        o = get_order(oid)
        if not o or not o.get('channel_msg'):
            return True
        b = _bot()
        if delete:
            await b.client.delete_messages(P2P_LIST_CHANNEL, o['channel_msg'])
            _exec('UPDATE orders SET channel_msg=NULL WHERE id=?', (oid,))
            return True
        card = order_card(0, o)
        await b.client.edit_message(P2P_LIST_CHANNEL, o['channel_msg'],
                                    f'{status_line}\n\n{card}',
                                    buttons=None, parse_mode='html')
        return True
    except Exception as e:
        logging.debug(f'p2p channel status {oid}: {e}')
        return False


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
                         if 'مال خودته' not in l and 'own order' not in l.lower())
        if o['side'] == 'sell':
            label = T(o['maker'], 'take_sell_order')
        else:
            label = T(o['maker'], 'take_buy_order')
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
                    f'💰 {tr["sats"]}+{tr["fee"]}s · 🏷 {_h(str(tr["price"]))}\n'
                    f'👤 buyer {user_link(tr["buyer"])} · seller {user_link(tr["seller"])}\n'
                    f'📌 stage: {tr["stage"]}\n'
                    f'🕒 {ts}{chr(10) + extra if extra else ""}')
        else:
            text = f'🤝 P2P · {event}\n🕒 {ts}{chr(10) + extra if extra else ""}'
        # The service env does not export BACKUP_CHANNEL_ID (the bot modules
        # hardcode it); falling back to the running module's constant keeps
        # audit aimed at the same channel the bot's own backup writes to.
        channel = int(os.getenv('BACKUP_CHANNEL_ID', '0') or 0)
        if not channel:
            channel = int(getattr(b, 'BACKUP_CHANNEL_ID', 0) or 0)
        if not channel:
            logging.warning(f'p2p audit skipped #{tid}: no backup channel configured')
            return
        await b.client.send_message(channel, text, parse_mode='html')
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
            await tg.edit_message(loc[0], loc[1], T(rater, 'rate_saved'),
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
        side = T(uid, 'card_sell')
    else:
        side = T(uid, 'card_buy')

    prem_tag = f' {prem:+g}%' if prem else ''
    if is_float and o.get('fiat_amount'):
        # fiat-based: fiat amount fixed; sats at market rate
        amount = T(uid, 'amount_fiat', amount=f'{int(o["fiat_amount"]):,}',
                   fiat=fw, flag=flag).strip()
        price = T(uid, 'rate_market', src_name=src_name, prem_tag=prem_tag)
    elif is_float:
        # sats-based: sats exact; price at market
        amount = T(uid, 'amount_sats', amount=fmt(o["sats"]))
        price = T(uid, 'rate_market', src_name=src_name, prem_tag=prem_tag)
    else:
        amount = T(uid, 'amount_sats', amount=fmt(o["sats"]))
        price = T(uid, 'fixed_price_line', price=_h(str(o["price"])), fiat=fw)

    method = f'\n{T(uid, "method_l")}: {_h(str(o["method"]))}' if o.get('method') else ''
    stats = f'\n{maker_stats_line(uid, o["maker"])}'
    own = f'\n{T(uid, "card_own")}' if o['maker'] == uid else ''
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
        return f'⭐ {avg} ({n} {T(uid, "stats_word")})'
    trades, days = maker_stats(maker_id)
    return T(uid, 'stats_hist', trades=trades, days=days)


def market_buttons(uid, orders, idx, filt):
    is_own = bool(orders) and orders[idx]['maker'] == uid
    rows = []
    if orders:
        rows.append([_btn(T(uid, 'take'), f'p2p:take:{orders[idx]["id"]}')])
    flabels = [(T(uid, 'mf_all'), 'all'), (T(uid, 'mf_sell'), 'sell'), (T(uid, 'mf_buy'), 'buy')]
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
    custom = _btn(T(uid, 'prem_custom'), 'p2p:prem:custom')
    return [row1, row2, [custom]]


async def _wiz_premium_done(client, uid, prem):
    """Shared tail of the wizard premium step (button or typed).
    Returns the optional fixed_resolved notice, or None when the live rate
    is unavailable (wizard stays on the premium step to retry)."""
    w = _wiz.get(uid) or {}
    w['premium'] = prem
    if w.get('price_mode') == 'float':
        w['step'] = 'method'
        _wiz[uid] = w
        return ''   # rate is read at take time
    if w.get('unit') == 'sats' or not w.get('fiat_amount'):
        w['step'] = 'method'
        _wiz[uid] = w
        return ''
    # toman+fixed: resolve sats NOW from rate + premium
    rate, rate_fiat = await bot_fiat_rate(uid)
    if _wiz.get(uid) is not w:
        return None
    if not rate:
        _wiz[uid] = w
        return None   # step stays 'premium' so the retry lands here again
    w['step'] = 'method'
    w['fiat_code'] = rate_fiat
    w['sats'] = float_sats(w['fiat_amount'], prem, rate)
    w['fee'] = fee_for(w['sats'])
    _wiz[uid] = w
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
    w = _wiz.get(uid)
    if not w:
        return False
    w['_ts'] = time.time()
    _wiz[uid] = w
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
        _wiz[uid] = w
        rate, rate_fiat = await bot_fiat_rate(uid)
        if _wiz.get(uid) is not w:
            return True
        if not rate:
            await event.respond(T(uid, 'float_no_rate'))
            return True
        w['fiat_code'] = rate_fiat
        w['sats'] = float_sats(amt, 0, rate)
        w['fee'] = fee_for(w['sats'])
        w['step'] = 'pmode'
        _wiz[uid] = w
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
        _wiz[uid] = w
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
        _wiz[uid] = w
        await event.respond(T(uid, 'wiz_method'),
                            buttons=[[_btn(T(uid, 'skip'), 'p2p:skipmethod')]])
        return True
    if step == 'price':
        if len(text) > 120:
            text = text[:120]
        w['price'] = text
        w['price_mode'] = 'fixed'
        w['step'] = 'method'
        _wiz[uid] = w
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
        _wiz[uid] = w
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
        _wiz[uid] = w
        # expiry picker
        t_text = T(uid, 'ttl_ask')
        t_labels = [T(uid, 'ttl_12h'), T(uid, 'ttl_24h'), T(uid, 'ttl_2d')]
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
    ttl_line = (T(uid, 'ttl_line', label=ttl_row[0] if fa else ttl_row[1])
                if ttl_row else '')

    mode = w.get('price_mode') or 'fixed'
    prem = w.get('premium') or 0
    code = (w.get('fiat_code') or 'IRT').upper()
    fw = fiat_word(uid, code)
    flag = FIAT_FLAGS.get(code, '💱')
    src_name = 'yadio.io' if code in ('IRT', 'IRR') else 'bot'
    prem_tag = f' {prem:+g}%' if prem else ''
    if w.get('side') == 'sell':
        side = T(uid, 'card_sell')
    else:
        side = T(uid, 'card_buy')

    if mode == 'float' and w.get('unit') == 'toman' and w.get('fiat_amount'):
        amount = T(uid, 'amount_fiat', amount=f'{int(w["fiat_amount"]):,}',
                   fiat=fw, flag=flag).strip()
        price = T(uid, 'rate_market', src_name=src_name, prem_tag=prem_tag)
    elif mode == 'float':
        amount = T(uid, 'amount_sats', amount=fmt(w["sats"]))
        price = T(uid, 'rate_market', src_name=src_name, prem_tag=prem_tag)
    else:
        amount = T(uid, 'amount_sats', amount=fmt(w["sats"]))
        price = T(uid, 'fixed_price_line', price=_h(str(w.get("price") or "—")), fiat=fw)

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
        price_disp = f'{T(uid, "float_tag")} · {fmt(w["sats"])} {sats_word(uid)}'
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
                         if 'مال خودته' not in l and 'own order' not in l.lower())
        if o['side'] == 'sell':
            label = T(uid, 'take_sell_order')
        else:
            label = T(uid, 'take_buy_order')
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


LOCK_OFFSET_MIN = 1        # unique identification sats added to lock amounts
LOCK_OFFSET_MAX = 997
LOCK_UNIQUE_WINDOW_SECS = 48 * 3600
_ACTIVE_LOCK_STAGES = ('locking', 'locked', 'fiat', 'payout', 'refund', 'refunding', 'disputed')


def _assign_lock_amount(db, tid, sats, fee):
    """Give the trade a lock amount unique among recent/live trades.

    Must run inside the caller's IMMEDIATE transaction: candidate selection
    and the UPDATE are one atomic step, so two concurrent takers of
    equal-sized orders can never be assigned the same amount.  The small
    offset (1..997 sats) differs from the raw sats+fee every legacy trade
    paid, and from every lock/payment amount of the last 48h or still in a
    money-live stage.  Returns the assigned amount, or None when the offset
    space is exhausted (the trade then stays on the manual path, v1)."""
    base = int(sats) + int(fee)
    rows = db.execute(
        'SELECT COALESCE(lock_amount, sats+fee) AS amt FROM trades WHERE id<>? '
        'AND (COALESCE(lock_started,created,updated) > ? OR stage IN ('
        + ','.join('?' for _ in _ACTIVE_LOCK_STAGES) + '))',
        (tid, time.time() - LOCK_UNIQUE_WINDOW_SECS) + _ACTIVE_LOCK_STAGES).fetchall()
    used = {int(r['amt']) for r in rows if r['amt'] is not None}
    for offset in range(LOCK_OFFSET_MIN, LOCK_OFFSET_MAX + 1):
        candidate = base + offset
        if candidate not in used:
            db.execute('UPDATE trades SET lock_amount=? WHERE id=? AND lock_amount IS NULL',
                       (candidate, tid))
            return candidate
    return None   # pathological volume; trade stays manual-lock (safety v1)


def lock_amount_of(tr):
    """Exact sats the seller must send; None for legacy rows (manual handling)."""
    la = tr.get('lock_amount') if tr else None
    return int(la) if la else None


def _tr_field(tr, name):
    """Read a trade column from a dict (read API) or sqlite3.Row (in-txn)."""
    if not tr:
        return None
    if isinstance(tr, dict):
        return tr.get(name)
    try:
        return tr[name]
    except (IndexError, KeyError):
        return None


def _v2_unique_lock(tr):
    """The provably-unique lock amount, only for safety-version-2 trades.

    Version 2 is set exactly when a unique lock amount was assigned, so the
    pair (lock_amount set, safety_version>=2) is the dust-free settlement
    contract; every lesser version keeps the historical amounts."""
    la = _tr_field(tr, 'lock_amount')
    sv = _tr_field(tr, 'safety_version')
    return int(la) if la and int(sv or 0) >= 2 else None


def payout_amount_of(tr):
    """Exact sats the escrow pays the buyer on release.

    v2: lock_amount - fee — the buyer also receives the unique-tag offset, so
    the shared escrow wallet is left with zero dust once the fee sweeps
    (locked = sats + fee + tag; paid out = (lock - fee) + fee).
    v1/legacy: the raw trade amount, byte-identical to the historical send."""
    la = _v2_unique_lock(tr)
    return la - int(tr['fee']) if la is not None else int(tr['sats'])


def refund_amount_of(tr):
    """Exact sats refunded to the seller.

    v2: the whole unique lock amount (seller gets everything back, dust = 0).
    v1/legacy: sats + fee, byte-identical to the historical send."""
    la = _v2_unique_lock(tr)
    return la if la is not None else int(tr['sats']) + int(tr['fee'])


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
            fee = fee_for(sats)
            cur = db.execute(
                'INSERT INTO trades (order_id, buyer, seller, sats, fee, price, method, '
                'stage, created, updated, lock_started, fiat_final, rate_used, premium, safety_version) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (oid, buyer, seller, sats, fee, row['price'],
                 row['method'], ST_LOCKING, time.time(), time.time(), time.time(),
                 fiat_final, rate_used, row['premium'], 1))
            tid = cur.lastrowid
            # Unique lock amount, assigned inside the same IMMEDIATE claim
            # transaction: exact-amount receipt attribution becomes safe.
            # Version 2 marks trades whose lock amount is provably unique;
            # on exhaustion the trade stays v1 (manual path, unchanged).
            if _assign_lock_amount(db, tid, sats, fee) is not None:
                db.execute('UPDATE trades SET safety_version=2 WHERE id=?', (tid,))
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
    # v2 trades lock the pre-assigned unique amount (sats+fee+tag); v1/legacy
    # trades keep sending the raw sats+fee exactly as before.
    need = lock_amount_of(tr) or (tr['sats'] + tr['fee'])

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
        payment = getattr(resp, 'payment', None)
        return (str(getattr(payment, 'status', '')),
                getattr(payment, 'id', '') or '')

    claim = _exec('UPDATE trades SET lock_sent=1 WHERE id=? AND stage=? AND seller=? '
                  'AND COALESCE(safety_version,0)>=1 AND COALESCE(lock_sent,0)=0 '
                  "AND COALESCE(lock_started,created) > ((julianday('now')-2440587.5)*86400.0 - ?)",
                  (tid, ST_LOCKING, uid, LOCK_WAIT_SECS))
    if claim.rowcount != 1:
        await notify(client, uid, T(uid, 'lock_held'))
        return
    try:
        result = await offloop(_lock_sdk())
        if result is None:
            _exec('UPDATE trades SET lock_sent=0 WHERE id=? AND stage=?', (tid, ST_LOCKING))
            await notify(client, uid, T(uid, 'lock_fail_bal', need=fmt(need)))
            return
        status, pay_id = result
        logging.info(f'p2p lock send trade #{tid}: {need}s -> {status} ({pay_id})')
        if pay_id:
            _exec('UPDATE trades SET lock_pay_id=? WHERE id=? AND stage=?', (pay_id, tid, ST_LOCKING))
        if _event_value(status) == 'COMPLETED':
            await mark_locked(tid)
        else:
            # Pending, failure and unknown outcomes all remain claimed. A send
            # may have succeeded despite an error; safe reconciliation is unavailable.
            await notify(client, uid, T(uid, 'lock_held'))
            await notify(client, admin_id(), f'⚠️ p2p #{tid}: lock {status or "unknown"} — held; safe reconciliation unavailable')
    except Exception as e:
        logging.error(f'p2p lock trade #{tid}: {e}')
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: lock outcome unknown — manual reconciliation required')
        await notify(client, uid, T(uid, 'lock_held'))


async def _announce_locked(tr, event='LOCKED (escrow funded)'):
    """Room/audit announcements shared by the send-result and receipt paths."""
    b = _bot()
    for party in (tr['buyer'], tr['seller']):
        await reply_to_room(b.client, tr['id'], party, f'🔒 {T(party, "st_locked")}')
    await update_room(b.client, tr['id'])
    await audit(b.client, tr['id'], event)
    logging.info(f'p2p trade #{tr["id"]} LOCKED')
    # warm the buyer's spark address so the payout later doesn't stall
    asyncio.get_event_loop().create_task(spark_address_of(tr['buyer']))


async def mark_locked(tid):
    """Only called after the seller's own send returned COMPLETED."""
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_LOCKING:
        return
    cur = _exec('UPDATE trades SET stage=?, escrow_verified=1, updated=?, locked_at=?, '
                'cancel_seller=0, cancel_buyer=0 WHERE id=? AND stage=? AND lock_sent=1',
                (ST_LOCKED, time.time(), time.time(), tid, ST_LOCKING))
    if cur.rowcount != 1:
        return
    await _announce_locked(tr)


async def do_fiat(client, uid, tid):
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_LOCKED or uid != tr['buyer'] or not tr['escrow_verified']:
        await notify(client, uid, T(uid, 'no_action'))
        return
    cur = _exec('UPDATE trades SET stage=?, fiat_at=?, updated=?, '
                'cancel_seller=0, cancel_buyer=0 WHERE id=? AND stage=?',
                (ST_FIAT, time.time(), time.time(), tid, ST_LOCKED))
    if cur.rowcount != 1:
        return
    await update_room(client, tid)
    await reply_to_room(client, tid, tr['seller'], T(tr['seller'], 'ping_fiat'))
    await audit(client, tid, 'FIAT CONFIRMED (buyer pressed paid)')
    logging.info(f'p2p trade #{tid} fiat confirmed by buyer')


async def do_release(client, uid, tid):
    """Seller releases: escrow pays buyer's wallet; fee sweeps to owner."""
    tr = get_trade(tid)
    if not tr or tr['stage'] != ST_FIAT or uid != tr['seller'] or not tr['escrow_verified'] or (tr['safety_version'] or 0) < 1:
        await notify(client, uid, T(uid, 'no_action'))
        return
    # atomic claim: only ONE money-moving path may own this trade — a second
    # click, a replayed callback, or a racing cancel can never double-send
    cur = _exec("UPDATE trades SET stage=?, cancel_seller=0, cancel_buyer=0, updated=? "
                "WHERE id=? AND stage=? AND escrow_verified=1 AND COALESCE(safety_version,0)>=1", (ST_PAYOUT, time.time(), tid, ST_FIAT))
    if cur.rowcount != 1:
        await notify(client, uid, T(uid, 'no_action'))
        return
    await update_room(client, tid, note=T(uid, 'st_payout'), buttons=False)
    try:
        dest = await spark_address_of(tr['buyer'])
    except Exception as e:
        logging.error(f'p2p payout: buyer wallet unreachable #{tid}: {e}')
        _exec("UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=?",
              (ST_FIAT, time.time(), tid, ST_PAYOUT))
        await update_room(client, tid)
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: buyer wallet unreachable — payout parked')
        return
    ok, pid, err = await send_sats_from_escrow(dest, payout_amount_of(tr), f'payout#{tid}')
    if ok == 'PENDING' or (ok is True):
        _bind_escrow_send(tid, pid, True)
        await reconcile_escrow_send(pid)
    else:
        logging.error(f'p2p payout send failed #{tid}: {err}')
        # Once a send is attempted its outcome may be unknown; never reopen it.
        _spark_addr.pop(tr['buyer'], None)
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: payout outcome uncertain ({str(err)[:80]}); manual reconciliation required')


_rate_msgs = {}   # (tid, uid) -> (chat_id, msg_id)


async def finish_payout(tid):
    tr = _complete_escrow_trade(tid, True)
    if not tr:
        return
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
    if not tr or uid not in (tr['buyer'], tr['seller']) or tr['stage'] not in (ST_LOCKING, ST_LOCKED, ST_FIAT):
        await notify(client, uid, T(uid, 'no_action'))
        return
    other = tr['buyer'] if uid == tr['seller'] else tr['seller']
    if tr['stage'] == ST_LOCKING:
        cur = _exec('UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=? AND COALESCE(safety_version,0)>=1 AND COALESCE(lock_sent,0)=0',
                    (ST_CANCELED, time.time(), tid, ST_LOCKING))
        if cur.rowcount != 1:
            await notify(client, uid, T(uid, 'lock_held'))
            return
        _exec("UPDATE orders SET status='canceled' WHERE id=?", (tr['order_id'],))
        await audit(client, tid, f'CANCELED (one-sided pre-lock) by {uid}')
        await channel_status(tr['order_id'], '⛔️ لغو شد', delete=True)
        await notify(client, uid, T(uid, 'cx_solo'))
        await notify(client, other, T(other, 'cx_solo'))
        await update_room(client, tid)
        return
    if tr['stage'] == ST_LOCKED and uid == tr['seller'] and time.time() - (tr.get('locked_at') or tr.get('updated') or tr.get('created') or 0) >= FIAT_TIMEOUT_SECS:
        await timeout_dispute(client, tr, time.time(), FIAT_TIMEOUT_SECS)
        return
    col = 'cancel_seller' if uid == tr['seller'] else 'cancel_buyer'
    cur = _exec(f'UPDATE trades SET {col}=1 WHERE id=? AND stage=?', (tid, tr['stage']))
    if cur.rowcount != 1:
        return
    tr = get_trade(tid)
    if tr['stage'] in (ST_LOCKED, ST_FIAT) and tr['cancel_seller'] and tr['cancel_buyer']:
        await start_refund(client, tid, expected_stage=tr['stage'], require_votes=True)
    else:
        await notify(client, other, T(other, 'cx_req'))
        await update_room(client, tid)


async def start_refund(client, tid, expected_stage=None, timeout_before=None, require_votes=False):
    tr = get_trade(tid)
    if not tr or tr['stage'] not in (ST_LOCKED, ST_FIAT) or not tr['escrow_verified'] or (tr['safety_version'] or 0) < 1:
        return
    expected_stage = expected_stage or tr['stage']
    sql = 'UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=? AND escrow_verified=1 AND COALESCE(safety_version,0)>=1'
    args = [ST_REFUND, time.time(), tid, expected_stage]
    if timeout_before is not None:
        # A deadline is never evidence that fiat was not sent.
        return
    if require_votes:
        sql += ' AND cancel_seller=1 AND cancel_buyer=1'
    if _exec(sql, args).rowcount != 1:
        return
    try:
        dest = await spark_address_of(tr['seller'])
    except Exception as e:
        logging.error(f'p2p refund: seller wallet unreachable #{tid}: {e}')
        cur = _exec("UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=?",
                    (ST_REFUND, time.time(), tid, ST_REFUND))
        if cur.rowcount != 1:
            logging.warning(f'p2p refund park skipped #{tid}: stage changed concurrently')
            return
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: refund parked (seller wallet unreachable)')
        return
    ok, pid, err = await send_sats_from_escrow(dest, refund_amount_of(tr), f'refund#{tid}')
    if ok == 'PENDING' or (ok is True):
        _bind_escrow_send(tid, pid, False)
        await reconcile_escrow_send(pid)
    else:
        cur = _exec("UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=?",
                    (ST_REFUND, time.time(), tid, ST_REFUND))
        if cur.rowcount != 1:
            logging.warning(f'p2p refund park skipped #{tid}: stage changed concurrently')
            return
        await notify(client, admin_id(), f'⚠️ p2p #{tid}: refund failed ({str(err)[:80]})')


async def finish_refund(tid):
    tr = _complete_escrow_trade(tid, False)
    if not tr:
        return
    await channel_status(tr['order_id'], T(tr['seller'], 'refunded_channel'))
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


async def timeout_dispute(client, tr, now, seconds):
    # Read the clock only after acquiring the same lock that serializes writes.
    with _db_lock:
        now = time.time()
        cur = _exec("UPDATE trades SET stage=?, dispute_reason=?, updated=?, cancel_seller=0, cancel_buyer=0 "
                    "WHERE id=? AND stage=? AND COALESCE(locked_at,updated,created) <= ?",
                    (ST_DISPUTED, 'Fiat payment unconfirmed at deadline; verify with both parties',
                     now, tr['id'], ST_LOCKED, now - seconds))
    if cur.rowcount != 1:
        return
    await update_room(client, tr['id'])
    for party in (tr['buyer'], tr['seller']):
        await notify(client, party, T(party, 'fiat_timeout_dispute'))
    await admin_case(client, tr['id'], auto=True)


async def open_dispute(client, uid, tid, reason=None):
    tr = get_trade(tid)
    # dispute only makes sense once the escrow is actually funded (lock paid)
    if not tr or uid not in (tr['buyer'], tr['seller']) or tr['stage'] not in (ST_LOCKED, ST_FIAT, ST_DISPUTED):
        await notify(client, uid, T(uid, 'dispute_not_locked'))
        return False
    prev = (tr.get('dispute_reason') or '').strip()
    merged = f'{prev}\n—\n{reason}' if prev and reason else (reason or prev or '')
    cur = _exec("UPDATE trades SET stage='disputed', dispute_reason=?, updated=?, cancel_seller=0, cancel_buyer=0 WHERE id=? AND stage=?",
                (merged, time.time(), tid, tr['stage']))
    if cur.rowcount != 1:
        return False
    await audit(client, tid, f'DISPUTE FILED by {uid}', f'📝 {(merged or "")[:200]}')
    await update_room(client, tid)
    await admin_case(client, tid, auto=False)
    return True


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
            await b.client.send_message(uid, T(uid, 'username_nudge'))
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
    b = _bot()
    for adm in admins:
        st = stage_label(adm, tr['stage'])
        reason = T(adm, 'adm_reason', reason=_h(tr["dispute_reason"])) if tr.get('dispute_reason') else ''
        auto_line = T(adm, 'adm_auto') if auto else ''
        text = T(adm, 'adm_case_text', id=tid, auto=auto_line,
                 buyer=f'{_h(buyer_un)} · {user_link(tr["buyer"])}',
                 seller=f'{_h(seller_un)} · {user_link(tr["seller"])}',
                 sats=fmt(tr['sats']), fee=fmt(tr['fee']), price=_h(str(tr['price'] or '—')),
                 stage=_h(st), age=_h(_age_label(adm, tr)), reason=reason)
        buttons = [[_btn(T(adm, 'adm_release_btn'), f'p2p:admrel:{tid}'),
                    _btn(T(adm, 'adm_refund_btn'), f'p2p:admref:{tid}')],
                   [_btn(T(adm, 'adm_chat_btn'), f'p2p:admchat:{tid}')]]
        try:
            await b.client.send_message(adm, text, buttons=buttons, parse_mode='html')
        except Exception as e:
            logging.error(f'p2p case notify {adm}: {e}')
    logging.info(f'p2p dispute case #{tid} -> {len(admins)} admin(s) (auto={auto})')


async def admin_release(client, tid, legacy_ack=False, actor=None):
    tr = get_trade(tid)
    if legacy_ack and (not actor or not is_p2p_admin(actor)):
        return
    if not tr or tr['stage'] not in (ST_DISPUTED, ST_FIAT, ST_LOCKED) or not tr['escrow_verified'] or ((tr['safety_version'] or 0) < 1 and not legacy_ack):
        await notify(client, admin_id(), T(admin_id(), 'no_action'))
        return
    cur = _exec("UPDATE trades SET stage=?, payout_id=NULL, updated=? "
                "WHERE id=? AND stage=? AND escrow_verified=1 AND (COALESCE(safety_version,0)>=1 OR ?=1)",
                (ST_PAYOUT, time.time(), tid, tr['stage'], int(legacy_ack)))
    if cur.rowcount != 1:
        await notify(client, admin_id(), T(admin_id(), 'no_action'))
        return
    # force path: pay the buyer from escrow regardless of whose turn it is
    await update_room(client, tid, note=T(tr['buyer'], 'st_payout'), buttons=False)
    try:
        dest = await spark_address_of(tr['buyer'])
    except Exception as e:
        await notify(client, admin_id(), T(admin_id(), 'adm_buyer_unreachable', id=tid, err=str(e)[:80]))
        return
    ok, pid, err = await send_sats_from_escrow(dest, payout_amount_of(tr), f'adminpayout#{tid}')
    if ok == 'PENDING' or (ok is True):
        _bind_escrow_send(tid, pid, True)
        await reconcile_escrow_send(pid)
    else:
        await notify(client, admin_id(), T(admin_id(), 'adm_payout_failed', id=tid, err=str(err)[:80]))


async def admin_refund(client, tid, legacy_ack=False, actor=None):
    tr = get_trade(tid)
    if legacy_ack and (not actor or not is_p2p_admin(actor)):
        return
    if not tr or not tr['escrow_verified'] or ((tr['safety_version'] or 0) < 1 and not legacy_ack):
        return
    if tr['stage'] in (ST_DONE, ST_REFUNDED, ST_CANCELED):
        await notify(client, admin_id(), T(admin_id(), 'no_action'))
        return
    if tr['stage'] in (ST_DISPUTED, ST_LOCKED, ST_FIAT):
        # Claim directly, including explicitly acknowledged legacy cases.
        cur = _exec("UPDATE trades SET stage=?, refund_id=NULL, updated=? "
                    "WHERE id=? AND stage=? AND escrow_verified=1 AND (COALESCE(safety_version,0)>=1 OR ?=1)",
                    (ST_REFUND, time.time(), tid, tr['stage'], int(legacy_ack)))
        if cur.rowcount != 1:
            await notify(client, admin_id(), T(admin_id(), 'no_action'))
            return
        await update_room(client, tid, buttons=False)
        try:
            dest = await spark_address_of(tr['seller'])
        except Exception as e:
            await notify(client, admin_id(), T(admin_id(), 'adm_seller_unreachable', id=tid, err=str(e)[:80]))
            return
        ok, pid, err = await send_sats_from_escrow(dest, refund_amount_of(tr), f'adminrefund#{tid}')
        if ok == 'PENDING' or (ok is True):
            _bind_escrow_send(tid, pid, False)
            await reconcile_escrow_send(pid)
            return
        cur = _exec("UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=?",
                    (ST_REFUND, time.time(), tid, ST_REFUND))
        if cur.rowcount != 1:
            logging.warning(f'p2p admin refund park skipped #{tid}: stage changed concurrently')
            return
        await notify(client, admin_id(), T(admin_id(), 'adm_refund_failed', id=tid, err=str(err)[:80]))
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
            await channel_status(o['id'], T(o['maker'], 'expired_channel'), delete=True)
            await notify(client, o['maker'], T(o['maker'], 'expired_notify', id=o['id']))
    # 1) taken but never locked
    for tr in _rows('SELECT * FROM trades WHERE stage=?', (ST_LOCKING,)):
        if now - (tr['lock_started'] or tr['created']) > LOCK_WAIT_SECS:
            cur = _exec("UPDATE trades SET stage='canceled', updated=? WHERE id=? AND stage=? AND COALESCE(safety_version,0)>=1 AND COALESCE(lock_sent,0)=0 AND COALESCE(lock_started,created) <= ?",
                        (now, tr['id'], ST_LOCKING, now - LOCK_WAIT_SECS))
            if cur.rowcount != 1:
                continue
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
    # 2) Buyer silence is not proof of non-payment.
    for tr in _rows('SELECT * FROM trades WHERE stage=?', (ST_LOCKED,)):
        locked_at = tr.get('locked_at') or tr.get('updated') or tr.get('created') or now
        if now - locked_at >= FIAT_TIMEOUT_SECS * 2:
            await timeout_dispute(client, tr, now, FIAT_TIMEOUT_SECS * 2)
        elif tr['warned'] == 0 and now - locked_at >= FIAT_TIMEOUT_SECS:
            cur = _exec('UPDATE trades SET warned=1 WHERE id=? AND stage=? AND warned=0 '
                        'AND COALESCE(locked_at,updated,created) <= ?',
                        (tr['id'], ST_LOCKED, now - FIAT_TIMEOUT_SECS))
            if cur.rowcount:
                for party in (tr['buyer'], tr['seller']):
                    await notify(client, party, T(party, 'fiat_warn'))
    # 3) fiat confirmed, seller silent -> auto dispute
    for tr in _rows('SELECT * FROM trades WHERE stage=?', (ST_FIAT,)):
        if tr['fiat_at'] and now - tr['fiat_at'] > RELEASE_TIMEOUT_SECS:
            cur = _exec("UPDATE trades SET stage='disputed', updated=?, cancel_seller=0, cancel_buyer=0 WHERE id=? AND stage=?",
                        (now, tr['id'], ST_FIAT))
            if cur.rowcount != 1:
                continue
            await update_room(client, tr['id'])
            for party in (tr['buyer'], tr['seller']):
                await notify(client, party, T(party, 'auto_dispute'))
            await admin_case(client, tr['id'], auto=True)
    # Replay and alerts do not depend on wallet connectivity.
    await replay_escrow_journal()
    # 4) payout/refund in flight — check escrow ledger (send-side bookkeeping)
    try:
        async def _lp():
            sdk = await escrow_sdk()
            return await sdk.list_payments(request=_bot().ListPaymentsRequest(limit=50))
        resp = await offloop(_lp())
        for payment in getattr(resp, 'payments', []) or []:
            if _payment_is_sats(payment):
                await on_escrow_payment(str(getattr(payment, 'payment_type', '')),
                    getattr(payment, 'amount_sats', getattr(payment, 'amount', 0)) or 0,
                    getattr(payment, 'id', ''), str(getattr(payment, 'status', '')))
        for row in _rows("SELECT payment_id FROM escrow_events WHERE direction='SEND' AND status='COMPLETED' AND conflict=0"):
            await reconcile_escrow_send(row['payment_id'])
    except Exception as e:
        logging.debug(f'p2p sweep ledger check: {e}')
    # 4b) bound payout/refund ids reconcile even when the ledger check above
    # failed — settlement needs only the journaled evidence, not the wallet.
    await _reconcile_bound_sends()
    # Automatic fee sends are disabled, including for pre-existing DONE trades.


# ==================== boot ====================

async def boot(client):
    init_db()
    if not enabled_for_all():
        logging.info('p2p: no public access or valid allowlist users — module idle')
        return
    if not os.path.exists(ESCROW_SEED_PATH):
        await replay_escrow_journal()
        logging.warning('p2p: escrow seed missing — section disabled')
        return
    try:
        await escrow_sdk()
        await startup_scan()
        logging.info('p2p section booted')
    except Exception as e:
        logging.error(f'p2p boot failed: {e}')
        await replay_escrow_journal()


def enabled_for_all():
    return p2p_public() or bool(test_users())


# ==================== admin panel ====================
# Shared row filters so the panel counts always match the paged listings.
HOLDS_WHERE = ("((stage='locking' AND (lock_sent=1 OR COALESCE(safety_version,0)=0)) "
               "OR stage IN ('payout','refunding','invalidated') "
               "OR (stage IN ('locked','fiat_sent','disputed') "
               "AND (COALESCE(escrow_verified,0)=0 OR COALESCE(safety_version,0)<1)) "
               "OR (stage='done' AND COALESCE(fee_paid,0)=0 AND COALESCE(fee,0)>0))")
EVENTS_WHERE = "(direction='RECEIVE' OR conflict=1 OR status='FAILED')"


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
        'holds': c(f"SELECT COUNT(*) AS c FROM trades WHERE {HOLDS_WHERE}"),
        'events': c(f"SELECT COUNT(*) AS c FROM escrow_events WHERE {EVENTS_WHERE}"),
        'banned': c('SELECT COUNT(*) AS c FROM banned'),
        'admins': len(p2p_admin_ids()),
    }


def chat_summary(tid, limit=30, trunc=200, uid=None):
    who = uid if uid is not None else admin_id()
    rows = _rows('SELECT * FROM chat_msgs WHERE tid=? ORDER BY id DESC LIMIT ?', (tid, limit))
    if not rows:
        return T(who, 'adm_chat_empty')
    rows = list(reversed(rows))
    tr = get_trade(tid)
    seller = tr['seller'] if tr else None
    lines = [T(who, 'adm_chat_title', id=tid, n=len(rows))]
    for r in rows:
        role = T(who, 'adm_chat_seller') if r['uid'] == seller else T(who, 'adm_chat_buyer')
        t = r['text'] or ''
        if len(t) > trunc:
            t = t[:trunc] + '…'
        lines.append(f'{role}: {t}')
    return '\n'.join(lines)


def _hold_conflict_lines(tr, attempt=None):
    """Read-only conflict evidence bound to this trade's payment IDs (if any)."""
    pids = [pid for pid in (tr.get('lock_pay_id'), tr.get('payout_id'), tr.get('refund_id'),
                            (attempt or {}).get('payment_id')) if pid]
    if not pids:
        return []
    q = ','.join('?' for _ in pids)
    lines = []
    for ev in _rows(f'SELECT * FROM escrow_events WHERE payment_id IN ({q}) AND conflict=1', pids):
        obs = _rows('SELECT direction,amount,status FROM escrow_observations '
                    'WHERE event_id=? ORDER BY observed_at', (ev['id'],))
        obs_txt = ' | '.join(f'{o["direction"]} {o["amount"]} {o["status"]}' for o in obs) or '—'
        lines.append(f'#{ev["id"]} {ev["payment_id"]}: {obs_txt}')
    return lines


_STAGE_LABEL_KEY = {
    ST_LOCKING: 'adm_stage_locking', ST_LOCKED: 'adm_stage_locked',
    ST_FIAT: 'adm_stage_fiat', ST_DISPUTED: 'adm_stage_disputed',
    ST_PAYOUT: 'adm_stage_payout', ST_REFUND: 'adm_stage_refunding',
    ST_DONE: 'adm_stage_done', ST_REFUNDED: 'adm_stage_refunded',
    ST_CANCELED: 'adm_stage_canceled', ST_INVALIDATED: 'adm_stage_invalidated',
}


def stage_label(uid, stage):
    """Localized stage badge for admin listings (raw stage as fallback)."""
    key = _STAGE_LABEL_KEY.get(stage)
    return T(uid, key) if key else stage


def _age_label(uid, tr):
    """Rough trade age since its last update, for admin case cards."""
    ts = tr.get('updated') or tr.get('created')
    if not ts:
        return T(uid, 'unknown')
    secs = max(0, time.time() - ts)
    if secs < 3600:
        return T(uid, 'age_mins', n=int(secs // 60))
    if secs < 86400:
        return T(uid, 'age_hours', n=int(secs // 3600))
    return T(uid, 'age_days', n=int(secs // 86400))


def _back_row(uid):
    return [_btn(T(uid, 'admin_back'), 'p2p:adm')]


async def _show_panel(client, uid, text, buttons, edit_event=None, parse_mode=None):
    """Shared tail for admin screens: edit in place or push a pinned panel."""
    if edit_event is not None:
        kw = {'buttons': buttons}
        if parse_mode:
            kw['parse_mode'] = parse_mode
        await edit_event.edit(text, **kw)
    else:
        await _send_panel(client, uid, text, buttons, parse_mode=parse_mode)


async def admin_panel(client, uid, edit_event=None):
    st = p2p_stats()
    attention = []
    if st['disputes']:
        attention.append(T(uid, 'adm_attn_disputes', n=st['disputes']))
    if st['holds']:
        attention.append(T(uid, 'adm_attn_holds', n=st['holds']))
    if st['events']:
        attention.append(T(uid, 'adm_attn_events', n=st['events']))
    header = (T(uid, 'adm_attention_title') + '\n' + '\n'.join(attention) + '\n\n') if attention else ''
    text = (header + T(uid, 'adm_panel_title') + '\n'
            + T(uid, 'adm_st_trades', trades=st['trades'], done=st['done']) + '\n'
            + T(uid, 'adm_st_users', users=st['users']) + '\n'
            + T(uid, 'adm_st_fees', fees=fmt(st['fees'])) + '\n'
            + T(uid, 'adm_st_orders', orders=st['open_orders']) + '\n'
            + T(uid, 'adm_st_disputes', disputes=st['disputes']) + '\n'
            + T(uid, 'adm_st_mod', banned=st['banned'], admins=st['admins']))
    def _badge(label, n):
        return f'{label} ({n})' if n else label
    buttons = [
        [_btn(_badge(T(uid, 'adm_disputes_btn'), st['disputes']), 'p2p:adm:disputes')],
        [_btn(_badge(T(uid, 'events_panel'), st['events']), 'p2p:admevents:0')],
        [_btn(_badge(T(uid, 'holds_panel'), st['holds']), 'p2p:admholds:0')],
        [_btn(T(uid, 'adm_ban_btn'), 'p2p:adm:ban'), _btn(T(uid, 'adm_banlist_btn'), 'p2p:adm:banlist')],
        [_btn(T(uid, 'adm_admins_btn'), 'p2p:adm:admins')],
        [_btn(T(uid, 'back_p2p'), 'p2p:home')],
    ]
    await _show_panel(client, uid, text, buttons, edit_event)


async def admin_disputes(client, uid, edit_event=None):
    rows = _rows("SELECT * FROM trades WHERE stage='disputed' ORDER BY updated DESC LIMIT 20")
    if not rows:
        text, buttons = T(uid, 'adm_disputes_empty'), [_back_row(uid)]
    else:
        text = T(uid, 'adm_disputes_title', n=len(rows))
        for r in rows:
            text += '\n' + T(uid, 'adm_dispute_card', id=r['id'], sats=fmt(r['sats']),
                             price=str(r['price'] or '—'), buyer=r['buyer'], seller=r['seller'],
                             age=_age_label(uid, r))
        buttons = [[_btn(f'#{r["id"]} · {fmt(r["sats"])} {sats_word(uid)}', f'p2p:admcase:{r["id"]}')] for r in rows]
        buttons.append(_back_row(uid))
    await _show_panel(client, uid, text, buttons, edit_event)


async def admin_banlist(client, uid, edit_event=None):
    rows = _rows('SELECT * FROM banned ORDER BY at DESC LIMIT 30')
    if not rows:
        text, buttons = T(uid, 'adm_banlist_empty'), [_back_row(uid)]
    else:
        text = T(uid, 'adm_banlist_title', n=len(rows))
        buttons = []
        for r in rows:
            reason = f' — {r["reason"]}' if r['reason'] else ''
            text += f'\n• {r["uid"]}{reason}'
            buttons.append([_btn(T(uid, 'adm_unban_btn', target=r['uid']), f'p2p:adm:unban:{r["uid"]}')])
        buttons.append(_back_row(uid))
    await _show_panel(client, uid, text, buttons, edit_event)


async def admin_admins(client, uid, edit_event=None):
    ids = p2p_admin_ids()
    text = T(uid, 'adm_admins_title') + '\n' + '\n'.join(
        f'• {i}' + (T(uid, 'adm_owner_tag') if i == admin_id() else '') for i in ids)
    buttons = []
    if uid == admin_id():
        buttons.append([_btn(T(uid, 'adm_add_btn'), 'p2p:adm:admadd')])
        for i in ids:
            if i != admin_id():
                buttons.append([_btn(T(uid, 'adm_del_btn', target=i), f'p2p:adm:admdel:{i}')])
    buttons.append(_back_row(uid))
    await _show_panel(client, uid, text, buttons, edit_event)


async def admin_case_view(client, uid, tid, edit_event=None):
    tr = get_trade(tid)
    if not tr:
        text, buttons = T(uid, 'adm_case_missing'), [_back_row(uid)]
    else:
        # two wallet/username lookups run concurrently — the loading toast
        # on the admcase route covers the wait
        buyer_un, seller_un = await asyncio.gather(username_line(tr['buyer']),
                                                   username_line(tr['seller']))
        reason = T(uid, 'adm_reason', reason=_h(tr["dispute_reason"])) if tr.get('dispute_reason') else ''
        text = T(uid, 'adm_case_view_text', id=tid,
                 buyer=f'{_h(buyer_un)} · {user_link(tr["buyer"])}',
                 seller=f'{_h(seller_un)} · {user_link(tr["seller"])}',
                 sats=fmt(tr['sats']), fee=fmt(tr['fee']), price=_h(str(tr['price'] or '—')),
                 stage=_h(stage_label(uid, tr['stage'])), reason=reason,
                 age=_h(_age_label(uid, tr)),
                 chat=_h(chat_summary(tid, uid=uid)))
        buttons = [[_btn(T(uid, 'adm_release_btn'), f'p2p:admrel:{tid}'),
                    _btn(T(uid, 'adm_refund_btn'), f'p2p:admref:{tid}')],
                   [_btn(T(uid, 'adm_chat_full_btn'), f'p2p:admchat:{tid}'),
                    _btn(T(uid, 'admin_back'), 'p2p:adm')]]
    await _show_panel(client, uid, text, buttons, edit_event, parse_mode='html')


def _home_rows(uid):
    """Shared P2P home keyboard (single admin entry, in-progress badge)."""
    rows = [[_btn(T(uid, 'market'), 'p2p:market'), _btn(T(uid, 'new_order'), 'p2p:new')]]
    n = active_trade_count(uid)
    if n:
        rows.append([_btn(f'{T(uid, "active_title")} ({n})', 'p2p:active')])
    rows.append([_btn(T(uid, 'my_orders'), 'p2p:mine'), _btn(T(uid, 'guide'), 'p2p:guide')])
    if is_p2p_admin(uid):
        rows.append([_btn(T(uid, 'admin_panel_btn'), 'p2p:adm')])
    rows.append([_btn(T(uid, 'back_menu'), 'p2p:exit')])
    return rows


# ==================== routing: callbacks ====================

async def _admin_gate(event, uid):
    """Shared admin prelude for on_callback branches (item 25): alerts
    non-admins with the admin_only popup. Returns True when the caller
    must return immediately."""
    if is_p2p_admin(uid):
        return False
    await event.answer(T(uid, 'admin_only'), alert=True)
    return True


async def _cb_rate(client, event, uid, data):
    """p2p:rate:<tid>:<ratee>:<stars> — membership-gated rating save."""
    await event.answer()
    try:
        _, _, tid, ratee, stars = data.split(':')
        tid, ratee, stars = int(tid), int(ratee), int(stars)
    except (ValueError, TypeError):
        return  # forged callback data
    # membership gate (IDOR fix): only a done trade's counterparty may be rated
    tr = get_trade(tid)
    if (not tr or uid not in (tr['buyer'], tr['seller'])
            or ratee not in (tr['buyer'], tr['seller'])
            or ratee == uid or tr['stage'] != ST_DONE):
        return
    await save_rating(client, uid, ratee, tid, stars)


async def _cb_close(client, event, uid, data):
    """p2p:close:<tid> — drop the closer's room panel + pin, kill chat."""
    await event.answer()
    try:
        tid = int(data.split(':')[2])
    except (ValueError, IndexError):
        return  # forged callback data
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


async def on_callback(client, event, data):
    uid = event.sender_id
    init_db()
    admin = is_p2p_admin(uid)
    if is_banned(uid) and not admin:
        await event.answer(T(uid, 'banned_answer'), alert=True)
        return
    if not admin and not enabled_for(uid):
        await event.answer(T(uid, 'testing_answer'))
        return

    if data == 'p2p' or data == 'p2p:home':
        await event.answer()
        if not has_agreed(uid):
            text = f'{T(uid, "tos_title")}\n\n{T(uid, "tos_body")}'
            await event.edit(text, buttons=[[_btn(T(uid, 'tos_ok'), 'p2p:agree')]])
            return
        text = (f'{T(uid, "home_title")}\n\n{T(uid, "home_body")}\n'
                f'{T(uid, "home_fee")}')
        await event.edit(text, buttons=_home_rows(uid))
        return

    if data == 'p2p:agree':
        await event.answer()
        set_agreed(uid)
        text = (f'{T(uid, "home_title")}\n\n{T(uid, "home_body")}\n'
                f'{T(uid, "home_fee")}')
        await event.edit(text, buttons=_home_rows(uid))
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
        if mode == 'fixed' and w.get('unit') == 'toman' and w.get('fiat_amount'):
            # toman-seller manual price: ask how many SATS for his toman amount
            w['step'] = 'sats_price'
            _wiz[uid] = w
            await event.edit(T(uid, 'wiz_sats_price'))
        elif mode == 'fixed':
            w['step'] = 'price'
            _wiz[uid] = w
            await event.edit(T(uid, 'wiz_price'))
        elif mode == 'float' and w.get('unit') == 'sats':
            # sats + live rate: premium % adjusts the derived fiat value
            w['step'] = 'premium'
            _wiz[uid] = w
            await event.edit(T(uid, 'wiz_premium'), buttons=_prem_buttons(uid))
        elif mode == 'float' and w.get('unit') == 'toman' and w.get('fiat_amount'):
            # toman + live rate: premium % on top of the market rate
            w['step'] = 'premium'
            _wiz[uid] = w
            await event.edit(T(uid, 'wiz_premium'), buttons=_prem_buttons(uid))
        else:
            w['step'] = 'fiat_amount'
            _wiz[uid] = w
            _rate, _fiat = await bot_fiat_rate(uid)
            fw = fiat_word(uid, _fiat)
            txt = T(uid, 'wiz_fiat_amount').replace('تومان', fw).replace('Toman', fw)
            await event.edit(txt)
        return


    if data.startswith('p2p:rate:'):
        await _cb_rate(client, event, uid, data)
        return

    if data.startswith('p2p:close:'):
        await _cb_close(client, event, uid, data)
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
            btns.append([_btn(f'#{tr["id"]} · {role} {fmt(tr["sats"])} {sats_word(uid)}',
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
            _wiz[uid] = w
            txt = T(uid, 'wiz_amount')
        else:
            w['step'] = 'fiat_amount'
            _wiz[uid] = w
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
        _wiz[uid] = w
        # expiry picker (same as the text path)
        t_text = T(uid, 'ttl_ask')
        t_labels = [T(uid, 'ttl_12h'), T(uid, 'ttl_24h'), T(uid, 'ttl_2d')]
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
            lines.append(f'#{o["id"]} · {side_word(uid, o["side"])} {fmt(o["sats"])} {sats_word(uid)} — '
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
                f'{T(uid, "amount_l")}: {fmt(o["sats"])} {sats_word(uid)}\n'
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
        tr = get_trade(tid)
        if not tr or uid not in (tr['buyer'], tr['seller']) or tr['stage'] not in (ST_LOCKED, ST_FIAT, ST_DISPUTED):
            await notify(client, uid, T(uid, 'no_action'))
            return
        _wiz[f'dsp:{uid}'] = tid  # capture optional note as next text
        # in-place: the confirm row becomes the note prompt — no leftover buttons
        await event.edit(T(uid, 'dispute_note'),
                         buttons=[[_btn('«', f'p2p:room:{tid}')]])
        return

    # ---- پنل ادمین ----
    if data.startswith('p2p:admholds:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        cursor = max(0, int(data.split(':')[2]))
        rows = _rows(f"SELECT * FROM trades WHERE id>? AND {HOLDS_WHERE} ORDER BY id LIMIT 10", (cursor,))
        total = (_one(f"SELECT COUNT(*) AS c FROM trades WHERE {HOLDS_WHERE}") or {}).get('c') or 0
        buttons = [[_btn(f'#{r["id"]} · {stage_label(uid, r["stage"])}', f'p2p:admhold:{r["id"]}')] for r in rows]
        if len(rows) == 10:
            buttons.append([_btn(T(uid, 'next'), f'p2p:admholds:{rows[-1]["id"]}')])
        buttons.append(_back_row(uid))
        await event.edit(f'{T(uid, "holds_panel")} ({total})\n\n{T(uid, "holds_title")}', buttons=buttons)
        return
    if data.startswith('p2p:admhold:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        tr = get_trade(int(data.split(':')[2]))
        if not tr:
            await event.edit(T(uid, 'trade_missing')); return
        attempt = _one('SELECT * FROM escrow_fee_attempts WHERE trade_id=?', (tr['id'],))
        unknown = T(uid, 'unknown')
        text = T(uid, 'hold_detail', id=tr['id'], stage=stage_label(uid, tr['stage']), buyer=tr['buyer'], seller=tr['seller'], lock=tr['lock_pay_id'] or unknown, payout=tr['payout_id'] or unknown, refund=tr['refund_id'] or unknown, fee=tr['fee'], tries=tr['fee_tries'], fee_id=(attempt or {}).get('payment_id') or unknown)
        # conflict evidence for invalidated / conflicted holds
        conflicts = _hold_conflict_lines(tr, attempt)
        if conflicts:
            text += T(uid, 'hold_conflict_title', lines='\n'.join(conflicts))
        buttons = []
        settled = {r['kind'] for r in _rows('SELECT kind FROM escrow_adjudications WHERE trade_id=?', (tr['id'],))}
        # settle buttons only where bound evidence can exist and none is recorded yet
        if tr['payout_id'] and 'payout' not in settled and tr['stage'] in (ST_PAYOUT, ST_DONE):
            buttons.append([_btn(T(uid, 'mark_settled_payout'), f'p2p:adjudicate:payout:{tr["id"]}')])
        if tr['refund_id'] and 'refund' not in settled and tr['stage'] == ST_REFUND:
            buttons.append([_btn(T(uid, 'mark_settled_refund'), f'p2p:adjudicate:refund:{tr["id"]}')])
        if (attempt or {}).get('payment_id') and 'fee' not in settled and tr['stage'] == ST_DONE and not tr['fee_paid']:
            buttons.append([_btn(T(uid, 'mark_settled_fee'), f'p2p:adjudicate:fee:{tr["id"]}')])
        # one-attempt fee send: only with an adjudicated, conflict-free payout and no prior attempt
        if tr['stage'] == ST_DONE and not tr['fee_paid'] and not tr['fee_tries'] and not attempt:
            proof = _one("SELECT a.payment_id FROM escrow_adjudications a JOIN escrow_events e ON e.id=a.event_id "
                         "WHERE a.trade_id=? AND a.kind='payout' AND e.conflict=0 AND e.direction='SEND' "
                         "AND e.amount=a.amount AND e.status='COMPLETED' AND e.payment_id=a.payment_id "
                         "AND EXISTS (SELECT 1 FROM escrow_observations o WHERE o.event_id=e.id "
                         "AND o.direction='SEND' AND o.amount=e.amount AND o.status='COMPLETED')", (tr['id'],))
            if proof and proof.get('payment_id') == tr['payout_id']:
                buttons.append([_btn(T(uid, 'send_fee'), f'p2p:feeconfirm:{tr["id"]}')])
        # legacy trades resolve through the dispute case (acknowledgement path)
        if (tr['safety_version'] or 0) < 1 and tr['stage'] in (ST_LOCKED, ST_FIAT, ST_DISPUTED):
            buttons.append([_btn(T(uid, 'hold_open_case'), f'p2p:admcase:{tr["id"]}')])
        buttons.append([_btn(T(uid, 'holds_back'), 'p2p:admholds:0')])
        await event.edit(text, buttons=buttons, parse_mode=None)
        return
    if data.startswith('p2p:adjudicate:'):
        if await _admin_gate(event, uid):
            return
        _, _, kind, tid = data.split(':')
        result = admin_adjudicate(uid, int(tid), kind)
        if result == 'settled':
            await deliver_trade_notices(client, int(tid))
        await event.answer(T(uid, 'result_' + result.replace(' ', '_')), alert=True)
        return
    if data.startswith('p2p:feeconfirm:'):
        if await _admin_gate(event, uid):
            return
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if not tr or tr['stage'] != ST_DONE or tr['fee_paid'] or tr['fee_tries'] \
                or _one('SELECT 1 FROM escrow_fee_attempts WHERE trade_id=?', (tid,)):
            await event.answer(T(uid, 'result_held'), alert=True); return
        nonce = issue_escrow_confirmation(uid, tid, 'fee')
        await event.answer()
        await event.edit(T(uid, 'confirm_fee_text', fee=tr['fee']), buttons=[[_btn(T(uid, 'confirm_fee'), f'p2p:feesend:{tid}:{nonce}')], [_btn(T(uid, 'cancel_admin'), f'p2p:admhold:{tid}')]])
        return
    if data.startswith('p2p:feesend:'):
        if await _admin_gate(event, uid):
            return
        parts = data.split(':')
        if len(parts) != 4 or not parts[2].isdigit() or not consume_escrow_confirmation(uid, int(parts[2]), 'fee', parts[3]):
            await event.answer(T(uid, 'confirmation_invalid'), alert=True); return
        await event.answer()
        result = await admin_send_fee(client, uid, int(parts[2]))
        await event.respond(T(uid, 'result_held') if result.startswith('held') else T(uid, 'result_' + result.replace(' ', '_')))
        return
    if data.startswith('p2p:admevents:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        cursor = int(data.split(':')[2])
        rows = _rows(f"SELECT * FROM escrow_events WHERE {EVENTS_WHERE} AND id>? ORDER BY id LIMIT 10", (max(0, cursor),))
        total = (_one(f"SELECT COUNT(*) AS c FROM escrow_events WHERE {EVENTS_WHERE}") or {}).get('c') or 0
        text = f'{T(uid, "events_panel")} ({total})\n\n{T(uid, "events_title")}'
        buttons = [[_btn(f'#{r["id"]} · {r["direction"]} · {r["amount"]}', f'p2p:admevent:{r["id"]}')] for r in rows]
        if len(rows) == 10:
            buttons.append([_btn(T(uid, 'next'), f'p2p:admevents:{rows[-1]["id"]}')])
        buttons.append(_back_row(uid))
        await event.edit(text, buttons=buttons)
        return
    if data.startswith('p2p:admevent:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        eid = int(data.split(':')[2])
        row = _one('SELECT * FROM escrow_events WHERE id=?', (eid,))
        if not row:
            await event.edit(T(uid, 'event_missing')); return
        observations = _rows('SELECT direction,amount,status FROM escrow_observations WHERE event_id=? ORDER BY observed_at', (eid,))
        evidence = '\n'.join(f'{r["direction"]} {r["amount"]} · {r["status"]}' for r in observations)
        text = T(uid, 'event_detail', id=eid, pid=row['payment_id'] or T(uid, 'unknown'), direction=row['direction'], amount=row['amount'], status=row['status'], conflict=row['conflict'], evidence=evidence, review=row['review_note'] or T(uid, 'not_reviewed'))
        buttons = [[_btn(T(uid, 'record_evidence'), f'p2p:admreview:{eid}')], [_btn(T(uid, 'events_back'), 'p2p:admevents:0')]]
        await event.edit(text, buttons=buttons, parse_mode=None)
        return
    if data.startswith('p2p:admreview:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        eid = int(data.split(':')[2])
        _wiz[f'adm:{uid}'] = {'m': 'event_review', 'id': eid, 'ts': time.time()}
        await event.edit(T(uid, 'event_review_prompt'))
        return
    if data.startswith('p2p:admcase:'):
        if await _admin_gate(event, uid):
            return
        # loading toast: the case view performs wallet/username lookups
        await event.answer(T(uid, 'adm_loading_case'))
        await admin_case_view(client, uid, int(data.split(':')[2]), edit_event=event)
        return
    if data.startswith('p2p:admchat:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        tid = int(data.split(':')[2])
        await event.edit(chat_summary(tid, 60, uid=uid),
                         buttons=[[_btn(T(uid, 'adm_case_back'), f'p2p:admcase:{tid}')]])
        return
    if data.startswith('p2p:adm:unban:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        tuid = int(data.split(':')[3])
        unban_user(tuid)
        try:
            await notify(client, tuid, T(tuid, 'adm_unban_notify'))
        except Exception:
            pass
        await admin_banlist(client, uid, edit_event=event)
        return
    if data.startswith('p2p:adm:admdel:'):
        if uid != admin_id():
            await event.answer(T(uid, 'adm_owner_only'), alert=True); return
        await event.answer()
        _exec('DELETE FROM p2p_admins WHERE uid=?', (int(data.split(':')[3]),))
        await admin_admins(client, uid, edit_event=event)
        return
    if data == 'p2p:adm':
        if await _admin_gate(event, uid):
            return
        await event.answer()
        await admin_panel(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:disputes':
        if await _admin_gate(event, uid):
            return
        await event.answer()
        await admin_disputes(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:ban':
        if await _admin_gate(event, uid):
            return
        await event.answer()
        _wiz[f'adm:{uid}'] = {'m': 'ban', 'ts': time.time()}
        await event.edit(T(uid, 'adm_ban_prompt'),
                         buttons=[_back_row(uid)])
        return
    if data == 'p2p:adm:banlist':
        if await _admin_gate(event, uid):
            return
        await event.answer()
        await admin_banlist(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:admins':
        if await _admin_gate(event, uid):
            return
        await event.answer()
        await admin_admins(client, uid, edit_event=event)
        return
    if data == 'p2p:adm:admadd':
        if uid != admin_id():
            await event.answer(T(uid, 'adm_owner_only_add'), alert=True); return
        await event.answer()
        _wiz[f'adm:{uid}'] = {'m': 'admadd', 'ts': time.time()}
        await event.edit(T(uid, 'adm_add_prompt'),
                         buttons=[_back_row(uid)])
        return
    if data.startswith('p2p:adm:banok:'):
        if await _admin_gate(event, uid):
            return
        st = _wiz.get(f'adm:{uid}')
        if (not isinstance(st, dict) or st.get('m') != 'ban2'
                or time.time() - (st.get('ts') or 0) > 900):
            await event.answer(T(uid, 'adm_ban_expired'), alert=True); return
        target = int(data.split(':')[3])
        if target != st.get('target'):
            await event.answer(T(uid, 'adm_ban_mismatch'), alert=True); return
        await event.answer()
        _wiz.pop(f'adm:{uid}', None)
        reason = st.get('reason', '')
        ban_user(target, reason)
        try:
            await notify(client, target, T(target, 'adm_ban_notify'))
        except Exception:
            pass
        await audit(client, 0, 'P2P BAN', f'{target} — {reason or "—"} (by {uid})')
        await admin_banlist(client, uid, edit_event=event)
        return
    if data.startswith('p2p:adm:cxlive:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        tid = int(data.split(':')[3])
        tr = get_trade(tid)
        if tr and tr['stage'] == ST_LOCKING:
            cur = _exec("UPDATE trades SET stage=?, updated=? WHERE id=? AND stage=? AND COALESCE(safety_version,0)>=1 AND COALESCE(lock_sent,0)=0",
                        (ST_CANCELED, time.time(), tid, ST_LOCKING))
            if cur.rowcount != 1:
                await event.respond(T(uid, 'adm_cx_inflight'))
                return
            _exec("UPDATE orders SET status='open', taken_at=NULL WHERE id=? AND status='taken'",
                  (tr['order_id'],))
            for party in (tr['buyer'], tr['seller']):
                await notify(client, party, T(party, 'cx_solo'))
            await channel_status(tr['order_id'], '⛔️ لغو شد', delete=True)
            await update_room(client, tid)
            await audit(client, 0, 'P2P ADMIN CANCEL', f'trade #{tid} (by {uid})')
            await event.respond(T(uid, 'adm_cx_done', id=tid))
        else:
            await event.respond(T(uid, 'adm_cx_only_locking'))
        return
    if data.startswith('p2p:legacyack:'):
        if await _admin_gate(event, uid):
            return
        parts = data.split(':')
        if len(parts) != 5 or parts[2] not in ('release', 'refund') or not parts[3].isdigit():
            await event.answer(T(uid, 'confirmation_invalid'), alert=True); return
        action, tid = parts[2], int(parts[3])
        if not consume_escrow_confirmation(uid, tid, 'legacy_' + action, parts[4]):
            await event.answer(T(uid, 'confirmation_invalid'), alert=True); return
        tr = get_trade(tid)
        if not tr or (tr['safety_version'] or 0) >= 1:
            await event.answer(T(uid, 'legacy_none'), alert=True); return
        _exec('INSERT INTO escrow_admin_actions(trade_id,kind,admin_uid,acted_at) VALUES (?,?,?,?)', (tid, 'legacy_ack_' + action, uid, time.time()))
        await event.answer()
        if action == 'release':
            await admin_release(client, tid, legacy_ack=True, actor=uid)
        else:
            await admin_refund(client, tid, legacy_ack=True, actor=uid)
        await event.respond(T(uid, 'legacy_recorded'))
        return
    if data.startswith('p2p:admrel:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if tr and (tr['safety_version'] or 0) < 1:
            nonce = issue_escrow_confirmation(uid, tid, 'legacy_release')
            await event.edit(T(uid, 'legacy_warning'), buttons=[[_btn(T(uid, 'legacy_release'), f'p2p:legacyack:release:{tid}:{nonce}')], [_btn(T(uid, 'cancel_admin'), f'p2p:admcase:{tid}')]])
            return
        await admin_release(client, tid)
        await notify(client, uid, T(uid, 'admin_check_status', id=tid))
        return

    if data.startswith('p2p:admref:'):
        if await _admin_gate(event, uid):
            return
        await event.answer()
        tid = int(data.split(':')[2])
        tr = get_trade(tid)
        if tr and (tr['safety_version'] or 0) < 1:
            nonce = issue_escrow_confirmation(uid, tid, 'legacy_refund')
            await event.edit(T(uid, 'legacy_warning'), buttons=[[_btn(T(uid, 'legacy_refund'), f'p2p:legacyack:refund:{tid}:{nonce}')], [_btn(T(uid, 'cancel_admin'), f'p2p:admcase:{tid}')]])
            return
        await admin_refund(client, tid)
        await notify(client, uid, T(uid, 'admin_check_status', id=tid))
        return

    await event.answer()


async def _edit_event_room(event, uid, tr, confirm):
    if not tr or uid not in (tr['buyer'], tr['seller']):
        return
    await event.edit(room_text(uid, tr), buttons=room_buttons(uid, tr, confirm=confirm))


# ==================== routing: text (wizard + anonymous chat) ====================

async def on_text(client, event, text):
    """Returns True when the p2p section consumed this text."""
    uid = event.sender_id
    init_db()
    if not is_p2p_admin(uid) and not enabled_for(uid):
        return False

    if not text and any(getattr(event, kind, None) for kind in ('media', 'photo', 'document', 'audio', 'video', 'voice')):
        return False

    # wizard capture
    w = _wiz.get(uid)
    if w and isinstance(w, dict) and w.get('step') in ('amount', 'price', 'sats_price', 'method', 'ttl', 'fiat_amount', 'premium', 'unit', 'pmode'):
        # ttl is button-only; free text in that step falls through to chat/wallet
        result = await wiz_text(client, event, uid, text)
        if result and _wiz.get(uid) is w:
            _wiz[uid] = w          # persist mutations back to the shelf
        return result

    # dispute note capture
    key = f'dsp:{uid}'
    if key in _wiz:
        tid = _wiz.pop(key)
        opened = await open_dispute(client, uid, tid, reason=text)
        if opened:
            await event.respond(T(uid, 'dispute_open'))
        return True

    # admin panel input capture (ban uid / add admin)
    akey = f'adm:{uid}'
    if akey in _wiz:
        st = _wiz.pop(akey)
        mode = st.get('m') if isinstance(st, dict) else None
        if not is_p2p_admin(uid) or not mode or time.time() - (st.get('ts') or 0) > 900:
            return True
        if mode == 'event_review':
            if review_escrow_event(uid, st.get('id'), text):
                await event.respond(T(uid, 'event_review_saved'))
            else:
                await event.respond(T(uid, 'event_review_failed'))
            return True
        if mode == 'admadd' and uid != admin_id():
            await event.respond(T(uid, 'adm_owner_only_add'))
            return True
        parts = text.split(None, 1)
        try:
            target = int(parts[0].strip().lstrip('@'))
        except Exception:
            await event.respond(T(uid, 'adm_invalid_uid'))
            _wiz[akey] = {'m': mode, 'ts': time.time()}
            return True
        reason = parts[1].strip() if len(parts) > 1 else ''
        if mode == 'ban':
            if target == uid:
                await event.respond(T(uid, 'adm_ban_self'))
                return True
            _wiz[akey] = {'m': 'ban2', 'target': target, 'reason': reason, 'ts': time.time()}
            lines = [T(uid, 'adm_ban_confirm'), '', T(uid, 'adm_ban_user', target=target)]
            if reason:
                lines.append(T(uid, 'adm_ban_reason', reason=reason))
            kb = [[_btn(T(uid, 'adm_ban_yes'), f'p2p:adm:banok:{target}'), _btn(T(uid, 'adm_ban_no'), 'p2p:adm')]]
            lt = live_trade_of(target)
            if lt:
                lines.append(T(uid, 'adm_ban_active', id=lt['id'], stage=lt['stage']))
                if lt['stage'] == ST_LOCKING:
                    lines.append(T(uid, 'adm_ban_locking_hint'))
                    kb.insert(1, [_btn(T(uid, 'adm_cx_btn', id=lt['id']), f'p2p:adm:cxlive:{lt["id"]}')])
                else:
                    lines.append(T(uid, 'adm_ban_money_hint'))
            await event.respond('\n'.join(lines), buttons=kb)
            return True
        elif mode == 'admadd':
            if is_banned(target):
                await event.respond(T(uid, 'adm_banned_target'))
                return True
            _exec('INSERT OR REPLACE INTO p2p_admins (uid, added_by, at) VALUES (?,?,?)',
                  (target, uid, time.time()))
            try:
                await notify(client, target, T(target, 'adm_added_notify'))
            except Exception:
                pass
            await event.respond(T(uid, 'adm_added', target=target))
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
            await client.send_message(other, T(uid, 'chat_relay_msg',
                                               tid=tr['id'], text=text))
            await event.respond(T(uid, 'msg_sent'))
        except Exception as e:
            logging.error(f'p2p chat relay {uid}: {e}')
            await event.respond(T(uid, 'msg_fail'))
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


async def on_media(client, event):
    """Relay photos only; unsupported media remains with the wallet handler."""
    if not getattr(event, 'photo', None):
        return False
    return await on_photo(client, event)


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
    n = active_trade_count(uid)
    if n > 0:
        badge = T(uid, 'p2p_btn_active', n=n)
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
            f'{T(uid, "amount_l")}: {fmt(o["sats"])} {sats_word(uid)}\n'
            f'{T(uid, "price_l")}: {o["price"]}{method}\n'
            f'{maker_stats_line(uid, o["maker"])}')
    await _send_panel(client, uid, text, buttons=[
        [_btn(T(uid, 'take_yes'), f'p2p:takego:{oid}')],
        [_btn(T(uid, 'back_p2p'), 'p2p:home')]])
