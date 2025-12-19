from telethon import TelegramClient, events, Button
from keyvalue_sqlite import KeyValueSqlite
from breez_sdk_spark import *
from mnemonic import Mnemonic
import logging
import asyncio
from random import choice
from string import ascii_lowercase
import os
from dotenv import load_dotenv
from datetime import datetime
import threading
from telethon.tl.types import User, Chat, Channel
import re


load_dotenv()

# Enable logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

mnemo = Mnemonic("english")

# Database configuration with lock
DB_PATH = './db-bot.sqlite'
db = KeyValueSqlite(DB_PATH, 'table-name')
db_lock = threading.Lock()  # Thread-safe database access

# Telegram API credentials
api_id = os.getenv('TELEGRAM_API_ID')
api_hash = os.getenv('TELEGRAM_API_HASH')
bot_token = os.getenv('TELEGRAM_BOT_TOKEN')

# Spark SDK configuration
config = default_config(network=Network.MAINNET)
config.api_key = os.getenv('BREEZ_API_KEY')
config.prefer_spark_over_lightning = True

# Initialize users list in database
db.set_default('users', [])
db.set_default('notification_users', {})  # Database table for persistent notifications

# Initialize Telegram client
client = TelegramClient('bot', api_id, api_hash).start(bot_token=bot_token)

# User state tracking
user_steps = {}
user_data = {}

# Payment notification tracking (timestamp-based)
user_last_payment_time = {}  # {user_id: last_payment_timestamp}
user_last_activity = {}  # {user_id: last_activity_timestamp}

# System uptime tracking
bot_start_time = None

# Fiat rates cache
fiat_rates_cache = {
    'rates': {},
    'last_update': None,
    'update_interval': 300  # 5 minutes cache
}

# Pending zap confirmations
pending_zap_confirmations = {}  # {user_id: {'amount_sats': x, 'receiver_id': y, ...}}


async def get_fiat_rates():
    """Get fiat exchange rates with caching"""
    global fiat_rates_cache

    now = datetime.now()

    # Check if cache is valid
    if (fiat_rates_cache['last_update'] and
        (now - fiat_rates_cache['last_update']).total_seconds() < fiat_rates_cache['update_interval'] and
        fiat_rates_cache['rates']):
        return fiat_rates_cache['rates']

    try:
        # Get a temporary SDK connection to fetch rates
        users = db.get('users')
        if not users:
            return {}

        sdk = await get_wallet(users[0])
        rates_response = await sdk.list_fiat_rates()
        await close_wallet(sdk)

        # Convert to dict for easy lookup
        # Rate object has: coin (currency code), value (BTC price in that currency)
        rates_dict = {}
        for rate in rates_response.rates:
            rates_dict[rate.coin] = rate.value

        fiat_rates_cache['rates'] = rates_dict
        fiat_rates_cache['last_update'] = now

        logging.info(f"Updated fiat rates: USD=${rates_dict.get('USD', 'N/A'):,.2f}")
        return rates_dict

    except Exception as error:
        logging.error(f"Error fetching fiat rates: {error}")
        return fiat_rates_cache.get('rates', {})


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


def load_notifications_from_db():
    """Load notification states from database to memory on startup (thread-safe)"""
    try:
        with db_lock:
            notification_data = db.get('notification_users')
            if not notification_data:
                logging.info("No saved notification states found")
                return 0

            loaded = 0
            for user_id_str, data in notification_data.items():
                if data.get('enabled', False):
                    user_id = int(user_id_str)
                    user_last_payment_time[user_id] = data.get('last_timestamp', 0)
                    user_last_activity[user_id] = datetime.now()
                    loaded += 1

            logging.info(f"Loaded {loaded} users with notifications enabled")
            return loaded

    except Exception as error:
        logging.error(f"Error loading notifications from DB: {error}")
        return 0


def save_notification_to_db(user_id, enabled, last_timestamp=None):
    """Save notification state to database for persistence (thread-safe)"""
    try:
        with db_lock:
            notification_data = db.get('notification_users')
            user_id_str = str(user_id)

            if user_id_str not in notification_data:
                notification_data[user_id_str] = {}

            notification_data[user_id_str]['enabled'] = enabled
            notification_data[user_id_str]['last_activity'] = datetime.now().isoformat()

            if last_timestamp is not None:
                notification_data[user_id_str]['last_timestamp'] = last_timestamp

            db.set('notification_users', notification_data)
    except Exception as error:
        logging.error(f"Error saving notification to DB: {error}")


def is_notifications_enabled(user_id):
    """Check if notifications are enabled for user"""
    return user_id in user_last_payment_time


async def disable_notifications(user_id, reason='manual'):
    """Disable notifications for user (thread-safe)

    Args:
        user_id: User ID
        reason: 'manual' (user choice) or 'auto_inactive' (24h timeout)
    """
    if user_id in user_last_payment_time:
        last_ts = user_last_payment_time[user_id]
        del user_last_payment_time[user_id]
    else:
        last_ts = None

    if user_id in user_last_activity:
        del user_last_activity[user_id]

    # Save to DB with reason (thread-safe)
    try:
        with db_lock:
            notification_data = db.get('notification_users')
            user_id_str = str(user_id)

            if user_id_str not in notification_data:
                notification_data[user_id_str] = {}

            notification_data[user_id_str]['enabled'] = False
            notification_data[user_id_str]['disabled_reason'] = reason
            notification_data[user_id_str]['disabled_at'] = datetime.now().isoformat()

            if last_ts is not None:
                notification_data[user_id_str]['last_timestamp'] = last_ts

            db.set('notification_users', notification_data)

        logging.info(f"Disabled notifications for user {user_id} (reason: {reason})")
    except Exception as error:
        logging.error(f"Error disabling notifications: {error}")


async def mark_user_active(user_id):
    """Mark user as active - NO auto-enable notifications (thread-safe)"""
    user_last_activity[user_id] = datetime.now()

    # Don't auto-enable anymore - user must manually enable via Settings
    # Just update activity in DB if already enabled
    if user_id in user_last_payment_time:
        try:
            with db_lock:
                notification_data = db.get('notification_users')
                user_id_str = str(user_id)
                if user_id_str in notification_data:
                    notification_data[user_id_str]['last_activity'] = datetime.now().isoformat()
                    db.set('notification_users', notification_data)
        except Exception as error:
            logging.debug(f"Error updating activity: {error}")


