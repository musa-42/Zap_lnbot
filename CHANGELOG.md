# Changelog — Zap Wallet Bot

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
