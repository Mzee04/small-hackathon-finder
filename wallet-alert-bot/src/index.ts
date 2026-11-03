import { loadConfig } from "./config/index.js";
import { BotDatabase } from "./db/database.js";
import { createBot, formatAlert } from "./telegram/bot.js";
import { ChainPoller } from "./watcher/poller.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const db = new BotDatabase(config.databasePath);
  db.ensureAdmin(config.adminTelegramUserId);

  console.log(
    `[boot] admin=${config.adminTelegramUserId} chains=${config.chains.map((c) => c.id).join(",")} db=${config.databasePath}${config.dryRun ? " dryRun=1" : ""}`,
  );

  const bot = createBot(config, db);

  const poller = new ChainPoller(config, db, async (activity, telegramIds) => {
    const text = formatAlert(activity);
    for (const chatId of telegramIds) {
      try {
        await bot.api.sendMessage(chatId, text, {
          link_preview_options: { is_disabled: true },
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        console.warn(`[alert] failed to notify ${chatId}: ${message}`);
      }
    }
  });

  const shutdown = async (signal: string) => {
    console.log(`[boot] ${signal} — shutting down`);
    poller.stop();
    bot.stop();
    db.close();
    process.exit(0);
  };
  process.once("SIGINT", () => void shutdown("SIGINT"));
  process.once("SIGTERM", () => void shutdown("SIGTERM"));

  poller.start();
  await bot.start({
    onStart: (info) => {
      console.log(`[boot] Telegram bot @${info.username} online`);
    },
  });
}

main().catch((err) => {
  console.error("[boot] fatal:", err instanceof Error ? err.message : err);
  process.exit(1);
});
