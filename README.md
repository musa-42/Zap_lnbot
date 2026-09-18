# ⚡ Zap Wallet Bot

A self-custodial Bitcoin Lightning wallet bot for Telegram, built on the
[Breez Spark SDK](https://sdk-doc-spark.breez.technology/). Users get a full
Lightning + on-chain wallet inside a private chat — seeds are encrypted and
never leave their own chat.

Originally built as a personal wallet bot, now open-sourced.

## ✨ Features

- **Lightning payments** — pay BOLT11 invoices, LNURL-pay, Lightning
  addresses (`user@domain`) and even Telegram `@usernames`
- **On-chain sends** with fee-speed selection (slow / medium / fast)
- **Receive** — no-amount invoices instantly with Set Amount / Set Memo,
  static on-chain address, personal Lightning Address, QR support
- **Scan QR photos** — send a QR screenshot instead of pasting a string
- **Batch Pay** — queue multiple payments, review fees, send all
- **Built-in exchange** — swap BTC ⇄ fiat via ff.io
- **Buy Bitcoin** — on-ramp via MoonPay / Cash App
- **Contacts** — address book for frequent recipients
- **Transaction history** with receipts: preimage, payment hash, tx id,
  one-tap-copy code blocks, explorer links (sparkscan.io)
- **12 languages** — English, فارسی, Русский, 中文, العربية, Español,
  Türkçe (+ partial De/Fr/Pt/Hi/Id). Auto-detected, per-user, persists
  across restarts, full RTL support
- **Tools** — calculator, recommended fees, manual sync, message signing
- **Security**
  - Seed phrases encrypted with Fernet (AES), key derived from the bot
    token via PBKDF2-HMAC-SHA256 (600k iterations)
  - Optional send-password protection (bcrypt + brute-force lockout)
  - Automated DB backups to a private Telegram channel
  - Emergency exit (unilateral exit) — signs transactions only, never broadcasts

## 🚀 Quick Start

### Requirements

- Python 3.10+
- A Telegram bot token ([@BotFather](https://t.me/BotFather))
- A Breez Spark SDK API key (greenlight — get one from Breez)
- `libzbar0` (for QR photo decoding; `apt install libzbar0`)

### Install

```bash
git clone <this-repo>
cd <repo>
pip install -r requirements.txt
```

### Configure

Create a `.env` file:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_API_ID=12345678          # from my.telegram.org
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef

# Spark SDK
BREEZ_API_KEY=your-spark-api-key

# Optional
DONATE_USER_ID=0                  # Telegram user id that receives donations
FFIO_API_KEY=                     # ff.io exchange keys (optional feature)
FFIO_API_SECRET=
FFIO_REFCODE=
BACKUP_CHANNEL_ID=0               # private channel id for periodic backups
```

### Run

```bash
python3 zap_wallet_bot.py
```

The bot token is read from `.env` (with a hidden `getpass` prompt fallback).
Users start the bot, a wallet is created for them, and the seed phrase is
shown once for backup — encrypted at rest from then on.

### Run with systemd

```ini
[Unit]
Description=Zap Lightning Wallet Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/zap-lnbot
ExecStart=/usr/bin/python3 zap_wallet_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 🗂 Project Structure

```
zap_wallet_bot.py   # main bot (~9k lines): handlers, wallet logic, exchange
zap_i18n.py         # i18n engine + 12 language packs
.env                # secrets (never committed)
```

## 🔐 Security Notes

- Seeds are stored **encrypted** in a local SQLite DB; the encryption key is
  derived from the bot token with PBKDF2 (600k iterations). If the DB leaks,
  the seeds stay protected as long as the token is secret.
- Legacy single-SHA-256 encrypted seeds are **auto-migrated** to the stronger
  KDF on startup.
- Keep `.env`, `bot.session`, and `db-bot.sqlite` out of any repo — they
  never appear in git here (see `.gitignore`).
- Donations default to user id `0` (the bot's own wallet). Set
  `DONATE_USER_ID` explicitly if you enable the Donate button.

## ⚠️ Disclaimer

This software is provided as-is, for educational purposes. Crypto wallets
carry real financial risk — audit the code before running it with real funds.
Not affiliated with Breez or FixedFloat.

## 📄 License

MIT