async def check_new_payments(user_id):
    """Check for new RECEIVED payments via history - timestamp based"""
    try:
        # Get recent payments (limit 10 to catch multiple new ones)
        payments = await get_payment_history(user_id, limit=10)

        if not payments:
            if user_id not in user_last_payment_time:
                user_last_payment_time[user_id] = 0
            return

        # Get last checked timestamp
        last_checked_time = user_last_payment_time.get(user_id, 0)

        # Find all new RECEIVED payments since last check
        new_received_payments = []
        latest_timestamp = last_checked_time

        for payment in payments:
            payment_timestamp = getattr(payment, 'timestamp', 0)
            payment_type = getattr(payment, 'payment_type', None)

            # Debug log to see actual timestamp values
            if payment_timestamp > 0:
                logging.debug(f"Payment timestamp: {payment_timestamp}, type: {payment_type}")

            # Update latest timestamp
            if payment_timestamp > latest_timestamp:
                latest_timestamp = payment_timestamp

            # Check if it's a new RECEIVED payment
            if payment_timestamp > last_checked_time:
                # Check if it's RECEIVE type (PaymentType.RECEIVE)
                if payment_type == PaymentType.RECEIVE:
                    new_received_payments.append(payment)

        # Update last checked timestamp IN MEMORY
        user_last_payment_time[user_id] = latest_timestamp

        # Save to DB to persist across restarts
        save_notification_to_db(user_id, enabled=True, last_timestamp=latest_timestamp)

        # If this is first check (initialization), don't notify
        if last_checked_time == 0:
            logging.debug(f"[{user_id}] First check - initialized with timestamp {latest_timestamp}")
            return

        # Get USD rate for notifications
        usd_rate = await get_usd_rate()

        # Notify user about each new received payment
        for payment in reversed(new_received_payments):  # Oldest first
            amount = getattr(payment, 'amount_sats', getattr(payment, 'amount', 0))
            payment_id = getattr(payment, 'id', 'N/A')
            timestamp = getattr(payment, 'timestamp', 0)

            # Format time - smart detection for seconds vs milliseconds
            if timestamp:
                try:
                    # If timestamp > 1e12, it's in milliseconds
                    if timestamp > 1000000000000:
                        dt = datetime.fromtimestamp(timestamp / 1000)
                    else:
                        dt = datetime.fromtimestamp(timestamp)
                    time_str = dt.strftime('%Y-%m-%d %H:%M')
                except Exception as e:
                    logging.debug(f"Timestamp conversion error: {e}, timestamp={timestamp}")
                    time_str = datetime.now().strftime('%Y-%m-%d %H:%M')
            else:
                time_str = datetime.now().strftime('%Y-%m-%d %H:%M')

            # Format amount with USD
            amount_str = await format_amount_with_usd(amount)

            await client.send_message(
                user_id,
                f"**Payment Received!**\n\n"
                f"Amount: {amount_str}\n"
                f"{time_str}\n"
                f"ID: `{payment_id}`\n\n"
                f"Use /start to see your updated balance!"
            )
            logging.info(f"[{user_id}] Notified: {amount} sats received at {time_str}")

    except Exception as error:
        logging.debug(f"Check error [{user_id}]: {error}")


async def monitor_active_users():
    """Background polling - checks active users with smart intervals

    Optimized for scalability:
    - Batch processing with configurable batch size
    - Staggered checks to distribute load
    - Automatic cleanup of inactive users
    - Error isolation per user
    """
    batch_size = 100  # Increased batch size for better throughput
    check_interval = 20  # 20 seconds base interval
    inactive_threshold = 86400  # 24 hours
    stagger_delay = 0.1  # 100ms between individual user checks

    while True:
        try:
            await asyncio.sleep(check_interval)

            current_time = datetime.now()
            active_users = []
            inactive_users = []

            # Collect users to process
            user_ids = list(user_last_payment_time.keys())

            for user_id in user_ids:
                last_activity = user_last_activity.get(user_id)

                if last_activity:
                    time_diff = (current_time - last_activity).total_seconds()

                    if time_diff < inactive_threshold:
                        active_users.append(user_id)
                    else:
                        inactive_users.append(user_id)
                else:
                    inactive_users.append(user_id)

            # Clean up inactive users in batch
            for user_id in inactive_users:
                await disable_notifications(user_id, reason='auto_inactive')

            if inactive_users:
                logging.info(f"Cleaned up {len(inactive_users)} inactive users")

            if not active_users:
                continue

            # Process users in batches with staggered delays
            total_users = len(active_users)
            processed = 0

            for i in range(0, total_users, batch_size):
                batch = active_users[i:i + batch_size]

                # Create tasks for parallel processing within batch
                tasks = []
                for user_id in batch:
                    tasks.append(asyncio.create_task(safe_check_payments(user_id)))

                # Wait for batch to complete with timeout
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=30.0  # 30 second timeout per batch
                    )
                except asyncio.TimeoutError:
                    logging.warning(f"Batch timeout for users {i}-{i+len(batch)}")

                processed += len(batch)

                # Small delay between batches to prevent overload
                if i + batch_size < total_users:
                    await asyncio.sleep(1)

            if processed > 0:
                logging.debug(f"Checked {processed}/{total_users} active users")

        except Exception as error:
            logging.error(f"Monitor error: {error}")
            await asyncio.sleep(60)


async def safe_check_payments(user_id):
    """Safely check payments for a user with error isolation"""
    try:
        await check_new_payments(user_id)
    except Exception as e:
        logging.debug(f"Error checking user {user_id}: {e}")


async def get_balance(user_id):
    """Get wallet balance for a user"""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        info = await sdk.get_info(request=GetInfoRequest(ensure_synced=True))
        balance_sats = info.balance_sats
        return str(balance_sats)
    except Exception as error:
        logging.error(f"Error getting balance: {error}")
        return "0"
    finally:
        if sdk:
            await close_wallet(sdk)


async def init(user_id):
    """Initialize new user wallet with mnemonic"""
    users = db.get(f'users')
    if user_id not in users:
        words = mnemo.generate(strength=128)
        logging.info(f"New wallet created for user {user_id}")
        db.set_default(f'{user_id}', words)
        users.append(user_id)
        db.set(f'users', users)


async def get_wallet(user_id):
    """Get or create wallet SDK instance with connection management"""
    try:
        mnemonic = db.get(f'{user_id}')
        seed = Seed.MNEMONIC(mnemonic=mnemonic, passphrase=None)
        sdk = await connect(
            request=ConnectRequest(config=config, seed=seed, storage_dir=f"./.{user_id}")
        )
        return sdk
    except Exception as error:
        logging.error(f"Error connecting wallet for user {user_id}: {error}")
        raise


async def close_wallet(sdk):
    """Safely close wallet connection"""
    try:
        if sdk:
           await sdk.disconnect()
    except Exception as error:
        logging.debug(f"Error closing wallet: {error}")


