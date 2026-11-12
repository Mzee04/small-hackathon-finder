/**
 * Smoke path: validates config + SQLite + (optional) Telegram getMe.
 * Set SKIP_TELEGRAM=1 to avoid hitting Telegram API.
 * Does not require live EVM RPC success.
 */
import { loadConfig } from "./config/index.js";
import { BotDatabase } from "./db/database.js";
import { Bot } from "grammy";

async function main(): Promise<void> {
  const config = loadConfig();
  console.log("OK config loaded");
  console.log(
    `  admin id: ${config.adminTelegramUserId}`,
  );
  console.log(
    `  chains (${config.chains.length}): ${config.chains.map((c) => `${c.id}->${c.rpcUrl.includes("alchemy") || c.rpcUrl.includes("quiknode") || c.rpcUrl.includes("infura") ? "custom" : "public"}`).join(", ")}`,
  );
  console.log(`  db path: ${config.databasePath}`);
  console.log(`  poll interval: ${config.pollIntervalMs}ms`);
  console.log(`  dry run: ${config.dryRun}`);

  const tokenLooksValid = /^\d+:[A-Za-z0-9_-]+$/.test(config.telegramBotToken);
  if (!tokenLooksValid) {
    throw new Error("TELEGRAM_BOT_TOKEN format looks invalid");
  }
  console.log("OK telegram token format");

  const db = new BotDatabase(config.databasePath);
  db.ensureAdmin(config.adminTelegramUserId);
  const admin = db.getUser(config.adminTelegramUserId);
  if (!admin?.approved || !admin.is_admin) {
    throw new Error("admin row not seeded correctly");
  }

  const sample = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045";
  db.addWallet(config.adminTelegramUserId, sample);
  const listed = db.listWallets(config.adminTelegramUserId);
  if (!listed.some((w) => w.address.toLowerCase() === sample.toLowerCase())) {
    throw new Error("wallet add/list failed");
  }
  db.removeWallet(config.adminTelegramUserId, sample);
  console.log("OK sqlite allowlist + wallets");

  if (process.env.SKIP_TELEGRAM === "1") {
    console.log("OK smoke (telegram skipped)");
    db.close();
    return;
  }

  const bot = new Bot(config.telegramBotToken);
  const me = await bot.api.getMe();
  console.log(`OK telegram getMe @${me.username} id=${me.id}`);
  db.close();
  console.log("SMOKE PASSED");
}

main().catch((err) => {
  console.error("SMOKE FAILED:", err instanceof Error ? err.message : err);
  process.exit(1);
});
