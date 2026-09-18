from telethon import TelegramClient, events, Button
from keyvalue_sqlite import KeyValueSqlite
from breez_sdk_spark import *
from mnemonic import Mnemonic
import logging
import asyncio
import io
from random import choice
from string import ascii_lowercase
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import threading
from telethon.tl.types import User, Chat, Channel
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
    print("🔐 ZAP WALLET BOT v31 — SECURE STARTUP")
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
        import sys
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
BACKUP_CHANNEL_ID = int(os.getenv('BACKUP_CHANNEL_ID', '0'))  # optional Telegram channel for DB backups
BACKUP_INTERVAL_MINUTES = 30  # Backup every 30 minutes
SOURCE_FILE_PATH = __file__


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


def hash_send_password(password):
    """Hash password for secure storage (bcrypt). Kept for backward compat."""
    return _bcrypt_hash_password(password)


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
                    "🔓 **Password Reset Complete**\n\n"
                    "Your send password has been removed as requested.\n\n"
                    "You can set a new password in Settings.",
                    buttons=[[Button.inline("⚙️ Settings", b"settings")]]
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


async def edit_or_send(chat_id, text, buttons=None, reply_to=None, edit_msg=None):
    """Try to edit existing message, if fails send new one.
    edit_msg: message object to edit (optional)
    """
    try:
        if edit_msg:
            await edit_msg.edit(text, buttons=buttons)
            return edit_msg
    except:
        pass

    # Send new message
    return await client.send_message(chat_id, text, buttons=buttons, reply_to=reply_to)


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
FFIO_API_KEY = os.getenv('FFIO_API_KEY', '')  # optional: get yours at ff.io
FFIO_API_SECRET = os.getenv('FFIO_API_SECRET', '')
FFIO_REFCODE = os.getenv('FFIO_REFCODE', '')
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
            rates_response = await sdk.list_fiat_rates()
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

    return rates_dict if rates_dict else fiat_rates_cache.get('rates', {})


def sats_to_usd(sats, usd_rate):
    """Convert sats to USD"""
    if not usd_rate or usd_rate == 0:
        return None
    # rate is BTC per USD, so: sats / 100_000_000 * rate = USD
    btc = sats / 100_000_000
    usd = btc * usd_rate
    return round(usd, 2)


def usd_to_sats(usd, usd_rate):
    """Convert USD to sats"""
    if not usd_rate or usd_rate == 0:
        return None
    # rate is BTC per USD, so: USD / rate * 100_000_000 = sats
    btc = usd / usd_rate
    sats = int(btc * 100_000_000)
    return sats


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
    btc = sats / 100_000_000
    fiat_value = btc * fiat_rate
    return round(fiat_value, 2)


async def format_balance_with_fiat(balance_sats, user_id):
    """Format balance with user's preferred fiat equivalent"""
    balance_int = int(balance_sats)
    fiat_code = get_user_fiat(user_id)
    fiat_rate = await get_fiat_rate(fiat_code)
    if fiat_rate:
        fiat_value = sats_to_fiat(balance_int, fiat_rate)
        if fiat_value is not None:
            symbol = "$" if fiat_code == "USD" else ""
            # For large values (low-value currencies like IRT), show without decimals
            if fiat_value >= 1000:
                return f"{balance_int:,} sats (~{symbol}{fiat_value:,.0f} {fiat_code})"
            else:
                return f"{balance_int:,} sats (~{symbol}{fiat_value:,.2f} {fiat_code})"
    return f"{balance_int:,} sats"


async def format_balance_with_usd(balance_sats):
    """Format balance with USD equivalent"""
    usd_rate = await get_usd_rate()
    if usd_rate:
        usd_value = sats_to_usd(int(balance_sats), usd_rate)
        if usd_value is not None:
            return f"{balance_sats} sats (~${usd_value} USD)"
    return f"{balance_sats} sats"


async def format_amount_with_usd(amount_sats):
    """Format amount with USD equivalent"""
    usd_rate = await get_usd_rate()
    if usd_rate:
        usd_value = sats_to_usd(int(amount_sats), usd_rate)
        if usd_value is not None:
            return f"{amount_sats} sats (~${usd_value})"
    return f"{amount_sats} sats"


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


def get_status_icon_and_text(status):
    """Get status emoji and text from payment status"""
    status_str = str(status).lower()
    if 'complete' in status_str or 'succeeded' in status_str:
        return "✅", "Success"
    elif 'pending' in status_str:
        return "⏳", "Pending"
    elif 'failed' in status_str:
        return "❌", "Failed"
    else:
        return "⚪", str(status)


def is_payment_successful(payment):
    """Check if a payment is truly successful by examining status and HTLC details

    Returns:
        tuple: (is_successful: bool, failure_reason: str or None)
    """
    # Check main payment status
    status = getattr(payment, 'status', None)
    status_str = str(status).lower() if status else ''

    # If status explicitly shows failure
    if 'failed' in status_str:
        return False, "Payment failed"

    # Check HTLC details for Lightning payments
    details = getattr(payment, 'details', None)
    if details:
        htlc_details = getattr(details, 'htlc_details', None)
        if htlc_details:
            htlc_status = getattr(htlc_details, 'status', None)
            htlc_status_str = str(htlc_status).upper() if htlc_status else ''

            # RETURNED means HTLC was returned/failed
            if 'RETURNED' in htlc_status_str or 'FAILED' in htlc_status_str:
                return False, "HTLC returned - payment route failed"

            # Check for preimage - no preimage means no proof of payment
            preimage = getattr(htlc_details, 'preimage', None)
            if 'COMPLETE' in status_str or 'SUCCEEDED' in status_str:
                if not preimage:
                    # Status says complete but no preimage - might still be pending
                    pass  # Don't fail here, let it through

    # If status shows success or complete
    if 'complete' in status_str or 'succeeded' in status_str:
        return True, None

    # If pending, we'll consider it successful for now (user will be notified later if it fails)
    if 'pending' in status_str:
        return True, None

    # Default: assume successful if no explicit failure
    return True, None


def get_direction_info(payment):
    """Get direction icon and text from payment type"""
    payment_type = getattr(payment, 'payment_type', 'Unknown')

    if hasattr(payment, 'direction'):
        direction = payment.direction
        if direction == PaymentDirection.INCOMING:
            return "📥", "Received", True
        else:
            return "📤", "Sent", False
    else:
        # Fallback: check payment_type
        type_str = str(payment_type).upper()
        if 'RECEIVE' in type_str:
            return "📥", "Received", True
        else:
            return "📤", "Sent", False


def extract_description_from_payment(payment):
    """Extract description/memo from payment object - tries all possible locations

    Args:
        payment: Payment object from SDK

    Returns:
        Description string or empty string if not found
    """
    # DEBUG: Log the full payment structure
    import logging
    logger = logging.getLogger(__name__)

    logger.info(f"=== Payment Object Debug ===")
    logger.info(f"Payment type: {type(payment)}")
    payment_dir = [attr for attr in dir(payment) if not attr.startswith('_')]
    logger.info(f"Payment dir: {payment_dir}")

    # Log payment object attributes
    for attr in ['description', 'memo', 'note']:
        if hasattr(payment, attr):
            value = getattr(payment, attr)
            logger.info(f"payment.{attr} = {value}")

    details = getattr(payment, 'details', None)
    if details:
        logger.info(f"Details type: {type(details)}")
        details_dir = [attr for attr in dir(details) if not attr.startswith('_')]
        logger.info(f"Details dir: {details_dir}")

        # Log details object attributes
        for attr in dir(details):
            if not attr.startswith('_'):
                try:
                    value = getattr(details, attr)
                    if not callable(value):
                        logger.info(f"details.{attr} = {value}")
                except Exception as e:
                    logger.info(f"details.{attr} = <error: {e}>")

        # Try __dict__ if available
        if hasattr(details, '__dict__'):
            logger.info(f"Details __dict__: {details.__dict__}")

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


def build_queue_text(queue):
    """Live queue summary: every pending payment + totals."""
    total = 0
    fee_total = 0
    lines = ["\U0001f4e2 **Payment Queue**\n"]
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
        lines.append(f"{i}. `{dd}` \u2014 {amt_s} sats")
    lines.append(f"\n\u26fd Fees so far: {fee_total:,} sats")
    lines.append(f"\U0001f4b0 **Queue total: {total:,} sats**")
    return "\n".join(lines), total, fee_total


def payment_share_button(payment_id):
    """Share row: view the transaction on sparkscan.io explorer."""
    if not payment_id or payment_id == 'N/A':
        return []
    return [Button.url("🔍 View on Explorer", f"https://sparkscan.io/tx/{payment_id}")]


