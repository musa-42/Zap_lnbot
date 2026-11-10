from telethon import TelegramClient, events, Button
from keyvalue_sqlite import KeyValueSqlite
from breez_sdk_spark import *
from mnemonic import Mnemonic
import logging
import asyncio
import io
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import sys
import threading
import contextlib
from telethon.tl.types import Channel
from telethon.tl import types as tl_types
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
import re
import qrcode
from io import BytesIO
from cryptography.fernet import Fernet
import base64
import hashlib
import shutil
import getpass
import hmac
import bcrypt
import secrets
import json
import aiohttp
from zap_i18n import t, get_user_lang, set_user_lang, detect_lang_code, init_i18n, LANG_NAMES, SUPPORTED_LANGS
import time as time_module
# item 27: section modules imported at top (no cycle: neither imports the
# bot; both have assignment-only module bodies). Four function-local imports
# remain ON PURPOSE — they are the sys.modules injection seams the offline
# exec-isolation tests patch (token_wd_quote_totals, token_withdraw_reconcile,
# message_handler ×2).
import zap_p2p
import zap_tokens as zt


load_dotenv()

# Enable logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

mnemo = Mnemonic("english")

# ==================== SECURE TOKEN & ENCRYPTION (v30) ====================
# The bot token doubles as the master secret for seed encryption. It is loaded
# from the TELEGRAM_BOT_TOKEN env var (see .env) with a runtime getpass()
# fallback. It is NEVER written to the source file or the database.
bot_token = None

cipher_suite = None          # current Fernet cipher (PBKDF2-derived key)
legacy_cipher_suite = None   # legacy Fernet cipher (single SHA-256) for old seeds

# PBKDF2 work factor for deriving the Fernet key from the bot token.
# The old scheme used a single SHA-256, which lets an attacker try millions of
# token guesses per second if the DB leaks. PBKDF2 with this many iterations
# slows each guess to a crawl (defense in depth on top of a high-entropy token).
ENC_KDF_ITERATIONS = 600_000
ENC_KDF_SALT_DB_KEY = 'enc_kdf_salt'


def _load_or_create_kdf_salt():
    """Load (or create and persist) the PBKDF2 salt.

    The salt is NOT secret — its only job is to be unique to this install so
    precomputed tables can't be reused. keyvalue_sqlite JSON-encodes values, so
    we store the salt hex-encoded (bytes aren't JSON-serializable). Falling back
    to a deterministic value is still strictly stronger than the old single SHA-256.
    """
    try:
        with db_lock:
            stored = db.get(ENC_KDF_SALT_DB_KEY)
            if stored:
                # Accept a hex string (what we write) or, defensively, raw bytes.
                if isinstance(stored, (bytes, bytearray)):
                    return bytes(stored)
                return bytes.fromhex(stored)
            salt = secrets.token_bytes(16)
            db.set(ENC_KDF_SALT_DB_KEY, salt.hex())
            return salt
    except Exception as error:
        logging.warning(f"Falling back to fixed KDF salt: {error}")
        return hashlib.sha256(b'zap-wallet-v30-kdf-salt').digest()


def get_bot_token():
    """Load the bot token from env (.env), prompting at runtime as a fallback."""
    global bot_token
    print("\n" + "=" * 60)
    print("🔐 ZAP WALLET BOT v32 — SECURE STARTUP")
    print("=" * 60)

    env_token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if env_token and ':' in env_token and len(env_token) >= 40:
        bot_token = env_token
        print("✅ Bot token loaded from TELEGRAM_BOT_TOKEN (env / .env)")
        return bot_token

    print("ℹ️  TELEGRAM_BOT_TOKEN not found in environment.")
    try:
        bot_token = getpass.getpass("🤖 Enter Telegram Bot Token (input hidden): ")
    except EOFError:
        bot_token = sys.stdin.readline().strip()
    if not bot_token or ':' not in bot_token or len(bot_token) < 40:
        print("❌ Invalid bot token! Expected format: <id>:<secret>")
        raise SystemExit(1)
    print("✅ Bot token received")
    return bot_token


def _fernet_from_key_material(raw_key: bytes) -> Fernet:
    """Build a Fernet cipher from 32 bytes of key material."""
    return Fernet(base64.urlsafe_b64encode(raw_key))


def init_encryption():
    """Derive the Fernet key(s) from the bot token.

    New seeds are encrypted with a PBKDF2-HMAC-SHA256 derived key (work factor
    ENC_KDF_ITERATIONS). The legacy single-SHA-256 key is kept only so seeds
    written by older bot versions can still be decrypted (and then re-encrypted
    with the new scheme by migrate_existing_seeds()).
    """
    global cipher_suite, legacy_cipher_suite
    if not bot_token:
        raise Exception("Bot token not set!")
    token_bytes = bot_token.encode()

    # New scheme: PBKDF2 with a per-install salt + high iteration count.
    salt = _load_or_create_kdf_salt()
    new_key = hashlib.pbkdf2_hmac('sha256', token_bytes, salt, ENC_KDF_ITERATIONS, dklen=32)
    cipher_suite = _fernet_from_key_material(new_key)

    # Legacy scheme: single SHA-256 (kept for backward-compatible decryption).
    legacy_key = hashlib.sha256(token_bytes).digest()
    legacy_cipher_suite = _fernet_from_key_material(legacy_key)

    print(f"✅ Encryption initialized (PBKDF2-HMAC-SHA256, {ENC_KDF_ITERATIONS:,} iterations)")


def encrypt_seed(seed_phrase):
    """Encrypt a seed phrase with the current (PBKDF2-derived) key."""
    if not seed_phrase or not cipher_suite:
        return None
    return cipher_suite.encrypt(seed_phrase.encode()).decode()


def decrypt_seed(encrypted_seed):
    """Decrypt a seed phrase.

    Tries the current key first, then the legacy SHA-256 key, so seeds written
    by older bot versions still decrypt. Plaintext (12-24 word) seeds are
    returned as-is so the migration step can pick them up and re-encrypt them.
    """
    if not encrypted_seed:
        return None
    # Plaintext mnemonic (old, never-encrypted format)
    if ' ' in encrypted_seed and len(encrypted_seed.split()) in (12, 15, 18, 21, 24):
        return encrypted_seed
    for cipher in (cipher_suite, legacy_cipher_suite):
        if not cipher:
            continue
        try:
            return cipher.decrypt(encrypted_seed.encode()).decode()
        except Exception:
            continue
    # Last resort: treat as plaintext
    return encrypted_seed


def is_seed_encrypted(seed_data):
    """Check if seed is already encrypted"""
    if not seed_data:
        return False
    # Plaintext BIP39 seeds are 12/15/18/21/24 words separated by spaces
    if ' ' in seed_data and len(seed_data.split()) in (12, 15, 18, 21, 24):
        return False
    return True


def _seed_decryptable_with(cipher, token_str):
    """Return True if token_str decrypts cleanly with the given cipher."""
    if not cipher:
        return False
    try:
        cipher.decrypt(token_str.encode()).decode()
        return True
    except Exception:
        return False


def migrate_existing_seeds():
    """Migrate seeds to the current encryption scheme.

    Two cases are handled:
      1. Plaintext (12-24 word) seeds     -> encrypt with the new PBKDF2 key.
      2. Legacy SHA-256-encrypted seeds   -> decrypt with the legacy key and
         re-encrypt with the new PBKDF2 key.
    Seeds already encrypted with the current key are left untouched. Seeds that
    can't be decrypted with either key (e.g. wrong token) are NOT touched, so
    nothing is ever destroyed.
    """
    print("\n🔄 Checking seeds for encryption migration...")

    users = db.get('users')
    if not users:
        print("   No users found")
        return

    encrypted_count = 0      # plaintext -> encrypted
    reencrypted_count = 0    # legacy -> new scheme
    already_current = 0

    for user_id in users:
        try:
            seed_data = db.get(f'{user_id}')
            if not seed_data:
                continue

            if not is_seed_encrypted(seed_data):
                # Plaintext mnemonic -> encrypt with the new key.
                encrypted_seed = encrypt_seed(seed_data)
                if encrypted_seed:
                    db.set(f'{user_id}', encrypted_seed)
                    encrypted_count += 1
                    logging.info(f"   Encrypted plaintext seed for user {user_id}")
                continue

            # It's a Fernet token. Already on the new key?
            if _seed_decryptable_with(cipher_suite, seed_data):
                already_current += 1
                continue

            # Otherwise try the legacy key and re-encrypt with the new key.
            if _seed_decryptable_with(legacy_cipher_suite, seed_data):
                mnemonic = legacy_cipher_suite.decrypt(seed_data.encode()).decode()
                new_encrypted = encrypt_seed(mnemonic)
                if new_encrypted:
                    db.set(f'{user_id}', new_encrypted)
                    reencrypted_count += 1
                    logging.info(f"   Re-encrypted legacy seed for user {user_id} to PBKDF2 scheme")
            else:
                logging.warning(f"   Could not decrypt seed for user {user_id} with any known key (wrong token?)")
        except Exception as e:
            logging.error(f"   Error migrating user {user_id}: {e}")

    print(f"   ✅ Migration complete: {encrypted_count} plaintext encrypted, "
          f"{reencrypted_count} legacy re-encrypted, {already_current} already current")


# ==================== QR CODE GENERATION ====================
def generate_qr_code(data):
    """Generate QR code image and return as bytes (as Telegram image)"""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    # Set filename so Telethon sends as image, not document
    buffer.name = 'qrcode.png'
    return buffer


def decode_qr_image(image_bytes):
    """Decode a QR code from image bytes. Returns the decoded string or None."""
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        results = pyzbar_decode(img)
        for r in results:
            data = r.data.decode('utf-8', errors='replace').strip()
            if data:
                return data
    except Exception as error:
        logging.warning(f"QR decode failed: {error}")
    return None


# ==================== BACKUP CONFIGURATION ====================
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)
BACKUP_INTERVAL_MINUTES = 30  # Backup every 30 minutes
SOURCE_FILE_PATH = __file__
# Owner-approved interim gate (item 6): default ON — backups unchanged.
# Set BACKUP_INCLUDE_SOURCE=0 to stop shipping source files to the channel.
BACKUP_INCLUDE_SOURCE = os.getenv("BACKUP_INCLUDE_SOURCE", "1") == "1"


# Database configuration with lock
DB_PATH = './db-bot.sqlite'
db = KeyValueSqlite(DB_PATH, 'table-name')
db_lock = threading.Lock()  # Thread-safe database access
init_i18n(db)  # i18n per-user language store

# Telegram API credentials (bot_token will be set at runtime)
api_id = os.getenv('TELEGRAM_API_ID')
api_hash = os.getenv('TELEGRAM_API_HASH')
# bot_token is now asked at runtime - see get_bot_token()

# Spark SDK configuration
config = default_config(network=Network.MAINNET)
config.api_key = os.getenv('BREEZ_API_KEY')
config.prefer_spark_over_lightning = True
# v31.1: auto-claim on-chain deposits up to network-recommended fee + 1 sat/vbyte leeway
config.max_deposit_claim_fee = MaxFee(
    onchain=MaxOnchainFee.NETWORK_RECOMMENDED(leeway_sat_per_vbyte=1))
# Cross-chain USDT/USDC (Orchestra/Boltz providers) — disabled by default,
# must opt in (providers run background websockets, mainnet only)
config.cross_chain_config = CrossChainConfig(
    default_slippage_bps=None, default_target_overpay_bps=None)

# Service fee on USDT/USDC withdraw (owner: 0.5% — نشان داده می‌شود در تأیید)
TOKEN_SERVICE_FEE_BPS = 50  # 50 basis points = 0.5%

# Initialize users list in database
db.set_default('users', [])
db.set_default('notification_users', {})  # Database table for persistent notifications
db.set_default('user_fiat', {})  # User's preferred fiat currency
db.set_default('user_send_passwords', {})  # User's send transaction passwords (bcrypt-hashed)
db.set_default('user_password_hints', {})  # User's password hints (plain text)
db.set_default('password_reset_requests', {})  # Password reset requests with timestamp
db.set_default('password_failures', {})  # Failed send-password attempts + lockout (v30)
db.set_default('enc_kdf_salt', None)  # PBKDF2 salt for seed encryption (v30)

# Create client instance (will be started later with bot token)
# Using a placeholder - actual connection happens in main
client = TelegramClient('bot', api_id, api_hash)

# Store the main event loop for SDK callbacks
main_event_loop = asyncio.get_event_loop()

# Patch Breez SDK's event loop getter to use the main loop
# This fixes the "no running event loop" error in SDK callbacks
import breez_sdk_spark.breez_sdk_spark as breez_module
def _patched_get_event_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread, return main loop
        return main_event_loop
breez_module._uniffi_get_event_loop = _patched_get_event_loop

# User state tracking
user_steps = {}
user_data = {}

# ==================== SHARED CONSTANTS (item 9) ====================
# Mechanical extraction of repeatedly-inlined literals; values identical to
# the previous inline constants. Frozen zap/tip-flow sites keep their literals.
SATS_PER_BTC = 100_000_000
MSAT_PER_SAT = 1000
PAYMENT_COMPLETION_TIMEOUT_SECS = 10
# Hold rework (owner order 2026-11): a PENDING/unknown send result is resolved
# by a READ-ONLY background recheck — poll cadence and total observation
# window before the calm still-processing terminal message.
PAYMENT_RECHECK_INTERVAL_SECS = 10
PAYMENT_RECHECK_TOTAL_SECS = 120
# Bounded gate release (2026-11 incident: an already-paid invoice raised a
# conclusive AlreadyExists duplicate which was classified as a send_started
# UNKNOWN; the timer-only recheck aged out and left payment_processing set
# FOREVER — the user could never pay again). When a timer-only (payment_id
# = None) recheck ages out still-unknown, ONE delayed gate release is
# scheduled: it reopens the gate only if the SAME prepare_response is still
# stashed (a newer flow built meanwhile must survive). The already-submitted
# invoice is never auto-retried — a later manual retry hits the network's
# duplicate rejection and _duplicate_payment_error classifies it, so it
# cannot double-pay silently. payment_id!=None rechecks resolve via
# evidence and never use this.
PAYMENT_GATE_RELEASE_SECS = 1800
# Hang guard (2026-11 incident: one wedged SDK call froze every wallet touch
# bot-wide — it held connections_lock/user_lock forever). Read-only SDK calls
# are cancelled after this budget so callers surface a clean error and locks
# release. Sends are NEVER wrapped: a timed-out send is an UNKNOWN outcome
# and must flow to the hold/recheck path, never look retryable.
SDK_READ_TIMEOUT_SECS = 45
SDK_DISCONNECT_TIMEOUT_SECS = 15


async def sdk_read(awaitable, what, timeout_secs=SDK_READ_TIMEOUT_SECS):
    """Await a READ-ONLY SDK call with a hang guard.

    asyncio.wait_for cancels the wedged call and raises a clean TimeoutError,
    so the caller's generic error handling runs and any lock it holds is
    released (async-with releases on cancellation). Only read-only /
    setup / teardown calls may be wrapped — never send_payment, lnurl_pay or
    token withdrawals: cancelling a submitted-but-slow send would turn a
    success into a retryable-looking failure (double-pay risk).
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_secs)
    except asyncio.TimeoutError:
        logging.error(f'SDK read-only call timed out after {timeout_secs}s: {what}')
        raise TimeoutError(f'{what} timed out after {timeout_secs}s')


def donate_user_id():
    """Owner/admin Telegram id from env, read per call (never cached)."""
    try:
        return int(os.getenv('DONATE_USER_ID', '0'))
    except (TypeError, ValueError):
        return 0


# Per-user asyncio lock registry (item 8): serializes check-and-set sequences
# on a user's state (adopted incrementally; see payment-flow handlers).
_user_locks = {}


def user_lock(uid):
    """Return the per-user asyncio.Lock, creating it on first use."""
    lock = _user_locks.get(uid)
    if lock is None:
        lock = _user_locks[uid] = asyncio.Lock()
    return lock


def sanitize_memo(text):
    """Remove non-printable characters from memo text for Lightning invoices"""
    if not text:
        return text
    # Remove zero-width characters and other non-printable chars
    import unicodedata
    # Keep only printable characters
    return ''.join(c for c in text if unicodedata.category(c) not in ('Cc', 'Cf', 'Co', 'Cs'))


# ==================== SEND PASSWORD (v30: bcrypt + lockout) ====================
# Security notes:
#  - Passwords are hashed with bcrypt (salt + cost built in). The previous
#    scheme stored UNSALTED SHA-256 hex, which is trivially brute-forced if the
#    DB ever leaks. Legacy SHA-256 hashes are still verified (constant-time) and
#    then transparently upgraded to bcrypt on the next successful login.
#  - Online guessing is throttled: after MAX_PASSWORD_ATTEMPTS wrong attempts
#    the account is locked for PASSWORD_LOCKOUT_MINUTES minutes. Enforcement
#    lives inside verify_send_password(), so every call site is protected.
BCRYPT_COST = 12
MAX_PASSWORD_ATTEMPTS = 5
PASSWORD_LOCKOUT_MINUTES = 15
_BCRYPT_PW_MAXBYTES = 72  # bcrypt only uses the first 72 bytes of a password


def _bcrypt_hash_password(password: str) -> str:
    """Hash a password with bcrypt (handles the 72-byte limit safely)."""
    pw_bytes = password.encode('utf-8')[:_BCRYPT_PW_MAXBYTES]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt(rounds=BCRYPT_COST)).decode()


def _bcrypt_verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time password verification against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode('utf-8')[:_BCRYPT_PW_MAXBYTES],
                              stored_hash.encode('utf-8'))
    except (ValueError, TypeError):
        return False


def _is_legacy_sha256_hash(stored_hash) -> bool:
    """Detect old unsalted SHA-256 hashes (exactly 64 lowercase hex chars)."""
    return isinstance(stored_hash, str) and len(stored_hash) == 64 and all(
        c in '0123456789abcdef' for c in stored_hash)


def _legacy_sha256(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ---- lockout helpers ----
def _get_password_failures():
    return db.get('password_failures') or {}


def _save_password_failures(data):
    db.set('password_failures', data)


def get_password_lockout_remaining(user_id) -> int:
    """Seconds remaining on the lockout, or 0 if not locked."""
    info = _get_password_failures().get(str(user_id))
    if not info:
        return 0
    locked_until = info.get('locked_until')
    if not locked_until:
        return 0
    try:
        remaining = (datetime.fromisoformat(locked_until) - datetime.now()).total_seconds()
        return max(0, int(remaining))
    except Exception:
        return 0


def is_password_locked(user_id) -> bool:
    return get_password_lockout_remaining(user_id) > 0


def _record_password_failure(user_id):
    """Record a failed attempt and lock if the threshold is reached."""
    data = _get_password_failures()
    key = str(user_id)
    info = data.get(key) or {'count': 0}
    info['count'] = info.get('count', 0) + 1
    if info['count'] >= MAX_PASSWORD_ATTEMPTS:
        info['locked_until'] = (datetime.now() + timedelta(minutes=PASSWORD_LOCKOUT_MINUTES)).isoformat()
        info['count'] = 0
    data[key] = info
    _save_password_failures(data)


def _clear_password_failures(user_id):
    data = _get_password_failures()
    if str(user_id) in data:
        del data[str(user_id)]
        _save_password_failures(data)


def set_user_send_password(user_id, password):
    """Set send password for user (bcrypt-hashed)."""
    passwords = db.get('user_send_passwords') or {}
    passwords[str(user_id)] = _bcrypt_hash_password(password)
    db.set('user_send_passwords', passwords)
    _clear_password_failures(user_id)


def get_user_send_password_hash(user_id):
    """Get hashed send password for user (returns None if not set)."""
    passwords = db.get('user_send_passwords') or {}
    return passwords.get(str(user_id))


def verify_send_password(user_id, password):
    """Verify send password (constant-time, lockout-aware, legacy-upgrading)."""
    # 1) Lockout check — protects every call site automatically.
    if is_password_locked(user_id):
        return False
    stored_hash = get_user_send_password_hash(user_id)
    if not stored_hash:
        return False

    ok = False
    if _is_legacy_sha256_hash(stored_hash):
        # Legacy unsalted SHA-256: constant-time compare, then upgrade to bcrypt.
        ok = hmac.compare_digest(stored_hash, _legacy_sha256(password))
        if ok:
            try:
                passwords = db.get('user_send_passwords') or {}
                passwords[str(user_id)] = _bcrypt_hash_password(password)
                db.set('user_send_passwords', passwords)
                logging.info(f"Upgraded legacy send-password hash for user {user_id} to bcrypt")
            except Exception as e:
                logging.error(f"Failed to upgrade password hash: {e}")
    else:
        ok = _bcrypt_verify_password(password, stored_hash)

    if ok:
        _clear_password_failures(user_id)
    else:
        _record_password_failure(user_id)
    return ok


def has_send_password(user_id):
    """Check if user has set a send password."""
    return get_user_send_password_hash(user_id) is not None


def remove_send_password(user_id):
    """Remove send password for user."""
    passwords = db.get('user_send_passwords') or {}
    if str(user_id) in passwords:
        del passwords[str(user_id)]
        db.set('user_send_passwords', passwords)
    _clear_password_failures(user_id)
    # Also remove hint when removing password
    remove_password_hint(user_id)


def set_password_hint(user_id, hint):
    """Set password hint for user"""
    hints = db.get('user_password_hints') or {}
    hints[str(user_id)] = hint
    db.set('user_password_hints', hints)


def get_password_hint(user_id):
    """Get password hint for user (returns None if not set)"""
    hints = db.get('user_password_hints') or {}
    return hints.get(str(user_id))


def remove_password_hint(user_id):
    """Remove password hint for user"""
    hints = db.get('user_password_hints') or {}
    if str(user_id) in hints:
        del hints[str(user_id)]
        db.set('user_password_hints', hints)


# Password reset after 3 days
PASSWORD_RESET_DAYS = 3


def request_password_reset(user_id):
    """Request password reset - will be processed after PASSWORD_RESET_DAYS days"""
    resets = db.get('password_reset_requests') or {}
    resets[str(user_id)] = {
        'requested_at': datetime.now().isoformat(),
        'reset_at': (datetime.now() + timedelta(days=PASSWORD_RESET_DAYS)).isoformat()
    }
    db.set('password_reset_requests', resets)


def get_password_reset_request(user_id):
    """Get password reset request info for user"""
    resets = db.get('password_reset_requests') or {}
    return resets.get(str(user_id))


def cancel_password_reset(user_id):
    """Cancel password reset request"""
    resets = db.get('password_reset_requests') or {}
    if str(user_id) in resets:
        del resets[str(user_id)]
        db.set('password_reset_requests', resets)


def get_reset_remaining_time(user_id):
    """Get remaining time until password reset (returns None if no request)"""
    reset_info = get_password_reset_request(user_id)
    if not reset_info:
        return None
    reset_at = datetime.fromisoformat(reset_info['reset_at'])
    remaining = reset_at - datetime.now()
    if remaining.total_seconds() <= 0:
        return timedelta(seconds=0)
    return remaining


async def process_password_resets():
    """Process pending password resets (called periodically)"""
    resets = db.get('password_reset_requests') or {}
    now = datetime.now()
    to_remove = []

    for user_id_str, info in resets.items():
        reset_at = datetime.fromisoformat(info['reset_at'])
        if now >= reset_at:
            # Time to reset password
            user_id = int(user_id_str)
            remove_send_password(user_id)
            to_remove.append(user_id_str)
            # Notify user
            try:
                await client.send_message(
                    user_id,
                    t(user_id, "pw_reset_done_notify"),
                    buttons=[[Button.inline(t(user_id, "btn_settings"), b"settings")]]
                )
            except Exception as e:
                logging.debug(f"Could not notify user {user_id} about password reset: {e}")

    # Remove processed resets
    for user_id_str in to_remove:
        del resets[user_id_str]
    if to_remove:
        db.set('password_reset_requests', resets)


async def password_reset_monitor():
    """Background task to process password resets every hour"""
    check_interval = 3600  # Check every hour

    while True:
        try:
            await asyncio.sleep(check_interval)
            await process_password_resets()
        except Exception as e:
            logging.error(f"Error in password reset monitor: {e}")


async def delete_password_message(event):
    """Delete password message securely (for both sides)"""
    try:
        # Try to delete using client.delete_messages for better control
        await client.delete_messages(event.chat_id, [event.id], revoke=True)
    except Exception as e:
        logging.debug(f"Could not delete password message: {e}")
        try:
            await event.delete()
        except:
            pass


def get_sender_info(event):
    """Get sender_id and is_anonymous flag from event.

    Handles cases where user sends as channel (anonymous admin).
    Returns: (sender_id, is_anonymous)
    """
    sender_id = event.sender_id

    # If sender_id exists and is positive, it's a regular user
    if sender_id and sender_id > 0:
        return sender_id, False

    # Check if sender is a Channel (anonymous admin sending as channel)
    if event.sender and isinstance(event.sender, Channel):
        # Use -100 prefix for channel IDs
        channel_id = int(f"-100{event.sender.id}")
        return channel_id, True

    # Fallback: use sender_id if available, or chat_id
    if sender_id:
        return sender_id, sender_id < 0

    # Last resort: use chat_id (for group context)
    if hasattr(event, 'chat_id') and event.chat_id:
        return event.chat_id, True

    return None, False

# Event-based notification system (replaces polling)
# {user_id: {'sdk': sdk_instance, 'listener_id': listener_id}}
active_user_connections = {}
connections_lock = asyncio.Lock()

# Payment deduplication - track notified payment IDs to prevent duplicates
# {payment_id: timestamp} - auto-cleaned after 1 hour
notified_payments = {}
notified_payments_lock = threading.Lock()


def _load_notified_payments(store, now, lifetime_secs=3600):
    """v32 (item 13): restore notified deposit ids from the kv store so a
    restart cannot re-notify payments already announced. Expired and
    malformed entries are dropped; mirrors the 1h in-memory cleanup."""
    restored = {}
    for pid, ts in (store.get('notified_payments') or {}).items():
        try:
            ts = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if (now - ts).total_seconds() <= lifetime_secs:
            restored[pid] = ts
    return restored


def _save_notified_payments(store):
    """v32 (item 13): best-effort persist of the notified-id map. Callers
    must hold notified_payments_lock (threading.Lock is not reentrant)."""
    try:
        store.set('notified_payments',
                  {pid: ts.isoformat() for pid, ts in notified_payments.items()})
    except Exception as e:
        logging.warning(f'notified_payments persist failed: {e}')


notified_payments.update(_load_notified_payments(db, datetime.now()))

# Pending sender info for username payments - so event listener can include sender in notification
# {recipient_id: {'sender_info': str, 'timestamp': datetime, 'amount': int}}
pending_sender_info = {}
pending_sender_info_lock = threading.Lock()

# User activity tracking for 24h timeout
user_last_activity = {}  # {user_id: last_activity_datetime}

# System uptime tracking
bot_start_time = None
bot_username = None  # Will be fetched at startup

# Fiat rates cache
fiat_rates_cache = {
    'rates': {},
    'last_update': None,
    'update_interval': 300  # 5 minutes cache
}

# Pending zap confirmations
pending_zap_confirmations = {}  # {user_id: {'amount_sats': x, 'receiver_id': y, ...}}

# ==================== FF.IO EXCHANGE INTEGRATION ====================
# Env seam (item 5): ff.io credentials come from env/.env only — the public
# copy ships no bundled credentials.
FFIO_API_KEY = os.getenv("FFIO_API_KEY", "")
FFIO_API_SECRET = os.getenv("FFIO_API_SECRET", "")
FFIO_REFCODE = os.getenv("FFIO_REFCODE", "")
FFIO_BASE_URL = "https://ff.io/api/v2/"
FFIO_AFFTAX = 0.5  # affiliate tax percentage

# Cache for ff.io currencies
ffio_currencies_cache = {
    'currencies': None,
    'last_update': None,
    'update_interval': 600  # 10 minutes
}

# Track pending ff.io orders {user_id: {order_id, token, ...}}
pending_ffio_orders = {}


def ffio_sign(data_str):
    """Create HMAC SHA256 signature for ff.io API"""
    return hmac.new(
        FFIO_API_SECRET.encode(),
        data_str.encode(),
        hashlib.sha256
    ).hexdigest()


async def ffio_request(endpoint, data=None):
    """Make authenticated request to ff.io API"""
    if data is None:
        data = {}
    data_json = json.dumps(data)
    signature = ffio_sign(data_json)

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-API-KEY": FFIO_API_KEY,
        "X-API-SIGN": signature,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{FFIO_BASE_URL}{endpoint}",
            data=data_json,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            result = await resp.json()
            return result


async def ffio_get_currencies():
    """Get list of available currencies from ff.io"""
    now = time_module.time()
    cache = ffio_currencies_cache
    if cache['currencies'] and cache['last_update'] and (now - cache['last_update']) < cache['update_interval']:
        return cache['currencies']

    result = await ffio_request("ccies")
    if result and result.get('code') == 0 and result.get('data'):
        currencies = result['data']
        cache['currencies'] = currencies
        cache['last_update'] = now
        return currencies
    return None


async def ffio_get_price(from_ccy, to_ccy, amount, direction="from", rate_type="float"):
    """Get exchange rate quote from ff.io"""
    data = {
        "type": rate_type,
        "fromCcy": from_ccy,
        "toCcy": to_ccy,
        "direction": direction,
        "amount": str(amount),
        "usd": True,
        "refcode": FFIO_REFCODE,
        "afftax": FFIO_AFFTAX
    }
    logging.info(f"ff.io price request: {from_ccy} -> {to_ccy}, amount={amount}, type={rate_type}")
    result = await ffio_request("price", data)
    if result and result.get('code') != 0:
        logging.warning(f"ff.io price error: {result}")
    return result


async def ffio_create_order(from_ccy, to_ccy, amount, to_address, direction="from", rate_type="float", tag=None):
    """Create exchange order on ff.io"""
    data = {
        "type": rate_type,
        "fromCcy": from_ccy,
        "toCcy": to_ccy,
        "direction": direction,
        "amount": str(amount),
        "toAddress": to_address,
        "refcode": FFIO_REFCODE,
        "afftax": FFIO_AFFTAX
    }
    if tag:
        data["tag"] = tag
    return await ffio_request("create", data)


async def ffio_log_order_to_channel(user_id, event, order, direction, from_ccy, to_ccy):
    """Log ff.io order details to backup channel"""
    try:
        # Get user info
        sender = await event.get_sender()
        username = f"@{sender.username}" if sender and sender.username else "N/A"
        first_name = sender.first_name if sender and sender.first_name else ""
        last_name = sender.last_name if sender and sender.last_name else ""
        lang = sender.lang_code if sender and hasattr(sender, 'lang_code') and sender.lang_code else "N/A"

        order_id = order.get('id', 'N/A')
        token = order.get('token', 'N/A')
        from_info = order.get('from', {})
        to_info = order.get('to', {})
        time_info = order.get('time', {})

        log_text = (
            f"🔄 **FF.io Order Created**\n\n"
            f"**User:**\n"
            f"• ID: `{user_id}`\n"
            f"• Username: {username}\n"
            f"• Name: {first_name} {last_name}\n"
            f"• Lang: {lang}\n\n"
            f"**Order:**\n"
            f"• ID: `{order_id}`\n"
            f"• Token: `{token}`\n"
            f"• Direction: {direction}\n"
            f"• Type: {from_ccy} → {to_ccy}\n\n"
            f"**From:**\n"
            f"• Coin: {from_info.get('coin', 'N/A')} ({from_info.get('name', '')})\n"
            f"• Amount: {from_info.get('amount', 'N/A')}\n"
            f"• Address: `{from_info.get('address', 'N/A')}`\n"
            + (f"• Tag: `{from_info.get('tag')}`\n" if from_info.get('tag') else "")
            + f"\n**To:**\n"
            f"• Coin: {to_info.get('coin', 'N/A')} ({to_info.get('name', '')})\n"
            f"• Amount: {to_info.get('amount', 'N/A')}\n"
            f"• Address: `{to_info.get('address', 'N/A')}`\n"
            + (f"• Tag: `{to_info.get('tag')}`\n" if to_info.get('tag') else "")
            + f"\n"
            f"**Time:**\n"
            f"• Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• Expiration: {datetime.fromtimestamp(time_info['expiration']).strftime('%Y-%m-%d %H:%M:%S') if time_info.get('expiration') else 'N/A'}\n"
            f"• Status: NEW"
        )

        await client.send_message(BACKUP_CHANNEL_ID, log_text, parse_mode='md')
    except Exception as e:
        logging.error(f"Error logging ff.io order to channel: {e}")


async def ffio_get_order(order_id, token):
    """Get order status from ff.io"""
    data = {
        "id": order_id,
        "token": token
    }
    return await ffio_request("order", data)


def ffio_group_currencies_by_coin(currencies, direction='recv'):
    """Group ff.io currencies by coin, filter by recv/send availability.
    direction: 'recv' for receive (user sends TO ff.io), 'send' for send (ff.io sends TO user)
    Returns: {coin: [{code, network, name, tag, ...}, ...]}
    """
    grouped = {}
    for ccy in currencies:
        if ccy.get('code') == 'BTCLN':
            continue  # Skip Lightning itself
        # For receive flow (user deposits crypto): need recv=1
        # For send flow (user gets crypto): need send=1
        if direction == 'recv' and not ccy.get('recv'):
            continue
        if direction == 'send' and not ccy.get('send'):
            continue
        coin = ccy.get('coin', ccy['code'])
        if coin not in grouped:
            grouped[coin] = []
        grouped[coin].append(ccy)
    return grouped


# Popular coins shown first (order matters)
FFIO_POPULAR_COINS = [
    'BTC', 'ETH', 'USDT', 'USDC', 'TRX', 'SOL', 'TON',
    'XRP', 'LTC', 'DOGE', 'XMR', 'BNB', 'ADA', 'DOT',
    'AVAX', 'SHIB', 'BCH', 'PEPE'
]


# Number of columns for currency list buttons (adjustable: 2, 3, or 4)
FFIO_BUTTON_COLUMNS = 3


def build_coin_buttons(grouped, prefix, back_data):
    """Build multi-column coin buttons list.
    prefix: 'rcv_coin_' or 'snd_coin_'
    back_data: bytes callback for back button
    """
    coin_items = []
    shown = set()
    for coin in FFIO_POPULAR_COINS:
        if coin in grouped and coin not in shown:
            coin_items.append(coin)
            shown.add(coin)
    for coin in sorted(grouped.keys()):
        if coin not in shown:
            coin_items.append(coin)
            shown.add(coin)

    buttons = []
    row = []
    for coin in coin_items:
        label = coin
        row.append(Button.inline(label, f"{prefix}{coin}".encode()))
        if len(row) == FFIO_BUTTON_COLUMNS:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("« Back", back_data)])
    return buttons


async def monitor_ffio_orders():
    """Background task to monitor pending ff.io orders"""
    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds
            orders_to_remove = []
            for user_id, order_info in list(pending_ffio_orders.items()):
                try:
                    result = await ffio_get_order(order_info['id'], order_info['token'])
                    if result and result.get('code') == 0 and result.get('data'):
                        order_data = result['data']
                        status = order_data.get('status')
                        old_status = order_info.get('last_status')

                        if status != old_status:
                            order_info['last_status'] = status
                            # Notify user of status change
                            status_msgs = {
                                'PENDING': '⏳ Deposit detected, waiting for confirmations...',
                                'EXCHANGE': '🔄 Exchange in progress...',
                                'WITHDRAW': '📤 Sending to destination...',
                                'DONE': '✅ Exchange completed successfully!',
                                'EXPIRED': '⏰ Order expired.',
                                'EMERGENCY': '⚠️ Emergency - order needs attention.',
                            }
                            msg = status_msgs.get(status, f'Status: {status}')
                            direction = order_info.get('direction', 'receive')

                            # Extract tx info
                            from_info = order_data.get('from', {})
                            to_info = order_data.get('to', {})
                            from_tx = from_info.get('tx', {})
                            to_tx = to_info.get('tx', {})
                            from_txid = from_tx.get('id', '')
                            to_txid = to_tx.get('id', '')

                            # User notification
                            tx_text = ""
                            if from_txid:
                                tx_text += f"\n📥 Deposit TX: `{from_txid}`"
                            if to_txid:
                                tx_text += f"\n📤 Withdraw TX: `{to_txid}`"

                            text = (
                                f"🔄 **Exchange Update**\n\n"
                                f"Order: `{order_info['id']}`\n"
                                f"{msg}{tx_text}"
                            )

                            try:
                                await client.send_message(user_id, text)
                            except:
                                pass

                            # Log status update to backup channel
                            try:
                                from_amount = from_info.get('amount', '?')
                                to_amount = to_info.get('amount', '?')
                                from_coin = from_info.get('coin', '?')
                                to_coin = to_info.get('coin', '?')

                                channel_text = (
                                    f"🔄 **FF.io Order Update**\n\n"
                                    f"👤 User: `{user_id}`\n"
                                    f"🆔 Order: `{order_info['id']}`\n"
                                    f"📊 Status: **{old_status}** → **{status}**\n"
                                    f"↔️ {from_amount} {from_coin} → {to_amount} {to_coin}\n"
                                    f"🔀 Direction: {direction}"
                                )
                                if from_txid:
                                    channel_text += f"\n📥 Deposit TX: `{from_txid}`"
                                if to_txid:
                                    channel_text += f"\n📤 Withdraw TX: `{to_txid}`"
                                if from_info.get('address'):
                                    channel_text += f"\n📋 From Addr: `{from_info['address']}`"
                                if to_info.get('address'):
                                    channel_text += f"\n📋 To Addr: `{to_info['address']}`"

                                await client.send_message(BACKUP_CHANNEL_ID, channel_text, parse_mode='md')
                            except Exception as log_e:
                                logging.error(f"Error logging ff.io update to channel: {log_e}")

                            if status in ('DONE', 'EXPIRED', 'EMERGENCY'):
                                orders_to_remove.append(user_id)

                except Exception as e:
                    logging.error(f"Error checking ff.io order for {user_id}: {e}")

            for uid in orders_to_remove:
                pending_ffio_orders.pop(uid, None)

        except Exception as e:
            logging.error(f"Error in ff.io order monitor: {e}")
            await asyncio.sleep(60)

# ==================== END FF.IO INTEGRATION ====================


async def get_yadio_rates():
    """Get fiat rates from Yadio API"""
    try:
        async with aiohttp.ClientSession() as session:
            # Get BTC rate for all currencies
            async with session.get('https://api.yadio.io/exrates/BTC', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # data format: {"BTC": {"USD": 100000, "EUR": 90000, ...}, "timestamp": ...}
                    if 'BTC' in data:
                        return data['BTC']
        return {}
    except Exception as e:
        logging.error(f"Error fetching Yadio rates: {e}")
        return {}


async def get_fiat_rates():
    """Get fiat exchange rates with caching (SDK + Yadio API)"""
    global fiat_rates_cache

    now = datetime.now()

    # Check if cache is valid
    if (fiat_rates_cache['last_update'] and
        (now - fiat_rates_cache['last_update']).total_seconds() < fiat_rates_cache['update_interval'] and
        fiat_rates_cache['rates']):
        return fiat_rates_cache['rates']

    rates_dict = {}

    # Try SDK first
    try:
        users = db.get('users')
        if users:
            sdk = await get_wallet(users[0])
            rates_response = await sdk_read(sdk.list_fiat_rates(), 'list_fiat_rates')
            await close_wallet(sdk)

            for rate in rates_response.rates:
                rates_dict[rate.coin] = rate.value
    except Exception as error:
        logging.error(f"Error fetching SDK fiat rates: {error}")

    # Merge with Yadio API rates (adds more currencies)
    try:
        yadio_rates = await get_yadio_rates()
        for currency, rate in yadio_rates.items():
            if currency not in rates_dict:  # Don't overwrite SDK rates
                rates_dict[currency] = rate
    except Exception as error:
        logging.error(f"Error merging Yadio rates: {error}")

    if rates_dict:
        fiat_rates_cache['rates'] = rates_dict
        fiat_rates_cache['last_update'] = now
        logging.info(f"Updated fiat rates: USD=${rates_dict.get('USD', 'N/A'):,.2f}, total {len(rates_dict)} currencies")
    else:
        # Was silent: a rate cycle that produces nothing must be visible in
        # the journal, or a dead updater looks like fresh quiet operation.
        logging.warning('Fiat rate cycle produced no rates (SDK and Yadio both empty); '
                        f'serving last known rates: {bool(fiat_rates_cache.get("rates"))}')

    return rates_dict if rates_dict else fiat_rates_cache.get('rates', {})


def sats_to_usd(sats, usd_rate):
    """Convert sats to USD"""
    if not usd_rate or usd_rate == 0:
        return None
    # rate is BTC per USD, so: sats / 100_000_000 * rate = USD
    btc = sats / SATS_PER_BTC
    usd = btc * usd_rate
    return round(usd, 2)


def usd_to_sats(usd, usd_rate):
    """Convert USD to sats"""
    if not usd_rate or usd_rate == 0:
        return None
    # rate is BTC per USD, so: USD / rate * 100_000_000 = sats
    btc = usd / usd_rate
    # pinned semantics: float truncation is the spec (see test_money_math_golden.py)
    sats = int(btc * SATS_PER_BTC)
    return sats


def _md_escape(s):
    """v32 (item 14): neutralize Telegram markdown metacharacters in
    user-controlled interpolands (payment memos, display names). Backslash-
    escapes backslash, *, _ and ( — blocking bold/italic injection and
    [text](url) link phishing. Benign text without those bytes is unchanged."""
    for ch in ('\\', '*', '_', '('):
        s = s.replace(ch, '\\' + ch)
    return s


def cross_chain_address_details(dest_details):
    """Build SDK CrossChainAddressDetails from a parse_external_address dict."""
    if not isinstance(dest_details, dict):
        return None
    import breez_sdk_spark as _b
    fam = {"evm": _b.CrossChainAddressFamily.EVM,
           "solana": _b.CrossChainAddressFamily.SOLANA,
           "tron": _b.CrossChainAddressFamily.TRON}.get(
              (dest_details.get("family") or "").lower(),
              _b.CrossChainAddressFamily.EVM)
    return _b.CrossChainAddressDetails(
        address=dest_details["address"], address_family=fam,
        contract_address=dest_details.get("contract"),
        chain_id=dest_details.get("chain_id"), amount=None)


def token_withdraw_auth_binding(user_id, ud):
    """Immutable fingerprint of the exact confirmation the user authorized."""
    return json.dumps([user_id, *[ud.get(k) for k in (
        'tk_confirm_nonce', 'tk_asset', 'tk_chain', 'tk_address', 'tk_amount',
        'tk_dest_details', 'tk_quote')]], sort_keys=True, separators=(',', ':'))


def token_withdraw_claim(user_id, nonce, details=None):
    """Durable single-writer claim; unresolved sends block new withdrawals."""
    import sqlite3
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS token_withdraw_attempts (
            nonce TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL,
            details TEXT NOT NULL, payment_id TEXT NOT NULL, updated REAL NOT NULL)""")
        conn.execute("CREATE TABLE IF NOT EXISTS token_withdraw_migration (id INTEGER PRIMARY KEY CHECK(id=1), actor INTEGER NOT NULL, audited REAL NOT NULL)")
        # A unique quote may never settle two local attempts, even after restart.
        conn.execute("CREATE TABLE IF NOT EXISTS token_withdraw_quote_claims (quote_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE)")
        if not conn.execute("SELECT 1 FROM token_withdraw_migration WHERE id=1").fetchone():
            return False
        blocked = conn.execute("SELECT 1 FROM token_withdraw_attempts WHERE user_id=? "
                               "AND status NOT IN ('safe_failed', 'delivered', 'refunded') LIMIT 1",
                               (user_id,)).fetchone()
        if blocked:
            return False
        try:
            conn.execute("INSERT INTO token_withdraw_attempts VALUES (?, ?, 'claimed', ?, '', ?)",
                         (nonce, user_id, json.dumps(details or {}), time_module.time()))
        except sqlite3.IntegrityError:
            return False
        return True


def token_withdraw_prepare_send(user_id, nonce, quote_id):
    import sqlite3
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT details FROM token_withdraw_attempts WHERE user_id=? AND nonce=? AND status='claimed'", (user_id, nonce)).fetchone()
        if not row:
            raise RuntimeError('withdrawal claim missing')
        details = json.loads(row[0])
        conn.execute('INSERT INTO token_withdraw_quote_claims (quote_id, nonce) VALUES (?, ?)', (quote_id, nonce))
        details['quote_id'] = quote_id
        conn.execute("UPDATE token_withdraw_attempts SET status='sending', details=?, updated=? WHERE user_id=? AND nonce=? AND status='claimed'",
                     (json.dumps(details), time_module.time(), user_id, nonce))


def token_withdraw_finish(user_id, nonce, status, payment_id):
    import sqlite3
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        expected = 'claimed' if status == 'safe_failed' else 'sending'
        result = conn.execute("UPDATE token_withdraw_attempts SET status=?, payment_id=?, updated=? "
                              "WHERE user_id=? AND nonce=? AND status=?",
                              (status, str(payment_id or ''), time_module.time(), user_id, nonce, expected))
        if result.rowcount != 1:
            raise RuntimeError('withdrawal claim missing or already settled')


def token_withdraw_admin(actor):
    try:
        # kept as a direct env read: this function is exec-isolated by the
        # token offline tests (no module constants in its namespace)
        owner = int(os.getenv('DONATE_USER_ID', '0'))
        return owner > 0 and actor == owner
    except (TypeError, ValueError):
        return False


def token_withdraw_register_legacy(actor, user_id):
    """Record a historical uncertain send before opening the migration gate."""
    if not token_withdraw_admin(actor):
        raise PermissionError('owner only')
    import sqlite3
    nonce = 'legacy-' + secrets.token_hex(16)
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS token_withdraw_attempts (
            nonce TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL,
            details TEXT NOT NULL, payment_id TEXT NOT NULL, updated REAL NOT NULL)""")
        conn.execute("INSERT INTO token_withdraw_attempts VALUES (?, ?, 'legacy_unknown', '{}', '', ?)", (nonce, user_id, time_module.time()))
    return nonce


def token_withdraw_migration_ack(actor):
    """Owner attests ALL pre-tracking withdrawals were audited, including untracked ones."""
    if not token_withdraw_admin(actor):
        raise PermissionError('owner only')
    import sqlite3
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS token_withdraw_migration (id INTEGER PRIMARY KEY CHECK(id=1), actor INTEGER NOT NULL, audited REAL NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO token_withdraw_migration VALUES (1, ?, ?)', (actor, time_module.time()))


async def token_withdraw_reconcile(actor, user_id, nonce):
    if not token_withdraw_admin(actor):
        raise PermissionError('owner only')
    import sqlite3
    import zap_tokens as zt
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        row = conn.execute('SELECT status, details, payment_id FROM token_withdraw_attempts WHERE user_id=? AND nonce=?', (user_id, nonce)).fetchone()
    if not row:
        return 'not_found'
    status, raw_details, payment_id = row
    if status in ('delivered', 'refunded', 'safe_failed'):
        return status
    try:
        details = json.loads(raw_details)
        quote_id = details.get('quote_id')
        quote = details.get('quote')
        if not quote_id or not quote:
            return 'manual_review'
        sdk = await get_wallet(user_id)
        try:
            payment = await zt.find_withdrawal_payment(sdk, quote_id, payment_id)
        finally:
            await close_wallet(sdk)
        outcome = zt.withdrawal_evidence(payment, quote_id, quote)
        if outcome not in ('delivered', 'refunded'):
            if outcome in ('refund_needed', 'partial_delivery'):
                try:
                    await client.send_message(user_id, token_withdraw_notice(user_id, outcome, nonce))
                except Exception:
                    logging.warning('token withdrawal hold notification failed')
            return outcome if outcome in ('pending', 'refund_needed', 'failed_unproven', 'partial_delivery') else 'manual_review'
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS token_withdraw_audit (nonce TEXT NOT NULL, user_id INTEGER NOT NULL, actor INTEGER NOT NULL, outcome TEXT NOT NULL, payment_id TEXT NOT NULL, at REAL NOT NULL)')
            updated = conn.execute('UPDATE token_withdraw_attempts SET status=?, payment_id=?, updated=? WHERE user_id=? AND nonce=? AND status=? AND details=? AND payment_id=?',
                                   (outcome, payment.id, time_module.time(), user_id, nonce, status, raw_details, payment_id))
            if not updated.rowcount:
                return 'changed_retry_review'
            conn.execute('INSERT INTO token_withdraw_audit VALUES (?, ?, ?, ?, ?, ?)', (nonce, user_id, actor, outcome, payment.id, time_module.time()))
        try:
            await client.send_message(user_id, token_withdraw_notice(user_id, outcome, nonce))
        except Exception:
            logging.warning('token withdrawal review notification failed')
        return outcome
    except Exception:
        logging.warning('token withdrawal reconciliation unavailable; hold retained')
        return 'manual_review'


def token_withdraw_notice(user_id, state, ref=''):
    fa = get_user_lang(user_id) == 'fa'
    texts = {
        'pending': ('⏳ ارسال ثبت شد، اما رسیدن به مقصد تأیید نشده؛ برای بررسی با پشتیبانی تماس بگیر. برداشت بعدی مسدود است.',
                    '⏳ Submitted, but destination delivery is not confirmed. Contact support for reconciliation; further withdrawals are held.'),
        'claimed': ('⚠️ برداشت آماده‌سازی‌شده پس از قطع برنامه نیاز به بررسی دارد؛ دوباره تلاش نکن.',
                    '⚠️ A prepared withdrawal requires review after interruption; do not retry.'),
        'sending': ('⚠️ برداشت ممکن است ارسال شده باشد؛ دوباره تلاش نکن و با پشتیبانی تماس بگیر.',
                    '⚠️ The withdrawal may have been sent; do not retry. Contact support.'),
        'unknown': ('⚠️ وضعیت برداشت نامشخص است؛ ممکن است ارسال شده باشد. دوباره تلاش نکن و با پشتیبانی وضعیت را بررسی کن.',
                    '⚠️ Withdrawal outcome is unknown and may have been sent. Do not retry; contact support to reconcile it.'),
        'blocked': ('⚠️ برداشت‌ها به دلیل بررسی برداشت قبلی یا بررسی تاریخی مسدود است؛ با پشتیبانی تماس بگیر. وضعیت: /token_status',
                    '⚠️ Withdrawals are held for a previous attempt or historical settlement audit. Contact support; do not retry. Status: /token_status'),
        'failed': ('❌ برداشت انجام نشد. وضعیت و موجودی را پیش از تلاش دوباره بررسی کن.',
                   '❌ Withdrawal failed. Check its status and balance before trying again.'),
        'delivered': ('✅ برداشت به مقصد رسیده و بررسی شده است.', '✅ Withdrawal delivery was confirmed after review.'),
        'refunded': ('↩️ برداشت برگشت داده شده و بررسی شده است.', '↩️ Withdrawal refund was confirmed after review.'),
        'refund_needed': ('⚠️ برداشت نیاز به بازپرداخت دارد؛ هنوز برگشت آن تأیید نشده است. برداشت بعدی مسدود است.',
                          '⚠️ Refund action is needed but the refund is not confirmed. Further withdrawals remain held.'),
        'partial_delivery': ('⚠️ مبلغ رسیده کمتر از برآورد است؛ برداشت بعدی تا بررسی مسدود است.',
                             '⚠️ Less than the estimated amount was delivered. Further withdrawals remain held for review.'),
        'quote_changed': ('⚠️ مبلغ یا کارمزد تغییر کرده؛ برداشت انجام نشد. دوباره قیمت بگیر.',
                          '⚠️ Amount or fee changed; nothing was sent. Request a new quote.'),
    }
    text = texts[state][0 if fa else 1]
    return text + (f"\n\nRef: `{ref}`" if ref else '')


async def token_wd_quote_totals(sdk, asset, chain, addr, amount, svc_fee_sats,
                                dest_details=None):
    """Quote the USDB debit and fingerprint before confirmation."""
    try:
        import zap_tokens as zt
        address_details = cross_chain_address_details(dest_details)
        routes = zt.pick_routes(await zt.get_routes(sdk, direction='send',
                                                  address_details=address_details), asset)
        entry = next(r for r in routes if r['chain'].lower() == chain.lower())
        prep, entry, spark_asset, amount_in_needed, _req = await zt.prepare_withdraw(
            sdk, asset, chain, addr, zt.to_base_units(amount, 6),
            address_details)
        pm = getattr(prep, "payment_method", None)
        fee_base = int(pm.fee_amount)
        estimated_out = int(pm.estimated_out)
        transfer_fee_sats = int(pm.source_transfer_fee_sats)
        if amount_in_needed <= 0:
            raise ValueError('missing withdrawal input amount')
        if estimated_out <= 0:
            raise ValueError('missing withdrawal output amount')
        quote = zt.withdraw_quote(prep, entry['pair'], addr, zt.USDB_TOKEN_ID)
        fee_h = zt.format_base_units(fee_base, entry["decimals"])
        return fee_h, None, quote
    except Exception as quote_err:
        logging.info(f"token_wd_quote: {quote_err}")
        return None, None, None


async def get_usd_rate():
    """Get current USD rate"""
    rates = await get_fiat_rates()
    return rates.get('USD', None)


def get_user_fiat(user_id):
    """Get user's preferred fiat currency, defaults to USD"""
    user_fiat = db.get('user_fiat')
    return user_fiat.get(str(user_id), 'USD')


def set_user_fiat(user_id, fiat_code):
    """Set user's preferred fiat currency"""
    user_fiat = db.get('user_fiat')
    user_fiat[str(user_id)] = fiat_code
    db.set('user_fiat', user_fiat)


# Currency emoji flags mapping
FIAT_EMOJI = {
    'USD': '🇺🇸', 'EUR': '🇪🇺', 'GBP': '🇬🇧', 'JPY': '🇯🇵', 'CHF': '🇨🇭',
    'CAD': '🇨🇦', 'AUD': '🇦🇺', 'CNY': '🇨🇳', 'INR': '🇮🇳', 'RUB': '🇷🇺',
    'TRY': '🇹🇷', 'AED': '🇦🇪', 'BRL': '🇧🇷', 'KRW': '🇰🇷', 'MXN': '🇲🇽',
    'SGD': '🇸🇬', 'HKD': '🇭🇰', 'NOK': '🇳🇴', 'SEK': '🇸🇪', 'DKK': '🇩🇰',
    'PLN': '🇵🇱', 'ZAR': '🇿🇦', 'THB': '🇹🇭', 'MYR': '🇲🇾', 'IDR': '🇮🇩',
    'PHP': '🇵🇭', 'VND': '🇻🇳', 'CZK': '🇨🇿', 'ILS': '🇮🇱', 'CLP': '🇨🇱',
    'ARS': '🇦🇷', 'COP': '🇨🇴', 'PEN': '🇵🇪', 'NGN': '🇳🇬', 'EGP': '🇪🇬',
    'PKR': '🇵🇰', 'BDT': '🇧🇩', 'UAH': '🇺🇦', 'KZT': '🇰🇿', 'SAR': '🇸🇦',
    'QAR': '🇶🇦', 'KWD': '🇰🇼', 'BHD': '🇧🇭', 'OMR': '🇴🇲', 'JOD': '🇯🇴',
    'TWD': '🇹🇼', 'NZD': '🇳🇿', 'HUF': '🇭🇺', 'RON': '🇷🇴', 'BGN': '🇧🇬',
    'HRK': '🇭🇷', 'ISK': '🇮🇸', 'IRT': '🇮🇷', 'IRR': '🇮🇷', 'IQD': '🇮🇶', 'LBP': '🇱🇧',
}


def get_fiat_emoji(fiat_code):
    """Get emoji flag for fiat currency"""
    return FIAT_EMOJI.get(fiat_code, '💱')


async def get_fiat_rate(fiat_code):
    """Get rate for a specific fiat currency"""
    rates = await get_fiat_rates()
    return rates.get(fiat_code, None)


def sats_to_fiat(sats, fiat_rate):
    """Convert sats to fiat currency"""
    if not fiat_rate or fiat_rate == 0:
        return None
    btc = sats / SATS_PER_BTC
    fiat_value = btc * fiat_rate
    return round(fiat_value, 2)


async def format_balance_with_fiat(balance_sats, user_id):
    """Format balance with user's preferred fiat equivalent"""
    balance_int = int(balance_sats)
    unit = t(user_id, "sats_unit")
    fiat_code = get_user_fiat(user_id)
    fiat_rate = await get_fiat_rate(fiat_code)
    if fiat_rate:
        fiat_value = sats_to_fiat(balance_int, fiat_rate)
        if fiat_value is not None:
            symbol = "$" if fiat_code == "USD" else ""
            # For large values (low-value currencies like IRT), show without decimals
            if fiat_value >= 1000:
                return f"{balance_int:,} {unit} (~{symbol}{fiat_value:,.0f} {fiat_code})"
            else:
                return f"{balance_int:,} {unit} (~{symbol}{fiat_value:,.2f} {fiat_code})"
    return f"{balance_int:,} {unit}"


async def format_balance_with_usd(balance_sats, user_id=None):
    """Format balance with USD equivalent (unit word follows the user's lang)"""
    unit = t(user_id, "sats_unit")
    usd_rate = await get_usd_rate()
    if usd_rate:
        usd_value = sats_to_usd(int(balance_sats), usd_rate)
        if usd_value is not None:
            return f"{balance_sats} {unit} (~${usd_value} USD)"
    return f"{balance_sats} {unit}"


async def format_amount_with_usd(amount_sats, user_id=None):
    """Format amount with USD equivalent (unit word follows the user's lang)"""
    unit = t(user_id, "sats_unit")
    usd_rate = await get_usd_rate()
    if usd_rate:
        usd_value = sats_to_usd(int(amount_sats), usd_rate)
        if usd_value is not None:
            return f"{amount_sats} {unit} (~${usd_value})"
    return f"{amount_sats} {unit}"


# ==================== HELPER: Format Payment Details ====================
def format_timestamp(timestamp):
    """Format timestamp to readable string"""
    if timestamp:
        try:
            if timestamp > 1000000000000:
                dt = datetime.fromtimestamp(timestamp / 1000)
            else:
                dt = datetime.fromtimestamp(timestamp)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return 'Unknown'
    return 'Unknown'


def get_status_icon_and_text(status, user_id=None):
    """Get status emoji and localized text from payment status"""
    status_str = str(status).lower()
    if 'complete' in status_str or 'succeeded' in status_str:
        return "✅", t(user_id, "st_success")
    elif 'pending' in status_str:
        return "⏳", t(user_id, "st_pending")
    elif 'failed' in status_str:
        return "❌", t(user_id, "st_failed")
    else:
        return "⚪", str(status)


# ==================== POST-PAYMENT TIP PROMPT (donate after success) ====================
# After any successful SEND payment, show a separate thank-you message with
# donation quick-amount buttons. Fires at most once every TIP_PROMPT_COOLDOWN
# hours per user, and NEVER after donation payments themselves (no loops).
TIP_AMOUNTS = [100, 500, 1000]
TIP_PROMPT_COOLDOWN_HOURS = 72
last_tip_prompt = {}   # {user_id: datetime}


async def show_tip_prompt(user_id, chat_id=None):
    """Send a friendly donation prompt after a successful payment (cooldown-gated).

    - Skips when user just donated (user_steps == 'donate_amount' flow clears data
      before this runs; we rely on the flag set by the donate branch).
    - Cooldown: once per TIP_PROMPT_COOLDOWN_HOURS per user.
    """
    try:
        if user_data.get(user_id, {}).get('just_donated'):
            user_data.get(user_id, {}).pop('just_donated', None)
            return

        now = datetime.now()
        last = last_tip_prompt.get(user_id)
        if last and (now - last) < timedelta(hours=TIP_PROMPT_COOLDOWN_HOURS):
            return
        last_tip_prompt[user_id] = now

        target = chat_id or user_id
        amount_row = [Button.inline(f"{a:,} ⚡", f"tip_amount_{a}".encode()) for a in TIP_AMOUNTS]
        await client.send_message(
            target,
            t(user_id, "tip_prompt_text"),
            buttons=[
                amount_row,
                [Button.inline(t(user_id, "btn_custom_amount"), b"tip_custom")],
                [Button.inline(t(user_id, "btn_later"), b"tip_dismiss")]
            ]
        )
    except Exception as e:
        logging.debug(f"Tip prompt skipped: {e}")


# ==================== BATCH PAY (durable per-item persistence) ====================

@contextlib.contextmanager
def _batch_db():
    """Durable batch queue (idempotent — old DBs migrate on first open)."""
    import sqlite3
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute('''CREATE TABLE IF NOT EXISTS batch_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            destination TEXT NOT NULL, amount INTEGER NOT NULL,
            route_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued','sent','failed','unknown')),
            payment_id TEXT)''')
        with con:
            yield con
    finally:
        con.close()


def _batch_fingerprint(kind, amount, destination):
    """Bind the exact confirmed intent: route kind + amount + destination.

    SDK payment objects are NEVER stringified into this (read_token risk)."""
    return hashlib.sha256(f'{kind}|{int(amount)}|{destination}'.encode()).hexdigest()


def _batch_add(batch_id, user_id, destination, amount, fingerprint):
    """Persist one confirmed item at confirmation time (status queued)."""
    with _batch_db() as con:
        cur = con.execute("INSERT INTO batch_items (batch_id,user_id,destination,amount,route_fingerprint,status) "
                          "VALUES (?,?,?,?,?, 'queued')",
                          (batch_id, user_id, destination, int(amount), fingerprint))
        return cur.lastrowid


def _batch_rows(batch_id, user_id, statuses=None):
    q = 'SELECT id,destination,amount,route_fingerprint,status,payment_id FROM batch_items ' \
        'WHERE batch_id=? AND user_id=?'
    args = [batch_id, user_id]
    if statuses:
        q += ' AND status IN (%s)' % ','.join('?' * len(statuses))
        args.extend(statuses)
    with _batch_db() as con:
        return con.execute(q + ' ORDER BY id', args).fetchall()


def _batch_claim(item_id):
    """Atomic claim: only a queued/failed row with NO payment_id. A crash after
    the claim leaves payment_id set, so the row can never be auto-retried."""
    token = 'claim-' + secrets.token_hex(12)
    with _batch_db() as con:
        claimed = con.execute("UPDATE batch_items SET payment_id=? WHERE id=? "
                              "AND status IN ('queued','failed') "
                              "AND (payment_id IS NULL OR payment_id='')",
                              (token, item_id)).rowcount == 1
    return token if claimed else None


def _batch_finish(item_id, status, payment_id=None):
    with _batch_db() as con:
        con.execute('UPDATE batch_items SET status=?, payment_id=? WHERE id=?',
                    (status, payment_id, item_id))


def _batch_clear(user_id):
    """queue_clear: also clear the durable batch queue."""
    with _batch_db() as con:
        con.execute('DELETE FROM batch_items WHERE user_id=?', (user_id,))


def _batch_abandon(user_id):
    """Starting a new batch: any queued/failed rows of earlier batches are held
    as unknown — their confirmed route is gone and they must never silently re-send."""
    with _batch_db() as con:
        cur = con.execute("UPDATE batch_items SET status='unknown' WHERE user_id=? "
                          "AND status IN ('queued','failed')", (user_id,))
        return cur.rowcount


def _batch_recover_orphans():
    """Boot recovery: queued/failed rows from a previous run have no live route — hold them."""
    with _batch_db() as con:
        cur = con.execute("UPDATE batch_items SET status='unknown' WHERE status IN ('queued','failed')")
        return cur.rowcount


async def _admin_alert(text):
    """Best-effort DM to the admin/owner (DONATE_USER_ID) about held funds or
    reconciliation needs. Never raises into the caller."""
    try:
        admin_id = donate_user_id()
        if admin_id > 0:
            await client.send_message(admin_id, text)
    except Exception as e:
        logging.error(f'_admin_alert failed: {e}')


async def _batch_send_item(user_id, entry):
    """Send ONE confirmed batch item using its add-time route (the route is never
    re-resolved at send time). Returns (status, payment_id)."""
    submitted = False
    try:
        kind = entry.get('kind')
        prep = entry.get('prepare_response')
        if prep is None:
            return 'failed', None
        submitted = True
        if kind in ('lightning-address', 'lnurl-pay'):
            payment = await lnurl_pay(user_id, prep)
        else:
            payment = await send_payment(user_id, prep, entry.get('options'))
        ok, reason = is_payment_successful(payment)
        pid = getattr(payment, 'id', None)
        pid = pid if isinstance(pid, str) else None
        if ok:
            return 'sent', pid
        if reason == 'failed':
            return 'failed', pid
        return 'unknown', pid
    except Exception:
        return ('unknown' if submitted else 'failed'), None


async def _batch_send_all(event, user_id):
    """Send every queued/failed item of the user's current batch.

    Each item was bound (destination + amount + route fingerprint) at
    confirmation; every send is claimed atomically and verified against the
    returned status. Unknown outcomes are held and never auto-retried."""
    ud = user_data.get(user_id) or {}
    batch_id = ud.get('batch_id')
    entries = ud.get('batch_items') or []
    if not batch_id or not entries:
        await event.edit(t(user_id, "batch_empty"), buttons=None)
        return
    # send-password gate — identical check and hash binding to normal payments
    if has_send_password(user_id):
        auth = ud.get('batch_auth')
        if auth != [batch_id, get_user_send_password_hash(user_id)]:
            ud['batch_auth_pending'] = batch_id
            user_steps[user_id] = 'verify_send_password_for_batch'
            await event.answer()
            await event.respond(t(user_id, 'batch_password_required'))
            return
    await event.answer(t(user_id, "batch_sending"))
    progress = await event.edit(t(user_id, "batch_sending_progress", i=0, n=len(entries)))
    ok = fail = held = 0
    total = 0
    by_id = {e.get('row_id'): e for e in entries}
    rows = _batch_rows(batch_id, user_id)
    for i, row in enumerate(rows, 1):
        item_id, dest, amount, fingerprint, status, payment_id = row
        if status == 'sent':
            ok += 1
            total += int(amount)
            continue
        if status == 'unknown':
            held += 1
            continue
        if status != 'failed' and status != 'queued':
            continue
        if payment_id:
            # claim residue from a crash: never auto-retry an ambiguous send
            _batch_finish(item_id, 'unknown', payment_id)
            held += 1
            await _admin_alert(f'batch item {item_id} of user {user_id} had an unfinished claim — held')
            continue
        entry = by_id.get(item_id)
        if (entry is None or entry.get('dest') != dest
                or int(entry.get('amount') or -1) != int(amount)
                or entry.get('fingerprint') != fingerprint
                or _batch_fingerprint(entry.get('kind'), amount, dest) != fingerprint):
            _batch_finish(item_id, 'unknown', payment_id)
            held += 1
            await _admin_alert(f'batch item {item_id} of user {user_id} lost its confirmed route — held')
            continue
        token = _batch_claim(item_id)
        if not token:
            held += 1
            continue
        send_status, pid = await _batch_send_item(user_id, entry)
        if send_status == 'sent':
            _batch_finish(item_id, 'sent', pid)
            ok += 1
            total += int(amount)
        elif send_status == 'failed':
            _batch_finish(item_id, 'failed', None)  # conclusive: retry allowed
            fail += 1
        else:
            _batch_finish(item_id, 'unknown', token)  # ambiguous: hold, never retry
            held += 1
            logging.error('Batch item %s requires manual reconciliation: user=%s batch=%s claim=%s',
                          item_id, user_id, batch_id, token)
        try:
            await progress.edit(t(user_id, "batch_progress", i=i, n=len(rows),
                                   ok=ok, fail=fail, held=held))
        except Exception:
            pass
    user_steps.pop(user_id, None)
    done = t(user_id, "batch_done", ok=ok, fail=fail, held=held, total=f"{total:,}")
    if held:
        done += '\n\n' + t(user_id, 'batch_hold')
    await progress.edit(done, buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])


def is_payment_successful(payment):
    """Only the SDK's explicit COMPLETED status proves payment success."""
    status = getattr(payment, 'status', None)
    details = getattr(payment, 'details', None)
    htlc = getattr(details, 'htlc_details', None)
    htlc_status = getattr(htlc, 'status', None)
    htlc_name = getattr(htlc_status, 'name', htlc_status)
    if htlc_name in ('RETURNED', 'FAILED') and status is PaymentStatus.COMPLETED:
        return False, 'unknown'
    if status is PaymentStatus.COMPLETED:
        return True, None
    if status is PaymentStatus.FAILED:
        return False, 'failed'
    if status is PaymentStatus.PENDING:
        return False, 'pending'
    return False, 'unknown'


def _duplicate_payment_error(err):
    """Conclusive duplicate-submission rejection (2026-11 incident).

    The SDK raises this when the user confirms an ALREADY-PAID invoice: the
    network rejects the new request outright, so NOTHING new was sent and the
    outcome is NOT unknown — the caller must release the duplicate-send gate
    instead of wedging it behind a timer-only recheck. Matches ONLY this
    explicit signature (case-insensitive, robust to the SDK's nested gRPC
    quoting — the details bytes containing DUPLICATE_OPERATION appear inside
    str(err) as logged); any other error, including a plain TimeoutError,
    stays an unknown outcome and keeps the old recheck path.
    """
    try:
        s = str(err)
    except Exception:
        return False
    s = s.lower()
    if 'duplicate_operation' in s:
        return True
    return ('alreadyexists' in s
            and ('preimage request already exists' in s or 'paymenthash' in s))


# ==================== PAYMENT RECHECK (read-only outcome poller) ====================
# Owner order (2026-11): a PENDING/unknown SDK send result must never be shown
# to the user as 'payment_hold'. The message is edited to a neutral
# 'processing' text and this background task resolves it by polling the SDK
# READ-ONLY (get_payment_by_id). It never retries and never refunds — the only
# writes are message edits and duplicate-send-gate cleanup.
_PAYMENT_RECHECK_TASKS = set()


def _payment_success_text(amount_display, fee_display, payment_id, user_id=None):
    """Success text shared by the live send path and the recheck task."""
    return t(user_id, "pay_success",
             amount=amount_display, fee=fee_display, payment_id=payment_id)


def _payment_success_buttons(user_id, payment_id):
    return [
        [Button.inline(t(user_id, "btn_pay_details"), f"track_{payment_id}".encode())],
        [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
    ]


def _lnurl_success_text(amount_display, fee_display, user_id=None):
    return t(user_id, "pay_success_lnurl",
             amount=amount_display, fee=fee_display)


def _ffio_success_text(order_id, to_ccy, user_id=None):
    return t(user_id, "ff_pay_success", order_id=order_id, to_ccy=to_ccy)


def _ffio_success_buttons(user_id, order_id):
    return [[Button.inline(t(user_id, "btn_check_status"), f"ffio_st_{order_id}".encode())]]


def _donation_success_text(amount_display, fee_display, user_id=None):
    return t(user_id, "donate_success",
             amount=amount_display, fee=fee_display)


async def _safe_edit_message(chat_id, message_id, text, buttons=None):
    """Edit a message from a background task; a deleted/inaccessible message
    must never crash the recheck."""
    try:
        await client.edit_message(chat_id, message_id, text, buttons=buttons)
    except Exception as e:
        logging.warning(f'recheck could not edit message {message_id} in {chat_id}: {e}')


async def _payment_recheck_gate_cleanup(user_id, prepare_response, manage_gate,
                                        order_info, failed):
    """Clear duplicate-send guards ONLY after a conclusive recheck result.

    Money-safety decision (documented per owner review): while a send's
    outcome is unresolved the payment_processing gate MUST stay set — it is
    the only thing stopping a second tap from re-sending the same invoice, so
    the still-unknown terminal of the recheck never calls this. It runs once
    the poll observed COMPLETED or FAILED. The stashed prepare_response is
    dropped only if it is still THIS payment's object: the user may have
    built a newer send flow while gated and that one must survive (the live
    path's whole-dict `del user_data[user_id]` is intentionally not repeated
    here for the same reason).
    """
    try:
        if order_info is not None and failed:
            # Conclusive fail: reopen the ff.io pay button. On success the
            # gate stays set — the order is still owed its goods, no double-pay.
            order_info['payment_processing'] = False
        if not manage_gate:
            return
        async with user_lock(user_id):
            ud = user_data.get(user_id)
            if not isinstance(ud, dict):
                return
            ud['payment_processing'] = False
            if prepare_response is not None and ud.get('prepare_response') is prepare_response:
                ud.pop('prepare_response', None)
                user_steps.pop(user_id, None)
    except Exception as e:
        logging.warning(f'payment recheck gate cleanup failed for user {user_id}: {e}')


async def _payment_gate_release_after(user_id, prepare_response):
    """Bounded auto-release of the duplicate-send gate after a timer-only
    (payment_id=None) recheck aged out still-unknown.

    Without this the gate stayed set forever with no user escape. The gate is
    cleared ONLY if it is still set AND the stashed prepare_response is still
    the SAME object — a newer flow built meanwhile must survive untouched.
    This NEVER sends or retries anything; a later manual retry of the same
    invoice will be rejected by the network and caught by
    _duplicate_payment_error instead of double-paying silently.
    """
    try:
        await asyncio.sleep(PAYMENT_GATE_RELEASE_SECS)
        async with user_lock(user_id):
            ud = user_data.get(user_id)
            if (isinstance(ud, dict) and ud.get('payment_processing')
                    and ud.get('prepare_response') is prepare_response):
                ud['payment_processing'] = False
                ud.pop('prepare_response', None)
                user_steps.pop(user_id, None)
                logging.warning(
                    'payment gate auto-released after unknown-outcome window uid=%s '
                    '(invoice already submitted once; user accepts responsibility on retry)',
                    user_id)
    except Exception as e:
        logging.warning(f'payment gate auto-release failed for user {user_id}: {e}')


async def _payment_recheck_task(user_id, chat_id, message_id, payment_id,
                                success_text, success_buttons=None, amount=None,
                                fee=0, prepare_response=None, manage_gate=False,
                                order_info=None):
    """Poll a PENDING/unknown send read-only and resolve the held message.

    Every PAYMENT_RECHECK_INTERVAL_SECS, up to PAYMENT_RECHECK_TOTAL_SECS:
    COMPLETED → the same success text/buttons the live path shows;
    FAILED → 'payment_failed'; still unknown after the window → ONE calm
    'payment_still_processing' edit (payment ID shown) and the duplicate-send
    gate stays set through the window. This task NEVER sends, retries or
    refunds. A payment_id of None/'N/A' (raised send, no payment object) runs
    timer-only: scanning history to guess which payment is ours is not
    reliable enough to claim success — same reasoning as
    zap_tokens.find_withdrawal_payment — so it can only age out to the
    still-processing message; such a timer-only aging-out additionally
    schedules ONE bounded gate release (see _payment_gate_release_after) so
    the user is not gated out of paying forever.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PAYMENT_RECHECK_TOTAL_SECS
    can_poll = isinstance(payment_id, str) and payment_id and payment_id != 'N/A'
    while loop.time() < deadline:
        await asyncio.sleep(PAYMENT_RECHECK_INTERVAL_SECS)
        if loop.time() >= deadline or not can_poll:
            continue
        payment = None
        try:
            payment = await get_payment_by_id(user_id, payment_id)
        except Exception as e:
            logging.warning(f'payment recheck poll error user={user_id} payment={payment_id}: {e}')
        if payment is None:
            logging.warning(f'payment recheck poll unresolved user={user_id} payment={payment_id}')
            continue
        is_success, failure_reason = is_payment_successful(payment)
        if is_success:
            if isinstance(amount, int) and amount > 0:
                track_volume('out', amount + (fee or 0))
            await _safe_edit_message(chat_id, message_id, success_text,
                                     buttons=success_buttons)
            await _payment_recheck_gate_cleanup(user_id, prepare_response,
                                                manage_gate, order_info, failed=False)
            return
        if failure_reason == 'failed':
            await _safe_edit_message(chat_id, message_id, t(user_id, 'payment_failed'),
                                     buttons=None)
            await _payment_recheck_gate_cleanup(user_id, prepare_response,
                                                manage_gate, order_info, failed=True)
            return
    await _safe_edit_message(chat_id, message_id,
                             t(user_id, 'payment_still_processing', payment_id=payment_id or 'N/A'),
                             buttons=None)
    if manage_gate and prepare_response is not None and not can_poll:
        # Timer-only unknown (no payment object ever came back): keep the gate
        # through the observation window, then ONE bounded release so the user
        # is not locked out of paying forever. payment_id!=None rechecks
        # resolve via evidence and never schedule this.
        task = asyncio.create_task(
            _payment_gate_release_after(user_id, prepare_response))
        _PAYMENT_RECHECK_TASKS.add(task)
        task.add_done_callback(_PAYMENT_RECHECK_TASKS.discard)


def _spawn_payment_recheck(**kwargs):
    """Fire-and-forget the recheck; keep a hard reference so the task is
    never garbage-collected mid-poll."""
    task = asyncio.create_task(_payment_recheck_task(**kwargs))
    _PAYMENT_RECHECK_TASKS.add(task)
    task.add_done_callback(_PAYMENT_RECHECK_TASKS.discard)
    return task


def get_direction_info(payment, user_id=None):
    """Get direction icon and localized text from payment type"""
    payment_type = getattr(payment, 'payment_type', 'Unknown')

    if hasattr(payment, 'direction'):
        direction = payment.direction
        if direction == PaymentDirection.INCOMING:
            return "📥", t(user_id, "dir_received"), True
        else:
            return "📤", t(user_id, "dir_sent"), False
    else:
        # Fallback: check payment_type
        type_str = str(payment_type).upper()
        if 'RECEIVE' in type_str:
            return "📥", t(user_id, "dir_received"), True
        else:
            return "📤", t(user_id, "dir_sent"), False


def extract_description_from_payment(payment):
    """Extract description/memo from payment object - tries all possible locations

    Args:
        payment: Payment object from SDK

    Returns:
        Description string or empty string if not found
    """
    details = getattr(payment, 'details', None)

    # Try direct description on payment object
    if hasattr(payment, 'description') and payment.description:
        return payment.description

    if not details:
        return ""

    # Try different detail types for description
    if hasattr(details, 'description') and details.description:
        return details.description

    if hasattr(details, 'memo') and details.memo:
        return details.memo

    # For Lightning payments, check nested structures
    # PaymentDetailsLightning might have payment_request with description
    if hasattr(details, 'payment_request') and details.payment_request:
        # Try to get description from the payment request
        pr = details.payment_request
        if hasattr(pr, 'description') and pr.description:
            return pr.description
        if hasattr(pr, 'memo') and pr.memo:
            return pr.memo

    # Try to extract from invoice (BOLT11)
    if hasattr(details, 'invoice') and details.invoice:
        inv = details.invoice
        if hasattr(inv, 'description') and inv.description:
            return inv.description
        if hasattr(inv, 'memo') and inv.memo:
            return inv.memo

    # Try from PaymentDetails Lightning variant
    # The SDK might have different structures based on payment type
    try:
        # Check if details is a PaymentDetailLightning variant
        if hasattr(details, '__dict__'):
            for key, value in details.__dict__.items():
                if key in ['description', 'memo', 'note'] and value:
                    return str(value)
    except Exception:
        pass

    # Check if payment object has any description-related field
    for attr in dir(payment):
        if 'desc' in attr.lower() or 'memo' in attr.lower() or 'note' in attr.lower():
            try:
                value = getattr(payment, attr, None)
                if value and not callable(value):
                    return str(value)
            except Exception:
                continue

    return ""


def build_queue_text(queue, user_id=None):
    """Live queue summary: every pending payment + totals."""
    total = 0
    fee_total = 0
    lines = [t(user_id, "queue_title")]
    for i, it in enumerate(queue, 1):
        amt = it.get('amount')
        fee = int(it.get('fee') or 0)
        dest = None
        ru = it.get('resolved_user')
        if isinstance(ru, dict):
            dest = ru.get('display') or ru.get('username')
        if not dest:
            pr = it.get('prepare_response')
            s = str(getattr(pr, 'payment_request', '')) if pr else ''
            dest = (s[:26] + "\u2026") if s else it.get('payment_type', '?')
        if isinstance(amt, int):
            total += amt
        fee_total += fee
        dd = dest if len(dest) <= 30 else dest[:29] + "\u2026"
        amt_s = f"{amt:,}" if isinstance(amt, int) else str(amt)
        lines.append(t(user_id, "queue_item", i=i, dest=dd, amount=amt_s))
    lines.append(t(user_id, "queue_fees", total=fee_total))
    lines.append(t(user_id, "queue_total", total=total))
    return "\n".join(lines), total, fee_total


def payment_share_button(payment_id, user_id=None):
    """Share row: view the transaction on sparkscan.io explorer."""
    if not payment_id or payment_id == 'N/A':
        return []
    return [Button.url(t(user_id, "btn_view_explorer"), f"https://sparkscan.io/tx/{payment_id}")]


async def format_payment_details(payment, payment_id=None, user_id=None):
    """Format payment details into a readable string - reusable helper function

    Args:
        payment: Payment object from SDK
        payment_id: Optional payment ID (will use payment.id if not provided)

    Returns:
        Formatted string with payment details
    """
    amount = getattr(payment, 'amount', 0)
    amount_sats = getattr(payment, 'amount_sats', amount)
    timestamp = getattr(payment, 'timestamp', 0)
    status = getattr(payment, 'status', 'Unknown')
    payment_type = getattr(payment, 'payment_type', 'Unknown')
    fees = getattr(payment, 'fees', 0)
    method = getattr(payment, 'method', None)
    details = getattr(payment, 'details', None)

    if not payment_id:
        payment_id = getattr(payment, 'id', 'N/A')

    # Get direction info
    direction_icon, direction_str, is_incoming = get_direction_info(payment, user_id)

    # Format timestamp
    time_str = format_timestamp(timestamp)

    # Status emoji
    status_icon, status_text = get_status_icon_and_text(status, user_id)

    # Format method
    method_str = str(method).replace('PaymentMethod.', '') if method else 'N/A'

    # Format amount with USD
    amount_str = await format_amount_with_usd(amount_sats, user_id)
    fee_str = await format_amount_with_usd(fees, user_id) if fees else f"0 {t(user_id, 'sats_unit')}"

    # Extract description/memo from payment details
    description = extract_description_from_payment(payment)

    # Extract tx_id if present (for onchain transactions)
    tx_id_line = ""
    if details:
        if hasattr(details, 'tx_id') and details.tx_id:
            tx_id_line = f"🔗 {t(user_id, 'rep_lbl_txid')}\n`{details.tx_id}`\n\n"

    # ---- Extract all proof/hash fields from details (per type) ----
    def _shorten(s, n=20):
        return s if not s or len(s) <= n else s[:n] + "…"

    proof_lines = []
    dest_lines = []

    def add_proof(label, value, shorten=True):
        if value:
            v = str(value)
            proof_lines.append(f"{label}: `{_shorten(v) if shorten else v}`")

    def add_dest(label, value):
        if value:
            dest_lines.append(f"{label}: `{value}`")

    # Payment ID (hash of the payment itself)
    payment_hash = None
    preimage = None
    invoice_str = None
    destination = None
    lnurl_pay_info = None
    htlc_details = getattr(details, 'htlc_details', None) or (details if hasattr(details, 'preimage') else None)

    # Pull fields from wherever they exist
    for obj in (details, payment):
        if not obj:
            continue
        payment_hash = payment_hash or getattr(obj, 'payment_hash', None) or getattr(getattr(obj, 'id', None), 'hash', None)
        preimage = preimage or getattr(obj, 'preimage', None) or getattr(htlc_details, 'preimage', None) if htlc_details or obj is details else preimage
        invoice_str = invoice_str or getattr(obj, 'payment_request', None) or getattr(obj, 'invoice', None)

    # From lightning details specifically
    if hasattr(details, 'preimage'):
        preimage = preimage or details.preimage
    if hasattr(details, 'payment_hash') and not payment_hash:
        payment_hash = details.payment_hash
    if hasattr(htlc_details, 'preimage') and htlc_details and not preimage:
        preimage = htlc_details.preimage
    if details is not None:
        destination = getattr(details, 'destination', None)
        lnurl_pay_info = getattr(details, 'lnurl_pay_info', None)
        if hasattr(destination, 'invoice') and not invoice_str:
            invoice_str = destination.invoice

    # Fallback: payment.id IS the payment hash in Breez SDK — but if a REAL
    # payment_hash exists separately (e.g. details), prefer it and keep ID as ID.
    real_hash = None
    for obj in (details, payment, htlc_details):
        v = getattr(obj, 'payment_hash', None) if obj else None
        if v:
            real_hash = str(v)
            break
    # only show Payment Hash if it's actually different from the ID
    has_separate_hash = bool(real_hash) and real_hash != str(payment_id)

    msg = (
        f"{t(user_id, 'rep_title')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{t(user_id, 'rep_sec_general')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{direction_icon} {t(user_id, 'rep_lbl_type')} {direction_str}\n"
        f"💰 {t(user_id, 'rep_lbl_amount')} {amount_str}\n"
        f"⚡ {t(user_id, 'rep_lbl_fee')} {fee_str}\n"
        f"{status_icon} {t(user_id, 'rep_lbl_status')} {status_text}\n"
        f"🕐 {t(user_id, 'rep_lbl_time')} {time_str}\n"
        f"🏷️ {t(user_id, 'rep_lbl_ptype')} {str(payment_type).replace('PaymentType.', '')}\n"
        f"📡 {t(user_id, 'rep_lbl_method')} {method_str}\n"
    )

    if description:
        msg += f"💬 {t(user_id, 'rep_lbl_desc')} {_md_escape(str(description))}\n"

    # ---- PROOF section ----
    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"{t(user_id, 'rep_sec_proof')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    if tx_id_line:
        msg += f"🔗 {t(user_id, 'rep_lbl_txid')}\n`{details.tx_id}`\n\n"
    if has_separate_hash:
        msg += f"#️⃣ {t(user_id, 'rep_lbl_hash')}\n`{real_hash}`\n\n"
    if preimage:
        msg += f"🔑 {t(user_id, 'rep_lbl_preimage')}\n`{preimage}`\n\n"
    if not any([tx_id_line, has_separate_hash, preimage]):
        msg += t(user_id, 'rep_no_proof')

    # ---- DESTINATION / INVOICE section ----
    if invoice_str or dest_lines:
        msg += (
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{t(user_id, 'rep_sec_route')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        for dl in dest_lines:
            msg += dl + "\n"
        if invoice_str:
            # full invoice in ONE code block — copy button copies it whole
            inv_full = str(invoice_str)
            msg += f"⚡ {t(user_id, 'rep_lbl_invoice')}\n```{inv_full}```\n"

    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 {t(user_id, 'rep_lbl_id')}\n`{payment_id}`"
    )

    return msg


# ==================== Event-Based Notification System ====================

class UserEventListener(EventListener):
    """Event listener for a specific user's wallet - handles payment notifications"""

    def __init__(self, user_id):
        self.user_id = user_id

    async def on_event(self, event: SdkEvent):
        """Handle SDK events - async method as required by Breez SDK"""
        try:
            if isinstance(event, SdkEvent.PAYMENT_SUCCEEDED):
                payment = event.payment
                # Check if it's an incoming payment (RECEIVE)
                if hasattr(payment, 'payment_type') and payment.payment_type == PaymentType.RECEIVE:
                    payment_id = getattr(payment, 'id', None)

                    # Deduplication check - prevent duplicate notifications
                    if payment_id:
                        with notified_payments_lock:
                            if payment_id in notified_payments:
                                logging.debug(f"[{self.user_id}] Duplicate payment {payment_id}, skipping")
                                return
                            # Mark as notified
                            notified_payments[payment_id] = datetime.now()
                            _save_notified_payments(db)

                    # Schedule notification on main event loop
                    asyncio.run_coroutine_threadsafe(
                        self._notify_payment_received(payment),
                        main_event_loop
                    )
            elif isinstance(event, SdkEvent.SYNCED):
                logging.debug(f"[{self.user_id}] Wallet synced")
            elif isinstance(event, SdkEvent.UNCLAIMED_DEPOSITS):
                # v31.1: auto-claim failed (fee above limit) — nudge user to claim manually
                asyncio.run_coroutine_threadsafe(
                    self._notify_unclaimed_deposits(), main_event_loop)
        except Exception as e:
            logging.error(f"[{self.user_id}] Event handler error: {e}")

    async def _notify_unclaimed_deposits(self):
        try:
            await client.send_message(
                self.user_id,
                t(self.user_id, "dep_detected_notify"))
        except Exception as e:
            logging.error(f"[{self.user_id}] unclaimed-deposit notify failed: {e}")

    async def _notify_payment_received(self, payment):
        """Send payment notification to user - runs on Telethon's event loop"""
        try:
            amount = getattr(payment, 'amount_sats', getattr(payment, 'amount', 0))
            payment_id = getattr(payment, 'id', 'N/A')
            timestamp = getattr(payment, 'timestamp', 0)

            # Track incoming volume
            if amount and amount > 0:
                track_volume('in', amount)

            # Format time
            time_str = format_timestamp(timestamp)

            # Format amount with USD
            amount_str = await format_amount_with_usd(amount, user_id)

            # Extract description/memo from payment details using helper
            description = extract_description_from_payment(payment)

            # Check if there's sender info from a username payment
            sender_line = ""
            with pending_sender_info_lock:
                logging.debug(f"[{self.user_id}] Checking pending_sender_info: {self.user_id in pending_sender_info}")
                if self.user_id in pending_sender_info:
                    info = pending_sender_info[self.user_id]
                    # Check if this is likely the same payment (within 60 seconds)
                    time_diff = (datetime.now() - info['timestamp']).total_seconds()
                    logging.debug(f"[{self.user_id}] Found sender info, time_diff={time_diff}s, sender={info['sender_info']}")
                    if time_diff < 60:
                        sender_line = t(self.user_id, "from_line", sender=info['sender_info'])
                        del pending_sender_info[self.user_id]
                        logging.info(f"[{self.user_id}] Added sender to notification: {info['sender_info']}")

            # No fee until a durable claim and exact deposit attribution are available.
            fee_note = ""

            await client.send_message(
                self.user_id,
                t(self.user_id, "payment_received",
                  sender=sender_line, amount=amount_str, fee_note=fee_note,
                  time=time_str,
                  desc=(t(self.user_id, "desc_line", desc=_md_escape(str(description)))
                        if description else ''),
                  pid=payment_id)
            )
            logging.info(f"[{self.user_id}] Notified: {amount} sats received")

        except Exception as e:
            logging.error(f"[{self.user_id}] Notification error: {e}")


# ==================== Volume Tracking ====================
def track_volume(direction: str, amount_sats: int):
    """Track incoming/outgoing volume in database

    Args:
        direction: 'in' for incoming, 'out' for outgoing
        amount_sats: Amount in satoshis
    """
    try:
        with db_lock:
            stats = db.get('bot_stats')
            if stats is None:
                stats = {'total_in': 0, 'total_out': 0}

            if direction == 'in':
                stats['total_in'] = stats.get('total_in', 0) + amount_sats
            elif direction == 'out':
                stats['total_out'] = stats.get('total_out', 0) + amount_sats

            db.set('bot_stats', stats)
            logging.info(f"Volume tracked: {direction} {amount_sats} sats")
    except Exception as e:
        logging.error(f"Error tracking volume: {e}")


def get_volume_stats():
    """Get total volume statistics

    Returns:
        dict with 'total_in' and 'total_out' in sats
    """
    try:
        with db_lock:
            stats = db.get('bot_stats')
            if stats is None:
                return {'total_in': 0, 'total_out': 0}
            return stats
    except Exception as e:
        logging.error(f"Error getting volume stats: {e}")
        return {'total_in': 0, 'total_out': 0}


def save_notification_to_db(user_id, enabled):
    """Save notification state to database for persistence (thread-safe)"""
    try:
        with db_lock:
            notification_data = db.get('notification_users')
            user_id_str = str(user_id)

            if user_id_str not in notification_data:
                notification_data[user_id_str] = {}

            notification_data[user_id_str]['enabled'] = enabled
            notification_data[user_id_str]['last_activity'] = datetime.now().isoformat()

            db.set('notification_users', notification_data)
    except Exception as error:
        logging.error(f"Error saving notification to DB: {error}")


async def enable_notifications(user_id):
    """Enable event-based notifications for a user by creating persistent SDK connection"""
    # Skip for channels/anonymous (negative IDs)
    if not user_id or user_id < 0:
        return False

    async with connections_lock:
        # Already connected
        if user_id in active_user_connections:
            logging.debug(f"[{user_id}] Already has active event listener")
            return True

        try:
            # Get mnemonic and create SDK connection
            encrypted_mnemonic = db.get(f'{user_id}')
            if not encrypted_mnemonic:
                logging.debug(f"[{user_id}] No wallet found")
                return False

            # Decrypt seed (handles both encrypted and plaintext)
            mnemonic = decrypt_seed(encrypted_mnemonic)
            seed = Seed.MNEMONIC(mnemonic=mnemonic, passphrase=None)
            sdk = await sdk_read(
                connect(
                    request=ConnectRequest(config=config, seed=seed, storage_dir=f"./.{user_id}")
                ),
                f'connect({user_id})'
            )

            # Create event listener for this user
            listener = UserEventListener(user_id)
            listener_id = await sdk_read(
                sdk.add_event_listener(listener=listener),
                f'add_event_listener({user_id})'
            )

            # Store connection info
            active_user_connections[user_id] = {
                'sdk': sdk,
                'listener_id': listener_id,
                'listener': listener
            }

            # Save to DB
            save_notification_to_db(user_id, enabled=True)

            logging.info(f"[{user_id}] Event listener enabled (ID: {listener_id})")
            return True

        except Exception as e:
            logging.error(f"[{user_id}] Failed to enable notifications: {e}")
            return False


async def disable_notifications(user_id, reason='auto_inactive'):
    """Disable notifications by removing event listener and closing SDK connection"""
    async with connections_lock:
        if user_id not in active_user_connections:
            logging.debug(f"[{user_id}] No active connection to disable")
            return

        try:
            conn = active_user_connections[user_id]
            sdk = conn.get('sdk')
            listener_id = conn.get('listener_id')

            # Remove event listener
            if sdk and listener_id:
                try:
                    await sdk_read(sdk.remove_event_listener(id=listener_id),
                                   f'remove_event_listener({user_id})',
                                   timeout_secs=SDK_DISCONNECT_TIMEOUT_SECS)
                except Exception as e:
                    logging.debug(f"[{user_id}] Error removing listener: {e}")

            # Disconnect SDK
            if sdk:
                try:
                    await sdk_read(sdk.disconnect(), f'disconnect({user_id})',
                                   timeout_secs=SDK_DISCONNECT_TIMEOUT_SECS)
                except Exception as e:
                    logging.debug(f"[{user_id}] Error disconnecting SDK: {e}")

            # Remove from active connections
            del active_user_connections[user_id]

            # Update DB
            try:
                with db_lock:
                    notification_data = db.get('notification_users')
                    user_id_str = str(user_id)
                    if user_id_str not in notification_data:
                        notification_data[user_id_str] = {}
                    notification_data[user_id_str]['enabled'] = False
                    notification_data[user_id_str]['disabled_reason'] = reason
                    notification_data[user_id_str]['disabled_at'] = datetime.now().isoformat()
                    db.set('notification_users', notification_data)
            except Exception as e:
                logging.debug(f"[{user_id}] DB update error: {e}")

            logging.info(f"[{user_id}] Notifications disabled (reason: {reason})")

        except Exception as e:
            logging.error(f"[{user_id}] Error disabling notifications: {e}")
            # Clean up anyway
            if user_id in active_user_connections:
                del active_user_connections[user_id]

    # Also clean up activity tracking
    if user_id in user_last_activity:
        del user_last_activity[user_id]


async def mark_user_active(user_id):
    """Mark user as active and auto-enable notifications if not already enabled"""
    # Skip for channels/anonymous (negative IDs)
    if not user_id or user_id < 0:
        return

    user_last_activity[user_id] = datetime.now()

    # Auto-enable notifications when user interacts with bot
    if user_id not in active_user_connections:
        # Check if user has a wallet first
        users = db.get('users')
        if user_id in users:
            asyncio.create_task(enable_notifications(user_id))
            logging.debug(f"[{user_id}] Auto-enabling notifications on activity")
    else:
        # Update activity in DB
        try:
            with db_lock:
                notification_data = db.get('notification_users')
                user_id_str = str(user_id)
                if user_id_str in notification_data:
                    notification_data[user_id_str]['last_activity'] = datetime.now().isoformat()
                    db.set('notification_users', notification_data)
        except Exception as error:
            logging.debug(f"Error updating activity: {error}")


async def monitor_inactive_users():
    """Background task to disable notifications for users inactive for 24+ hours"""
    inactive_threshold = 86400  # 24 hours in seconds
    check_interval = 300  # Check every 5 minutes
    payment_cache_lifetime = 3600  # Clean payment IDs older than 1 hour

    while True:
        try:
            await asyncio.sleep(check_interval)

            current_time = datetime.now()
            inactive_users = []

            # Find inactive users
            async with connections_lock:
                for user_id in list(active_user_connections.keys()):
                    last_activity = user_last_activity.get(user_id)

                    if last_activity:
                        time_diff = (current_time - last_activity).total_seconds()
                        if time_diff >= inactive_threshold:
                            inactive_users.append(user_id)
                    else:
                        # No activity recorded, mark as inactive
                        inactive_users.append(user_id)

            # Disable notifications for inactive users (outside the lock)
            for user_id in inactive_users:
                await disable_notifications(user_id, reason='auto_inactive')

            if inactive_users:
                logging.info(f"Disabled notifications for {len(inactive_users)} inactive users")

            # Cleanup old payment IDs to prevent memory leak
            with notified_payments_lock:
                old_payments = [
                    pid for pid, ts in notified_payments.items()
                    if (current_time - ts).total_seconds() > payment_cache_lifetime
                ]
                for pid in old_payments:
                    del notified_payments[pid]
                if old_payments:
                    logging.debug(f"Cleaned up {len(old_payments)} old payment IDs")
                    _save_notified_payments(db)

            # Cleanup old pending sender info (older than 2 minutes)
            with pending_sender_info_lock:
                old_senders = [
                    uid for uid, info in pending_sender_info.items()
                    if (current_time - info['timestamp']).total_seconds() > 120
                ]
                for uid in old_senders:
                    del pending_sender_info[uid]

            # Log active connections count
            active_count = len(active_user_connections)
            if active_count > 0:
                logging.debug(f"Active event listeners: {active_count}")

        except Exception as error:
            logging.error(f"Inactive monitor error: {error}")
            await asyncio.sleep(60)


async def get_balance_raw(user_id: int) -> int:
    """Get wallet balance for a user, in sats (item 27: typed core read).

    Per SDK docs: get_info reads a local cache refreshed by background sync;
    ensure_synced=True only awaits the INITIAL post-connect sync (can hang
    minutes on fresh connections) — it is NOT a "force fresh sync". With the
    cached read + Synced-event refresh pattern this is correct for a bot.
    """
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        info = await sdk_read(sdk.get_info(request=GetInfoRequest(ensure_synced=False)),
                              f'get_info({user_id})')
        return info.balance_sats
    except Exception as error:
        logging.error(f"Error getting balance: {error}")
        return 0
    finally:
        if sdk:
            await close_wallet(sdk)


async def get_balance(user_id: int) -> str:
    """str-returning wrapper kept for the existing call sites (bytes: str
    of the sats int, "0" on error)."""
    return str(await get_balance_raw(user_id))


async def init(user_id):
    """Initialize new user wallet with mnemonic (encrypted)"""
    users = db.get(f'users')
    if user_id not in users:
        words = mnemo.generate(strength=128)
        logging.info(f"New wallet created for user {user_id}")
        # Encrypt seed before storing
        encrypted_seed = encrypt_seed(words)
        db.set_default(f'{user_id}', encrypted_seed)
        users.append(user_id)
        db.set(f'users', users)


async def get_wallet(user_id):
    """Get or create wallet SDK instance with connection management"""
    try:
        # Enable notifications for this user (so they get payment notifications)
        # Skip for channels/anonymous (negative IDs) - they can't receive notifications
        if user_id and user_id > 0:
            try:
                await sdk_read(enable_notifications(user_id),
                               f'enable_notifications({user_id})')
            except Exception as e:
                logging.debug(f"Could not enable notifications for {user_id}: {e}")

        # Reuse existing connection if available
        if user_id in active_user_connections:
            existing = active_user_connections[user_id].get('sdk')
            if existing:
                return existing

        encrypted_mnemonic = db.get(f'{user_id}')
        # Decrypt seed (handles both encrypted and plaintext for backward compatibility)
        mnemonic = decrypt_seed(encrypted_mnemonic)
        seed = Seed.MNEMONIC(mnemonic=mnemonic, passphrase=None)
        sdk = await sdk_read(
            connect(
                request=ConnectRequest(config=config, seed=seed, storage_dir=f"./.{user_id}")
            ),
            f'connect({user_id})'
        )
        return sdk
    except Exception as error:
        logging.error(f"Error connecting wallet for user {user_id}: {error}")
        raise


async def close_wallet(sdk):
    """Safely close wallet connection (skip if it's an active notification connection)"""
    try:
        if not sdk:
            return
        # Don't close connections that are used for notifications
        for conn_info in active_user_connections.values():
            if conn_info.get('sdk') is sdk:
                return  # Skip - this SDK is used for notifications
        await sdk_read(sdk.disconnect(), 'disconnect', timeout_secs=SDK_DISCONNECT_TIMEOUT_SECS)
    except Exception as error:
        logging.debug(f"Error closing wallet: {error}")


async def create_invoice(user_id, amount_sats=None, description="Zap payment"):
    """Create Lightning invoice"""
    try:
        sdk = await get_wallet(user_id)
        payment_method = ReceivePaymentMethod.BOLT11_INVOICE(
            description=description,
            amount_sats=amount_sats,
            expiry_secs=None,
            payment_hash=None,  # Added in SDK v0.12.x
            receiver_identity_public_key=None  # Required in newer SDK
        )
        request = ReceivePaymentRequest(payment_method=payment_method)
        response = await sdk.receive_payment(request=request)
        payment_request = response.payment_request
        return payment_request
    except Exception as error:
        logging.error(f"Error creating invoice: {error}")
        raise


async def create_onchain_address(user_id):
    """Create onchain Bitcoin address for receiving"""
    try:
        sdk = await get_wallet(user_id)
        request = ReceivePaymentRequest(
            payment_method=ReceivePaymentMethod.BITCOIN_ADDRESS(new_address=True)
        )
        response = await sdk.receive_payment(request=request)

        payment_request = response.payment_request
        receive_fee_sats = response.fee

        logging.debug(f"Onchain Address: {payment_request}")
        logging.debug(f"Receive Fees: {receive_fee_sats} sats")

        return payment_request, receive_fee_sats
    except Exception as error:
        logging.error(f"Error creating onchain address: {error}")
        raise


async def get_static_onchain_address(user_id):
    """Get the wallet's STATIC onchain address (same every time, never rotates)"""
    try:
        sdk = await get_wallet(user_id)
        request = ReceivePaymentRequest(
            payment_method=ReceivePaymentMethod.BITCOIN_ADDRESS(new_address=False)
        )
        response = await sdk.receive_payment(request=request)
        return response.payment_request
    except Exception as error:
        logging.error(f"Error getting static onchain address: {error}")
        return None
    finally:
        if sdk:
            await close_wallet(sdk)


async def get_payment_history(user_id, limit=20):
    """Get payment history for a user"""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        response = await sdk_read(sdk.list_payments(request=ListPaymentsRequest()),
                                  f'list_payments({user_id})')
        payments = response.payments

        # Sort by timestamp (most recent first) and limit
        sorted_payments = sorted(
            payments,
            key=lambda p: getattr(p, 'timestamp', 0),
            reverse=True
        )[:limit]

        return sorted_payments
    except Exception as error:
        logging.error(f"Error getting payment history: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def get_payment_by_id(user_id, payment_id):
    """Get a specific payment by its ID"""
    sdk = None
    try:
        sdk = await get_wallet(user_id)

        # Normalize search ID
        search_id = str(payment_id).strip()
        search_id_lower = search_id.lower()

        # First try the direct get_payment method
        try:
            response = await sdk_read(
                sdk.get_payment(request=GetPaymentRequest(payment_id=search_id)),
                f'get_payment({user_id})'
            )
            if response and response.payment:
                logging.info(f"[get_payment_by_id] Found via get_payment: {search_id}")
                return response.payment
        except Exception as e:
            logging.debug(f"get_payment failed for {search_id}, trying list_payments: {e}")

        # Fallback: search in payment list
        response = await sdk_read(sdk.list_payments(request=ListPaymentsRequest()),
                                  f'list_payments({user_id})')
        if response and response.payments:
            logging.info(f"[get_payment_by_id] Searching {len(response.payments)} payments for {search_id}")

            for payment in response.payments:
                p_id = getattr(payment, 'id', None)
                if p_id:
                    p_id_str = str(p_id).strip()
                    # Exact match (case insensitive)
                    if p_id_str.lower() == search_id_lower:
                        logging.info(f"[get_payment_by_id] Found exact match: {p_id_str}")
                        return payment
                    # Partial match (in case ID is truncated or formatted differently)
                    if search_id_lower in p_id_str.lower() or p_id_str.lower() in search_id_lower:
                        logging.info(f"[get_payment_by_id] Found partial match: {p_id_str} ~ {search_id}")
                        return payment

            # Debug: log all payment IDs
            all_ids = [str(getattr(p, 'id', 'N/A')) for p in response.payments]
            logging.warning(f"[get_payment_by_id] Payment {search_id} not found in {len(all_ids)} payments")
            logging.warning(f"[get_payment_by_id] Available IDs: {all_ids[:20]}")  # Log first 20

        return None
    except Exception as error:
        logging.error(f"Error getting payment {payment_id}: {error}")
        return None
    finally:
        if sdk:
            await close_wallet(sdk)


async def get_private_mode(user_id):
    """Get user's private mode setting from SDK"""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        settings = await sdk.get_user_settings()
        return settings.spark_private_mode_enabled
    except Exception as error:
        logging.error(f"Error getting private mode: {error}")
        return False
    finally:
        if sdk:
            await close_wallet(sdk)


async def set_private_mode(user_id, enabled: bool):
    """Set user's private mode setting in SDK"""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        request = UpdateUserSettingsRequest(spark_private_mode_enabled=enabled)
        await sdk.update_user_settings(request=request)
        return True
    except Exception as error:
        logging.error(f"Error setting private mode: {error}")
        return False
    finally:
        if sdk:
            await close_wallet(sdk)


# ==================== v30: payment-input detection + display helpers ====================
# Cheap heuristics to decide whether a pasted DM looks like a payment destination
# (invoice / address / LNURL / Lightning address / Telegram username). The
# authoritative parsing still happens in the send_invoice step; this only lets us
# auto-route the user into Send without making them tap the button first.
_PAYMENT_INPUT_REGEXES = [
    re.compile(r'^ln(bc|tb|bcrt)1', re.IGNORECASE),              # BOLT11 invoice
    re.compile(r'^(lightning|bitcoin|lnurl):', re.IGNORECASE),   # scheme URIs
    re.compile(r'^lnurl[a-z0-9]+$', re.IGNORECASE),              # raw LNURL (bech32)
    re.compile(r'^bc1[ac-hj-np-z02-9]{6,}$', re.IGNORECASE),     # Bech32 / Bech32m BTC
    re.compile(r'^[13][a-km-zA-HJ-NP-Z1-9]{25,39}$'),            # legacy / P2SH BTC
    re.compile(r'^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'),  # Lightning address
    re.compile(r'^@[A-Za-z0-9_]{3,}$'),                          # Telegram username
]


def _looks_like_payment_input(text):
    """Heuristic: does this message look like an invoice / address / username?"""
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    return any(rx.match(t) for rx in _PAYMENT_INPUT_REGEXES)


def _format_fees(fees, user_id):
    """Human-readable recommended on-chain fees (clearly labeled)."""
    return t(user_id, "fees_card", fastest=fees.fastest_fee,
             half=fees.half_hour_fee, hour=fees.hour_fee,
             economy=fees.economy_fee, minimum=fees.minimum_fee)


async def parse_input(sdk , input_str):
    """Parse Lightning invoice, address, or Bitcoin address"""
    try:
        parsed = await sdk.parse(input=input_str)
        return parsed
    except Exception as error:
        logging.error(f"Error parsing input: {error}")
        raise

async def resolve_username(username):
    """Resolve Telegram username to Lightning Address
    Returns: (invoice, user_info_dict)
    """
    try:
        user = await client.get_entity(username)
        receiver_id = user.id

        # Check if it's a channel/group
        is_channel = isinstance(user, Channel)
        is_group = is_channel and user.megagroup

        # Get display name
        if is_channel:
            display_name = user.title
            user_type = "Group" if is_group else "Channel"
        else:
            display_name = user.first_name or ""
            if user.last_name:
                display_name += f" {user.last_name}"
            user_type = "User"

        # Check if user has started the bot (exists in users list)
        users = db.get('users') or []
        has_started_bot = receiver_id in users

        await init(receiver_id)
        receiver_invoice = await create_invoice(receiver_id)

        user_info = {
            'id': receiver_id,
            'username': username.lstrip('@'),
            'display_name': display_name,
            'type': user_type,
            'is_channel': is_channel,
            'is_group': is_group,
            'is_bot': getattr(user, 'bot', False) if not is_channel else False,
            'has_started_bot': has_started_bot
        }

        return receiver_invoice, user_info
    except Exception as error:
        logging.error(f"Error resolving username: {error}")
        raise


async def prepare_payment(user_id, invoice, amount_sats=None):
    """Prepare payment and get fee information"""
    try:
        sdk = await get_wallet(user_id)

        # newer SDK needs a PaymentRequest enum, not a raw string
        if isinstance(invoice, str):
            from breez_sdk_spark import PaymentRequest as _PR
            invoice = _PR.INPUT(invoice)
        request = PrepareSendPaymentRequest(
            payment_request=invoice,
            amount=amount_sats
        )

        prepare_response = await sdk_read(
            sdk.prepare_send_payment(request=request),
            f'prepare_send_payment({user_id})'
        )

        fee_sats = 0
        spark_fee = 0

        if isinstance(prepare_response.payment_method, SendPaymentMethod.BOLT11_INVOICE):
            fee_sats = prepare_response.payment_method.lightning_fee_sats
            spark_fee = prepare_response.payment_method.spark_transfer_fee_sats
        elif isinstance(prepare_response.payment_method, SendPaymentMethod.BITCOIN_ADDRESS):
            # For onchain payments, return fee quote
            return prepare_response, None, None

        return prepare_response, fee_sats, spark_fee
    except Exception as error:
        logging.error(f"Error preparing payment: {error}")
        raise


async def prepare_lnurl_pay(user_id, pay_request, amount_sats, comment=None):
    """Prepare LNURL payment"""
    try:
        sdk = await get_wallet(user_id)

        request = PrepareLnurlPayRequest(
            amount=amount_sats,
            pay_request=pay_request,
            comment=comment,
            validate_success_action_url=True
        )
        prepare_response = await sdk.prepare_lnurl_pay(request=request)
        fee_sats = prepare_response.fee_sats

        return prepare_response, fee_sats
    except Exception as error:
        logging.error(f"Error preparing LNURL pay: {error}")
        raise


async def send_payment(user_id, prepare_response, options=None):
    """Execute payment"""
    try:
        sdk = await get_wallet(user_id)

        if options:
            request = SendPaymentRequest(prepare_response=prepare_response, options=options)
        else:
            request = SendPaymentRequest(prepare_response=prepare_response)

        send_response = await sdk.send_payment(request=request)
        payment = send_response.payment
        return payment
    except Exception as error:
        logging.error(f"Error sending payment: {error}")
        raise


# ==================== v30 NEW SDK FEATURE WRAPPERS ====================
# Thin async wrappers around the Spark SDK's newer capabilities. Each follows
# the existing get_wallet()/close_wallet() lifecycle used by the rest of the bot.
async def list_user_contacts(user_id):
    """List the user's saved contacts (Spark SDK address book)."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        return await sdk.list_contacts(request=ListContactsRequest())
    except Exception as error:
        logging.error(f"Error listing contacts for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def add_user_contact(user_id, name, payment_identifier):
    """Add a contact to the user's address book."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        return await sdk.add_contact(request=AddContactRequest(
            name=name, payment_identifier=payment_identifier))
    except Exception as error:
        logging.error(f"Error adding contact for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def delete_user_contact(user_id, contact_id):
    """Delete a contact by id."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        await sdk.delete_contact(id=contact_id)
        return True
    except Exception as error:
        logging.error(f"Error deleting contact for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def get_recommended_fees(user_id):
    """Fetch recommended on-chain network fees."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        return await sdk.recommended_fees()
    except Exception as error:
        logging.error(f"Error fetching fees for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def sync_user_wallet(user_id):
    """Force a wallet sync and return refreshed info."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        # Explicit sync IS the right way to force fresh data (docs: ensure_synced
        # only awaits initial sync); read the cache right after it completes.
        await sdk.sync_wallet(request=SyncWalletRequest())
        return await sdk.get_info(request=GetInfoRequest(ensure_synced=False))
    except Exception as error:
        logging.error(f"Error syncing wallet for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def get_lightning_address(user_id):
    """Get the user's registered Lightning address (if any)."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        return await sdk.get_lightning_address()
    except Exception as error:
        logging.error(f"Error getting LN address for {user_id}: {error}")
        return None
    finally:
        if sdk:
            await close_wallet(sdk)


async def register_lightning_address(user_id, username, description=None):
    """Register a reusable Lightning address (e.g. you@breez.lt)."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        kwargs = {'username': username}
        if description is not None:
            kwargs['description'] = description
        return await sdk.register_lightning_address(request=RegisterLightningAddressRequest(**kwargs))
    except Exception as error:
        logging.error(f"Error registering LN address for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def delete_lightning_address(user_id):
    """Delete the user's Lightning address."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        await sdk.delete_lightning_address()
        return True
    except Exception as error:
        logging.error(f"Error deleting LN address for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def buy_bitcoin_url(user_id, locked_amount_sat=None):
    """Get a Buy Bitcoin on-ramp redirect URL."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        # breez_sdk_spark 0.27.x: BuyBitcoinRequest.MOONPAY(delivery, redirect_url);
        # a locked sats amount is prefilled via MoonpayDelivery.BITCOIN(amount_sat=...).
        delivery = MoonpayDelivery.BITCOIN(amount_sat=locked_amount_sat) if locked_amount_sat is not None else None
        resp = await sdk.buy_bitcoin(request=BuyBitcoinRequest.MOONPAY(delivery=delivery, redirect_url=None))
        return resp.url
    except Exception as error:
        logging.error(f"Error creating buy-bitcoin URL for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def sign_message_with_wallet(user_id, message, compact=True):
    """Sign an arbitrary message to prove wallet ownership."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        return await sdk.sign_message(request=SignMessageRequest(message=message, compact=compact))
    except Exception as error:
        logging.error(f"Error signing message for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def check_message_with_wallet(user_id, message, pubkey, signature):
    """Verify a signed message against a pubkey."""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        return await sdk.check_message(request=CheckMessageRequest(
            message=message, pubkey=pubkey, signature=signature))
    except Exception as error:
        logging.error(f"Error checking message for {user_id}: {error}")
        raise
    finally:
        if sdk:
            await close_wallet(sdk)


async def lnurl_pay(user_id, prepare_response):
    """Execute LNURL payment"""
    try:
        sdk = await get_wallet(user_id)
        response = await sdk.lnurl_pay(LnurlPayRequest(prepare_response=prepare_response))
        return response
    except Exception as error:
        logging.error(f"Error LNURL pay: {error}")
        raise


def get_main_buttons(user_id=None):
    """Get main menu buttons (translated per user)"""
    if user_id is None:
        user_id = 0
    rows = [
        [Button.inline(t(user_id, "btn_send"), b"send"), Button.inline(t(user_id, "btn_receive"), b"receive")],
        [Button.inline(t(user_id, "btn_history"), b"history"), Button.inline(t(user_id, "btn_refresh"), b"refresh")],
        [Button.inline(t(user_id, "btn_settings"), b"settings"), Button.inline(t(user_id, "btn_tools"), b"more_tools")],
    ]
    # P2P section (owner + test users only — hidden from everyone else)
    try:
        row = zap_p2p.main_menu_button(user_id)
        if row is not None:
            rows.append([row])
    except Exception as p2p_err:
        logging.debug(f"p2p menu button skipped: {p2p_err}")
    return rows


async def show_main_menu(event, edit=False, loading_msg=None):
    """Show main wallet menu with fiat value"""
    user_id = event.sender_id
    await init(user_id)

    # Mark user as active for payment monitoring
    await mark_user_active(user_id)

    balance = await get_balance(user_id)
    balance_display = await format_balance_with_fiat(balance, user_id)

    # Get fiat rate for display using user's preferred currency
    fiat_code = get_user_fiat(user_id)
    fiat_rate = await get_fiat_rate(fiat_code)
    fiat_emoji = get_fiat_emoji(fiat_code)
    rate_info = ""
    if fiat_rate:
        sats_per_unit = SATS_PER_BTC / fiat_rate  # Sats per 1 fiat unit
        # For low-value currencies (like IRT), show fiat per sat instead
        if sats_per_unit < 1:
            fiat_per_sat = fiat_rate / SATS_PER_BTC  # Fiat per 1 sat
            rate_info = t(user_id, "rate_line_sat",
                          emoji=fiat_emoji, value=fiat_per_sat, code=fiat_code)
        else:
            rate_info = t(user_id, "rate_line_unit",
                          emoji=fiat_emoji, value=int(sats_per_unit), code=fiat_code)

    message = f"""{t(user_id, 'app_title')}

{t(user_id, 'balance_line', amount=balance_display)}
{rate_info.strip()}

{t(user_id, 'backup_seed_hint')}
"""

    buttons = get_main_buttons(user_id)

    # Edit loading message if provided
    if loading_msg:
        await loading_msg.edit(message, buttons=buttons)
    elif edit:
        # Try to edit the message
        await event.edit(message, buttons=buttons)
    else:
        await client.send_message(event.chat_id, message, buttons=buttons)


async def show_help_menu(event):
    """Show help and guide for using the bot"""
    user_id = event.sender_id
    help_text = t(user_id, 'help_text')

    await event.edit(
        help_text,
        buttons=[[
            Button.inline(t(user_id, "btn_donate"), b"donate"),
            Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")
        ]]
    )


async def show_transaction_history(event, user_id, page=0):
    """Show transaction history with pagination and USD values"""
    try:
        await event.answer(t(user_id, "hist_loading"))

        payments = await get_payment_history(user_id, limit=50)

        if not payments:
            await event.edit(
                f"{t(user_id, 'hist_empty_title')}\n\n"
                f"{t(user_id, 'hist_empty')}",
                buttons=_row_back_menu(user_id)
            )
            return

        # Pagination
        items_per_page = 10
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        page_payments = payments[start_idx:end_idx]

        # Get USD rate for display
        usd_rate = await get_usd_rate()

        history_text = t(user_id, 'hist_title', page=page + 1) + "\n\n"

        for payment in page_payments:
            # Get payment details
            amount = getattr(payment, 'amount_sats', getattr(payment, 'amount', 0))
            timestamp = getattr(payment, 'timestamp', 0)
            status = getattr(payment, 'status', 'Unknown')
            details = getattr(payment, 'details', None)

            # Determine if incoming or outgoing
            direction_icon, direction_str, is_incoming = get_direction_info(payment, user_id)

            # Format timestamp
            time_str = format_timestamp(timestamp)

            # Format amount — cross-chain token payments show the TOKEN amount
            # (USDT/USDC) + network, not sats
            method_str = str(getattr(payment, "method", ""))
            is_token_payment = any(x in method_str for x in
                                   ("TOKEN", "DEPOSIT", "WITHDRAW"))
            chain_str = ""
            tx_hash = ""
            token_amount_str = ""
            if is_token_payment:
                det = getattr(payment, "details", None)
                conv = getattr(det, "conversion_info", None)
                if conv is not None:
                    tx_hash = getattr(conv, "external_tx_hash", "") or ""
                try:
                    import re as _re
                    mm = _re.search(r"chain=(\w+)", str(conv or ""))
                    if mm:
                        chain_str = mm.group(1).upper()
                except Exception:
                    pass
                # delivered_amount (receive) or amount (send) in token base units
                tok_base = getattr(conv, "delivered_amount", None) if conv else None
                if not tok_base and payment.payment_type is not None and \
                        str(getattr(payment, "payment_type", "")).endswith("SEND"):
                    tok_base = amount
                if tok_base:
                    tok_human = int(tok_base) / 1_000_000  # USDT/USDC: 6 decimals
                    token_amount_str = f"{tok_human:g} USDT"

            if is_token_payment and token_amount_str:
                if is_incoming:
                    amount_str = f"+{token_amount_str}"
                else:
                    amount_str = f"-{token_amount_str}"
            elif is_incoming:
                amount_str = f"+{int(amount):,}"
            else:
                amount_str = f"-{int(amount):,}"

            # Add USD value (sats payments only — token payments are already $)
            usd_str = ""
            if usd_rate and not (is_token_payment and token_amount_str):
                usd_value = sats_to_usd(int(amount), usd_rate)
                if usd_value is not None:
                    usd_str = f" (~${usd_value})"

            # Status emoji
            status_icon, status_text = get_status_icon_and_text(status, user_id)

            # Extract description/memo from payment details
            description = ""
            if details:
                if hasattr(details, 'description') and details.description:
                    description = f" 💬 {details.description[:30]}..." if len(details.description) > 30 else f" 💬 {details.description}"
                elif hasattr(details, 'memo') and details.memo:
                    description = f" 💬 {details.memo[:30]}..." if len(details.memo) > 30 else f" 💬 {details.memo}"

            # Check if transaction has tx_id (or a cross-chain tx hash)
            tx_indicator = " 🔗" if (hasattr(details, 'tx_id') and details.tx_id) or tx_hash else ""

            # Details deep-link for REGULAR payments (link_id under the 64-char
            # Telegram start cap); token rows use the full TXID as the tracker.

            # sats suffix only for non-token payments
            unit_str = "" if (is_token_payment and token_amount_str) else f" {t(user_id, 'sats_unit')}"
            row = [f"{direction_icon} **{direction_str}** {status_icon}{tx_indicator}",
                   f"   💰 {amount_str}{unit_str}{usd_str}{description}"]
            if chain_str:
                row.append(f"   📡 {chain_str}")
            row.append(f"   📅 {time_str}")
            if tx_hash:
                # token payment: the on-chain hash IS the tracker
                row.append(f"   🔖 TXID: `{tx_hash}`")
            else:
                # regular payment: Details deep link. Telegram caps the start
                # param at 64 chars — 64-hex ids (67 with 'dt_') overflow it, so
                # truncate to 40 chars; get_payment_by_id partial-matches the rest.
                payment_id = getattr(payment, 'id', None)
                link_id = payment_id.split(':', 1)[0][:40] if payment_id else None
                if link_id and bot_username:
                    row.append(f"   🔍 [{t(user_id, 'hist_details')}](https://t.me/{bot_username}?start=dt_{link_id})")
                elif link_id:
                    row.append(f"   🔍 /dt_{link_id}")
            history_text += "\n".join(row) + "\n\n"

        # Pagination buttons
        buttons = []
        nav_buttons = []

        if page > 0:
            nav_buttons.append(Button.inline(t(user_id, "btn_prev"), f"history_page_{page - 1}".encode()))

        if end_idx < len(payments):
            nav_buttons.append(Button.inline(t(user_id, "btn_next"), f"history_page_{page + 1}".encode()))

        if nav_buttons:
            buttons.append(nav_buttons)

        # Add scan button and back to menu
        buttons.append([
            Button.inline(t(user_id, "btn_scan_payment"), b"scan_payment"),
            Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")
        ])

        await event.edit(history_text, buttons=buttons)

    except Exception as error:
        await event.edit(
            t(user_id, 'hist_error', error=error),
            buttons=_row_back_menu(user_id)
        )
        logging.error(f"Transaction history error: {error}")


async def prepare_and_show_fee(event, user_id, invoice, amount):
    """Prepare payment and show fee confirmation with USD values"""
    try:
        res = await event.respond(t(user_id, "calc_loading"))

        prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, amount)

        user_data[user_id]['prepare_response'] = prepare_response
        user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=PAYMENT_COMPLETION_TIMEOUT_SECS)

        # Use spark_fee if available (even if 0), otherwise use fee_sats
        if spark_fee is not None:
            final_fee = spark_fee
        else:
            final_fee = fee_sats if fee_sats else 0

        user_data[user_id]['fee'] = final_fee
        total = amount + final_fee if amount else final_fee

        # Get USD values
        amount_usd = await format_amount_with_usd(amount, user_id) if amount else 'Variable'
        fee_usd = await format_amount_with_usd(final_fee, user_id)
        total_usd = await format_amount_with_usd(total, user_id)

        # Build recipient info if sending to username
        recipient_text = ""
        recipient_tips = ""
        resolved_user = user_data[user_id].get('resolved_user')
        buttons = []

        if resolved_user:
            ru = resolved_user
            has_started = ru.get('has_started_bot', False)

            # Show bot status
            if has_started:
                bot_status = "✅"
            else:
                bot_status = "⚠️"

            recipient_text = t(user_id, "pay_to_line", username=ru['username'],
                                name=_md_escape(str(ru['display_name'])),
                                status=bot_status)

            if ru['is_channel'] or ru['is_group']:
                recipient_tips = t(user_id, "pay_tip_group")
            elif not has_started:
                recipient_tips = t(user_id, "pay_tip_notstarted")
            # If has_started is True, no warning needed

            # Anonymous sender option (default: show sender)
            is_anonymous = user_data[user_id].get('send_anonymous', False)
            if is_anonymous:
                anon_text = t(user_id, "pay_sender_anon")
                anon_btn = Button.inline(t(user_id, "btn_show_name"), b"toggle_send_anon")
            else:
                anon_text = t(user_id, "pay_sender_visible")
                anon_btn = Button.inline(t(user_id, "btn_anon"), b"toggle_send_anon")

            recipient_text += f"{anon_text}\n\n"
            buttons.append([anon_btn])

        # Add password hint if user doesn't have one
        password_hint = ""
        if not has_send_password(user_id):
            password_hint = t(user_id, "pay_pw_hint")

        buttons.append([
            Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
            Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
        ])

        _q = (user_data.get(user_id) or {}).get('send_queue') or []
        _q_txt = (build_queue_text(_q, user_id)[0] + "\n\n" + "─" * 10 + "\n" + t(user_id, "queue_new_payment") + "\n") if _q else ""
        await res.edit(
            _q_txt + t(user_id, "pay_summary", recipient=recipient_text, amount=amount_usd,
                       fee=fee_usd, total=total_usd, tips=recipient_tips, hint=password_hint),
            buttons=buttons
        )

    except Exception as error:
        await res.edit(
            t(user_id, "pay_prep_err", error=str(error)),
            buttons=_row_back_menu(user_id)
        )
        if user_id in user_steps:
            del user_steps[user_id]


# ==================== Event Handlers ====================

@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/start.*'))
async def start_command(event):
    """Handle /start command with optional deep link parameter"""
    user_id = event.sender_id

    # Auto-detect language from the Telegram client on first contact
    if not db.get('user_lang') or str(user_id) not in (db.get('user_lang') or {}):
        detected = detect_lang_code(getattr(event.sender, 'lang_code', None) if event.sender else None)
        if detected:
            set_user_lang(user_id, detected)

    # Mark user as active
    await mark_user_active(user_id)

    # Check for deep link parameter (e.g., /start dt_PAYMENT_ID)
    text = (event.text or '').strip()
    if ' ' in text:
        param = text.split(' ', 1)[1].strip()

        # Handle payment details deep link
        if param.startswith('dt_'):
            payment_id = param[3:].split(':', 1)[0]  # strip prefix + any ':N' tail
            payment = await get_payment_by_id(user_id, payment_id)

            if payment:
                details_text = await format_payment_details(payment, payment_id, user_id=user_id)

                share_row = payment_share_button(payment_id, user_id=user_id)
                await event.respond(
                    details_text,
                    buttons=[
                        share_row if share_row else [],
                        [Button.inline(t(user_id, "btn_history"), b"history")],
                        [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                    ]
                )
                return
            else:
                await event.respond(
                    t(user_id, "pay_not_found", pid=payment_id),
                    buttons=_row_back_menu(user_id)
                )
                return
        elif param.startswith('p2p_'):
            # P2P order deep link from the listings channel
            try:
                oid = int(param[4:])
                await zap_p2p.deep_link_take(client, user_id, oid)
            except Exception as p2p_dl_err:
                logging.error(f'p2p deep link failed: {p2p_dl_err}')
                await event.respond('⚠️ سفارش در دسترس نیست.')
            return

    # Send loading message first for better UX
    loading_msg = await event.respond("⏳")
    await show_main_menu(event, loading_msg=loading_msg)


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/dt_(.+)'))
async def payment_details_command(event):
    """Show payment details by ID from /dt_PAYMENT_ID command"""
    user_id = event.sender_id
    await mark_user_active(user_id)

    # Extract payment ID from command
    match = re.match(r'/dt_(.+)', event.text)
    if not match:
        await event.respond(t(user_id, "dt_invalid_id"))
        return

    payment_id = match.group(1).strip()
    # Telegram start-param limit is 64 chars — composite ids ('hash:0') get
    # truncated in deep links; the part before ':' is enough (partial match).
    payment_id = payment_id.split(':', 1)[0]

    # Get payment details
    payment = await get_payment_by_id(user_id, payment_id)

    if payment:
        details_text = await format_payment_details(payment, payment_id, user_id=user_id)
        share_row = payment_share_button(payment_id, user_id=user_id)

        btns = ([share_row] if share_row else []) + [
            [Button.inline(t(user_id, "btn_history"), b"history")],
            [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
        ]
        await event.respond(details_text, buttons=btns)
    else:
        await event.respond(
            t(user_id, "pay_not_found", pid=payment_id),
            buttons=_row_back_menu(user_id)
        )


def _row_back_menu(uid):
    """Single back-to-menu row (dedup of the 43× inline literal; bytes identical)."""
    return [[Button.inline(t(uid, "btn_back_menu"), b"back_to_menu")]]


def _row_cancel_to_menu(uid):
    """btn_cancel → back_to_menu row (dedup; bytes identical)."""
    return [[Button.inline(t(uid, "btn_cancel"), b"back_to_menu")]]


def _row_cancel_settings(uid):
    """btn_cancel → settings_back row (dedup; bytes identical)."""
    return [[Button.inline(t(uid, "btn_cancel"), b"settings_back")]]


async def _cb_tktrack(event, user_id):
    """tktrack leaf (item 26): deposit tracking paused — inform only."""
    await event.answer(t(user_id, 'deposit_tracking_paused'), alert=True)


async def _cb_signmsg_help(event, user_id):
    """signmsg_help leaf (item 26): static sign/verify help screen."""
    await event.answer()
    await event.edit(
        t(user_id, "signmsg_help"),
        buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])


async def _cb_del_lnaddress(event, user_id):
    """del_lnaddress leaf (item 26): delete-confirmation screen — accidental
    taps must not delete instantly."""
    await event.answer()
    await event.edit(
        t(user_id, "lnaddr_del_confirm"),
        buttons=[
            [Button.inline(t(user_id, "btn_del_yes"), b"del_lnaddress_confirm")],
            [Button.inline(t(user_id, "btn_cancel"), b"change_ln_address")]
        ])


@client.on(events.CallbackQuery)
async def callback_handler(event):
    """Handle inline button callbacks"""
    user_id = event.sender_id
    data = event.data.decode()

    # Mark user as active on any interaction
    await mark_user_active(user_id)

    # Donation recipient user ID (configure this)
    DONATE_USER_ID = donate_user_id()

    try:
        # ==================== P2P section (owner + test users) ====================
        if data == "p2p" or data.startswith("p2p:"):
            await zap_p2p.on_callback(client, event, data)
            return

        # ==================== v30 NEW CALLBACKS (More Tools / Contacts / LN Address) ====================
        if data == "more_tools":
            await event.answer()
            await event.edit(
                t(user_id, "tools_title") + "\n\n",
                buttons=[
                    [Button.inline(t(user_id, "btn_calculator"), b"calc"), Button.inline(t(user_id, "btn_fees"), b"fees")],
                    [Button.inline(t(user_id, "btn_batch_pay"), b"batch_pay"), Button.inline(t(user_id, "btn_contacts"), b"contacts")],
                    [Button.inline(t(user_id, "btn_buy_bitcoin"), b"buy"), Button.inline(t(user_id, "btn_sync"), b"sync")],
                    [Button.inline(t(user_id, "btn_sign_msg"), b"signmsg_help")],
                    [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                ])
            return

        # ==================== USDT/USDC (Spark cross-chain) ====================
        if data == "token_deposit":
            await event.answer()
            rows = [
                [Button.inline("💵 USDT", b"tkdasset:USDT")],
                [Button.inline("💵 USDC", b"tkdasset:USDC")],
                [Button.inline(t(user_id, "btn_back"), b"receive")]
            ]
            await event.edit(t(user_id, "token_deposit_title"), buttons=rows)
            return

        if data.startswith("tkdasset:"):
            await event.answer()
            asset = data.split(":", 1)[1]
            try:
                import zap_tokens as zt
                sdk = await get_wallet(user_id)
                routes = await zt.get_routes(sdk)
                entries = zt.pick_routes(routes, asset)
                if not entries:
                    await event.edit(t(user_id, "token_no_routes"),
                                     buttons=[[Button.inline(t(user_id, "btn_back"), b"token_deposit")]])
                    return
                rows = []
                for entry in entries:
                    rows.append([Button.inline(
                        f"{entry['asset']} · {entry['chain'].upper()}",
                        f"tkdep:{entry['asset']}:{entry['chain']}".encode())])
                rows.append([Button.inline(t(user_id, "btn_back"), b"token_deposit")])
                await event.edit(t(user_id, "token_chain_pick", asset=asset), buttons=rows)
            except Exception as e:
                logging.error(f"tkdasset: {e}")
                await event.edit(t(user_id, "err_generic", error=zt.user_err_text(user_id, e)),
                                 buttons=[[Button.inline(t(user_id, "btn_back"), b"token_deposit")]])
            return

        if data.startswith("tkdep:"):
            await event.answer()
            try:
                _, asset, chain = data.split(":", 2)
                import zap_tokens as zt
                sdk = await get_wallet(user_id)
                await event.edit(t(user_id, "token_busy"))
                entry_all = zt.pick_routes(await zt.get_routes(sdk), asset)
                entry = next((r for r in entry_all if r["chain"].lower() == chain.lower()), None)
                if entry is None:
                    await event.edit(t(user_id, "token_no_routes"),
                                     buttons=[[Button.inline(t(user_id, "btn_back"), b"token_deposit")]])
                    return
                # Provider needs a real amount to quote/lock the rate — ask the user first
                user_steps[user_id] = "token_dep_amount"
                ud = user_data.setdefault(user_id, {})
                ud["tk_asset"] = asset
                ud["tk_chain"] = chain
                dec = entry["decimals"]
                # Live min/max hint (USD + sats) with observed fallbacks
                try:
                    bounds = await zt.route_bounds(sdk, "receive", asset, chain)
                except Exception:
                    bounds = {"min_usd": zt.RECEIVE_MIN_USDT, "max_usd": None}
                usd_rate = await get_usd_rate()
                note_lines = []
                if bounds.get("min_usd"):
                    mn = bounds["min_usd"]
                    mn_sats = usd_to_sats(mn, usd_rate)
                    s = f" (~{mn_sats:,} {t(user_id, 'sats_unit')})" if mn_sats else ""
                    note_lines.append(t(user_id, "dep_min_note", amount=f"{mn:g}", sats=s))
                if bounds.get("max_usd"):
                    mx = bounds["max_usd"]
                    mx_sats = usd_to_sats(mx, usd_rate)
                    s = f" (~{mx_sats:,} {t(user_id, 'sats_unit')})" if mx_sats else ""
                    note_lines.append(t(user_id, "dep_max_note", amount=f"{mx:g}", sats=s))
                min_note = ("\n" + "\n".join(note_lines)) if note_lines else ""
                await event.edit(
                    t(user_id, "token_dep_amount",
                      asset=asset, chain=chain.upper()) + min_note,
                    buttons=_row_cancel_to_menu(user_id))
            except Exception as e:
                logging.error(f"tkdep: {e}")
                await event.edit(t(user_id, "err_generic", error=zt.user_err_text(user_id, e)),
                                 buttons=[[Button.inline(t(user_id, "btn_back"), b"token_deposit")]])
            return

        if data.startswith("tkwd_max:"):
            await event.answer()
            try:
                import zap_tokens as zt
                ud_asset = data.split(":", 1)[1]
                sdk = await get_wallet(user_id)
                raw_usdb = await zt.usdb_raw_balance(sdk)
                bal_usdb = zt.format_base_units(raw_usdb, 6)
                if raw_usdb <= 0:
                    await event.edit(t(user_id, "token_amount_bad"),
                                     buttons=_row_cancel_to_menu(user_id))
                    return
                ud = user_data.get(user_id) or {}
                addr = ud.get("tk_address", "")
                if user_steps.get(user_id) != 'token_wd_amount' or ud_asset != ud.get('tk_asset'):
                    await event.edit(token_withdraw_notice(user_id, 'failed'))
                    return
                ud["tk_amount"] = bal_usdb
                ud["tk_net_amount"] = bal_usdb
                ud["tk_amount_base"] = zt.to_base_units(bal_usdb, 6)
                fee_sats = None
                q_fee, q_total, quote = await token_wd_quote_totals(
                    sdk, ud_asset, (ud.get("tk_chain", "") or ""), addr, bal_usdb, fee_sats,
                    ud.get("tk_dest_details"))
                if q_fee is None:
                    await event.edit(token_withdraw_notice(user_id, 'failed'),
                                     buttons=_row_cancel_to_menu(user_id))
                    return
                if quote[6] > await zt.usdb_raw_balance(sdk):
                    await event.edit(t(user_id, "token_amount_high", balance=zt.format_base_units(await zt.usdb_raw_balance(sdk), 6)))
                    return
                total_note = ""
                ud["tk_quote"] = quote
                ud["tk_confirm_nonce"] = secrets.token_hex(12)
                user_steps[user_id] = "token_wd_confirm"
                await event.edit(
                    t(user_id, "token_wd_confirm",
                      asset=ud_asset, amount=zt.format_base_units(quote[8], quote[0][5]), address=addr,
                      chain=(ud.get("tk_chain", "") or "").upper(), fee="—",
                      svc_fee="0",
                      svc_sats="0", input_amount=zt.format_base_units(quote[6], 6),
                      network_fee=q_fee, transfer_fee=str(quote[10]),
                      net=bal_usdb, sats="—") + total_note,
                    buttons=[[Button.inline(t(user_id, "btn_confirm_wd"), f"tkwd_go:{ud['tk_confirm_nonce']}".encode())],
                             [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
            except Exception as e:
                logging.error(f"tkwd_max: {e}")
                await event.edit(t(user_id, "err_generic", error=zt.user_err_text(user_id, e)),
                                 buttons=[[Button.inline(t(user_id, "btn_cancel"), b"token_withdraw")]])
            return

        if data == "tktrack":
            await _cb_tktrack(event, user_id)
            return

        if data == "token_withdraw":
            await event.answer()
            rows = [
                [Button.inline("💵 USDT", b"tkwasset:USDT")],
                [Button.inline("💵 USDC", b"tkwasset:USDC")],
                [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
            ]
            await event.edit(t(user_id, "token_withdraw_pick"), buttons=rows)
            return

        if data.startswith("tkwasset:"):
            await event.answer()
            asset = data.split(":", 1)[1]
            try:
                import zap_tokens as zt
                sdk = await get_wallet(user_id)
                routes = await zt.get_routes(sdk)
                entries = zt.pick_routes(routes, asset)
                if not entries:
                    await event.edit(t(user_id, "token_no_routes"),
                                     buttons=[[Button.inline(t(user_id, "btn_cancel"), b"token_withdraw")]])
                    return
                rows = []
                for entry in entries:
                    rows.append([Button.inline(
                        f"{entry['asset']} · {entry['chain'].upper()}",
                        f"tkwd:{entry['asset']}:{entry['chain']}".encode())])
                rows.append([Button.inline(t(user_id, "btn_cancel"), b"token_withdraw")])
                await event.edit(t(user_id, "token_chain_pick", asset=asset),
                                 buttons=rows)
            except Exception as e:
                logging.error(f"tkwasset: {e}")
                await event.edit(t(user_id, "err_generic", error=zt.user_err_text(user_id, e)),
                                 buttons=[[Button.inline(t(user_id, "btn_cancel"), b"token_withdraw")]])
            return

        if data.startswith("tkwd:"):
            await event.answer()
            try:
                _, asset, chain = data.split(":", 2)
                user_steps[user_id] = "token_wd_address"
                ud = user_data.setdefault(user_id, {})
                ud["tk_asset"] = asset
                ud["tk_chain"] = chain
                await event.edit(
                    t(user_id, "token_withdraw_title",
                      asset=asset,
                      chain=chain.upper()),
                    buttons=_row_cancel_to_menu(user_id))
            except Exception as e:
                logging.error(f"tkwd: {e}")
                await event.edit(t(user_id, "err_generic", error=zt.user_err_text(user_id, e)),
                                 buttons=_row_cancel_to_menu(user_id))
            return

        if data == "tkwd_go" or data.startswith("tkwd_go:"):
            nonce = data.partition(':')[2]
            current = user_data.get(user_id) or {}
            if not nonce or user_steps.get(user_id) != "token_wd_confirm" or nonce != current.get("tk_confirm_nonce"):
                await event.answer(t(user_id, "session_expired"), alert=True)
                return
            if not getattr(event, 'is_private', False):
                await event.answer(t(user_id, 'token_wd_private_only'), alert=True)
                return
            binding = token_withdraw_auth_binding(user_id, current)
            if has_send_password(user_id):
                if (current.pop('tk_auth_verified', None) != binding
                        or current.pop('tk_auth_password_hash', None) != get_user_send_password_hash(user_id)):
                    current['tk_auth_pending'] = binding
                    user_steps[user_id] = 'verify_send_password_for_token'
                    await event.answer()
                    await event.edit(t(user_id, 'token_wd_password_prompt'),
                        buttons=[[Button.inline(t(user_id, 'btn_cancel'), b'back_to_menu')]])
                    return
            else:
                current.pop('tk_auth_verified', None)
                current.pop('tk_auth_password_hash', None)
            current.pop('tk_auth_pending', None)
            ud = dict(current)
            asset = ud.get("tk_asset"); chain = ud.get("tk_chain")
            addr = ud.get("tk_address"); amount = ud.get("tk_amount")
            dest_details = ud.get("tk_dest_details")
            if not (asset and chain and addr and amount):
                await event.answer(t(user_id, "session_expired"), alert=True)
                return
            try:
                claimed = token_withdraw_claim(user_id, nonce, {
                    'asset': asset, 'chain': chain, 'address': addr, 'amount': amount,
                    'quote': ud.get('tk_quote')})
            except Exception as e:
                logging.error(f"token withdrawal claim failed: {e}")
                await event.answer(t(user_id, "wd_process_fail"), alert=True)
                return
            if not claimed:
                await event.answer()
                await event.respond(token_withdraw_notice(user_id, 'blocked'))
                return
            user_steps[user_id] = "token_wd_processing"
            sending = False
            outcome = None
            payment = None
            try:
                await event.answer()
                # local import kept on purpose: sys.modules injection seam for
                # the offline exec-isolated tkwd_go test (item 27)
                import zap_tokens as zt
                sdk = await get_wallet(user_id)
                # SEND filter needs the parsed address details
                address_details = cross_chain_address_details(dest_details)
                entry_all = zt.pick_routes(
                    await zt.get_routes(sdk, direction="send", address_details=address_details),
                    asset)
                entry = next((r for r in entry_all if r["chain"].lower() == chain.lower()), None)
                if entry is None:
                    token_withdraw_finish(user_id, nonce, 'safe_failed', '')
                    await event.edit(t(user_id, "token_no_routes"),
                                     buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                # ALWAYS re-derive from the confirmed amount — never trust a stale
                # tk_amount_base left in user_data by an earlier max-button press
                amount_base = zt.to_base_units(amount, 6)
                prep, entry2, spark_asset, amount_in_needed, wd_req = await zt.prepare_withdraw(
                    sdk, asset, chain, addr, amount_base,
                    address_details)
                # show the quoted fee from the prepare response
                fee = 0
                pm = getattr(prep, "payment_method", None)
                fee = int(pm.fee_amount)
                dec = entry["decimals"]
                actual_quote = zt.withdraw_quote(prep, entry2['pair'], addr, zt.USDB_TOKEN_ID)
                if actual_quote != ud.get('tk_quote'):
                    token_withdraw_finish(user_id, nonce, 'safe_failed', '')
                    await event.edit(token_withdraw_notice(user_id, 'quote_changed'),
                                     buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                fee_h = zt.from_base_units(fee, dec)
                await event.edit(t(user_id, "token_wd_started"))
                sending = True
                token_withdraw_prepare_send(user_id, nonce, zt.withdraw_quote_id(prep))
                payment = await zt.execute_withdraw(
                    sdk, prep, amount_in_needed=amount_in_needed,
                    req=wd_req)
                status = getattr(payment, "status", None)
                outcome = zt.withdraw_source_outcome(status)
                token_withdraw_finish(user_id, nonce, outcome, getattr(payment, 'id', ''))
                menu_btn = [[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                net_amount = amount
                tx_ref = ""
                try:
                    det = getattr(payment, "details", None)
                    ci = getattr(det, "conversion_info", None)
                    if ci:
                        tx_ref = getattr(ci, "external_tx_hash", None) or getattr(ci, "order_id", None) or ""
                except Exception:
                    pass
                if outcome == 'completed':
                    await client.send_message(user_id, t(user_id, "token_wd_done",
                                                         asset=asset,
                                                         amount=zt.fmt_amount(net_amount),
                                                         address=addr, chain=chain.upper(),
                                                         tx=tx_ref or getattr(payment, "id", "")),
                                              buttons=menu_btn)
                else:
                    logging.info(f"token wd {outcome}: {getattr(payment, 'id', '?')} status={status}")
                    await client.send_message(user_id, token_withdraw_notice(
                        user_id, outcome, tx_ref or getattr(payment, 'id', '')), buttons=menu_btn)
                user_steps.pop(user_id, None)
                ud.pop("tk_asset", None); ud.pop("tk_chain", None)
                ud.pop("tk_address", None); ud.pop("tk_amount", None); ud.pop("tk_amount_base", None)
                ud.pop("tk_dest_details", None)
                ud.pop("tk_service_fee", None); ud.pop("tk_net_amount", None)
                ud.pop("tk_fee_sats", None)
            except Exception:
                logging.error('token withdrawal submission failed; check hold state')
                if not sending:
                    token_withdraw_finish(user_id, nonce, 'safe_failed', '')
                if outcome == 'completed':
                    message = t(user_id, "token_wd_done", asset=asset,
                                amount=zt.fmt_amount(amount), address=addr, chain=chain.upper(),
                                tx=getattr(payment, 'id', ''))
                else:
                    message = token_withdraw_notice(user_id, outcome or ('unknown' if sending else 'failed'),
                                                    getattr(payment, 'id', ''))
                await client.send_message(user_id, message,
                                          buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
            return

        if data == "language_menu" or data.startswith("setlang:"):
            if data == "language_menu":
                await event.answer()
                current = get_user_lang(user_id)
                rows, row = [], []
                for code in SUPPORTED_LANGS:
                    name = LANG_NAMES.get(code, code)
                    mark = "✓ " if code == current else ""
                    row.append(Button.inline(f"{mark}{name}", f"setlang:{code}".encode()))
                    if len(row) == 2:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                rows.append([Button.inline(t(user_id, "btn_back_settings"), b"settings")])
                await event.edit(
                    t(user_id, "select_language", current=LANG_NAMES.get(current, current)),
                    buttons=rows)
                return
            # setlang:<code>
            lang = data.split(":", 1)[1]
            set_user_lang(user_id, lang)
            await event.answer(t(user_id, "lang_set", lang=LANG_NAMES.get(lang, lang)))
            # re-render language menu with new current
            current = get_user_lang(user_id)
            rows, row = [], []
            for code in SUPPORTED_LANGS:
                name = LANG_NAMES.get(code, code)
                mark = "✓ " if code == current else ""
                row.append(Button.inline(f"{mark}{name}", f"setlang:{code}".encode()))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([Button.inline(t(user_id, "btn_back_settings"), b"settings")])
            await event.edit(
                t(user_id, "select_language", current=LANG_NAMES.get(current, current)),
                buttons=rows)
            return

        if data == "calc":
            await event.answer()
            user_steps[user_id] = "calc_input"
            fiat_code = get_user_fiat(user_id)
            await event.edit(
                t(user_id, "calc_title", fiat=fiat_code),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"more_tools")]])
            return

        if data == "contacts":
            await event.answer()
            await show_contacts(event, edit=True)
            return

        if data == "buy":
            await event.answer(t(user_id, "load_generic"))
            try:
                url = await buy_bitcoin_url(user_id)
                await event.edit(t(user_id, "buy_bitcoin", url=url),
                                 buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]],
                                 link_preview=False)
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "fees":
            await event.answer(t(user_id, "load_fees"))
            try:
                fees = await get_recommended_fees(user_id)
                await event.edit(_format_fees(fees, user_id),
                                 buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "sync":
            await event.answer(t(user_id, "syncing"))
            try:
                info = await sync_user_wallet(user_id)
                await event.edit(
                    t(user_id, "synced", balance=f"{getattr(info, 'balance_sats', 0):,}"),
                    buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "signmsg_help":
            await _cb_signmsg_help(event, user_id)
            return

        if data == "del_lnaddress":
            # Confirmation step — accidental taps must not delete instantly
            await _cb_del_lnaddress(event, user_id)
            return

        if data == "del_lnaddress_confirm":
            await event.answer(t(user_id, "deleting"))
            try:
                await delete_lightning_address(user_id)
                await event.edit(t(user_id, "lnaddr_deleted"),
                                 buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]])
            return

        if data.startswith("contactpay_"):
            idx = data.split("_", 1)[1]
            cmap = (user_data.get(user_id) or {}).get('contact_map') or {}
            contact = cmap.get(idx)
            if not contact:
                await event.answer(t(user_id, "contact_expired"), alert=True)
                return
            user_data.setdefault(user_id, {})['contact_pay'] = {
                'name': contact['name'], 'identifier': contact['identifier']}
            user_steps[user_id] = "contact_pay_amount"
            await event.answer()
            await event.edit(
                t(user_id, "contact_pay_prompt", name=contact['name']),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"contacts")]])
            return

        if data.startswith("contactdel_"):
            idx = data.split("_", 1)[1]
            cmap = (user_data.get(user_id) or {}).get('contact_map') or {}
            contact = cmap.get(idx)
            if not contact:
                await event.answer(t(user_id, "contact_expired"), alert=True)
                return
            try:
                await delete_user_contact(user_id, contact['id'])
                await event.answer(t(user_id, "contact_deleted_alert"), alert=True)
            except Exception as de:
                await event.answer(t(user_id, "contact_del_failed", error=str(de)), alert=True)
                return
            await show_contacts(event, edit=True)
            return

        if data == "confcontactpay":
            cp = (user_data.get(user_id) or {}).get('contact_pay') or {}
            prep = cp.get('prepare')
            if not prep:
                await event.answer(t(user_id, "session_expired"), alert=True)
                return
            await event.answer(t(user_id, "sending"))
            try:
                await send_payment(user_id, prep)
            except Exception as se:
                user_steps.pop(user_id, None)
                await event.edit(t(user_id, "contact_pay_failed", error=str(se)),
                                 buttons=[[Button.inline(t(user_id, "btn_back_contacts"), b"contacts")]])
                return
            name = cp.get('name', 'contact')
            user_steps.pop(user_id, None)
            (user_data.get(user_id) or {}).pop('contact_pay', None)
            await event.edit(
                t(user_id, "contact_sent", name=name),
                buttons=_row_back_menu(user_id))
            return

        if data == "done":
            await event.answer()
            try:
                msg = await event.get_message()
                if msg:
                    await msg.delete()
            except:
                pass
            return

        if data == "refresh":
            await event.edit("⏳")
            await show_main_menu(event, edit=True)
            await event.answer(t(user_id, "refresh_done"))
            return

        elif data == "help":
            await event.answer()
            await show_help_menu(event)
            return

        elif data == "history":
            await show_transaction_history(event, user_id, page=0)
            return

        elif data.startswith("history_page_"):
            page = int(data.split("_")[2])
            await show_transaction_history(event, user_id, page=page)
            return

        elif data.startswith("track_"):
            # Track payment by ID - use the reusable helper
            payment_id = data[6:]  # Remove "track_" prefix
            await event.answer(t(user_id, "load_search"))

            logging.info(f"[track_] user={user_id} searching for payment_id={payment_id!r}")

            payment = await get_payment_by_id(user_id, payment_id)

            if payment:
                details_text = await format_payment_details(payment, payment_id, user_id=user_id)
                share_row = payment_share_button(payment_id, user_id=user_id)

                btns = ([share_row] if share_row else []) + [
                    [Button.inline(t(user_id, "btn_history"), b"history")],
                    [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                ]
                await event.edit(details_text, buttons=btns)
            else:
                # Debug: Get payment list to show what IDs exist
                try:
                    sdk = await get_wallet(user_id)
                    response = await sdk.list_payments(request=ListPaymentsRequest())
                    sample_ids = [getattr(p, 'id', 'N/A') for p in (response.payments or [])[:10]]
                    logging.info(f"[track_] Payment not found. Sample IDs in wallet: {sample_ids}")
                    await close_wallet(sdk)
                except Exception as e:
                    logging.error(f"[track_] Debug list failed: {e}")

                await event.edit(
                    t(user_id, "pay_not_found_id", pid=payment_id),
                    buttons=_row_back_menu(user_id)
                )
            return

        elif data == "exit_confirm":
            await event.answer(t(user_id, "signing"))
            ud = user_data.get(user_id) or {}
            prep = ud.get('exit_prepared')
            addr = ud.get('exit_address', '?')
            if not prep:
                await event.edit(t(user_id, "exit_expired"), buttons=None)
                return
            try:
                sdk = await get_wallet(user_id)
                from breez_sdk_spark import UnilateralExitRequest, CpfpSigner

                class _NoopSigner(CpfpSigner):
                    async def sign_psbt(self, psbt_bytes):
                        return psbt_bytes  # no external funding inputs to sign

                # unilateral_exit builds the FULLY SIGNED tx set but does NOT broadcast.
                resp = await sdk.unilateral_exit(
                    UnilateralExitRequest(prepared=prep, funding_inputs=[]),
                    signer=_NoopSigner(),
                )
                txs = list(getattr(resp, 'transactions', []) or [])
                if not txs:
                    await event.edit(t(user_id, "exit_no_txs"), buttons=None)
                    return
                # v34: persist per-tx kind/status/deps so the user can broadcast
                # correctly per docs (package relay, order, timelocks).
                ud['exit_signed_txs'] = [
                    {'txid': getattr(x, 'txid', ''), 'hex': getattr(x, 'tx_hex', ''),
                     'cpfp': getattr(x, 'cpfp_tx_hex', None) or '',
                     'kind': str(getattr(x, 'kind', '') or ''),
                     'status': str(getattr(x, 'status', '') or ''),
                     'timelock': int(getattr(x, 'csv_timelock_blocks', 0) or 0),
                     'depends_on': list(getattr(x, 'depends_on', []) or [])}
                    for x in txs
                ]
                recov = int(getattr(resp, 'recoverable_value_sat', 0) or 0)
                fee = int(getattr(resp, 'total_fee_sat', 0) or 0)
                n_confirmed = sum(1 for x in ud['exit_signed_txs'] if 'Confirmed' in x['status'])
                lines = [t(user_id, "exit_signed_hdr", n=len(txs))]
                for i, x in enumerate(ud['exit_signed_txs'], 1):
                    kind_tag = x['kind'].split('.')[-1].rstrip('()') if x['kind'] else f"tx{i}"
                    status_tag = (t(user_id, "exit_onchain_tag") if 'Confirmed' in x['status'] else "")
                    lines.append(t(user_id, "exit_tx_line", i=i, kind=kind_tag,
                                   txid=x['txid'][:24], size=len(x['hex'])//2, status=status_tag))
                if n_confirmed:
                    lines.append(t(user_id, "exit_onchain_note", n=n_confirmed))
                lines.append(t(user_id, "exit_value_line", recov=f"{recov:,}", fee=f"{fee:,}"))
                lines.append(t(user_id, "exit_to_line", addr=addr))
                lines.append(t(user_id, "exit_broadcast_help"))
                await event.answer()
                # v34: send raw hex as message + file, NO broadcast button
                hex_payload = "\n\n".join(
                    f"txid: {x['txid']}\n"
                    f"kind: {x['kind']}\n"
                    f"timelock_blocks: {x['timelock']}\n"
                    f"depends_on: {', '.join(x['depends_on']) or '-'}\n"
                    f"status: {x['status']}\n"
                    f"{x['hex']}" + (f"\ncpfp: {x['cpfp']}" if x['cpfp'] else "")
                    for x in ud['exit_signed_txs'])
                fname = f"zap-exit-signed-{user_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
                await client.send_file(event.chat_id, io.BytesIO(hex_payload.encode()),
                                       force_document=True,
                                       attributes=[tl_types.DocumentAttributeFilename(fname)],
                                       caption=t(user_id, "exit_file_caption"))
                await event.edit("\n".join(lines), buttons=[
                    [Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]
                ])
            except Exception as e:
                logging.error(f"exit sign: {e}")
                await event.edit(
                    t(user_id, "exit_sign_failed", error=str(e)),
                    buttons=[[Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]])
            return

        # (v34: exit_save_hex and exit_broadcast handlers removed — hex is now
        # auto-sent as a file at sign time, and broadcasting is user-side only.
        # NOTE from official docs: CPFP pairs need package relay
        # (bitcoin-cli submitpackage); mempool.space rejects zero-fee parents.)

        elif data == "emergency_exit":
            await event.answer()
            await event.edit(
                t(user_id, "exit_menu"),
                buttons=[
                    [Button.inline(t(user_id, "btn_export_bk"), b"exit_export_state")],
                    [Button.inline(t(user_id, "btn_quote_exit"), b"exit_prepare")],
                    [Button.inline(t(user_id, "btn_back_settings"), b"settings")]
                ])
            return

        elif data == "exit_export_state":
            await event.answer(t(user_id, "exporting"))
            try:
                sdk = await get_wallet(user_id)
                resp = await sdk.export_unilateral_exit_state()
                state = getattr(resp, 'exit_state', None) or str(resp)
                fname = f"zap-exit-state-{user_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
                out = t(user_id, "exit_export_card", state=state[:3500])
                if len(state) > 3500:
                    await client.send_file(event.chat_id, io.BytesIO(state.encode()),
                                           force_document=True,
                                           attributes=[tl_types.DocumentAttributeFilename(fname)],
                                           caption=t(user_id, "exit_export_caption", fname=fname))
                    out = t(user_id, "exit_export_done")
                await event.edit(out, buttons=[[Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]])
            return

        elif data == "exit_prepare":
            await event.answer()
            user_steps[user_id] = "exit_address_input"
            await event.edit(
                t(user_id, "fw_prompt"),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"emergency_exit")]])
            return

        elif data == "webhook_list":
            await event.answer(t(user_id, "load_webhooks"))
            try:
                sdk = await get_wallet(user_id)
                hooks = await sdk.list_webhooks() or []
                if not hooks:
                    txt = t(user_id, "wh_none")
                else:
                    lines = [t(user_id, "wh_list_title")]
                    for w in hooks:
                        lines.append(f"• `{getattr(w,'url','?')}`")
                    txt = "\n".join(lines)
                await event.edit(txt,
                    buttons=[
                        [Button.inline(t(user_id, "btn_reg_url"), b"webhook_add")],
                        [Button.inline(t(user_id, "btn_remove"), b"webhook_del")],
                        [Button.inline(t(user_id, "btn_back_settings"), b"settings")]
                    ])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]])
            return

        elif data == "webhook_add":
            await event.answer()
            user_steps[user_id] = "webhook_url_input"
            await event.edit(
                t(user_id, "wh_add"),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"webhook_list")]])
            return

        elif data == "webhook_del":
            await event.answer()
            user_steps[user_id] = "webhook_del_input"
            await event.edit(
                t(user_id, "wh_del"),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"webhook_list")]])
            return

        elif data == "queue_clear":
            await event.answer()
            ud = user_data.get(user_id)
            if ud:
                ud.pop('send_queue', None)
                ud.pop('batch_prep', None)
                ud.pop('batch_items', None)
                ud.pop('batch_pairs', None)
                ud.pop('batch_id', None)
                ud.pop('batch_auth', None)
                ud.pop('batch_auth_pending', None)
            _batch_clear(user_id)  # the durable batch queue is cleared too
            user_steps.pop(user_id, None)
            await event.edit(t(user_id, "queue_cleared"),
                             buttons=_row_back_menu(user_id))
            return

        # (v34: legacy paste-queue handlers queue_send_all / queue_send_all_confirmed
        # removed — unreachable, superseded by interactive Batch Pay
        # (batch_send_all / batch_add_more). queue_clear kept: batch Cancel All uses it.)

        # v33: add another after batch started — ask next destination
        elif data == "batch_add_more":
            await event.answer()
            ud = user_data.get(user_id) or {}
            if not ud.get('batch_items'):
                await event.edit(t(user_id, "batch_empty_restart"), buttons=None)
                return
            user_steps[user_id] = "batch_dest"
            await event.edit(
                t(user_id, "batch_recipient_prompt", n=len(ud['batch_items']) + 1),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"more_tools")]])
            return

        elif data == "batch_pay":
            await event.answer()
            user_data[user_id] = user_data.get(user_id) or {}
            _batch_abandon(user_id)  # earlier unsent batches are held as unknown, never re-sent
            user_data[user_id]['batch_id'] = 'b-' + secrets.token_hex(8)
            user_data[user_id]['batch_items'] = []      # prepared payments
            user_data[user_id]['batch_pairs'] = []      # display lines
            user_data[user_id].pop('batch_auth', None)
            user_data[user_id].pop('batch_auth_pending', None)
            user_steps[user_id] = "batch_dest"
            await event.edit(
                t(user_id, "batch_intro"),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"more_tools")]]
            )
            return

        elif data == "batch_send_all":
            await _batch_send_all(event, user_id)
            return

        elif data == "conversion_limits":
            await event.answer(t(user_id, "load_limits"))
            try:
                sdk = await get_wallet(user_id)
                resp = await sdk.fetch_conversion_limits(FetchConversionLimitsRequest())
                await event.edit(
                    t(user_id, "conv_limits", resp=resp),
                    buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        elif data == "refund_conversions":
            await event.answer(t(user_id, "refunding_convs"))
            try:
                sdk = await get_wallet(user_id)
                resp = await sdk.refund_pending_conversions()
                n = len(getattr(resp, 'refunds', []) or [])
                await event.edit(
                    t(user_id, "refund_convs_done", n=n),
                    buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            except Exception as e:
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        elif data == "settings":
            await event.answer()
            current_fiat = get_user_fiat(user_id)
            fiat_emoji = get_fiat_emoji(current_fiat)
            has_password = has_send_password(user_id)
            password_status = "✅" if has_password else "➖"
            password_btn_text = t(user_id, "btn_send_password") + (" ✏️" if has_password else "")

            # Get private mode status
            private_mode = await get_private_mode(user_id)
            private_status = "✅" if private_mode else "➖"
            private_btn_text = t(user_id, "btn_private_mode") + (" ➖" if private_mode else " ✅")

            lang_name = LANG_NAMES.get(get_user_lang(user_id), get_user_lang(user_id))

            await event.edit(
                f"{t(user_id, 'settings_title')}\n\n"
                f"{fiat_emoji} {t(user_id, 'btn_change_fiat')}: **{current_fiat}**\n"
                f"{t(user_id, 'btn_send_password')}: {password_status}\n"
                f"{t(user_id, 'btn_private_mode')}: {private_status}\n"
                f"{t(user_id, 'btn_language')}: {lang_name}",
                buttons=[
                    [Button.inline(t(user_id, "btn_backup_seed"), b"backup")],
                    [Button.inline(t(user_id, "btn_restore"), b"recovery"), Button.inline(t(user_id, "btn_deposits"), b"unclaimed_deposits")],
                    [Button.inline(t(user_id, "btn_ln_address"), b"change_ln_address"), Button.inline(t(user_id, "btn_change_fiat"), b"change_fiat")],
                    [Button.inline(password_btn_text, b"set_send_password"), Button.inline(private_btn_text, b"toggle_private_mode")],
                    [Button.inline(t(user_id, "btn_language"), b"language_menu"), Button.inline(t(user_id, "btn_help"), b"help")],
                    [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                ]
            )
            return

        elif data == "backup":
            await event.answer()
            # Check if user has password set - require it before showing seed
            if has_send_password(user_id):
                user_steps[user_id] = "verify_password_for_backup"
                hint = get_password_hint(user_id)
                hint_text = t(user_id, "pw_hint_hint", hint=hint) if hint else ""
                await event.edit(
                    t(user_id, "pw_required_backup", hint=hint_text),
                    buttons=[
                        [Button.inline(t(user_id, "btn_cancel"), b"settings_back")]
                    ]
                )
                # Store the prompt message ID for later editing
                if user_id not in user_data:
                    user_data[user_id] = {}
                user_data[user_id]['password_prompt_msg_id'] = event.message_id
            else:
                # No password set - show seed directly
                encrypted_seed = db.get(f'{user_id}')
                seed = decrypt_seed(encrypted_seed)
                await event.edit(
                    t(user_id, "seed_show", seed=seed),
                    buttons=_row_cancel_settings(user_id)
                )

        elif data == "recovery":
            user_steps[user_id] = "recovery_input"
            await event.answer()
            await event.edit(
                t(user_id, "recovery_card"),
                buttons=_row_cancel_settings(user_id)
            )

        elif data == "change_ln_address":
            try:
                address = await get_lightning_address(user_id)
                await event.answer()
                if address and getattr(address, 'lightning_address', None):
                    await event.edit(
                        t(user_id, "lnaddr_current",
                          addr=address.lightning_address, lnurl=address.lnurl),
                        buttons=[
                            [Button.inline(t(user_id, "btn_chaddr"), b"confirm_change_ln_address")],
                            [Button.inline(t(user_id, "btn_del_addr"), b"del_lnaddress")],
                            [Button.inline(t(user_id, "btn_back_settings"), b"settings")]
                        ]
                    )
                else:
                    await event.edit(
                        t(user_id, "lnaddr_none"),
                        buttons=[
                            [Button.inline(t(user_id, "btn_regaddr"), b"confirm_change_ln_address")],
                            [Button.inline(t(user_id, "btn_back_settings"), b"settings")]
                        ]
                    )
            except Exception as error:
                await event.answer(t(user_id, "err_alert_line", error=str(error)), alert=True)

        elif data == "confirm_change_ln_address":
            user_steps[user_id] = "change_ln_address_input"
            await event.edit(
                t(user_id, "lnaddr_change"),
                buttons=_row_cancel_settings(user_id)
            )

        # ==================== PRIVATE MODE ====================
        elif data == "toggle_private_mode":
            await event.answer(t(user_id, "updated"))
            current_mode = await get_private_mode(user_id)
            new_mode = not current_mode
            success = await set_private_mode(user_id, new_mode)

            if success:
                status_text = t(user_id, "pmode_enabled") if new_mode else t(user_id, "pmode_disabled")
                status_icon = "✅" if new_mode else "❌"
                privacy_note = (t(user_id, "pmode_status_on") if new_mode
                                else t(user_id, "pmode_status_off"))
                await event.edit(
                    t(user_id, "pmode_card", icon=status_icon, status=status_text,
                      note=privacy_note),
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                )
            else:
                await event.edit(
                    t(user_id, "pmode_failed"),
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                )
            return

        # ==================== FIAT CURRENCY ====================
        elif data == "change_fiat":
            await event.answer()
            try:
                # Get available fiat currencies from SDK + Yadio
                rates = await get_fiat_rates()
                current_fiat = get_user_fiat(user_id)
                current_emoji = get_fiat_emoji(current_fiat)

                # Common fiat currencies to show first
                common_fiats = ['USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'CNY', 'INR', 'RUB', 'TRY', 'AED', 'BRL', 'KRW', 'MXN', 'SAR', 'IRT', 'IQD']

                # Build buttons - show common ones first
                buttons = []
                row = []

                # Add common currencies with flag emojis
                for fiat in common_fiats:
                    if fiat in rates:
                        emoji = get_fiat_emoji(fiat)
                        check = "✓" if fiat == current_fiat else ""
                        row.append(Button.inline(f"{emoji}{check}{fiat}", f"set_fiat:{fiat}".encode()))
                        if len(row) == 3:
                            buttons.append(row)
                            row = []

                if row:
                    buttons.append(row)

                buttons.append([Button.inline(t(user_id, "btn_back_settings"), b"settings")])

                await event.edit(
                    t(user_id, "fiat_select", emoji=current_emoji, fiat=current_fiat),
                    buttons=buttons
                )
            except Exception as error:
                logging.error(f"Error loading fiat currencies: {error}")
                await event.edit(
                    t(user_id, "fiat_load_err", error=str(error)),
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                )

        elif data.startswith("set_fiat:"):
            fiat_code = data.split(":")[1]
            set_user_fiat(user_id, fiat_code)
            fiat_emoji = get_fiat_emoji(fiat_code)
            await event.answer(f"✅ {fiat_emoji} {fiat_code}")
            # Re-render the fiat picker (current selection updated)
            rates = await get_fiat_rates()
            current_fiat = get_user_fiat(user_id)
            common_fiats = ['USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'CNY', 'INR', 'RUB', 'TRY', 'AED', 'BRL', 'KRW', 'MXN', 'SAR', 'IRT', 'IQD']
            buttons = []
            row = []
            for fiat in common_fiats:
                if fiat in rates:
                    emoji = get_fiat_emoji(fiat)
                    check = "✓" if fiat == current_fiat else ""
                    row.append(Button.inline(f"{emoji}{check}{fiat}", f"set_fiat:{fiat}".encode()))
                    if len(row) == 3:
                        buttons.append(row)
                        row = []
            if row:
                buttons.append(row)
            buttons.append([Button.inline(t(user_id, "btn_back_settings"), b"settings")])
            await event.edit(
                t(user_id, "fiat_header", title=t(user_id, 'btn_change_fiat'),
                  emoji=fiat_emoji, fiat=current_fiat),
                buttons=buttons
            )

        # ==================== LNURL-AUTH (v31.1) ====================
        elif data == "lnurlauth_ok":
            ud = user_data.get(user_id) or {}
            auth_req = ud.get('lnurl_auth_request')
            if not auth_req:
                await event.answer(t(user_id, "lnurl_auth_none"), alert=True)
                return
            await event.answer()
            await event.edit(t(user_id, "authenticating"))
            try:
                auth_sdk = await get_wallet(user_id)
                auth_result = await auth_sdk.lnurl_auth(request_data=auth_req)
                if isinstance(auth_result, LnurlCallbackStatus.OK):
                    await event.edit(t(user_id, "lnurl_auth_ok"))
                elif isinstance(auth_result, LnurlCallbackStatus.ERROR_STATUS):
                    auth_reason = getattr(
                        getattr(auth_result, 'error_details', None), 'reason', 'unknown')
                    await event.edit(t(user_id, "lnurl_auth_failed", reason=auth_reason))
                else:
                    await event.edit(t(user_id, "lnurl_auth_unexpected", result=auth_result))
            except Exception as auth_err:
                await event.edit(t(user_id, "lnurl_auth_err", error=str(auth_err)[:150]))
            user_data[user_id] = {}
            return

        # ==================== UNCLAIMED DEPOSITS ====================
        elif data == "unclaimed_deposits":
            await event.answer()
            try:
                await event.edit(t(user_id, "checking_deposits"))
                sdk = await get_wallet(user_id)
                response = await sdk.list_unclaimed_deposits(ListUnclaimedDepositsRequest())
                deposits = response.deposits

                if not deposits:
                    await event.edit(
                        t(user_id, "dep_none"),
                        buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                    )
                    return

                text = t(user_id, "dep_list_title")
                buttons = []
                for i, dep in enumerate(deposits):
                    amount_display = f"{dep.amount_sats:,} {t(user_id, 'sats_unit')}"
                    error_text = ""
                    if dep.claim_error:
                        if isinstance(dep.claim_error, DepositClaimError.MAX_DEPOSIT_CLAIM_FEE_EXCEEDED):
                            req_fee = getattr(dep.claim_error, 'required_fee_sats', None)
                            req_rate = getattr(dep.claim_error, 'required_fee_rate_sat_per_vbyte', None)
                            if req_rate:
                                error_text = t(user_id, "dep_fee_exceeded_rate", rate=req_rate)
                            elif req_fee:
                                error_text = t(user_id, "dep_fee_exceeded_sats", fee=req_fee)
                            else:
                                error_text = t(user_id, "dep_fee_exceeded")
                        elif isinstance(dep.claim_error, DepositClaimError.MISSING_UTXO):
                            error_text = t(user_id, "dep_missing_utxo")
                        elif isinstance(dep.claim_error, DepositClaimError.GENERIC):
                            msg = getattr(dep.claim_error, 'message', '')
                            error_text = (t(user_id, "dep_err_line", error=msg) if msg
                                          else t(user_id, "dep_generic_err"))
                        else:
                            error_text = t(user_id, "dep_generic_err")

                    text += t(user_id, "dep_item_line", i=i+1, txid=dep.txid[:12],
                              vout=dep.vout, amount=amount_display, error=error_text) + "\n"

                    # Store deposit info for later use
                    dep_key = f"{dep.txid}:{dep.vout}"
                    ud = user_data.setdefault(user_id, {})
                    ud.setdefault('unclaimed_deposits', {})[dep_key] = {
                        'txid': dep.txid,
                        'vout': dep.vout,
                        'amount_sats': dep.amount_sats,
                    }

                    # Store by index for short callback data
                    ud.setdefault('unclaimed_list', [])
                    dep_data = {'txid': dep.txid, 'vout': dep.vout, 'amount_sats': dep.amount_sats}
                    if dep.claim_error and isinstance(dep.claim_error, DepositClaimError.MAX_DEPOSIT_CLAIM_FEE_EXCEEDED):
                        dep_data['required_fee_sats'] = getattr(dep.claim_error, 'required_fee_sats', None)
                        dep_data['required_fee_rate'] = getattr(dep.claim_error, 'required_fee_rate_sat_per_vbyte', None)
                    if i >= len(ud['unclaimed_list']):
                        ud['unclaimed_list'].append(dep_data)

                    buttons.append([Button.inline(t(user_id, "btn_claim_n", i=i+1, amt=amount_display), f"ucl_{i}".encode())])

                text += t(user_id, "dep_legend")
                buttons.append([Button.inline(t(user_id, "btn_back_settings"), b"settings")])

                await event.edit(text, buttons=buttons)

            except Exception as e:
                logging.error(f"Error listing unclaimed deposits: {e}")
                await event.edit(t(user_id, "err_alert_line", error=e), buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]])

        elif data.startswith("ucl_") and not data.startswith("ucl_fee_") and not data.startswith("ucl_claim") and not data.startswith("ucl_refund"):
            # Show claim/refund options for a specific deposit by index
            await event.answer()
            try:
                idx = int(data.split("_")[1])
                ud = user_data.get(user_id, {})
                deposits_list = ud.get('unclaimed_list', [])

                if idx >= len(deposits_list):
                    await event.edit(t(user_id, "dep_not_found_retry"), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])
                    return

                deposit = deposits_list[idx]
                # Store selected deposit for claim/refund
                ud['claim_txid'] = deposit['txid']
                ud['claim_vout'] = deposit['vout']

                amount_display = f"{deposit['amount_sats']:,} {t(user_id, 'sats_unit')}"

                await event.edit(
                    t(user_id, "dep_details", amount=amount_display,
                      txid=deposit['txid'], vout=deposit['vout']),
                    buttons=[
                        [Button.inline(t(user_id, "btn_claim_ln"), b"ucl_claim")],
                        [Button.inline(t(user_id, "btn_refund_btc"), b"ucl_refund")],
                        [Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]
                    ]
                )
            except Exception as e:
                logging.error(f"Error showing deposit details: {e}")
                await event.edit(t(user_id, "err_alert_line", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])

        elif data == "ucl_claim":
            # Claim deposit to Lightning
            await event.answer()
            try:
                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')
                if not txid:
                    await event.edit(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])
                    return

                # Find deposit info with required fee
                dep_info = None
                for dep in ud.get('unclaimed_list', []):
                    if dep['txid'] == txid and dep['vout'] == vout:
                        dep_info = dep
                        break

                req_fee = dep_info.get('required_fee_sats') if dep_info else None
                req_rate = dep_info.get('required_fee_rate') if dep_info else None

                if req_fee:
                    # SDK told us the exact required fee — show it for user approval
                    amount_sats = dep_info.get('amount_sats', 0)
                    net_amount = amount_sats - req_fee
                    await event.edit(
                        t(user_id, "claim_confirm_fee",
                          amount=f"{amount_sats:,}", fee=f"{req_fee:,}",
                          rate=req_rate, net=f"{net_amount:,}"),
                        buttons=[
                            [Button.inline(t(user_id, "btn_confirm_claim"), b"ucl_fee_required")],
                            [Button.inline(t(user_id, "btn_custom_fee"), b"ucl_fee_custom")],
                            [Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]
                        ]
                    )
                else:
                    # No required fee info — show fee speed options
                    await event.edit(t(user_id, "getting_fees"))
                    sdk = await get_wallet(user_id)
                    rec_fees = await sdk.recommended_fees()

                    ud['rec_fees'] = {
                        'fastest': rec_fees.fastest_fee,
                        'half_hour': rec_fees.half_hour_fee,
                        'hour': rec_fees.hour_fee,
                        'economy': rec_fees.economy_fee,
                        'minimum': rec_fees.minimum_fee,
                    }

                    await event.edit(
                        t(user_id, "claim_select_fee", txid=txid[:24]),
                        buttons=[
                            [Button.inline(t(user_id, "btn_fee_fast_r", rate=rec_fees.fastest_fee), b"ucl_fee_fast")],
                            [Button.inline(t(user_id, "btn_fee_medium_r", rate=rec_fees.half_hour_fee), b"ucl_fee_medium")],
                            [Button.inline(t(user_id, "btn_fee_slow_r", rate=rec_fees.hour_fee), b"ucl_fee_slow")],
                            [Button.inline(t(user_id, "btn_custom"), b"ucl_fee_custom")],
                            [Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]
                        ]
                    )
            except Exception as e:
                logging.error(f"Error preparing claim: {e}")
                await event.edit(t(user_id, "err_alert_line", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])

        elif data == "ucl_refund":
            # Refund deposit to BTC address
            await event.answer()
            try:
                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')
                if not txid:
                    await event.edit(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])
                    return

                ud['refund_txid'] = txid
                ud['refund_vout'] = vout
                user_steps[user_id] = "ucl_refund_address"

                await event.edit(
                    t(user_id, "refund_prompt", txid=txid[:24]),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]]
                )
            except Exception as e:
                logging.error(f"Error preparing refund: {e}")
                await event.edit(t(user_id, "err_alert_line", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])

        elif data.startswith("ucl_fee_"):
            await event.answer()
            try:
                fee_choice = data[len("ucl_fee_"):]
                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')

                if not txid:
                    await event.edit(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])
                    return

                if fee_choice == "custom":
                    user_steps[user_id] = "ucl_custom_fee"
                    await event.edit(
                        t(user_id, "custom_fee_prompt"),
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]]
                    )
                    return

                if fee_choice == "required":
                    # Use exact required fee from SDK
                    dep_info = None
                    for dep in ud.get('unclaimed_list', []):
                        if dep['txid'] == txid and dep['vout'] == vout:
                            dep_info = dep
                            break
                    req_fee = dep_info.get('required_fee_sats') if dep_info else None
                    if req_fee:
                        fee = MaxFee(onchain=MaxOnchainFee.FIXED(amount=req_fee))
                    else:
                        fee = MaxFee(onchain=MaxOnchainFee.RATE(sat_per_vbyte=10))
                else:
                    # Map to MaxFee from recommended fees
                    rec = ud.get('rec_fees', {})
                    if fee_choice == "fast":
                        fee = MaxFee(onchain=MaxOnchainFee.RATE(sat_per_vbyte=rec.get('fastest', 10)))
                    elif fee_choice == "medium":
                        fee = MaxFee(onchain=MaxOnchainFee.RATE(sat_per_vbyte=rec.get('half_hour', 5)))
                    else:  # slow (hour)
                        fee = MaxFee(onchain=MaxOnchainFee.RATE(sat_per_vbyte=rec.get('hour', 3)))

                await event.edit(t(user_id, "claiming_deposit"))
                sdk = await get_wallet(user_id)
                request = ClaimDepositRequest(txid=txid, vout=vout, max_fee=fee)
                result = await sdk.claim_deposit(request)
                outcome = result.outcome
                if outcome.is_SETTLED():
                    payment = outcome.payment
                    amount_display = f"{payment.amount_sats:,} {t(user_id, 'sats_unit')}" if hasattr(payment, 'amount_sats') else "✅"
                    msg = t(user_id, "claim_settled", amount=amount_display)
                elif outcome.is_SUBMITTED():
                    msg = t(user_id, "claim_submitted")
                elif outcome.is_DEFERRED():
                    msg = t(user_id, "claim_deferred", reason=outcome.reason)
                else:
                    msg = t(user_id, "claim_processed")
                await event.edit(
                    msg,
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                )

            except Exception as e:
                logging.error(f"Error claiming deposit: {e}")
                await event.edit(t(user_id, "claim_failed", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_try_again"), b"unclaimed_deposits"), Button.inline(t(user_id, "btn_back_settings"), b"settings")]])

        elif data == "receive":
            await event.answer()

            # Get balance with fiat
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_fiat(balance, user_id)

            # Fetch lightning address + static onchain address (may be None)
            try:
                ln_address_obj = await get_lightning_address(user_id)
                ln_address = getattr(ln_address_obj, 'lightning_address', None) if ln_address_obj else None
            except Exception:
                ln_address = None
            static_btc = await get_static_onchain_address(user_id)
            try:
                spark_addr = await zap_p2p.spark_address_of(user_id)
            except Exception:
                spark_addr = None

            msg_content = (
                f"{t(user_id, 'receive_title')}\n\n"
                f"{t(user_id, 'balance_line', amount=balance_display)}\n\n"
            )
            if ln_address:
                msg_content += f"⚡ {t(user_id, 'your_ln_address')}:\n`{ln_address}`\n\n"
            if spark_addr:
                msg_content += f"✨ {t(user_id, 'your_spark_address')}:\n`{spark_addr}`\n\n"
            if static_btc:
                msg_content += f"₿ {t(user_id, 'your_onchain_address')}:\n`{static_btc}`"

            msg_buttons = [
                [Button.inline(t(user_id, "btn_ln_invoice"), b"receive_lightning")],
                [Button.inline(t(user_id, "btn_onchain"), b"receive_onchain")],
                [Button.inline(t(user_id, "btn_exchange"), b"receive_exchange")],
                [Button.inline(t(user_id, "btn_token_usd"), b"token_deposit")],
                [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
            ]

            # Get the message to check if it has media
            msg = await event.get_message()
            if msg and msg.media:
                # Message has media (QR photo) - must delete and send new
                try:
                    await msg.delete()
                except:
                    pass
                await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)
            else:
                # Text message - can edit
                try:
                    await event.edit(msg_content, buttons=msg_buttons)
                except:
                    await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)

        elif data == "receive_lightning":
            await event.answer()
            address = await get_lightning_address(user_id)

            # No lightning address registered yet -> offer registration
            if not address or not getattr(address, 'lightning_address', None):
                await event.edit(
                    t(user_id, 'ln_no_address'),
                    buttons=[
                        [Button.inline(t(user_id, "btn_register_ln"), b"confirm_change_ln_address")],
                        [Button.inline(t(user_id, "btn_back"), b"receive")]
                    ]
                )
                return

            # Generate invoice immediately (no amount) — user-friendly default
            try:
                invoice = await create_invoice(user_id, amount_sats=None)
                qr_buffer = generate_qr_code(invoice)
                caption = (
                    f"{t(user_id, 'invoice_no_amount_title')}\n\n"
                    f"`{invoice}`"
                )
                # QR display: delete old message and send new with photo
                try:
                    msg = await event.get_message()
                    if msg:
                        await msg.delete()
                except:
                    pass
                await client.send_file(
                    event.chat_id, qr_buffer, force_document=False,
                    caption=caption,
                    buttons=[
                        [Button.inline(t(user_id, "btn_set_amount"), b"receive_set_amount_no_memo")],
                        [Button.inline(t(user_id, "btn_set_memo"), b"receive_add_memo")],
                        [Button.inline(t(user_id, "btn_back"), b"receive")]
                    ])
            except Exception as error:
                await event.edit(t(user_id, "err_bare", error=error), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])

        elif data == "receive_onchain":
            await event.answer()
            try:
                msg = await event.edit(t(user_id, "generating_addr"))

                btc_address, receive_fee = await create_onchain_address(user_id)
                fee_display = await format_amount_with_usd(receive_fee, user_id)

                # Generate QR code for Bitcoin address
                qr_buffer = generate_qr_code(f"bitcoin:{btc_address}")

                # Delete old message and send new one with QR code as image
                await msg.delete()
                await client.send_file(
                    event.chat_id,
                    qr_buffer,
                    force_document=False,
                    caption=(
                        f"{t(user_id, 'onchain_title')}\n\n"
                        f"₿ `{btc_address}`\n\n"
                        + (f"⛽ {t(user_id, 'network_fee')}: {fee_display}" if receive_fee and receive_fee > 0 else "")
                    ),
                    buttons=None
                )
                # single follow-up message with combined nav
                await client.send_message(
                    event.chat_id,
                    t(user_id, 'onchain_note'),
                    buttons=[
                        [Button.inline(t(user_id, "btn_back"), b"receive"),
                         Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                    ]
                )
            except Exception as error:
                await event.edit(
                    t(user_id, "err_bare", error=error),
                    buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]]
                )

        # ==================== RECEIVE EXCHANGE (ff.io) ====================
        elif data == "receive_exchange":
            await event.answer()
            try:
                await event.edit(t(user_id, 'ff_loading'))
                currencies = await ffio_get_currencies()
                if not currencies:
                    await event.edit(t(user_id, 'ff_load_fail'), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])
                    return
                grouped = ffio_group_currencies_by_coin(currencies, 'recv')
                buttons = build_coin_buttons(grouped, "rcv_coin_", b"receive")
                user_data.setdefault(user_id, {})['ffio_currencies'] = currencies
                await event.edit(
                    f"{t(user_id, 'ff_recv_title')}\n\n"
                    f"{t(user_id, 'ff_terms')}",
                    buttons=buttons
                )
            except Exception as e:
                logging.error(f"receive_exchange error: {e}")
                await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])

        elif data.startswith("rcv_coin_"):
            await event.answer()
            coin = data[9:]
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            if not currencies:
                await event.edit(t(user_id, "ccy_load_fail"), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])
                return
            grouped = ffio_group_currencies_by_coin(currencies, 'recv')
            networks = grouped.get(coin, [])
            logging.info(f"ff.io: User selected coin {coin}, found {len(networks)} networks: {[n['code'] for n in networks]}")
            if len(networks) == 1:
                ccy = networks[0]
                logging.info(f"ff.io: Single network for {coin}, using code: {ccy['code']}")
                ud['ffio_from_ccy'] = ccy['code']
                ud['ffio_from_name'] = ccy['name']
                ud['ffio_tag_type'] = ccy.get('tag')
                ud['ffio_direction'] = 'receive'
                user_data[user_id] = ud
                # Show rate type selection
                await event.edit(
                    t(user_id, "ff_recv_rate_type", name=ccy['name']),
                    buttons=[
                        [Button.inline(t(user_id, "btn_rate_fixed"), b"rcv_fixed")],
                        [Button.inline(t(user_id, "btn_rate_float"), b"rcv_float")],
                        [Button.inline(t(user_id, "btn_back"), b"receive_exchange")]
                    ]
                )
            elif len(networks) > 1:
                buttons = []
                for ccy in networks:
                    buttons.append([Button.inline(f"{ccy['name']}", f"rcv_net_{ccy['code']}".encode())])
                buttons.append([Button.inline(t(user_id, "btn_back"), b"receive_exchange")])
                await event.edit(t(user_id, "ff_select_net", coin=coin), buttons=buttons)
            else:
                await event.edit(t(user_id, "ff_coin_unavailable", coin=coin), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive_exchange")]])

        elif data.startswith("rcv_net_"):
            await event.answer()
            ccy_code = data[8:]
            logging.info(f"ff.io: User selected network with code: {ccy_code}")
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            ccy_info = next((c for c in (currencies or []) if c['code'] == ccy_code), None)
            if not ccy_info:
                logging.warning(f"ff.io: Currency code {ccy_code} not found in currencies list")
                await event.edit(t(user_id, "ff_ccy_not_found"), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive_exchange")]])
                return
            logging.info(f"ff.io: Found currency info for {ccy_code}: {ccy_info.get('name')}")
            ud['ffio_from_ccy'] = ccy_code
            ud['ffio_from_name'] = ccy_info['name']
            ud['ffio_tag_type'] = ccy_info.get('tag')
            ud['ffio_direction'] = 'receive'
            user_data[user_id] = ud
            await event.edit(
                t(user_id, "ff_recv_rate_type", name=ccy_info['name']),
                buttons=[
                    [Button.inline(t(user_id, "btn_rate_fixed"), b"rcv_fixed")],
                    [Button.inline(t(user_id, "btn_rate_float"), b"rcv_float")],
                    [Button.inline(t(user_id, "btn_back"), b"receive_exchange")]
                ]
            )

        elif data in ("rcv_fixed", "rcv_float"):
            await event.answer()
            ud = user_data.get(user_id, {})
            rate_type = "fixed" if data == "rcv_fixed" else "float"
            ud['ffio_rate_type'] = rate_type
            user_data[user_id] = ud
            user_steps[user_id] = "ffio_receive_amount"
            from_ccy = ud.get('ffio_from_ccy', '?')
            from_name = ud.get('ffio_from_name', '?')
            rate_label = "Fixed" if data == "rcv_fixed" else "Float"

            # Get min/max from price API
            minmax_text = ""
            try:
                price_info = await ffio_get_price(from_ccy, "BTCLN", 0, rate_type=rate_type)
                if price_info and price_info.get('data'):
                    pdata = price_info['data']
                    fr = pdata.get('from', {})
                    min_val = fr.get('min', '')
                    max_val = fr.get('max', '')
                    if min_val or max_val:
                        minmax_text = t(user_id, "ff_minmax_pair", mn=min_val, mx=max_val)
            except:
                pass

            await event.edit(
                t(user_id, "ff_amount_prompt", name=from_name, rate=rate_label,
                  minmax=minmax_text),
                buttons=[[Button.inline(t(user_id, "btn_back"), b"receive_exchange")]]
            )

        elif data.startswith("rcv_ex_confirm_"):
            # Double-click prevention for ff.io order creation
            ud = user_data.get(user_id, {})
            if ud.get('ffio_order_creating'):
                await event.answer(t(user_id, "order_creating"))
                return

            await event.answer()
            try:
                # Mark as creating to prevent double-clicks
                ud['ffio_order_creating'] = True
                user_data[user_id] = ud

                await event.edit(t(user_id, "creating_order"))
                from_ccy = ud.get('ffio_from_ccy')
                amount = ud.get('ffio_amount')

                if not from_ccy or not amount:
                    ud.pop('ffio_order_creating', None)
                    await event.edit(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])
                    return

                # Create a Lightning invoice for receiving
                invoice = await create_invoice(user_id, amount_sats=None)

                # Create ff.io order: from_ccy -> BTCLN, destination is our invoice
                rate_type = ud.get('ffio_rate_type', 'float')
                result = await ffio_create_order(
                    from_ccy=from_ccy,
                    to_ccy="BTCLN",
                    amount=amount,
                    to_address=invoice,
                    direction="from",
                    rate_type=rate_type
                )

                if result and result.get('code') == 0 and result.get('data'):
                    order = result['data']
                    order_id = order.get('id')
                    token = order.get('token')
                    from_info = order.get('from', {})
                    to_info = order.get('to', {})
                    deposit_address = from_info.get('address', 'N/A')
                    deposit_tag = from_info.get('tag', '')
                    deposit_amount = from_info.get('amount', amount)
                    from_name = from_info.get('name', from_info.get('coin', from_ccy))
                    receive_amount = to_info.get('amount', '?')
                    time_info = order.get('time', {})
                    expiration_ts = time_info.get('expiration')
                    left_secs = time_info.get('left', 0)
                    if left_secs:
                        mins = int(left_secs) // 60
                        expiry_text = f"{mins} min"
                    elif expiration_ts:
                        expiry_text = datetime.fromtimestamp(expiration_ts).strftime('%H:%M:%S')
                    else:
                        expiry_text = "N/A"

                    # Track order
                    pending_ffio_orders[user_id] = {
                        'id': order_id,
                        'token': token,
                        'direction': 'receive',
                        'last_status': 'NEW',
                        'from_ccy': from_ccy,
                        'from_name': from_name,
                    }

                    # Log order to backup channel
                    asyncio.create_task(ffio_log_order_to_channel(user_id, event, order, 'receive', from_ccy, 'BTCLN'))

                    # Generate QR for deposit address
                    qr_buffer = generate_qr_code(deposit_address)

                    msg = await event.get_message()
                    if msg:
                        try:
                            await msg.delete()
                        except:
                            pass

                    await client.send_file(
                        event.chat_id,
                        qr_buffer,
                        force_document=False,
                        caption=t(
                            user_id, "ff_order_created_recv", oid=order_id,
                            amount=deposit_amount, ccy=from_name, btc=receive_amount,
                            addr=deposit_address,
                            tag=(t(user_id, "ff_tag_line", tag=deposit_tag) if deposit_tag else ""),
                            expiry=expiry_text,
                            tagwarn=(t(user_id, "ff_tag_warn") if deposit_tag else "")),
                        buttons=[
                            [Button.inline(t(user_id, "btn_check_status"), f"ffio_st_{order_id}".encode())],
                        ]
                    )
                else:
                    error_msg = result.get('msg', 'Unknown error') if result else 'API error'
                    await event.edit(
                        t(user_id, "ff_exchange_err", error=error_msg),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"receive_exchange")], [Button.inline(t(user_id, "btn_back"), b"receive")]]
                    )

                # Clean up user state
                user_steps.pop(user_id, None)

            except Exception as e:
                logging.error(f"Error creating ff.io receive order: {e}")
                await event.edit(t(user_id, "err_alert_line", error=e), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])

        # ==================== SEND EXCHANGE (ff.io) ====================
        elif data == "send_exchange":
            await event.answer()
            try:
                balance = await get_balance(user_id)
                balance_display = await format_balance_with_usd(balance, user_id)
                await event.edit(t(user_id, 'ff_loading'))
                currencies = await ffio_get_currencies()
                if not currencies:
                    await event.edit(t(user_id, 'ff_load_fail'), buttons=[[Button.inline(t(user_id, "btn_back"), b"send")]])
                    return
                grouped = ffio_group_currencies_by_coin(currencies, 'send')
                buttons = build_coin_buttons(grouped, "snd_coin_", b"send")
                user_data.setdefault(user_id, {})['ffio_currencies'] = currencies
                await event.edit(
                    f"{t(user_id, 'ff_send_title', balance=balance_display)}\n\n"
                    f"{t(user_id, 'ff_terms')}",
                    buttons=buttons
                )
            except Exception as e:
                logging.error(f"send_exchange error: {e}")
                await event.edit(t(user_id, "alert_error", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"send")]])

        elif data.startswith("snd_coin_"):
            await event.answer()
            coin = data[9:]
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            if not currencies:
                await event.edit(t(user_id, "ff_ccy_load_fail"), buttons=[[Button.inline(t(user_id, "btn_back"), b"send")]])
                return
            grouped = ffio_group_currencies_by_coin(currencies, 'send')
            networks = grouped.get(coin, [])
            if len(networks) == 1:
                ccy = networks[0]
                ud['ffio_to_ccy'] = ccy['code']
                ud['ffio_to_name'] = ccy['name']
                ud['ffio_tag_type'] = ccy.get('tag')
                ud['ffio_direction'] = 'send'
                user_data[user_id] = ud
                await event.edit(
                    t(user_id, "ff_send_rate_type", name=ccy['name']),
                    buttons=[
                        [Button.inline(t(user_id, "btn_rate_fixed"), b"snd_fixed")],
                        [Button.inline(t(user_id, "btn_rate_float"), b"snd_float")],
                        [Button.inline(t(user_id, "btn_back"), b"send_exchange")]
                    ]
                )
            elif len(networks) > 1:
                buttons = []
                for ccy in networks:
                    buttons.append([Button.inline(f"{ccy['name']}", f"snd_net_{ccy['code']}".encode())])
                buttons.append([Button.inline(t(user_id, "btn_back"), b"send_exchange")])
                await event.edit(t(user_id, "ff_select_net", coin=coin), buttons=buttons)
            else:
                await event.edit(t(user_id, "ff_coin_unavailable_send", coin=coin), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])

        elif data.startswith("snd_net_"):
            await event.answer()
            ccy_code = data[8:]
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            ccy_info = next((c for c in (currencies or []) if c['code'] == ccy_code), None)
            if not ccy_info:
                await event.edit(t(user_id, "ff_ccy_not_found"), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])
                return
            ud['ffio_to_ccy'] = ccy_code
            ud['ffio_to_name'] = ccy_info['name']
            ud['ffio_tag_type'] = ccy_info.get('tag')
            ud['ffio_direction'] = 'send'
            user_data[user_id] = ud
            await event.edit(
                t(user_id, "ff_send_rate_type", name=ccy_info['name']),
                buttons=[
                    [Button.inline(t(user_id, "btn_rate_fixed"), b"snd_fixed")],
                    [Button.inline(t(user_id, "btn_rate_float"), b"snd_float")],
                    [Button.inline(t(user_id, "btn_back"), b"send_exchange")]
                ]
            )

        elif data in ("snd_fixed", "snd_float"):
            await event.answer()
            ud = user_data.get(user_id, {})
            rate_type = "fixed" if data == "snd_fixed" else "float"
            ud['ffio_rate_type'] = rate_type
            user_data[user_id] = ud
            user_steps[user_id] = "ffio_send_address"
            to_ccy = ud.get('ffio_to_ccy', '?')
            to_name = ud.get('ffio_to_name', '?')
            tag_note = ""
            rate_label = "Fixed" if data == "snd_fixed" else "Float"

            # Get min/max
            minmax_text = ""
            try:
                price_info = await ffio_get_price("BTCLN", to_ccy, 0, rate_type=rate_type)
                if price_info and price_info.get('data'):
                    fr = price_info['data'].get('from', {})
                    min_val = fr.get('min', '')
                    max_val = fr.get('max', '')
                    if min_val or max_val:
                        # Convert BTC min/max to sats
                        try:
                            min_sats = int(float(min_val) * SATS_PER_BTC) if min_val else ''
                            max_sats = int(float(max_val) * SATS_PER_BTC) if max_val else ''
                            minmax_text = t(user_id, "ff_sats_minmax_pre",
                                            mn=f"{min_sats:,}", mx=f"{max_sats:,}")
                            ud['ffio_min_sats'] = min_sats
                            ud['ffio_max_sats'] = max_sats
                        except:
                            minmax_text = t(user_id, "ff_btc_minmax_pre", mn=min_val, mx=max_val)
            except:
                pass

            await event.edit(
                t(user_id, "ff_send_addr_prompt", name=to_name, rate=rate_label,
                  minmax=minmax_text, tag=tag_note),
                buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
            )

        elif data == "ffio_skip_tag":
            await event.answer()
            ud = user_data.get(user_id, {})
            user_steps[user_id] = "ffio_send_amount"
            to_name = ud.get('ffio_to_name', ud.get('ffio_to_ccy', '?'))
            address = ud.get('ffio_to_address', '?')
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance, user_id)

            min_sats = ud.get('ffio_min_sats', '')
            max_sats = ud.get('ffio_max_sats', '')
            limit_text = ""
            if min_sats or max_sats:
                limit_text = t(user_id, "ff_sats_limit_line",
                               mn=f"{min_sats:,}", mx=f"{max_sats:,}")

            await event.edit(
                t(user_id, "ff_amount_prompt_send", name=to_name, balance=balance_display,
                  addr=f"{address[:30]}...", limits=limit_text),
                buttons=[
                    [Button.inline(t(user_id, "btn_send_all"), b"ffio_send_all")],
                    [Button.inline(t(user_id, "btn_cancel"), b"send_exchange")]
                ]
            )

        elif data.startswith("snd_ex_confirm_") and not data.startswith("snd_ex_confirm_n"):
            await event.answer()
            try:
                await event.edit(t(user_id, "creating_order"))
                ud = user_data.get(user_id, {})
                to_ccy = ud.get('ffio_to_ccy')
                to_address = ud.get('ffio_to_address')
                amount_sats = ud.get('ffio_amount_sats')
                is_send_all = ud.get('ffio_send_all', False)

                if not to_ccy or not to_address or not amount_sats:
                    await event.edit(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"send")]])
                    return

                # For send-all: deduct estimated fee reserve BEFORE creating order
                # so only one order is created on ff.io
                if is_send_all:
                    balance = int(await get_balance(user_id))
                    # Reserve 1% of balance for fee (min 10 sats)
                    # pinned semantics: float artifacts in int(balance*0.01) are the spec
                    fee_reserve = max(10, int(balance * 0.01))
                    amount_sats = balance - fee_reserve
                    if amount_sats <= 0:
                        await event.edit(
                            t(user_id, "insuff_after_fee_reserve", balance=f"{balance:,}"),
                            buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                        )
                        return
                    min_sats = ud.get('ffio_min_sats')
                    if min_sats and amount_sats < min_sats:
                        await event.edit(
                            t(user_id, "ff_below_min_after_reserve",
                              amount=f"{amount_sats:,}", min=f"{min_sats:,}"),
                            buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                        )
                        return
                    ud['ffio_amount_sats'] = amount_sats

                amount_btc = amount_sats / SATS_PER_BTC  # pinned semantics: float BTC to ff.io

                tag = ud.get('ffio_to_tag')
                rate_type = ud.get('ffio_rate_type', 'float')
                result = await ffio_create_order(
                    from_ccy="BTCLN",
                    to_ccy=to_ccy,
                    amount=amount_btc,
                    to_address=to_address,
                    direction="from",
                    rate_type=rate_type,
                    tag=tag
                )

                if result and result.get('code') == 0 and result.get('data'):
                    order = result['data']
                    order_id = order.get('id')
                    token = order.get('token')
                    deposit_address = order.get('from', {}).get('address', 'N/A')
                    deposit_amount = order.get('from', {}).get('amount', '?')
                    receive_amount = order.get('to', {}).get('amount', '?')

                    try:
                        invoice = deposit_address
                        prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, None)

                        fee_display = await format_amount_with_usd(fee_sats or 0, user_id)
                        amount_display = await format_amount_with_usd(amount_sats, user_id)

                        pending_ffio_orders[user_id] = {
                            'id': order_id,
                            'token': token,
                            'direction': 'send',
                            'last_status': 'NEW',
                            'to_ccy': to_ccy,
                            'prepare_response': prepare_response,
                        }

                        # Log order to backup channel
                        asyncio.create_task(ffio_log_order_to_channel(user_id, event, order, 'send', 'BTCLN', to_ccy))

                        fee_note = ""
                        if is_send_all:
                            fee_note = t(user_id, "ff_fee_note_sendall")

                        await event.edit(
                            t(user_id, "ff_order_created_send", oid=order_id,
                              amount=deposit_amount, recv=receive_amount, ccy=to_ccy,
                              fee=fee_display, fee_note=fee_note),
                            buttons=[
                                [Button.inline(t(user_id, "btn_confirm_send"), f"ffio_pay_{order_id}".encode())],
                                [Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")]
                            ]
                        )
                    except Exception as e:
                        await event.edit(
                            t(user_id, "ff_payment_err", error=str(e)),
                            buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                        )
                else:
                    error_msg = result.get('msg', 'Unknown error') if result else 'API error'
                    await event.edit(
                        t(user_id, "ff_exchange_err", error=error_msg),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"send_exchange")], [Button.inline(t(user_id, "btn_back"), b"send")]]
                    )

                user_steps.pop(user_id, None)
                ud.pop('ffio_send_all', None)

            except Exception as e:
                logging.error(f"Error creating ff.io send order: {e}")
                await event.edit(t(user_id, "err_alert_line", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"send")]])

        elif data == "ffio_send_all":
            await event.answer()
            ud = user_data.get(user_id, {})
            to_ccy = ud.get('ffio_to_ccy', '?')
            to_name = ud.get('ffio_to_name', '?')
            balance = int(await get_balance(user_id))
            max_sats = ud.get('ffio_max_sats')
            if max_sats and balance > max_sats:
                amount_sats = max_sats
            else:
                amount_sats = balance
            if amount_sats <= 0:
                await event.edit(t(user_id, "ff_no_balance"), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])
                return
            min_sats = ud.get('ffio_min_sats')
            if min_sats and amount_sats < min_sats:
                await event.edit(
                    t(user_id, "ff_balance_too_low", min=f"{min_sats:,}", balance=f"{balance:,}"),
                    buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                )
                return
            ud['ffio_send_all'] = True
            ud['ffio_amount_sats'] = amount_sats
            amount_btc = amount_sats / SATS_PER_BTC  # pinned semantics: float BTC to ff.io
            try:
                rate_type = ud.get('ffio_rate_type', 'float')
                price_result = await ffio_get_price("BTCLN", to_ccy, amount_btc, rate_type=rate_type)
                if price_result and price_result.get('code') == 0 and price_result.get('data'):
                    receive_amount = price_result['data'].get('to', {}).get('amount', '?')
                    rate = price_result['data'].get('to', {}).get('rate', '?')
                    amount_display = await format_amount_with_usd(amount_sats, user_id)
                    await event.edit(
                        t(user_id, "ff_quote_sendall", send=amount_display,
                          recv=receive_amount, ccy=to_ccy, rate=rate),
                        buttons=[
                            [Button.inline(t(user_id, "btn_create_order"), b"snd_ex_confirm_yes")],
                            [Button.inline(t(user_id, "btn_cancel_x"), b"send_exchange")]
                        ]
                    )
                else:
                    error_msg = price_result.get('msg', 'Unknown error') if price_result else 'API error'
                    await event.edit(t(user_id, "ff_quote_err", error=error_msg), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])
            except Exception as e:
                await event.edit(t(user_id, "err_alert_line", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])

        elif data.startswith("ffio_pay_"):
            # Double-click prevention for ff.io payment
            async with user_lock(user_id):
                order_info = pending_ffio_orders.get(user_id)
                if order_info and order_info.get('payment_processing'):
                    await event.answer(t(user_id, "pay_in_progress"))
                    return

                order_id = data[9:]  # after "ffio_pay_"

                # Check password for ff.io payment
                if has_send_password(user_id) and not (order_info and order_info.get('password_verified')):
                    await event.answer()
                    user_steps[user_id] = "verify_send_password_for_ffio"
                    if user_id not in user_data:
                        user_data[user_id] = {}
                    user_data[user_id]['pending_ffio_order_id'] = order_id
                    hint = get_password_hint(user_id)
                    hint_text = t(user_id, "pw_hint_hint", hint=hint) if hint else ""
                    await event.edit(
                        t(user_id, "pw_required_pay", hint=hint_text),
                        buttons=[[Button.inline(t(user_id, "btn_cancel_x"), b"send_exchange")]]
                    )
                    # Store message ID for later editing
                    user_data[user_id]['password_prompt_msg_id'] = event.message_id
                    return

                await event.answer()
                try:
                    if not order_info or order_info['id'] != order_id:
                        await event.edit(t(user_id, "ff_order_not_found"), buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
                        return

                    # Mark as processing to prevent double-clicks
                    order_info['payment_processing'] = True

                    await event.edit(t(user_id, "ff_sending_ln"))

                    prepare_response = order_info.get('prepare_response')
                    if not prepare_response:
                        await event.edit(t(user_id, "ff_session_expired_pay"), buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
                        return

                    options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=PAYMENT_COMPLETION_TIMEOUT_SECS)
                    payment = await send_payment(user_id, prepare_response, options)

                    if payment:
                        # Check if payment is truly successful
                        is_success, failure_reason = is_payment_successful(payment)

                        if is_success:
                            to_ccy = order_info.get('to_ccy', '?')
                            await event.edit(
                                _ffio_success_text(order_id, to_ccy, user_id=user_id),
                                buttons=_ffio_success_buttons(user_id, order_id)
                            )
                            await show_tip_prompt(user_id)
                        else:
                            if failure_reason == 'failed':
                                await event.edit(
                                    t(user_id, 'payment_failed') + t(user_id, "order_line", oid=order_id),
                                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                                )
                            else:
                                # pending/unknown: neutral processing text + read-only
                                # background recheck (no retry, no refund)
                                await event.edit(t(user_id, 'payment_processing_status'), buttons=None)
                                _spawn_payment_recheck(
                                    user_id=user_id, chat_id=event.chat_id,
                                    message_id=event.message_id,
                                    payment_id=getattr(payment, 'id', None),
                                    success_text=_ffio_success_text(order_id, order_info.get('to_ccy', '?'), user_id=user_id),
                                    success_buttons=_ffio_success_buttons(user_id, order_id),
                                    order_info=order_info)
                    else:
                        # send returned no payment object at all: same neutral
                        # handling, timer-only (nothing identifiable to poll)
                        await event.edit(t(user_id, 'payment_processing_status'), buttons=None)
                        _spawn_payment_recheck(
                            user_id=user_id, chat_id=event.chat_id,
                            message_id=event.message_id, payment_id=None,
                            success_text='', order_info=order_info)
                except Exception as e:
                    if _duplicate_payment_error(e):
                        # Conclusive duplicate (2026-11 incident): the network
                        # rejected the request outright — nothing new was sent.
                        # Reopen this order's pay button instead of wedging it.
                        if order_info:
                            order_info['payment_processing'] = False
                        logging.info(
                            'duplicate invoice rejected (already-paid payment hash) uid=%s',
                            user_id)
                        await event.edit(
                            t(user_id, 'payment_duplicate_invoice'),
                            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                        )
                    else:
                        logging.error(f"Error paying ff.io order: {e}")
                        await event.edit(t(user_id, "err_alert_line", error=e), buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])

        elif data.startswith("ffio_st_"):
            await event.answer(t(user_id, "ff_status_checking"))
            try:
                order_id = data[8:]  # after "ffio_st_"
                # Get token from tracked orders
                order_info = pending_ffio_orders.get(user_id, {})
                token = order_info.get('token', '')

                if not token:
                    await event.answer(t(user_id, "order_refresh_expired"), alert=True)
                    return

                result = await ffio_get_order(order_id, token)
                if result and result.get('code') == 0 and result.get('data'):
                    order = result['data']
                    status = order.get('status', '?')
                    from_info = order.get('from', {})
                    to_info = order.get('to', {})

                    from_name = from_info.get('name', from_info.get('coin', from_info.get('code', '?')))
                    to_name = to_info.get('name', to_info.get('coin', to_info.get('code', '?')))

                    status_icons = {
                        'NEW': '🆕', 'PENDING': '⏳', 'EXCHANGE': '🔄',
                        'WITHDRAW': '📤', 'DONE': '✅', 'EXPIRED': '⏰', 'EMERGENCY': '⚠️'
                    }
                    icon = status_icons.get(status, '❓')

                    # Format amounts without scientific notation
                    def fmt_amount(val):
                        try:
                            v = float(val)
                            return f"{v:.8f}".rstrip('0').rstrip('.')
                        except:
                            return str(val) if val else '?'

                    from_amt = fmt_amount(from_info.get('amount'))
                    to_amt = fmt_amount(to_info.get('amount'))

                    text = (
                        t(user_id, "ff_status_card", oid=order_id, icon=icon, status=status)
                        + t(user_id, "ff_status_from", amt=from_amt, ccy=from_name) + "\n"
                        + t(user_id, "ff_status_to", amt=to_amt, ccy=to_name) + "\n"
                    )

                    # Time info
                    time_info = order.get('time', {})
                    left = time_info.get('left')
                    if left and int(left) > 0:
                        text += t(user_id, "ff_status_time", m=int(left)//60, s=int(left)%60)

                    if from_info.get('tx', {}).get('id'):
                        tx_id = from_info['tx']['id']
                        text += t(user_id, "ff_status_tx", tx=tx_id)

                    confs = from_info.get('tx', {}).get('confirmations')
                    req_confs = from_info.get('reqConfirmations')
                    if confs is not None and req_confs:
                        text += t(user_id, "ff_status_confs", c=confs, req=req_confs)

                    buttons = []
                    if status not in ('DONE', 'EXPIRED'):
                        buttons.append([Button.inline(t(user_id, "btn_refresh"), f"ffio_st_{order_id}".encode())])

                    msg = await event.get_message()
                    if msg and msg.media:
                        # Message has QR/media - don't delete it, send status as new message
                        await client.send_message(event.chat_id, text, buttons=buttons if buttons else None)
                    else:
                        try:
                            await event.edit(text, buttons=buttons if buttons else None)
                        except Exception:
                            await event.answer(t(user_id, "ff_status_unchanged"), alert=False)
                else:
                    error_msg = result.get('msg', 'No data') if result else 'API error'
                    await event.answer(t(user_id, "alert_error", error=error_msg), alert=True)
            except Exception as e:
                await event.answer(t(user_id, "alert_error", error=str(e)), alert=True)

        elif data == "receive_add_memo":
            await event.answer()
            # remember existing invoice state so Set Memo can come back to it
            user_data[user_id] = user_data.get(user_id) or {}
            user_steps[user_id] = "receive_memo"
            msg_content = t(user_id, "memo_prompt")
            msg_buttons = [[Button.inline(t(user_id, "btn_cancel"), b"receive_lightning")]]
            # Get the message to check if it has media
            msg = await event.get_message()
            if msg and msg.media:
                try:
                    await msg.delete()
                except:
                    pass
                await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)
            else:
                try:
                    await event.edit(msg_content, buttons=msg_buttons)
                except:
                    await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)

        elif data == "receive_skip_memo":
            await event.answer()
            try:
                invoice = await create_invoice(user_id, amount_sats=None)

                # Generate QR code for invoice
                qr_buffer = generate_qr_code(invoice)

                # QR display: always delete old message and send new
                try:
                    msg = await event.get_message()
                    if msg:
                        await msg.delete()
                except:
                    pass
                await client.send_file(
                    event.chat_id,
                    qr_buffer,
                    force_document=False,
                    caption=(
                        f"{t(user_id, 'invoice_no_amount_title')}\n\n"
                        f"`{invoice}`"
                    ),
                    buttons=[
                        [Button.inline(t(user_id, "btn_set_amount"), b"receive_set_amount_no_memo")],
                        [Button.inline(t(user_id, "btn_back"), b"receive_lightning")]
                    ]
                )
            except Exception as error:
                await event.edit(t(user_id, "err_alert_line", error=str(error)))

        elif data == "receive_set_amount":
            await event.answer()
            user_steps[user_id] = "receive_amount"

            # Get USD rate for hint
            usd_rate = await get_usd_rate()
            hint = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                hint = t(user_id, "usd_hint", one=f"{sats_per_dollar:,}",
                         five=f"{usd_to_sats(5, usd_rate):,}")

            msg_content = t(user_id, "set_amount_prompt", hint=hint)
            msg_buttons = [[Button.inline(t(user_id, "btn_cancel"), b"receive_lightning")]]
            # Get the message to check if it has media
            msg = await event.get_message()
            if msg and msg.media:
                try:
                    await msg.delete()
                except:
                    pass
                await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)
            else:
                try:
                    await event.edit(msg_content, buttons=msg_buttons)
                except:
                    await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)

        elif data == "receive_set_amount_no_memo":
            await event.answer()
            # ensure user_data exists so a previously-set memo survives the amount update
            user_data[user_id] = user_data.get(user_id) or {}
            user_steps[user_id] = "receive_amount_no_memo"

            # Get USD rate for hint
            usd_rate = await get_usd_rate()
            hint = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                hint = t(user_id, "usd_hint_short", one=f"{sats_per_dollar:,}")

            msg_content = t(user_id, "set_amount_prompt", hint=hint)
            msg_buttons = [[Button.inline(t(user_id, "btn_cancel"), b"receive_lightning")]]
            # Get the message to check if it has media
            msg = await event.get_message()
            if msg and msg.media:
                try:
                    await msg.delete()
                except:
                    pass
                await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)
            else:
                try:
                    await event.edit(msg_content, buttons=msg_buttons)
                except:
                    await client.send_message(event.chat_id, msg_content, buttons=msg_buttons)

        elif data == "send":
            await event.answer()

            # Get balance with fiat
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_fiat(balance, user_id)

            await event.edit(
                f"{t(user_id, 'send_title')}\n\n"
                f"{t(user_id, 'available_line', amount=balance_display)}\n\n"
                f"{t(user_id, 'send_how')}",
                buttons=[
                    [Button.inline(t(user_id, "btn_ln_btc"), b"send_lightning")],
                    [Button.inline(t(user_id, "btn_exchange"), b"send_exchange")],
                    [Button.inline(t(user_id, "btn_token_usd"), b"token_withdraw")],
                    [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                ]
            )

        elif data == "send_lightning":
            user_steps[user_id] = "send_invoice"
            await event.answer()

            # Get balance with fiat
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_fiat(balance, user_id)

            await event.edit(
                f"{t(user_id, 'send_title')}\n\n"
                f"{t(user_id, 'send_paste_any')}\n\n"
                f"{t(user_id, 'available_line', amount=balance_display)}\n\n"
                f"📷 {t(user_id, 'send_qr_hint')}",
                buttons=_row_cancel_to_menu(user_id)
            )

        # Toggle anonymous sender for username payments
        elif data == "toggle_send_anon":
            await event.answer()
            if user_id not in user_data:
                await event.answer(t(user_id, "session_expired"), alert=True)
                return

            # Toggle anonymous mode
            current = user_data[user_id].get('send_anonymous', False)
            user_data[user_id]['send_anonymous'] = not current

            # Rebuild the payment summary with updated anonymous status
            resolved_user = user_data[user_id].get('resolved_user')
            if not resolved_user:
                return

            ru = resolved_user
            has_started = ru.get('has_started_bot', False)
            amount = user_data[user_id].get('amount', 0)
            fee = user_data[user_id].get('fee', 0)
            total = amount + fee if amount else fee

            amount_usd = await format_amount_with_usd(amount, user_id) if amount else 'Variable'
            fee_usd = await format_amount_with_usd(fee, user_id)
            total_usd = await format_amount_with_usd(total, user_id)

            # Show bot status
            bot_status = "✅" if has_started else "⚠️"
            recipient_text = t(user_id, "pay_to_line", username=ru['username'],
                                name=_md_escape(str(ru['display_name'])),
                                status=bot_status)

            is_anonymous = user_data[user_id].get('send_anonymous', False)
            if is_anonymous:
                anon_text = t(user_id, "pay_sender_anon")
                anon_btn = Button.inline(t(user_id, "btn_show_name"), b"toggle_send_anon")
            else:
                anon_text = t(user_id, "pay_sender_visible")
                anon_btn = Button.inline(t(user_id, "btn_anon"), b"toggle_send_anon")

            recipient_text += f"{anon_text}\n\n"

            if ru['is_channel'] or ru['is_group']:
                recipient_tips = t(user_id, "pay_tip_group")
            elif not has_started:
                recipient_tips = t(user_id, "pay_tip_notstarted")
            else:
                recipient_tips = ""

            password_hint = ""
            if not has_send_password(user_id):
                password_hint = t(user_id, "pay_pw_hint")

            buttons = [
                [anon_btn],
                [
                    Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                    Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                ],
            ]

            _q = (user_data.get(user_id) or {}).get('send_queue') or []
            _q_txt = (build_queue_text(_q, user_id)[0] + "\n\n" + "─" * 10 + "\n" + t(user_id, "queue_new_payment") + "\n") if _q else ""
            await event.edit(
                _q_txt + t(user_id, "pay_summary",
                           recipient=recipient_text, amount=amount_usd,
                           fee=fee_usd, total=total_usd,
                           tips=recipient_tips, hint=password_hint),
                buttons=buttons
            )
            return

        # Toggle anonymous sender for send all username payments
        elif data == "toggle_send_anon_all":
            await event.answer()
            if user_id not in user_data:
                await event.answer(t(user_id, "session_expired"), alert=True)
                return

            # Toggle anonymous mode
            current = user_data[user_id].get('send_anonymous', False)
            user_data[user_id]['send_anonymous'] = not current

            # Rebuild the send all summary with updated anonymous status
            resolved_user = user_data[user_id].get('resolved_user')
            if not resolved_user:
                return

            ru = resolved_user
            has_started = ru.get('has_started_bot', False)
            amount = user_data[user_id].get('amount', 0)
            fee = user_data[user_id].get('fee', 0)
            total = amount + fee if amount else fee
            original_balance = user_data[user_id].get('original_balance', total)

            balance_display = await format_balance_with_usd(original_balance, user_id)
            fee_display = await format_amount_with_usd(fee, user_id)
            amount_display = await format_amount_with_usd(amount, user_id)
            total_display = await format_amount_with_usd(total, user_id)

            bot_status = "✅" if has_started else "⚠️"
            recipient_text = t(user_id, "pay_to_line", username=ru['username'],
                                name=_md_escape(str(ru['display_name'])),
                                status=bot_status)

            is_anonymous = user_data[user_id].get('send_anonymous', False)
            if is_anonymous:
                anon_text = t(user_id, "pay_sender_anon")
                anon_btn = Button.inline(t(user_id, "btn_show_name"), b"toggle_send_anon_all")
            else:
                anon_text = t(user_id, "pay_sender_visible")
                anon_btn = Button.inline(t(user_id, "btn_anon"), b"toggle_send_anon_all")

            recipient_text += f"{anon_text}\n\n"

            buttons = [
                [anon_btn],
                [
                    Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                    Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                ],
            ]

            await event.edit(
                t(user_id, "pay_summary_sendall",
                  recipient=recipient_text, balance=balance_display,
                  fee=fee_display, amount=amount_display, total=total_display),
                buttons=buttons
            )
            return

        elif data == "back_to_menu":
            await event.answer()
            if user_id in user_steps:
                del user_steps[user_id]
            if user_id in user_data:
                del user_data[user_id]
            # Get the message to check if it has media
            msg = await event.get_message()
            if msg and msg.media:
                # Media panel (e.g. the deposit QR): DON'T delete it — strip
                # its buttons (address must survive) and open the menu as a
                # NEW message (owner rule)
                try:
                    await client.edit_message(event.chat_id, event.message_id, buttons=None)
                except Exception:
                    pass
                await show_main_menu(event, edit=False)
            else:
                try:
                    await show_main_menu(event, edit=True)
                except:
                    await show_main_menu(event, edit=False)

        elif data == "settings_back":
            if user_id in user_steps:
                del user_steps[user_id]
            if user_id in user_data:
                del user_data[user_id]

            current_fiat = get_user_fiat(user_id)
            fiat_emoji = get_fiat_emoji(current_fiat)
            has_password = has_send_password(user_id)
            password_status = "✅" if has_password else "➖"
            password_btn_text = t(user_id, "btn_send_password") + (" ✏️" if has_password else "")

            # Get private mode status
            private_mode = await get_private_mode(user_id)
            private_status = "✅" if private_mode else "➖"
            private_btn_text = t(user_id, "btn_private_mode") + (" ➖" if private_mode else " ✅")

            lang_name = LANG_NAMES.get(get_user_lang(user_id), get_user_lang(user_id))

            await event.edit(
                f"{t(user_id, 'settings_title')}\n\n"
                f"{fiat_emoji} {t(user_id, 'btn_change_fiat')}: **{current_fiat}**\n"
                f"{t(user_id, 'btn_send_password')}: {password_status}\n"
                f"{t(user_id, 'btn_private_mode')}: {private_status}\n"
                f"{t(user_id, 'btn_language')}: {lang_name}",
                buttons=[
                    [Button.inline(t(user_id, "btn_backup_seed"), b"backup")],
                    [Button.inline(t(user_id, "btn_restore"), b"recovery"), Button.inline(t(user_id, "btn_deposits"), b"unclaimed_deposits")],
                    [Button.inline(t(user_id, "btn_ln_address"), b"change_ln_address"), Button.inline(t(user_id, "btn_change_fiat"), b"change_fiat")],
                    [Button.inline(password_btn_text, b"set_send_password"), Button.inline(private_btn_text, b"toggle_private_mode")],
                    [Button.inline(t(user_id, "btn_language"), b"language_menu")],
                    [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                ]
            )

        # ==================== SEND PASSWORD SETTINGS ====================
        elif data == "set_send_password":
            await event.answer()
            has_password = has_send_password(user_id)

            if has_password:
                # User already has password - show options
                hint = get_password_hint(user_id)
                hint_text = t(user_id, "pw_current_hint", hint=hint) if hint else t(user_id, "pw_no_hint")
                await event.edit(
                    t(user_id, "pw_send_menu", hint_text=hint_text),
                    buttons=[
                        [Button.inline(t(user_id, "btn_chpass"), b"change_send_password")],
                        [Button.inline(t(user_id, "btn_hint"), b"set_password_hint")],
                        [Button.inline(t(user_id, "btn_rm_pass"), b"remove_send_password")],
                        [Button.inline(t(user_id, "btn_forgot"), b"forgot_password")],
                        [Button.inline(t(user_id, "btn_cancel"), b"settings_back")]
                    ]
                )
            else:
                # No password - prompt to set one
                user_steps[user_id] = "set_send_password"
                await event.edit(
                    t(user_id, "pw_set_prompt"),
                    buttons=_row_cancel_settings(user_id)
                )
                # Store message ID for later editing
                if user_id not in user_data:
                    user_data[user_id] = {}
                user_data[user_id]['password_prompt_msg_id'] = event.message_id

        elif data == "change_send_password":
            await event.answer()
            user_steps[user_id] = "verify_old_send_password"
            hint = get_password_hint(user_id)
            hint_text = f"\n\n💡 Hint: {hint}" if hint else ""
            await event.edit(
                t(user_id, "pw_change_prompt", hint=hint_text),
                buttons=_row_cancel_settings(user_id)
            )
            # Store the prompt message ID for later editing
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['password_prompt_msg_id'] = event.message_id

        elif data == "remove_send_password":
            await event.answer()
            user_steps[user_id] = "confirm_remove_send_password"
            hint = get_password_hint(user_id)
            hint_text = f"\n\n💡 Hint: {hint}" if hint else ""
            await event.edit(
                t(user_id, "pw_remove_prompt", hint=hint_text),
                buttons=_row_cancel_settings(user_id)
            )
            # Store the prompt message ID for later editing
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['password_prompt_msg_id'] = event.message_id

        elif data == "set_password_hint":
            await event.answer()
            user_steps[user_id] = "set_password_hint"
            current_hint = get_password_hint(user_id)
            current_text = t(user_id, "pw_current_hint_line", hint=current_hint) if current_hint else ""
            await event.edit(
                t(user_id, "pw_hint_prompt", current=current_text),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"set_send_password")]]
            )

        elif data == "forgot_password":
            await event.answer()
            hint = get_password_hint(user_id)
            reset_info = get_password_reset_request(user_id)

            # Check if reset already requested
            if reset_info:
                remaining = get_reset_remaining_time(user_id)
                if remaining and remaining.total_seconds() > 0:
                    # Still waiting
                    days = remaining.days
                    hours = remaining.seconds // 3600
                    mins = (remaining.seconds % 3600) // 60
                    time_str = f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"
                    await event.edit(
                        t(user_id, "pw_reset_pending", time=time_str),
                        buttons=[
                            [Button.inline(t(user_id, "btn_cancel_reset"), b"cancel_password_reset")],
                            [Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]
                        ]
                    )
                    return
                else:
                    # Time is up - reset password now!
                    remove_send_password(user_id)
                    cancel_password_reset(user_id)
                    await event.edit(
                        t(user_id, "pw_reset_complete"),
                        buttons=[[Button.inline(t(user_id, "btn_settings"), b"settings")]]
                    )
                    return

            hint_text = t(user_id, "pw_your_hint", hint=hint) if hint else ""
            await event.edit(
                t(user_id, "pw_forgot_card", hint_text=hint_text, days=PASSWORD_RESET_DAYS),
                buttons=[
                    [Button.inline(t(user_id, "btn_try_again"), b"backup")],
                    [Button.inline(t(user_id, "btn_req_reset"), b"request_password_reset")],
                    [Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]
                ]
            )

        elif data == "request_password_reset":
            await event.answer()
            request_password_reset(user_id)
            reset_at = datetime.now() + timedelta(days=PASSWORD_RESET_DAYS)
            await event.edit(
                t(user_id, "pw_reset_requested", date=reset_at.strftime('%Y-%m-%d %H:%M')),
                buttons=[
                    [Button.inline(t(user_id, "btn_cancel_reset"), b"cancel_password_reset")],
                    [Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]
                ]
            )

        elif data == "cancel_password_reset":
            await event.answer()
            cancel_password_reset(user_id)
            await event.edit(
                t(user_id, "pw_reset_cancelled"),
                buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
            )

        elif data == "skip_password_hint":
            await event.answer()
            if user_id in user_steps:
                del user_steps[user_id]
            await event.edit(
                t(user_id, "pw_setup_complete"),
                buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
            )

        elif data.startswith("tip_amount_"):
            # Quick tip amount from post-payment prompt
            amount = int(data.replace("tip_amount_", ""))
            DONATE_USER_ID = int(os.getenv('DONATE_USER_ID', '0'))
            await event.answer(t(user_id, "tip_sending_toast"))
            try:
                await init(DONATE_USER_ID)
                donate_invoice = await create_invoice(DONATE_USER_ID, amount_sats=amount, description="Zap tip - thank you!")
                prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, donate_invoice, amount)
                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
                payment = await send_payment(user_id, prepare_response, options)
                is_success, failure_reason = is_payment_successful(payment)
                if is_success:
                    # mark so the tip prompt doesn't re-fire on this tip payment
                    if user_id in user_data:
                        user_data[user_id]['just_donated'] = True
                    await event.edit(
                        t(user_id, "tip_sent_amount", amount=f"{amount:,} {t(user_id, 'sats_unit')}"),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                    )
                else:
                    await event.edit(
                        t(user_id, "tip_failed_reason", reason=failure_reason),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                    )
            except Exception as error:
                await event.edit(
                    t(user_id, "tip_failed_reason", reason=error),
                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                )

        elif data == "tip_custom":
            await event.answer()
            user_steps[user_id] = "tip_custom_amount"
            await event.edit(
                t(user_id, "tip_enter_amount"),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"tip_dismiss")]]
            )

        elif data == "tip_dismiss":
            await event.answer()
            try:
                await event.delete()
            except Exception:
                pass

        elif data == "donate":
            await event.answer()
            await event.edit(
                t(user_id, 'donate_title'),
                buttons=[
                    [Button.inline(t(user_id, "btn_zap_now"), b"donate_send")],
                    [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                ]
            )

        elif data == "donate_send":
            user_steps[user_id] = "donate_amount"

            # Get USD equivalents
            usd_rate = await get_usd_rate()
            usd_info = ""
            if usd_rate:
                usd_info = t(
                    user_id, "donate_usd_info",
                    a=f"{sats_to_usd(1000, usd_rate):.2f}", b=f"{sats_to_usd(5000, usd_rate):.2f}",
                    c=f"{sats_to_usd(10000, usd_rate):.2f}", d=f"{sats_to_usd(21000, usd_rate):.2f}")

            msg = await event.edit(
                t(user_id, 'donate_amount_title') + usd_info,
                buttons=_row_cancel_to_menu(user_id)
            )
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['last_message_id'] = msg.id

        elif data == "scan_payment":
            await event.answer()
            user_steps[user_id] = "scan_payment_input"
            msg = await event.edit(
                t(user_id, "scan_payment_prompt"),
                buttons=_row_cancel_to_menu(user_id)
            )
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['last_message_id'] = msg.id

        elif data.startswith("confirm_payment_"):
            # Double-click prevention: check if payment is already being processed
            async with user_lock(user_id):
                if user_id in user_data and user_data[user_id].get('payment_processing'):
                    await event.answer(t(user_id, "pay_in_progress"))
                    return

                if user_id in user_data and 'prepare_response' in user_data[user_id]:
                    # Check if user has send password and hasn't verified it yet
                    if has_send_password(user_id) and not user_data[user_id].get('password_verified'):
                        await event.answer()
                        user_steps[user_id] = "verify_send_password_for_payment"
                        hint = get_password_hint(user_id)
                        hint_text = t(user_id, "pw_hint_hint", hint=hint) if hint else ""
                        await event.edit(
                            t(user_id, "pw_required_pay", hint=hint_text),
                            buttons=[[Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")]]
                        )
                        # Store message ID for later editing
                        user_data[user_id]['password_prompt_msg_id'] = event.message_id
                        return

                    send_started = False
                    try:
                        # Mark payment as processing IMMEDIATELY to prevent double-clicks
                        user_data[user_id]['payment_processing'] = True

                        await event.answer(t(user_id, "processing_payment"))
                        prepare_response = user_data[user_id]['prepare_response']
                        payment_type = user_data[user_id].get('payment_type', 'bolt11')
                        fee = user_data[user_id].get('fee', 0)

                        if payment_type == 'lnurl':
                            send_started = True
                            response = await lnurl_pay(user_id, prepare_response)
                            lnurl_payment = getattr(response, 'payment', None)
                            amount = user_data[user_id].get('amount', 'N/A')
                            amount_display = await format_amount_with_usd(amount, user_id) if isinstance(amount, int) else amount
                            fee_display = await format_amount_with_usd(fee, user_id)
                            is_success, failure_reason = is_payment_successful(lnurl_payment)
                            if not is_success:
                                if failure_reason == 'failed':
                                    await event.edit(t(user_id, 'payment_failed'), buttons=None)
                                    return
                                # Owner order: never show 'payment_hold' — neutral
                                # processing text + read-only background recheck.
                                # payment_processing stays set while unresolved.
                                await event.edit(t(user_id, 'payment_processing_status'), buttons=None)
                                _spawn_payment_recheck(
                                    user_id=user_id, chat_id=event.chat_id,
                                    message_id=event.message_id,
                                    payment_id=getattr(lnurl_payment, 'id', None),
                                    success_text=_lnurl_success_text(amount_display, fee_display, user_id=user_id),
                                    amount=amount if isinstance(amount, int) else None,
                                    fee=fee, prepare_response=prepare_response,
                                    manage_gate=True)
                                return

                            # Track outgoing volume
                            if isinstance(amount, int) and amount > 0:
                                track_volume('out', amount + fee)

                            await event.edit(_lnurl_success_text(amount_display, fee_display, user_id=user_id))
                            await show_tip_prompt(user_id)
                        else:
                            options = user_data[user_id].get('options')

                            # Get resolved user info BEFORE payment
                            resolved_user = user_data[user_id].get('resolved_user')
                            is_anonymous = user_data[user_id].get('send_anonymous', False)
                            pre_amount = user_data[user_id].get('amount', 0)

                            # Store sender info BEFORE payment so event listener can use it
                            if resolved_user:
                                recipient_id = resolved_user['id']
                                has_started = resolved_user.get('has_started_bot', False)

                                if has_started:
                                    try:
                                        # Build sender info
                                        if is_anonymous:
                                            sender_info = t(user_id, "anon_sender_label")
                                        else:
                                            sender = await client.get_entity(user_id)
                                            sender_name = f"@{sender.username}" if sender.username else sender.first_name
                                            sender_info = sender_name

                                        # Store sender info for event listener BEFORE payment
                                        with pending_sender_info_lock:
                                            pending_sender_info[recipient_id] = {
                                                'sender_info': sender_info,
                                                'timestamp': datetime.now(),
                                                'amount': pre_amount
                                            }
                                    except Exception as e:
                                        logging.debug(f"Could not prepare sender info: {e}")

                            # Now send the payment
                            send_started = True
                            payment = await send_payment(user_id, prepare_response, options)

                            payment_id = getattr(payment, 'id', 'N/A')
                            amount = getattr(payment, 'amount', user_data[user_id].get('amount', 'N/A'))
                            amount_display = await format_amount_with_usd(amount, user_id) if isinstance(amount, int) else amount
                            fee_display = await format_amount_with_usd(fee, user_id)

                            # Check if payment is truly successful
                            is_success, failure_reason = is_payment_successful(payment)

                            # An unresolved SDK result is not a failed payment; preserve the
                            # processing claim in memory and prohibit this confirmation replay.
                            # Owner order: never show 'payment_hold' on the send path. Show a
                            # neutral processing text and resolve it with a READ-ONLY
                            # background recheck (no retry, no refund). payment_processing
                            # deliberately stays set while the outcome is unresolved — it is
                            # the only duplicate-send guard; the recheck clears it on a
                            # conclusive result (see _payment_recheck_gate_cleanup).
                            if not is_success and failure_reason != 'failed':
                                await event.edit(t(user_id, 'payment_processing_status'), buttons=None)
                                _spawn_payment_recheck(
                                    user_id=user_id, chat_id=event.chat_id,
                                    message_id=event.message_id, payment_id=payment_id,
                                    success_text=_payment_success_text(amount_display, fee_display, payment_id, user_id=user_id),
                                    success_buttons=_payment_success_buttons(user_id, payment_id),
                                    amount=amount if isinstance(amount, int) else None,
                                    fee=fee, prepare_response=prepare_response,
                                    manage_gate=True)
                                return
                            if is_success:
                                # Track outgoing volume
                                if isinstance(amount, int) and amount > 0:
                                    track_volume('out', amount + fee)

                                await event.edit(
                                    _payment_success_text(amount_display, fee_display, payment_id, user_id=user_id),
                                    buttons=_payment_success_buttons(user_id, payment_id)
                                )
                                await show_tip_prompt(user_id)
                            else:
                                # Payment failed - remove pending sender info
                                if resolved_user:
                                    with pending_sender_info_lock:
                                        if resolved_user['id'] in pending_sender_info:
                                            del pending_sender_info[resolved_user['id']]

                                await event.edit(
                                    t(user_id, "pay_failed_detail",
                                      amount=amount_display, reason=failure_reason,
                                      pid=payment_id),
                                    buttons=[
                                        [Button.inline(t(user_id, "btn_pay_details"), f"track_{payment_id}".encode())],
                                        [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                                    ]
                                )

                        del user_data[user_id]
                        if user_id in user_steps:
                            del user_steps[user_id]

                    except Exception as error:
                        if send_started and _duplicate_payment_error(error):
                            # Conclusive duplicate (2026-11 incident): the network
                            # rejected the request outright — this payment hash was
                            # already submitted, so NOTHING new was sent and the
                            # outcome is NOT unknown. Release the gate instead of
                            # wedging it behind a timer-only recheck forever; never
                            # spawn a send or a recheck here.
                            if user_id in user_data:
                                user_data[user_id]['payment_processing'] = False
                            logging.info(
                                'duplicate invoice rejected (already-paid payment hash) uid=%s',
                                user_id)
                            await event.edit(
                                t(user_id, 'payment_duplicate_invoice'),
                                buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                            )
                        elif send_started:
                            # Submission may have succeeded; do not reopen this callback.
                            # No payment object came back, so the recheck runs timer-only
                            # and ages out to the calm still-processing message. The
                            # payment_processing gate stays set: the outcome is unknown
                            # and a second confirm could double-pay.
                            await event.edit(t(user_id, 'payment_processing_status'), buttons=None)
                            _spawn_payment_recheck(
                                user_id=user_id, chat_id=event.chat_id,
                                message_id=event.message_id, payment_id=None,
                                success_text='', prepare_response=prepare_response,
                                manage_gate=True)
                        else:
                            if user_id in user_data:
                                user_data[user_id]['payment_processing'] = False
                            await event.edit(
                                t(user_id, "pay_failed_err", error=str(error)),
                                buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                            )
                else:
                    await event.answer(t(user_id, "pay_data_expired"))

        elif data.startswith("onchain_speed_") and not data.endswith("_all"):
            if user_id not in user_data:
                await event.edit(t(user_id, "session_expired"))
                return

            user_data_obj = user_data[user_id]

            if 'amount' not in user_data_obj:
                await event.edit(
                    t(user_id, "amount_missing_retry"),
                    buttons=_row_back_menu(user_id)
                )
                return

            speed = data.split("_")[2]

            if speed == "slow":
                confirmation_speed = OnchainConfirmationSpeed.SLOW
            elif speed == "medium":
                confirmation_speed = OnchainConfirmationSpeed.MEDIUM
            else:
                confirmation_speed = OnchainConfirmationSpeed.FAST

            amount = user_data_obj['amount']
            selected_fee = user_data_obj['fees'][speed]

            user_data_obj['options'] = SendPaymentOptions.BITCOIN_ADDRESS(
                confirmation_speed=confirmation_speed
            )
            user_data_obj['fee'] = selected_fee

            # Format with USD
            amount_display = await format_amount_with_usd(amount, user_id)
            fee_display = await format_amount_with_usd(selected_fee, user_id)
            total_display = await format_amount_with_usd(amount + selected_fee, user_id)

            await event.edit(
                t(user_id, "pay_summary_onchain",
                  amount=amount_display, speed=speed, fee=fee_display,
                  total=total_display),
                buttons=[
                    [
                        Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                        Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                    ],
                ]
            )

        # Quick send amount buttons
        elif data.startswith("send_quick_"):
            amount = int(data.replace("send_quick_", ""))
            await event.answer(t(user_id, "quick_send_toast", amount=f"{amount:,}"))

            # Check balance
            balance = int(await get_balance(user_id))
            if balance < amount:
                await event.edit(
                    t(user_id, "insuff_quick", amount=amount, balance=balance),
                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                )
                return

            # Check if user has payment data
            if user_id not in user_data or ('invoice' not in user_data[user_id] and 'pay_request' not in user_data[user_id]):
                await event.edit(
                    t(user_id, "session_expired"),
                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                )
                return

            user_data[user_id]['amount'] = amount
            invoice = user_data[user_id].get('invoice')
            payment_type = user_data[user_id].get('payment_type', 'bolt11')

            if payment_type == 'lnurl':
                # LNURL payment - prepare LNURL pay
                pay_request = user_data[user_id]['pay_request']
                min_sats = user_data[user_id].get('min_sats', 1)
                max_sats = user_data[user_id].get('max_sats', 999999999)

                if amount < min_sats or amount > max_sats:
                    await event.edit(
                        t(user_id, "send_amount_range", min_sats=min_sats, max_sats=max_sats),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"send"), Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                    )
                    return

                await event.edit(t(user_id, "send_preparing"))
                try:
                    sdk = await get_wallet(user_id)
                    prepare_response = await sdk.prepare_lnurl_pay(PrepareLnurlPayRequest(pay_request=pay_request, amount=amount))

                    fee = 0
                    if hasattr(prepare_response, 'fees'):
                        fees = prepare_response.fees
                        if hasattr(fees, 'lightning_fee_sats'):
                            fee = fees.lightning_fee_sats
                        elif hasattr(fees, 'total_fee_sats'):
                            fee = fees.total_fee_sats
                    elif hasattr(prepare_response, 'fee_sats'):
                        fee = prepare_response.fee_sats

                    user_data[user_id]['prepare_response'] = prepare_response
                    user_data[user_id]['fee'] = fee
                    user_steps[user_id] = "send_confirm"

                    amount_display = await format_amount_with_usd(amount, user_id)
                    fee_display = await format_amount_with_usd(fee, user_id)
                    total = amount + fee
                    total_display = await format_amount_with_usd(total, user_id)

                    await event.edit(
                        t(user_id, "pay_summary_quick",
                          amount=amount_display, fee=fee_display, total=total_display),
                        buttons=[
                            [Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"), Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")],
                        ]
                    )
                except Exception as e:
                    await event.edit(t(user_id, "err_alert_line", error=e), buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
            elif payment_type == 'onchain':
                # Onchain: need fee speed selection (same as typed-amount flow)
                try:
                    res = await event.edit(t(user_id, "calc_onchain_fees"))
                    prepare_response, _, _ = await prepare_payment(user_id, invoice, amount)
                    fee_quote = prepare_response.payment_method.fee_quote
                    slow_fee = fee_quote.speed_slow.user_fee_sat + fee_quote.speed_slow.l1_broadcast_fee_sat
                    medium_fee = fee_quote.speed_medium.user_fee_sat + fee_quote.speed_medium.l1_broadcast_fee_sat
                    fast_fee = fee_quote.speed_fast.user_fee_sat + fee_quote.speed_fast.l1_broadcast_fee_sat
                    user_data[user_id]['prepare_response'] = prepare_response
                    user_data[user_id]['fees'] = {'slow': slow_fee, 'medium': medium_fee, 'fast': fast_fee}
                    amount_display = await format_amount_with_usd(amount, user_id)
                    await event.edit(
                        t(user_id, "pay_summary_speeds",
                          amount=amount_display, slow_fee=slow_fee,
                          medium_fee=medium_fee, fast_fee=fast_fee),
                        buttons=[
                            [
                                Button.inline(t(user_id, "btn_fee_slow"), b"onchain_speed_slow"),
                                Button.inline(t(user_id, "btn_fee_medium"), b"onchain_speed_medium"),
                                Button.inline(t(user_id, "btn_fee_fast"), b"onchain_speed_fast")
                            ],
                            [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                        ]
                    )
                except Exception as e:
                    await event.edit(t(user_id, "err_bare", error=e), buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
            else:
                # Regular invoice payment
                user_steps[user_id] = "send_confirm"
                await prepare_and_show_fee(event, user_id, invoice, amount)
            return

        elif data == "send_withdraw_all":
            # Withdraw all balance
            balance = int(await get_balance(user_id))
            if balance <= 0:
                await event.answer(t(user_id, "no_balance_wd"))
                return

            # Check if user has payment data (invoice OR pay_request for LNURL)
            if user_id in user_data and ('invoice' in user_data[user_id] or 'pay_request' in user_data[user_id]):
                payment_type = user_data[user_id].get('payment_type', 'bolt11')

                try:
                    res = await event.edit(t(user_id, "calc_fees_wd"))

                    if payment_type == 'onchain':
                        # Get bitcoin address from user_data
                        btc_address = user_data[user_id].get('invoice') or user_data[user_id].get('address')
                        if not btc_address:
                            await event.answer(t(user_id, "pay_data_missing"))
                            return

                        prepare_response, _, _ = await prepare_payment(user_id, btc_address, balance)

                        fee_quote = prepare_response.payment_method.fee_quote
                        slow_fee = fee_quote.speed_slow.user_fee_sat + fee_quote.speed_slow.l1_broadcast_fee_sat
                        medium_fee = fee_quote.speed_medium.user_fee_sat + fee_quote.speed_medium.l1_broadcast_fee_sat
                        fast_fee = fee_quote.speed_fast.user_fee_sat + fee_quote.speed_fast.l1_broadcast_fee_sat

                        slow_amount = balance - slow_fee
                        medium_amount = balance - medium_fee
                        fast_amount = balance - fast_fee

                        user_data[user_id]['fees'] = {
                            'slow': slow_fee,
                            'medium': medium_fee,
                            'fast': fast_fee
                        }
                        user_data[user_id]['withdraw_all'] = True
                        user_data[user_id]['original_balance'] = balance

                        # Format with USD
                        balance_display = await format_balance_with_usd(balance, user_id)

                        await res.edit(
                            t(user_id, "wd_all_onchain", balance=balance_display,
                              slow_fee=slow_fee, slow_amt=slow_amount,
                              med_fee=medium_fee, med_amt=medium_amount,
                              fast_fee=fast_fee, fast_amt=fast_amount),
                            buttons=[
                                [
                                    Button.inline(t(user_id, "btn_fee_slow"), b"onchain_speed_slow_all"),
                                    Button.inline(t(user_id, "btn_fee_medium"), b"onchain_speed_medium_all"),
                                    Button.inline(t(user_id, "btn_fee_fast"), b"onchain_speed_fast_all")
                                ],
                                [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                            ]
                        )
                    else:
                        # Lightning (BOLT11 or LNURL)
                        if payment_type == 'lnurl':
                            # LNURL Payment
                            pay_request = user_data[user_id].get('pay_request')
                            if not pay_request:
                                await event.answer(t(user_id, "pay_data_missing"))
                                return

                            min_sats = user_data[user_id].get('min_sats', 0)
                            max_sats = user_data[user_id].get('max_sats', 0)

                            # Check limits
                            if balance < min_sats:
                                await res.edit(
                                    t(user_id, "insufficient_balance"),
                                    buttons=_row_back_menu(user_id)
                                )
                                return

                            send_amount = min(balance, max_sats)
                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, send_amount)
                            final_amount = send_amount - fee_sats

                            if final_amount <= 0:
                                await res.edit(t(user_id, "balance_too_low_fees"), buttons=_row_back_menu(user_id))
                                return

                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, final_amount)
                            user_data[user_id]['prepare_response'] = prepare_response
                            user_data[user_id]['fee'] = fee_sats
                            user_data[user_id]['amount'] = final_amount

                            # Format with USD
                            balance_display = await format_balance_with_usd(balance, user_id)
                            fee_display = await format_amount_with_usd(fee_sats, user_id)
                            amount_display = await format_amount_with_usd(final_amount, user_id)

                            await res.edit(
                                t(user_id, "wd_all_lnurl", balance=balance_display,
                                  fee=fee_display, amount=amount_display),
                                buttons=[
                                    [Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes")],
                                    [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                                ]
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]
                        else:
                            # BOLT11 Invoice
                            invoice = user_data[user_id].get('invoice')
                            if not invoice:
                                await event.answer(t(user_id, "pay_data_missing"))
                                return

                            prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, balance)

                            # Use spark_fee if available, otherwise fee_sats
                            if spark_fee is not None:
                                final_fee = spark_fee
                            else:
                                final_fee = fee_sats if fee_sats else 0

                            final_amount = balance - final_fee

                            if final_amount <= 0:
                                await res.edit(
                                    t(user_id, "insuff_withdraw"),
                                    buttons=_row_back_menu(user_id)
                                )
                                return

                            prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, final_amount)

                            # Use spark_fee if available, otherwise fee_sats
                            if spark_fee is not None:
                                final_fee = spark_fee
                            else:
                                final_fee = fee_sats if fee_sats else 0

                            user_data[user_id]['prepare_response'] = prepare_response
                            user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=PAYMENT_COMPLETION_TIMEOUT_SECS)
                            user_data[user_id]['fee'] = final_fee
                            user_data[user_id]['amount'] = final_amount
                            user_data[user_id]['original_balance'] = balance

                            total = final_amount + final_fee

                            # Format with USD
                            balance_display = await format_balance_with_usd(balance, user_id)
                            fee_display = await format_amount_with_usd(final_fee, user_id)
                            amount_display = await format_amount_with_usd(final_amount, user_id)
                            total_display = await format_amount_with_usd(total, user_id)

                            # Check if this is username payment (has resolved_user)
                            resolved_user = user_data[user_id].get('resolved_user')
                            buttons = []
                            recipient_text = ""

                            if resolved_user:
                                ru = resolved_user
                                has_started = ru.get('has_started_bot', False)
                                bot_status = "✅" if has_started else "⚠️"
                                recipient_text = f"👤 To: @{ru['username']} ({ru['display_name']}) {bot_status}\n\n"

                                # Anonymous sender option
                                is_anonymous = user_data[user_id].get('send_anonymous', False)
                                if is_anonymous:
                                    anon_text = t(user_id, "pay_sender_anon")
                                    anon_btn = Button.inline(t(user_id, "btn_show_name"), b"toggle_send_anon_all")
                                else:
                                    anon_text = t(user_id, "pay_sender_visible")
                                    anon_btn = Button.inline(t(user_id, "btn_anon"), b"toggle_send_anon_all")

                                recipient_text += f"{anon_text}\n\n"
                                buttons.append([anon_btn])

                            buttons.append([
                                Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                                Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                            ])
                            await res.edit(
                                t(user_id, "pay_summary_sendall",
                                  recipient=recipient_text, balance=balance_display,
                                  fee=fee_display, amount=amount_display,
                                  total=total_display),
                                buttons=buttons
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]

                except Exception as error:
                    await event.edit(
                        t(user_id, "err_alert_line", error=str(error)),
                        buttons=_row_back_menu(user_id)
                    )

        elif data.startswith("onchain_speed_") and data.endswith("_all"):
            # Handle withdraw all with specific speed
            if user_id in user_data and user_data[user_id].get('withdraw_all'):
                speed_parts = data.split("_")
                speed = speed_parts[2]

                user_data_obj = user_data[user_id]
                balance = user_data_obj['original_balance']
                selected_fee = user_data_obj['fees'][speed]
                final_amount = balance - selected_fee

                if final_amount <= 0:
                    await event.answer(t(user_id, "insuff_after_fees"))
                    return

                try:
                    invoice = user_data_obj['invoice']
                    prepare_response, _, _ = await prepare_payment(user_id, invoice, final_amount)

                    if speed == "slow":
                        confirmation_speed = OnchainConfirmationSpeed.SLOW
                    elif speed == "medium":
                        confirmation_speed = OnchainConfirmationSpeed.MEDIUM
                    else:
                        confirmation_speed = OnchainConfirmationSpeed.FAST

                    options = SendPaymentOptions.BITCOIN_ADDRESS(
                        confirmation_speed=confirmation_speed
                    )

                    user_data_obj['prepare_response'] = prepare_response
                    user_data_obj['options'] = options
                    user_data_obj['fee'] = selected_fee
                    user_data_obj['amount'] = final_amount

                    # Format with USD
                    balance_display = await format_balance_with_usd(balance, user_id)
                    fee_display = await format_amount_with_usd(selected_fee, user_id)
                    amount_display = await format_amount_with_usd(final_amount, user_id)

                    await event.edit(
                        t(user_id, "wd_all_summary", balance=balance_display,
                          speed=speed, fee=fee_display, amount=amount_display),
                        buttons=[
                            [
                                Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                                Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")
                            ]
                        ]
                    )
                except Exception as error:
                    await event.answer(t(user_id, "err_alert_line", error=str(error)))

        # Zap cancel handler
        elif data.startswith("zap_cancel_"):
            zap_id = data.replace("zap_cancel_", "")

            # Find the zap data by zap_id
            zap_data = None
            zap_owner_id = None
            for uid, zdata in pending_zap_confirmations.items():
                if zdata.get('id') == zap_id:
                    zap_data = zdata
                    zap_owner_id = uid
                    break

            if not zap_data:
                await event.answer(t(user_id, "zap_req_expired"), alert=True)
                return

            # Check if the user clicking is the sender
            # For anonymous requests, allow any user to cancel
            is_anonymous = zap_data.get('is_anonymous', False)

            # During 'initializing' phase, is_anonymous might not be set yet
            # In that case, check the original sender_id from the event
            if is_anonymous is False and zap_data.get('status') == 'initializing':
                # Still initializing, use sender_id from stored data
                if user_id != zap_data['sender_id']:
                    await event.answer(t(user_id, "zap_only_sender_cancel"), alert=True)
                    return
            elif not is_anonymous and user_id != zap_data['sender_id']:
                await event.answer(t(user_id, "zap_only_sender_cancel"), alert=True)
                return

            # Mark as cancelled - the background task will handle cleanup
            if zap_owner_id in pending_zap_confirmations:
                pending_zap_confirmations[zap_owner_id]['status'] = 'cancelled'

            await event.edit(t(user_id, "zap_cancelled"), buttons=None)
            await event.answer(t(user_id, "zap_cancelled_toast"))

        # Balance button handler
        elif data.startswith("show_balance_"):
            balance_id = data.replace("show_balance_", "")

            # Import the pending_balance from balance_handler
            from __main__ import balance_handler
            pending = getattr(balance_handler, 'pending_balance', {})

            if balance_id not in pending:
                await event.answer(t(user_id, "balance_req_expired"), alert=True)
                return

            balance_data = pending[balance_id]

            # Check if the user clicking is the owner
            # If sent anonymously (via channel), allow the clicker to see their own balance
            is_anonymous = balance_data.get('is_anonymous', False)
            if not is_anonymous and user_id != balance_data['user_id']:
                await event.answer(t(user_id, "balance_not_yours"), alert=True)
                return

            # For anonymous requests, use the clicking user's ID for their balance
            target_user_id = user_id

            # Show balance via answer (private popup)
            await init(target_user_id)
            balance = await get_balance(target_user_id)
            balance_display = await format_balance_with_usd(balance, target_user_id)

            await event.answer(
                t(user_id, "balance_reveal", balance=balance_display),
                alert=True
            )

            # Clean up old balance requests (older than 5 minutes)
            now = datetime.now()
            expired = [k for k, v in pending.items()
                      if (now - v['created_at']).total_seconds() > 300]
            for k in expired:
                del pending[k]

    except Exception as error:
        logging.error(f"Callback error: {error}")
        await event.answer(t(user_id, "err_alert_line", error=str(error)))


async def parse_amount_input(text, usd_rate=None):
    """Parse amount input - supports both sats and USD ($)

    Returns: (amount_sats, is_usd, original_usd)
    """
    text = text.strip()

    # Check for USD format ($X or X$)
    usd_match = re.match(r'^\$(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\$$', text)

    if usd_match:
        usd_amount = float(usd_match.group(1) or usd_match.group(2))

        if not usd_rate:
            usd_rate = await get_usd_rate()

        if not usd_rate:
            return None, True, usd_amount  # Can't convert

        sats = usd_to_sats(usd_amount, usd_rate)
        return sats, True, usd_amount

    # Regular sats input
    if text.isdigit():
        return int(text), False, None

    # Try to parse as float for sats
    # pinned semantics: int(float()) silently floors; inf/nan fall to the except
    try:
        return int(float(text)), False, None
    except:
        return None, False, None


async def _step_webhook_url(event, user_id, text):
    """webhook_url_input step (item 26): register an HTTPS webhook."""
    url = text.strip()
    if not url.startswith("https://"):
        await event.respond(t(user_id, "wh_https"))
        return
    user_steps.pop(user_id, None)
    try:
        sdk = await get_wallet(user_id)
        import secrets as _sec
        from breez_sdk_spark import RegisterWebhookRequest, WebhookEventType
        secret = _sec.token_hex(16)
        events = [WebhookEventType.LIGHTNING_SEND_FINISHED,
                  WebhookEventType.LIGHTNING_RECEIVE_FINISHED,
                  WebhookEventType.STATIC_DEPOSIT_FINISHED]
        resp = await sdk.register_webhook(RegisterWebhookRequest(
            url=url, secret=secret, event_types=events))
        wid = getattr(resp, 'webhook_id', getattr(resp, 'id', '?'))
        await event.respond(
            t(user_id, "wh_registered", wid=wid, secret=secret))
    except Exception as e:
        await event.respond(t(user_id, "err_bare", error=str(e)))


async def _step_webhook_del(event, user_id, text):
    """webhook_del_input step (item 26): unregister the matching webhook."""
    user_steps.pop(user_id, None)
    try:
        sdk = await get_wallet(user_id)
        from breez_sdk_spark import UnregisterWebhookRequest
        hooks = await sdk.list_webhooks() or []
        wid = None
        for w in hooks:
            if text.strip() in (getattr(w, 'id', ''), getattr(w, 'url', '')):
                wid = getattr(w, 'id', '')
                break
        if not wid:
            await event.respond(t(user_id, "wh_no_match"))
            return
        await sdk.unregister_webhook(UnregisterWebhookRequest(webhook_id=wid))
        await event.respond(t(user_id, "wh_removed"))
    except Exception as e:
        await event.respond(t(user_id, "err_bare", error=str(e)))


@client.on(events.NewMessage(func=lambda e: e.is_private and not (e.text or '').startswith('/')))
async def message_handler(event):
    """Handle text messages based on user step"""
    user_id = event.sender_id

    # Mark user as active
    await mark_user_active(user_id)

    # Secret input must never enter P2P wizard, chat, or media routing.
    sensitive_step = user_steps.get(user_id) in {
        'set_send_password', 'set_new_send_password', 'verify_old_send_password',
        'confirm_remove_send_password', 'verify_password_for_backup',
        'verify_send_password_for_payment', 'verify_send_password_for_ffio',
        'verify_send_password_for_token',
        'verify_send_password_for_batch',
        'set_password_hint', 'set_password_hint_after_password', 'recovery_input',
    }
    has_media = any(getattr(event, kind, None) for kind in ('photo', 'document', 'audio', 'video', 'voice'))
    if sensitive_step and (has_media or getattr(event, 'media', None)):
        await event.respond(t(user_id, "media_not_text"))
        return

    # ---- P2P section: check media first if active trade chat open ----
    if not sensitive_step:
        try:
            import zap_p2p
            if has_media and not (event.text or '').strip():
                if await zap_p2p.on_media(client, event):
                    return
        except Exception as p2p_err:
            logging.error(f"p2p media pre-check error: {p2p_err}")

    # ---- QR image support: photos get QR-decoded into payment text ----
    if getattr(event, 'photo', None) and not (event.text or '').strip():
        current_step_qr = user_steps.get(user_id)
        if current_step_qr in ('send_invoice', 'send_queue_next', None):
            try:
                media_bytes = await event.download_media(bytes)
                decoded = decode_qr_image(media_bytes) if media_bytes else None
                if decoded:
                    # Strip common QR URI wrappers; treat the rest as pasted input
                    for prefix in ('bitcoin:', 'lightning:'):
                        if decoded.startswith(prefix):
                            decoded = decoded[len(prefix):]
                            break
                    event.text = decoded
                    if current_step_qr is None:
                        user_steps[user_id] = 'send_invoice'
                else:
                    await event.respond(
                        f"❌ {t(user_id, 'qr_not_found')}",
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                    )
                    return
            except Exception as qr_error:
                logging.warning(f"QR handling failed: {qr_error}")

    text = (event.text or '').strip()

    # ---- P2P section: wizard steps + anonymous trade chat (own gate, first try) ----
    if not sensitive_step:
        try:
            import zap_p2p
            # photos go to the anonymous trade chat when a chat is open
            if has_media and not (event.text or '').strip():
                if await zap_p2p.on_media(client, event):
                    return
            if await zap_p2p.on_text(client, event, text):
                return
        except Exception as p2p_err:
            logging.error(f"p2p text router error: {p2p_err}")

    current_step = user_steps.get(user_id)

    if current_step == "send_queue_next":
        # while queuing, treat pasted payment input as a new payment to parse
        if _looks_like_payment_input(text):
            user_steps[user_id] = "send_invoice"
            current_step = "send_invoice"
        # else: fall through — user may type something else; keep state

    if not current_step:
        # v30: auto-detect a pasted invoice / address / username and jump into Send.
        if _looks_like_payment_input(text):
            user_steps[user_id] = "send_invoice"
            current_step = "send_invoice"
        else:
            await show_main_menu(event)
            return

    try:
        if current_step == 'verify_send_password_for_batch':
            ud = user_data.get(user_id) or {}
            batch_id = ud.get('batch_auth_pending')
            await delete_password_message(event)
            entries = ud.get('batch_items') or []
            if (not batch_id or batch_id != ud.get('batch_id') or not entries
                    or not has_send_password(user_id)):
                ud.pop('batch_auth_pending', None)
                user_steps.pop(user_id, None)
                await event.respond(t(user_id, 'batch_password_failed'))
                return
            if not verify_send_password(user_id, text):
                await event.respond(t(user_id, 'batch_password_retry'))
                return
            ud.pop('batch_auth_pending', None)
            ud['batch_auth'] = [batch_id, get_user_send_password_hash(user_id)]
            user_steps.pop(user_id, None)
            await event.respond(t(user_id, 'batch_password_verified'), buttons=[
                [Button.inline(t(user_id, 'btn_send_all_n', n=len(entries)), b'batch_send_all')]])
            return

        if current_step == 'verify_send_password_for_token':
            ud = user_data.get(user_id) or {}
            binding = ud.get('tk_auth_pending')
            await delete_password_message(event)
            if (not binding or user_steps.get(user_id) != current_step
                    or token_withdraw_auth_binding(user_id, ud) != binding
                    or not has_send_password(user_id)):
                ud.pop('tk_auth_pending', None)
                ud.pop('tk_auth_verified', None)
                ud.pop('tk_auth_password_hash', None)
                await event.respond(t(user_id, 'token_wd_password_failed'), buttons=[
                    [Button.inline(t(user_id, 'btn_cancel'), b'back_to_menu')]])
                return
            if not verify_send_password(user_id, text):
                ud.pop('tk_auth_verified', None)
                ud.pop('tk_auth_password_hash', None)
                await event.respond(t(user_id, 'token_wd_password_retry'), buttons=[
                    [Button.inline(t(user_id, 'btn_cancel'), b'back_to_menu')]])
                return
            ud.pop('tk_auth_pending', None)
            ud['tk_auth_verified'] = binding
            ud['tk_auth_password_hash'] = get_user_send_password_hash(user_id)
            user_steps[user_id] = 'token_wd_confirm'
            await event.respond(t(user_id, 'token_wd_password_verified'), buttons=[
                [Button.inline(t(user_id, 'btn_confirm_wd'),
                               f"tkwd_go:{ud['tk_confirm_nonce']}".encode())],
                [Button.inline(t(user_id, 'btn_cancel'), b'back_to_menu')]])
            return

        # ---- v30: contact payment flow (enter amount) ----
        if current_step == "contact_pay_amount":
            try:
                amount = int(text.replace(',', ''))
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await event.respond(t(user_id, "sats_positive_invalid"))
                return
            cp = (user_data.get(user_id) or {}).get('contact_pay') or {}
            identifier = cp.get('identifier')
            if not identifier:
                user_steps.pop(user_id, None)
                await event.respond(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back_contacts"), b"contacts")]])
                return
            await event.respond(t(user_id, "preparing_payment"))
            try:
                prep, fee, spark_fee = await prepare_payment(user_id, identifier, amount)
            except Exception as pe:
                user_steps.pop(user_id, None)
                await event.respond(t(user_id, "contact_prep_err", error=str(pe)),
                                    buttons=[[Button.inline(t(user_id, "btn_back_contacts"), b"contacts")]])
                return
            cp['prepare'] = prep
            user_data.setdefault(user_id, {})['contact_pay'] = cp
            fee_note = ""
            try:
                if isinstance(prep.payment_method, SendPaymentMethod.BOLT11_INVOICE):
                    fee_note = t(user_id, "contact_fee_note", fee=fee or 0)
            except Exception:
                pass
            user_steps[user_id] = "contact_pay_confirm"
            await event.respond(
                t(user_id, "contact_confirm", amount=f"{amount:,}",
                  name=cp.get('name'), fee_note=fee_note),
                buttons=[
                    [Button.inline(t(user_id, "btn_confirm_send"), b"confcontactpay")],
                    [Button.inline(t(user_id, "btn_cancel"), b"contacts")]
                ])
            return

        # ---- v30: sats / BTC / fiat calculator ----
        if current_step == "calc_input":
            raw = text.strip()
            fiat_code = get_user_fiat(user_id)
            fiat_code_l = (fiat_code or '').lower()
            m = re.match(r'^\$?\s*([\d.,]+)\s*([A-Za-z]*)$', raw)
            if not m:
                await event.respond(t(user_id, "calc_parse_err"))
                return
            try:
                value = float(m.group(1).replace(',', ''))
            except ValueError:
                await event.respond(t(user_id, "calc_invalid_number"))
                return
            unit = m.group(2).lower()
            fiat_rate = await get_fiat_rate(fiat_code) or 0

            # pinned semantics: int(round(...)) float rounding is the spec
            if unit in ('btc', 'bitcoin', 'xbt'):
                sats = int(round(value * SATS_PER_BTC))
            elif raw.startswith('$') or (fiat_code_l and unit == fiat_code_l) or unit == 'usd':
                if not fiat_rate:
                    await event.respond(t(user_id, "calc_no_rate", fiat=fiat_code))
                    return
                sats = int(round(value / fiat_rate * SATS_PER_BTC))
            else:
                sats = int(round(value))  # default: sats

            btc = sats / SATS_PER_BTC
            fiat_emoji = get_fiat_emoji(fiat_code)
            fiat_val = (btc * fiat_rate) if fiat_rate else None
            if fiat_val is not None:
                fiat_line = f"{fiat_emoji} **{fiat_val:,.2f} {fiat_code}**"
            else:
                fiat_line = t(user_id, "calc_rate_unavailable", emoji=fiat_emoji)

            del user_steps[user_id]
            await event.respond(
                t(user_id, "calc_result", sats=f"{sats:,}",
                  btc=f"{btc:.8f}", fiat=fiat_line),
                buttons=[
                    [Button.inline(t(user_id, "btn_calc_another"), b"calc")],
                    [Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]
                ])
            return

        # Handle scan payment input
        if current_step == "scan_payment_input":
            payment_id = text.strip()

            # Clean up the message
            last_message_id = user_data.get(user_id, {}).get('last_message_id')
            if last_message_id:
                try:
                    await client.edit_message(user_id, last_message_id, t(user_id, "scan_looking_up"))
                except:
                    pass
            await event.delete()
            payment = await get_payment_by_id(user_id, payment_id)

            del user_steps[user_id]
            if user_id in user_data:
                del user_data[user_id]

            if payment:
                details_text = await format_payment_details(payment, payment_id, user_id=user_id)

                if last_message_id:
                    try:
                        await client.edit_message(
                            user_id,
                            last_message_id,
                            details_text,
                            buttons=[
                                [Button.inline(t(user_id, "btn_scan_another"), b"scan_payment")],
                                [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                            ]
                        )
                    except:
                        await event.respond(
                            details_text,
                            buttons=[
                                [Button.inline(t(user_id, "btn_scan_another"), b"scan_payment")],
                                [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                            ]
                        )
                else:
                    await event.respond(
                        details_text,
                        buttons=[
                            [Button.inline(t(user_id, "btn_scan_another"), b"scan_payment")],
                            [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                        ]
                    )
            else:
                error_msg = t(user_id, "scan_not_found", pid=payment_id)
                if last_message_id:
                    try:
                        await client.edit_message(
                            user_id,
                            last_message_id,
                            error_msg,
                            buttons=[
                                [Button.inline(t(user_id, "btn_try_again"), b"scan_payment")],
                                [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                            ]
                        )
                    except:
                        await event.respond(
                            error_msg,
                            buttons=[
                                [Button.inline(t(user_id, "btn_try_again"), b"scan_payment")],
                                [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                            ]
                        )
                else:
                    await event.respond(
                        error_msg,
                        buttons=[
                            [Button.inline(t(user_id, "btn_try_again"), b"scan_payment")],
                            [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                        ]
                    )
            return

        # Handle custom tip amount input
        elif current_step == "tip_custom_amount":
            amount, is_usd, usd_value = await parse_amount_input(text)
            if amount is None or amount <= 0:
                await event.respond(t(user_id, "tip_invalid_amount"))
                return
            DONATE_USER_ID = int(os.getenv('DONATE_USER_ID', '0'))
            try:
                msg = await event.respond(t(user_id, "tip_sending_toast"))
                await init(DONATE_USER_ID)
                donate_invoice = await create_invoice(DONATE_USER_ID, amount_sats=amount, description="Zap tip - thank you!")
                prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, donate_invoice, amount)
                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
                payment = await send_payment(user_id, prepare_response, options)
                is_success, failure_reason = is_payment_successful(payment)
                if user_id in user_steps:
                    del user_steps[user_id]
                if is_success:
                    if user_id in user_data:
                        user_data[user_id]['just_donated'] = True
                    await client.edit_message(user_id, msg.id,
                        t(user_id, "tip_sent_amount", amount=await format_amount_with_usd(amount, user_id)),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
                else:
                    await client.edit_message(user_id, msg.id,
                        t(user_id, "tip_failed_reason", reason=failure_reason),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
            except Exception as error:
                if user_id in user_steps:
                    del user_steps[user_id]
                await event.respond(t(user_id, "tip_failed_reason", reason=error))
            return

        # Handle donation amount input
        elif current_step == "donate_amount":
            DONATE_USER_ID = donate_user_id()

            # Parse amount (sats or USD)
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                await event.respond(t(user_id, "donate_amount_invalid"))
                return

            if amount <= 0:
                await event.respond(t(user_id, "amount_gt0"))
                return

            try:
                last_message_id = user_data.get(user_id, {}).get('last_message_id')

                if last_message_id:
                    try:
                        await client.edit_message(user_id, last_message_id, t(user_id, "processing_donation"))
                    except:
                        msg = await event.respond(t(user_id, "processing_donation"))
                        last_message_id = msg.id
                else:
                    msg = await event.respond(t(user_id, "processing_donation"))
                    last_message_id = msg.id

                await init(DONATE_USER_ID)

                donate_invoice = await create_invoice(DONATE_USER_ID, amount_sats=amount, description="Donation - Thank you!")
                prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, donate_invoice, amount)

                if spark_fee is not None:
                    final_fee = spark_fee
                else:
                    final_fee = fee_sats if fee_sats else 0

                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=PAYMENT_COMPLETION_TIMEOUT_SECS)
                payment = await send_payment(user_id, prepare_response, options)

                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]

                try:
                    await client.delete_messages(user_id, last_message_id)
                except:
                    pass

                # Format amount display
                amount_display = await format_amount_with_usd(amount, user_id)
                fee_display = await format_amount_with_usd(final_fee, user_id)

                # Check if payment is truly successful
                is_success, failure_reason = is_payment_successful(payment)

                if is_success:
                    if user_id in user_data:
                        user_data[user_id]['just_donated'] = True
                    last_tip_prompt[user_id] = datetime.now()
                    await client.send_message(
                        user_id,
                        _donation_success_text(amount_display, fee_display, user_id=user_id),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                    )
                else:
                    if failure_reason == 'failed':
                        await client.send_message(
                            user_id,
                            t(user_id, 'payment_failed') + t(user_id, "amount_line", amount=amount_display),
                            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                        )
                        return
                    # pending/unknown: neutral processing text + read-only
                    # background recheck editing this message (no retry, no refund)
                    hold_msg = await client.send_message(
                        user_id,
                        t(user_id, 'payment_processing_status') + t(user_id, "amount_line", amount=amount_display)
                    )
                    _spawn_payment_recheck(
                        user_id=user_id, chat_id=user_id, message_id=hold_msg.id,
                        payment_id=getattr(payment, 'id', None),
                        success_text=_donation_success_text(amount_display, fee_display, user_id=user_id),
                        success_buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
                    return

                try:
                    await client.send_message(
                        DONATE_USER_ID,
                        t(user_id, "donation_notify",
                          amount=amount_display, uid=user_id,
                          tx=getattr(payment, 'id', 'N/A'))
                    )
                except:
                    pass

            except Exception as error:
                try:
                    last_message_id = user_data.get(user_id, {}).get('last_message_id')
                    if last_message_id:
                        try:
                            await client.delete_messages(user_id, last_message_id)
                        except:
                            pass

                        await client.send_message(
                            user_id,
                            t(user_id, "donate_failed", error=str(error)),
                            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            t(user_id, "donate_failed", error=str(error)),
                            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                        )
                except:
                    await event.respond(
                        t(user_id, "donate_failed", error=str(error)),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                    )

                if user_id in user_steps:
                    del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]
            return

        # Unclaimed deposit - custom fee input
        elif current_step == "ucl_custom_fee":
            try:
                fee_rate = int(text.strip())
                if fee_rate < 1:
                    await event.respond(t(user_id, "fee_min_1"), buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]])
                    return

                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')
                user_steps.pop(user_id, None)

                if not txid:
                    await event.respond(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])
                    return

                await event.respond(t(user_id, "claiming_deposit"))
                sdk = await get_wallet(user_id)
                fee = MaxFee(onchain=MaxOnchainFee.RATE(sat_per_vbyte=fee_rate))
                request = ClaimDepositRequest(txid=txid, vout=vout, max_fee=fee)
                result = await sdk.claim_deposit(request)
                outcome = result.outcome
                if outcome.is_SETTLED():
                    payment = outcome.payment
                    amount_display = f"{payment.amount_sats:,} {t(user_id, 'sats_unit')}" if hasattr(payment, 'amount_sats') else "✅"
                    msg = t(user_id, "claim_settled", amount=amount_display)
                elif outcome.is_SUBMITTED():
                    msg = t(user_id, "claim_submitted")
                elif outcome.is_DEFERRED():
                    msg = t(user_id, "claim_deferred", reason=outcome.reason)
                else:
                    msg = t(user_id, "claim_processed")
                await event.respond(
                    msg,
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                )
            except ValueError:
                await event.respond(t(user_id, "fee_invalid_number"), buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]])
            except Exception as e:
                user_steps.pop(user_id, None)
                logging.error(f"Error claiming with custom fee: {e}")
                await event.respond(t(user_id, "claim_failed", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_try_again"), b"unclaimed_deposits")]])

        # Unclaimed deposit - refund address input
        elif current_step == "ucl_refund_address":
            address = text.strip()
            ud = user_data.setdefault(user_id, {})
            ud['refund_address'] = address
            user_steps[user_id] = "ucl_refund_fee"
            await event.respond(
                t(user_id, "refund_to_prompt", addr=address),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]]
            )

        # Unclaimed deposit - refund fee input
        elif current_step == "ucl_refund_fee":
            try:
                fee_rate = int(text.strip())
                if fee_rate < 1:
                    await event.respond(t(user_id, "fee_min_1"), buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]])
                    return

                ud = user_data.get(user_id, {})
                txid = ud.get('refund_txid')
                vout = ud.get('refund_vout')
                address = ud.get('refund_address')
                user_steps.pop(user_id, None)

                if not txid or not address:
                    await event.respond(t(user_id, "session_expired"), buttons=[[Button.inline(t(user_id, "btn_back"), b"unclaimed_deposits")]])
                    return

                await event.respond(t(user_id, "refunding_deposit"))
                sdk = await get_wallet(user_id)
                fee = Fee.RATE(sat_per_vbyte=fee_rate)
                request = RefundDepositRequest(txid=txid, vout=vout, destination_address=address, fee=fee)
                result = await sdk.refund_deposit(request)

                await event.respond(
                    t(user_id, "refund_sent", tx=result.tx_id, addr=address),
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]]
                )
            except ValueError:
                await event.respond(t(user_id, "fee_invalid_number"), buttons=[[Button.inline(t(user_id, "btn_cancel"), b"unclaimed_deposits")]])
            except Exception as e:
                user_steps.pop(user_id, None)
                logging.error(f"Error refunding deposit: {e}")
                await event.respond(t(user_id, "refund_failed", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_try_again"), b"unclaimed_deposits")]])

        # ==================== SEND PASSWORD INPUT HANDLERS ====================
        elif current_step == "set_send_password":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            if len(password) < 4:
                await event.respond(
                    t(user_id, "pw_too_short"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
                return

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            # Set the password
            set_user_send_password(user_id, password)

            # Ask for hint - try to edit prompt message
            user_steps[user_id] = "set_password_hint_after_password"
            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id, prompt_msg_id,
                        t(user_id, "pw_set_hint_prompt"),
                        buttons=[[Button.inline(t(user_id, "btn_skip"), b"skip_password_hint")]]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    t(user_id, "pw_set_hint_prompt"),
                    buttons=[[Button.inline(t(user_id, "btn_skip"), b"skip_password_hint")]]
                )

        elif current_step == "set_password_hint_after_password":
            hint = text.strip()

            # Delete the hint message (both sides)
            await delete_password_message(event)

            if hint and len(hint) > 0:
                set_password_hint(user_id, hint)

            del user_steps[user_id]

            hint_msg = t(user_id, "pw_hint_done_line", hint=hint) if hint else ""
            await client.send_message(
                event.chat_id,
                t(user_id, "pw_setup_complete_hint", hint_msg=hint_msg),
                buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
            )

        elif current_step == "set_password_hint":
            hint = text.strip()

            # Delete the hint message (both sides)
            await delete_password_message(event)

            if hint and len(hint) > 0:
                set_password_hint(user_id, hint)
                msg = t(user_id, "hint_updated", hint=hint)
            else:
                remove_password_hint(user_id)
                msg = t(user_id, "hint_removed")

            del user_steps[user_id]

            await client.send_message(
                event.chat_id,
                msg,
                buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
            )

        elif current_step == "verify_password_for_backup":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            # Delete the password message for security (both sides)
            await delete_password_message(event)

            if not verify_send_password(user_id, password):
                hint = get_password_hint(user_id)
                hint_text = f"\n\n💡 Hint: {hint}" if hint else ""
                # Try to edit the prompt message
                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id,
                            prompt_msg_id,
                            t(user_id, "pw_incorrect", hint=hint_text),
                            buttons=[
                                [Button.inline(t(user_id, "btn_try_again"), b"backup")],
                                [Button.inline(t(user_id, "btn_cancel"), b"settings_back")]
                            ]
                        )
                    else:
                        raise Exception("No prompt message")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "pw_incorrect", hint=hint_text),
                        buttons=[
                            [Button.inline(t(user_id, "btn_try_again"), b"backup")],
                            [Button.inline(t(user_id, "btn_cancel"), b"settings_back")]
                        ]
                    )
                del user_steps[user_id]
                return

            # Password correct - show seed
            del user_steps[user_id]
            encrypted_seed = db.get(f'{user_id}')
            seed = decrypt_seed(encrypted_seed)
            # Try to edit the prompt message
            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id,
                        prompt_msg_id,
                        t(user_id, "seed_reveal", seed=seed),
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                    )
                else:
                    raise Exception("No prompt message")
            except:
                await client.send_message(
                    event.chat_id,
                    t(user_id, "seed_reveal", seed=seed),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
            # Clean up
            if user_id in user_data:
                user_data[user_id].pop('password_prompt_msg_id', None)

        elif current_step == "verify_old_send_password":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            if not verify_send_password(user_id, password):
                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            t(user_id, "pw_incorrect_plain"),
                            buttons=[[Button.inline(t(user_id, "btn_try_again"), b"change_send_password"), Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "pw_incorrect_plain"),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"change_send_password"), Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                    )
                del user_steps[user_id]
                return

            # Password verified, now ask for new password
            user_steps[user_id] = "set_new_send_password"
            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id, prompt_msg_id,
                        t(user_id, "pw_verified_new_prompt"),
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    t(user_id, "pw_verified_new_prompt"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )

        elif current_step == "set_new_send_password":
            password = text.strip()

            if len(password) < 4:
                await event.respond(
                    t(user_id, "pw_too_short"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
                return

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            # Set the new password
            set_user_send_password(user_id, password)
            del user_steps[user_id]

            await client.send_message(
                event.chat_id,
                t(user_id, "pw_changed"),
                buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
            )

        elif current_step == "confirm_remove_send_password":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            if not verify_send_password(user_id, password):
                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            t(user_id, "pw_incorrect_plain"),
                            buttons=[[Button.inline(t(user_id, "btn_try_again"), b"remove_send_password"), Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "pw_incorrect_plain"),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"remove_send_password"), Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                    )
                del user_steps[user_id]
                return

            # Remove the password
            remove_send_password(user_id)
            del user_steps[user_id]

            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id, prompt_msg_id,
                        t(user_id, "pw_removed"),
                        buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    t(user_id, "pw_removed"),
                    buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings_back")]]
                )
            # Clean up
            if user_id in user_data:
                user_data[user_id].pop('password_prompt_msg_id', None)

        elif current_step == "verify_send_password_for_payment":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            if not verify_send_password(user_id, password):
                # Wrong password - cancel payment
                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            t(user_id, "pw_incorrect_pay_cancelled"),
                            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "pw_incorrect_pay_cancelled"),
                        buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                    )
                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]
                return

            # Password verified - proceed with payment
            del user_steps[user_id]

            # Mark password as verified for this session
            if user_id in user_data:
                user_data[user_id]['password_verified'] = True

                # Now show confirmation screen again or trigger payment
                ud = user_data[user_id]
                amount = ud.get('amount', 0)
                fee = ud.get('fee', 0)
                payment_type = ud.get('payment_type', 'bolt11')

                amount_display = await format_amount_with_usd(amount, user_id) if isinstance(amount, int) else str(amount)
                fee_display = await format_amount_with_usd(fee, user_id) if isinstance(fee, int) else str(fee)
                total = (amount if isinstance(amount, int) else 0) + (fee if isinstance(fee, int) else 0)
                total_display = await format_amount_with_usd(total, user_id) if total > 0 else "N/A"

                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            t(user_id, "pw_verified_summary",
                              amount=amount_display, fee=fee_display,
                              total=total_display),
                            buttons=[
                                [
                                    Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                                    Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                                    ]
                            ]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "pw_verified_summary",
                          amount=amount_display, fee=fee_display,
                          total=total_display),
                        buttons=[
                            [
                                Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                                Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                                ]
                        ]
                    )

        elif current_step == "verify_send_password_for_ffio":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            if not verify_send_password(user_id, password):
                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            t(user_id, "pw_incorrect_pay_cancelled"),
                            buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "pw_incorrect_pay_cancelled"),
                        buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                    )
                del user_steps[user_id]
                return

            # Password verified - mark and proceed with ff.io payment
            del user_steps[user_id]

            order_id = user_data.get(user_id, {}).get('pending_ffio_order_id')
            order_info = pending_ffio_orders.get(user_id)

            if not order_info or not order_id or order_info['id'] != order_id:
                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            t(user_id, "ff_order_expired"),
                            buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        t(user_id, "ff_order_expired"),
                        buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]]
                    )
                return

            # Mark password as verified and show confirm button again
            order_info['password_verified'] = True

            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id, prompt_msg_id,
                        t(user_id, "pw_verified_ffio", oid=order_id),
                        buttons=[
                            [Button.inline(t(user_id, "btn_confirm_send"), f"ffio_pay_{order_id}".encode())],
                            [Button.inline(t(user_id, "btn_cancel_x"), b"send_exchange")]
                        ]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    t(user_id, "pw_verified_ffio", oid=order_id),
                    buttons=[
                        [Button.inline(t(user_id, "btn_confirm_send"), f"ffio_pay_{order_id}".encode())],
                        [Button.inline(t(user_id, "btn_cancel_x"), b"send_exchange")]
                    ]
                )

        # Recovery wallet input
        elif current_step == "recovery_input":
            words = text.strip()
            word_list = words.split()

            if len(word_list) != 12:
                await event.respond(
                    t(user_id, "recovery_bad_12"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
                return

            # Validate mnemonic
            try:
                if not mnemo.check(words):
                    await event.respond(
                        t(user_id, "recovery_bad_words"),
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                    )
                    return
            except:
                await event.respond(
                    t(user_id, "recovery_bad_format"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
                return

            # v32 item 7a — verify-first: prove the new phrase actually loads
            # BEFORE any destructive side effect (old wallet dir removal / seed
            # overwrite). A wrong-but-valid 12-word phrase previously meant
            # permanent fund loss.
            verify_sdk = None
            verify_dir = f"./.{user_id}-recovery-verify"
            try:
                shutil.rmtree(verify_dir, ignore_errors=True)
                verify_sdk = await connect(
                    request=ConnectRequest(
                        config=config,
                        seed=Seed.MNEMONIC(mnemonic=words, passphrase=None),
                        storage_dir=verify_dir,
                    )
                )
            except Exception as e:
                logging.error(f"Recovery verification failed for {user_id}: {e}")
                await event.respond(
                    t(user_id, "recovery_bad_words"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
                return
            finally:
                try:
                    if verify_sdk is not None:
                        await verify_sdk.disconnect()
                except Exception:
                    pass
                shutil.rmtree(verify_dir, ignore_errors=True)

            # Disconnect old wallet connection if exists
            if user_id in active_user_connections:
                try:
                    old_conn = active_user_connections.pop(user_id)
                    if old_conn.get('sdk'):
                        await old_conn['sdk'].disconnect()
                except Exception as e:
                    logging.debug(f"Error disconnecting old wallet: {e}")

            # Remove old storage directory
            old_storage = f"./.{user_id}"
            try:
                if os.path.exists(old_storage):
                    shutil.rmtree(old_storage)
            except Exception as e:
                logging.debug(f"Error removing old storage: {e}")

            # Encrypt and save new seed
            encrypted_seed = encrypt_seed(words)
            db.set(f'{user_id}', encrypted_seed)

            del user_steps[user_id]

            # Connect to new wallet and sync
            try:
                sdk = await get_wallet(user_id)
                await sdk.sync_wallet(request=SyncWalletRequest())
            except Exception as e:
                logging.debug(f"Error syncing recovered wallet: {e}")

            await event.respond(
                t(user_id, "recovery_done"),
                buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
            )

        elif current_step == "token_dep_amount":
            text_in = (text or "").strip()
            if not text_in or text_in.startswith("/"):
                return
            try:
                ud = user_data.get(user_id) or {}
                sdk = await get_wallet(user_id)
                amount = zt.parse_human_amount(text_in)
                if amount is None:
                    await event.respond(t(user_id, "token_amount_bad"),
                                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                asset = ud.get("tk_asset", "USDT")
                chain = ud.get("tk_chain", "")
                ud["tk_dep_amount"] = amount
                await event.respond(t(user_id, "token_busy"))
                entry_all = zt.pick_routes(await zt.get_routes(sdk), asset)
                entry = next((r for r in entry_all if r["chain"].lower() == chain.lower()), None)
                if entry is None:
                    await event.respond(t(user_id, "token_no_routes"),
                                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                amount_base = zt.to_base_units(amount, entry["decimals"])
                deposit_addr, fee, _e = await zt.make_deposit(
                    sdk, asset, chain, amount_base)
                user_steps.pop(user_id, None)
                # Show the BARE recipient address in text; the QR keeps the
                # full URI so EVM wallets can auto-fill chain+contract+amount
                bare_addr = zt.clean_deposit_address(deposit_addr)
                fee_h = zt.from_base_units(int(fee or 0), entry["decimals"])
                usd_rate = await get_usd_rate()
                net_dep = max(0.0, amount - fee_h)
                dep_sats = usd_to_sats(net_dep, usd_rate) if usd_rate else None
                fee_sats = max(1, int(dep_sats * TOKEN_SERVICE_FEE_BPS / 10_000)) if dep_sats else None
                cap = t(user_id, "token_recv_ready",
                        asset=asset, chain=chain.upper(),
                        address=bare_addr,
                        amount=zt.fmt_amount(amount),
                        fee=zt.fmt_amount(fee_h),
                        fee_sats=f"{fee_sats:,} {t(user_id, 'sats_unit')}" if fee_sats else "0.5%",
                        sats=f"{dep_sats:,} {t(user_id, 'sats_unit')}" if dep_sats else "—")
                cap += "\n\n" + t(user_id, 'deposit_fee_paused')
                qr_buf = generate_qr_code(deposit_addr)
                await client.send_file(event.chat_id, qr_buf, force_document=False,
                                       caption=cap,
                                       buttons=[[Button.inline(t(user_id, "btn_track"), b"tktrack")],
                                                [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]])
            except Exception as e:
                logging.error(f"token_dep_amount: {e}")
                await event.respond(t(user_id, "err_generic", error=str(e)),
                                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
            return

        elif current_step == "token_wd_address":
            text_in = (text or "").strip()
            if not text_in or text_in.startswith("/"):
                return
            try:
                sdk = await get_wallet(user_id)
                dest = await zt.parse_external_address(sdk, text_in)
                if dest is None:
                    tk_chain = (user_data.get(user_id) or {}).get("tk_chain", "")
                    await event.respond(t(user_id, "token_parse_fail",
                                          chain=tk_chain.upper()),
                                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                ud = user_data.get(user_id) or {}
                ud["tk_address"] = dest["address"]
                ud["tk_dest_details"] = dest
                user_steps[user_id] = "token_wd_amount"
                ud_asset = (user_data.get(user_id) or {}).get("tk_asset", "USDT")
                bal_usdb = await zt.usdb_human(sdk)
                bal_line = zt.format_base_units(await zt.usdb_raw_balance(sdk), 6) + " USDB"
                try:
                    bounds = await zt.route_bounds(sdk, "send", ud_asset,
                                                   ud.get("tk_chain", ""),
                                                   cross_chain_address_details(dest))
                except Exception:
                    bounds = {"min_usd": zt.SEND_MIN_USD, "max_usd": None}
                note_lines = []
                if bounds.get("min_usd"):
                    note_lines.append(t(user_id, "token_wd_min", amount=f"{bounds['min_usd']:g}"))
                bounds_note = ("\n" + "\n".join(note_lines)) if note_lines else ""
                await event.respond(
                    t(user_id, "token_wd_amount",
                      asset=ud_asset,
                      balance=bal_line) + bounds_note,
                    buttons=[[Button.inline(t(user_id, "btn_wd_all"), f"tkwd_max:{ud_asset}"),
                              Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
            except Exception as e:
                logging.error(f"token_wd_address: {e}")
                await event.respond(t(user_id, "err_generic", error=str(e)),
                                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
            return

        elif current_step == "token_wd_amount":
            text_in = (text or "").strip()
            if not text_in or text_in.startswith("/"):
                return
            try:
                ud = user_data.get(user_id) or {}
                sdk = await get_wallet(user_id)
                amount = zt.parse_human_amount(text_in)
                from decimal import Decimal, InvalidOperation
                try:
                    exact = Decimal(text_in.replace(',', '.').replace('٫', '.'))
                    valid_units = exact.is_finite() and exact > 0 and exact * 1_000_000 == (exact * 1_000_000).to_integral_value()
                except InvalidOperation:
                    valid_units = False
                if amount is None or not valid_units:
                    await event.respond(t(user_id, "token_amount_bad"),
                                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                amount = str(exact)
                try:
                    _bounds = await zt.route_bounds(sdk, "send", ud.get("tk_asset", "USDT"),
                                                    ud.get("tk_chain", ""),
                                                    cross_chain_address_details(ud.get('tk_dest_details')))
                    _min_usd = _bounds.get("min_usd")
                    if _min_usd and Decimal(amount) < Decimal(str(_min_usd)):
                        await event.respond(
                            t(user_id, "token_wd_min_err", amount=f"{_min_usd:g}"),
                            buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                        return
                except Exception:
                    pass
                raw_usdb = await zt.usdb_raw_balance(sdk)
                if zt.to_base_units(amount, 6) > raw_usdb:
                    await event.respond(t(user_id, "token_amount_high",
                                          balance=zt.format_base_units(raw_usdb, 6) + ' USDB'),
                                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                asset = ud.get("tk_asset", "USDT")
                chain = ud.get("tk_chain", "")
                addr = ud.get("tk_address", "")
                ud["tk_amount"] = amount
                ud["tk_net_amount"] = amount
                ud["tk_amount_base"] = zt.to_base_units(amount, 6)
                fee_sats = None
                q_fee, q_total, quote = await token_wd_quote_totals(
                    sdk, asset, chain, addr, amount, fee_sats,
                    ud.get("tk_dest_details"))
                if q_fee is None:
                    await event.respond(token_withdraw_notice(user_id, 'failed'),
                                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
                    return
                if quote[6] > await zt.usdb_raw_balance(sdk):
                    await event.respond(t(user_id, "token_amount_high", balance=zt.format_base_units(await zt.usdb_raw_balance(sdk), 6)))
                    return
                total_note = ""
                ud["tk_quote"] = quote
                ud["tk_confirm_nonce"] = secrets.token_hex(12)
                user_steps[user_id] = "token_wd_confirm"
                await event.respond(
                    t(user_id, "token_wd_confirm",
                      asset=asset, amount=zt.format_base_units(quote[8], quote[0][5]), address=addr,
                      chain=chain.upper(), fee="—",
                      svc_fee="0",
                      svc_sats="0", input_amount=zt.format_base_units(quote[6], 6),
                      network_fee=q_fee, transfer_fee=str(quote[10]),
                      net=amount, sats="—") + total_note,
                    buttons=[
                        [Button.inline(t(user_id, "btn_confirm_wd"), f"tkwd_go:{ud['tk_confirm_nonce']}".encode())],
                        [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                    ])
            except Exception as e:
                logging.error(f"token_wd_amount: {e}")
                await event.respond(t(user_id, "err_generic", error=str(e)),
                                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]])
            return

        elif current_step == "exit_address_input":
            addr = text.strip()
            if not addr.startswith(("bc1", "1", "3")):
                await event.respond(t(user_id, "exit_addr_invalid"))
                return
            user_steps.pop(user_id, None)
            await event.respond(t(user_id, "exit_preparing"))
            try:
                sdk = await get_wallet(user_id)
                from breez_sdk_spark import PrepareUnilateralExitRequest, CpfpFundingKind, ExitLeafSelection

                # Quote at multiple fee rates — the docs say an Auto exit at a
                # high rate can be uneconomical or even empty (no leaf worth
                # exiting), so pick the best viable rate instead of hardcoding.
                async def _quote(rate):
                    return await sdk.prepare_unilateral_exit(PrepareUnilateralExitRequest(
                        fee_rate_sat_per_vbyte=rate,
                        funding_kind=CpfpFundingKind.P2WPKH(),
                        destination=addr,
                        selection=ExitLeafSelection.AUTO(),
                    ))

                rates = (20, 10, 5, 2, 1)
                quotes = []
                for rate in rates:
                    try:
                        q = await _quote(rate)
                        q_leaves = getattr(q, 'leaves', []) or []
                        if q_leaves:  # empty leaves = nothing worth exiting at this rate
                            quotes.append((rate, q))
                    except Exception as qe:
                        logging.debug(f"exit quote @{rate} sat/vB failed: {qe}")
                if not quotes:
                    await event.respond(
                        t(user_id, "exit_none_possible"),
                        buttons=[[Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]])
                    return

                # Economy rule from docs: recoverable must exceed total fee.
                # Prefer the HIGHEST rate that stays net-positive (fastest
                # confirm), falling back to lower rates.
                chosen_rate, prep = None, None
                for rate, q in quotes:  # rates are ordered high→low
                    q_recov = int(getattr(q, 'recoverable_value_sat', 0) or 0)
                    q_fee = int(getattr(q, 'total_fee_sat', 0) or 0)
                    if q_recov > q_fee:
                        chosen_rate, prep = rate, q
                        break
                if prep is None and quotes:
                    # Nothing net-positive at any rate — surface best (lowest) anyway with a strong warning
                    chosen_rate, prep = quotes[-1]

                leaves = getattr(prep, 'leaves', []) or []
                n_leaves = len(leaves)
                recov = int(getattr(prep, 'recoverable_value_sat', 0) or 0)
                total_fee = int(getattr(prep, 'total_fee_sat', 0) or 0)
                fanout = int(getattr(prep, 'fanout_fee_sat', 0) or 0)
                utxo_fund = int(getattr(prep, 'single_utxo_funding_sat', 0) or 0)

                # Save quote + leaf ids: docs say persisting leaf ids and
                # re-quoting with ExitLeafSelection::Specific is the reliable
                # resume path for an interrupted exit.
                leaf_ids = [getattr(l, 'node_id', None) or getattr(l, 'leaf_id', None) or getattr(l, 'slug', None) for l in leaves]
                user_data[user_id] = user_data.get(user_id) or {}
                user_data[user_id]['exit_prepared'] = prep
                user_data[user_id]['exit_address'] = addr
                user_data[user_id]['exit_rate'] = chosen_rate
                user_data[user_id]['exit_leaf_ids'] = [x for x in leaf_ids if x]

                # Funding gate: an external on-chain UTXO is ALWAYS required
                # when single_utxo_funding_sat > 0 — and a Spark-only balance
                # (zero unclaimed deposits) cannot satisfy it (verified live).
                can_check_deposits = True
                unclaimed = 0
                try:
                    dep_resp = await sdk.list_unclaimed_deposits(
                        request=__import__('breez_sdk_spark').ListUnclaimedDepositsRequest())
                    unclaimed = len(getattr(dep_resp, 'deposits', []) or [])
                except Exception as de:
                    logging.debug(f"exit deposit check failed: {de}")
                    can_check_deposits = False

                needs_funding = utxo_fund > 0
                funding_blocked = needs_funding and (unclaimed == 0)
                net = recov - total_fee

                lines = [
                    t(user_id, "exit_prepared"),
                    t(user_id, "exit_line_to", addr=addr),
                    t(user_id, "exit_line_leaves", leaves=n_leaves),
                    t(user_id, "exit_line_recoverable", recov=recov),
                    t(user_id, "exit_line_fee", total_fee=total_fee, rate=chosen_rate),
                ]
                if fanout:
                    lines.append(t(user_id, "exit_line_fanout", fanout=fanout))
                if needs_funding:
                    lines.append(t(user_id, "exit_line_funding", utxo_fund=utxo_fund))
                lines.append(t(user_id, "exit_line_net", net=net))
                if net <= 0:
                    lines.append(t(user_id, "exit_not_worth"))

                if funding_blocked:
                    lines.append(t(user_id, "exit_funding_blocked", utxo_fund=utxo_fund))
                    await event.respond("\n".join(lines),
                        buttons=[[Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]])
                    return

                confirm_text = (t(user_id, "exit_sign_no_broadcast") if not needs_funding
                                else t(user_id, "exit_sign_with_utxo", utxo_fund=utxo_fund))
                lines.append(t(user_id, "exit_confirm_signs"))
                if needs_funding:
                    lines.append(t(user_id, "exit_sign_spends", utxo_fund=utxo_fund))
                await event.respond(
                    "\n".join(lines),
                    buttons=[
                        [Button.inline(confirm_text, b"exit_confirm")],
                        [Button.inline(t(user_id, "btn_export_state"), b"exit_export_state")],
                        [Button.inline(t(user_id, "btn_cancel_x"), b"emergency_exit")]
                    ])
            except Exception as e:
                logging.error(f"exit prepare: {e}")
                await event.respond(t(user_id, "exit_prepare_failed", error=e),
                    buttons=[[Button.inline(t(user_id, "btn_exit_menu"), b"emergency_exit")]])
            return

        # v32: webhook url input
        elif current_step == "webhook_url_input":
            await _step_webhook_url(event, user_id, text)
            return

        # v32: webhook delete input
        elif current_step == "webhook_del_input":
            await _step_webhook_del(event, user_id, text)
            return

        # v33 batch: destination input
        elif current_step == "batch_dest":
            dest = text.strip()
            kind_hint = None
            if dest.startswith('@'):
                # Telegram username: resolve to receiver's internal invoice directly
                kind_hint = 'invoice'
                try:
                    invoice, _info = await resolve_username(dest)
                    if not invoice:
                        raise Exception("receiver has no wallet")
                    dest = invoice
                except Exception as e:
                    await event.respond(t(user_id, "resolve_failed", dest=dest, error=e))
                    return
            if kind_hint is None and '@' in dest and '.' in dest and not dest.lower().startswith(('lnbc', 'lnurl')):
                kind_hint = 'lightning-address'
            elif kind_hint is None and dest.startswith(('bc1', '1', '3')):
                kind_hint = 'onchain'
            elif kind_hint is None and dest.lower().startswith(('lnurl', 'lnbc')):
                kind_hint = 'invoice'
            elif kind_hint is None and (dest.startswith('spark:') or dest.startswith('sp1')):
                kind_hint = 'spark'
            if kind_hint is None:
                await event.respond(t(user_id, "batch_unrecognized"))
                return

            # block self-payment early
            try:
                my_addr = await get_lightning_address(user_id)
                my_ln = getattr(my_addr, 'lightning_address', None) if my_addr else None
                if my_ln and my_ln.lower() == dest.lower():
                    await event.respond(t(user_id, "self_pay_blocked"))
                    return
            except Exception:
                pass

            ud = user_data.get(user_id) or {}
            ud['batch_pending_dest'] = dest
            ud['batch_pending_kind'] = kind_hint

            # invoices with embedded amount can skip the amount step? NO — batch items
            # each have their own explicit amount, keep consistent: always ask amount
            user_steps[user_id] = "batch_amount"
            await event.respond(
                t(user_id, "batch_dest_prompt",
                  n=len(ud.get('batch_items', [])) + 1,
                  dest=f"{dest[:30]}…", kind=kind_hint),
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"more_tools")]])
            return

        # v33 batch: amount input → prepare immediately → show running list
        elif current_step == "batch_amount":
            amount_txt = text.strip().lstrip('$').replace(',', '')
            if not amount_txt.isdigit():
                await event.respond(t(user_id, "batch_amount_invalid"))
                return
            sats = int(amount_txt)
            ud = user_data.get(user_id) or {}
            dest = ud.get('batch_pending_dest')
            kind = ud.get('batch_pending_kind')
            if not dest or not kind:
                await event.respond(t(user_id, "batch_session_lost"))
                user_steps.pop(user_id, None)
                return

            status = await event.respond(t(user_id, "preparing_payment"))
            try:
                bolt_opts = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=PAYMENT_COMPLETION_TIMEOUT_SECS)
                fee = 0
                if kind == 'onchain':
                    prep, fee0, _sf = await prepare_payment(user_id, dest, sats)
                    try:
                        fq = prep.payment_method.fee_quote
                        fee = fq.speed_medium.user_fee_sat + fq.speed_medium.l1_broadcast_fee_sat
                    except Exception:
                        fee = int(fee0 or 0)
                    opts = SendPaymentOptions.BITCOIN_ADDRESS(confirmation_speed=OnchainConfirmationSpeed.MEDIUM)
                elif kind == 'lightning-address':
                    # LUD-16: fetch pay params over HTTP, then prepare as LNURL-pay
                    import aiohttp as _ah
                    _u_, _dom = dest.split('@', 1)
                    _url = f"https://{_dom}/.well-known/lnurlp/{_u_}"
                    async with _ah.ClientSession() as _sess:
                        async with _sess.get(_url, timeout=_ah.ClientTimeout(total=15)) as _r:
                            if _r.status != 200:
                                raise Exception(f"lookup failed (HTTP {_r.status})")
                            _j = await _r.json(content_type=None)
                    if _j.get('tag') != 'payRequest':
                        raise Exception("not a payRequest")
                    from breez_sdk_spark import LnurlPayRequestDetails, PrepareLnurlPayRequest
                    _min = int(_j.get('minSendable', 1000)) // MSAT_PER_SAT
                    _max = int(_j.get('maxSendable', 1000000000)) // MSAT_PER_SAT
                    if sats < _min or sats > _max:
                        raise Exception(f"amount must be {_min:,}-{_max:,} sats for {dest}")
                    _det = LnurlPayRequestDetails(
                        callback=_j.get('callback'),
                        min_sendable=int(_j.get('minSendable', 1000)),
                        max_sendable=int(_j.get('maxSendable', 1000000000)),
                        metadata_str=_j.get('metadata', '[["text/plain",""]]'),
                        comment_allowed=int(_j.get('commentAllowed', 0)),
                        domain=_dom,
                        url="", address=dest,
                        allows_nostr=bool(_j.get('allowsNostr', False)),
                        nostr_pubkey=_j.get('nostrPubkey') or '',
                    )
                    sdk = await get_wallet(user_id)
                    prep = await sdk.prepare_lnurl_pay(PrepareLnurlPayRequest(
                        amount=sats, pay_request=_det,
                        comment=None, validate_success_action_url=True))
                    fee = int(getattr(prep, 'fee_sats', 0) or 0)
                    opts = bolt_opts
                elif kind == 'invoice':
                    prep, fee, _sf = await prepare_payment(user_id, dest, sats)
                    opts = bolt_opts
                elif kind == 'lnurl-pay':
                    sdk = await get_wallet(user_id)
                    parsed = await parse_input(sdk, dest)
                    details = parsed[0]
                    prep = await sdk.prepare_lnurl_pay(PrepareLnurlPayRequest(
                        amount=sats, pay_request=details,
                        comment=None, validate_success_action_url=True))
                    fee = int(getattr(prep, 'fee_sats', 0) or 0)
                    opts = bolt_opts
                elif kind == 'spark':
                    prep, fee, _sf = await prepare_payment(user_id, dest, sats)
                    opts = None
                else:
                    raise Exception("unknown type")

                # Bind destination + amount + route fingerprint durably at confirmation.
                if not ud.get('batch_id'):
                    ud['batch_id'] = 'b-' + secrets.token_hex(8)
                fingerprint = _batch_fingerprint(kind, sats, dest)
                row_id = _batch_add(ud['batch_id'], user_id, dest, sats, fingerprint)
                entry = {'dest': dest, 'amount': sats, 'fee': int(fee or 0), 'kind': kind,
                         'prepare_response': prep, 'options': opts,
                         'row_id': row_id, 'fingerprint': fingerprint}
                ud.pop('batch_pending_dest', None)
                ud.pop('batch_pending_kind', None)
                ud.setdefault('batch_items', []).append(entry)
                ud.setdefault('batch_pairs', []).append((dest, sats, kind))
                items = ud['batch_items']
                total = sum(int(e.get('amount') or 0) for e in items)
                fee_total = sum(int(e.get('fee') or 0) for e in items)

                lines = [t(user_id, "batch_added_line", n=len(items))]
                for i, e in enumerate(items, 1):
                    d = e['dest'] if len(e['dest']) <= 30 else e['dest'][:29] + "…"
                    lines.append(t(user_id, "batch_item_line", i=i, dest=d,
                                   amount=f"{e['amount']:,}", fee=f"{e['fee']:,}"))
                lines.append(t(user_id, "batch_totals_line", total=f"{total:,}",
                               fees=f"{fee_total:,}", grand=f"{total + fee_total:,}"))
                lines.append(t(user_id, "batch_next_hint"))
                user_steps.pop(user_id, None)
                try: await status.delete()
                except Exception: pass
                await event.respond("\n".join(lines), buttons=[
                    [Button.inline(t(user_id, "btn_send_all_n", n=len(items)), b"batch_send_all")],
                    [Button.inline(t(user_id, "btn_add_another"), b"batch_add_more")],
                    [Button.inline(t(user_id, "btn_cancel_all"), b"queue_clear")]
                ])
            except Exception as e:
                import traceback as _tb
                logging.error(f"batch prepare: {e}\n{_tb.format_exc()}")
                await event.respond(t(user_id, "batch_prep_err", error=str(e)),
                                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"more_tools")]])
            return

        # v31: LNURL-Withdraw amount input
        elif current_step == "lnurl_withdraw_amount":
            try:
                amt = int(re.sub(r'[^0-9]', '', text))
            except ValueError:
                await event.respond(t(user_id, "lnurl_wd_number"))
                return
            wd = (user_data.get(user_id) or {}).get('withdraw_request')
            if not wd:
                await event.respond(t(user_id, "lnurl_wd_expired"))
                user_steps.pop(user_id, None)
                return
            if amt < wd.min_withdrawable // MSAT_PER_SAT or amt > wd.max_withdrawable // MSAT_PER_SAT:
                await event.respond(
                    t(user_id, "lnurl_wd_range",
                      mn=f"{wd.min_withdrawable // MSAT_PER_SAT:,}",
                      mx=f"{wd.max_withdrawable // MSAT_PER_SAT:,}"))
                return
            user_steps.pop(user_id, None)
            user_data.pop(user_id, None)
            await event.respond(t(user_id, "claiming_generic"))
            try:
                sdk = await get_wallet(user_id)
                from breez_sdk_spark import LnurlWithdrawRequest, LnurlWithdrawRequestDetails
                details_req = LnurlWithdrawRequestDetails(
                    callback=wd.callback,
                    k1=wd.k1,
                    default_description=getattr(wd, 'default_description', '') or 'Zap withdraw',
                    min_withdrawable=wd.min_withdrawable,
                    max_withdrawable=wd.max_withdrawable,
                )
                resp = await sdk.lnurl_withdraw(LnurlWithdrawRequest(
                    amount_sats=amt,
                    withdraw_request=details_req,
                    completion_timeout_secs=20,
                ))
                await event.respond(
                    t(user_id, "lnurl_wd_sent", amt=f"{amt:,}"))
            except Exception as e:
                logging.error(f"lnurl_withdraw error: {e}")
                await event.respond(t(user_id, "lnurl_wd_failed", error=str(e)))
            return

        # Change Lightning address input
        elif current_step == "change_ln_address_input":
            username = text.strip().lower()

            # Validate username format
            if not username.replace('_', '').isalnum():
                await event.respond(
                    t(user_id, "lnaddr_username_invalid"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )
                return

            try:
                sdk = await get_wallet(user_id)
                request = RegisterLightningAddressRequest(username=username, description="Zap Zap")
                new_address = await sdk.register_lightning_address(request=request)

                del user_steps[user_id]

                await event.respond(
                    t(user_id, "lnaddr_updated",
                      addr=new_address.lightning_address,
                      lnurl=new_address.lnurl),
                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(
                    t(user_id, "lnaddr_update_err", error=str(error)),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"settings_back")]]
                )

        # Receive memo input
        # ==================== FF.IO EXCHANGE TEXT INPUT HANDLERS ====================
        elif current_step == "ffio_receive_amount":
            try:
                amount_str = text.strip()
                amount = float(amount_str)
                if amount <= 0:
                    raise ValueError("Amount must be positive")

                ud = user_data.get(user_id, {})
                from_ccy = ud.get('ffio_from_ccy', '?')
                ud['ffio_amount'] = amount

                # Get price quote
                try:
                    rate_type = ud.get('ffio_rate_type', 'float')
                    price_result = await ffio_get_price(from_ccy, "BTCLN", amount, rate_type=rate_type)
                    if price_result and price_result.get('code') == 0 and price_result.get('data'):
                        price_data = price_result['data']
                        from_data = price_data.get('from', {})
                        to_data = price_data.get('to', {})

                        # Check min/max limits
                        min_val = from_data.get('min')
                        max_val = from_data.get('max')
                        if min_val and float(min_val) > 0 and amount < float(min_val):
                            await event.reply(
                                t(user_id, "ff_amount_too_low",
                                  min=f"{min_val} {from_ccy}", amount=f"{amount} {from_ccy}"),
                                buttons=[[Button.inline(t(user_id, "btn_try_again"), b"receive_exchange")]]
                            )
                            return
                        if max_val and float(max_val) > 0 and amount > float(max_val):
                            await event.reply(
                                t(user_id, "ff_amount_too_high",
                                  max=f"{max_val} {from_ccy}", amount=f"{amount} {from_ccy}"),
                                buttons=[[Button.inline(t(user_id, "btn_try_again"), b"receive_exchange")]]
                            )
                            return

                        receive_btc_raw = to_data.get('amount', '?')
                        rate = to_data.get('rate', '?')
                        # Format BTC properly (no scientific notation) and convert to sats
                        try:
                            btc_val = float(receive_btc_raw)
                            receive_btc = f"{btc_val:.8f}".rstrip('0').rstrip('.')
                            receive_sats = int(btc_val * SATS_PER_BTC)
                            sats_display = f" ({receive_sats:,} {t(user_id, 'sats_unit')})"
                        except:
                            receive_btc = str(receive_btc_raw)
                            sats_display = ""

                        min_max_note = ""
                        if min_val or max_val:
                            min_max_note = t(user_id, "ff_limits_line", mn=min_val, mx=max_val, ccy=from_ccy)

                        await event.reply(
                            t(user_id, "ff_quote_recv", amount=amount, ccy=from_ccy,
                              btc=receive_btc, sats=sats_display, rate=rate,
                              limits=min_max_note),
                            buttons=[
                                [Button.inline(t(user_id, "btn_create_order"), b"rcv_ex_confirm_yes")],
                                [Button.inline(t(user_id, "btn_cancel_x"), b"receive_exchange")]
                            ]
                        )
                    else:
                        error_msg = price_result.get('msg', 'Unknown error') if price_result else 'API error'
                        # Log the full response for debugging
                        logging.warning(f"ff.io quote error for {from_ccy}: {price_result}")
                        await event.reply(
                            t(user_id, "ff_quote_err_detail", ccy=from_ccy, error=error_msg),
                            buttons=[[Button.inline(t(user_id, "btn_try_coin"), b"receive_exchange")]]
                        )
                        user_steps.pop(user_id, None)
                except Exception as e:
                    logging.error(f"ff.io quote exception for {from_ccy}: {e}")
                    await event.reply(t(user_id, "ff_quote_get_err", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"receive_exchange")]])
                    user_steps.pop(user_id, None)
            except ValueError:
                await event.reply(
                    t(user_id, "ff_invalid_amount"),
                    buttons=[[Button.inline(t(user_id, "btn_cancel"), b"receive_exchange")]]
                )

        elif current_step == "ffio_send_address":
            address = text.strip()
            if len(address) < 10:
                await event.reply(t(user_id, "ff_invalid_address"))
                return

            ud = user_data.get(user_id, {})
            ud['ffio_to_address'] = address

            # Check if tag/memo needed
            tag_type = ud.get('ffio_tag_type')
            if tag_type:
                user_steps[user_id] = "ffio_send_tag"
                await event.reply(
                    t(user_id, "ff_tag_prompt", tag=tag_type, addr=f"{address[:30]}..."),
                    buttons=[[Button.inline(t(user_id, "btn_skip"), b"ffio_skip_tag"), Button.inline(t(user_id, "btn_cancel"), b"send_exchange")]]
                )
                return

            user_steps[user_id] = "ffio_send_amount"
            to_name = ud.get('ffio_to_name', ud.get('ffio_to_ccy', '?'))
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance, user_id)

            min_sats = ud.get('ffio_min_sats', '')
            max_sats = ud.get('ffio_max_sats', '')
            limit_text = ""
            if min_sats or max_sats:
                limit_text = t(user_id, "ff_sats_limit_line", mn=f"{min_sats:,}", mx=f"{max_sats:,}")

            await event.reply(
                t(user_id, "ff_amount_prompt_send", name=to_name,
                  balance=balance_display, addr=f"{address[:30]}...",
                  limits=limit_text),
                buttons=[
                    [Button.inline(t(user_id, "btn_send_all"), b"ffio_send_all")],
                    [Button.inline(t(user_id, "btn_cancel"), b"send_exchange")]
                ]
            )

        elif current_step == "ffio_send_tag":
            ud = user_data.get(user_id, {})
            tag_val = text.strip()
            if tag_val.lower() != 'skip':
                ud['ffio_to_tag'] = tag_val

            user_steps[user_id] = "ffio_send_amount"
            to_name = ud.get('ffio_to_name', ud.get('ffio_to_ccy', '?'))
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance, user_id)

            min_sats = ud.get('ffio_min_sats', '')
            max_sats = ud.get('ffio_max_sats', '')
            limit_text = ""
            if min_sats or max_sats:
                limit_text = t(user_id, "ff_sats_limit_line", mn=f"{min_sats:,}", mx=f"{max_sats:,}")

            await event.reply(
                t(user_id, "ff_amount_prompt_send", name=to_name,
                  balance=balance_display,
                  addr=f"{ud.get('ffio_to_address', '?')[:30]}...",
                  limits=limit_text),
                buttons=[
                    [Button.inline(t(user_id, "btn_send_all"), b"ffio_send_all")],
                    [Button.inline(t(user_id, "btn_cancel"), b"send_exchange")]
                ]
            )

        elif current_step == "ffio_send_amount":
            try:
                ud = user_data.get(user_id, {})
                to_ccy = ud.get('ffio_to_ccy', '?')

                # Parse amount (returns tuple: amount_sats, is_usd, original_usd)
                usd_rate = await get_usd_rate()
                parsed = await parse_amount_input(text.strip(), usd_rate)
                amount_sats = parsed[0] if parsed else None

                if not amount_sats or amount_sats <= 0:
                    raise ValueError("Invalid amount")

                balance = await get_balance(user_id)
                if amount_sats > int(balance):
                    await event.reply(t(user_id, "insuff_generic"), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])
                    return

                # Check min/max from stored values
                min_sats = ud.get('ffio_min_sats')
                max_sats = ud.get('ffio_max_sats')
                if min_sats and amount_sats < min_sats:
                    await event.reply(
                        t(user_id, "ff_amount_too_low_sats",
                          min=f"{min_sats:,}", amount=f"{amount_sats:,}"),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"send_exchange")]]
                    )
                    return
                if max_sats and amount_sats > max_sats:
                    await event.reply(
                        t(user_id, "ff_amount_too_high_sats",
                          max=f"{max_sats:,}", amount=f"{amount_sats:,}"),
                        buttons=[[Button.inline(t(user_id, "btn_try_again"), b"send_exchange")]]
                    )
                    return

                ud['ffio_amount_sats'] = amount_sats
                amount_btc = amount_sats / SATS_PER_BTC  # pinned semantics: float BTC to ff.io

                # Get price quote
                try:
                    rate_type = ud.get('ffio_rate_type', 'float')
                    price_result = await ffio_get_price("BTCLN", to_ccy, amount_btc, rate_type=rate_type)
                    if price_result and price_result.get('code') == 0 and price_result.get('data'):
                        price_data = price_result['data']
                        from_data = price_data.get('from', {})
                        receive_amount = price_data.get('to', {}).get('amount', '?')
                        rate = price_data.get('to', {}).get('rate', '?')

                        # Double-check min/max from API response
                        api_min = from_data.get('min')
                        api_max = from_data.get('max')
                        if api_min and float(api_min) > 0 and amount_btc < float(api_min):
                            min_s = int(float(api_min) * SATS_PER_BTC)
                            await event.reply(
                                t(user_id, "ff_amount_too_low_api",
                                  min=f"{min_s:,}", amount=f"{amount_sats:,}"),
                                buttons=[[Button.inline(t(user_id, "btn_try_again"), b"send_exchange")]]
                            )
                            return
                        if api_max and float(api_max) > 0 and amount_btc > float(api_max):
                            max_s = int(float(api_max) * SATS_PER_BTC)
                            await event.reply(
                                t(user_id, "ff_amount_too_high_api",
                                  max=f"{max_s:,}", amount=f"{amount_sats:,}"),
                                buttons=[[Button.inline(t(user_id, "btn_try_again"), b"send_exchange")]]
                            )
                            return

                        amount_display = await format_amount_with_usd(amount_sats, user_id)
                        min_max_note = ""
                        if api_min or api_max:
                            try:
                                mn = f"{int(float(api_min)*SATS_PER_BTC):,}" if api_min else "?"
                                mx = f"{int(float(api_max)*SATS_PER_BTC):,}" if api_max else "?"
                                min_max_note = t(user_id, "ff_limits_sats_line", mn=mn, mx=mx)
                            except:
                                pass

                        await event.reply(
                            t(user_id, "ff_quote_send", amount=amount_display,
                              recv=receive_amount, ccy=to_ccy, rate=rate,
                              limits=min_max_note),
                            buttons=[
                                [Button.inline(t(user_id, "btn_create_order"), b"snd_ex_confirm_yes")],
                                [Button.inline(t(user_id, "btn_cancel_x"), b"send_exchange")]
                            ]
                        )
                    else:
                        error_msg = price_result.get('msg', 'Unknown error') if price_result else 'API error'
                        await event.reply(t(user_id, "ff_quote_err_short", error=error_msg), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])
                        user_steps.pop(user_id, None)
                except Exception as e:
                    await event.reply(t(user_id, "err_alert_line", error=str(e)), buttons=[[Button.inline(t(user_id, "btn_back"), b"send_exchange")]])
                    user_steps.pop(user_id, None)
            except ValueError:
                await event.reply(t(user_id, "ff_invalid_amount_sats"))

        elif current_step == "receive_memo":
            memo = text.strip()
            user_data[user_id] = user_data.get(user_id) or {}
            # keep any previously-set amount so it survives the memo update
            pending_amount = user_data[user_id].get('pending_amount')
            user_data[user_id]['memo'] = memo
            user_steps.pop(user_id, None)

            try:
                invoice = await create_invoice(
                    user_id,
                    amount_sats=pending_amount,
                    description=memo
                )

                qr_buffer = generate_qr_code(invoice)
                try:
                    msg = await event.get_message()
                    if msg and msg.media:
                        await msg.delete()
                except:
                    pass
                caption_lines = [
                    t(user_id, 'invoice_title'),
                    f"💰 {t(user_id, 'amount_label')}: {await format_amount_with_usd(pending_amount, user_id)}" if pending_amount else t(user_id, 'invoice_no_amount_title'),
                    f"💬 {t(user_id, 'memo_label')}: {memo}",
                    "",
                    f"`{invoice}`"
                ]
                await client.send_file(
                    event.chat_id,
                    qr_buffer,
                    force_document=False,
                    caption="\n".join(caption_lines),
                    buttons=[
                        [Button.inline(t(user_id, "btn_set_amount"), b"receive_set_amount_no_memo")],
                        [Button.inline(t(user_id, "btn_set_memo"), b"receive_add_memo")],
                        [Button.inline(t(user_id, "btn_back"), b"receive")]
                    ]
                )
            except Exception as error:
                await event.respond(t(user_id, "err_bare", error=str(error)))

        # Receive amount input (with memo)
        elif current_step == "receive_amount":
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                if is_usd:
                    await event.respond(t(user_id, "usd_conv_fail"))
                else:
                    await event.respond(t(user_id, "amount_invalid_plain"))
                return

            if amount <= 0:
                await event.respond(t(user_id, "amount_gt0"))
                return

            memo = user_data.get(user_id, {}).get('memo', 'Zap payment')

            try:
                invoice = await create_invoice(user_id, amount_sats=amount, description=memo)

                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]

                amount_display = await format_amount_with_usd(amount, user_id)

                # Generate QR code for invoice
                qr_buffer = generate_qr_code(invoice)

                await client.send_file(
                    event.chat_id,
                    qr_buffer,
                    force_document=False,
                    caption=t(user_id, "invoice_caption",
                               amount=amount_display, memo=memo, invoice=invoice),
                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(t(user_id, "err_alert_line", error=str(error)))

        # Receive amount input (without memo)
        elif current_step == "receive_amount_no_memo":
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                if is_usd:
                    await event.respond(t(user_id, "usd_conv_fail"))
                else:
                    await event.respond(t(user_id, "amount_invalid_plain"))
                return

            if amount <= 0:
                await event.respond(t(user_id, "amount_gt0"))
                return

            try:
                # Preserve any memo the user set earlier (kept in user_data)
                memo = user_data.get(user_id, {}).get('pending_memo') or user_data.get(user_id, {}).get('memo')
                invoice = await create_invoice(user_id, amount_sats=amount, description=memo or "Zap payment")

                user_steps.pop(user_id, None)
                if user_id in user_data:
                    # keep memo in user_data so later Set Amount can reuse it
                    user_data[user_id]['pending_amount'] = amount
                    if memo:
                        user_data[user_id]['memo'] = memo
                    else:
                        user_data[user_id].pop('pending_memo', None)

                amount_display = await format_amount_with_usd(amount, user_id)

                # Generate QR code for invoice
                qr_buffer = generate_qr_code(invoice)

                caption_lines = [t(user_id, 'invoice_title')]
                caption_lines.append(f"💰 {t(user_id, 'amount_label')}: {amount_display}")
                if memo:
                    caption_lines.append(f"💬 {t(user_id, 'memo_label')}: {memo}")
                caption_lines.append("")
                caption_lines.append(f"`{invoice}`")

                await client.send_file(
                    event.chat_id,
                    qr_buffer,
                    force_document=False,
                    caption="\n".join(caption_lines),
                    buttons=[
                        [Button.inline(t(user_id, "btn_set_amount"), b"receive_set_amount_no_memo")],
                        [Button.inline(t(user_id, "btn_set_memo"), b"receive_add_memo")],
                        [Button.inline(t(user_id, "btn_back"), b"receive")]
                    ]
                )
            except Exception as error:
                await event.respond(t(user_id, "err_bare", error=str(error)))

        # ==================== SEND: Parse Invoice ====================
        elif current_step == "send_invoice":
            try:
                # Check if it's a username (starts with @)
                processing_msg = None
                resolved_user_info = None
                if text.startswith('@'):
                    processing_msg = await event.respond(
                        t(user_id, "resolving_user", txt=text)
                    )
                    lightning_address, resolved_user_info = await resolve_username(text)
                    text = lightning_address
                sdk = await get_wallet(user_id)
                parsed = await parse_input(sdk, text)
                input_type = parsed
                details = parsed[0]

                invoice_text = text
                needs_amount = False
                parsed_amount = None
                input_type_str = "Unknown"

                # BOLT11 Invoice
                if isinstance(input_type, InputType.BOLT11_INVOICE):
                    input_type_str = "BOLT11 Invoice"
                    if hasattr(details, 'amount_msat') and details.amount_msat:
                        parsed_amount = details.amount_msat // MSAT_PER_SAT
                    else:
                        needs_amount = True

                # Lightning Address (LNURL-Pay)
                elif isinstance(input_type, InputType.LIGHTNING_ADDRESS):
                    input_type_str = "Lightning Address"
                    pay_request = details.pay_request
                    min_sats = pay_request.min_sendable // MSAT_PER_SAT
                    max_sats = pay_request.max_sendable // MSAT_PER_SAT

                    user_data[user_id] = {
                        'pay_request': pay_request,
                        'payment_type': 'lnurl',
                        'min_sats': min_sats,
                        'max_sats': max_sats
                    }
                    needs_amount = True

                # LNURL-Pay
                elif isinstance(input_type, InputType.LNURL_PAY):
                    input_type_str = "LNURL-Pay"
                    min_sats = details.min_sendable // MSAT_PER_SAT
                    max_sats = details.max_sendable // MSAT_PER_SAT
                    user_data[user_id] = {
                        'pay_request': details,
                        'payment_type': 'lnurl',
                        'min_sats': min_sats,
                        'max_sats': max_sats
                    }
                    needs_amount = True

                # Bitcoin Address
                elif isinstance(input_type, InputType.BITCOIN_ADDRESS):
                    input_type_str = "Bitcoin Address"
                    user_data[user_id] = {
                        'address': details.address,
                        'payment_type': 'onchain',
                        'invoice': text
                    }
                    needs_amount = True

                # LNURL-Withdraw
                elif isinstance(input_type, InputType.LNURL_WITHDRAW):
                    # v31: LNURL-Withdraw now works — claim funds straight to wallet
                    input_type_str = "LNURL-Withdraw"
                    min_sats = getattr(details, 'min_withdrawable', 0) // MSAT_PER_SAT
                    max_sats = getattr(details, 'max_withdrawable', 0) // MSAT_PER_SAT
                    user_data[user_id] = {
                        'withdraw_request': details,
                        'payment_type': 'lnurl_withdraw',
                        'min_sats': min_sats,
                        'max_sats': max_sats
                    }
                    if processing_msg:
                        await processing_msg.edit(
                            t(user_id, "lnurl_wd_detected",
                              mn=f"{min_sats:,}", mx=f"{max_sats:,}"),
                            buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            t(user_id, "lnurl_wd_detected",
                              mn=f"{min_sats:,}", mx=f"{max_sats:,}"),
                            buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                        )
                    user_steps[user_id] = "lnurl_withdraw_amount"
                    return

                # LNURL-Auth (v31.1): passwordless sign-in via domain key
                elif isinstance(input_type, InputType.LNURL_AUTH):
                    input_type_str = "LNURL-Auth"
                    domain = getattr(details, 'domain', '?')
                    action = getattr(details, 'action', None) or 'auth'
                    user_data[user_id] = {
                        'lnurl_auth_request': details,
                        'payment_type': 'lnurl_auth'
                    }
                    msg = t(user_id, "lnurl_auth_card",
                            domain=domain, action=action)
                    auth_btns = [[Button.inline(t(user_id, "btn_confirm"), b"lnurlauth_ok"),
                                  Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                    if processing_msg:
                        await processing_msg.edit(msg, buttons=auth_btns)
                    else:
                        await event.respond(msg, buttons=auth_btns)
                    return

                # Store invoice data if not LNURL
                if 'payment_type' not in user_data.get(user_id, {}):
                    user_data[user_id] = {
                        'invoice': invoice_text,
                        'needs_amount': needs_amount,
                        'parsed_amount': parsed_amount,
                        'input_type': input_type_str,
                        'payment_type': 'bolt11'
                    }

                # Store resolved user info if available
                if resolved_user_info:
                    user_data[user_id]['resolved_user'] = resolved_user_info

                # If needs amount, ask for it
                if needs_amount:
                    user_steps[user_id] = "send_amount"

                    # Get USD rate for hint
                    usd_rate = await get_usd_rate()
                    hint = ""
                    if usd_rate:
                        sats_per_dollar = usd_to_sats(1, usd_rate)
                        hint = t(user_id, "usd_hint_approx", one=f"{sats_per_dollar:,}")

                    # Quick amount buttons + withdraw all
                    buttons = [
                        [
                            Button.inline("1K", b"send_quick_1000"),
                            Button.inline("5K", b"send_quick_5000"),
                            Button.inline("10K", b"send_quick_10000"),
                            Button.inline("50K", b"send_quick_50000")
                        ],
                        [Button.inline(t(user_id, "btn_send_all"), b"send_withdraw_all")],
                        [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                    ]

                    # Build resolved info text
                    resolved_text = ""
                    tips_text = ""
                    if resolved_user_info:
                        ru = resolved_user_info
                        has_started = ru.get('has_started_bot', False)

                        # Show bot status
                        if has_started:
                            bot_status = t(user_id, "bot_started")
                        else:
                            bot_status = t(user_id, "bot_not_started")

                        resolved_text = t(
                            user_id, "send_to_block",
                            username=ru['username'],
                            name=ru['display_name'],
                            uid=ru['id'], type=ru['type'],
                            status=bot_status)
                        # Add tips based on user type and bot status
                        if ru['is_channel'] or ru['is_group']:
                            tips_text = t(user_id, "tips_group_wallet")
                        elif not has_started:
                            tips_text = t(user_id, "tips_not_started")
                        # If has_started is True, no tips needed

                    # Build amount instruction
                    amount_instruction = t(user_id, "send_choose_amount")

                    # Edit processing message or send new one
                    if processing_msg:
                        await processing_msg.edit(
                            t(user_id, "send_amount_prompt",
                              resolved=resolved_text, tips=tips_text,
                              instruction=amount_instruction, hint=hint),
                            buttons=buttons
                        )
                    else:
                        await event.respond(
                            t(user_id, "send_amount_prompt", resolved="",
                              tips="", instruction=amount_instruction,
                              hint=hint),
                            buttons=buttons
                        )
                else:
                    # Delete processing message if exists
                    if processing_msg:
                        try:
                            await processing_msg.delete()
                        except:
                            pass
                    # Prepare payment with parsed amount
                    user_steps[user_id] = "send_confirm"
                    await prepare_and_show_fee(event, user_id, invoice_text, parsed_amount)

            except Exception as error:
                # Edit processing message with error or send new one
                error_msg = t(user_id, "invalid_input_card", error=str(error))
                if processing_msg:
                    await processing_msg.edit(
                        error_msg,
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                    )
                else:
                    await event.respond(
                        error_msg,
                        buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
                    )
            return

        # Send amount for zero-amount invoice
        # ==================== SEND: Get Amount ====================
        elif current_step == "send_amount":
            # Parse amount (sats or USD)
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                if is_usd:
                    await event.respond(
                        t(user_id, "usd_conv_fail_retry"),
                        buttons=[
                            [Button.inline(t(user_id, "btn_wd_all"), b"send_withdraw_all")],
                            [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                        ]
                    )
                else:
                    await event.respond(
                        t(user_id, "amount_invalid_retry"),
                        buttons=[
                            [Button.inline(t(user_id, "btn_wd_all"), b"send_withdraw_all")],
                            [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                        ]
                    )
                return

            if amount <= 0:
                await event.respond(
                    t(user_id, "amount_gt0_wd"),
                    buttons=[
                        [Button.inline(t(user_id, "btn_wd_all"), b"send_withdraw_all")],
                        [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                    ]
                )
                return

            if user_id in user_data:
                user_data[user_id]['amount'] = amount
                data = user_data[user_id]
                payment_type = data.get('payment_type', 'bolt11')

                # LNURL Payment
                if payment_type == 'lnurl':
                    if amount < data['min_sats'] or amount > data['max_sats']:
                        await event.respond(
                            t(user_id, "lnurl_range_err",
                              mn=f"{data['min_sats']:,}",
                              mx=f"{data['max_sats']:,}"),
                            buttons=[
                                [Button.inline(t(user_id, "btn_wd_all"), b"send_withdraw_all")],
                                [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                            ]
                        )
                        return

                    try:
                        res = await event.respond(t(user_id, "calc_loading"))
                        prepare_response, fee_sats = await prepare_lnurl_pay(
                            user_id, data['pay_request'], amount
                        )

                        user_data[user_id]['prepare_response'] = prepare_response
                        user_data[user_id]['fee'] = fee_sats

                        total = amount + fee_sats

                        # Format with USD
                        amount_display = await format_amount_with_usd(amount, user_id)
                        fee_display = await format_amount_with_usd(fee_sats, user_id)
                        total_display = await format_amount_with_usd(total, user_id)

                        _q = (user_data.get(user_id) or {}).get('send_queue') or []
                        _q_txt = (build_queue_text(_q, user_id)[0] + "\n\n" + "─" * 10 + "\n" + t(user_id, "queue_new_payment") + "\n") if _q else ""
                        await res.edit(
                            _q_txt + t(user_id, "pay_summary_lnurl",
                                       amount=amount_display, fee=fee_display,
                                       total=total_display),
                            buttons=[
                                [
                                    Button.inline(t(user_id, "btn_confirm"), b"confirm_payment_yes"),
                                    Button.inline(t(user_id, "btn_cancel_x"), b"back_to_menu")
                                ],
                            ]
                        )
                        del user_steps[user_id]
                    except Exception as error:
                        await event.respond(
                            t(user_id, "prep_err_retry", error=str(error)),
                            buttons=[
                                [Button.inline(t(user_id, "btn_wd_all"), b"send_withdraw_all")],
                                [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                            ]
                        )

                # Onchain Payment
                elif payment_type == 'onchain':
                    try:
                        res = await event.respond(t(user_id, "onchain_fees_loading"))
                        user_data[user_id]['amount'] = amount
                        prepare_response, _, _ = await prepare_payment(user_id, data['invoice'], amount)

                        fee_quote = prepare_response.payment_method.fee_quote
                        slow_fee = fee_quote.speed_slow.user_fee_sat + fee_quote.speed_slow.l1_broadcast_fee_sat
                        medium_fee = fee_quote.speed_medium.user_fee_sat + fee_quote.speed_medium.l1_broadcast_fee_sat
                        fast_fee = fee_quote.speed_fast.user_fee_sat + fee_quote.speed_fast.l1_broadcast_fee_sat

                        user_data[user_id]['prepare_response'] = prepare_response
                        user_data[user_id]['fees'] = {
                            'slow': slow_fee,
                            'medium': medium_fee,
                            'fast': fast_fee
                        }

                        # Format with USD
                        amount_display = await format_amount_with_usd(amount, user_id)

                        await res.edit(
                            t(user_id, "onchain_fee_options",
                              amount=amount_display, slow=f"{slow_fee:,}",
                              med=f"{medium_fee:,}", fast=f"{fast_fee:,}"),
                            buttons=[
                                [
                                    Button.inline(t(user_id, "btn_fee_slow"), b"onchain_speed_slow"),
                                    Button.inline(t(user_id, "btn_fee_medium"), b"onchain_speed_medium"),
                                    Button.inline(t(user_id, "btn_fee_fast"), b"onchain_speed_fast")
                                ],
                                [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                            ]
                        )
                        del user_steps[user_id]
                    except Exception as error:
                        await event.respond(
                            t(user_id, "fee_calc_err", error=str(error)),
                            buttons=[
                                [Button.inline(t(user_id, "btn_wd_all"), b"send_withdraw_all")],
                                [Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]
                            ]
                        )

                # Regular BOLT11 Payment
                else:
                    invoice = data['invoice']
                    user_steps[user_id] = "send_confirm"
                    await prepare_and_show_fee(event, user_id, invoice, amount)
            else:
                await event.respond(
                    t(user_id, "session_expired"),
                    buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
                )
                del user_steps[user_id]
            return

    except Exception as error:
        logging.error(f"Message handler error: {error}")
        await event.respond(
            t(user_id, "err_alert_line", error=str(error)),
            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
        )
        if user_id in user_steps:
            del user_steps[user_id]


# ==================== Balance Command ====================
# ==================== v30 NEW COMMAND HANDLERS ====================
async def show_contacts(event, edit=False):
    """Render the user's address book (shared by /contacts and the contacts callback)."""
    user_id = event.sender_id
    try:
        contacts = await list_user_contacts(user_id)
    except Exception as e:
        msg = t(user_id, "contacts_load_err", error=str(e))
        btns = _row_back_menu(user_id)
        if edit:
            try:
                await event.edit(msg, buttons=btns)
            except Exception:
                await event.respond(msg, buttons=btns)
        else:
            await event.respond(msg, buttons=btns)
        return

    # Cache contacts by short index so callback data stays well under Telegram's
    # 64-byte limit (the SDK contact ids can be long).
    cmap = {str(i): {'id': c.id, 'name': c.name, 'identifier': c.payment_identifier}
            for i, c in enumerate(contacts or [])}
    user_data.setdefault(user_id, {})['contact_map'] = cmap

    btns = []
    if not cmap:
        msg = t(user_id, "contacts_empty")
    else:
        lines = [t(user_id, "contacts_title")]
        for idx, c in cmap.items():
            ident = c['identifier']
            shown = (ident[:24] + '…') if len(ident) > 25 else ident
            lines.append(t(user_id, "contacts_item", name=c['name'], ident=shown))
            btns.append([
                Button.inline(f"📤 {c['name']}", f"contactpay_{idx}".encode()),
                Button.inline("🗑️", f"contactdel_{idx}".encode()),
            ])
        msg = "\n".join(lines)
    btns.append([Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")])

    if edit:
        try:
            await event.edit(msg, buttons=btns)
        except Exception:
            await event.respond(msg, buttons=btns)
    else:
        await event.respond(msg, buttons=btns)


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/token_status\s*$'))
async def token_status_handler(event):
    import sqlite3
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = conn.execute("SELECT nonce, status FROM token_withdraw_attempts WHERE user_id=? AND status NOT IN ('safe_failed','delivered','refunded') ORDER BY updated LIMIT 10", (event.sender_id,)).fetchall() if 'token_withdraw_attempts' in tables else []
        gate = 'token_withdraw_migration' in tables and bool(conn.execute('SELECT 1 FROM token_withdraw_migration WHERE id=1').fetchone())
    if not rows:
        await event.respond(token_withdraw_notice(event.sender_id, 'blocked') if not gate else
                            ('برداشت معلق ثبت‌شده‌ای وجود ندارد.' if get_user_lang(event.sender_id) == 'fa' else 'No recorded withdrawal is on hold.'))
        return
    known = {'pending', 'unknown', 'claimed', 'sending'}
    for nonce, status in rows:
        await event.respond(token_withdraw_notice(event.sender_id, status if status in known else 'blocked', nonce))


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/token_audit(?:\s+.*)?$'))
async def token_audit_handler(event):
    """Owner-only: status, review USER NONCE, or acknowledge historical audit."""
    if not token_withdraw_admin(event.sender_id):
        return
    import sqlite3
    args = (event.raw_text or '').split()
    if len(args) == 1:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            gate = 'token_withdraw_migration' in tables and bool(conn.execute('SELECT 1 FROM token_withdraw_migration WHERE id=1').fetchone())
            rows = conn.execute("SELECT user_id, nonce, status FROM token_withdraw_attempts WHERE status NOT IN ('safe_failed','delivered','refunded') ORDER BY updated LIMIT 30").fetchall() if 'token_withdraw_attempts' in tables else []
        lines = [f'Historical audit gate: {"open" if gate else "CLOSED"}. Untracked old sends are not discoverable here.',
                 'Review: /token_audit review USER_ID NONCE',
                 'Hold an untracked historical attempt: /token_audit hold USER_ID',
                 'After auditing ALL historical settlements, including untracked sends, and recording holds for unresolved cases: /token_audit attest_all_history_audited',
                 'First 30 held attempts (no automatic resend):']
        lines.extend(f'{uid} {nonce} {status}' for uid, nonce, status in rows)
        await event.respond('\n'.join(lines))
    elif len(args) == 2 and args[1] == 'attest_all_history_audited':
        token_withdraw_migration_ack(event.sender_id)
        await event.respond('Historical audit attested; gate opened. Existing unresolved attempts remain held.')
    elif len(args) == 3 and args[1] == 'hold' and args[2].isdigit() and int(args[2]) > 0:
        nonce = token_withdraw_register_legacy(event.sender_id, int(args[2]))
        await event.respond(f'Historical attempt held: {nonce}. Manual investigation required; no automatic release.')
    elif len(args) == 4 and args[1] == 'review' and args[2].isdigit():
        result = await token_withdraw_reconcile(event.sender_id, int(args[2]), args[3])
        await event.respond(f'Review result: {result}. Only delivered, refunded or safe_failed clear holds; all other results remain held. Never retry an ambiguous send.')
    else:
        await event.respond('Use /token_audit for instructions.')


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/fees'))
async def fees_handler(event):
    """Show recommended on-chain network fees."""
    try:
        await event.respond(t(event.sender_id, "fees_loading"))
        fees = await get_recommended_fees(event.sender_id)
        await event.respond(_format_fees(fees, event.sender_id),
                            buttons=[[Button.inline(t(event.sender_id, "btn_back_menu"), b"back_to_menu")]])
    except Exception as e:
        await event.respond(t(event.sender_id, "fees_failed", error=str(e)))


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/sync'))
async def sync_handler(event):
    """Force a wallet sync."""
    try:
        await event.respond(t(event.sender_id, "syncing_wallet"))
        info = await sync_user_wallet(event.sender_id)
        await event.respond(
            t(event.sender_id, "sync_done",
              balance=f"{getattr(info, 'balance_sats', 0):,}"),
            buttons=[[Button.inline(t(event.sender_id, "btn_back_menu"), b"back_to_menu")]])
    except Exception as e:
        await event.respond(t(event.sender_id, "sync_failed", error=str(e)))


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/signmsg(?:\s+(.+))?'))
async def signmsg_handler(event):
    """Sign a message to prove wallet ownership. Usage: /signmsg <message>"""
    try:
        message = (event.pattern_match.group(1) or '').strip()
        if not message:
            await event.respond(t(event.sender_id, "signmsg_usage"))
            return
        status = await event.respond(t(event.sender_id, "signing_msg"))
        resp = await sign_message_with_wallet(event.sender_id, message)
        await status.edit(
            t(event.sender_id, "signmsg_done", msg=message,
              pubkey=resp.pubkey, sig=resp.signature),
            buttons=[[Button.inline(t(event.sender_id, "btn_back_menu"), b"back_to_menu")]])
    except Exception as e:
        await event.respond(t(event.sender_id, "signmsg_failed", error=str(e)))


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/verifymsg\s+(\S+)\s+(\S+)\s+(.+)'))
async def verifymsg_handler(event):
    """Verify a signed message. Usage: /verifymsg <pubkey> <signature> <message>"""
    try:
        pubkey = event.pattern_match.group(1)
        signature = event.pattern_match.group(2)
        message = event.pattern_match.group(3).strip()
        resp = await check_message_with_wallet(event.sender_id, message, pubkey, signature)
        verdict = (t(event.sender_id, "verify_valid") if resp.is_valid
                  else t(event.sender_id, "verify_invalid"))
        await event.respond(
            t(event.sender_id, "verify_result", verdict=verdict,
              msg=message, pubkey=pubkey),
            buttons=[[Button.inline(t(event.sender_id, "btn_back_menu"), b"back_to_menu")]])
    except Exception as e:
        await event.respond(t(event.sender_id, "verify_failed", error=str(e)))


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/buy'))
async def buy_handler(event):
    """Get a Buy Bitcoin on-ramp link."""
    try:
        url = await buy_bitcoin_url(event.sender_id)
        await event.respond(
            t(event.sender_id, "buy_card", url=url),
            buttons=[[Button.inline(t(event.sender_id, "btn_back_menu"), b"back_to_menu")]],
            link_preview=False)
    except Exception as e:
        await event.respond(t(event.sender_id, "buy_failed", error=str(e)))


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/contacts'))
async def contacts_handler(event):
    """List saved contacts."""
    await show_contacts(event, edit=False)


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/addcontact\s+(\S+)\s+(\S+)'))
async def add_contact_handler(event):
    """Add a contact: /addcontact <name> <address-or-invoice>"""
    try:
        name = event.pattern_match.group(1).strip()
        identifier = event.pattern_match.group(2).strip()
        if len(name) > 48:
            await event.respond(t(event.sender_id, "contact_name_long"))
            return
        contact = await add_user_contact(event.sender_id, name, identifier)
        await event.respond(
            t(event.sender_id, "contact_saved", name=contact.name,
              ident=contact.payment_identifier),
            buttons=[[Button.inline(t(event.sender_id, "btn_view_contacts"), b"contacts")],
                     [Button.inline(t(event.sender_id, "btn_back_menu"), b"back_to_menu")]])
    except Exception as e:
        await event.respond(t(event.sender_id, "contact_add_failed", error=str(e)))


@client.on(events.NewMessage(pattern=r'(?i)/zap_balance(?:@\w+)?\b'))
async def balance_handler(event):
    """Show wallet balance with private button"""
    # Use helper to handle anonymous channel senders
    user_id, is_anonymous = get_sender_info(event)

    if not user_id:
        await event.reply(t(user_id, "err_no_sender"))
        return

    # For anonymous senders, show balance directly as text (no button)
    if is_anonymous:
        await init(user_id)
        balance = await get_balance(user_id)
        balance_display = await format_balance_with_usd(balance, user_id)
        await event.reply(t(user_id, "balance_anon_text", balance=balance_display))
        return

    # Generate unique balance request ID
    import time
    balance_id = f"bal_{user_id}_{int(time.time())}"

    # Store the request with anonymous flag
    if not hasattr(balance_handler, 'pending_balance'):
        balance_handler.pending_balance = {}
    balance_handler.pending_balance[balance_id] = {
        'user_id': user_id,
        'is_anonymous': is_anonymous,
        'created_at': datetime.now()
    }

    await event.reply(
        t(user_id, "balance_check_prompt"),
        buttons=[
            [Button.inline(t(user_id, "btn_show_bal"), f"show_balance_{balance_id}".encode())]
        ]
    )


@client.on(events.NewMessage(pattern=r'(?i)/debug(?:@\w+)?\b'))
async def debug_handler(event):
    """Show bot statistics with volume tracking"""
    users = db.get('users')
    total_users = len(users) if users else 0

    # Get volume stats
    volume_stats = get_volume_stats()
    total_in = volume_stats.get('total_in', 0)
    total_out = volume_stats.get('total_out', 0)

    # Get USD rate for conversion
    usd_rate = await get_usd_rate()

    # Format volumes with USD
    unit = t(event.sender_id, "sats_unit")
    if usd_rate:
        total_in_usd = sats_to_usd(total_in, usd_rate)
        total_out_usd = sats_to_usd(total_out, usd_rate)
        in_display = f"{total_in:,} {unit} (~${total_in_usd:,.2f})"
        out_display = f"{total_out:,} {unit} (~${total_out_usd:,.2f})"
    else:
        in_display = f"{total_in:,} {unit}"
        out_display = f"{total_out:,} {unit}"

    stats_msg = t(event.sender_id, "debug_stats", users=total_users,
                  vin=in_display, vout=out_display)

    await event.reply(stats_msg)


# ==================== TIP System with $ support, message, and confirmation ====================
@client.on(events.NewMessage(pattern=r'(?i)/zap(?:@\w+)?\b\s*$'))
async def zap_help_handler(event):
    """Show zap usage when /zap is typed with no arguments (e.g. tapped from the
    Telegram command menu, which also appends @BotUsername)."""
    await event.reply(t(event.sender_id, "zap_usage"))


@client.on(events.NewMessage(pattern=r'(?i)/zap(?:@\w+)?\s+(\$?\d+(?:\.\d+)?)\s*(?:@(\w+))?(?:\s+(.+))?'))
async def tip_handler(event):
    """Handle /zap command in groups with username support, $ flag, and optional message

    Examples:
    - /zap 1000 - Send 1000 sats (reply to message)
    - /zap 1000 @username - Send 1000 sats to @username
    - /zap 1000 @username Great job! - Send 1000 sats with message
    - /zap $5 - Send $5 worth of sats (reply to message)
    - /zap $10 @username - Send $10 to @username
    - /zap 21 @alice You're awesome! - Send 21 sats with message
    """
    if event.is_private:
        await event.reply(t(event.sender_id, "zap_groups_only"))
        return

    try:
        match = event.pattern_match
        amount_str = match.group(1)
        username = match.group(2)
        message = match.group(3)  # Optional message after username

        # If no username but there's a message, the regex might have captured it differently
        # Check if the message is actually part of the third group
        if not message and username:
            # The regex might not capture spaces correctly, let's handle this
            # Split the original text and find the message part
            parts = event.text.strip().split(None, 3)  # Split into max 4 parts
            if len(parts) > 3:
                message = parts[3]

        receiver_id = None

        # Use helper to handle anonymous channel senders
        sender_id, is_anonymous = get_sender_info(event)

        if not sender_id:
            await event.reply(t(sender_id, "err_no_sender"))
            return

        # Quick basic validation first
        if amount_str.startswith('$'):
            amount = int(float(amount_str[1:]) * 1000)  # Rough estimate for display
            amount_display_temp = f"${amount_str[1:]}"
        else:
            amount = int(amount_str)
            amount_display_temp = f"{amount:,} {t(sender_id, 'sats_unit')}"

        if amount <= 0:
            await event.reply(t(sender_id, "zap_amount_gt0"))
            return

        # Create zap ID and store BASIC data immediately (before sending message)
        import time
        zap_id = f"{sender_id}_{int(time.time())}"

        # Store minimal zap data NOW so cancel button works immediately
        pending_zap_confirmations[sender_id] = {
            'id': zap_id,
            'sender_id': sender_id,
            'status': 'initializing',  # Will be updated to 'processing' later
            'amount_str': amount_str,
            'username': username,
            'message': message,
            'created_at': datetime.now()
        }

        # Send INSTANT loading message with cancel button
        _msg = await event.reply(
            t(sender_id, "zap_loading"),
            buttons=[
                Button.inline(t(sender_id, "btn_cancel_x"), f"zap_cancel_{zap_id}".encode())
            ]
        )

        # Now do the slow operations in background
        asyncio.create_task(process_zap_full(
            zap_id, _msg, event, sender_id, is_anonymous,
            amount_str, username, message, amount_display_temp
        ))

    except Exception as error:
        logging.error(f"Zap error: {error}")
        await event.reply(t(event.sender_id, "zap_failed_reason", reason=error))


async def add_zap_reaction(chat_id, message_id):
    """Add ⚡ reaction to the zapped message"""
    try:
        await client(SendReactionRequest(
            peer=chat_id,
            msg_id=message_id,
            reaction=[ReactionEmoji(emoticon="⚡")]
        ))
        logging.info(f"Added ⚡ reaction to message {message_id} in chat {chat_id}")
        return True
    except Exception as error:
        logging.debug(f"Could not add reaction: {error}")
        return False


async def _zap_mention(client, user, uid, plain=False):
    """v32 (zap display fix): best From/To mention for zap_announce/zap_success.

    Resolution order:
      1. active @username on the entity we already hold;
      2. the Telegram multi-username vector (.usernames) — the FIRST entry
         is the primary active username (this is what leaked raw numeric
         ids before: .username None while the vector had entries);
      3. ONE fresh read-only client.get_entity(uid) re-check (5s guard) —
         cached group entities can predate a user's username; never
         touches SDK/wallet state;
      4. clickable [first_name](tg://user?id=...) mention — zap messages
         render with the client's default Markdown parse mode (the
         templates already use **bold**), so tg://user links resolve;
      5. plain #<id> / str(<id>).

    plain=True is for non-markdown sinks (the invoice memo): same
    username resolution, but never emits link syntax — @username or the
    bare numeric id, byte-identical to the pre-fix memo.
    """
    def _primary_username(entity):
        vector = getattr(entity, 'usernames', None)
        if vector:
            name = getattr(vector[0], 'username', None)  # first = primary
            if name:
                return name
        return None

    entity = user
    username = getattr(entity, 'username', None) if entity is not None else None
    primary = _primary_username(entity)
    if not username and not primary:
        try:
            entity = await asyncio.wait_for(client.get_entity(uid), timeout=5)
            username = getattr(entity, 'username', None)
            primary = _primary_username(entity)
        except Exception:
            pass  # keep whatever the original entity still offers below
    if username:
        return f'@{username}'
    if primary:
        return f'@{primary}'
    first_name = getattr(entity, 'first_name', None) if entity is not None else None
    if first_name and not plain:
        return f'[{_md_escape(str(first_name))}](tg://user?id={uid})'
    if not first_name and not plain:
        return f'#{uid}'
    return str(uid)


# Full zap processing in background (all slow operations here)
async def process_zap_full(zap_id, msg, event, sender_id, is_anonymous, amount_str, username, message, amount_display_temp):
    """Process all zap operations in background"""
    try:
        # Check if cancelled during initializing phase
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit(t(sender_id, "zap_cancelled"), buttons=None)
            except:
                pass  # Message already edited by cancel handler
            del pending_zap_confirmations[sender_id]
            return

        # Parse amount (sats or USD) - SLOW API CALL
        usd_rate = await get_usd_rate()

        if amount_str.startswith('$'):
            # USD amount
            usd_amount = float(amount_str[1:])
            if not usd_rate:
                try:
                    await msg.edit(t(sender_id, "zap_no_usd_rate"), buttons=None)
                except:
                    pass
                return
            amount = usd_to_sats(usd_amount, usd_rate)
            amount_display = f"${usd_amount} ({amount:,} {t(sender_id, 'sats_unit')})"
        else:
            # Sats amount
            amount = int(amount_str)
            if usd_rate:
                usd_value = sats_to_usd(amount, usd_rate)
                amount_display = f"{amount:,} {t(sender_id, 'sats_unit')} (~${usd_value:.2f})"
            else:
                amount_display = f"{amount:,} {t(sender_id, 'sats_unit')}"

        # Check if cancelled after amount parsing
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit(t(sender_id, "zap_cancelled"), buttons=None)
            except:
                pass  # Message already edited by cancel handler
            del pending_zap_confirmations[sender_id]
            return

        # Determine receiver - SLOW API CALL
        reply_msg_id = None  # Store reply message ID for reaction
        if username:
            try:
                user = await client.get_entity(username)
                receiver_id = user.id
                if isinstance(user, Channel):
                    receiver_id = int(f"-100{user.id}")
                receiver_name = f"@{username}"
            except Exception as error:
                try:
                    await msg.edit(t(sender_id, "zap_target_not_found", username=username), buttons=None)
                except:
                    pass
                return
        elif event.is_reply:
            reply_msg = await event.get_reply_message()
            reply_msg_id = reply_msg.id  # Store for reaction
            # Handle case where reply is to a channel message (sender could be None)
            receiver_id, _ = get_sender_info(reply_msg)
            if not receiver_id:
                try:
                    await msg.edit(t(sender_id, "zap_unknown_target"), buttons=None)
                except:
                    pass
                return
            if reply_msg.sender:
                receiver_name = await _zap_mention(client, reply_msg.sender, receiver_id)
            else:
                receiver_name = str(receiver_id)
        else:
            try:
                await msg.edit(
                    t(sender_id, "zap_usage_reply"),
                    buttons=None
                )
            except:
                pass
            return

        if sender_id == receiver_id:
            try:
                await msg.edit(t(sender_id, "zap_self"), buttons=None)
            except:
                pass
            return

        # Check if cancelled after receiver determination
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit(t(sender_id, "zap_cancelled"), buttons=None)
            except:
                pass  # Message already edited by cancel handler
            del pending_zap_confirmations[sender_id]
            return

        # Initialize wallets - SLOW SDK OPERATION
        await init(sender_id)
        await init(receiver_id)

        # Check balance - SLOW SDK OPERATION
        # Spark-to-Spark transfers are feeless; Lightning fallback costs a routing fee.
        # Get the REAL fee from prepare_payment instead of guessing, so a user with
        # exactly `amount` sats isn't wrongly rejected (v35 fix).
        balance = int(await get_balance(sender_id))
        if balance < amount:
            try:
                await msg.edit(
                    t(sender_id, "zap_insuff_balance"),
                    buttons=None
                )
            except:
                pass
            return

        # Check if cancelled after wallet operations
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit(t(sender_id, "zap_cancelled"), buttons=None)
            except:
                pass  # Message already edited by cancel handler
            del pending_zap_confirmations[sender_id]
            return

        # Get sender name
        sender_memo_name = None
        if is_anonymous:
            sender_name = t(sender_id, "zap_anon_name")
            if event.sender and hasattr(event.sender, 'title') and event.sender.title:
                sender_name = f"📢 {event.sender.title}"
        else:
            if event.sender:
                sender_name = await _zap_mention(client, event.sender, sender_id)
            else:
                sender_name = str(sender_id)
            # Invoice memos are not message-rendered: same resolution but no
            # markdown link syntax (byte-identical to the pre-fix memo).
            sender_memo_name = await _zap_mention(client, event.sender, sender_id, plain=True)

        # Prepare description for the invoice (include message if provided)
        # Sanitize to remove non-printable characters (like ZWNJ in Persian)
        sender_for_memo = sender_memo_name if sender_memo_name is not None else sender_name
        if message:
            description = sanitize_memo(f"Zap from {sender_for_memo}: {message}")
        else:
            description = sanitize_memo(f"Zap from {sender_for_memo}")

        # FINAL CHECK before starting payment - if cancelled, don't proceed
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit(t(sender_id, "zap_cancelled"), buttons=None)
            except:
                pass  # Message already edited by cancel handler
            del pending_zap_confirmations[sender_id]
            return

        # Store zap data for cancellation
        pending_zap_confirmations[sender_id] = {
            'id': zap_id,
            'sender_id': sender_id,
            'receiver_id': receiver_id,
            'amount_sats': amount,
            'sender_name': sender_name,
            'receiver_name': receiver_name,
            'chat_id': event.chat_id,
            'original_msg_id': event.id,
            'reply_msg_id': reply_msg_id,  # For ⚡ reaction on replied message
            'created_at': datetime.now(),
            'amount_display': amount_display,
            'description': description,
            'is_anonymous': is_anonymous,
            'status': 'processing'
        }

        # Edit with full details
        await msg.edit(
            t(sender_id, "zap_announce", amount=amount_display, sender=sender_name, receiver=receiver_name),
            buttons=[
                Button.inline(t(sender_id, "btn_cancel_x"), f"zap_cancel_{zap_id}".encode())
            ]
        )

        # Start both tasks: payment and button removal timer
        asyncio.create_task(process_zap_payment(zap_id, msg, sender_id, receiver_id, amount, amount_display, sender_name, receiver_name, description, is_anonymous))
        asyncio.create_task(remove_cancel_button_after_delay(zap_id, msg, amount_display, sender_name, receiver_name))

    except Exception as error:
        logging.error(f"Zap processing error: {error}")
        try:
            await msg.edit(t(sender_id, "zap_failed_reason", reason=error), buttons=None)
        except:
            pass


# Background task to remove cancel button after delay
async def remove_cancel_button_after_delay(zap_id, msg, amount_display, sender_name, receiver_name):
    """Remove cancel button after 0.5 seconds if payment still processing"""
    await asyncio.sleep(0.5)

    # Find the zap data
    zap_owner_id = None
    for uid, zdata in pending_zap_confirmations.items():
        if zdata.get('id') == zap_id:
            zap_owner_id = uid
            break

    # Only remove button if still processing (not cancelled or completed)
    if zap_owner_id and zap_owner_id in pending_zap_confirmations:
        zap_data = pending_zap_confirmations[zap_owner_id]
        if zap_data.get('status') == 'processing':
            try:
                await msg.edit(
                    t(zap_owner_id, "zap_announce", amount=amount_display, sender=sender_name, receiver=receiver_name),
                    buttons=None
                )
            except:
                pass  # Message might have been edited by payment task


# Background task to process zap payment
async def process_zap_payment(zap_id, msg, sender_id, receiver_id, amount, amount_display, sender_name, receiver_name, description, is_anonymous):
    """Process zap payment in background, allowing cancellation"""
    try:
        # Find the zap data
        zap_data = None
        zap_owner_id = None
        for uid, zdata in pending_zap_confirmations.items():
            if zdata.get('id') == zap_id:
                zap_data = zdata
                zap_owner_id = uid
                break

        if not zap_data:
            await msg.edit(t(sender_id, "zap_data_missing"), buttons=None)
            return

        # Small delay to allow quick cancellation (0.5 seconds)
        await asyncio.sleep(0.5)

        # Check if cancelled before starting payment
        if zap_owner_id in pending_zap_confirmations and pending_zap_confirmations[zap_owner_id].get('status') == 'cancelled':
            await msg.edit(t(sender_id, "zap_cancelled_plain"), buttons=None)
            del pending_zap_confirmations[zap_owner_id]
            return

        # Create invoice and send payment
        receiver_invoice = await create_invoice(receiver_id, amount_sats=amount, description=description)
        prepare_response, fee_sats, spark_fee = await prepare_payment(sender_id, receiver_invoice, amount)

        # v35: re-verify balance against the REAL fee. If balance < amount + fee,
        # the Lightning route can't be paid — but Spark is feeless, so if
        # spark_fee == 0 the payment still works with exact balance.
        balance = int(await get_balance(sender_id))
        total_needed = amount + (spark_fee or 0)
        if balance < total_needed:
            await msg.edit(
                t(sender_id, "zap_insuff_fees"),
                buttons=None)
            if zap_owner_id in pending_zap_confirmations:
                del pending_zap_confirmations[zap_owner_id]
            return

        # Check if cancelled again before final payment
        if zap_owner_id in pending_zap_confirmations and pending_zap_confirmations[zap_owner_id].get('status') == 'cancelled':
            await msg.edit(t(sender_id, "zap_cancelled_plain"), buttons=None)
            del pending_zap_confirmations[zap_owner_id]
            return

        options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
        payment = await send_payment(sender_id, prepare_response, options)

        # Check if payment is truly successful
        is_success, failure_reason = is_payment_successful(payment)

        if is_success:
            # Track outgoing volume for zap
            if amount and amount > 0:
                track_volume('out', amount + (fee_sats or 0))

            # If payment succeeded, show success regardless of cancellation status
            # (payment has already gone through at this point)
            await msg.edit(
                t(sender_id, "zap_success", amount=amount_display, sender=sender_name, receiver=receiver_name),
                buttons=None  # Remove cancel button
            )
            # Tip prompt goes to the SENDER's private chat, never the group
            if sender_id:
                await show_tip_prompt(sender_id, chat_id=sender_id)
        else:
            await msg.edit(
                t(sender_id, "zap_failed_block", amount=amount_display, reason=failure_reason),
                buttons=None
            )
            # Clean up pending zap
            if zap_owner_id in pending_zap_confirmations:
                del pending_zap_confirmations[zap_owner_id]
            return

        # Add ⚡ reaction to the replied message (if it was a reply)
        if zap_data and zap_data.get('reply_msg_id') and zap_data.get('chat_id'):
            await add_zap_reaction(zap_data['chat_id'], zap_data['reply_msg_id'])

        # Clean up
        if zap_owner_id in pending_zap_confirmations:
            del pending_zap_confirmations[zap_owner_id]

    except Exception as error:
        logging.error(f"Zap payment error: {error}")
        error_msg = t(sender_id, "zap_failed_reason", reason=error)

        # Only edit if not already cancelled
        zap_owner_id = None
        for uid, zdata in pending_zap_confirmations.items():
            if zdata.get('id') == zap_id:
                zap_owner_id = uid
                break

        if zap_owner_id in pending_zap_confirmations and pending_zap_confirmations[zap_owner_id].get('status') != 'cancelled':
            await msg.edit(error_msg, buttons=None)

        if zap_owner_id in pending_zap_confirmations:
            del pending_zap_confirmations[zap_owner_id]


# Cleanup old pending zaps periodically
async def cleanup_pending_zaps():
    """Remove expired zap confirmations"""
    while True:
        try:
            await asyncio.sleep(300)  # Every 5 minutes

            now = datetime.now()
            expired = []

            for user_id, zap_data in pending_zap_confirmations.items():
                created = zap_data.get('created_at')
                if created and (now - created).total_seconds() > 300:  # 5 minute expiry
                    expired.append(user_id)

            for user_id in expired:
                del pending_zap_confirmations[user_id]

            if expired:
                logging.info(f"Cleaned up {len(expired)} expired zap confirmations")

        except Exception as error:
            logging.error(f"Cleanup error: {error}")


# ==================== BACKUP SERVICE ====================
def _snapshot_sqlite(src_path, dest_path):
    """Consistent online snapshot of a (possibly WAL) sqlite database."""
    import sqlite3 as _sq
    src = _sq.connect(src_path)
    dst = _sq.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def backup_to_telegram():
    """Periodic backup of databases and source code to Telegram channel"""
    await asyncio.sleep(60)  # Wait 1 minute before first backup

    while True:
        try:
            now = datetime.now()
            timestamp = now.strftime('%Y-%m-%d_%H-%M-%S')

            # Backup database file
            if os.path.exists(DB_PATH):
                try:
                    # Create a copy of the database
                    backup_db_path = f'/tmp/db-bot-backup-{timestamp}.sqlite'
                    # Consistent snapshot (includes WAL content)
                    _snapshot_sqlite(DB_PATH, backup_db_path)

                    await client.send_file(
                        BACKUP_CHANNEL_ID,
                        backup_db_path,
                        caption=f"🗄️ **Database Backup**\n\n📅 {now.strftime('%Y-%m-%d %H:%M:%S')}\n📁 {DB_PATH}"
                    )

                    # Clean up temp file
                    os.remove(backup_db_path)
                    logging.info(f"Database backup sent to channel")
                except Exception as e:
                    logging.error(f"Database backup failed: {e}")

            # P2P database (WAL-safe snapshot)
            p2p_db = os.path.join(os.path.dirname(os.path.abspath(SOURCE_FILE_PATH)), 'p2p.db')
            if os.path.exists(p2p_db):
                try:
                    p2p_bak = f'/tmp/p2p-db-backup-{timestamp}.sqlite'
                    _snapshot_sqlite(p2p_db, p2p_bak)
                    await client.send_file(
                        BACKUP_CHANNEL_ID,
                        p2p_bak,
                        caption=f"🤝 **P2P Database Backup**\n\n📅 {now.strftime('%Y-%m-%d %H:%M:%S')}\n📁 {p2p_db}"
                    )
                    os.remove(p2p_bak)
                    logging.info("P2P database backup sent to channel")
                except Exception as e:
                    logging.error(f"P2P database backup failed: {e}")

            # Extra P2P source files
            for extra_src in ('zap_p2p.py', 'zap_i18n.py', 'zap_tokens.py') if BACKUP_INCLUDE_SOURCE else ():
                fp = os.path.join(os.path.dirname(os.path.abspath(SOURCE_FILE_PATH)), extra_src)
                if os.path.exists(fp):
                    try:
                        await client.send_file(
                            BACKUP_CHANNEL_ID, fp,
                            caption=f"📜 **Source Code Backup** ({extra_src})\n\n📅 {now.strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        logging.info(f"Source backup sent: {extra_src}")
                    except Exception as e:
                        logging.error(f"Source backup failed ({extra_src}): {e}")

            # Backup source code file
            if BACKUP_INCLUDE_SOURCE and os.path.exists(SOURCE_FILE_PATH):
                try:
                    await client.send_file(
                        BACKUP_CHANNEL_ID,
                        SOURCE_FILE_PATH,
                        caption=f"📜 **Source Code Backup**\n\n📅 {now.strftime('%Y-%m-%d %H:%M:%S')}\n📁 {SOURCE_FILE_PATH}"
                    )
                    logging.info(f"Source code backup sent to channel")
                except Exception as e:
                    logging.error(f"Source code backup failed: {e}")

            # Wait for next backup interval
            await asyncio.sleep(BACKUP_INTERVAL_MINUTES * 60)

        except Exception as error:
            logging.error(f"Backup service error: {error}")
            await asyncio.sleep(300)  # Wait 5 minutes on error


if __name__ == "__main__":
    # Increase file descriptor limit to prevent "Too many open files"
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 8192), hard))
        logging.info(f"File descriptor limit: {soft} -> {min(hard, 8192)}")
    except Exception as e:
        logging.warning(f"Could not increase file descriptor limit: {e}")

    # ==================== SECURE STARTUP ====================
    # Step 1: Get bot token from user (not from env file)
    get_bot_token()

    # Step 2: Initialize encryption with the bot token
    init_encryption()

    # Step 3: Backup database before any migration
    backup_path = f'./db-bot-backup-{datetime.now().strftime("%Y%m%d_%H%M%S")}.sqlite'
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
        print(f"📦 Database backed up to: {backup_path}")

    # Step 4: Migrate existing plaintext seeds to encrypted format
    migrate_existing_seeds()

    # Step 5: Start Telegram client with the runtime bot token
    print("\n🔄 Connecting to Telegram...")
    client.start(bot_token=bot_token)
    print("✅ Connected to Telegram")

    # Set bot start time for uptime tracking
    bot_start_time = datetime.now()

    print("\n" + "=" * 60)
    print("ZAP WALLET BOT v31 - SECURE + NEW SDK FEATURES")
    print("=" * 60)
    print(f"\nStarted at: {bot_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("\nNEW IN v30:")
    print("   - 🔒 Password hashing upgraded: bcrypt (was unsalted SHA-256)")
    print("   - 🔒 Send-password brute-force lockout (5 tries -> 15 min)")
    print("   - 🔒 Seed encryption KDF upgraded: PBKDF2-HMAC-SHA256 (600k iters)")
    print("   - 🔒 Bot token read from TELEGRAM_BOT_TOKEN env (runtime fallback)")
    print("   - 📒 Contacts / address book (/contacts, /addcontact, pay-from-list)")
    print("   - ⚡ Lightning address (🧰 More Tools → LN Address)")
    print("   - 🟧 Buy Bitcoin on-ramp (/buy)")
    print("   - ✍️ Sign / verify message (/signmsg, /verifymsg)")
    print("   - ⛽ Recommended fees (/fees)  +  🔄 Manual sync (/sync)")
    print("   - 🧰 'More Tools' menu button")
    print("\nSECURITY:")
    print("   - 🔐 Bot token loaded from env/.env (runtime getpass fallback)")
    print("   - 🔐 Seed phrases encrypted with Fernet (AES), PBKDF2-derived key")
    print("   - 🔐 Legacy seeds auto-migrated to the new KDF on startup")
    print(f"   - 📦 Backup every {BACKUP_INTERVAL_MINUTES} minutes")
    print(f"   - 📦 Backup channel: {BACKUP_CHANNEL_ID}")
    print("\nFEATURES:")
    print("   - Group /zap: durable one-shot claims + send password (same as tips)")
    print("   - Zap reaction on replied message")
    print("   - Cancel button available during zap processing")
    print("\nNOTIFICATION SYSTEM:")
    print("   - Event-driven (instant delivery)")
    print("   - Auto-enabled on bot usage")
    print("   - Auto-disabled after 24h inactivity")
    print("\n✅ Bot is ready and running!")
    print("=" * 60)

    # Start background tasks
    async def start_background_tasks():
        global bot_username
        await asyncio.sleep(2)  # Wait for bot to be fully ready

        # Fetch bot username for deep links
        try:
            me = await client.get_me()
            bot_username = me.username
            logging.info(f"Bot username: @{bot_username}")
        except Exception as e:
            logging.error(f"Failed to get bot username: {e}")

        asyncio.create_task(monitor_inactive_users())
        asyncio.create_task(cleanup_pending_zaps())
        asyncio.create_task(backup_to_telegram())
        asyncio.create_task(monitor_ffio_orders())
        asyncio.create_task(password_reset_monitor())
        # Create the durable batch tables up front (idempotent, migrates old DBs)
        with _batch_db():
            pass
        try:
            orphans = _batch_recover_orphans()
            if orphans:
                await _admin_alert(f'{orphans} batch item(s) from a previous run held as unknown — '
                                   'manual reconciliation needed')
        except Exception as e:
            logging.error(f'batch orphan recovery failed: {e}')

        # P2P section boot: escrow wallet + listener + one-shot restart scan
        async def _p2p_sweeper():
            while True:
                try:
                    await asyncio.sleep(60)
                    await zap_p2p.sweep(client)
                except Exception as sweep_err:
                    logging.error(f"p2p sweeper error: {sweep_err}")

        try:
            await zap_p2p.boot(client)
            asyncio.create_task(_p2p_sweeper())
            logging.info("P2P section started (escrow listener + sweeper)")
        except Exception as p2p_boot_err:
            logging.error(f"P2P boot failed: {p2p_boot_err}")

        logging.info("Background tasks started (including backup service, ff.io monitor & password reset monitor)")

        # Pre-fetch fiat rates
        try:
            await get_fiat_rates()
            logging.info("Fiat rates pre-fetched")
        except:
            pass

        # Process any pending password resets (in case bot was down)
        try:
            await process_password_resets()
            logging.info("Pending password resets processed")
        except Exception as e:
            logging.debug(f"Could not process password resets at startup: {e}")

    # Use client's loop to start background task
    client.loop.create_task(start_background_tasks())

    client.run_until_disconnected()
