# Changelog — Zap Wallet Bot

## v3.2 (2026-11-09)

Production sync: zap_wallet_bot_v32.py replaces zap_wallet_bot_v31.py
(removed from the repo; git history keeps it). test_tokens_async.py removed
(it loaded a runtime .env at import; the offline suite covers the flows).

### Security & funds-safety
- Wallet recovery is verify-first: a replacement mnemonic must load a wallet
  before any old wallet/seed/DB state is touched
- `_admin_alert`: best-effort owner DM on held funds / critical failures
- P2P rating IDOR fixed — ratings only count actual trade counterparties
- HTML-escaping of user-controlled text in public channel posts (P2P cards)
- Guarded stage writes: P2P trade stage transitions validated before persist
- FFIO credentials read from env only (FFIO_API_KEY / FFIO_API_SECRET /
  FFIO_REFCODE); the public copy ships empty defaults
- Tokens: zero-amount guards + cross-chain settlement evidence check
- MoonPay SDK 0.27 API fix (buy-bitcoin link)
- Per-user payment locks — no concurrent sends per user
- Hang protection: timeouts wrap read-only SDK calls (`sdk_read`)
- Payment status recheck edits the user's message in place instead of showing
  a hold wall-of-text

### Refactor
- 27-item audit-driven refactor: dead code removal, god-handler decomposition,
  shared constants
- ~200 user-facing strings migrated to zap_i18n
- Golden tests pin money math, UI construction, handler dispatch

### UI/UX
- Full Persian (fa) localization across the bot
- Unified buttons and wording; readable P2P order cards
- Error messages include next steps; thousand separators on amounts

### Tests
- 616+ offline regression tests (no network, no production DB)

### Config keys (env)
- Required for the ff.io exchange feature: FFIO_API_KEY, FFIO_API_SECRET
- New: P2P_LIST_CHANNEL (P2P order-listing channel), P2P_ESCROW_SEED_PATH
  (default: beside zap_p2p.py)
- Unchanged: TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH,
  BREEZ_API_KEY, BACKUP_CHANNEL_ID, P2P_TEST_USERS, DONATE_USER_ID

### Public copy
- Private values removed from the public sources: owner/channel ids, ff.io
  credentials, personal Lightning address — set via env/.env instead

## v3.1 — 2026-10-31 (production sync)

Sync of the live production source (zap_wallet_bot_v31.py + P2P module).

### P2P exchange (zap_p2p.py)
- Room redesign: role-based conversational texts, new price/rate layout, removed deposit bar
- Timeout texts made role-aware (fa/en); order auto-reposted to listing channel after timeout
- Admin panel: dispute release/refund buttons with admin guard, chat history table (chat_msgs)
- Ban/unban rework: two-step confirm (900s), self-ban guard, unban notification
- Premium via button flow (_prem_buttons), "market" button naming
- Float rate fixed: order's own fiat_code at take time (bot_fiat_rate_for)
- Spark address cache per uid + prewarm at lock, fee charged in background
- Slow SDK calls moved off the event loop (offloop) — fixes UI freezes
- SQLite backup every 30 min via backup API (WAL-safe) sent to backup channel
- Auto-remove "pinned" note on unpin

### Core wallet (zap_wallet_bot_v31.py)
- Removed 8 dead functions (rating_line, cached_rate, edit_or_send, hash_send_password,
  is_notifications_enabled, chain_display, matching_chain, is_rtl)
- Backup channel id moved to env (BACKUP_CHANNEL_ID)

### i18n (zap_i18n.py)
- Deduplicated dictionaries (keep-first per key), EN dictionary repaired
- 3 fa/en text bugs fixed

### Security review (2026-10-31)
- No secrets in source; all credentials via env/.env
- Admin-guarded money flows (admin_release / admin_refund)
- Parameterized SQL everywhere; no injection paths
- Private identifiers scrubbed from this public copy (user ids, channel ids, invites,
  personal LN address) — set them via env/.env

### Config keys (env)
- TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, BREEZ_API_KEY
- P2P_TEST_USERS, BACKUP_CHANNEL_ID, DONATE_USER_ID, GAME_WALLET_USER_ID
