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


def load_notifications_from_db():
    """Load notification states from database to memory on startup (thread-safe)"""
    try:
        with db_lock:
            notification_data = db.get('notification_users')
            if not notification_data:
                logging.info("📊 No saved notification states found")
                return 0
            
            loaded = 0
            for user_id_str, data in notification_data.items():
                if data.get('enabled', False):
                    user_id = int(user_id_str)
                    user_last_payment_time[user_id] = data.get('last_timestamp', 0)
                    user_last_activity[user_id] = datetime.now()
                    loaded += 1
            
            logging.info(f"📊 Loaded {loaded} users with notifications enabled")
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
            
        logging.info(f"🔕 Disabled notifications for user {user_id} (reason: {reason})")
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
            
            await client.send_message(
                user_id,
                f"🎉 **Payment Received!**\n\n"
                f"💰 Amount: {amount} sats\n"
                f"⏰ {time_str}\n"
                f"🆔 ID: `{payment_id}`\n\n"
                f"Use /start to see your updated balance!"
            )
            logging.info(f"💰 [{user_id}] Notified: {amount} sats received at {time_str}")
        
    except Exception as error:
        logging.debug(f"Check error [{user_id}]: {error}")


async def monitor_active_users():
    """Background polling - checks active users with smart intervals"""
    batch_size = 50
    check_interval = 30  # 30 seconds for fast notifications
    inactive_threshold = 86400  # 24 hours
    
    while True:
        try:
            await asyncio.sleep(check_interval)
            
            current_time = datetime.now()
            active_users = []
            inactive_users = []
            
            for user_id in list(user_last_payment_time.keys()):
                last_activity = user_last_activity.get(user_id)
                
                if last_activity:
                    time_diff = (current_time - last_activity).total_seconds()
                    
                    if time_diff < inactive_threshold:
                        active_users.append(user_id)
                    else:
                        inactive_users.append(user_id)
                else:
                    inactive_users.append(user_id)
            
            for user_id in inactive_users:
                if user_id in user_last_payment_time:
                    del user_last_payment_time[user_id]
                if user_id in user_last_activity:
                    del user_last_activity[user_id]
            
            if inactive_users:
                logging.info(f"🧹 Cleaned up {len(inactive_users)} inactive users")
            
            if not active_users:
                continue
            
            users_batch = active_users[:batch_size]
            
            for user_id in users_batch:
                try:
                    await check_new_payments(user_id)
                    await asyncio.sleep(0.2)
                except Exception as e:
                    logging.debug(f"Error checking user {user_id}: {e}")
            
            if users_batch:
                logging.debug(f"📊 Checked {len(users_batch)}/{len(active_users)} active users")
        
        except Exception as error:
            logging.error(f"Monitor error: {error}")
            await asyncio.sleep(60)


async def get_balance(user_id):
    """Get wallet balance for a user"""
    sdk = None
    try:
        sdk = await get_wallet(user_id)
        sdk.sync_wallet(request=SyncWalletRequest())
        info = await sdk.get_info(request=GetInfoRequest())
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
            pass #sdk.disconnect()  # No await needed
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
        receive_fee_sats = response.fee_sats
        
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


async def parse_input(input_str):
    """Parse Lightning invoice, address, or Bitcoin address"""
    try:
        parsed = await parse(input=input_str)
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
            amount_sats=amount_sats
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
        [Button.inline("💝 Donate", b"donate")],
    ]


