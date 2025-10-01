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

load_dotenv()

# Uncomment for debugging
# logging.basicConfig(level=logging.DEBUG)

mnemo = Mnemonic("english")

# Database configuration
DB_PATH = './db-bot.sqlite'
db = KeyValueSqlite(DB_PATH, 'table-name')

# Telegram API credentials (set these in environment variables)
api_id = os.getenv('TELEGRAM_API_ID')
api_hash = os.getenv('TELEGRAM_API_HASH')
bot_token = os.getenv('TELEGRAM_BOT_TOKEN')

# Spark SDK configuration
config = default_config(network=Network.MAINNET)
config.api_key = os.getenv('BREEZ_API_KEY')
config.prefer_spark_over_lightning = True

# Initialize users list in database
db.set_default(f'users', [])

# Initialize Telegram client
client = TelegramClient('bot', api_id, api_hash).start(bot_token=bot_token)

# User state tracking
user_steps = {}
user_data = {}


async def get_balance(user_id):
    """Get wallet balance for a user"""
    try:
        sdk = await get_wallet(user_id)
        sdk.sync_wallet(request=SyncWalletRequest())
        info = await sdk.get_info(request=GetInfoRequest())
        balance_sats = info.balance_sats
        return str(balance_sats)
    except Exception as error:
        logging.error(f"Error getting balance: {error}")
        return "0"


async def init(user_id):
    """Initialize new user wallet with mnemonic"""
    users = db.get(f'users')
    if user_id not in users:
        words = mnemo.generate(strength=128)
        logging.info(f"New wallet created for user {user_id}")
        print(f"Mnemonic: {words}")
        db.set_default(f'{user_id}', words)
        users.append(user_id)
        db.set(f'users', users)


async def get_wallet(user_id):
    """Get or create wallet SDK instance"""
    mnemonic = db.get(f'{user_id}')
    seed = Seed.MNEMONIC(mnemonic=mnemonic, passphrase=None)
    sdk = await connect(
        request=ConnectRequest(config=config, seed=seed, storage_dir=f"./.{user_id}")
    )
    return sdk


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


async def prepare_payment(user_id, invoice, amount_sats=None):
    """Prepare payment and get fee information"""
    try:
        sdk = await get_wallet(user_id)
        
        request = PrepareSendPaymentRequest(
            payment_request=invoice,
            amount_sats=amount_sats
        )

        prepare_response = await sdk.prepare_send_payment(request=request)
        print(prepare_response)
        
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
        [Button.inline("🔄 Refresh", b"refresh"), Button.inline("⚙️ Settings", b"settings")],
        [Button.inline("💝 Donate", b"donate")],
    ]


async def show_main_menu(event, edit=False):
    """Show main wallet menu"""
    user_id = event.sender_id
    await init(user_id)
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


async def prepare_and_show_fee(event, user_id, invoice, amount):
    """Prepare payment and show fee confirmation"""
    try:
        res = await event.respond("⚡ Calculating fees...")
        
        prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, amount)
        
        user_data[user_id]['prepare_response'] = prepare_response
        user_data[user_id]['options'] = SendPaymentOptions.BOLT11_INVOICE(prefer_spark=True)
        
        # Determine final fee - use spark_fee if available
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

@client.on(events.NewMessage(func=lambda e: e.is_private, pattern='/start'))
async def start_command(event):
    """Handle /start command"""
    await show_main_menu(event)


@client.on(events.CallbackQuery)
async def callback_handler(event):
    """Handle inline button callbacks"""
    user_id = event.sender_id
    data = event.data.decode()
    
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
        
        elif data == "settings":
            await event.answer()
            await event.edit(
                "⚙️ **Settings**\n\n"
                "Choose an option:",
                buttons=[
                    [Button.inline("🔐 Backup Seeds", b"backup")],
                    [Button.inline("🔄 Recovery Wallet", b"recovery")],
                    [Button.inline("⚡ Change Lightning Address", b"change_ln_address")],
                    [Button.inline("« Back to Menu", b"back_to_menu")]
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
        
        elif data == "receive":
            await event.answer()
            address = await get_lightning_address(user_id)
            await event.edit(
                f"📥 **Receive Payment**\n\n"
                f"⚡ **Your Lightning Address:**\n`{address.lightning_address}`\n\n"
                f"🔗 **LNURL:**\n`{address.lnurl}`\n\n"
                f"💬 Would you like to add a memo/description to your invoice?",
                buttons=[
                    [Button.inline("💬 Add Memo", b"receive_add_memo")],
                    [Button.inline("⏭ Skip", b"receive_skip_memo")],
                    [Button.inline("« Back", b"back_to_menu")]
                ]
            )
        
        elif data == "receive_add_memo":
            user_steps[user_id] = "receive_memo"
            user_data[user_id] = {}
            await event.edit(
                "💬 **Add Memo**\n\n"
                "Enter a description/memo for your invoice:",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
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
                        [Button.inline("« Back", b"back_to_menu")]
                    ]
                )
            except Exception as error:
                await event.edit(f"❌ Error: {error}")
        
        elif data == "receive_set_amount":
            user_steps[user_id] = "receive_amount"
            await event.edit(
                "💵 **Set Invoice Amount**\n\n"
                "Enter the amount in sats:",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
            )
        
        elif data == "receive_set_amount_no_memo":
            user_steps[user_id] = "receive_amount_no_memo"
            await event.edit(
                "💵 **Set Invoice Amount**\n\n"
                "Enter the amount in sats:",
                buttons=[[Button.inline("« Cancel", b"back_to_menu")]]
            )
        
        elif data == "send":
            user_steps[user_id] = "send_invoice"
            await event.answer()
            await event.edit(
                "📤 **Send Payment**\n\n"
                "Send me one of:\n"
                "• Lightning Invoice\n"
                "• Lightning Address (user@domain.com)\n"
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
            
            if user_id in user_data and 'invoice' in user_data[user_id]:
                invoice = user_data[user_id]['invoice']
                payment_type = user_data[user_id].get('payment_type', 'bolt11')
                
                try:
                    res = await event.edit("⚡ Calculating fees for withdrawal...")
                    
                    if payment_type == 'onchain':
                        prepare_response, _, _ = await prepare_payment(user_id, invoice, balance)
                        
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
                        prepare_response, fee_sats, spark_fee = await prepare_payment(user_id, invoice, balance)
                        
                        final_fee = spark_fee if spark_fee is not None else (fee_sats if fee_sats else 0)
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
                        final_fee = spark_fee if spark_fee is not None else (fee_sats if fee_sats else 0)
                        
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
        
        # Handle other message types (recovery, memo, amount, invoice, etc.)
        # [Rest of message handler code continues...]
        
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
    user_id = event.sender_id
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
            f"👤 To: {receiver_name}"
        )
        
        try:
            await client.send_message(
                receiver_id,
                f"🎉 **You received a zap!**\n\n"
                f"💰 Amount: {amount} sats\n"
                f"👤 From: {sender_name}\n"
                f"📍 In group: {event.chat.title if hasattr(event.chat, 'title') else 'Unknown'}\n\n"
                f"Use /start to manage your wallet!"
            )
        except:
            pass
        
    except Exception as error:
        logging.error(f"Zap error: {error}")
        await event.reply(f"❌ Zap failed: {error}")


if __name__ == "__main__":
    print("🚀 Starting bot...")
    print("📡 Connecting to Telegram...")
    client.run_until_disconnected()