async def format_payment_details(payment, payment_id=None):
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
    direction_icon, direction_str, is_incoming = get_direction_info(payment)

    # Format timestamp
    time_str = format_timestamp(timestamp)

    # Status emoji
    status_icon, status_text = get_status_icon_and_text(status)

    # Format method
    method_str = str(method).replace('PaymentMethod.', '') if method else 'N/A'

    # Format amount with USD
    amount_str = await format_amount_with_usd(amount_sats)
    fee_str = await format_amount_with_usd(fees) if fees else "0 sats"

    # Extract description/memo from payment details
    description = extract_description_from_payment(payment)

    # Extract tx_id if present (for onchain transactions)
    tx_id_line = ""
    if details:
        if hasattr(details, 'tx_id') and details.tx_id:
            tx_id_line = f"🔗 **TX ID:**\n`{details.tx_id}`\n\n"

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
        f"🧾 ** TRANSACTION REPORT ** 🧾\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"**📋 GENERAL**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{direction_icon} **Type:** {direction_str}\n"
        f"💰 **Amount:** {amount_str}\n"
        f"⚡ **Fee:** {fee_str}\n"
        f"{status_icon} **Status:** {status_text}\n"
        f"🕐 **Time:** {time_str}\n"
        f"🏷️ **Payment Type:** {str(payment_type).replace('PaymentType.', '')}\n"
        f"📡 **Method:** {method_str}\n"
    )

    if description:
        msg += f"💬 **Description:** {description}\n"

    # ---- PROOF section ----
    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"**🔐 PROOF & HASHES**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    if tx_id_line:
        msg += f"🔗 **TX ID:**\n`{details.tx_id}`\n\n"
    if has_separate_hash:
        msg += f"#️⃣ **Payment Hash:**\n`{real_hash}`\n\n"
    if preimage:
        msg += f"🔑 **Preimage:**\n`{preimage}`\n\n"
    if not any([tx_id_line, has_separate_hash, preimage]):
        msg += "_No proof data available for this transaction._\n"

    # ---- DESTINATION / INVOICE section ----
    if invoice_str or dest_lines:
        msg += (
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"**📍 ROUTE**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        for dl in dest_lines:
            msg += dl + "\n"
        if invoice_str:
            # full invoice in ONE code block — copy button copies it whole
            inv_full = str(invoice_str)
            msg += f"⚡ **Invoice:**\n```{inv_full}```\n"

    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 **ID:**\n`{payment_id}`"
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

                    # Schedule notification on main event loop
                    asyncio.run_coroutine_threadsafe(
                        self._notify_payment_received(payment),
                        main_event_loop
                    )
            elif isinstance(event, SdkEvent.SYNCED):
                logging.debug(f"[{self.user_id}] Wallet synced")
        except Exception as e:
            logging.error(f"[{self.user_id}] Event handler error: {e}")

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
            amount_str = await format_amount_with_usd(amount)

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
                        sender_line = f"👤 From: {info['sender_info']}\n"
                        del pending_sender_info[self.user_id]
                        logging.info(f"[{self.user_id}] Added sender to notification: {info['sender_info']}")

            await client.send_message(
                self.user_id,
                f"⚡ **Payment Received!**\n\n"
                f"{sender_line}"
                f"💰 Amount: {amount_str}\n"
                f"🕐 Time: {time_str}\n"
                f"{f'💬 **Description:** {description}' if description else ''}"
                f"\n🔖 **ID:** `{payment_id}`"
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


def is_notifications_enabled(user_id):
    """Check if notifications are enabled for user (event listener active)"""
    return user_id in active_user_connections


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
            sdk = await connect(
                request=ConnectRequest(config=config, seed=seed, storage_dir=f"./.{user_id}")
            )

            # Create event listener for this user
            listener = UserEventListener(user_id)
            listener_id = await sdk.add_event_listener(listener=listener)

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
                    await sdk.remove_event_listener(id=listener_id)
                except Exception as e:
                    logging.debug(f"[{user_id}] Error removing listener: {e}")

            # Disconnect SDK
            if sdk:
                try:
                    await sdk.disconnect()
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


async def get_balance(user_id):
    """Get wallet balance for a user.

    Per SDK docs: get_info reads a local cache refreshed by background sync;
    ensure_synced=True only awaits the INITIAL post-connect sync (can hang
    minutes on fresh connections) — it is NOT a "force fresh sync". With the
    cached read + Synced-event refresh pattern this is correct for a bot.
    """
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        info = await sdk.get_info(request=GetInfoRequest(ensure_synced=False))
        balance_sats = info.balance_sats
        return str(balance_sats)
    except Exception as error:
        logging.error(f"Error getting balance: {error}")
        return "0"
    finally:
        if sdk:
            await close_wallet(sdk)


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
                await enable_notifications(user_id)
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
        sdk = await connect(
            request=ConnectRequest(config=config, seed=seed, storage_dir=f"./.{user_id}")
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
        await sdk.disconnect()
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
        response = await sdk.list_payments(request=ListPaymentsRequest())
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
            response = await sdk.get_payment(request=GetPaymentRequest(payment_id=search_id))
            if response and response.payment:
                logging.info(f"[get_payment_by_id] Found via get_payment: {search_id}")
                return response.payment
        except Exception as e:
            logging.debug(f"get_payment failed for {search_id}, trying list_payments: {e}")

        # Fallback: search in payment list
        response = await sdk.list_payments(request=ListPaymentsRequest())
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


def _format_fees(fees):
    """Human-readable recommended on-chain fees (clearly labeled)."""
    return (
        "⛽ **Recommended Network Fees**\n"
        "_(sat/vByte = satoshis per virtual byte; higher fee = faster confirmation)_\n\n"
        f"⚡ **Next block** (fastest): `{fees.fastest_fee}`  — ~10 min\n"
        f"🕒 **Half hour**: `{fees.half_hour_fee}`  — ~30 min\n"
        f"🕐 **Hour**: `{fees.hour_fee}`  — ~60 min\n"
        f"🐢 **Economy**: `{fees.economy_fee}`  — slower, cheaper\n"
        f"🔻 **Minimum**: `{fees.minimum_fee}`  — lowest a miner will accept"
    )


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

        prepare_response = await sdk.prepare_send_payment(request=request)

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
        kwargs = {}
        if locked_amount_sat is not None:
            kwargs['locked_amount_sat'] = locked_amount_sat
        resp = await sdk.buy_bitcoin(request=BuyBitcoinRequest.MOONPAY(locked_amount_sat=locked_amount_sat, redirect_url=None))
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


def get_main_buttons(user_id=None):
    """Get main menu buttons (translated per user)"""
    if user_id is None:
        user_id = 0
    return [
        [Button.inline(t(user_id, "btn_send"), b"send"), Button.inline(t(user_id, "btn_receive"), b"receive")],
        [Button.inline(t(user_id, "btn_history"), b"history"), Button.inline(t(user_id, "btn_refresh"), b"refresh")],
        [Button.inline(t(user_id, "btn_settings"), b"settings"), Button.inline(t(user_id, "btn_help"), b"help")],
        [Button.inline(t(user_id, "btn_tools"), b"more_tools")],
    ]


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
        sats_per_unit = 100_000_000 / fiat_rate  # Sats per 1 fiat unit
        # For low-value currencies (like IRT), show fiat per sat instead
        if sats_per_unit < 1:
            fiat_per_sat = fiat_rate / 100_000_000  # Fiat per 1 sat
            rate_info = f"\n{fiat_emoji} **1 sat ≈** {fiat_per_sat:,.0f} {fiat_code}"
        else:
            rate_info = f"\n{fiat_emoji} **1 {fiat_code} ≈** {int(sats_per_unit):,} sats"

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
        await event.answer("📜 Loading history...")

        payments = await get_payment_history(user_id, limit=50)

        if not payments:
            await event.edit(
                f"{t(user_id, 'hist_empty_title')}\n\n"
                f"{t(user_id, 'hist_empty')}",
                buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
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
            direction_icon, direction_str, is_incoming = get_direction_info(payment)

            # Format timestamp
            time_str = format_timestamp(timestamp)

            # Format amount
            if is_incoming:
                amount_str = f"+{amount}"
            else:
                amount_str = f"-{amount}"

            # Add USD value
            usd_str = ""
            if usd_rate:
                usd_value = sats_to_usd(int(amount), usd_rate)
                if usd_value is not None:
                    usd_str = f" (~${usd_value})"

            # Status emoji
            status_icon, status_text = get_status_icon_and_text(status)

            # Extract description/memo from payment details
            description = ""
            if details:
                if hasattr(details, 'description') and details.description:
                    description = f" 💬 {details.description[:30]}..." if len(details.description) > 30 else f" 💬 {details.description}"
                elif hasattr(details, 'memo') and details.memo:
                    description = f" 💬 {details.memo[:30]}..." if len(details.memo) > 30 else f" 💬 {details.memo}"

            # Check if transaction has tx_id
            tx_indicator = " 🔗" if hasattr(details, 'tx_id') and details.tx_id else ""

            # Get payment ID for tracking - create deep link
            payment_id = getattr(payment, 'id', None)
            if payment_id and bot_username:
                details_link = f"[Details](https://t.me/{bot_username}?start=dt_{payment_id})"
            elif payment_id:
                details_link = f"/dt_{payment_id}"
            else:
                details_link = "Details"

            history_text += (
                f"{direction_icon} **{direction_str}** {status_icon}{tx_indicator}\n"
                f"   💰 {amount_str} sats{usd_str}{description}\n"
                f"   📅 {time_str}\n"
                f"   🔍 {details_link}\n\n"
            )

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
            buttons=[[Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]]
        )
        logging.error(f"Transaction history error: {error}")


async def prepare_and_show_fee(event, user_id, invoice, amount):
    """Prepare payment and show fee confirmation with USD values"""
    try:
        res = await event.respond("⚡ Calculating fees...")

        prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, amount)

        user_data[user_id]['prepare_response'] = prepare_response
        user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)

        # Use spark_fee if available (even if 0), otherwise use fee_sats
        if spark_fee is not None:
            final_fee = spark_fee
        else:
            final_fee = fee_sats if fee_sats else 0

        user_data[user_id]['fee'] = final_fee
        total = amount + final_fee if amount else final_fee

        # Get USD values
        amount_usd = await format_amount_with_usd(amount) if amount else 'Variable'
        fee_usd = await format_amount_with_usd(final_fee)
        total_usd = await format_amount_with_usd(total)

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

            recipient_text = f"👤 To: @{ru['username']} ({ru['display_name']}) {bot_status}\n\n"

            if ru['is_channel'] or ru['is_group']:
                recipient_tips = "\n\n⚠️ _Group/Channel wallet - notify admin!_"
            elif not has_started:
                recipient_tips = "\n\n⚠️ _Recipient hasn't started bot - won't be notified!_"
            # If has_started is True, no warning needed

            # Anonymous sender option (default: show sender)
            is_anonymous = user_data[user_id].get('send_anonymous', False)
            if is_anonymous:
                anon_text = "👻 Sender: **Anonymous**"
                anon_btn = Button.inline("👤 Show My Name", b"toggle_send_anon")
            else:
                anon_text = "👤 Sender: **Visible**"
                anon_btn = Button.inline("👻 Send Anonymous", b"toggle_send_anon")

            recipient_text += f"{anon_text}\n\n"
            buttons.append([anon_btn])

        # Add password hint if user doesn't have one
        password_hint = ""
        if not has_send_password(user_id):
            password_hint = "\n💡 _Set a send password in Settings for security_"

        buttons.append([
            Button.inline("✅ Confirm", b"confirm_payment_yes"),
            Button.inline("❌ Cancel", b"back_to_menu")
        ])

        _q = (user_data.get(user_id) or {}).get('send_queue') or []
        _q_txt = (build_queue_text(_q)[0] + "\n\n" + "─" * 10 + "\n🔹 **New payment:**\n") if _q else ""
        await res.edit(
            _q_txt + f"📋 **Payment Summary**\n\n"
            f"{recipient_text}"
            f"💰 Amount: {amount_usd}\n"
            f"⚡ Fee: {fee_usd}\n"
            f"💳 Total: {total_usd}\n\n"
            f"Confirm payment?{recipient_tips}{password_hint}",
            buttons=buttons
        )

    except Exception as error:
        await res.edit(
            f"❌ Error preparing payment:\n{error}",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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
    text = event.text.strip()
    if ' ' in text:
        param = text.split(' ', 1)[1].strip()

        # Handle payment details deep link
        if param.startswith('dt_'):
            payment_id = param[3:]  # Remove 'dt_' prefix
            payment = await get_payment_by_id(user_id, payment_id)

            if payment:
                details_text = await format_payment_details(payment, payment_id)

                share_row = payment_share_button(payment_id)
                await event.respond(
                    details_text,
                    buttons=[
                        share_row if share_row else [],
                        [Button.inline("📜 History", b"history")],
                        [Button.inline("« Menu", b"back_to_menu")]
                    ]
                )
                return
            else:
                await event.respond(
                    f"❌ **Payment Not Found**\n\n"
                    f"ID: `{payment_id}`\n\n"
                    f"This payment may have expired.",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                )
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
        await event.respond("❌ Invalid payment ID")
        return

    payment_id = match.group(1).strip()

    # Get payment details
    payment = await get_payment_by_id(user_id, payment_id)

    if payment:
        details_text = await format_payment_details(payment, payment_id)
        share_row = payment_share_button(payment_id)

        btns = ([share_row] if share_row else []) + [
            [Button.inline("📜 History", b"history")],
            [Button.inline("« Menu", b"back_to_menu")]
        ]
        await event.respond(details_text, buttons=btns)
    else:
        await event.respond(
            f"❌ **Payment Not Found**\n\n"
            f"ID: `{payment_id}`\n\n"
            f"This payment may have expired.",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
        )


@client.on(events.CallbackQuery)
async def callback_handler(event):
    """Handle inline button callbacks"""
    user_id = event.sender_id
    data = event.data.decode()

    # Mark user as active on any interaction
    await mark_user_active(user_id)

    # Donation recipient user ID (configure this)
    DONATE_USER_ID = int(os.getenv('DONATE_USER_ID', '0'))

    try:
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
                "🧮 **Satoshi / Bitcoin / Fiat Calculator**\n\n"
                "Send an amount with a unit:\n\n"
                "• `5000 sats`  (or just `5000`)\n"
                "• `0.001 btc`\n"
                f"• `$10`  (in {fiat_code})\n\n"
                "I'll convert it to sats, BTC, and fiat.",
                buttons=[[Button.inline("« Cancel", b"more_tools")]])
            return

        if data == "contacts":
            await event.answer()
            await show_contacts(event, edit=True)
            return

        if data == "buy":
            await event.answer("🟧 Loading...")
            try:
                url = await buy_bitcoin_url(user_id)
                await event.edit(f"🟧 **Buy Bitcoin**\n\n{url}",
                                 buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]],
                                 link_preview=False)
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "fees":
            await event.answer("⛽ Loading...")
            try:
                fees = await get_recommended_fees(user_id)
                await event.edit(_format_fees(fees),
                                 buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "sync":
            await event.answer("🔄 Syncing...")
            try:
                info = await sync_user_wallet(user_id)
                await event.edit(
                    t(user_id, "synced", balance=f"{getattr(info, 'balance_sats', 0):,}"),
                    buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "signmsg_help":
            await event.answer()
            await event.edit(
                "✍️ **Sign / Verify Message**\n\nProve wallet ownership:\n\n"
                "• `/signmsg <message>` — sign\n"
                "• `/verifymsg <pubkey> <signature> <message>` — verify",
                buttons=[[Button.inline(t(user_id, "btn_back_tools"), b"more_tools")]])
            return

        if data == "del_lnaddress":
            # Confirmation step — accidental taps must not delete instantly
            await event.answer()
            await event.edit(
                "⚠️ **Delete Lightning Address?**\n\n"
                "This cannot be undone. Your LN address will be removed "
                "and others can't pay you via it anymore.",
                buttons=[
                    [Button.inline("🗑️ Yes, delete it", b"del_lnaddress_confirm")],
                    [Button.inline("« Cancel", b"change_ln_address")]
                ])
            return

        if data == "del_lnaddress_confirm":
            await event.answer("🗑️ Deleting...")
            try:
                await delete_lightning_address(user_id)
                await event.edit("✅ Lightning address deleted.",
                                 buttons=[[Button.inline("« Settings", b"settings")]])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline("« Settings", b"settings")]])
            return

        if data.startswith("contactpay_"):
            idx = data.split("_", 1)[1]
            cmap = (user_data.get(user_id) or {}).get('contact_map') or {}
            contact = cmap.get(idx)
            if not contact:
                await event.answer("Contact list expired — reopen Contacts.", alert=True)
                return
            user_data.setdefault(user_id, {})['contact_pay'] = {
                'name': contact['name'], 'identifier': contact['identifier']}
            user_steps[user_id] = "contact_pay_amount"
            await event.answer()
            await event.edit(
                f"💸 Send to **{contact['name']}**\n\nEnter amount in **sats**:",
                buttons=[[Button.inline("« Cancel", b"contacts")]])
            return

        if data.startswith("contactdel_"):
            idx = data.split("_", 1)[1]
            cmap = (user_data.get(user_id) or {}).get('contact_map') or {}
            contact = cmap.get(idx)
            if not contact:
                await event.answer("Contact list expired — reopen Contacts.", alert=True)
                return
            try:
                await delete_user_contact(user_id, contact['id'])
                await event.answer("🗑️ Deleted", alert=True)
            except Exception as de:
                await event.answer(f"Error: {de}", alert=True)
                return
            await show_contacts(event, edit=True)
            return

        if data == "confcontactpay":
            cp = (user_data.get(user_id) or {}).get('contact_pay') or {}
            prep = cp.get('prepare')
            if not prep:
                await event.answer("Session expired.", alert=True)
                return
            await event.answer("Sending...")
            try:
                await send_payment(user_id, prep)
            except Exception as se:
                user_steps.pop(user_id, None)
                await event.edit(f"❌ Payment failed: {se}",
                                 buttons=[[Button.inline("« Contacts", b"contacts")]])
                return
            name = cp.get('name', 'contact')
            user_steps.pop(user_id, None)
            (user_data.get(user_id) or {}).pop('contact_pay', None)
            await event.edit(
                f"✅ **Payment Sent!**\n\nSent to **{name}**.",
                buttons=[[Button.inline("« Menu", b"back_to_menu")]])
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
            await event.answer("✅ Refreshed")
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
            await event.answer("🔍 Loading...")

            logging.info(f"[track_] user={user_id} searching for payment_id={payment_id!r}")

            payment = await get_payment_by_id(user_id, payment_id)

            if payment:
                details_text = await format_payment_details(payment, payment_id)
                share_row = payment_share_button(payment_id)

                btns = ([share_row] if share_row else []) + [
                    [Button.inline("📜 History", b"history")],
                    [Button.inline("« Menu", b"back_to_menu")]
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
                    f"❌ **Payment Not Found**\n\n"
                    f"No payment found with ID:\n`{payment_id}`\n\n"
                    f"Make sure you entered the correct payment ID.",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                )
            return

        elif data == "exit_confirm":
            await event.answer("🚨 Signing...")
            ud = user_data.get(user_id) or {}
            prep = ud.get('exit_prepared')
            addr = ud.get('exit_address', '?')
            if not prep:
                await event.edit("❌ Prepared exit expired — start again.", buttons=None)
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
                    await event.edit("❌ No transactions produced.", buttons=None)
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
                lines = [f"✅ **Signed! {len(txs)} transaction(s) ready** — NOT broadcast yet.\n"]
                for i, x in enumerate(ud['exit_signed_txs'], 1):
                    kind_tag = x['kind'].split('.')[-1].rstrip('()') if x['kind'] else f"tx{i}"
                    status_tag = " ✅already on-chain (skip)" if 'Confirmed' in x['status'] else ""
                    lines.append(f"{i}. [{kind_tag}] `{x['txid'][:24]}…` ({len(x['hex'])//2}B){status_tag}")
                if n_confirmed:
                    lines.append(f"\nℹ️ {n_confirmed} step(s) already on-chain — those are free, skip them.")
                lines.append(f"\n💰 Value: {recov:,} sats \u2014 ⛽ Fee: {fee:,} sats")
                lines.append(f"🏠 To: `{addr}`\n")
                lines.append(
                    "📡 **How to broadcast (important):**\n"
                    "• Broadcast in the order above — each tx depends on the ones before it.\n"
                    "• Pairs: each tx with a `cpfp:` line must go WITH its CPFP child in one package "
                    "(`bitcoin-cli submitpackage '[\"<parent>\",\"<child>\"]'`). Public APIs reject "
                    "the zero-fee parent alone.\n"
                    "• Txs WITHOUT a cpfp line (fan-out / sweep) broadcast alone, anywhere "
                    "(e.g. mempool.space/api/tx).\n"
                    "• Some steps have timelocks — wait for them to mature before broadcasting "
                    "(can take days; that's normal for emergency exits).")
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
                import io as _io
                fname = f"zap-exit-signed-{user_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
                await client.send_file(event.chat_id, _io.BytesIO(hex_payload.encode()),
                                       force_document=True,
                                       attributes=[tl_types.DocumentAttributeFilename(fname)],
                                       caption="📝 Signed exit tx hex — broadcast manually from any explorer/node.")
                await event.edit("\n".join(lines), buttons=[
                    [Button.inline("« Exit Menu", b"emergency_exit")]
                ])
            except Exception as e:
                logging.error(f"exit sign: {e}")
                await event.edit(
                    f"❌ Signing failed: {e}\n\n"
                    f"💡 Use 💾 Export State instead — import it in a compatible wallet to recover.",
                    buttons=[[Button.inline("« Exit Menu", b"emergency_exit")]])
            return

        # (v34: exit_save_hex and exit_broadcast handlers removed — hex is now
        # auto-sent as a file at sign time, and broadcasting is user-side only.
        # NOTE from official docs: CPFP pairs need package relay
        # (bitcoin-cli submitpackage); mempool.space rejects zero-fee parents.)

        elif data == "emergency_exit":
            await event.answer()
            await event.edit(
                "🚨 **Emergency Exit** — last resort only\n\n"
                "Pulls your Spark balance on-chain **without** asking the "
                "Spark operators. Use it when Spark is down or you don't "
                "trust it anymore.\n\n"
                "**Before you start:**\n"
                "• Needs an on-chain UTXO of yours to pay the fees — a "
                "wallet that only holds Spark sats **cannot** exit.\n"
                "• Multi-step with timelocks — a full exit can take days.\n"
                "• You broadcast the transactions yourself (bot gives you "
                "the signed hex, never sends it).\n\n"
                "💡 Spark still working? **📤 Send → Bitcoin address** is "
                "cheaper and instant.",
                buttons=[
                    [Button.inline("💾 Export State backup", b"exit_export_state")],
                    [Button.inline("🚨 Quote my exit", b"exit_prepare")],
                    [Button.inline("« Settings", b"settings")]
                ])
            return

        elif data == "exit_export_state":
            await event.answer("💾 Exporting...")
            try:
                sdk = await get_wallet(user_id)
                resp = await sdk.export_unilateral_exit_state()
                state = getattr(resp, 'exit_state', None) or str(resp)
                fname = f"zap-exit-state-{user_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
                out = f"💾 **Exit State Backup**\n\nSave this somewhere safe.\n\n```{state[:3500]}```"
                if len(state) > 3500:
                    await client.send_file(event.chat_id, io.BytesIO(state.encode()),
                                           force_document=True,
                                           attributes=[tl_types.DocumentAttributeFilename(fname)],
                                           caption=f"💾 Unilateral exit state backup\n📄 {fname}")
                    out = "✅ State exported as a JSON file — store it safely."
                await event.edit(out, buttons=[[Button.inline("« Exit Menu", b"emergency_exit")]])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline("« Exit Menu", b"emergency_exit")]])
            return

        elif data == "exit_prepare":
            await event.answer()
            user_steps[user_id] = "exit_address_input"
            await event.edit(
                "🚨 **Force Withdraw to Onchain Address**\n\n"
                "Send the Bitcoin address that should receive your funds:\n"
                "(bc1... / 1... / 3...)\n\n"
                "⚠️ On-chain fees apply. Double-check the address!",
                buttons=[[Button.inline("« Cancel", b"emergency_exit")]])
            return

        elif data == "webhook_list":
            await event.answer("🔗 Loading webhooks...")
            try:
                sdk = await get_wallet(user_id)
                hooks = await sdk.list_webhooks() or []
                if not hooks:
                    txt = "🔗 **Webhooks**\n\nNone registered."
                else:
                    lines = ["🔗 **Your Webhooks**\n"]
                    for w in hooks:
                        lines.append(f"• `{getattr(w,'url','?')}`")
                    txt = "\n".join(lines)
                await event.edit(txt,
                    buttons=[
                        [Button.inline("➕ Register URL", b"webhook_add")],
                        [Button.inline("➖ Remove", b"webhook_del")],
                        [Button.inline(t(user_id, "btn_back_settings"), b"settings")]
                    ])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline(t(user_id, "btn_back_settings"), b"settings")]])
            return

        elif data == "webhook_add":
            await event.answer()
            user_steps[user_id] = "webhook_url_input"
            await event.edit(
                "➕ **Register Webhook**\n\n"
                "Send the HTTPS URL that should receive payment events:",
                buttons=[[Button.inline("« Cancel", b"webhook_list")]])
            return

        elif data == "webhook_del":
            await event.answer()
            user_steps[user_id] = "webhook_del_input"
            await event.edit(
                "➖ **Remove Webhook**\n\n"
                "Send the webhook ID (or its URL) to remove:",
                buttons=[[Button.inline("« Cancel", b"webhook_list")]])
            return

        elif data == "queue_clear":
            await event.answer()
            ud = user_data.get(user_id)
            if ud:
                ud.pop('send_queue', None)
                ud.pop('batch_prep', None)
            user_steps.pop(user_id, None)
            await event.edit("\U0001f5d1\ufe0f Queue cleared.",
                             buttons=[[Button.inline("\u00ab Main Menu", b"back_to_menu")]])
            return

        # (v34: legacy paste-queue handlers queue_send_all / queue_send_all_confirmed
        # removed — unreachable, superseded by interactive Batch Pay
        # (batch_send_all / batch_add_more). queue_clear kept: batch Cancel All uses it.)

        # v33: add another after batch started — ask next destination
        elif data == "batch_add_more":
            await event.answer()
            ud = user_data.get(user_id) or {}
            if not ud.get('batch_items'):
                await event.edit("❌ Batch is empty — start Batch Pay again.", buttons=None)
                return
            user_steps[user_id] = "batch_dest"
            await event.edit(
                f"📢 **Recipient {len(ud['batch_items']) + 1}** — send the destination:\n"
                f"(invoice / LN address / @username / btc address)",
                buttons=[[Button.inline("« Cancel", b"more_tools")]])
            return

        elif data == "batch_pay":
            await event.answer()
            user_data[user_id] = user_data.get(user_id) or {}
            user_data[user_id]['batch_items'] = []      # prepared payments
            user_data[user_id]['batch_pairs'] = []      # display lines
            user_steps[user_id] = "batch_dest"
            await event.edit(
                "📢 **Batch Pay** — pay many people at once\n\n"
                "**Recipient 1** \u2014 send me the destination:\n\n"
                "• Lightning invoice (`lnbc...`)\n"
                "• Lightning address (`name@domain`)\n"
                "• Telegram username (`@user`)\n"
                "• Bitcoin address (`bc1...`)\n\n"
                "If the invoice has no amount, I'll ask for it.",
                buttons=[[Button.inline("« Cancel", b"more_tools")]]
            )
            return

        elif data == "batch_send_all":
            await event.answer("🚀 Sending batch...")
            ud = user_data.get(user_id) or {}
            items = ud.pop('batch_items', None) or []
            pairs = ud.pop('batch_pairs', None) or []
            if not items:
                await event.edit("❌ Batch is empty.", buttons=None)
                return
            progress = await event.edit(f"⏳ Sending 0/{len(items)}...")
            ok = fail = 0
            total = 0
            bolt_opts = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
            for i, item in enumerate(items, 1):
                try:
                    kind = item['kind']
                    dest = item['dest']
                    sats = item['amount']
                    if kind == 'onchain':
                        prep, _, _ = await prepare_payment(user_id, dest, sats)
                        opts = SendPaymentOptions.BITCOIN_ADDRESS(confirmation_speed=OnchainConfirmationSpeed.MEDIUM)
                        await send_payment(user_id, prep, opts)
                    elif kind == 'lightning-address':
                        # LUD-16 resolve + prepare (same as add-time; do NOT use Telegram resolve)
                        import aiohttp as _ah
                        _u_, _dom = dest.split('@', 1)
                        async with _ah.ClientSession() as _sess:
                            async with _sess.get(f"https://{_dom}/.well-known/lnurlp/{_u_}",
                                                 timeout=_ah.ClientTimeout(total=15)) as _r:
                                if _r.status != 200:
                                    raise Exception(f"lookup failed (HTTP {_r.status})")
                                _j = await _r.json(content_type=None)
                        from breez_sdk_spark import LnurlPayRequestDetails, PrepareLnurlPayRequest
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
                        pr = await sdk.prepare_lnurl_pay(PrepareLnurlPayRequest(
                            amount=sats, pay_request=_det,
                            comment=None, validate_success_action_url=True))
                        await lnurl_pay(user_id, pr)
                    elif kind == 'invoice':
                        prep, _, _ = await prepare_payment(user_id, dest, sats)
                        await send_payment(user_id, prep, bolt_opts)
                    elif kind == 'lnurl-pay':
                        # LNURL-pay executes via lnurl_pay, NOT send_payment
                        await lnurl_pay(user_id, item['prepare_response'])
                    elif kind == 'spark':
                        prep, _, _ = await prepare_payment(user_id, dest, sats)
                        await send_payment(user_id, prep)
                    else:
                        raise Exception("unknown type")
                    ok += 1
                    total += sats
                except Exception as e:
                    logging.error(f"batch item {i} failed: {e}")
                    fail += 1
                try:
                    await progress.edit(f"⏳ Sent {i}/{len(items)} (ok {ok}, fail {fail})")
                except Exception:
                    pass
            user_steps.pop(user_id, None)
            done = ("✅ **Batch complete!**\n"
                    f"✅ Success: {ok}  ❌ Failed: {fail}\n"
                    f"💰 Total sent: {total:,} sats")
            await progress.edit(done, buttons=[[Button.inline("« Menu", b"back_to_menu")]])
            return

        elif data == "conversion_limits":
            await event.answer("📊 Checking limits...")
            try:
                sdk = await get_wallet(user_id)
                resp = await sdk.fetch_conversion_limits(FetchConversionLimitsRequest())
                await event.edit(
                    f"📊 **Conversion Limits**\n\n"
                    f"`{resp}`",
                    buttons=[[Button.inline("« Tools", b"more_tools")]])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline("« Tools", b"more_tools")]])
            return

        elif data == "refund_conversions":
            await event.answer("🔨 Refunding stuck conversions...")
            try:
                sdk = await get_wallet(user_id)
                resp = await sdk.refund_pending_conversions()
                n = len(getattr(resp, 'refunds', []) or [])
                await event.edit(
                    f"✅ Refund scan done — {n} pending conversion(s) refunded.",
                    buttons=[[Button.inline("« Tools", b"more_tools")]])
            except Exception as e:
                await event.edit(f"❌ {e}", buttons=[[Button.inline("« Tools", b"more_tools")]])
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
                    [Button.inline(t(user_id, "btn_language"), b"language_menu")],
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
                hint_text = f"\n\n💡 Hint: {hint}" if hint else ""
                await event.edit(
                    f"🔒 **Password Required**\n\n"
                    f"Enter your password to view backup seeds:{hint_text}",
                    buttons=[
                        [Button.inline("« Cancel", b"settings_back")]
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
                    f"🔐 **Your recovery phrase**\n\n"
                    f"`{seed}`\n\n"
                    f"📝 Write it down on paper.\n"
                    f"🚫 Never share it — anyone with these 12 words can "
                    f"steal your wallet.\n"
                    f"📸 Screenshots are risky (cloud backups).",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )

        elif data == "recovery":
            user_steps[user_id] = "recovery_input"
            await event.answer()
            await event.edit(
                "🔄 **Wallet Recovery**\n\n"
                "Enter your 12-word recovery phrase:\n"
                "(Separate words with spaces)\n\n"
                "⚠️ **Warning:** Your current wallet will be replaced!",
                buttons=[[Button.inline("« Cancel", b"settings_back")]]
            )

        elif data == "change_ln_address":
            try:
                address = await get_lightning_address(user_id)
                await event.answer()
                if address and getattr(address, 'lightning_address', None):
                    await event.edit(
                        f"⚡ **Your Lightning Address:**\n"
                        f"`{address.lightning_address}`\n\n"
                        f"🔗 **LNURL:**\n`{address.lnurl}`\n\n"
                        "Change or delete it below.",
                        buttons=[
                            [Button.inline("✏️ Change Address", b"confirm_change_ln_address")],
                            [Button.inline("🗑️ Delete Address", b"del_lnaddress")],
                            [Button.inline("« Settings", b"settings")]
                        ]
                    )
                else:
                    await event.edit(
                        "⚡ **Lightning Address**\n\n"
                        "You don't have one yet. Register a reusable address "
                        "(e.g. `yourname`) to receive payments easily.",
                        buttons=[
                            [Button.inline("✏️ Register Address", b"confirm_change_ln_address")],
                            [Button.inline("« Settings", b"settings")]
                        ]
                    )
            except Exception as error:
                await event.answer(f"❌ Error: {error}", alert=True)

        elif data == "confirm_change_ln_address":
            user_steps[user_id] = "change_ln_address_input"
            await event.edit(
                "✏️ **Change Lightning Address**\n\n"
                "Enter your desired username:\n"
                "(Only lowercase letters, numbers, and underscores)\n\n"
                "Example: `satoshi_wallet`\n\n"
                "⚠️ Note: Username must be unique!",
                buttons=[[Button.inline("« Cancel", b"settings_back")]]
            )

        # ==================== PRIVATE MODE ====================
        elif data == "toggle_private_mode":
            await event.answer("⏳ Updating...")
            current_mode = await get_private_mode(user_id)
            new_mode = not current_mode
            success = await set_private_mode(user_id, new_mode)

            if success:
                status_text = "enabled" if new_mode else "disabled"
                status_icon = "✅" if new_mode else "❌"
                privacy_note = "🔒 Your transactions are now private." if new_mode else "🔓 Your transactions are now visible."
                await event.edit(
                    f"👻 **Private Mode {status_icon}**\n\n"
                    f"Private mode has been **{status_text}**.\n\n"
                    f"{privacy_note}\n\n"
                    f"⚠️ Note: This setting affects how your payments are stored on Spark network.",
                    buttons=[[Button.inline("« Settings", b"settings")]]
                )
            else:
                await event.edit(
                    "❌ **Error**\n\n"
                    "Failed to update private mode. Please try again.",
                    buttons=[[Button.inline("« Settings", b"settings")]]
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

                buttons.append([Button.inline("« Settings", b"settings")])

                await event.edit(
                    f"💱 **Select Fiat Currency**\n\n"
                    f"Current: {current_emoji} **{current_fiat}**",
                    buttons=buttons
                )
            except Exception as error:
                logging.error(f"Error loading fiat currencies: {error}")
                await event.edit(
                    f"❌ Error loading currencies: {error}",
                    buttons=[[Button.inline("« Settings", b"settings")]]
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
                f"💱 **{t(user_id, 'btn_change_fiat')}**\n\n"
                f"{t(user_id, 'btn_change_fiat')}: {fiat_emoji} **{current_fiat}**",
                buttons=buttons
            )

        # ==================== UNCLAIMED DEPOSITS ====================
        elif data == "unclaimed_deposits":
            await event.answer()
            try:
                await event.edit("⏳ Checking unclaimed deposits...")
                sdk = await get_wallet(user_id)
                response = await sdk.list_unclaimed_deposits(ListUnclaimedDepositsRequest())
                deposits = response.deposits

                if not deposits:
                    await event.edit(
                        "📦 **Unclaimed Deposits**\n\n"
                        "✅ No unclaimed deposits found.",
                        buttons=[[Button.inline("« Settings", b"settings")]]
                    )
                    return

                text = "📦 **Unclaimed Deposits**\n\n"
                buttons = []
                for i, dep in enumerate(deposits):
                    amount_display = f"{dep.amount_sats:,} sats"
                    error_text = ""
                    if dep.claim_error:
                        if isinstance(dep.claim_error, DepositClaimError.MAX_DEPOSIT_CLAIM_FEE_EXCEEDED):
                            req_fee = getattr(dep.claim_error, 'required_fee_sats', None)
                            req_rate = getattr(dep.claim_error, 'required_fee_rate_sat_per_vbyte', None)
                            if req_rate:
                                error_text = f" ⚠️ Fee exceeded (need {req_rate} sat/vB)"
                            elif req_fee:
                                error_text = f" ⚠️ Fee exceeded (need {req_fee} sats)"
                            else:
                                error_text = " ⚠️ Fee exceeded"
                        elif isinstance(dep.claim_error, DepositClaimError.MISSING_UTXO):
                            error_text = " ❌ Missing UTXO"
                        elif isinstance(dep.claim_error, DepositClaimError.GENERIC):
                            msg = getattr(dep.claim_error, 'message', '')
                            error_text = f" ❌ {msg}" if msg else " ❌ Error"
                        else:
                            error_text = " ❌ Error"

                    text += f"**{i+1}.** `{dep.txid[:12]}...:{dep.vout}` — **{amount_display}**{error_text}\n"

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

                    buttons.append([Button.inline(f"⚡ Claim #{i+1} ({amount_display})", f"ucl_{i}".encode())])

                text += "\n**⚡ Claim** = swap to Lightning\n**↩️ Refund** = send to BTC address"
                buttons.append([Button.inline("« Settings", b"settings")])

                await event.edit(text, buttons=buttons)

            except Exception as e:
                logging.error(f"Error listing unclaimed deposits: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Settings", b"settings")]])

        elif data.startswith("ucl_") and not data.startswith("ucl_fee_") and not data.startswith("ucl_claim") and not data.startswith("ucl_refund"):
            # Show claim/refund options for a specific deposit by index
            await event.answer()
            try:
                idx = int(data.split("_")[1])
                ud = user_data.get(user_id, {})
                deposits_list = ud.get('unclaimed_list', [])

                if idx >= len(deposits_list):
                    await event.edit("❌ Deposit not found. Try again.", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])
                    return

                deposit = deposits_list[idx]
                # Store selected deposit for claim/refund
                ud['claim_txid'] = deposit['txid']
                ud['claim_vout'] = deposit['vout']

                amount_display = f"{deposit['amount_sats']:,} sats"

                await event.edit(
                    f"📦 **Deposit Details**\n\n"
                    f"💰 Amount: **{amount_display}**\n"
                    f"🔗 TXID: `{deposit['txid']}`\n"
                    f"📍 Vout: {deposit['vout']}\n\n"
                    f"Choose an action:",
                    buttons=[
                        [Button.inline("⚡ Claim to Lightning", b"ucl_claim")],
                        [Button.inline("↩️ Refund to BTC Address", b"ucl_refund")],
                        [Button.inline("« Back", b"unclaimed_deposits")]
                    ]
                )
            except Exception as e:
                logging.error(f"Error showing deposit details: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])

        elif data == "ucl_claim":
            # Claim deposit to Lightning
            await event.answer()
            try:
                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')
                if not txid:
                    await event.edit("❌ Session expired.", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])
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
                        f"⚡ **Claim Deposit**\n\n"
                        f"💰 Amount: **{amount_sats:,} sats**\n"
                        f"💸 Network Fee: **{req_fee:,} sats** ({req_rate} sat/vB)\n"
                        f"📥 You receive: **~{net_amount:,} sats**\n\n"
                        f"Confirm claim?",
                        buttons=[
                            [Button.inline("✅ Confirm Claim", b"ucl_fee_required")],
                            [Button.inline("✏️ Custom Fee", b"ucl_fee_custom")],
                            [Button.inline("« Back", b"unclaimed_deposits")]
                        ]
                    )
                else:
                    # No required fee info — show fee speed options
                    await event.edit("⏳ Getting network fees...")
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
                        f"⚡ **Claim Deposit**\n\n"
                        f"🔗 TXID: `{txid[:24]}...`\n\n"
                        f"Select network fee speed:",
                        buttons=[
                            [Button.inline(f"🚀 Fast — {rec_fees.fastest_fee} sat/vB", b"ucl_fee_fast")],
                            [Button.inline(f"⚡ Medium — {rec_fees.half_hour_fee} sat/vB", b"ucl_fee_medium")],
                            [Button.inline(f"🐢 Slow — {rec_fees.hour_fee} sat/vB", b"ucl_fee_slow")],
                            [Button.inline("✏️ Custom", b"ucl_fee_custom")],
                            [Button.inline("« Back", b"unclaimed_deposits")]
                        ]
                    )
            except Exception as e:
                logging.error(f"Error preparing claim: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])

        elif data == "ucl_refund":
            # Refund deposit to BTC address
            await event.answer()
            try:
                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')
                if not txid:
                    await event.edit("❌ Session expired.", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])
                    return

                ud['refund_txid'] = txid
                ud['refund_vout'] = vout
                user_steps[user_id] = "ucl_refund_address"

                await event.edit(
                    f"↩️ **Refund Deposit**\n\n"
                    f"🔗 TXID: `{txid[:24]}...`\n\n"
                    f"Enter the BTC address to refund to:",
                    buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]]
                )
            except Exception as e:
                logging.error(f"Error preparing refund: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])

        elif data.startswith("ucl_fee_"):
            await event.answer()
            try:
                fee_choice = data[len("ucl_fee_"):]
                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')

                if not txid:
                    await event.edit("❌ Session expired.", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])
                    return

                if fee_choice == "custom":
                    user_steps[user_id] = "ucl_custom_fee"
                    await event.edit(
                        "✏️ Enter custom fee rate in **sat/vB**:\n\n"
                        "Example: `10`",
                        buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]]
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
                        fee = MaxFee.FIXED(amount=req_fee)
                    else:
                        fee = MaxFee.RATE(sat_per_vbyte=10)
                else:
                    # Map to MaxFee from recommended fees
                    rec = ud.get('rec_fees', {})
                    if fee_choice == "fast":
                        fee = MaxFee.RATE(sat_per_vbyte=rec.get('fastest', 10))
                    elif fee_choice == "medium":
                        fee = MaxFee.RATE(sat_per_vbyte=rec.get('half_hour', 5))
                    else:  # slow (hour)
                        fee = MaxFee.RATE(sat_per_vbyte=rec.get('hour', 3))

                await event.edit("⏳ Claiming deposit...")
                sdk = await get_wallet(user_id)
                request = ClaimDepositRequest(txid=txid, vout=vout, max_fee=fee)
                result = await sdk.claim_deposit(request)

                amount_display = f"{result.payment.amount_sats:,} sats" if hasattr(result.payment, 'amount_sats') else "✅"
                await event.edit(
                    f"✅ **Deposit Claimed Successfully!**\n\n"
                    f"⚡ Payment received in Lightning wallet.\n"
                    f"💰 {amount_display}",
                    buttons=[[Button.inline("« Settings", b"settings")]]
                )

            except Exception as e:
                logging.error(f"Error claiming deposit: {e}")
                await event.edit(f"❌ Claim failed: {e}", buttons=[[Button.inline("« Try Again", b"unclaimed_deposits"), Button.inline("« Settings", b"settings")]])

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

            msg_content = (
                f"{t(user_id, 'receive_title')}\n\n"
                f"{t(user_id, 'balance_line', amount=balance_display)}\n\n"
            )
            if ln_address:
                msg_content += f"⚡ {t(user_id, 'your_ln_address')}:\n`{ln_address}`\n\n"
            if static_btc:
                msg_content += f"₿ {t(user_id, 'your_onchain_address')}:\n`{static_btc}`"

            msg_buttons = [
                [Button.inline(t(user_id, "btn_ln_invoice"), b"receive_lightning")],
                [Button.inline(t(user_id, "btn_onchain"), b"receive_onchain")],
                [Button.inline(t(user_id, "btn_exchange"), b"receive_exchange")],
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
                await event.edit(f"❌ {error}", buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])

        elif data == "receive_onchain":
            await event.answer()
            try:
                msg = await event.edit("⏳ Generating onchain address...")

                btc_address, receive_fee = await create_onchain_address(user_id)
                fee_display = await format_amount_with_usd(receive_fee)

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
                    f"❌ {error}",
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
                await event.edit(f"❌ {e}", buttons=[[Button.inline(t(user_id, "btn_back"), b"receive")]])

        elif data.startswith("rcv_coin_"):
            await event.answer()
            coin = data[9:]
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            if not currencies:
                await event.edit("Failed to load currencies.", buttons=[[Button.inline("« Back", b"receive")]])
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
                    f"**{ccy['name']} -> Lightning BTC**\n\n"
                    f"⚖️ Select rate type:",
                    buttons=[
                        [Button.inline("🔒 Fixed Rate (guaranteed)", b"rcv_fixed")],
                        [Button.inline("📈 Float Rate (market)", b"rcv_float")],
                        [Button.inline("« Back", b"receive_exchange")]
                    ]
                )
            elif len(networks) > 1:
                buttons = []
                for ccy in networks:
                    buttons.append([Button.inline(f"{ccy['name']}", f"rcv_net_{ccy['code']}".encode())])
                buttons.append([Button.inline("« Back", b"receive_exchange")])
                await event.edit(f"🌐 **Select Network for {coin}:**", buttons=buttons)
            else:
                await event.edit(f"{coin} is not available.", buttons=[[Button.inline("« Back", b"receive_exchange")]])

        elif data.startswith("rcv_net_"):
            await event.answer()
            ccy_code = data[8:]
            logging.info(f"ff.io: User selected network with code: {ccy_code}")
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            ccy_info = next((c for c in (currencies or []) if c['code'] == ccy_code), None)
            if not ccy_info:
                logging.warning(f"ff.io: Currency code {ccy_code} not found in currencies list")
                await event.edit("Currency not found.", buttons=[[Button.inline("« Back", b"receive_exchange")]])
                return
            logging.info(f"ff.io: Found currency info for {ccy_code}: {ccy_info.get('name')}")
            ud['ffio_from_ccy'] = ccy_code
            ud['ffio_from_name'] = ccy_info['name']
            ud['ffio_tag_type'] = ccy_info.get('tag')
            ud['ffio_direction'] = 'receive'
            user_data[user_id] = ud
            await event.edit(
                f"**{ccy_info['name']} -> Lightning BTC**\n\n"
                f"⚖️ Select rate type:",
                buttons=[
                    [Button.inline("🔒 Fixed Rate (guaranteed)", b"rcv_fixed")],
                    [Button.inline("📈 Float Rate (market)", b"rcv_float")],
                    [Button.inline("« Back", b"receive_exchange")]
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
                        minmax_text = f"\nMin: **{min_val}** | Max: **{max_val}**\n"
            except:
                pass

            await event.edit(
                f"**{from_name} -> Lightning BTC** [{rate_label}]\n"
                f"{minmax_text}\n"
                f"💰 Enter the amount to exchange:\n"
                f"Example: `0.1` or `100`",
                buttons=[[Button.inline("« Back", b"receive_exchange")]]
            )

        elif data.startswith("rcv_ex_confirm_"):
            # Double-click prevention for ff.io order creation
            ud = user_data.get(user_id, {})
            if ud.get('ffio_order_creating'):
                await event.answer("⏳ Order creation in progress...")
                return

            await event.answer()
            try:
                # Mark as creating to prevent double-clicks
                ud['ffio_order_creating'] = True
                user_data[user_id] = ud

                await event.edit("⏳ Creating exchange order...")
                from_ccy = ud.get('ffio_from_ccy')
                amount = ud.get('ffio_amount')

                if not from_ccy or not amount:
                    ud.pop('ffio_order_creating', None)
                    await event.edit("❌ Session expired. Please try again.", buttons=[[Button.inline("« Back", b"receive")]])
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
                        caption=(
                            f"🔄 **Exchange Order Created!**\n\n"
                            f"🆔 Order: `{order_id}`\n"
                            f"📤 Send: **{deposit_amount} {from_name}**\n"
                            f"📥 Receive: **~{receive_amount} BTC** (Lightning)\n\n"
                            f"📋 **Deposit Address:**\n`{deposit_address}`\n"
                            + (f"🏷 **Memo/Tag:** `{deposit_tag}`\n" if deposit_tag else "")
                            + f"\n⏰ Expires in: {expiry_text}\n\n"
                            f"⚠️ Send **exactly** {deposit_amount} {from_name} to the address above."
                            + (f"\n⚠️ **Don't forget to include the Memo/Tag!**" if deposit_tag else "")
                            + f"\n✅ Your Lightning wallet will be credited automatically!"
                        ),
                        buttons=[
                            [Button.inline("🔍 Check Status", f"ffio_st_{order_id}".encode())],
                        ]
                    )
                else:
                    error_msg = result.get('msg', 'Unknown error') if result else 'API error'
                    await event.edit(
                        f"❌ **Exchange Error:**\n{error_msg}",
                        buttons=[[Button.inline("« Try Again", b"receive_exchange")], [Button.inline("« Back", b"receive")]]
                    )

                # Clean up user state
                user_steps.pop(user_id, None)

            except Exception as e:
                logging.error(f"Error creating ff.io receive order: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"receive")]])

        # ==================== SEND EXCHANGE (ff.io) ====================
        elif data == "send_exchange":
            await event.answer()
            try:
                balance = await get_balance(user_id)
                balance_display = await format_balance_with_usd(balance)
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
                await event.edit(f"Error: {e}", buttons=[[Button.inline("« Back", b"send")]])

        elif data.startswith("snd_coin_"):
            await event.answer()
            coin = data[9:]
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            if not currencies:
                await event.edit("Failed to load currencies.", buttons=[[Button.inline("« Back", b"send")]])
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
                    f"**Lightning BTC -> {ccy['name']}**\n\n"
                    f"⚖️ Select rate type:",
                    buttons=[
                        [Button.inline("🔒 Fixed Rate (guaranteed)", b"snd_fixed")],
                        [Button.inline("📈 Float Rate (market)", b"snd_float")],
                        [Button.inline("« Back", b"send_exchange")]
                    ]
                )
            elif len(networks) > 1:
                buttons = []
                for ccy in networks:
                    buttons.append([Button.inline(f"{ccy['name']}", f"snd_net_{ccy['code']}".encode())])
                buttons.append([Button.inline("« Back", b"send_exchange")])
                await event.edit(f"🌐 **Select Network for {coin}:**", buttons=buttons)
            else:
                await event.edit(f"{coin} is not available for sending.", buttons=[[Button.inline("« Back", b"send_exchange")]])

        elif data.startswith("snd_net_"):
            await event.answer()
            ccy_code = data[8:]
            ud = user_data.get(user_id, {})
            currencies = ud.get('ffio_currencies') or await ffio_get_currencies()
            ccy_info = next((c for c in (currencies or []) if c['code'] == ccy_code), None)
            if not ccy_info:
                await event.edit("Currency not found.", buttons=[[Button.inline("« Back", b"send_exchange")]])
                return
            ud['ffio_to_ccy'] = ccy_code
            ud['ffio_to_name'] = ccy_info['name']
            ud['ffio_tag_type'] = ccy_info.get('tag')
            ud['ffio_direction'] = 'send'
            user_data[user_id] = ud
            await event.edit(
                f"**Lightning BTC -> {ccy_info['name']}**\n\n"
                f"⚖️ Select rate type:",
                buttons=[
                    [Button.inline("🔒 Fixed Rate (guaranteed)", b"snd_fixed")],
                    [Button.inline("📈 Float Rate (market)", b"snd_float")],
                    [Button.inline("« Back", b"send_exchange")]
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
                            min_sats = int(float(min_val) * 100_000_000) if min_val else ''
                            max_sats = int(float(max_val) * 100_000_000) if max_val else ''
                            minmax_text = f"\nMin: **{min_sats:,} sats** | Max: **{max_sats:,} sats**"
                            ud['ffio_min_sats'] = min_sats
                            ud['ffio_max_sats'] = max_sats
                        except:
                            minmax_text = f"\nMin: **{min_val} BTC** | Max: **{max_val} BTC**"
            except:
                pass

            await event.edit(
                f"**Lightning BTC -> {to_name}** [{rate_label}]{minmax_text}\n\n"
                f"📋 Enter your wallet address:{tag_note}",
                buttons=[[Button.inline("« Back", b"send_exchange")]]
            )

        elif data == "ffio_skip_tag":
            await event.answer()
            ud = user_data.get(user_id, {})
            user_steps[user_id] = "ffio_send_amount"
            to_name = ud.get('ffio_to_name', ud.get('ffio_to_ccy', '?'))
            address = ud.get('ffio_to_address', '?')
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance)

            min_sats = ud.get('ffio_min_sats', '')
            max_sats = ud.get('ffio_max_sats', '')
            limit_text = ""
            if min_sats or max_sats:
                limit_text = f"Min: **{min_sats:,} sats** | Max: **{max_sats:,} sats**\n"

            await event.edit(
                f"**Exchange Lightning BTC -> {to_name}**\n\n"
                f"Balance: {balance_display}\n"
                f"Address: `{address[:30]}...`\n"
                f"{limit_text}\n"
                f"💰 Enter amount in **sats** to exchange:\n"
                f"Example: `50000` or `$10`",
                buttons=[
                    [Button.inline("💸 Send All", b"ffio_send_all")],
                    [Button.inline("« Cancel", b"send_exchange")]
                ]
            )

        elif data.startswith("snd_ex_confirm_") and not data.startswith("snd_ex_confirm_n"):
            await event.answer()
            try:
                await event.edit("⏳ Creating exchange order...")
                ud = user_data.get(user_id, {})
                to_ccy = ud.get('ffio_to_ccy')
                to_address = ud.get('ffio_to_address')
                amount_sats = ud.get('ffio_amount_sats')
                is_send_all = ud.get('ffio_send_all', False)

                if not to_ccy or not to_address or not amount_sats:
                    await event.edit("❌ Session expired. Please try again.", buttons=[[Button.inline("« Back", b"send")]])
                    return

                # For send-all: deduct estimated fee reserve BEFORE creating order
                # so only one order is created on ff.io
                if is_send_all:
                    balance = int(await get_balance(user_id))
                    # Reserve 1% of balance for fee (min 10 sats)
                    fee_reserve = max(10, int(balance * 0.01))
                    amount_sats = balance - fee_reserve
                    if amount_sats <= 0:
                        await event.edit(
                            f"❌ Insufficient balance after fee reserve.\n\n"
                            f"💰 Balance: **{balance:,} sats**",
                            buttons=[[Button.inline("« Back", b"send_exchange")]]
                        )
                        return
                    min_sats = ud.get('ffio_min_sats')
                    if min_sats and amount_sats < min_sats:
                        await event.edit(
                            f"❌ Amount after fee reserve is below minimum.\n\n"
                            f"💰 After fee: **{amount_sats:,} sats**\n"
                            f"📉 Minimum: **{min_sats:,} sats**",
                            buttons=[[Button.inline("« Back", b"send_exchange")]]
                        )
                        return
                    ud['ffio_amount_sats'] = amount_sats

                amount_btc = amount_sats / 100_000_000

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

                        fee_display = await format_amount_with_usd(fee_sats or 0)
                        amount_display = await format_amount_with_usd(amount_sats)

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
                            fee_note = "\n⚠️ Fee deducted from balance (Send All)"

                        await event.edit(
                            f"🔄 **Exchange Order Created!**\n\n"
                            f"🆔 Order: `{order_id}`\n"
                            f"📤 Send: **{deposit_amount} BTC** (Lightning)\n"
                            f"📥 Receive: **~{receive_amount} {to_ccy}**\n"
                            f"💸 Network Fee: {fee_display}{fee_note}\n\n"
                            f"⚡ Confirm to send Lightning payment to ff.io?",
                            buttons=[
                                [Button.inline("✅ Confirm & Send", f"ffio_pay_{order_id}".encode())],
                                [Button.inline("❌ Cancel", b"back_to_menu")]
                            ]
                        )
                    except Exception as e:
                        await event.edit(
                            f"❌ **Payment Error:**\n{e}",
                            buttons=[[Button.inline("« Back", b"send_exchange")]]
                        )
                else:
                    error_msg = result.get('msg', 'Unknown error') if result else 'API error'
                    await event.edit(
                        f"❌ **Exchange Error:**\n{error_msg}",
                        buttons=[[Button.inline("« Try Again", b"send_exchange")], [Button.inline("« Back", b"send")]]
                    )

                user_steps.pop(user_id, None)
                ud.pop('ffio_send_all', None)

            except Exception as e:
                logging.error(f"Error creating ff.io send order: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"send")]])

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
                await event.edit("❌ No balance to send.", buttons=[[Button.inline("« Back", b"send_exchange")]])
                return
            min_sats = ud.get('ffio_min_sats')
            if min_sats and amount_sats < min_sats:
                await event.edit(
                    f"❌ Balance too low for minimum.\n\n"
                    f"Minimum: **{min_sats:,} sats**\n"
                    f"Your balance: **{balance:,} sats**",
                    buttons=[[Button.inline("« Back", b"send_exchange")]]
                )
                return
            ud['ffio_send_all'] = True
            ud['ffio_amount_sats'] = amount_sats
            amount_btc = amount_sats / 100_000_000
            try:
                rate_type = ud.get('ffio_rate_type', 'float')
                price_result = await ffio_get_price("BTCLN", to_ccy, amount_btc, rate_type=rate_type)
                if price_result and price_result.get('code') == 0 and price_result.get('data'):
                    receive_amount = price_result['data'].get('to', {}).get('amount', '?')
                    rate = price_result['data'].get('to', {}).get('rate', '?')
                    amount_display = await format_amount_with_usd(amount_sats)
                    await event.edit(
                        f"🔄 **Exchange Quote (Send All)**\n\n"
                        f"📤 Send: **{amount_display}**\n"
                        f"📥 Receive: **~{receive_amount} {to_ccy}**\n"
                        f"📊 Rate: {rate}\n\n"
                        f"⚠️ Network fee will be deducted from send amount (like normal withdrawal).\n"
                        f"Proceed?",
                        buttons=[
                            [Button.inline("✅ Confirm & Create Order", b"snd_ex_confirm_yes")],
                            [Button.inline("❌ Cancel", b"send_exchange")]
                        ]
                    )
                else:
                    error_msg = price_result.get('msg', 'Unknown error') if price_result else 'API error'
                    await event.edit(f"❌ Quote Error: {error_msg}", buttons=[[Button.inline("« Back", b"send_exchange")]])
            except Exception as e:
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"send_exchange")]])

        elif data.startswith("ffio_pay_"):
            # Double-click prevention for ff.io payment
            order_info = pending_ffio_orders.get(user_id)
            if order_info and order_info.get('payment_processing'):
                await event.answer("⏳ Payment already in progress...")
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
                hint_text = f"\n\n💡 Hint: {hint}" if hint else ""
                await event.edit(
                    f"🔒 **Password Required**\n\n"
                    f"Enter your send password to confirm this payment:{hint_text}",
                    buttons=[[Button.inline("❌ Cancel", b"send_exchange")]]
                )
                # Store message ID for later editing
                user_data[user_id]['password_prompt_msg_id'] = event.message_id
                return

            await event.answer()
            try:
                if not order_info or order_info['id'] != order_id:
                    await event.edit("❌ Order not found.", buttons=[[Button.inline("« Menu", b"back_to_menu")]])
                    return

                # Mark as processing to prevent double-clicks
                order_info['payment_processing'] = True

                await event.edit("⏳ Sending Lightning payment to ff.io...")

                prepare_response = order_info.get('prepare_response')
                if not prepare_response:
                    await event.edit("❌ Payment session expired.", buttons=[[Button.inline("« Menu", b"back_to_menu")]])
                    return

                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
                payment = await send_payment(user_id, prepare_response, options)

                if payment:
                    # Check if payment is truly successful
                    is_success, failure_reason = is_payment_successful(payment)

                    if is_success:
                        to_ccy = order_info.get('to_ccy', '?')
                        await event.edit(
                            f"✅ **Lightning payment sent to ff.io!**\n\n"
                            f"🆔 Order: `{order_id}`\n"
                            f"📥 You will receive **{to_ccy}** at your address.\n\n"
                            f"⏳ Exchange is processing. You'll be notified when done.",
                            buttons=[
                                [Button.inline("🔍 Check Status", f"ffio_st_{order_id}".encode())],
                            ]
                        )
                    else:
                        await event.edit(
                            f"❌ **Payment Failed**\n\n"
                            f"🆔 Order: `{order_id}`\n"
                            f"Reason: {failure_reason}",
                            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                        )
                else:
                    await event.edit(
                        "❌ Payment failed.",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                    )
            except Exception as e:
                logging.error(f"Error paying ff.io order: {e}")
                await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Menu", b"back_to_menu")]])

        elif data.startswith("ffio_st_"):
            await event.answer("Checking order status...")
            try:
                order_id = data[8:]  # after "ffio_st_"
                # Get token from tracked orders
                order_info = pending_ffio_orders.get(user_id, {})
                token = order_info.get('token', '')

                if not token:
                    await event.answer("Order session expired. Cannot refresh.", alert=True)
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
                        f"🔍 **Order Status**\n\n"
                        f"🆔 Order: `{order_id}`\n"
                        f"{icon} Status: **{status}**\n\n"
                        f"📤 From: {from_amt} {from_name}\n"
                        f"📥 To: {to_amt} {to_name}\n"
                    )

                    # Time info
                    time_info = order.get('time', {})
                    left = time_info.get('left')
                    if left and int(left) > 0:
                        text += f"\n⏰ Time left: {int(left)//60}m {int(left)%60}s\n"

                    if from_info.get('tx', {}).get('id'):
                        tx_id = from_info['tx']['id']
                        text += f"\n🔗 TX: `{tx_id}`"

                    confs = from_info.get('tx', {}).get('confirmations')
                    req_confs = from_info.get('reqConfirmations')
                    if confs is not None and req_confs:
                        text += f"\n🔢 Confirmations: {confs}/{req_confs}"

                    buttons = []
                    if status not in ('DONE', 'EXPIRED'):
                        buttons.append([Button.inline("🔄 Refresh", f"ffio_st_{order_id}".encode())])

                    msg = await event.get_message()
                    if msg and msg.media:
                        # Message has QR/media - don't delete it, send status as new message
                        await client.send_message(event.chat_id, text, buttons=buttons if buttons else None)
                    else:
                        try:
                            await event.edit(text, buttons=buttons if buttons else None)
                        except Exception:
                            await event.answer("Status unchanged.", alert=False)
                else:
                    error_msg = result.get('msg', 'No data') if result else 'API error'
                    await event.answer(f"Error: {error_msg}", alert=True)
            except Exception as e:
                await event.answer(f"Error: {e}", alert=True)

        elif data == "receive_add_memo":
            await event.answer()
            # remember existing invoice state so Set Memo can come back to it
            user_data[user_id] = user_data.get(user_id) or {}
            user_steps[user_id] = "receive_memo"
            msg_content = (
                "💬 **Add Memo**\n\n"
                "Enter a description/memo for your invoice:"
            )
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
                await event.edit(f"❌ Error: {error}")

        elif data == "receive_set_amount":
            await event.answer()
            user_steps[user_id] = "receive_amount"

            # Get USD rate for hint
            usd_rate = await get_usd_rate()
            hint = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                hint = f"\n\n💡 Tip: $1 = {sats_per_dollar:,} sats, $5 = {usd_to_sats(5, usd_rate):,} sats"

            msg_content = (
                f"💵 **Set Invoice Amount**\n\n"
                f"Enter the amount in sats:\n"
                f"Or use $ prefix for USD (e.g., $5){hint}"
            )
            msg_buttons = [[Button.inline("« Cancel", b"receive_lightning")]]
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
                hint = f"\n\n💡 Tip: $1 = {sats_per_dollar:,} sats"

            msg_content = (
                f"💵 **Set Invoice Amount**\n\n"
                f"Enter the amount in sats:\n"
                f"Or use $ prefix for USD (e.g., $5){hint}"
            )
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
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
            )

        # Toggle anonymous sender for username payments
        elif data == "toggle_send_anon":
            await event.answer()
            if user_id not in user_data:
                await event.answer("Session expired", alert=True)
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

            amount_usd = await format_amount_with_usd(amount) if amount else 'Variable'
            fee_usd = await format_amount_with_usd(fee)
            total_usd = await format_amount_with_usd(total)

            # Show bot status
            bot_status = "✅" if has_started else "⚠️"
            recipient_text = f"👤 To: @{ru['username']} ({ru['display_name']}) {bot_status}\n\n"

            is_anonymous = user_data[user_id].get('send_anonymous', False)
            if is_anonymous:
                anon_text = "👻 Sender: **Anonymous**"
                anon_btn = Button.inline("👤 Show My Name", b"toggle_send_anon")
            else:
                anon_text = "👤 Sender: **Visible**"
                anon_btn = Button.inline("👻 Send Anonymous", b"toggle_send_anon")

            recipient_text += f"{anon_text}\n\n"

            if ru['is_channel'] or ru['is_group']:
                recipient_tips = "\n\n⚠️ _Group/Channel wallet - notify admin!_"
            elif not has_started:
                recipient_tips = "\n\n⚠️ _Recipient hasn't started bot - won't be notified!_"
            else:
                recipient_tips = ""

            password_hint = ""
            if not has_send_password(user_id):
                password_hint = "\n💡 _Set a send password in Settings for security_"

            buttons = [
                [anon_btn],
                [
                    Button.inline("✅ Confirm", b"confirm_payment_yes"),
                    Button.inline("❌ Cancel", b"back_to_menu")
                ],
            ]

            _q = (user_data.get(user_id) or {}).get('send_queue') or []
            _q_txt = (build_queue_text(_q)[0] + "\n\n" + "─" * 10 + "\n🔹 **New payment:**\n") if _q else ""
            await event.edit(
                _q_txt + f"📋 **Payment Summary**\n\n"
                f"{recipient_text}"
                f"💰 Amount: {amount_usd}\n"
                f"⚡ Fee: {fee_usd}\n"
                f"💳 Total: {total_usd}\n\n"
                f"Confirm payment?{recipient_tips}{password_hint}",
                buttons=buttons
            )
            return

        # Toggle anonymous sender for send all username payments
        elif data == "toggle_send_anon_all":
            await event.answer()
            if user_id not in user_data:
                await event.answer("Session expired", alert=True)
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

            balance_display = await format_balance_with_usd(original_balance)
            fee_display = await format_amount_with_usd(fee)
            amount_display = await format_amount_with_usd(amount)
            total_display = await format_amount_with_usd(total)

            bot_status = "✅" if has_started else "⚠️"
            recipient_text = f"👤 To: @{ru['username']} ({ru['display_name']}) {bot_status}\n\n"

            is_anonymous = user_data[user_id].get('send_anonymous', False)
            if is_anonymous:
                anon_text = "👻 Sender: **Anonymous**"
                anon_btn = Button.inline("👤 Show My Name", b"toggle_send_anon_all")
            else:
                anon_text = "👤 Sender: **Visible**"
                anon_btn = Button.inline("👻 Send Anonymous", b"toggle_send_anon_all")

            recipient_text += f"{anon_text}\n\n"

            buttons = [
                [anon_btn],
                [
                    Button.inline("✅ Confirm", b"confirm_payment_yes"),
                    Button.inline("❌ Cancel", b"back_to_menu")
                ],
            ]

            await event.edit(
                f"⚡ **Send All - Lightning**\n\n"
                f"{recipient_text}"
                f"💰 Balance: {balance_display}\n"
                f"⚡ Fee: {fee_display}\n"
                f"💸 Amount to send: {amount_display}\n"
                f"💳 Total: {total_display}\n\n"
                f"⚠️ Fee will be deducted from your balance\n\n"
                f"Confirm?",
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
                try:
                    await msg.delete()
                except:
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
                hint_text = f"💡 Current Hint: {hint}" if hint else "💡 No hint set"
                await event.edit(
                    f"🔒 **Send Password**\n\n"
                    f"You have a send password set.\n"
                    f"{hint_text}\n\n"
                    f"What would you like to do?",
                    buttons=[
                        [Button.inline("✏️ Change Password", b"change_send_password")],
                        [Button.inline("💡 Set/Change Hint", b"set_password_hint")],
                        [Button.inline("🗑️ Remove Password", b"remove_send_password")],
                        [Button.inline("❓ Forgot Password?", b"forgot_password")],
                        [Button.inline("« Cancel", b"settings_back")]
                    ]
                )
            else:
                # No password - prompt to set one
                user_steps[user_id] = "set_send_password"
                await event.edit(
                    "🔒 **Set Send Password**\n\n"
                    "Enter a password that will be required when sending payments.\n\n"
                    "⚠️ This adds an extra layer of security.\n"
                    "💡 Choose a password you can remember!",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
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
                f"🔒 **Change Send Password**\n\n"
                f"Enter your current password:{hint_text}",
                buttons=[[Button.inline("« Cancel", b"settings_back")]]
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
                f"🔒 **Remove Send Password**\n\n"
                f"Enter your current password to confirm removal:{hint_text}",
                buttons=[[Button.inline("« Cancel", b"settings_back")]]
            )
            # Store the prompt message ID for later editing
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['password_prompt_msg_id'] = event.message_id

        elif data == "set_password_hint":
            await event.answer()
            user_steps[user_id] = "set_password_hint"
            current_hint = get_password_hint(user_id)
            current_text = f"\n\nCurrent hint: {current_hint}" if current_hint else ""
            await event.edit(
                f"💡 **Set Password Hint**\n\n"
                f"Enter a hint to help you remember your password.\n"
                f"This hint will be shown when password is required.{current_text}\n\n"
                f"Example hints:\n"
                f"• \"My cat's name\"\n"
                f"• \"Birthday + favorite number\"\n"
                f"• \"First car model\"",
                buttons=[[Button.inline("« Cancel", b"set_send_password")]]
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
                        f"⏳ **Password Reset Pending**\n\n"
                        f"You already requested a password reset.\n\n"
                        f"⏱ Time remaining: **{time_str}**\n\n"
                        f"Your password will be automatically removed when the timer ends.",
                        buttons=[
                            [Button.inline("❌ Cancel Reset", b"cancel_password_reset")],
                            [Button.inline("« Settings", b"settings_back")]
                        ]
                    )
                    return
                else:
                    # Time is up - reset password now!
                    remove_send_password(user_id)
                    cancel_password_reset(user_id)
                    await event.edit(
                        "🔓 **Password Reset Complete**\n\n"
                        "Your send password has been removed.\n\n"
                        "You can set a new password in Settings.",
                        buttons=[[Button.inline("⚙️ Settings", b"settings")]]
                    )
                    return

            hint_text = f"💡 Your hint: **{hint}**\n\n" if hint else ""
            await event.edit(
                f"🔒 **Forgot Password?**\n\n"
                f"{hint_text}"
                f"If you can't remember your password, you can request a reset.\n\n"
                f"⚠️ **Important:**\n"
                f"• Password will be removed after **{PASSWORD_RESET_DAYS} days**\n"
                f"• You can cancel the reset anytime\n"
                f"• This is for security to prevent unauthorized access",
                buttons=[
                    [Button.inline("🔄 Try Again", b"backup")],
                    [Button.inline("🔓 Request Reset (3 days)", b"request_password_reset")],
                    [Button.inline("« Settings", b"settings_back")]
                ]
            )

        elif data == "request_password_reset":
            await event.answer()
            request_password_reset(user_id)
            reset_at = datetime.now() + timedelta(days=PASSWORD_RESET_DAYS)
            await event.edit(
                f"✅ **Reset Requested**\n\n"
                f"Your password will be automatically removed on:\n"
                f"📅 **{reset_at.strftime('%Y-%m-%d %H:%M')}**\n\n"
                f"You can cancel this request anytime before the deadline.",
                buttons=[
                    [Button.inline("❌ Cancel Reset", b"cancel_password_reset")],
                    [Button.inline("« Settings", b"settings_back")]
                ]
            )

        elif data == "cancel_password_reset":
            await event.answer()
            cancel_password_reset(user_id)
            await event.edit(
                "✅ **Reset Cancelled**\n\n"
                "Your password reset request has been cancelled.\n"
                "Your password remains active.",
                buttons=[[Button.inline("« Settings", b"settings_back")]]
            )

        elif data == "skip_password_hint":
            await event.answer()
            if user_id in user_steps:
                del user_steps[user_id]
            await event.edit(
                "✅ **Setup Complete!**\n\n"
                "Your send password is now active.\n\n"
                "You'll need this password to:\n"
                "• Send payments\n"
                "• View backup seeds\n\n"
                "💡 You can add a hint later from Settings > Password > Set Hint",
                buttons=[[Button.inline("« Settings", b"settings_back")]]
            )

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
                usd_info = (
                    f"\n\n💵 In USD terms:\n"
                    f"• 1,000 sats = ${sats_to_usd(1000, usd_rate):.2f}\n"
                    f"• 5,000 sats = ${sats_to_usd(5000, usd_rate):.2f}\n"
                    f"• 10,000 sats = ${sats_to_usd(10000, usd_rate):.2f}\n"
                    f"• 21,000 sats = ${sats_to_usd(21000, usd_rate):.2f}"
                )

            msg = await event.edit(
                t(user_id, 'donate_amount_title') + usd_info,
                buttons=[[Button.inline(t(user_id, "btn_cancel"), b"back_to_menu")]]
            )
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['last_message_id'] = msg.id

        elif data == "scan_payment":
            await event.answer()
            user_steps[user_id] = "scan_payment_input"
            msg = await event.edit(
                "🔍 **Scan Payment**\n\n"
                "Enter a payment ID to look up transaction details.\n\n"
                "Payment IDs look like:\n"
                "`019b492e...`\n\n"
                "Paste the payment ID below:",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
            )
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['last_message_id'] = msg.id

        elif data.startswith("confirm_payment_"):
            # Double-click prevention: check if payment is already being processed
            if user_id in user_data and user_data[user_id].get('payment_processing'):
                await event.answer("⏳ Payment already in progress...")
                return

            if user_id in user_data and 'prepare_response' in user_data[user_id]:
                # Check if user has send password and hasn't verified it yet
                if has_send_password(user_id) and not user_data[user_id].get('password_verified'):
                    await event.answer()
                    user_steps[user_id] = "verify_send_password_for_payment"
                    hint = get_password_hint(user_id)
                    hint_text = f"\n\n💡 Hint: {hint}" if hint else ""
                    await event.edit(
                        f"🔒 **Password Required**\n\n"
                        f"Enter your send password to confirm this payment:{hint_text}",
                        buttons=[[Button.inline("❌ Cancel", b"back_to_menu")]]
                    )
                    # Store message ID for later editing
                    user_data[user_id]['password_prompt_msg_id'] = event.message_id
                    return

                try:
                    # Mark payment as processing IMMEDIATELY to prevent double-clicks
                    user_data[user_id]['payment_processing'] = True

                    await event.answer("⚡ Processing payment...")
                    prepare_response = user_data[user_id]['prepare_response']
                    payment_type = user_data[user_id].get('payment_type', 'bolt11')
                    fee = user_data[user_id].get('fee', 0)

                    if payment_type == 'lnurl':
                        response = await lnurl_pay(user_id, prepare_response)
                        amount = user_data[user_id].get('amount', 'N/A')
                        amount_display = await format_amount_with_usd(amount) if isinstance(amount, int) else amount
                        fee_display = await format_amount_with_usd(fee)

                        # Track outgoing volume
                        if isinstance(amount, int) and amount > 0:
                            track_volume('out', amount + fee)

                        await event.edit(
                            f"✅ **Payment Successful!**\n\n"
                            f"💰 Amount: {amount_display}\n"
                            f"⚡ Fee: {fee_display}\n"
                            f"Type: LNURL-Pay"
                        )
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
                                        sender_info = "👻 Anonymous"
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
                        payment = await send_payment(user_id, prepare_response, options)

                        payment_id = getattr(payment, 'id', 'N/A')
                        amount = getattr(payment, 'amount', user_data[user_id].get('amount', 'N/A'))
                        amount_display = await format_amount_with_usd(amount) if isinstance(amount, int) else amount
                        fee_display = await format_amount_with_usd(fee)

                        # Check if payment is truly successful
                        is_success, failure_reason = is_payment_successful(payment)

                        if is_success:
                            # Track outgoing volume
                            if isinstance(amount, int) and amount > 0:
                                track_volume('out', amount + fee)

                            await event.edit(
                                f"✅ **Payment Successful!**\n\n"
                                f"💰 Amount: {amount_display}\n"
                                f"⚡ Fee: {fee_display}\n"
                                f"🆔 ID: `{payment_id}`",
                                buttons=[
                                    [Button.inline("🔍 Payment Details", f"track_{payment_id}".encode())],
                                    [Button.inline("« Menu", b"back_to_menu")]
                                ]
                            )
                        else:
                            # Payment failed - remove pending sender info
                            if resolved_user:
                                with pending_sender_info_lock:
                                    if resolved_user['id'] in pending_sender_info:
                                        del pending_sender_info[resolved_user['id']]

                            await event.edit(
                                f"❌ **Payment Failed**\n\n"
                                f"💰 Amount: {amount_display}\n"
                                f"Reason: {failure_reason}\n"
                                f"🆔 ID: `{payment_id}`",
                                buttons=[
                                    [Button.inline("🔍 Payment Details", f"track_{payment_id}".encode())],
                                    [Button.inline("« Menu", b"back_to_menu")]
                                ]
                            )

                    del user_data[user_id]
                    if user_id in user_steps:
                        del user_steps[user_id]

                except Exception as error:
                    # clear the stuck processing flag so future confirms work
                    if user_id in user_data:
                        user_data[user_id]['payment_processing'] = False
                    await event.edit(
                        f"❌ **Payment Failed**\n\n{error}",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                    )
            else:
                await event.answer("❌ Payment data expired")

        elif data.startswith("onchain_speed_") and not data.endswith("_all"):
            if user_id not in user_data:
                await event.edit("❌ Session expired. Please try again.")
                return

            user_data_obj = user_data[user_id]

            if 'amount' not in user_data_obj:
                await event.edit(
                    "❌ Amount not specified.\n"
                    "Please enter the amount again.",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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
            amount_display = await format_amount_with_usd(amount)
            fee_display = await format_amount_with_usd(selected_fee)
            total_display = await format_amount_with_usd(amount + selected_fee)

            await event.edit(
                f"📋 **Onchain Payment Summary**\n\n"
                f"💰 Amount: {amount_display}\n"
                f"⚡ Fee ({speed}): {fee_display}\n"
                f"💳 Total: {total_display}\n\n"
                f"Confirm payment?",
                buttons=[
                    [
                        Button.inline("✅ Confirm", b"confirm_payment_yes"),
                        Button.inline("❌ Cancel", b"back_to_menu")
                    ],
                ]
            )

        # Quick send amount buttons
        elif data.startswith("send_quick_"):
            amount = int(data.replace("send_quick_", ""))
            await event.answer(f"⚡ {amount:,} sats")

            # Check balance
            balance = int(await get_balance(user_id))
            if balance < amount:
                await event.edit(
                    f"❌ **Insufficient Balance**\n\n"
                    f"You need {amount:,} sats but only have {balance:,} sats.",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                )
                return

            # Check if user has payment data
            if user_id not in user_data or ('invoice' not in user_data[user_id] and 'pay_request' not in user_data[user_id]):
                await event.edit(
                    "❌ Session expired. Please start again.",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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
                        f"❌ Amount must be between {min_sats:,} and {max_sats:,} sats",
                        buttons=[[Button.inline("« Try Again", b"send"), Button.inline("« Cancel", b"back_to_menu")]]
                    )
                    return

                await event.edit("⚡ Preparing payment...")
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

                    amount_display = await format_amount_with_usd(amount)
                    fee_display = await format_amount_with_usd(fee)
                    total = amount + fee
                    total_display = await format_amount_with_usd(total)

                    await event.edit(
                        f"📋 **Payment Summary**\n\n"
                        f"💰 Amount: {amount_display}\n"
                        f"⚡ Fee: {fee_display}\n"
                        f"💳 Total: {total_display}\n\n"
                        f"Confirm payment?",
                        buttons=[
                            [Button.inline("✅ Confirm", b"confirm_payment_yes"), Button.inline("❌ Cancel", b"back_to_menu")],
                        ]
                    )
                except Exception as e:
                    await event.edit(f"❌ Error: {e}", buttons=[[Button.inline("« Menu", b"back_to_menu")]])
            elif payment_type == 'onchain':
                # Onchain: need fee speed selection (same as typed-amount flow)
                try:
                    res = await event.edit("⛽ Calculating onchain fees...")
                    prepare_response, _, _ = await prepare_payment(user_id, invoice, amount)
                    fee_quote = prepare_response.payment_method.fee_quote
                    slow_fee = fee_quote.speed_slow.user_fee_sat + fee_quote.speed_slow.l1_broadcast_fee_sat
                    medium_fee = fee_quote.speed_medium.user_fee_sat + fee_quote.speed_medium.l1_broadcast_fee_sat
                    fast_fee = fee_quote.speed_fast.user_fee_sat + fee_quote.speed_fast.l1_broadcast_fee_sat
                    user_data[user_id]['prepare_response'] = prepare_response
                    user_data[user_id]['fees'] = {'slow': slow_fee, 'medium': medium_fee, 'fast': fast_fee}
                    amount_display = await format_amount_with_usd(amount)
                    await event.edit(
                        f"📋 **Onchain Payment Summary**\n\n"
                        f"💰 Amount: {amount_display}\n\n"
                        f"⛽ **Fee Options:**\n"
                        f"🐌 Slow: {slow_fee:,} sats\n"
                        f"🚗 Medium: {medium_fee:,} sats\n"
                        f"🚀 Fast: {fast_fee:,} sats\n\n"
                        f"Choose confirmation speed:",
                        buttons=[
                            [
                                Button.inline("🐌 Slow", b"onchain_speed_slow"),
                                Button.inline("🚗 Medium", b"onchain_speed_medium"),
                                Button.inline("🚀 Fast", b"onchain_speed_fast")
                            ],
                            [Button.inline("« Cancel", b"back_to_menu")]
                        ]
                    )
                except Exception as e:
                    await event.edit(f"❌ {e}", buttons=[[Button.inline("« Menu", b"back_to_menu")]])
            else:
                # Regular invoice payment
                user_steps[user_id] = "send_confirm"
                await prepare_and_show_fee(event, user_id, invoice, amount)
            return

        elif data == "send_withdraw_all":
            # Withdraw all balance
            balance = int(await get_balance(user_id))
            if balance <= 0:
                await event.answer("❌ No balance to withdraw")
                return

            # Check if user has payment data (invoice OR pay_request for LNURL)
            if user_id in user_data and ('invoice' in user_data[user_id] or 'pay_request' in user_data[user_id]):
                payment_type = user_data[user_id].get('payment_type', 'bolt11')

                try:
                    res = await event.edit("Calculating fees for withdrawal...")

                    if payment_type == 'onchain':
                        # Get bitcoin address from user_data
                        btc_address = user_data[user_id].get('invoice') or user_data[user_id].get('address')
                        if not btc_address:
                            await event.answer("Bitcoin address missing")
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
                        balance_display = await format_balance_with_usd(balance)

                        await res.edit(
                            f"⛓️ **Withdraw All - Onchain**\n\n"
                            f"💰 Balance: {balance_display}\n\n"
                            f"⚡ **Fee Options:**\n"
                            f"🐢 Slow: {slow_fee} sats -> Send {slow_amount} sats\n"
                            f"🚗 Medium: {medium_fee} sats -> Send {medium_amount} sats\n"
                            f"🚀 Fast: {fast_fee} sats -> Send {fast_amount} sats\n\n"
                            f"Choose confirmation speed:",
                            buttons=[
                                [
                                    Button.inline("Slow", b"onchain_speed_slow_all"),
                                    Button.inline("Medium", b"onchain_speed_medium_all"),
                                    Button.inline("Fast", b"onchain_speed_fast_all")
                                ],
                                [Button.inline("« Cancel", b"back_to_menu")]
                            ]
                        )
                    else:
                        # Lightning (BOLT11 or LNURL)
                        if payment_type == 'lnurl':
                            # LNURL Payment
                            pay_request = user_data[user_id].get('pay_request')
                            if not pay_request:
                                await event.answer("Payment data missing")
                                return

                            min_sats = user_data[user_id].get('min_sats', 0)
                            max_sats = user_data[user_id].get('max_sats', 0)

                            # Check limits
                            if balance < min_sats:
                                await res.edit(
                                    f"**Insufficient Balance**",
                                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                                )
                                return

                            send_amount = min(balance, max_sats)
                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, send_amount)
                            final_amount = send_amount - fee_sats

                            if final_amount <= 0:
                                await res.edit("Balance too low after fees", buttons=[[Button.inline("« Menu", b"back_to_menu")]])
                                return

                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, final_amount)
                            user_data[user_id]['prepare_response'] = prepare_response
                            user_data[user_id]['fee'] = fee_sats
                            user_data[user_id]['amount'] = final_amount

                            # Format with USD
                            balance_display = await format_balance_with_usd(balance)
                            fee_display = await format_amount_with_usd(fee_sats)
                            amount_display = await format_amount_with_usd(final_amount)

                            await res.edit(
                                f"⚡ **Withdraw All - Lightning Address**\n\n"
                                f"💰 Balance: {balance_display}\n"
                                f"⚡ Fee: {fee_display}\n"
                                f"💸 Sending: {amount_display}\n\n"
                                f"Confirm?",
                                buttons=[
                                    [Button.inline("Confirm", b"confirm_payment_yes")],
                                    [Button.inline("« Cancel", b"back_to_menu")]
                                ]
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]
                        else:
                            # BOLT11 Invoice
                            invoice = user_data[user_id].get('invoice')
                            if not invoice:
                                await event.answer("Invoice missing")
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
                                    f"Insufficient balance for withdrawal.",
                                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                                )
                                return

                            prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, final_amount)

                            # Use spark_fee if available, otherwise fee_sats
                            if spark_fee is not None:
                                final_fee = spark_fee
                            else:
                                final_fee = fee_sats if fee_sats else 0

                            user_data[user_id]['prepare_response'] = prepare_response
                            user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
                            user_data[user_id]['fee'] = final_fee
                            user_data[user_id]['amount'] = final_amount
                            user_data[user_id]['original_balance'] = balance

                            total = final_amount + final_fee

                            # Format with USD
                            balance_display = await format_balance_with_usd(balance)
                            fee_display = await format_amount_with_usd(final_fee)
                            amount_display = await format_amount_with_usd(final_amount)
                            total_display = await format_amount_with_usd(total)

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
                                    anon_text = "👻 Sender: **Anonymous**"
                                    anon_btn = Button.inline("👤 Show My Name", b"toggle_send_anon_all")
                                else:
                                    anon_text = "👤 Sender: **Visible**"
                                    anon_btn = Button.inline("👻 Send Anonymous", b"toggle_send_anon_all")

                                recipient_text += f"{anon_text}\n\n"
                                buttons.append([anon_btn])

                            buttons.append([
                                Button.inline("✅ Confirm", b"confirm_payment_yes"),
                                Button.inline("❌ Cancel", b"back_to_menu")
                            ])
                            await res.edit(
                                f"⚡ **Send All - Lightning**\n\n"
                                f"{recipient_text}"
                                f"💰 Balance: {balance_display}\n"
                                f"⚡ Fee: {fee_display}\n"
                                f"💸 Amount to send: {amount_display}\n"
                                f"💳 Total: {total_display}\n\n"
                                f"⚠️ Fee will be deducted from your balance\n\n"
                                f"Confirm?",
                                buttons=buttons
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]

                except Exception as error:
                    await event.edit(
                        f"Error: {error}",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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
                    await event.answer("Insufficient balance after fees")
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
                    balance_display = await format_balance_with_usd(balance)
                    fee_display = await format_amount_with_usd(selected_fee)
                    amount_display = await format_amount_with_usd(final_amount)

                    await event.edit(
                        f"📋 **Withdraw All Summary**\n\n"
                        f"💰 Balance: {balance_display}\n"
                        f"⚡ Fee ({speed}): {fee_display}\n"
                        f"💸 Amount to send: {amount_display}\n\n"
                        f"⚠️ Fee will be deducted from your balance\n\n"
                        f"Confirm withdrawal?",
                        buttons=[
                            [
                                Button.inline("Confirm", b"confirm_payment_yes"),
                                Button.inline("« Cancel", b"back_to_menu")
                            ]
                        ]
                    )
                except Exception as error:
                    await event.answer(f"Error: {error}")

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
                await event.answer("⏰ Zap request expired or already completed", alert=True)
                return

            # Check if the user clicking is the sender
            # For anonymous requests, allow any user to cancel
            is_anonymous = zap_data.get('is_anonymous', False)

            # During 'initializing' phase, is_anonymous might not be set yet
            # In that case, check the original sender_id from the event
            if is_anonymous is False and zap_data.get('status') == 'initializing':
                # Still initializing, use sender_id from stored data
                if user_id != zap_data['sender_id']:
                    await event.answer("⚠️ Only the sender can cancel this zap!", alert=True)
                    return
            elif not is_anonymous and user_id != zap_data['sender_id']:
                await event.answer("⚠️ Only the sender can cancel this zap!", alert=True)
                return

            # Mark as cancelled - the background task will handle cleanup
            if zap_owner_id in pending_zap_confirmations:
                pending_zap_confirmations[zap_owner_id]['status'] = 'cancelled'

            await event.edit("❌ Zap cancelled.", buttons=None)
            await event.answer("Cancelled")

        # Balance button handler
        elif data.startswith("show_balance_"):
            balance_id = data.replace("show_balance_", "")

            # Import the pending_balance from balance_handler
            from __main__ import balance_handler
            pending = getattr(balance_handler, 'pending_balance', {})

            if balance_id not in pending:
                await event.answer("⏰ Request expired. Use /zap_balance again.", alert=True)
                return

            balance_data = pending[balance_id]

            # Check if the user clicking is the owner
            # If sent anonymously (via channel), allow the clicker to see their own balance
            is_anonymous = balance_data.get('is_anonymous', False)
            if not is_anonymous and user_id != balance_data['user_id']:
                await event.answer("⚠️ This is not your balance request!", alert=True)
                return

            # For anonymous requests, use the clicking user's ID for their balance
            target_user_id = user_id

            # Show balance via answer (private popup)
            await init(target_user_id)
            balance = await get_balance(target_user_id)
            balance_display = await format_balance_with_usd(balance)

            await event.answer(
                f"💰 Your Balance: {balance_display}",
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
        await event.answer(f"Error: {error}")


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
    try:
        return int(float(text)), False, None
    except:
        return None, False, None


@client.on(events.NewMessage(func=lambda e: e.is_private and not e.text.startswith('/')))
async def message_handler(event):
    """Handle text messages based on user step"""
    user_id = event.sender_id

    # Mark user as active
    await mark_user_active(user_id)

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

    text = event.text.strip()

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
        # ---- v30: contact payment flow (enter amount) ----
        if current_step == "contact_pay_amount":
            try:
                amount = int(text.replace(',', ''))
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await event.respond("❌ Enter a valid positive number of sats.")
                return
            cp = (user_data.get(user_id) or {}).get('contact_pay') or {}
            identifier = cp.get('identifier')
            if not identifier:
                user_steps.pop(user_id, None)
                await event.respond("❌ Session expired.", buttons=[[Button.inline("« Contacts", b"contacts")]])
                return
            await event.respond("⏳ Preparing payment...")
            try:
                prep, fee, spark_fee = await prepare_payment(user_id, identifier, amount)
            except Exception as pe:
                user_steps.pop(user_id, None)
                await event.respond(f"❌ Could not prepare payment: {pe}",
                                    buttons=[[Button.inline("« Contacts", b"contacts")]])
                return
            cp['prepare'] = prep
            user_data.setdefault(user_id, {})['contact_pay'] = cp
            fee_note = ""
            try:
                if isinstance(prep.payment_method, SendPaymentMethod.BOLT11_INVOICE):
                    fee_note = f"\n💸 Fee: ~{fee or 0} sats"
            except Exception:
                pass
            user_steps[user_id] = "contact_pay_confirm"
            await event.respond(
                f"✅ Confirm sending **{amount:,} sats** to **{cp.get('name')}**?{fee_note}",
                buttons=[
                    [Button.inline("✅ Confirm Send", b"confcontactpay")],
                    [Button.inline("« Cancel", b"contacts")]
                ])
            return

        # ---- v30: sats / BTC / fiat calculator ----
        if current_step == "calc_input":
            raw = text.strip()
            fiat_code = get_user_fiat(user_id)
            fiat_code_l = (fiat_code or '').lower()
            m = re.match(r'^\$?\s*([\d.,]+)\s*([A-Za-z]*)$', raw)
            if not m:
                await event.respond(
                    "❌ Couldn't parse that.\n\nTry:\n• `5000 sats`\n• `0.001 btc`\n• `$10`")
                return
            try:
                value = float(m.group(1).replace(',', ''))
            except ValueError:
                await event.respond("❌ Invalid number.")
                return
            unit = m.group(2).lower()
            fiat_rate = await get_fiat_rate(fiat_code) or 0

            if unit in ('btc', 'bitcoin', 'xbt'):
                sats = int(round(value * 100_000_000))
            elif raw.startswith('$') or (fiat_code_l and unit == fiat_code_l) or unit == 'usd':
                if not fiat_rate:
                    await event.respond(f"❌ No {fiat_code} rate available right now. Try again later.")
                    return
                sats = int(round(value / fiat_rate * 100_000_000))
            else:
                sats = int(round(value))  # default: sats

            btc = sats / 100_000_000
            fiat_emoji = get_fiat_emoji(fiat_code)
            fiat_val = (btc * fiat_rate) if fiat_rate else None
            if fiat_val is not None:
                fiat_line = f"{fiat_emoji} **{fiat_val:,.2f} {fiat_code}**"
            else:
                fiat_line = f"{fiat_emoji} _rate unavailable_"

            del user_steps[user_id]
            await event.respond(
                "🧮 **Conversion**\n\n"
                f"⚡ **{sats:,} sats**\n"
                f"₿ **{btc:.8f} BTC**\n"
                f"{fiat_line}",
                buttons=[
                    [Button.inline("🧮 Another", b"calc")],
                    [Button.inline("« Settings", b"settings")]
                ])
            return

        # Handle scan payment input
        if current_step == "scan_payment_input":
            payment_id = text.strip()

            # Clean up the message
            last_message_id = user_data.get(user_id, {}).get('last_message_id')
            if last_message_id:
                try:
                    await client.edit_message(user_id, last_message_id, "🔍 Looking up payment...")
                except:
                    pass
            await event.delete()
            payment = await get_payment_by_id(user_id, payment_id)

            del user_steps[user_id]
            if user_id in user_data:
                del user_data[user_id]

            if payment:
                details_text = await format_payment_details(payment, payment_id)

                if last_message_id:
                    try:
                        await client.edit_message(
                            user_id,
                            last_message_id,
                            details_text,
                            buttons=[
                                [Button.inline("🔍 Scan Another", b"scan_payment")],
                                [Button.inline("« Menu", b"back_to_menu")]
                            ]
                        )
                    except:
                        await event.respond(
                            details_text,
                            buttons=[
                                [Button.inline("🔍 Scan Another", b"scan_payment")],
                                [Button.inline("« Menu", b"back_to_menu")]
                            ]
                        )
                else:
                    await event.respond(
                        details_text,
                        buttons=[
                            [Button.inline("🔍 Scan Another", b"scan_payment")],
                            [Button.inline("« Menu", b"back_to_menu")]
                        ]
                    )
            else:
                error_msg = (
                    f"❌ **Payment Not Found**\n\n"
                    f"No payment found with ID:\n`{payment_id}`\n\n"
                    f"Make sure you entered the correct payment ID."
                )
                if last_message_id:
                    try:
                        await client.edit_message(
                            user_id,
                            last_message_id,
                            error_msg,
                            buttons=[
                                [Button.inline("🔍 Try Again", b"scan_payment")],
                                [Button.inline("« Menu", b"back_to_menu")]
                            ]
                        )
                    except:
                        await event.respond(
                            error_msg,
                            buttons=[
                                [Button.inline("🔍 Try Again", b"scan_payment")],
                                [Button.inline("« Menu", b"back_to_menu")]
                            ]
                        )
                else:
                    await event.respond(
                        error_msg,
                        buttons=[
                            [Button.inline("🔍 Try Again", b"scan_payment")],
                            [Button.inline("« Menu", b"back_to_menu")]
                        ]
                    )
            return

        # Handle donation amount input
        elif current_step == "donate_amount":
            DONATE_USER_ID = int(os.getenv('DONATE_USER_ID', '0'))

            # Parse amount (sats or USD)
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                await event.respond("Please enter a valid amount (sats or $USD)")
                return

            if amount <= 0:
                await event.respond("Amount must be greater than 0")
                return

            try:
                last_message_id = user_data.get(user_id, {}).get('last_message_id')

                if last_message_id:
                    try:
                        await client.edit_message(user_id, last_message_id, "Processing donation...")
                    except:
                        msg = await event.respond("Processing donation...")
                        last_message_id = msg.id
                else:
                    msg = await event.respond("Processing donation...")
                    last_message_id = msg.id

                await init(DONATE_USER_ID)

                donate_invoice = await create_invoice(DONATE_USER_ID, amount_sats=amount, description="Donation - Thank you!")
                prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, donate_invoice, amount)

                if spark_fee is not None:
                    final_fee = spark_fee
                else:
                    final_fee = fee_sats if fee_sats else 0

                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
                payment = await send_payment(user_id, prepare_response, options)

                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]

                try:
                    await client.delete_messages(user_id, last_message_id)
                except:
                    pass

                # Format amount display
                amount_display = await format_amount_with_usd(amount)
                fee_display = await format_amount_with_usd(final_fee)

                # Check if payment is truly successful
                is_success, failure_reason = is_payment_successful(payment)

                if is_success:
                    await client.send_message(
                        user_id,
                        f"**Donation Successful!**\n\n"
                        f"Amount: {amount_display}\n"
                        f"Fee: {fee_display}\n\n"
                        f"Thank you for your support!\n"
                        f"Your donation helps keep this bot running!",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                    )
                else:
                    await client.send_message(
                        user_id,
                        f"❌ **Donation Failed**\n\n"
                        f"Amount: {amount_display}\n"
                        f"Reason: {failure_reason}",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                    )
                    return

                try:
                    await client.send_message(
                        DONATE_USER_ID,
                        f"**New Donation Received!**\n\n"
                        f"Amount: {amount_display}\n"
                        f"From: User {user_id}\n"
                        f"TX: `{getattr(payment, 'id', 'N/A')}`"
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
                            f"Donation failed: {error}\n\n"
                            "You can also manually send to:\n"
                            "`your_wallet@example.com`",
                            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            f"Donation failed: {error}\n\n"
                            "You can also manually send to:\n"
                            "`your_wallet@example.com`",
                            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                        )
                except:
                    await event.respond(
                        f"Donation failed: {error}\n\n"
                        "You can also manually send to:\n"
                        "`your_wallet@example.com`",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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
                    await event.respond("❌ Fee must be at least 1 sat/vB.", buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]])
                    return

                ud = user_data.get(user_id, {})
                txid = ud.get('claim_txid')
                vout = ud.get('claim_vout')
                user_steps.pop(user_id, None)

                if not txid:
                    await event.respond("❌ Session expired.", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])
                    return

                await event.respond("⏳ Claiming deposit...")
                sdk = await get_wallet(user_id)
                fee = MaxFee.RATE(sat_per_vbyte=fee_rate)
                request = ClaimDepositRequest(txid=txid, vout=vout, max_fee=fee)
                result = await sdk.claim_deposit(request)

                amount_display = f"{result.payment.amount_sats:,} sats" if hasattr(result.payment, 'amount_sats') else "✅"
                await event.respond(
                    f"✅ **Deposit Claimed Successfully!**\n\n"
                    f"⚡ Payment received in Lightning wallet.\n"
                    f"💰 {amount_display}",
                    buttons=[[Button.inline("« Settings", b"settings")]]
                )
            except ValueError:
                await event.respond("❌ Enter a valid number (sat/vB).", buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]])
            except Exception as e:
                user_steps.pop(user_id, None)
                logging.error(f"Error claiming with custom fee: {e}")
                await event.respond(f"❌ Claim failed: {e}", buttons=[[Button.inline("« Try Again", b"unclaimed_deposits")]])

        # Unclaimed deposit - refund address input
        elif current_step == "ucl_refund_address":
            address = text.strip()
            ud = user_data.setdefault(user_id, {})
            ud['refund_address'] = address
            user_steps[user_id] = "ucl_refund_fee"
            await event.respond(
                f"↩️ **Refund to:**\n`{address}`\n\n"
                f"Enter fee rate in **sat/vB**:\n"
                f"Example: `5`",
                buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]]
            )

        # Unclaimed deposit - refund fee input
        elif current_step == "ucl_refund_fee":
            try:
                fee_rate = int(text.strip())
                if fee_rate < 1:
                    await event.respond("❌ Fee must be at least 1 sat/vB.", buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]])
                    return

                ud = user_data.get(user_id, {})
                txid = ud.get('refund_txid')
                vout = ud.get('refund_vout')
                address = ud.get('refund_address')
                user_steps.pop(user_id, None)

                if not txid or not address:
                    await event.respond("❌ Session expired.", buttons=[[Button.inline("« Back", b"unclaimed_deposits")]])
                    return

                await event.respond("⏳ Refunding deposit...")
                sdk = await get_wallet(user_id)
                fee = Fee.RATE(sat_per_vbyte=fee_rate)
                request = RefundDepositRequest(txid=txid, vout=vout, destination_address=address, fee=fee)
                result = await sdk.refund_deposit(request)

                await event.respond(
                    f"✅ **Refund Sent!**\n\n"
                    f"📋 TX: `{result.tx_id}`\n"
                    f"📍 To: `{address}`",
                    buttons=[[Button.inline("« Settings", b"settings")]]
                )
            except ValueError:
                await event.respond("❌ Enter a valid number (sat/vB).", buttons=[[Button.inline("« Cancel", b"unclaimed_deposits")]])
            except Exception as e:
                user_steps.pop(user_id, None)
                logging.error(f"Error refunding deposit: {e}")
                await event.respond(f"❌ Refund failed: {e}", buttons=[[Button.inline("« Try Again", b"unclaimed_deposits")]])

        # ==================== SEND PASSWORD INPUT HANDLERS ====================
        elif current_step == "set_send_password":
            password = text.strip()
            prompt_msg_id = user_data.get(user_id, {}).get('password_prompt_msg_id')

            if len(password) < 4:
                await event.respond(
                    "❌ Password must be at least 4 characters.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
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
                        "✅ **Password Set!**\n\n"
                        "💡 Now set a hint to help you remember it.\n\n"
                        "Enter a hint (or skip):\n"
                        "Example: \"My pet's name\" or \"Favorite number\"",
                        buttons=[[Button.inline("⏭ Skip", b"skip_password_hint")]]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    "✅ **Password Set!**\n\n"
                    "💡 Now set a hint to help you remember it.\n\n"
                    "Enter a hint (or skip):\n"
                    "Example: \"My pet's name\" or \"Favorite number\"",
                    buttons=[[Button.inline("⏭ Skip", b"skip_password_hint")]]
                )

        elif current_step == "set_password_hint_after_password":
            hint = text.strip()

            # Delete the hint message (both sides)
            await delete_password_message(event)

            if hint and len(hint) > 0:
                set_password_hint(user_id, hint)

            del user_steps[user_id]

            hint_msg = f"\n💡 Hint: {hint}" if hint else ""
            await client.send_message(
                event.chat_id,
                f"✅ **Setup Complete!**\n\n"
                f"Your send password is now active.{hint_msg}\n\n"
                f"You'll need this password to:\n"
                f"• Send payments\n"
                f"• View backup seeds",
                buttons=[[Button.inline("« Settings", b"settings_back")]]
            )

        elif current_step == "set_password_hint":
            hint = text.strip()

            # Delete the hint message (both sides)
            await delete_password_message(event)

            if hint and len(hint) > 0:
                set_password_hint(user_id, hint)
                msg = f"✅ **Hint Updated!**\n\nYour new hint: **{hint}**"
            else:
                remove_password_hint(user_id)
                msg = "✅ **Hint Removed!**"

            del user_steps[user_id]

            await client.send_message(
                event.chat_id,
                msg,
                buttons=[[Button.inline("« Settings", b"settings_back")]]
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
                            f"❌ Incorrect password.{hint_text}",
                            buttons=[
                                [Button.inline("🔄 Try Again", b"backup")],
                                [Button.inline("« Cancel", b"settings_back")]
                            ]
                        )
                    else:
                        raise Exception("No prompt message")
                except:
                    await client.send_message(
                        event.chat_id,
                        f"❌ Incorrect password.{hint_text}",
                        buttons=[
                            [Button.inline("🔄 Try Again", b"backup")],
                            [Button.inline("« Cancel", b"settings_back")]
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
                        f"🔐 **Your recovery phrase:**\n\n"
                        f"`{seed}`\n\n"
                        f"⚠️ Keep it safe and never share!",
                        buttons=[[Button.inline("« Cancel", b"settings_back")]]
                    )
                else:
                    raise Exception("No prompt message")
            except:
                await client.send_message(
                    event.chat_id,
                    f"🔐 **Your recovery phrase:**\n\n"
                    f"`{seed}`\n\n"
                    f"⚠️ Keep it safe and never share!",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
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
                            "❌ Incorrect password.",
                            buttons=[[Button.inline("🔄 Try Again", b"change_send_password"), Button.inline("« Cancel", b"settings_back")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        "❌ Incorrect password.",
                        buttons=[[Button.inline("🔄 Try Again", b"change_send_password"), Button.inline("« Cancel", b"settings_back")]]
                    )
                del user_steps[user_id]
                return

            # Password verified, now ask for new password
            user_steps[user_id] = "set_new_send_password"
            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id, prompt_msg_id,
                        "✅ Password verified!\n\n"
                        "Now enter your new password:",
                        buttons=[[Button.inline("« Cancel", b"settings_back")]]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    "✅ Password verified!\n\n"
                    "Now enter your new password:",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )

        elif current_step == "set_new_send_password":
            password = text.strip()

            if len(password) < 4:
                await event.respond(
                    "❌ Password must be at least 4 characters.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return

            # Delete the user's password message for security (both sides)
            await delete_password_message(event)

            # Set the new password
            set_user_send_password(user_id, password)
            del user_steps[user_id]

            await client.send_message(
                event.chat_id,
                "✅ **Password Changed!**\n\n"
                "Your send password has been updated.",
                buttons=[[Button.inline("« Settings", b"settings_back")]]
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
                            "❌ Incorrect password.",
                            buttons=[[Button.inline("🔄 Try Again", b"remove_send_password"), Button.inline("« Cancel", b"settings_back")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        "❌ Incorrect password.",
                        buttons=[[Button.inline("🔄 Try Again", b"remove_send_password"), Button.inline("« Cancel", b"settings_back")]]
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
                        "✅ **Password Removed!**\n\n"
                        "Send password protection has been disabled.",
                        buttons=[[Button.inline("« Settings", b"settings_back")]]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    "✅ **Password Removed!**\n\n"
                    "Send password protection has been disabled.",
                    buttons=[[Button.inline("« Settings", b"settings_back")]]
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
                            "❌ Incorrect password. Payment cancelled.",
                            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        "❌ Incorrect password. Payment cancelled.",
                        buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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

                amount_display = await format_amount_with_usd(amount) if isinstance(amount, int) else str(amount)
                fee_display = await format_amount_with_usd(fee) if isinstance(fee, int) else str(fee)
                total = (amount if isinstance(amount, int) else 0) + (fee if isinstance(fee, int) else 0)
                total_display = await format_amount_with_usd(total) if total > 0 else "N/A"

                try:
                    if prompt_msg_id:
                        await client.edit_message(
                            event.chat_id, prompt_msg_id,
                            f"🔓 **Password Verified!**\n\n"
                            f"💰 Amount: {amount_display}\n"
                            f"⚡ Fee: {fee_display}\n"
                            f"💳 Total: {total_display}\n\n"
                            f"Confirm payment?",
                            buttons=[
                                [
                                    Button.inline("✅ Confirm", b"confirm_payment_yes"),
                                    Button.inline("❌ Cancel", b"back_to_menu")
                                    ]
                            ]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        f"🔓 **Password Verified!**\n\n"
                        f"💰 Amount: {amount_display}\n"
                        f"⚡ Fee: {fee_display}\n"
                        f"💳 Total: {total_display}\n\n"
                        f"Confirm payment?",
                        buttons=[
                            [
                                Button.inline("✅ Confirm", b"confirm_payment_yes"),
                                Button.inline("❌ Cancel", b"back_to_menu")
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
                            "❌ Incorrect password. Payment cancelled.",
                            buttons=[[Button.inline("« Back", b"send_exchange")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        "❌ Incorrect password. Payment cancelled.",
                        buttons=[[Button.inline("« Back", b"send_exchange")]]
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
                            "❌ Order expired. Please try again.",
                            buttons=[[Button.inline("« Back", b"send_exchange")]]
                        )
                    else:
                        raise Exception("No prompt")
                except:
                    await client.send_message(
                        event.chat_id,
                        "❌ Order expired. Please try again.",
                        buttons=[[Button.inline("« Back", b"send_exchange")]]
                    )
                return

            # Mark password as verified and show confirm button again
            order_info['password_verified'] = True

            try:
                if prompt_msg_id:
                    await client.edit_message(
                        event.chat_id, prompt_msg_id,
                        f"🔓 **Password Verified!**\n\n"
                        f"⚡ Ready to send Lightning payment to ff.io?\n\n"
                        f"🆔 Order: `{order_id}`",
                        buttons=[
                            [Button.inline("✅ Confirm & Send", f"ffio_pay_{order_id}".encode())],
                            [Button.inline("❌ Cancel", b"send_exchange")]
                        ]
                    )
                else:
                    raise Exception("No prompt")
            except:
                await client.send_message(
                    event.chat_id,
                    f"🔓 **Password Verified!**\n\n"
                    f"⚡ Ready to send Lightning payment to ff.io?\n\n"
                    f"🆔 Order: `{order_id}`",
                    buttons=[
                        [Button.inline("✅ Confirm & Send", f"ffio_pay_{order_id}".encode())],
                        [Button.inline("❌ Cancel", b"send_exchange")]
                    ]
                )

        # Recovery wallet input
        elif current_step == "recovery_input":
            words = text.strip()
            word_list = words.split()

            if len(word_list) != 12:
                await event.respond(
                    "Invalid recovery phrase. Must be exactly 12 words.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return

            # Validate mnemonic
            try:
                if not mnemo.check(words):
                    await event.respond(
                        "Invalid recovery phrase. Please check your words.",
                        buttons=[[Button.inline("« Cancel", b"settings_back")]]
                    )
                    return
            except:
                await event.respond(
                    "Invalid recovery phrase format.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return

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
                "✅ **Wallet Recovered!**\n\n"
                "Your wallet has been restored from the recovery phrase.\n"
                "Balance synced successfully.",
                buttons=[[Button.inline("« Menu", b"back_to_menu")]]
            )

        # v32: emergency exit address input (v34 refactored per official docs:
        # economy check, funding gate, fee-rate choice, leaf-id persistence)
        elif current_step == "exit_address_input":
            addr = text.strip()
            if not addr.startswith(("bc1", "1", "3")):
                await event.respond("❌ That doesn't look like a Bitcoin address. Try again or Cancel.")
                return
            user_steps.pop(user_id, None)
            await event.respond("⏳ Preparing exit (quoting fee rates)...")
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
                        "⚠️ No exit possible right now.\n\n"
                        "At every fee rate, no leaf is worth exiting "
                        "(each leaf would cost more in fees than it holds), "
                        "or the wallet has no mature leaves yet.\n\n"
                        "💡 Try again later when on-chain fees are lower, "
                        "or use Export State as your recovery backup.",
                        buttons=[[Button.inline("« Exit Menu", b"emergency_exit")]])
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
                    f"⚠️ **Exit prepared** — review carefully:\n",
                    f"🏠 To: `{addr}`",
                    f"🐚 Leaves: {n_leaves}",
                    f"💰 Recoverable: {recov:,} sats",
                    f"⛽ Total fee: {total_fee:,} sats ({chosen_rate} sat/vB)",
                ]
                if fanout:
                    lines.append(f"🪭 Fan-out fee: {fanout:,} sats")
                if needs_funding:
                    lines.append(f"🔑 UTXO funding needed: {utxo_fund:,} sats (on-chain)")
                lines.append(f"\n🧮 Net to you: {net:,} sats")
                if net <= 0:
                    lines.append("🛑 **NOT worth it at this rate** — fee ≥ recoverable. Cancel, or retry when fees are lower.")

                if funding_blocked:
                    lines.append(
                        "\n🚫 **Cannot proceed:** the exit needs an on-chain UTXO "
                        f"of ≥{utxo_fund:,} sats to pay fees, but this wallet has "
                        "no unclaimed on-chain deposits to fund it with. A "
                        "Spark-only balance cannot be force-exited — this is an "
                        "SDK requirement, not a bug.\n\n"
                        "✅ Instead: use the normal **Send → Bitcoin address** "
                        "(cooperative withdrawal — cheaper, no UTXO needed), or "
                        "export your state as a backup.")
                    await event.respond("\n".join(lines),
                        buttons=[[Button.inline("« Exit Menu", b"emergency_exit")]])
                    return

                confirm_text = "✅ Sign (no broadcast)" if not needs_funding else f"✅ Sign with my UTXO ({utxo_fund:,} sats)"
                lines.append(
                    "\n📝 **CONFIRM only SIGNS** — nothing is sent to the network. "
                    "You get the signed transaction hex to broadcast yourself.")
                if needs_funding:
                    lines.append(
                        f"⚠️ Signing will spend one of your unclaimed on-chain "
                        f"deposits (~{utxo_fund:,} sats) as the fee funding UTXO.")
                await event.respond(
                    "\n".join(lines),
                    buttons=[
                        [Button.inline(confirm_text, b"exit_confirm")],
                        [Button.inline("❌ Cancel", b"emergency_exit")],
                        [Button.inline("💾 Export State", b"exit_export_state")]
                    ])
            except Exception as e:
                logging.error(f"exit prepare: {e}")
                await event.respond(f"❌ Exit prepare failed: {e}",
                    buttons=[[Button.inline("« Exit Menu", b"emergency_exit")]])
            return

        # v32: webhook url input
        elif current_step == "webhook_url_input":
            url = text.strip()
            if not url.startswith("https://"):
                await event.respond("❌ Must be an HTTPS URL.")
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
                    f"✅ Webhook registered.\nID: `{wid}`\n\n"
                    f"🔐 Signing secret (save it):\n`{secret}`")
            except Exception as e:
                await event.respond(f"❌ {e}")
            return

        # v32: webhook delete input
        elif current_step == "webhook_del_input":
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
                    await event.respond("❌ No webhook matches that ID/URL.")
                    return
                await sdk.unregister_webhook(UnregisterWebhookRequest(webhook_id=wid))
                await event.respond("✅ Webhook removed.")
            except Exception as e:
                await event.respond(f"❌ {e}")
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
                    await event.respond(f"❌ Could not resolve {dest}: {e}")
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
                await event.respond(
                    "❌ Didn't recognize that. Send an invoice, LN address, @username or btc address.")
                return

            # block self-payment early
            try:
                my_addr = await get_lightning_address(user_id)
                my_ln = getattr(my_addr, 'lightning_address', None) if my_addr else None
                if my_ln and my_ln.lower() == dest.lower():
                    await event.respond(
                        "❌ **That's your own Lightning address** — self-payment isn't allowed. "
                        "Send a different recipient.")
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
                f"➕ **Recipient {len(ud.get('batch_items', [])) + 1}** — `{dest[:30]}…`\n"
                f"Type: {kind_hint}\n\n"
                f"Now send the amount in sats (e.g. `1000`):",
                buttons=[[Button.inline("« Cancel", b"more_tools")]])
            return

        # v33 batch: amount input → prepare immediately → show running list
        elif current_step == "batch_amount":
            amount_txt = text.strip().lstrip('$').replace(',', '')
            if not amount_txt.isdigit():
                await event.respond("⚠️ Invalid amount. Send sats as a plain number (e.g. `2000`):")
                return
            sats = int(amount_txt)
            ud = user_data.get(user_id) or {}
            dest = ud.get('batch_pending_dest')
            kind = ud.get('batch_pending_kind')
            if not dest or not kind:
                await event.respond("❌ Session lost — start Batch Pay again from More Tools.")
                user_steps.pop(user_id, None)
                return

            status = await event.respond("⏳ Preparing payment...")
            try:
                bolt_opts = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
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
                    _min = int(_j.get('minSendable', 1000)) // 1000
                    _max = int(_j.get('maxSendable', 1000000000)) // 1000
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

                entry = {'dest': dest, 'amount': sats, 'fee': int(fee or 0), 'kind': kind,
                         'prepare_response': prep, 'options': opts}
                ud.pop('batch_pending_dest', None)
                ud.pop('batch_pending_kind', None)
                ud.setdefault('batch_items', []).append(entry)
                ud.setdefault('batch_pairs', []).append((dest, sats, kind))
                items = ud['batch_items']
                total = sum(int(e.get('amount') or 0) for e in items)
                fee_total = sum(int(e.get('fee') or 0) for e in items)

                lines = [f"✅ **Added!** — {len(items)} payment(s) in batch:\n"]
                for i, e in enumerate(items, 1):
                    d = e['dest'] if len(e['dest']) <= 30 else e['dest'][:29] + "…"
                    lines.append(f"{i}. `{d}` — {e['amount']:,} sats (+⛽{e['fee']:,})")
                lines.append(f"\n💰 Amounts: {total:,} + ⛽ Fees: {fee_total:,} = **{total + fee_total:,} sats**\n")
                lines.append("➕ Send next destination to add, or send all now:")
                user_steps.pop(user_id, None)
                try: await status.delete()
                except Exception: pass
                await event.respond("\n".join(lines), buttons=[
                    [Button.inline(f"🚀 Send All ({len(items)})", b"batch_send_all")],
                    [Button.inline("➕ Add Another", b"batch_add_more")],
                    [Button.inline("🗑 Cancel All", b"queue_clear")]
                ])
            except Exception as e:
                import traceback as _tb
                logging.error(f"batch prepare: {e}\n{_tb.format_exc()}")
                await event.respond(f"❌ Failed to prepare: {e}\n\nSend the destination again.",
                                    buttons=[[Button.inline("« Cancel", b"more_tools")]])
            return

        # v31: LNURL-Withdraw amount input
        elif current_step == "lnurl_withdraw_amount":
            try:
                amt = int(re.sub(r'[^0-9]', '', text))
            except ValueError:
                await event.respond("Send a number (sats).")
                return
            wd = (user_data.get(user_id) or {}).get('withdraw_request')
            if not wd:
                await event.respond("❌ Withdraw request expired — send the LNURL again.")
                user_steps.pop(user_id, None)
                return
            if amt < wd.min_withdrawable // 1000 or amt > wd.max_withdrawable // 1000:
                await event.respond(
                    f"Amount must be between {wd.min_withdrawable // 1000:,} and {wd.max_withdrawable // 1000:,} sats.")
                return
            user_steps.pop(user_id, None)
            user_data.pop(user_id, None)
            await event.respond("⏳ Claiming...")
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
                    f"✅ **Withdraw claim sent!**\n"
                    f"💰 {amt:,} sats incoming to your wallet.\n"
                    f"It completes once the source pays the invoice.")
            except Exception as e:
                logging.error(f"lnurl_withdraw error: {e}")
                await event.respond(f"❌ Withdraw failed: {e}")
            return

        # Change Lightning address input
        elif current_step == "change_ln_address_input":
            username = text.strip().lower()

            # Validate username format
            if not username.replace('_', '').isalnum():
                await event.respond(
                    "Username can only contain lowercase letters, numbers, and underscores.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return

            try:
                sdk = await get_wallet(user_id)
                request = RegisterLightningAddressRequest(username=username, description="Zap Zap")
                new_address = await sdk.register_lightning_address(request=request)

                del user_steps[user_id]

                await event.respond(
                    f"**Lightning Address Updated!**\n\n"
                    f"New Address: `{new_address.lightning_address}`\n"
                    f"LNURL: `{new_address.lnurl}`",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(
                    f"Error: {error}\n\n"
                    "Username might be taken. Try another one.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
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
                                f"❌ Amount too low.\n\n"
                                f"Minimum: **{min_val} {from_ccy}**\n"
                                f"You entered: **{amount} {from_ccy}**",
                                buttons=[[Button.inline("« Try Again", b"receive_exchange")]]
                            )
                            return
                        if max_val and float(max_val) > 0 and amount > float(max_val):
                            await event.reply(
                                f"❌ Amount too high.\n\n"
                                f"Maximum: **{max_val} {from_ccy}**\n"
                                f"You entered: **{amount} {from_ccy}**",
                                buttons=[[Button.inline("« Try Again", b"receive_exchange")]]
                            )
                            return

                        receive_btc_raw = to_data.get('amount', '?')
                        rate = to_data.get('rate', '?')
                        # Format BTC properly (no scientific notation) and convert to sats
                        try:
                            btc_val = float(receive_btc_raw)
                            receive_btc = f"{btc_val:.8f}".rstrip('0').rstrip('.')
                            receive_sats = int(btc_val * 100_000_000)
                            sats_display = f" ({receive_sats:,} sats)"
                        except:
                            receive_btc = str(receive_btc_raw)
                            sats_display = ""

                        min_max_note = ""
                        if min_val or max_val:
                            min_max_note = f"Limits: {min_val} - {max_val} {from_ccy}\n"

                        await event.reply(
                            f"🔄 **Exchange Quote**\n\n"
                            f"📤 Send: **{amount} {from_ccy}**\n"
                            f"📥 Receive: **~{receive_btc} BTC**{sats_display}\n"
                            f"📊 Rate: {rate}\n"
                            f"{min_max_note}\n"
                            f"Proceed with this exchange?",
                            buttons=[
                                [Button.inline("✅ Create Order", b"rcv_ex_confirm_yes")],
                                [Button.inline("❌ Cancel", b"receive_exchange")]
                            ]
                        )
                    else:
                        error_msg = price_result.get('msg', 'Unknown error') if price_result else 'API error'
                        # Log the full response for debugging
                        logging.warning(f"ff.io quote error for {from_ccy}: {price_result}")
                        await event.reply(
                            f"❌ **Quote Error**\n\n"
                            f"Currency: `{from_ccy}`\n"
                            f"Error: {error_msg}\n\n"
                            f"_This currency may not be available for exchange._",
                            buttons=[[Button.inline("« Try Different Coin", b"receive_exchange")]]
                        )
                        user_steps.pop(user_id, None)
                except Exception as e:
                    logging.error(f"ff.io quote exception for {from_ccy}: {e}")
                    await event.reply(f"Error getting quote: {e}", buttons=[[Button.inline("« Back", b"receive_exchange")]])
                    user_steps.pop(user_id, None)
            except ValueError:
                await event.reply(
                    "❌ Invalid amount. Please enter a valid number.\n"
                    "Example: `0.1` or `100`",
                    buttons=[[Button.inline("« Cancel", b"receive_exchange")]]
                )

        elif current_step == "ffio_send_address":
            address = text.strip()
            if len(address) < 10:
                await event.reply("Invalid address. Please try again.")
                return

            ud = user_data.get(user_id, {})
            ud['ffio_to_address'] = address

            # Check if tag/memo needed
            tag_type = ud.get('ffio_tag_type')
            if tag_type:
                user_steps[user_id] = "ffio_send_tag"
                await event.reply(
                    f"🏷 **{tag_type}**\n\n"
                    f"Enter **{tag_type}** for your address (if needed):\n"
                    f"`{address[:30]}...`",
                    buttons=[[Button.inline("⏭ Skip", b"ffio_skip_tag"), Button.inline("« Cancel", b"send_exchange")]]
                )
                return

            user_steps[user_id] = "ffio_send_amount"
            to_name = ud.get('ffio_to_name', ud.get('ffio_to_ccy', '?'))
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance)

            min_sats = ud.get('ffio_min_sats', '')
            max_sats = ud.get('ffio_max_sats', '')
            limit_text = ""
            if min_sats or max_sats:
                limit_text = f"Min: **{min_sats:,} sats** | Max: **{max_sats:,} sats**\n"

            await event.reply(
                f"**Exchange Lightning BTC -> {to_name}**\n\n"
                f"Balance: {balance_display}\n"
                f"Address: `{address[:30]}...`\n"
                f"{limit_text}\n"
                f"💰 Enter amount in **sats** to exchange:\n"
                f"Example: `50000` or `$10`",
                buttons=[
                    [Button.inline("💸 Send All", b"ffio_send_all")],
                    [Button.inline("« Cancel", b"send_exchange")]
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
            balance_display = await format_balance_with_usd(balance)

            min_sats = ud.get('ffio_min_sats', '')
            max_sats = ud.get('ffio_max_sats', '')
            limit_text = ""
            if min_sats or max_sats:
                limit_text = f"Min: **{min_sats:,} sats** | Max: **{max_sats:,} sats**\n"

            await event.reply(
                f"**Exchange Lightning BTC -> {to_name}**\n\n"
                f"Balance: {balance_display}\n"
                f"Address: `{ud.get('ffio_to_address', '?')[:30]}...`\n"
                f"{limit_text}\n"
                f"💰 Enter amount in **sats** to exchange:\n"
                f"Example: `50000` or `$10`",
                buttons=[
                    [Button.inline("💸 Send All", b"ffio_send_all")],
                    [Button.inline("« Cancel", b"send_exchange")]
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
                    await event.reply("Insufficient balance.", buttons=[[Button.inline("« Back", b"send_exchange")]])
                    return

                # Check min/max from stored values
                min_sats = ud.get('ffio_min_sats')
                max_sats = ud.get('ffio_max_sats')
                if min_sats and amount_sats < min_sats:
                    await event.reply(
                        f"❌ Amount too low.\n\n"
                        f"Minimum: **{min_sats:,} sats**\n"
                        f"You entered: **{amount_sats:,} sats**",
                        buttons=[[Button.inline("« Try Again", b"send_exchange")]]
                    )
                    return
                if max_sats and amount_sats > max_sats:
                    await event.reply(
                        f"❌ Amount too high.\n\n"
                        f"Maximum: **{max_sats:,} sats**\n"
                        f"You entered: **{amount_sats:,} sats**",
                        buttons=[[Button.inline("« Try Again", b"send_exchange")]]
                    )
                    return

                ud['ffio_amount_sats'] = amount_sats
                amount_btc = amount_sats / 100_000_000

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
                            min_s = int(float(api_min) * 100_000_000)
                            await event.reply(
                                f"Amount too low.\n\nMinimum: **{min_s:,} sats**\nYou entered: **{amount_sats:,} sats**",
                                buttons=[[Button.inline("« Try Again", b"send_exchange")]]
                            )
                            return
                        if api_max and float(api_max) > 0 and amount_btc > float(api_max):
                            max_s = int(float(api_max) * 100_000_000)
                            await event.reply(
                                f"Amount too high.\n\nMaximum: **{max_s:,} sats**\nYou entered: **{amount_sats:,} sats**",
                                buttons=[[Button.inline("« Try Again", b"send_exchange")]]
                            )
                            return

                        amount_display = await format_amount_with_usd(amount_sats)
                        min_max_note = ""
                        if api_min or api_max:
                            try:
                                mn = f"{int(float(api_min)*100_000_000):,}" if api_min else "?"
                                mx = f"{int(float(api_max)*100_000_000):,}" if api_max else "?"
                                min_max_note = f"Limits: {mn} - {mx} sats\n"
                            except:
                                pass

                        await event.reply(
                            f"🔄 **Exchange Quote**\n\n"
                            f"📤 Send: **{amount_display}**\n"
                            f"📥 Receive: **~{receive_amount} {to_ccy}**\n"
                            f"📊 Rate: {rate}\n"
                            f"{min_max_note}\n"
                            f"Proceed with this exchange?",
                            buttons=[
                                [Button.inline("✅ Create Order", b"snd_ex_confirm_yes")],
                                [Button.inline("❌ Cancel", b"send_exchange")]
                            ]
                        )
                    else:
                        error_msg = price_result.get('msg', 'Unknown error') if price_result else 'API error'
                        await event.reply(f"Quote Error: {error_msg}", buttons=[[Button.inline("« Back", b"send_exchange")]])
                        user_steps.pop(user_id, None)
                except Exception as e:
                    await event.reply(f"❌ Error: {e}", buttons=[[Button.inline("« Back", b"send_exchange")]])
                    user_steps.pop(user_id, None)
            except ValueError:
                await event.reply("❌ Invalid amount. Enter sats (e.g. `50000`) or USD (e.g. `$10`).")

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
                    f"💰 {t(user_id, 'amount_label')}: {await format_amount_with_usd(pending_amount)}" if pending_amount else t(user_id, 'invoice_no_amount_title'),
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
                await event.respond(f"❌ {error}")

        # Receive amount input (with memo)
        elif current_step == "receive_amount":
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                if is_usd:
                    await event.respond("Unable to convert USD. Please try again or enter amount in sats.")
                else:
                    await event.respond("Please enter a valid amount")
                return

            if amount <= 0:
                await event.respond("Amount must be greater than 0")
                return

            memo = user_data.get(user_id, {}).get('memo', 'Zap payment')

            try:
                invoice = await create_invoice(user_id, amount_sats=amount, description=memo)

                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]

                amount_display = await format_amount_with_usd(amount)

                # Generate QR code for invoice
                qr_buffer = generate_qr_code(invoice)

                await client.send_file(
                    event.chat_id,
                    qr_buffer,
                    force_document=False,
                    caption=(
                        f"📥 **Your Invoice**\n\n"
                        f"💰 Amount: {amount_display}\n"
                        f"💬 Memo: {memo}\n\n"
                        f"`{invoice}`"
                    ),
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(f"❌ Error: {error}")

        # Receive amount input (without memo)
        elif current_step == "receive_amount_no_memo":
            amount, is_usd, usd_value = await parse_amount_input(text)

            if amount is None:
                if is_usd:
                    await event.respond("Unable to convert USD. Please try again or enter amount in sats.")
                else:
                    await event.respond("Please enter a valid amount")
                return

            if amount <= 0:
                await event.respond("Amount must be greater than 0")
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

                amount_display = await format_amount_with_usd(amount)

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
                        [Button.inline(t(user_id, "btn_back_menu"), b"back_to_menu")]
                    ]
                )
            except Exception as error:
                await event.respond(f"❌ {error}")

        # ==================== SEND: Parse Invoice ====================
        elif current_step == "send_invoice":
            try:
                # Check if it's a username (starts with @)
                processing_msg = None
                resolved_user_info = None
                if text.startswith('@'):
                    processing_msg = await event.respond(
                        f"🔍 **Resolving Username**\n\n"
                        f"👤 `{text}`\n\n"
                        f"⏳ Please wait..."
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
                        parsed_amount = details.amount_msat // 1000
                    else:
                        needs_amount = True

                # Lightning Address (LNURL-Pay)
                elif isinstance(input_type, InputType.LIGHTNING_ADDRESS):
                    input_type_str = "Lightning Address"
                    pay_request = details.pay_request
                    min_sats = pay_request.min_sendable // 1000
                    max_sats = pay_request.max_sendable // 1000

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
                    min_sats = details.min_sendable // 1000
                    max_sats = details.max_sendable // 1000
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
                    min_sats = getattr(details, 'min_withdrawable', 0) // 1000
                    max_sats = getattr(details, 'max_withdrawable', 0) // 1000
                    user_data[user_id] = {
                        'withdraw_request': details,
                        'payment_type': 'lnurl_withdraw',
                        'min_sats': min_sats,
                        'max_sats': max_sats
                    }
                    if processing_msg:
                        await processing_msg.edit(
                            f"💸 **LNURL-Withdraw detected**\n\n"
                            f"You can claim this to your wallet.\n"
                            f"Range: {min_sats:,} – {max_sats:,} sats\n\n"
                            f"Send the amount to withdraw (in sats):",
                            buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            f"💸 **LNURL-Withdraw detected**\n\n"
                            f"You can claim this to your wallet.\n"
                            f"Range: {min_sats:,} – {max_sats:,} sats\n\n"
                            f"Send the amount to withdraw (in sats):",
                            buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                        )
                    user_steps[user_id] = "lnurl_withdraw_amount"
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
                        hint = f"\n\n💡 $1 ≈ {sats_per_dollar:,} sats"

                    # Quick amount buttons + withdraw all
                    buttons = [
                        [
                            Button.inline("1K", b"send_quick_1000"),
                            Button.inline("5K", b"send_quick_5000"),
                            Button.inline("10K", b"send_quick_10000"),
                            Button.inline("50K", b"send_quick_50000")
                        ],
                        [Button.inline("💸 Send All Balance", b"send_withdraw_all")],
                        [Button.inline("« Cancel", b"back_to_menu")]
                    ]

                    # Build resolved info text
                    resolved_text = ""
                    tips_text = ""
                    if resolved_user_info:
                        ru = resolved_user_info
                        has_started = ru.get('has_started_bot', False)

                        # Show bot status
                        if has_started:
                            bot_status = "✅ Bot started"
                        else:
                            bot_status = "⚠️ Bot not started"

                        resolved_text = (
                            f"✅ **Sending to:**\n"
                            f"👤 @{ru['username']}\n"
                            f"📛 {ru['display_name']}\n"
                            f"🆔 `{ru['id']}`\n"
                            f"📌 Type: {ru['type']}\n"
                            f"🤖 {bot_status}\n\n"
                        )
                        # Add tips based on user type and bot status
                        if ru['is_channel'] or ru['is_group']:
                            tips_text = (
                                "⚠️ **Important:**\n"
                                "• This is a **group/channel** wallet\n"
                                "• Admins won't be notified automatically\n"
                                "• Please inform the admin about this transfer\n\n"
                            )
                        elif not has_started:
                            tips_text = (
                                "⚠️ **Recipient hasn't started the bot!**\n"
                                "• They will **NOT** receive a notification\n"
                                "• The funds will be safe in their wallet\n"
                                "• Please tell them to start the bot\n\n"
                            )
                        # If has_started is True, no tips needed

                    # Build amount instruction
                    amount_instruction = (
                        "**Choose or type amount:**\n"
                        "• Type number: `5000` = 5000 sats\n"
                        "• Use $ prefix: `$5` = ~$5 in sats\n"
                        "• Or tap a quick button below"
                    )

                    # Edit processing message or send new one
                    if processing_msg:
                        await processing_msg.edit(
                            f"💵 **Enter Amount**\n\n"
                            f"{resolved_text}"
                            f"{tips_text}"
                            f"{amount_instruction}{hint}",
                            buttons=buttons
                        )
                    else:
                        await event.respond(
                            f"💵 **Enter Amount**\n\n"
                            f"{amount_instruction}{hint}",
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
                error_msg = (
                    f"❌ **Invalid Input**\n\n"
                    f"Error: {error}\n\n"
                    f"Please send a valid:\n"
                    f"• Lightning Invoice (lnbc...)\n"
                    f"• Lightning Address (user@domain.com)\n"
                    f"• Telegram Username (@username)\n"
                    f"• Bitcoin Address (bc1...)\n"
                    f"• LNURL\n\n"
                    f"Or cancel:"
                )
                if processing_msg:
                    await processing_msg.edit(
                        error_msg,
                        buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                    )
                else:
                    await event.respond(
                        error_msg,
                        buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
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
                        "❌ Unable to convert USD. Please try again or enter amount in sats.\n\n"
                        "Try again or cancel:",
                        buttons=[
                            [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                            [Button.inline("« Cancel", b"back_to_menu")]
                        ]
                    )
                else:
                    await event.respond(
                        "❌ Please enter a valid number in sats or $USD\n\n"
                        "Try again or cancel:",
                        buttons=[
                            [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                            [Button.inline("« Cancel", b"back_to_menu")]
                        ]
                    )
                return

            if amount <= 0:
                await event.respond(
                    "❌ Amount must be greater than 0\n\n"
                    "Try again or cancel:",
                    buttons=[
                        [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                        [Button.inline("« Cancel", b"back_to_menu")]
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
                            f"⚠️ Amount must be between {data['min_sats']:,} and {data['max_sats']:,} sats\n\n"
                            f"Please enter a valid amount:",
                            buttons=[
                                [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                                [Button.inline("« Cancel", b"back_to_menu")]
                            ]
                        )
                        return

                    try:
                        res = await event.respond("⚡ Calculating fees...")
                        prepare_response, fee_sats = await prepare_lnurl_pay(
                            user_id, data['pay_request'], amount
                        )

                        user_data[user_id]['prepare_response'] = prepare_response
                        user_data[user_id]['fee'] = fee_sats

                        total = amount + fee_sats

                        # Format with USD
                        amount_display = await format_amount_with_usd(amount)
                        fee_display = await format_amount_with_usd(fee_sats)
                        total_display = await format_amount_with_usd(total)

                        _q = (user_data.get(user_id) or {}).get('send_queue') or []
                        _q_txt = (build_queue_text(_q)[0] + "\n\n" + "─" * 10 + "\n🔹 **New payment:**\n") if _q else ""
                        await res.edit(
                            _q_txt + f"📋 **Payment Summary (LNURL)**\n\n"
                            f"💰 Amount: {amount_display}\n"
                            f"⚡ Fee: {fee_display}\n"
                            f"💳 Total: {total_display}\n\n"
                            f"Confirm payment?",
                            buttons=[
                                [
                                    Button.inline("✅ Confirm", b"confirm_payment_yes"),
                                    Button.inline("❌ Cancel", b"back_to_menu")
                                ],
                            ]
                        )
                        del user_steps[user_id]
                    except Exception as error:
                        await event.respond(
                            f"❌ Error preparing payment:\n{error}\n\n"
                            f"Please try again or cancel:",
                            buttons=[
                                [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                                [Button.inline("« Cancel", b"back_to_menu")]
                            ]
                        )

                # Onchain Payment
                elif payment_type == 'onchain':
                    try:
                        res = await event.respond("⛓️ Calculating onchain fees...")
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
                        amount_display = await format_amount_with_usd(amount)

                        await res.edit(
                            f"📋 **Onchain Payment Summary**\n\n"
                            f"💰 Amount: {amount_display}\n\n"
                            f"⚡ **Fee Options:**\n"
                            f"🐢 Slow: {slow_fee:,} sats\n"
                            f"🚗 Medium: {medium_fee:,} sats\n"
                            f"🚀 Fast: {fast_fee:,} sats\n\n"
                            f"Choose confirmation speed:",
                            buttons=[
                                [
                                    Button.inline("🐢 Slow", b"onchain_speed_slow"),
                                    Button.inline("🚗 Medium", b"onchain_speed_medium"),
                                    Button.inline("🚀 Fast", b"onchain_speed_fast")
                                ],
                                [Button.inline("« Cancel", b"back_to_menu")]
                            ]
                        )
                        del user_steps[user_id]
                    except Exception as error:
                        await event.respond(
                            f"❌ Error calculating fees:\n{error}\n\n"
                            f"Please try again or cancel:",
                            buttons=[
                                [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                                [Button.inline("« Cancel", b"back_to_menu")]
                            ]
                        )

                # Regular BOLT11 Payment
                else:
                    invoice = data['invoice']
                    user_steps[user_id] = "send_confirm"
                    await prepare_and_show_fee(event, user_id, invoice, amount)
            else:
                await event.respond(
                    "⏰ Session expired, please start again",
                    buttons=[[Button.inline("« Menu", b"back_to_menu")]]
                )
                del user_steps[user_id]
            return

    except Exception as error:
        logging.error(f"Message handler error: {error}")
        await event.respond(
            f"Error: {error}",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]]
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
        msg = f"❌ Could not load contacts: {e}"
        btns = [[Button.inline("« Menu", b"back_to_menu")]]
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
        msg = ("📒 **Address Book (empty)**\n\nNo contacts yet.\n\n"
               "Add one with:\n`/addcontact <name> <address-or-invoice>`")
    else:
        lines = ["📒 **Address Book**\n"]
        for idx, c in cmap.items():
            ident = c['identifier']
            shown = (ident[:24] + '…') if len(ident) > 25 else ident
            lines.append(f"• **{c['name']}** — `{shown}`")
            btns.append([
                Button.inline(f"📤 {c['name']}", f"contactpay_{idx}".encode()),
                Button.inline("🗑️", f"contactdel_{idx}".encode()),
            ])
        msg = "\n".join(lines)
    btns.append([Button.inline("« Menu", b"back_to_menu")])

    if edit:
        try:
            await event.edit(msg, buttons=btns)
        except Exception:
            await event.respond(msg, buttons=btns)
    else:
        await event.respond(msg, buttons=btns)


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/fees'))
async def fees_handler(event):
    """Show recommended on-chain network fees."""
    try:
        await event.respond("⏳ Fetching network fees...")
        fees = await get_recommended_fees(event.sender_id)
        await event.respond(_format_fees(fees),
                            buttons=[[Button.inline("« Menu", b"back_to_menu")]])
    except Exception as e:
        await event.respond(f"❌ Could not fetch fees: {e}")


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/sync'))
async def sync_handler(event):
    """Force a wallet sync."""
    try:
        await event.respond("🔄 Syncing wallet...")
        info = await sync_user_wallet(event.sender_id)
        await event.respond(
            f"✅ Wallet synced.\n💰 Balance: **{getattr(info, 'balance_sats', 0):,} sats**",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]])
    except Exception as e:
        await event.respond(f"❌ Sync failed: {e}")


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/signmsg(?:\s+(.+))?'))
async def signmsg_handler(event):
    """Sign a message to prove wallet ownership. Usage: /signmsg <message>"""
    try:
        message = (event.pattern_match.group(1) or '').strip()
        if not message:
            await event.respond("ℹ️ Usage: `/signmsg <message>`\nExample: `/signmsg hello world`")
            return
        status = await event.respond("✍️ Signing...")
        resp = await sign_message_with_wallet(event.sender_id, message)
        await status.edit(
            "✅ **Message signed**\n\n"
            f"📝 Message: `{message}`\n"
            f"🔑 Pubkey: `{resp.pubkey}`\n"
            f"🔏 Signature: `{resp.signature}`\n\n"
            "Verify with `/verifymsg <pubkey> <signature> <message>`.",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]])
    except Exception as e:
        await event.respond(f"❌ Signing failed: {e}")


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/verifymsg\s+(\S+)\s+(\S+)\s+(.+)'))
async def verifymsg_handler(event):
    """Verify a signed message. Usage: /verifymsg <pubkey> <signature> <message>"""
    try:
        pubkey = event.pattern_match.group(1)
        signature = event.pattern_match.group(2)
        message = event.pattern_match.group(3).strip()
        resp = await check_message_with_wallet(event.sender_id, message, pubkey, signature)
        verdict = "✅ **VALID** — signature matches." if resp.is_valid else "❌ **INVALID** — does not match."
        await event.respond(
            f"{verdict}\n\n📝 Message: `{message}`\n🔑 Pubkey: `{pubkey}`",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]])
    except Exception as e:
        await event.respond(f"❌ Verification failed: {e}\nUsage: `/verifymsg <pubkey> <signature> <message>`")


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern=r'/buy'))
async def buy_handler(event):
    """Get a Buy Bitcoin on-ramp link."""
    try:
        url = await buy_bitcoin_url(event.sender_id)
        await event.respond(
            f"🟧 **Buy Bitcoin**\n\nOpen the on-ramp here:\n\n{url}",
            buttons=[[Button.inline("« Menu", b"back_to_menu")]],
            link_preview=False)
    except Exception as e:
        await event.respond(f"❌ Could not create buy link: {e}")


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
            await event.respond("❌ Name too long (max 48 chars).")
            return
        contact = await add_user_contact(event.sender_id, name, identifier)
        await event.respond(
            f"✅ **Contact saved**\n\n• **{contact.name}** — `{contact.payment_identifier}`",
            buttons=[[Button.inline("📒 View Contacts", b"contacts")],
                     [Button.inline("« Menu", b"back_to_menu")]])
    except Exception as e:
        await event.respond(f"❌ Could not add contact: {e}")


@client.on(events.NewMessage(pattern=r'(?i)/zap_balance(?:@\w+)?\b'))
async def balance_handler(event):
    """Show wallet balance with private button"""
    # Use helper to handle anonymous channel senders
    user_id, is_anonymous = get_sender_info(event)

    if not user_id:
        await event.reply("❌ Could not identify sender.")
        return

    # For anonymous senders, show balance directly as text (no button)
    if is_anonymous:
        await init(user_id)
        balance = await get_balance(user_id)
        balance_display = await format_balance_with_usd(balance)
        await event.reply(f"💰 **Your Balance:** {balance_display}")
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
        "💰 **Check Your Balance**\n\n"
        "Click the button below to see your wallet balance.\n"
        "⚠️ Only you can see your balance.",
        buttons=[
            [Button.inline("👁️ Show My Balance", f"show_balance_{balance_id}".encode())]
        ]
    )


# ==================== Debug/Stats Command ====================
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
    if usd_rate:
        total_in_usd = sats_to_usd(total_in, usd_rate)
        total_out_usd = sats_to_usd(total_out, usd_rate)
        in_display = f"{total_in:,} sats (~${total_in_usd:,.2f})"
        out_display = f"{total_out:,} sats (~${total_out_usd:,.2f})"
    else:
        in_display = f"{total_in:,} sats"
        out_display = f"{total_out:,} sats"

    stats_msg = (
        f"📊 **Bot Statistics**\n\n"
        f"👥 **Total Users:** {total_users}\n\n"
        f"📥 **Total Volume In:** {in_display}\n"
        f"📤 **Total Volume Out:** {out_display}"
    )

    await event.reply(stats_msg)


# ==================== TIP System with $ support, message, and confirmation ====================
@client.on(events.NewMessage(pattern=r'(?i)/zap(?:@\w+)?\b\s*$'))
async def zap_help_handler(event):
    """Show zap usage when /zap is typed with no arguments (e.g. tapped from the
    Telegram command menu, which also appends @BotUsername)."""
    await event.reply(
        "⚡ **How to Zap:**\n\n"
        "Reply to someone's message with:\n"
        "`/zap <amount>`\n\n"
        "Or mention a user:\n"
        "`/zap <amount> @username [message]`\n\n"
        "**Examples:**\n"
        "• `/zap 1000`\n"
        "• `/zap $5`\n"
        "• `/zap 21 @alice Great job!`"
    )


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
        await event.reply("⚡ Zap command only works in groups!")
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
            await event.reply("❌ Could not identify sender.")
            return

        # Quick basic validation first
        if amount_str.startswith('$'):
            amount = int(float(amount_str[1:]) * 1000)  # Rough estimate for display
            amount_display_temp = f"${amount_str[1:]}"
        else:
            amount = int(amount_str)
            amount_display_temp = f"{amount:,} sats"

        if amount <= 0:
            await event.reply("❌ Amount must be greater than 0")
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
            "⚡ Zapping...",
            buttons=[
                Button.inline("❌ Cancel", f"zap_cancel_{zap_id}".encode())
            ]
        )

        # Now do the slow operations in background
        asyncio.create_task(process_zap_full(
            zap_id, _msg, event, sender_id, is_anonymous,
            amount_str, username, message, amount_display_temp
        ))

    except Exception as error:
        logging.error(f"Zap error: {error}")
        await event.reply(f"❌ Zap failed: {error}")


# Full zap processing in background (all slow operations here)
async def process_zap_full(zap_id, msg, event, sender_id, is_anonymous, amount_str, username, message, amount_display_temp):
    """Process all zap operations in background"""
    try:
        # Check if cancelled during initializing phase
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit("❌ Zap cancelled.", buttons=None)
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
                    await msg.edit("❌ Unable to get USD rate. Please try with sats amount.", buttons=None)
                except:
                    pass
                return
            amount = usd_to_sats(usd_amount, usd_rate)
            amount_display = f"${usd_amount} ({amount:,} sats)"
        else:
            # Sats amount
            amount = int(amount_str)
            if usd_rate:
                usd_value = sats_to_usd(amount, usd_rate)
                amount_display = f"{amount:,} sats (~${usd_value:.2f})"
            else:
                amount_display = f"{amount:,} sats"

        # Check if cancelled after amount parsing
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit("❌ Zap cancelled.", buttons=None)
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
                    await msg.edit(f"❌ User @{username} not found!", buttons=None)
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
                    await msg.edit("❌ Cannot zap this message (unknown sender).", buttons=None)
                except:
                    pass
                return
            if reply_msg.sender:
                receiver_name = f"@{reply_msg.sender.username}" if reply_msg.sender.username else str(receiver_id)
            else:
                receiver_name = str(receiver_id)
        else:
            try:
                await msg.edit(
                    "⚡ **How to Zap:**\n\n"
                    "Reply to someone's message or use:\n"
                    "`/zap <amount> @username [message]`\n\n"
                    "**Examples:**\n"
                    "• `/zap 1000 @john`\n"
                    "• `/zap $5 @alice`\n"
                    "• `/zap 21 @bob Great job!`",
                    buttons=None
                )
            except:
                pass
            return

        if sender_id == receiver_id:
            try:
                await msg.edit("❌ You can't zap yourself!", buttons=None)
            except:
                pass
            return

        # Check if cancelled after receiver determination
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit("❌ Zap cancelled.", buttons=None)
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
                    f"❌ **Insufficient balance!**",
                    buttons=None
                )
            except:
                pass
            return

        # Check if cancelled after wallet operations
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit("❌ Zap cancelled.", buttons=None)
            except:
                pass  # Message already edited by cancel handler
            del pending_zap_confirmations[sender_id]
            return

        # Get sender name
        if is_anonymous:
            sender_name = "Anonymous"
            if event.sender and hasattr(event.sender, 'title') and event.sender.title:
                sender_name = f"📢 {event.sender.title}"
        else:
            if event.sender:
                sender_name = f"@{event.sender.username}" if event.sender.username else str(sender_id)
            else:
                sender_name = str(sender_id)

        # Prepare description for the invoice (include message if provided)
        # Sanitize to remove non-printable characters (like ZWNJ in Persian)
        if message:
            description = sanitize_memo(f"Zap from {sender_name}: {message}")
        else:
            description = sanitize_memo(f"Zap from {sender_name}")

        # FINAL CHECK before starting payment - if cancelled, don't proceed
        if sender_id in pending_zap_confirmations and pending_zap_confirmations[sender_id].get('status') == 'cancelled':
            try:
                await msg.edit("❌ Zap cancelled.", buttons=None)
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
            f"⚡ **Zapping {amount_display}...**\n\n"
            f"👤 From: {sender_name}\n"
            f"👤 To: {receiver_name}",
            buttons=[
                Button.inline("❌ Cancel", f"zap_cancel_{zap_id}".encode())
            ]
        )

        # Start both tasks: payment and button removal timer
        asyncio.create_task(process_zap_payment(zap_id, msg, sender_id, receiver_id, amount, amount_display, sender_name, receiver_name, description, is_anonymous))
        asyncio.create_task(remove_cancel_button_after_delay(zap_id, msg, amount_display, sender_name, receiver_name))

    except Exception as error:
        logging.error(f"Zap processing error: {error}")
        try:
            await msg.edit(f"❌ Zap failed: {error}", buttons=None)
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
                    f"⚡ **Zapping {amount_display}...**\n\n"
                    f"👤 From: {sender_name}\n"
                    f"👤 To: {receiver_name}",
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
            await msg.edit("❌ Zap data not found", buttons=None)
            return

        # Small delay to allow quick cancellation (0.5 seconds)
        await asyncio.sleep(0.5)

        # Check if cancelled before starting payment
        if zap_owner_id in pending_zap_confirmations and pending_zap_confirmations[zap_owner_id].get('status') == 'cancelled':
            await msg.edit("❌ Zap cancelled", buttons=None)
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
                f"❌ **Insufficient balance for network fees**\n\n"
                f"💡 Deposit a few more sats and retry.",
                buttons=None)
            if zap_owner_id in pending_zap_confirmations:
                del pending_zap_confirmations[zap_owner_id]
            return

        # Check if cancelled again before final payment
        if zap_owner_id in pending_zap_confirmations and pending_zap_confirmations[zap_owner_id].get('status') == 'cancelled':
            await msg.edit("❌ Zap cancelled", buttons=None)
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
                f"✅ **Zap Successful!**\n\n"
                f"💰 Amount: {amount_display}\n"
                f"👤 From: {sender_name}\n"
                f"👤 To: {receiver_name}",
                buttons=None  # Remove cancel button
            )
        else:
            await msg.edit(
                f"❌ **Zap Failed**\n\n"
                f"💰 Amount: {amount_display}\n"
                f"Reason: {failure_reason}",
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
        error_msg = f"❌ Zap failed: {error}"

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


# ==================== BACKUP SERVICE ====================
async def backup_to_telegram():
    """Periodic backup of database and source code to Telegram channel"""
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
                    shutil.copy2(DB_PATH, backup_db_path)

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

            # Backup source code file
            if os.path.exists(SOURCE_FILE_PATH):
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
    print("   - Instant zap execution (no confirm button)")
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
