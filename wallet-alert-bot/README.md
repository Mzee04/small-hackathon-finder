# Wallet Alert Bot

Telegram bot that watches EVM wallets and alerts allowlisted users on **every** on-chain activity (native transfers, ERC-20, NFTs, approvals, contract calls). No amount threshold.

Designed for a single VPS: Node.js worker + SQLite + public RPCs (swap in Alchemy/QuickNode via env).

## Features

- Multi-chain: Ethereum, Base, Arbitrum, Optimism, Polygon, BSC, Avalanche, Robinhood Chain, Ink (configurable in `chains.json` / `ENABLED_CHAINS`)
- Private allowlist — admin approves friends by Telegram id or `@username`
- Each user manages only their own wallets (`/add`, `/remove`, `/list`) — unlimited addresses
- Short Telegram alerts with chain name, summary, explorer link
- Parallel per-chain polling with receipt-based catch-up (never silently snaps over lag)

## Commands

| Command | Who | Description |
|---------|-----|-------------|
| `/start` | anyone | Register; shows your Telegram id if not approved |
| `/help` | anyone | Help text |
| `/add <address>` | approved | Watch a wallet |
| `/remove <address>` | approved | Stop watching |
| `/list` | approved | List your wallets |
| `/approve <id\|@user>` | admin | Allowlist a user |
| `/revoke <id\|@user>` | admin | Remove access + their wallets |
| `/users` | admin | Show allowlist + pending usernames |

Admin Telegram user id is set via `ADMIN_TELEGRAM_USER_ID` (default owner id in `.env.example`).

## Quick start (local)

```bash
cd wallet-alert-bot
cp .env.example .env
# edit .env — set TELEGRAM_BOT_TOKEN and ADMIN_TELEGRAM_USER_ID
npm install
npm run smoke          # validates config, SQLite, Telegram getMe
npm run dev            # long-running bot + poller
```

Smoke without hitting Telegram:

```bash
SKIP_TELEGRAM=1 npm run smoke
```

Dry-run (Telegram on, chain polling off):

```bash
DRY_RUN=1 npm run dev
```

## Docker / VPS deploy

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. On the VPS:

```bash
git clone <this-repo> && cd small-hackathon-finder/wallet-alert-bot
cp .env.example .env
nano .env   # TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_USER_ID
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

3. Message the bot on Telegram with `/start` from the admin account.
4. `/approve <friend_id_or_@username>` then friends `/add 0x...`.

Data persists in `./data/bot.sqlite` (compose bind mount).

Update:

```bash
git pull
docker compose up -d --build
```

## Environment

See `.env.example`. Important knobs:

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `ADMIN_TELEGRAM_USER_ID` | Admin numeric id |
| `DATABASE_PATH` | SQLite file path |
| `POLL_INTERVAL_MS` | How often to poll (default 10000) |
| `BLOCK_BATCH_SIZE` | Tip-following window when near head (default 8) |
| `CATCHUP_BATCH_SIZE` | Max blocks per chain per tick while lagging (default 80) |
| `RECEIPT_CONCURRENCY` | Parallel `eth_getBlockReceipts` workers (default 10) |
| `CHAIN_DELAY_MS` | Optional pause between chains; unused when chains run in parallel (default 0) |
| `ENABLED_CHAINS` | Comma list of chain ids from `chains.json` |
| `RPC_<CHAIN>` | Optional paid RPC override (`RPC_ETHEREUM`, `RPC_BASE`, …) |
| `DRY_RUN` | `1` = skip chain polling |

## Public RPC rate limits

Public endpoints will **rate-limit** under multi-chain polling. The bot mitigates with:

- Parallel per-chain ticks + larger catch-up batches (no silent cursor snaps)
- `eth_getBlockReceipts` first (native/contract `from`/`to` + log fallback)
- Topic `eth_getLogs` when the RPC allows it; otherwise receipts-only
- Exponential backoff on 429/timeouts
- Cursor starts near chain tip (no historical backfill on first boot)

If alerts lag or logs show repeated rate errors, set Alchemy/QuickNode URLs:

```env
RPC_ETHEREUM=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
RPC_BASE=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
# …same pattern for other chains (see .env.example)
```

Or reduce `ENABLED_CHAINS` to the networks you care about.

## How detection works

Each tick, all enabled chains poll in parallel:

1. Advance from the stored block cursor (`BLOCK_BATCH_SIZE` near tip, up to `CATCHUP_BATCH_SIZE` when behind — **never snaps** over unread blocks).
2. Scan `eth_getBlockReceipts` (parallel) for outer `from`/`to` matches; fetch full txs only for hits (native + contract calls).
3. Pull `Transfer` / `Approval` / `ApprovalForAll` logs involving watched wallets via `eth_getLogs` when allowed, else from receipts.
4. Deduplicate alerts in SQLite and DM each owning Telegram user.

## Layout

```
wallet-alert-bot/
  chains.json          # chain list + default public RPCs + explorers
  src/config/          # env + chains loader
  src/db/              # SQLite allowlist + wallets + cursors
  src/telegram/        # grammY commands
  src/watcher/         # multi-chain poller
  src/smoke.ts         # config / DB / getMe check
  Dockerfile
  docker-compose.yml
```

## License

Same as the parent repository.
