# Wallet Alert Bot

Telegram bot that watches EVM wallets and alerts allowlisted users on **every** on-chain activity (native transfers, ERC-20, NFTs, approvals, contract calls). No amount threshold.

Designed for a single VPS: Node.js worker + SQLite + public RPCs (swap in Alchemy/QuickNode via env).

## Features

- Multi-chain: Ethereum, Base, Arbitrum, Optimism, Polygon, BSC, Avalanche, Robinhood Chain, Ink (configurable in `chains.json` / `ENABLED_CHAINS`)
- Private allowlist — admin approves friends by Telegram id or `@username`
- Each user manages only their own wallets (`/add`, `/remove`, `/list`) — unlimited addresses
- Short Telegram alerts with chain name, summary, explorer link
- Defensive polling with small block batches, per-chain delays, and RPC backoff

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
| `POLL_INTERVAL_MS` | How often to poll (default 15000) |
| `BLOCK_BATCH_SIZE` | Max new blocks per chain per tick (default 8) |
| `CHAIN_DELAY_MS` | Pause between chains (default 800) |
| `ENABLED_CHAINS` | Comma list of chain ids from `chains.json` |
| `RPC_<CHAIN>` | Optional RPC override (prepended; still falls back to `chains.json` `rpcUrls`) |
| `DRY_RUN` | `1` = skip chain polling |

## Public RPC rate limits

Free public endpoints rate-limit under multi-chain polling. Defaults prefer **drpc / chain-official** URLs that allow receipts or topic logs without paid keys; PublicNode remains a fallback. The bot mitigates with:

- Multi-URL `fallback` transport per chain (`chains.json` `rpcUrls`)
- Small `BLOCK_BATCH_SIZE` and `CHAIN_DELAY_MS`
- Exponential backoff on 429/timeouts
- Cursor starts near chain tip (no historical backfill on first boot)
- Token detection via `eth_getBlockReceipts` (avoids PublicNode’s “specify an address” `eth_getLogs` restriction), with `eth_getLogs` only as fallback when receipts are unavailable

Paid Alchemy/QuickNode URLs are optional — set `RPC_*` if you want them as the primary endpoint.

Or reduce `ENABLED_CHAINS` to the networks you care about.

## How detection works

Each tick, per enabled chain:

1. Advance from the stored block cursor in batches.
2. Scan block transactions where `from`/`to` matches a watched wallet (native + contract calls).
3. Load block receipts and filter `Transfer` / `Approval` / `ApprovalForAll` involving watched wallets (tokens + NFTs). Fall back to topic `eth_getLogs` only if receipt scanning is unsupported on that RPC.
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
