# ⚡ Telegram Bitcoin Lightning Wallet Bot

Bitcoin Lightning wallet bot for Telegram, enabling instant peer-to-peer payments, zaps, and donations with zero-fee internal transfers using Breez Spark SDK.

[@Zap_lnbot](https://t.me/Zap_lnbot)

## ✨ Features

- 🔐 Self-Custodial: Users have full control of their funds with mnemonic seed phrases
- ⚡ Lightning Fast: Instant payments using Bitcoin Lightning Network via Spark Breez SDK
- 💸 Zero-Fee Internal Transfers: Send sats between bot users with 0 fees using Spark
- 🌍 Lightning Address: Each user gets their own Lightning address (user@breez.tips)
- 💬 Group Zaps: Tip users in Telegram groups with /zap command
- 🔗 Multi-Payment Support:
  - Lightning invoices (BOLT11)
  - Lightning addresses
  - LNURL-Pay
  - Bitcoin on-chain addresses
- 💰 Withdraw All: Empty your entire wallet with one command
- 📝 Invoice Memos: Add custom descriptions to invoices
- 🔄 Wallet Recovery: Restore wallet using 12-word mnemonic
- ⚙️ Customizable Lightning Address: Change your Lightning username anytime

## 🚀 Quick Start

### Prerequisites

- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram API credentials (from [my.telegram.org](https://my.telegram.org))
- breez API Key (from [Breez SDK](https://breez.technology/request-api-key/#contact-us-form-sdk))

### Installation

1. Clone the repository:
git clone https://github.com/musa-42/Zap_lnbot.git
cd Zap_lnbot

1. Install dependencies:
pip install -r requirements.txt

1. Set up environment variables:
cp .env.example .env

Edit .env and add your credentials:
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token
BREEZ_API_KEY=your_breez_api_key
DONATE_USER_ID=your_telegram_user_id  # Optional: for donations

1. Run the bot:
python main.py

## 📖 Usage

### Private Commands

- /start - Initialize your wallet and view balance
- /zap_balance - Quick balance check

### Group Commands

- /zap <amount> - Zap someone by replying to their message
- /zap <amount> @username - Zap someone by mentioning their username

Examples:
/zap 1000
/zap 5000 @alice

### Bot Menu

When you send /start, you’ll see the main menu with options:

- 📤 Send - Send Lightning or on-chain payments
- 📥 Receive - Create invoices and view your Lightning address
- 🔄 Refresh - Update your balance
- ⚙️ Settings - Manage your wallet
  - 🔐 Backup Seeds - View your 12-word recovery phrase
  - 🔄 Recovery Wallet - Restore from mnemonic
  - ⚡ Change Lightning Address - Customize your address
- 💝 Donate - Support the bot developer

## 🔒 Security

- Only mnemonic seeds are stored
- Local SQLite database - No external servers
- Self-custodial - Users maintain full control of their funds

⚠️ Important: Always backup your recovery phrase and store it securely!

## 🛠️ Tech Stack

- [Telethon](https://github.com/LonamiWebs/Telethon) - Telegram MTProto API client
- [Breez SDK Spark](https://github.com/breez/breez-sdk) - Lightning Network payments
- [KeyValue SQLite](https://github.com/ccbrown/keyvalue-sqlite) - Simple key-value database
- [Python Mnemonic](https://github.com/trezor/python-mnemonic) - BIP39 mnemonic generation

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
1. Create your feature branch (`git checkout -b feature/AmazingFeature`)
1. Commit

## ⚠️ Disclaimer

This bot is provided as-is for educational and experimental purposes. Use at your own risk. Always test with small amounts first.

- Not financial advice
- No warranty provided
- Users are responsible for securing their recovery phrases
- Loss of recovery phrase means loss of funds

## 🙏 Acknowledgments

- [Breez](https://breez.technology/) for the amazing Spark SDK
- [spark](https://spark.money/) for Lightning infrastructure
- Telegram for the Bot API

## Support
- Donate: musa@breez.tips
-----
Made with ⚡ and Bitcoin