async def show_main_menu(event, edit=False):
    """Show main wallet menu"""
    user_id = event.sender_id
    await init(user_id)
    
    # Mark user as active for payment monitoring
    await mark_user_active(user_id)
    
    balance = await get_balance(user_id)
    
    message = f"""💳 **Your Wallet**

💰 **Balance:** {balance} sats

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

**Examples:**
• `/zap 500` - Send 500 sats to replied user
• `/zap 2100 @alice` - Send 2100 sats to @alice
• `/zap 21000` - Send 21000 sats (reply to message)

**📜 Transaction History:**
Click "History" to view your recent transactions

**⚙️ Settings:**
• Backup Seeds - Save your recovery phrase
• Recovery Wallet - Restore from seed
• Change Lightning Address - Customize your address
• **Notifications** - Enable payment alerts (OFF by default)

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
    """Show transaction history with pagination"""
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
        
        history_text = f"📜 **Transaction History** (Page {page + 1})\n\n"
        
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
                f"   💰 {amount_str} sats\n"
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
    """Prepare payment and show fee confirmation"""
    try:
        res = await event.respond("⚡ Calculating fees...")
        
        prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, amount)
        
        user_data[user_id]['prepare_response'] = prepare_response
        user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True)
        
        # Use spark_fee if available (even if 0), otherwise use fee_sats
        if spark_fee is not None:
            final_fee = spark_fee
        else:
            final_fee = fee_sats if fee_sats else 0
        
        user_data[user_id]['fee'] = final_fee
        total = amount + final_fee if amount else final_fee

        await res.edit(
            f"📋 **Payment Summary**\n\n"
            f"💰 Amount: {amount if amount else 'Variable'} sats\n"
            f"⚡ Fee: {final_fee} sats\n"
            f"💳 Total: {total} sats\n\n"
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
            f"✅ **Notification System Active**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"🕐 Last Payment Time: {payment_time_str}\n"
            f"🕐 Last Activity: {activity_str}\n"
            f"📊 Total users: {len(user_last_payment_time)}\n\n"
            f"Status: **Monitoring** ✓"
        )
    else:
        msg = (
            f"❌ **Not Monitored**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"📊 Total users: {len(user_last_payment_time)}\n\n"
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
                notif_detail = "You're receiving payment notifications every 30 seconds."
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
                f"• Check interval: 30 seconds\n"
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
                "• Check interval: 30 seconds\n"
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
                "You're receiving payment notifications every 30 seconds.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚙️ **Settings:**\n"
                "• Check interval: 30 seconds\n"
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
                status = "❌ Disabled"
                message = "🔕 **Notifications Disabled**\n\nYou won't receive payment notifications anymore.\n\nYou can re-enable anytime from Settings."
            else:
                # Enable
                await mark_user_active(user_id)
                status = "✅ Enabled"
                message = "🔔 **Notifications Enabled**\n\nYou'll receive instant notifications for incoming payments!\n\nChecks every 2 minutes."
            
            await event.answer(f"Notifications {status}")
            await event.edit(
                message,
                buttons=[[Button.inline("« Back to Settings", b"settings")]]
            )
        
        elif data == "receive":
            await event.answer()
            address = await get_lightning_address(user_id)
            await event.edit(
                f"📥 **Receive Payment**\n\n"
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
            await event.edit(
                f"📥 **Receive via Lightning**\n\n"
                f"⚡ **Your Lightning Address:**\n`{address.lightning_address}`\n\n"
                f"🔗 **LNURL:**\n`{address.lnurl}`\n\n"
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
                
                await msg.edit(
                    f"📥 **Receive via Onchain**\n\n"
                    f"₿ **Bitcoin Address:**\n`{btc_address}`\n\n"
                    f"⚡ **Receive Fee:** {receive_fee} sats\n\n"
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
            await event.edit(
                "💵 **Set Invoice Amount**\n\n"
                "Enter the amount in sats:",
                buttons=[[Button.inline("« Cancel", b"receive_lightning")]]
            )
        
        elif data == "receive_set_amount_no_memo":
            user_steps[user_id] = "receive_amount_no_memo"
            await event.edit(
                "💵 **Set Invoice Amount**\n\n"
                "Enter the amount in sats:",
                buttons=[[Button.inline("« Cancel", b"receive_lightning")]]
            )
        
        elif data == "send":
            user_steps[user_id] = "send_invoice"
            await event.answer()
            await event.edit(
                "📤 **Send Payment**\n\n"
                "Send me one of:\n"
                "• Lightning Invoice\n"
                "• Lightning Address (user@domain.com)\n"
                "• Telegram Username (@username)\n"
                "• Bitcoin Address\n"
                "• LNURL",
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
            msg = await event.edit(
                "💝 **Donate Amount**\n\n"
                "How many sats would you like to donate?\n\n"
                "Suggested amounts:\n"
                "• 1,000 sats (☕ Coffee)\n"
                "• 5,000 sats (🍕 Pizza)\n"
                "• 10,000 sats (❤️ Supporter)\n"
                "• 21,000 sats (🚀 Champion!)\n\n"
                "Enter amount:",
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
                        await event.edit(
                            f"✅ **Payment Successful!**\n\n"
                            f"💰 Amount: {amount} sats\n"
                            f"⚡ Fee: {fee} sats\n"
                            f"Type: LNURL-Pay"
                        )
                    else:
                        options = user_data[user_id].get('options')
                        payment = await send_payment(user_id, prepare_response, options)
                        
                        payment_id = getattr(payment, 'id', 'N/A')
                        amount = getattr(payment, 'amount', user_data[user_id].get('amount', 'N/A'))
                        
                        await event.edit(
                            f"✅ **Payment Successful!**\n\n"
                            f"💰 Amount: {amount} sats\n"
                            f"⚡ Fee: {fee} sats\n"
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
        
        elif data.startswith("onchain_speed_"):
            if user_id in user_data:
                speed = data.split("_")[2]
                user_data_obj = user_data[user_id]
                
                if speed == "slow":
                    confirmation_speed = OnchainConfirmationSpeed.SLOW
                elif speed == "medium":
                    confirmation_speed = OnchainConfirmationSpeed.MEDIUM
                else:
                    confirmation_speed = OnchainConfirmationSpeed.FAST
                
                options = SendPaymentOptions.BITCOIN_ADDRESS(
                    confirmation_speed=confirmation_speed
                )
                user_data_obj['options'] = options
                
                selected_fee = user_data_obj['fees'][speed]
                amount = user_data_obj['amount']
                user_data_obj['fee'] = selected_fee
                
                await event.edit(
                    f"📋 **Onchain Payment Summary**\n\n"
                    f"💰 Amount: {amount} sats\n"
                    f"⚡ Fee ({speed}): {selected_fee} sats\n"
                    f"💳 Total: {amount + selected_fee} sats\n\n"
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
                    res = await event.edit("⚡ Calculating fees for withdrawal...")
                    
                    if payment_type == 'onchain':
                        # Get bitcoin address from user_data
                        btc_address = user_data[user_id].get('invoice') or user_data[user_id].get('address')
                        if not btc_address:
                            await event.answer("❌ Bitcoin address missing")
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
                        
                        await res.edit(
                            f"📋 **Withdraw All - Onchain**\n\n"
                            f"💰 Balance: {balance} sats\n\n"
                            f"⚡ **Fee Options:**\n"
                            f"🐢 Slow: {slow_fee} sats → Send {slow_amount} sats\n"
                            f"🚶 Medium: {medium_fee} sats → Send {medium_amount} sats\n"
                            f"🚀 Fast: {fast_fee} sats → Send {fast_amount} sats\n\n"
                            f"Choose confirmation speed:",
                            buttons=[
                                [
                                    Button.inline("🐢 Slow", b"onchain_speed_slow_all"),
                                    Button.inline("🚶 Medium", b"onchain_speed_medium_all"),
                                    Button.inline("🚀 Fast", b"onchain_speed_fast_all")
                                ],
                                [Button.inline("❌ Cancel", b"back_to_menu")]
                            ]
                        )
                    else:
                        # Lightning (BOLT11 or LNURL)
                        if payment_type == 'lnurl':
                            # LNURL Payment
                            pay_request = user_data[user_id].get('pay_request')
                            if not pay_request:
                                await event.answer("❌ Payment data missing")
                                return
                            
                            min_sats = user_data[user_id].get('min_sats', 0)
                            max_sats = user_data[user_id].get('max_sats', 0)
                            
                            # Check limits
                            if balance < min_sats:
                                await res.edit(
                                    f"❌ **Insufficient Balance**\n\n"
                                    f"💰 Balance: {balance} sats\n"
                                    f"📉 Minimum: {min_sats} sats",
                                    buttons=[[Button.inline("« Back", b"back_to_menu")]]
                                )
                                return
                            
                            send_amount = min(balance, max_sats)
                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, send_amount)
                            final_amount = send_amount - fee_sats
                            
                            if final_amount <= 0:
                                await res.edit("❌ Balance too low after fees", buttons=[[Button.inline("« Back", b"back_to_menu")]])
                                return
                            
                            prepare_response, fee_sats = await prepare_lnurl_pay(user_id, pay_request, final_amount)
                            user_data[user_id]['prepare_response'] = prepare_response
                            user_data[user_id]['fee'] = fee_sats
                            user_data[user_id]['amount'] = final_amount
                            
                            await res.edit(
                                f"📋 **Withdraw All - Lightning Address**\n\n"
                                f"💰 Balance: {balance} sats\n"
                                f"⚡ Fee: {fee_sats} sats\n"
                                f"💸 Sending: {final_amount} sats\n\n"
                                f"Confirm?",
                                buttons=[
                                    [Button.inline("✅ Confirm", b"confirm_payment_yes"), Button.inline("❌ Cancel", b"back_to_menu")]
                                ]
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]
                        else:
                            # BOLT11 Invoice
                            invoice = user_data[user_id].get('invoice')
                            if not invoice:
                                await event.answer("❌ Invoice missing")
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
                                    f"❌ Insufficient balance\n\n"
                                    f"Balance: {balance} sats\n"
                                    f"Fee: {final_fee} sats\n\n"
                                    f"Cannot withdraw all.",
                                    buttons=[[Button.inline("« Back", b"back_to_menu")]]
                                )
                                return
                            
                            prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, final_amount)
                            
                            # Use spark_fee if available, otherwise fee_sats
                            if spark_fee is not None:
                                final_fee = spark_fee
                            else:
                                final_fee = fee_sats if fee_sats else 0
                            
                            user_data[user_id]['prepare_response'] = prepare_response
                            user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True)
                            user_data[user_id]['fee'] = final_fee
                            user_data[user_id]['amount'] = final_amount
                            
                            total = final_amount + final_fee
                            
                            await res.edit(
                                f"📋 **Withdraw All - Lightning**\n\n"
                                f"💰 Balance: {balance} sats\n"
                                f"⚡ Fee: {final_fee} sats\n"
                                f"💸 Amount to send: {final_amount} sats\n"
                                f"💳 Total: {total} sats\n\n"
                                f"ℹ️ Fee will be deducted from your balance\n\n"
                                f"Confirm withdrawal?",
                                buttons=[
                                    [
                                        Button.inline("✅ Confirm", b"confirm_payment_yes"),
                                        Button.inline("❌ Cancel", b"back_to_menu")
                                    ]
                                ]
                            )
                            if user_id in user_steps:
                                del user_steps[user_id]
                        
                except Exception as error:
                    await event.edit(
                        f"❌ Error: {error}",
                        buttons=[[Button.inline("« Back", b"back_to_menu")]]
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
                    await event.answer("❌ Insufficient balance after fees")
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
                    
                    await event.edit(
                        f"📋 **Withdraw All Summary**\n\n"
                        f"💰 Balance: {balance} sats\n"
                        f"⚡ Fee ({speed}): {selected_fee} sats\n"
                        f"💸 Amount to send: {final_amount} sats\n\n"
                        f"ℹ️ Fee will be deducted from your balance\n\n"
                        f"Confirm withdrawal?",
                        buttons=[
                            [
                                Button.inline("✅ Confirm", b"confirm_payment_yes"),
                                Button.inline("❌ Cancel", b"back_to_menu")
                            ]
                        ]
                    )
                except Exception as error:
                    await event.answer(f"❌ Error: {error}")
    
    except Exception as error:
        logging.error(f"Callback error: {error}")
        await event.answer(f"❌ Error: {error}")


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
            if not text.isdigit():
                await event.respond("❌ Please enter a valid number in sats")
                return
            
            amount = int(text)
            if amount <= 0:
                await event.respond("❌ Amount must be greater than 0")
                return
            
            try:
                last_message_id = user_data.get(user_id, {}).get('last_message_id')
                
                if last_message_id:
                    try:
                        await client.edit_message(user_id, last_message_id, "⚡ Processing donation...")
                    except:
                        msg = await event.respond("⚡ Processing donation...")
                        last_message_id = msg.id
                else:
                    msg = await event.respond("⚡ Processing donation...")
                    last_message_id = msg.id
                
                await init(DONATE_USER_ID)
                
                donate_invoice = await create_invoice(DONATE_USER_ID, amount_sats=amount, description="Donation - Thank you! ❤️")
                prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, donate_invoice, amount)
                
                if spark_fee is not None:
                    final_fee = spark_fee
                else:
                    final_fee = fee_sats if fee_sats else 0
                
                options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True)
                payment = await send_payment(user_id, prepare_response, options)
                
                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]
                
                try:
                    await client.delete_messages(user_id, last_message_id)
                except:
                    pass
                
                await client.send_message(
                    user_id,
                    f"✅ **Donation Successful!**\n\n"
                    f"💰 Amount: {amount} sats\n"
                    f"⚡ Fee: {final_fee} sats\n\n"
                    f"🙏 Thank you for your support!\n"
                    f"Your donation helps keep this bot running!",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
                
                try:
                    await client.send_message(
                        DONATE_USER_ID,
                        f"🎉 **New Donation Received!**\n\n"
                        f"💰 Amount: {amount} sats\n"
                        f"👤 From: User {user_id}\n"
                        f"🆔 TX: `{getattr(payment, 'id', 'N/A')}`"
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
                            f"❌ Donation failed: {error}\n\n"
                            "You can also manually send to:\n"
                            "`musa@breez.tips`",
                            buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                        )
                    else:
                        await event.respond(
                            f"❌ Donation failed: {error}\n\n"
                            "You can also manually send to:\n"
                            "`musa@breez.tips`",
                            buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                        )
                except:
                    await event.respond(
                        f"❌ Donation failed: {error}\n\n"
                        "You can also manually send to:\n"
                        "`musa@breez.tips`",
                        buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
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
                    "❌ Invalid recovery phrase. Must be exactly 12 words.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return
            
            # Validate mnemonic
            try:
                if not mnemo.check(words):
                    await event.respond(
                        "❌ Invalid recovery phrase. Please check your words.",
                        buttons=[[Button.inline("« Cancel", b"settings_back")]]
                    )
                    return
            except:
                await event.respond(
                    "❌ Invalid recovery phrase format.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return
            
            # Save new seed
            db.set(f'{user_id}', words)
            
            del user_steps[user_id]
            
            await event.respond(
                "✅ **Wallet Recovered!**\n\n"
                "Your wallet has been restored from the recovery phrase.\n"
                "Syncing your balance...",
                buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
            )
        
        # Change Lightning address input
        elif current_step == "change_ln_address_input":
            username = text.strip().lower()
            
            # Validate username format
            if not username.replace('_', '').isalnum():
                await event.respond(
                    "❌ Username can only contain lowercase letters, numbers, and underscores.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
                return
            
            try:
                sdk = await get_wallet(user_id)
                request = RegisterLightningAddressRequest(username=username, description="Zap Zap")
                new_address = await sdk.register_lightning_address(request=request)
                
                del user_steps[user_id]
                
                await event.respond(
                    f"✅ **Lightning Address Updated!**\n\n"
                    f"⚡ New Address: `{new_address.lightning_address}`\n"
                    f"🔗 LNURL: `{new_address.lnurl}`",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(
                    f"❌ Error: {error}\n\n"
                    "Username might be taken. Try another one.",
                    buttons=[[Button.inline("« Cancel", b"settings_back")]]
                )
        
        # Receive memo input
        elif current_step == "receive_memo":
            memo = text.strip()
            user_data[user_id]['memo'] = memo
            user_steps[user_id] = "receive_amount"
            
            await event.respond(
                "💵 **Set Invoice Amount**\n\n"
                "Enter the amount in sats:",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
            )
        
        # Receive amount input (with memo)
        elif current_step == "receive_amount":
            if not text.isdigit():
                await event.respond("❌ Please enter a valid number in sats")
                return
            
            amount = int(text)
            if amount <= 0:
                await event.respond("❌ Amount must be greater than 0")
                return
            
            memo = user_data.get(user_id, {}).get('memo', 'Zap payment')
            
            try:
                invoice = await create_invoice(user_id, amount_sats=amount, description=memo)
                
                del user_steps[user_id]
                if user_id in user_data:
                    del user_data[user_id]
                
                await event.respond(
                    f"📥 **Your Invoice**\n\n"
                    f"💰 Amount: {amount} sats\n"
                    f"💬 Memo: {memo}\n\n"
                    f"`{invoice}`",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(f"❌ Error: {error}")
        
        # Receive amount input (without memo)
        elif current_step == "receive_amount_no_memo":
            if not text.isdigit():
                await event.respond("❌ Please enter a valid number in sats")
                return
            
            amount = int(text)
            if amount <= 0:
                await event.respond("❌ Amount must be greater than 0")
                return
            
            try:
                invoice = await create_invoice(user_id, amount_sats=amount)
                
                del user_steps[user_id]
                
                await event.respond(
                    f"📥 **Your Invoice**\n\n"
                    f"💰 Amount: {amount} sats\n\n"
                    f"`{invoice}`",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
            except Exception as error:
                await event.respond(f"❌ Error: {error}")
        
        # ==================== SEND: Parse Invoice ====================
        elif current_step == "send_invoice":
            try:
                # Check if it's a username (starts with @)
                if text.startswith('@'):
                    lightning_address = await resolve_username(text)
                    await event.respond(
                        f"🔍 **Username Detection**\n\n"
                        f"Resolved: `{text}`\n\n"
                        f"Processing...",
                        buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                    )
                    text = lightning_address
                
                parsed = await parse_input(text)
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
                    await event.respond(
                        "❌ LNURL-Withdraw not supported for sending\n\n"
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
                    
                    # Add withdraw all button
                    buttons = [
                        [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                        [Button.inline("« Cancel", b"back_to_menu")]
                    ]
                    
                    await event.respond(
                        "💵 **Enter Amount**\n\n"
                        "How many sats do you want to send?\n\n"
                        "Or withdraw your entire balance:",
                        buttons=buttons
                    )
                else:
                    # Prepare payment with parsed amount
                    user_steps[user_id] = "send_confirm"
                    await prepare_and_show_fee(event, user_id, invoice_text, parsed_amount)
                
            except Exception as error:
                await event.respond(
                    f"❌ **Invalid Input**\n\n"
                    f"Error: {error}\n\n"
                    f"Please send a valid:\n"
                    f"• Lightning Invoice (lnbc...)\n"
                    f"• Lightning Address (user@domain.com)\n"
                    f"• Telegram Username (@username)\n"
                    f"• Bitcoin Address (bc1...)\n"
                    f"• LNURL\n\n"
                    f"Or cancel:",
                    buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
                )
            return
        
        # Send amount for zero-amount invoice
        # ==================== SEND: Get Amount ====================
        elif current_step == "send_amount":
            if not text.isdigit():
                await event.respond(
                    "❌ Please enter a valid number in sats\n\n"
                    "Try again or cancel:",
                    buttons=[
                        [Button.inline("💸 Withdraw All", b"send_withdraw_all")],
                        [Button.inline("« Cancel", b"back_to_menu")]
                    ]
                )
                return
            
            amount = int(text)
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
                            f"❌ Amount must be between {data['min_sats']} and {data['max_sats']} sats\n\n"
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
                        
                        await res.edit(
                            f"📋 **Payment Summary (LNURL)**\n\n"
                            f"💰 Amount: {amount} sats\n"
                            f"⚡ Fee: {fee_sats} sats\n"
                            f"💳 Total: {total} sats\n\n"
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
                        res = await event.respond("⚡ Calculating fees...")
                        prepare_response, _, _ = await prepare_payment(user_id, data['invoice'], amount)
                        
                        fee_quote = prepare_response.payment_method.fee_quote
                        slow_fee = fee_quote.speed_slow.user_fee_sat + fee_quote.speed_slow.l1_broadcast_fee_sat
                        medium_fee = fee_quote.speed_medium.user_fee_sat + fee_quote.speed_medium.l1_broadcast_fee_sat
                        fast_fee = fee_quote.speed_fast.user_fee_sat + fee_quote.speed_fast.l1_broadcast_fee_sat
                        
                        # Get fee rates (sat/vB)
                        slow_rate = fee_quote.speed_slow.l1_sat_per_vbyte
                        medium_rate = fee_quote.speed_medium.l1_sat_per_vbyte
                        fast_rate = fee_quote.speed_fast.l1_sat_per_vbyte
                        
                        user_data[user_id]['prepare_response'] = prepare_response
                        user_data[user_id]['fees'] = {
                            'slow': slow_fee,
                            'medium': medium_fee,
                            'fast': fast_fee
                        }
                        
                        await res.edit(
                            f"📋 **Onchain Payment Summary**\n\n"
                            f"💰 Amount: {amount} sats\n\n"
                            f"⚡ **Fee Options:**\n"
                            f"🐢 Slow: {slow_fee} sats ({slow_rate} sat/vB)\n"
                            f"🚶 Medium: {medium_fee} sats ({medium_rate} sat/vB)\n"
                            f"🚀 Fast: {fast_fee} sats ({fast_rate} sat/vB)\n\n"
                            f"Choose confirmation speed:",
                            buttons=[
                                [
                                    Button.inline("🐢 Slow", b"onchain_speed_slow"),
                                    Button.inline("🚶 Medium", b"onchain_speed_medium"),
                                    Button.inline("🚀 Fast", b"onchain_speed_fast")
                                ],
                                [Button.inline("❌ Cancel", b"back_to_menu")]
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
                    "❌ Session expired, please start again",
                    buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
                )
                del user_steps[user_id]
            return
        
    except Exception as error:
        logging.error(f"Message handler error: {error}")
        await event.respond(
            f"❌ Error: {error}",
            buttons=[[Button.inline("« Back to Menu", b"back_to_menu")]]
        )
        if user_id in user_steps:
            del user_steps[user_id]


# ==================== Balance Command ====================
@client.on(events.NewMessage(pattern=r'/zap_balance'))
async def balance_handler(event):
    """Show wallet balance"""
    user_id = event.sender_id if event.sender_id else event.chat.id
    
    await init(user_id)
    balance = await get_balance(user_id)
    
    message = f"""💳 **Your Wallet**