async def create_invoice(user_id, amount_sats=None, description="Zap payment"):
    """Create Lightning invoice"""
    try:
        sdk = await get_wallet(user_id)
        payment_method = ReceivePaymentMethod.BOLT11_INVOICE(
            description=description,
            amount_sats=amount_sats
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
            payment_method=ReceivePaymentMethod.BITCOIN_ADDRESS()
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


async def get_lightning_address(user_id):
    """Get or register Lightning address"""
    try:
        sdk = await get_wallet(user_id)
        Address = await sdk.get_lightning_address()
        if not Address:
            uid = ''.join(choice(ascii_lowercase) for i in range(6))
            request = RegisterLightningAddressRequest(username=uid, description="Zap Zap")
            Address = await sdk.register_lightning_address(request=request)
        return Address
    except Exception as error:
        logging.error(f"Error get Lightning address: {error}")
        raise


async def parse_input(sdk , input_str):
    """Parse Lightning invoice, address, or Bitcoin address"""
    try:
        parsed = await sdk.parse(input=input_str)
        return parsed
    except Exception as error:
        logging.error(f"Error parsing input: {error}")
        raise

async def resolve_username(username):
    """Resolve Telegram username to Lightning Address"""
    try:
        user = await client.get_entity(username)
        receiver_id = user.id
        await init(receiver_id)
        receiver_invoice = await create_invoice(receiver_id)
        return receiver_invoice
    except Exception as error:
        logging.error(f"Error resolving username: {error}")
        raise


async def prepare_payment(user_id, invoice, amount_sats=None):
    """Prepare payment and get fee information"""
    try:
        sdk = await get_wallet(user_id)

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
            amount_sats=amount_sats,
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


async def lnurl_pay(user_id, prepare_response):
    """Execute LNURL payment"""
    try:
        sdk = await get_wallet(user_id)
        response = await sdk.lnurl_pay(LnurlPayRequest(prepare_response=prepare_response))
        return response
    except Exception as error:
        logging.error(f"Error LNURL pay: {error}")
        raise


def get_main_buttons():
    """Get main menu buttons"""
    return [
        [Button.inline("📤 Send", b"send"), Button.inline("📥 Receive", b"receive")],
        [Button.inline("📜 History", b"history"), Button.inline("🔄 Refresh", b"refresh")],
        [Button.inline("⚙️ Settings", b"settings"), Button.inline("❓ Help", b"help")],
        [Button.inline("💵 USD Rate", b"usd_rate"), Button.inline("💝 Donate", b"donate")],
    ]


async def show_main_menu(event, edit=False):
    """Show main wallet menu with USD value"""
    user_id = event.sender_id
    await init(user_id)

    # Mark user as active for payment monitoring
    await mark_user_active(user_id)

    balance = await get_balance(user_id)
    balance_display = await format_balance_with_usd(balance)

    message = f"""💳 **Your Wallet**

💰 **Balance:** {balance_display}

⚠️ **Important:** Please backup your wallet from **Settings** to avoid losing access!

Choose an option:
"""

    if edit and hasattr(event, 'edit'):
        await event.edit(message, buttons=get_main_buttons())
    elif edit and hasattr(event, 'message'):
        await event.message.edit(message, buttons=get_main_buttons())
    else:
        await event.respond(message, buttons=get_main_buttons())


async def show_help_menu(event):
    """Show help and guide for using the bot"""
    help_text = """📖 **Bot Guide & Help**

**🚀 Getting Started:**
1. Use /start to open your wallet
2. **⚠️ IMPORTANT: Backup your seed phrase in Settings!**
3. Never share your seed phrase with anyone
4. Start sending and receiving Bitcoin!

**🔐 Backup Your Wallet (CRITICAL!):**
• Go to Settings → Backup Seeds
• Save your 12-word recovery phrase
• Store it safely offline
• **Without backup, you may lose your funds!**

**📤 Sending Payments:**
• Click "Send" button
• Paste Lightning Invoice, Lightning Address, Bitcoin Address, or LNURL
• Confirm the amount and fees
• Done! ⚡

**📥 Receiving Payments:**
• Click "Receive" button
• Choose Lightning or Onchain
• Share your Lightning Address or Invoice
• Get paid instantly!

**⚡ Zap Command (Groups):**
Use `/zap` command in groups to tip others:

**Method 1: Reply to message**
Reply to someone's message and type:
`/zap 1000`

**Method 2: Mention username**
`/zap 1000 @username`

**💵 NEW! Dollar Amount:**
`/zap $5` - Send $5 worth of sats
`/zap $10 @alice` - Send $10 to @alice

**Examples:**
• `/zap 500` - Send 500 sats to replied user
• `/zap 2100 @alice` - Send 2100 sats to @alice
• `/zap $1` - Send $1 worth of sats
• `/zap $5 @bob` - Send $5 to @bob

**📜 Transaction History:**
Click "History" to view your recent transactions

**⚙️ Settings:**
• Backup Seeds - Save your recovery phrase
• Recovery Wallet - Restore from seed
• Change Lightning Address - Customize your address
• **Notifications** - Enable payment alerts (OFF by default)

**💵 USD Rate:**
• View current BTC/USD exchange rate
• See your balance in USD
• Send/receive shows USD equivalent

**🔔 Payment Notifications:**
• Go to Settings → Notifications
• Turn ON to receive instant payment alerts
• Get notified when you receive payments
• Auto-pauses after 24h inactivity
• **Note:** Disabled by default to save resources

**💡 Tips:**
• Keep your seed phrase safe and private
• Small amounts = Lightning (instant)
• Large amounts = Onchain (secure)
• Check fees before confirming
• Always backup before receiving large amounts

**Need more help?**
Contact: @zap_ln
"""

    await event.edit(
        help_text,
        buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
    )


async def show_transaction_history(event, user_id, page=0):
    """Show transaction history with pagination and USD values"""
    try:
        await event.answer("📜 Loading history...")

        payments = await get_payment_history(user_id, limit=50)

        # Update last payment timestamp when viewing history
        if payments and user_id in user_last_payment_time:
            latest_timestamp = getattr(payments[0], 'timestamp', 0)
            if latest_timestamp > 0:
                user_last_payment_time[user_id] = latest_timestamp
                logging.debug(f"Updated timestamp for user {user_id} from history view")

        if not payments:
            await event.edit(
                "📜 **Transaction History**\n\n"
                "No transactions yet.\n\n"
                "Start using your wallet to see transactions here!",
                buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
            )
            return

        # Pagination
        items_per_page = 10
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        page_payments = payments[start_idx:end_idx]

        # Get USD rate for display
        usd_rate = await get_usd_rate()

        history_text = f"**Transaction History** (Page {page + 1})\n\n"

        for payment in page_payments:
            # Get payment details
            payment_type = getattr(payment, 'payment_type', 'Unknown')
            amount = getattr(payment, 'amount_sats', getattr(payment, 'amount', 0))
            timestamp = getattr(payment, 'timestamp', 0)
            status = getattr(payment, 'status', 'Unknown')

            # Format timestamp - smart detection for seconds vs milliseconds
            if timestamp:
                try:
                    # If timestamp > 1e12, it's in milliseconds
                    if timestamp > 1000000000000:
                        dt = datetime.fromtimestamp(timestamp / 1000)
                    else:
                        dt = datetime.fromtimestamp(timestamp)
                    time_str = dt.strftime('%Y-%m-%d %H:%M')
                except Exception as e:
                    logging.debug(f"Timestamp conversion error: {e}, timestamp={timestamp}")
                    time_str = 'Unknown'
            else:
                time_str = 'Unknown'

            # Determine if incoming or outgoing
            is_incoming = False
            if hasattr(payment, 'direction'):
                direction = payment.direction
                if direction == PaymentDirection.INCOMING:
                    is_incoming = True
                    icon = "📥"
                    amount_str = f"+{amount}"
                    direction_text = "Received"
                else:
                    icon = "📤"
                    amount_str = f"-{amount}"
                    direction_text = "Sent"
            else:
                # Fallback: check payment_type
                icon = "💳"
                amount_str = str(amount)
                direction_text = "Payment"

            # Add USD value
            usd_str = ""
            if usd_rate:
                usd_value = sats_to_usd(int(amount), usd_rate)
                if usd_value is not None:
                    usd_str = f" (~${usd_value})"

            # Status emoji
            status_str = str(status).lower()
            if 'complete' in status_str or 'succeeded' in status_str:
                status_icon = "✅"
                status_text = "Complete"
            elif 'pending' in status_str:
                status_icon = "⏳"
                status_text = "Pending"
            elif 'failed' in status_str:
                status_icon = "❌"
                status_text = "Failed"
            else:
                status_icon = "⚪"
                status_text = str(status)

            # Format payment type
            type_str = str(payment_type).replace('PaymentType.', '')

            history_text += (
                f"{icon} **{direction_text}** {status_icon}\n"
                f"   💰 {amount_str} sats{usd_str}\n"
                f"   📅 {time_str}\n"
                f"   🏷️ {type_str}\n\n"
            )

        # Pagination buttons
        buttons = []
        nav_buttons = []

        if page > 0:
            nav_buttons.append(Button.inline("⬅️ Previous", f"history_page_{page - 1}".encode()))

        if end_idx < len(payments):
            nav_buttons.append(Button.inline("Next ➡️", f"history_page_{page + 1}".encode()))

        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([Button.inline("« Back to Menu", b"back_to_menu")])

        await event.edit(history_text, buttons=buttons)

    except Exception as error:
        await event.edit(
            f"❌ Error loading history:\n{error}",
            buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
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

        await res.edit(
            f"📋 **Payment Summary**\n\n"
            f"💰 Amount: {amount_usd}\n"
            f"⚡ Fee: {fee_usd}\n"
            f"💳 Total: {total_usd}\n\n"
            f"Confirm payment?",
            buttons=[
                [
                    Button.inline("✅ Confirm", b"confirm_payment_yes"),
                    Button.inline("❌ Cancel", b"back_to_menu")
                ]
            ]
        )

    except Exception as error:
        await res.edit(
            f"❌ Error preparing payment:\n{error}",
            buttons=[[Button.inline("« Back", b"back_to_menu")]]
        )
        if user_id in user_steps:
            del user_steps[user_id]


# ==================== Event Handlers ====================

@client.on(events.NewMessage(func=lambda e: e.is_private, pattern='/start'))
async def start_command(event):
    """Handle /start command"""
    # Mark user as active
    await mark_user_active(event.sender_id)

    await show_main_menu(event)


@client.on(events.NewMessage(func=lambda e: e.is_private, pattern='/debug'))
async def debug_command(event):
    """Debug command to check notification system status"""
    user_id = event.sender_id

    # Check if user is being monitored
    if user_id in user_last_payment_time:
        last_payment_time = user_last_payment_time[user_id]
        last_activity = user_last_activity.get(user_id)
        activity_str = last_activity.strftime('%Y-%m-%d %H:%M:%S') if last_activity else 'Never'

        # Convert timestamp to readable format - smart detection
        if last_payment_time > 0:
            try:
                # If timestamp > 1e12, it's in milliseconds
                if last_payment_time > 1000000000000:
                    dt = datetime.fromtimestamp(last_payment_time / 1000)
                else:
                    dt = datetime.fromtimestamp(last_payment_time)
                payment_time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception as e:
                logging.debug(f"Timestamp error: {e}")
                payment_time_str = 'Unknown'
        else:
            payment_time_str = 'Not initialized'

        msg = (
            f"**Notification System Active**\n\n"
            f"User ID: `{user_id}`\n"
            f"Last Payment Time: {payment_time_str}\n"
            f"Last Activity: {activity_str}\n"
            f"Total users: {len(user_last_payment_time)}\n\n"
            f"Status: **Monitoring**"
        )
    else:
        msg = (
            f"**Not Monitored**\n\n"
            f"User ID: `{user_id}`\n"
            f"Total users: {len(user_last_payment_time)}\n\n"
            f"Use /start to activate monitoring"
        )

    await event.respond(msg)


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
        if data == "done":
            await event.delete()
            return

        if data == "refresh":
            await event.edit("⏳")
            await show_main_menu(event, edit=True)
            await event.answer("✅ Balance refreshed")
            return

        elif data == "help":
            await event.answer()
            await show_help_menu(event)
            return

        elif data == "usd_rate":
            await event.answer()
            rates = await get_fiat_rates()
            usd_rate = rates.get('USD', None)

            if usd_rate:
                # Calculate some example conversions
                btc_price = usd_rate
                sats_per_dollar = usd_to_sats(1, usd_rate)

                balance = await get_balance(user_id)
                balance_usd = sats_to_usd(int(balance), usd_rate)

                await event.edit(
                    f"💵 **USD Exchange Rate**\n\n"
                    f"₿ **BTC/USD:** ${btc_price:,.2f}\n"
                    f"💰 **1 USD =** {sats_per_dollar:,} sats\n\n"
                    f"💳 **Your Balance:**\n"
                    f"{balance} sats = ${balance_usd:.2f} USD\n\n"
                    f"📊 **Quick Reference:**\n"
                    f"100 sats = ${sats_to_usd(100, usd_rate):.4f}\n"
                    f"1,000 sats = ${sats_to_usd(1000, usd_rate):.3f}\n"
                    f"10,000 sats = ${sats_to_usd(10000, usd_rate):.2f}\n"
                    f"100,000 sats = ${sats_to_usd(100000, usd_rate):.2f}\n\n"
                    f"🕐 Last updated: {fiat_rates_cache['last_update'].strftime('%H:%M:%S') if fiat_rates_cache['last_update'] else 'N/A'}",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
            else:
                await event.edit(
                    "❌ Unable to fetch exchange rate.\nPlease try again later.",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
            return

        elif data == "history":
            await show_transaction_history(event, user_id, page=0)
            return

        elif data.startswith("history_page_"):
            page = int(data.split("_")[2])
            await show_transaction_history(event, user_id, page=page)
            return

        elif data == "settings":
            await event.answer()

            await event.edit(
                "⚙️ **Settings**\n\n"
                "Choose an option:",
                buttons=[
                    [Button.inline("🔐 Backup Seeds", b"backup")],
                    [Button.inline("🔄 Recovery Wallet", b"recovery")],
                    [Button.inline("⚡ Change Lightning Address", b"change_ln_address")],
                    [Button.inline("🔔 Notifications", b"notifications_menu")],
                    [Button.inline("« Back to Menu", b"back_to_menu")]
                ]
            )
            return

        elif data == "notifications_menu":
            await event.answer()

            # Check notification status
            if user_id in user_last_payment_time:
                notif_status = "✅ **Enabled**"
                notif_detail = "You're receiving payment notifications every 20 seconds."
                toggle_text = "🔕 Turn OFF"
                toggle_action = b"notif_turn_off"
            else:
                # Check if manually disabled or auto-paused
                notification_data = db.get('notification_users')
                user_data_db = notification_data.get(str(user_id), {})
                disabled_reason = user_data_db.get('disabled_reason', None)

                if disabled_reason == 'manual':
                    notif_status = "❌ **Disabled**"
                    notif_detail = "You've turned off notifications manually."
                elif disabled_reason == 'auto_inactive':
                    notif_status = "⏸️ **Paused**"
                    notif_detail = "Auto-paused due to 24h inactivity. Will resume when you use the bot."
                else:
                    notif_status = "❌ **Not Enabled**"
                    notif_detail = "Turn on to receive instant payment alerts."

                toggle_text = "🔔 Turn ON"
                toggle_action = b"notif_turn_on"

            await event.edit(
                f"🔔 **Notifications**\n\n"
                f"Status: {notif_status}\n\n"
                f"{notif_detail}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚙️ **Settings:**\n"
                f"• Check interval: 20 seconds\n"
                f"• Auto-pause: After 24h inactivity\n"
                f"• Only RECEIVE payments notify\n",
                buttons=[
                    [Button.inline(toggle_text, toggle_action)],
                    [Button.inline("« Back to Settings", b"settings")]
                ]
            )
            return

        elif data == "notif_turn_off":
            # Disable notifications
            await disable_notifications(user_id, reason='manual')
            await event.answer("🔕 Notifications disabled")

            await event.edit(
                "🔔 **Notifications**\n\n"
                "Status: ❌ **Disabled**\n\n"
                "You've turned off notifications manually.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚙️ **Settings:**\n"
                "• Check interval: 20 seconds\n"
                "• Auto-pause: After 24h inactivity\n"
                "• Only RECEIVE payments notify\n",
                buttons=[
                    [Button.inline("🔔 Turn ON", b"notif_turn_on")],
                    [Button.inline("« Back to Settings", b"settings")]
                ]
            )
            return

        elif data == "notif_turn_on":
            # Enable notifications (force enable even if manually disabled)
            await mark_user_active(user_id)

            # Force enable
            if user_id not in user_last_payment_time:
                asyncio.create_task(check_new_payments(user_id))
                save_notification_to_db(user_id, enabled=True)

            await event.answer("🔔 Notifications enabled")

            await event.edit(
                "🔔 **Notifications**\n\n"
                "Status: ✅ **Enabled**\n\n"
                "You're receiving payment notifications every 20 seconds.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚙️ **Settings:**\n"
                "• Check interval: 20 seconds\n"
                "• Auto-pause: After 24h inactivity\n"
                "• Only RECEIVE payments notify\n",
                buttons=[
                    [Button.inline("🔕 Turn OFF", b"notif_turn_off")],
                    [Button.inline("« Back to Settings", b"settings")]
                ]
            )
            return

        elif data == "backup":
            seed = db.get(f'{user_id}')
            await event.answer()
            await event.edit(
                f"🔐 **Your recovery phrase:**\n\n"
                f"`{seed}`\n\n"
                f"⚠️ Keep it safe and never share!",
                buttons=[[Button.inline("« Back", b"settings_back")]]
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
                await event.edit(
                    f"⚡ **Your Current Lightning Address:**\n"
                    f"`{address.lightning_address}`\n\n"
                    f"🔗 **LNURL:**\n"
                    f"`{address.lnurl}`\n\n"
                    "Would you like to change it?",
                    buttons=[
                        [Button.inline("✏️ Change Address", b"confirm_change_ln_address")],
                        [Button.inline("« Back", b"settings")]
                    ]
                )
            except Exception as error:
                await event.answer(f"❌ Error: {error}")

        elif data == "confirm_change_ln_address":
            user_steps[user_id] = "change_ln_address_input"
            await event.edit(
                "✏️ **Change Lightning Address**\n\n"
                "Enter your desired username:\n"
                "(Only lowercase letters, numbers, and underscores)\n\n"
                "Example: `musa_wallet`\n\n"
                "⚠️ Note: Username must be unique!",
                buttons=[[Button.inline("« Cancel", b"settings_back")]]
            )

        elif data == "toggle_notifications":
            # Toggle notification status
            if user_id in user_last_payment_time:
                # Disable
                del user_last_payment_time[user_id]
                if user_id in user_last_activity:
                    del user_last_activity[user_id]
                status = "Disabled"
                message = "**Notifications Disabled**\n\nYou won't receive payment notifications anymore.\n\nYou can re-enable anytime from Settings."
            else:
                # Enable
                await mark_user_active(user_id)
                status = "Enabled"
                message = "**Notifications Enabled**\n\nYou'll receive instant notifications for incoming payments!\n\nChecks every 20 seconds."

            await event.answer(f"Notifications {status}")
            await event.edit(
                message,
                buttons=[[Button.inline("Back to Settings", b"settings")]]
            )

        elif data == "receive":
            await event.answer()
            address = await get_lightning_address(user_id)

            # Get balance with USD
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance)

            await event.edit(
                f"📥 **Receive Payment**\n\n"
                f"💰 **Current Balance:** {balance_display}\n\n"
                f"⚡ **Your Lightning Address:**\n`{address.lightning_address}`\n\n"
                f"🔗 **LNURL:**\n`{address.lnurl}`\n\n"
                f"Choose receive method:",
                buttons=[
                    [Button.inline("⚡ Lightning Invoice", b"receive_lightning")],
                    [Button.inline("₿ Onchain Address", b"receive_onchain")],
                    [Button.inline("« Back", b"back_to_menu")]
                ]
            )

        elif data == "receive_lightning":
            await event.answer()
            address = await get_lightning_address(user_id)

            # Get USD rate for reference
            usd_rate = await get_usd_rate()
            usd_info = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                usd_info = f"\n💡 **Tip:** $1 = {sats_per_dollar:,} sats"

            await event.edit(
                f"📥 **Receive via Lightning**\n\n"
                f"⚡ **Your Lightning Address:**\n`{address.lightning_address}`\n\n"
                f"🔗 **LNURL:**\n`{address.lnurl}`{usd_info}\n\n"
                f"💬 Would you like to add a memo/description to your invoice?",
                buttons=[
                    [Button.inline("💬 Add Memo", b"receive_add_memo")],
                    [Button.inline("⏭ Skip", b"receive_skip_memo")],
                    [Button.inline("« Back", b"receive")]
                ]
            )

        elif data == "receive_onchain":
            await event.answer()
            try:
                msg = await event.edit("⏳ Generating onchain address...")

                btc_address, receive_fee = await create_onchain_address(user_id)
                fee_display = await format_amount_with_usd(receive_fee)

                await msg.edit(
                    f"📥 **Receive via Onchain**\n\n"
                    f"₿ **Bitcoin Address:**\n`{btc_address}`\n\n"
                    f"⚡ **Receive Fee:** {fee_display}\n\n"
                    f"⚠️ **Note:** Funds will appear after blockchain confirmation\n"
                    f"💡 Use Lightning for instant payments!",
                    buttons=[
                        [Button.inline("« Back to Receive", b"receive")],
                        [Button.inline("« Back to Menu", b"back_to_menu")]
                    ]
                )
            except Exception as error:
                await event.edit(
                    f"❌ Error generating address:\n{error}",
                    buttons=[[Button.inline("« Back", b"receive")]]
                )

        elif data == "receive_add_memo":
            user_steps[user_id] = "receive_memo"
            user_data[user_id] = {}
            await event.edit(
                "💬 **Add Memo**\n\n"
                "Enter a description/memo for your invoice:",
                buttons=[[Button.inline("« Cancel", b"receive_lightning")]]
            )

        elif data == "receive_skip_memo":
            await event.answer()
            try:
                invoice = await create_invoice(user_id, amount_sats=None)
                await event.edit(
                    f"📥 **Your Invoice** (no amount set)\n\n"
                    f"`{invoice}`\n\n"
                    "This invoice can accept any amount. Want to set a specific amount?",
                    buttons=[
                        [Button.inline("💵 Set Amount", b"receive_set_amount_no_memo")],
                        [Button.inline("« Back", b"receive_lightning")]
                    ]
                )
            except Exception as error:
                await event.edit(f"❌ Error: {error}")

        elif data == "receive_set_amount":
            user_steps[user_id] = "receive_amount"

            # Get USD rate for hint
            usd_rate = await get_usd_rate()
            hint = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                hint = f"\n\n💡 Tip: $1 = {sats_per_dollar:,} sats, $5 = {usd_to_sats(5, usd_rate):,} sats"

            await event.edit(
                f"💵 **Set Invoice Amount**\n\n"
                f"Enter the amount in sats:\n"
                f"Or use $ prefix for USD (e.g., $5){hint}",
                buttons=[[Button.inline("« Cancel", b"receive_lightning")]]
            )

        elif data == "receive_set_amount_no_memo":
            user_steps[user_id] = "receive_amount_no_memo"

            # Get USD rate for hint
            usd_rate = await get_usd_rate()
            hint = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                hint = f"\n\n💡 Tip: $1 = {sats_per_dollar:,} sats, $5 = {usd_to_sats(5, usd_rate):,} sats"

            await event.edit(
                f"💵 **Set Invoice Amount**\n\n"
                f"Enter the amount in sats:\n"
                f"Or use $ prefix for USD (e.g., $5){hint}",
                buttons=[[Button.inline("« Cancel", b"receive_lightning")]]
            )

        elif data == "send":
            user_steps[user_id] = "send_invoice"
            await event.answer()

            # Get balance with USD
            balance = await get_balance(user_id)
            balance_display = await format_balance_with_usd(balance)

            await event.edit(
                f"📤 **Send Payment**\n\n"
                f"💰 **Available Balance:** {balance_display}\n\n"
                f"Send me one of:\n"
                f"• Lightning Invoice\n"
                f"• Lightning Address (user@domain.com)\n"
                f"• Telegram Username (@username)\n"
                f"• Bitcoin Address\n"
                f"• LNURL",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
            )

        elif data == "back_to_menu":
            if user_id in user_steps:
                del user_steps[user_id]
            if user_id in user_data:
                del user_data[user_id]
            await show_main_menu(event, edit=True)

        elif data == "settings_back":
            if user_id in user_steps:
                del user_steps[user_id]
            if user_id in user_data:
                del user_data[user_id]

            await event.edit(
                "⚙️ **Settings**\n\n"
                "Choose an option:",
                buttons=[
                    [Button.inline("🔐 Backup Seeds", b"backup")],
                    [Button.inline("🔄 Recovery Wallet", b"recovery")],
                    [Button.inline("⚡ Change Lightning Address", b"change_ln_address")],
                    [Button.inline("🔔 Notifications", b"notifications_menu")],
                    [Button.inline("« Back to Menu", b"back_to_menu")]
                ]
            )

        elif data == "donate":
            await event.answer()
            await event.edit(
                "💝 **Support This Bot**\n\n"
                "Thank you for considering to support!\n\n"
                "You can donate in two ways:\n\n"
                "1️⃣ **Zap directly to this bot:**\n"
                "   Just send a payment to the bot!\n\n"
                "2️⃣ **Send to developer:**\n"
                "   `musa@breez.tips`\n\n"
                "Every zap helps keep this bot running! ⚡",
                buttons=[
                    [Button.inline("⚡ Zap Now", b"donate_send")],
                    [Button.inline("« Back to Menu", b"back_to_menu")]
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
                f"💝 **Donate Amount**\n\n"
                f"How many sats would you like to donate?\n\n"
                f"Suggested amounts:\n"
                f"• 1,000 sats (☕ Coffee)\n"
                f"• 5,000 sats (🍕 Pizza)\n"
                f"• 10,000 sats (❤️ Supporter)\n"
                f"• 21,000 sats (🚀 Champion!){usd_info}\n\n"
                f"Enter amount (sats or $USD):",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
            )
            if user_id not in user_data:
                user_data[user_id] = {}
            user_data[user_id]['last_message_id'] = msg.id

        elif data.startswith("confirm_payment_"):
            if user_id in user_data and 'prepare_response' in user_data[user_id]:
                try:
                    await event.answer("⚡ Processing payment...")
                    prepare_response = user_data[user_id]['prepare_response']
                    payment_type = user_data[user_id].get('payment_type', 'bolt11')
                    fee = user_data[user_id].get('fee', 0)

                    if payment_type == 'lnurl':
                        response = await lnurl_pay(user_id, prepare_response)
                        amount = user_data[user_id].get('amount', 'N/A')
                        amount_display = await format_amount_with_usd(amount) if isinstance(amount, int) else amount
                        fee_display = await format_amount_with_usd(fee)

                        await event.edit(
                            f"✅ **Payment Successful!**\n\n"
                            f"💰 Amount: {amount_display}\n"
                            f"⚡ Fee: {fee_display}\n"
                            f"Type: LNURL-Pay"
                        )
                    else:
                        options = user_data[user_id].get('options')
                        payment = await send_payment(user_id, prepare_response, options)

                        payment_id = getattr(payment, 'id', 'N/A')
                        amount = getattr(payment, 'amount', user_data[user_id].get('amount', 'N/A'))
                        amount_display = await format_amount_with_usd(amount) if isinstance(amount, int) else amount
                        fee_display = await format_amount_with_usd(fee)

                        await event.edit(
                            f"✅ **Payment Successful!**\n\n"
                            f"💰 Amount: {amount_display}\n"
                            f"⚡ Fee: {fee_display}\n"
                            f"🆔 ID: `{payment_id}`\n"
                            f"🧭 Explore: https://sparkscan.io"
                        )

                    del user_data[user_id]
                    if user_id in user_steps:
                        del user_steps[user_id]

                    await show_main_menu(event)
                except Exception as error:
                    await event.edit(
                        f"❌ **Payment Failed**\n\n{error}",
                        buttons=[[Button.inline("« Back", b"back_to_menu")]]
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
                    buttons=[[Button.inline("« Back", b"back_to_menu")]]
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
                    ]
                ]
            )

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
                            f"**Withdraw All - Onchain**\n\n"
                            f"Balance: {balance_display}\n\n"
                            f"**Fee Options:**\n"
                            f"Slow: {slow_fee} sats -> Send {slow_amount} sats\n"
                            f"Medium: {medium_fee} sats -> Send {medium_amount} sats\n"
                            f"Fast: {fast_fee} sats -> Send {fast_amount} sats\n\n"
                            f"Choose confirmation speed:",
                            buttons=[
                                [
                                    Button.inline("Slow", b"onchain_speed_slow_all"),
                                    Button.inline("Medium", b"onchain_speed_medium_all"),
                                    Button.inline("Fast", b"onchain_speed_fast_all")
                                ],
                                [Button.inline("Cancel", b"back_to_menu")]
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
                                    f"**Insufficient Balance**\n\n"
                                    f"Balance: {balance} sats\n"
                                    f"Minimum: {min_sats} sats",
                                    buttons=[[Button.inline("Back", b"back_to_menu")]]
                                )
                                return

                            send_amount = min(balance, max_sats)
                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, send_amount)
                            final_amount = send_amount - fee_sats

                            if final_amount <= 0:
                                await res.edit("Balance too low after fees", buttons=[[Button.inline("Back", b"back_to_menu")]])
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
                                f"**Withdraw All - Lightning Address**\n\n"
                                f"Balance: {balance_display}\n"
                                f"Fee: {fee_display}\n"
                                f"Sending: {amount_display}\n\n"
                                f"Confirm?",
                                buttons=[
                                    [Button.inline("Confirm", b"confirm_payment_yes"), Button.inline("Cancel", b"back_to_menu")]
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
                                    f"Insufficient balance\n\n"
                                    f"Balance: {balance} sats\n"
                                    f"Fee: {final_fee} sats\n\n"
                                    f"Cannot withdraw all.",
                                    buttons=[[Button.inline("Back", b"back_to_menu")]]
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

                            total = final_amount + final_fee

                            # Format with USD
                            balance_display = await format_balance_with_usd(balance)
                            fee_display = await format_amount_with_usd(final_fee)
                            amount_display = await format_amount_with_usd(final_amount)
                            total_display = await format_amount_with_usd(total)

                            await res.edit(
                                f"**Withdraw All - Lightning**\n\n"
                                f"Balance: {balance_display}\n"
                                f"Fee: {fee_display}\n"
                                f"Amount to send: {amount_display}\n"
                                f"Total: {total_display}\n\n"
                                f"Fee will be deducted from your balance\n\n"
                                f"Confirm withdrawal?",
                                buttons=[
                                    [
                                        Button.inline("Confirm", b"confirm_payment_yes"),
                                        Button.inline("Cancel", b"back_to_menu")
                                    ]
                                ]
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]

                except Exception as error:
                    await event.edit(
                        f"Error: {error}",
                        buttons=[[Button.inline("Back", b"back_to_menu")]]
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
                        f"**Withdraw All Summary**\n\n"
                        f"Balance: {balance_display}\n"
                        f"Fee ({speed}): {fee_display}\n"
                        f"Amount to send: {amount_display}\n\n"
                        f"Fee will be deducted from your balance\n\n"
                        f"Confirm withdrawal?",
                        buttons=[
                            [
                                Button.inline("Confirm", b"confirm_payment_yes"),
                                Button.inline("Cancel", b"back_to_menu")
                            ]
                        ]
                    )
                except Exception as error:
                    await event.answer(f"Error: {error}")

        # Zap confirmation handlers
        elif data.startswith("zap_confirm_"):
            # Extract zap ID from callback data
            zap_id = data.replace("zap_confirm_", "")

            # Find the zap data by zap_id (not by user_id clicking)
            zap_data = None
            zap_owner_id = None
            for uid, zdata in pending_zap_confirmations.items():
                if zdata.get('id') == zap_id:
                    zap_data = zdata
                    zap_owner_id = uid
                    break

            if not zap_data:
                await event.answer("⏰ Zap request expired", alert=True)
                return

            # Check if the user clicking is the sender
            sender_id = zap_data['sender_id']
            if user_id != sender_id:
                await event.answer("⚠️ Only the sender can confirm this zap!", alert=True)
                return

            try:
                await event.edit("⚡ Zapping now...")

                receiver_id = zap_data['receiver_id']
                amount = zap_data['amount_sats']
                amount_display = zap_data.get('amount_display', f"{amount:,} sats")

                # Create invoice and send payment
                receiver_invoice = await create_invoice(receiver_id, amount_sats=amount)
                prepare_response, fee_sats, spark_fee = await prepare_payment(sender_id, receiver_invoice, amount)

                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True, completion_timeout_secs=10)
                payment = await send_payment(sender_id, prepare_response, options)

                # Format success message
                sender_name = zap_data.get('sender_name', str(sender_id))
                receiver_name = zap_data.get('receiver_name', str(receiver_id))

                success_msg = (
                    f"✅ **Zap Successful!**\n\n"
                    f"💰 Amount: {amount_display}\n"
                    f"👤 From: {sender_name}\n"
                    f"👤 To: {receiver_name}\n\n"
                    f"⚡ Transaction complete!"
                )

                # Update the confirmation message
                await event.edit(success_msg)

                # Clean up
                if zap_owner_id in pending_zap_confirmations:
                    del pending_zap_confirmations[zap_owner_id]

            except Exception as error:
                await event.edit(f"❌ Zap failed: {error}")
                if zap_owner_id in pending_zap_confirmations:
                    del pending_zap_confirmations[zap_owner_id]

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
                await event.answer("⏰ Zap request expired", alert=True)
                return

            # Check if the user clicking is the sender
            if user_id != zap_data['sender_id']:
                await event.answer("⚠️ Only the sender can cancel this zap!", alert=True)
                return

            if zap_owner_id in pending_zap_confirmations:
                del pending_zap_confirmations[zap_owner_id]

            await event.edit("❌ Zap cancelled.")
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
            if user_id != balance_data['user_id']:
                await event.answer("⚠️ This is not your balance request!", alert=True)
                return

            # Show balance via answer (private popup)
            await init(user_id)
            balance = await get_balance(user_id)
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
    text = event.text.strip()

    # Mark user as active
    await mark_user_active(user_id)

    current_step = user_steps.get(user_id)

    if not current_step:
        await show_main_menu(event)
        return

    try:
        # Handle donation amount input
        if current_step == "donate_amount":
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

                await client.send_message(
                    user_id,
                    f"**Donation Successful!**\n\n"
                    f"Amount: {amount_display}\n"
                    f"Fee: {fee_display}\n\n"
                    f"Thank you for your support!\n"
                    f"Your donation helps keep this bot running!",
                    buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                )

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
                            "`musa@breez.tips`",
                            buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            f"Donation failed: {error}\n\n"
                            "You can also manually send to:\n"
                            "`musa@breez.tips`",
                            buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                        )
                except:
                    await event.respond(
                        f"Donation failed: {error}\n\n"
                        "You can also manually send to:\n"
                        "`musa@breez.tips`",
                        buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                    )

                if user_id in user_steps:
                    del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]
            return

        # Recovery wallet input
        elif current_step == "recovery_input":
            words = text.strip()
            word_list = words.split()

            if len(word_list) != 12:
                await event.respond(
                    "Invalid recovery phrase. Must be exactly 12 words.",
                    buttons=[[Button.inline("Cancel", b"settings_back")]]
                )
                return

            # Validate mnemonic
            try:
                if not mnemo.check(words):
                    await event.respond(
                        "Invalid recovery phrase. Please check your words.",
                        buttons=[[Button.inline("Cancel", b"settings_back")]]
                    )
                    return
            except:
                await event.respond(
                    "Invalid recovery phrase format.",
                    buttons=[[Button.inline("Cancel", b"settings_back")]]
                )
                return

            # Save new seed
            db.set(f'{user_id}', words)

            del user_steps[user_id]

            await event.respond(
                "**Wallet Recovered!**\n\n"
                "Your wallet has been restored from the recovery phrase.\n"
                "Syncing your balance...",
                buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
            )

        # Change Lightning address input
        elif current_step == "change_ln_address_input":
            username = text.strip().lower()

            # Validate username format
            if not username.replace('_', '').isalnum():
                await event.respond(
                    "Username can only contain lowercase letters, numbers, and underscores.",
                    buttons=[[Button.inline("Cancel", b"settings_back")]]
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
                    buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(
                    f"Error: {error}\n\n"
                    "Username might be taken. Try another one.",
                    buttons=[[Button.inline("Cancel", b"settings_back")]]
                )

        # Receive memo input
        elif current_step == "receive_memo":
            memo = text.strip()
            user_data[user_id]['memo'] = memo
            user_steps[user_id] = "receive_amount"

            # Get USD rate for hint
            usd_rate = await get_usd_rate()
            hint = ""
            if usd_rate:
                sats_per_dollar = usd_to_sats(1, usd_rate)
                hint = f"\n\nTip: $1 = {sats_per_dollar:,} sats"

            await event.respond(
                f"**Set Invoice Amount**\n\n"
                f"Enter the amount in sats or $USD:{hint}",
                buttons=[[Button.inline("Cancel", b"back_to_menu")]]
            )

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

                await event.respond(
                    f"**Your Invoice**\n\n"
                    f"Amount: {amount_display}\n"
                    f"Memo: {memo}\n\n"
                    f"`{invoice}`",
                    buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(f"Error: {error}")

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
                invoice = await create_invoice(user_id, amount_sats=amount)

                del user_steps[user_id]

                amount_display = await format_amount_with_usd(amount)

                await event.respond(
                    f"**Your Invoice**\n\n"
                    f"Amount: {amount_display}\n\n"
                    f"`{invoice}`",
                    buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(f"Error: {error}")

        # ==================== SEND: Parse Invoice ====================
        elif current_step == "send_invoice":
            try:
                # Check if it's a username (starts with @)
                processing_msg = None
                if text.startswith('@'):
                    processing_msg = await event.respond(
                        f"🔍 **Resolving Username**\n\n"
                        f"👤 `{text}`\n\n"
                        f"⏳ Please wait..."
                    )
                    lightning_address = await resolve_username(text)
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
                    input_type_str = "LNURL-Withdraw"
                    if processing_msg:
                        await processing_msg.edit(
                            "⚠️ **LNURL-Withdraw** not supported for sending\n\n"
                            "Please send a valid:\n"
                            "• Lightning Invoice\n"
                            "• Lightning Address\n"
                            "• Bitcoin Address",
                            buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            "⚠️ **LNURL-Withdraw** not supported for sending\n\n"
                            "Please send a valid:\n"
                            "• Lightning Invoice\n"
                            "• Lightning Address\n"
                            "• Bitcoin Address",
                            buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                        )
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

                # If needs amount, ask for it
                if needs_amount:
                    user_steps[user_id] = "send_amount"

                    # Get USD rate for hint
                    usd_rate = await get_usd_rate()
                    hint = ""
                    if usd_rate:
                        sats_per_dollar = usd_to_sats(1, usd_rate)
                        hint = f"\n\n💡 Tip: $1 = {sats_per_dollar:,} sats"

                    # Add withdraw all button
                    buttons = [
                        [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                        [Button.inline("« Cancel", b"back_to_menu")]
                    ]

                    # Edit processing message or send new one
                    if processing_msg:
                        await processing_msg.edit(
                            f"💵 **Enter Amount**\n\n"
                            f"✅ Resolved: `{user_data[user_id].get('pay_request', {})}`\n\n"
                            f"How many sats do you want to send?\n"
                            f"Or use $USD (e.g., $5){hint}\n\n"
                            f"Or withdraw your entire balance:",
                            buttons=buttons
                        )
                    else:
                        await event.respond(
                            f"💵 **Enter Amount**\n\n"
                            f"How many sats do you want to send?\n"
                            f"Or use $USD (e.g., $5){hint}\n\n"
                            f"Or withdraw your entire balance:",
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

                        await res.edit(
                            f"📋 **Payment Summary (LNURL)**\n\n"
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
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
                del user_steps[user_id]
            return

    except Exception as error:
        logging.error(f"Message handler error: {error}")
        await event.respond(
            f"Error: {error}",
            buttons=[[Button.inline("Back to Menu", b"back_to_menu")]]
        )
        if user_id in user_steps:
            del user_steps[user_id]


# ==================== Balance Command ====================
@client.on(events.NewMessage(pattern=r'/zap_balance'))
async def balance_handler(event):
    """Show wallet balance with private button"""
    user_id = event.sender_id

    # Generate unique balance request ID
    import time
    balance_id = f"bal_{user_id}_{int(time.time())}"

    # Store the request
    if not hasattr(balance_handler, 'pending_balance'):
        balance_handler.pending_balance = {}
    balance_handler.pending_balance[balance_id] = {
        'user_id': user_id,
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


# ==================== TIP System with $ support and confirmation ====================
@client.on(events.NewMessage(pattern=r'/zap\s+(\$?\d+(?:\.\d+)?)\s*(?:@(\w+))?'))
async def tip_handler(event):
    """Handle /zap command in groups with username support and $ flag

    Examples:
    - /zap 1000 - Send 1000 sats (reply to message)
    - /zap 1000 @username - Send 1000 sats to @username
    - /zap $5 - Send $5 worth of sats (reply to message)
    - /zap $10 @username - Send $10 to @username
    """
    if event.is_private:
        await event.reply("⚡ Zap command only works in groups!")
        return

    try:
        match = event.pattern_match
        amount_str = match.group(1)
        username = match.group(2)
        sender_id = event.sender_id
        receiver_id = None

        # Parse amount (sats or USD)
        usd_rate = await get_usd_rate()

        if amount_str.startswith('$'):
            # USD amount
            usd_amount = float(amount_str[1:])
            if not usd_rate:
                await event.reply("❌ Unable to get USD rate. Please try with sats amount.")
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

        # Determine receiver
        if username:
            try:
                user = await client.get_entity(username)
                receiver_id = user.id
                if isinstance(user, Channel):
                    receiver_id = int(f"-100{user.id}")
                receiver_name = f"@{username}"
            except Exception as error:
                await event.reply(f"❌ User @{username} not found!")
                return
        elif event.is_reply:
            reply_msg = await event.get_reply_message()
            receiver_id = reply_msg.sender_id
            receiver_name = f"@{reply_msg.sender.username}" if reply_msg.sender.username else str(reply_msg.sender.id)
        else:
            await event.reply(
                "⚡ **How to Zap:**\n\n"
                "Reply to someone's message or use:\n"
                "`/zap <amount> @username`\n\n"
                "**Examples:**\n"
                "• `/zap 1000 @john`\n"
                "• `/zap $5 @alice`"
            )
            return

        if sender_id == receiver_id:
            await event.reply("❌ You can't zap yourself!")
            return

        if amount <= 0:
            await event.reply("❌ Amount must be greater than 0")
            return

        # Initialize wallets
        await init(sender_id)
        await init(receiver_id)

        # Check sender balance
        balance = int(await get_balance(sender_id))
        if balance < amount:
            balance_display = await format_balance_with_usd(balance)
            await event.reply(
                f"❌ **Insufficient balance!**\n\n"
                f"💰 Your balance: {balance_display}\n"
                f"💸 Requested: {amount_display}"
            )
            return

        # Create confirmation request
        sender_name = f"@{event.sender.username}" if event.sender.username else str(event.sender.id)

        # Generate unique zap ID
        import time
        zap_id = f"{sender_id}_{int(time.time())}"

        # Store pending zap
        pending_zap_confirmations[sender_id] = {
            'id': zap_id,
            'sender_id': sender_id,
            'receiver_id': receiver_id,
            'amount_sats': amount,
            'sender_name': sender_name,
            'receiver_name': receiver_name,
            'chat_id': event.chat_id,
            'original_msg_id': event.id,
            'created_at': datetime.now(),
            'amount_display': amount_display
        }

        # Send confirmation request in the SAME CHAT with inline buttons
        await event.reply(
            f"⚡ **Confirm Zap**\n\n"
            f"💰 Amount: {amount_display}\n"
            f"👤 From: {sender_name}\n"
            f"👤 To: {receiver_name}\n\n"
            f"⏳ Waiting for confirmation...",
            buttons=[
                [
                    Button.inline("✅ Confirm", f"zap_confirm_{zap_id}".encode()),
                    Button.inline("❌ Cancel", f"zap_cancel_{zap_id}".encode())
                ]
            ]
        )

    except Exception as error:
        logging.error(f"Zap error: {error}")
        await event.reply(f"❌ Zap failed: {error}")


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
                try:
                    await client.send_message(
                        user_id,
                        "Your zap request has expired. Please try again."
                    )
                except:
                    pass

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

    # Set bot start time for uptime tracking
    bot_start_time = datetime.now()

    print("=" * 60)
    print("ZAP WALLET BOT v17 - USD SUPPORT + OPTIMIZED")
    print("=" * 60)
    print(f"\nStarted at: {bot_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("\nNEW FEATURES IN v17:")
    print("   - USD rate display in menu")
    print("   - Balance shown in sats + USD")
    print("   - Send/Receive with USD conversion")
    print("   - /zap $5 - Send dollar amounts")
    print("   - Zap confirmation before sending")
    print("   - Optimized notification system")
    print("\nNOTIFICATION SYSTEM:")
    print("   - DISABLED by default")
    print("   - Enable in: Settings -> Notifications")
    print("   - Database-backed (survives restarts)")
    print("   - Check interval: 20 seconds")
    print("   - Batch processing: 100 users/cycle")
    print("   - Auto-pause: After 24h inactivity")
    print("\nLoading notification states from database...")

    # Load notification states from DB
    loaded_users = load_notifications_from_db()

    print(f"{loaded_users} user(s) have notifications enabled")
    print("\nNotification monitor will start with bot...")
    print("\nBot is ready and running!")
    print("=" * 60)

    # Start background tasks
    async def start_background_tasks():
        await asyncio.sleep(2)  # Wait for bot to be fully ready
        asyncio.create_task(monitor_active_users())
        asyncio.create_task(cleanup_pending_zaps())
        logging.info("Background tasks started")

        # Pre-fetch fiat rates
        try:
            await get_fiat_rates()
            logging.info("Fiat rates pre-fetched")
        except:
            pass

    # Use client's loop to start background task
    client.loop.create_task(start_background_tasks())

    client.run_until_disconnected()