💰 **Balance:** {balance} sats"""
    
    await event.reply(message)

# ==================== TIP System ====================
@client.on(events.NewMessage(pattern=r'/zap\s+(\d+)(?:\s+@(\w+))?'))
async def tip_handler(event):
    """Handle /zap command in groups with username support"""
    if event.is_private:
        await event.reply("❌ zap command only works in groups!")
        return
     
    try:
        match = event.pattern_match
        amount = int(match.group(1))
        username = match.group(2)
        sender_id = event.sender_id
        receiver_id = None
        
        if username:
            try:
                user = await client.get_entity(username)
                receiver_id = user.id
                if isinstance(user, Channel):
                    receiver_id = int(f"-100{user.id}")
                
            except Exception as error:
                await event.reply(f"❌ User @{username} not found!")
                return
        elif event.is_reply:
            reply_msg = await event.get_reply_message()
            receiver_id = reply_msg.sender_id
        else:
            await event.reply(
                "❌ Reply to someone's message or use:\n"
                "`/zap <amount> @username`\n\n"
                "Example: `/zap 1000 @john`"
            )
            return
        
        if sender_id == receiver_id:
            await event.reply("❌ You can't zap yourself!")
            return
        
        if amount <= 0:
            await event.reply("❌ Amount must be greater than 0")
            return
        
        _msg = await event.reply(f"🌩 Zapping {amount} sats now...")
        
        await init(sender_id)
        await init(receiver_id)
        
        receiver_invoice = await create_invoice(receiver_id, amount_sats=amount)
        prepare_response, fee_sats, spark_fee = await prepare_payment(sender_id, receiver_invoice, amount)
        
        options = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True)
        payment = await send_payment(sender_id, prepare_response, options)
        
        sender_name = f"@{event.sender.username}" if event.sender.username else str(event.sender.id)
        
        if username:
            receiver_name = f"@{username}"
        elif event.is_reply:
            reply_msg = await event.get_reply_message()
            receiver_name = f"@{reply_msg.sender.username}" if reply_msg.sender.username else str(reply_msg.sender.id)
        else:
            receiver_name = str(receiver_id)
        
        payment_id = getattr(payment, 'id', 'N/A')
        
        await _msg.edit(
            f"**⚡️ Zap confirmed!**\n\n"
            f"💰 Amount: {amount} sats\n"
            f"👤 From: {sender_name}\n"
            f"👤 To: {receiver_name} ID: {receiver_id}"
        )
        
        # Payment notification will be sent automatically by check_new_payments
        
    except Exception as error:
        logging.error(f"Zap error: {error}")
        await event.reply(f"❌ Zap failed: {error}")


if __name__ == "__main__":
    # Increase file descriptor limit to prevent "Too many open files"
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 4096), hard))
        logging.info(f"📊 File descriptor limit: {soft} → {min(hard, 4096)}")
    except Exception as e:
        logging.warning(f"Could not increase file descriptor limit: {e}")
    
    # Set bot start time for uptime tracking
    bot_start_time = datetime.now()
    
    print("=" * 60)
    print("🚀 ZAP WALLET BOT v13 - STABLE VERSION")
    print("=" * 60)
    print(f"\n📡 Started at: {bot_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("\n🔔 NOTIFICATION SYSTEM:")
    print("   ⚠️  DISABLED by default")
    print("   ✅ Enable in: Settings → Notifications")
    print("   ✅ Database-backed (survives restarts)")
    print("   ✅ Check interval: 30 seconds")
    print("   ✅ Auto-pause: After 24h inactivity")
    print("   ✅ Only RECEIVE payments trigger notifications")
    print("\n💾 Loading notification states from database...")
    
    # Load notification states from DB
    loaded_users = load_notifications_from_db()
    
    print(f"📊 {loaded_users} user(s) have notifications enabled")
    print("\n🔔 Notification monitor will start with bot...")
    print("\n✅ Bot is ready and running!")
    print("=" * 60)
    
    # Start monitor in background after event loop is running
    async def start_background_tasks():
        await asyncio.sleep(2)  # Wait for bot to be fully ready
        asyncio.create_task(monitor_active_users())
        logging.info("🔔 Payment monitoring started")
    
    # Use client's loop to start background task
    client.loop.create_task(start_background_tasks())
    
    client.run_until_disconnected()

