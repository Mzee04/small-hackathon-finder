import { Bot, GrammyError, HttpError } from "grammy";
import type { AppConfig } from "../config/index.js";
import type { BotDatabase } from "../db/database.js";
import type { DetectedActivity } from "../watcher/types.js";

function helpText(isAdmin: boolean): string {
  const base = [
    "Wallet Alert Bot — track EVM wallet activity across major chains.",
    "",
    "Commands:",
    "/start — register / check access",
    "/help — show this help",
    "/add <address> — watch a wallet (your list only)",
    "/remove <address> — stop watching",
    "/list — your watched wallets",
  ];
  if (isAdmin) {
    base.push(
      "",
      "Admin:",
      "/approve <telegram_id|@username> — allowlist a user",
      "/revoke <telegram_id|@username> — remove access + their wallets",
      "/users — show allowlist + pending usernames",
    );
  }
  return base.join("\n");
}

function parseTarget(raw: string): { type: "id"; id: number } | { type: "username"; username: string } {
  const value = raw.trim();
  if (/^\d+$/.test(value)) {
    return { type: "id", id: Number(value) };
  }
  const username = value.replace(/^@/, "");
  if (!/^[A-Za-z0-9_]{5,32}$/.test(username)) {
    throw new Error("Expected a numeric Telegram user id or @username");
  }
  return { type: "username", username };
}

export function createBot(config: AppConfig, db: BotDatabase): Bot {
  const bot = new Bot(config.telegramBotToken);

  const requireApproved = async (
    ctx: { from?: { id: number; username?: string }; reply: (t: string) => Promise<unknown> },
  ): Promise<boolean> => {
    if (!ctx.from) return false;
    db.upsertUserContact(ctx.from.id, ctx.from.username ?? null);
    if (ctx.from.id === config.adminTelegramUserId) {
      db.ensureAdmin(config.adminTelegramUserId);
    }
    if (!db.isApproved(ctx.from.id)) {
      await ctx.reply(
        "You are not on the allowlist yet. Ask the admin to /approve your Telegram user id or @username.",
      );
      return false;
    }
    return true;
  };

  bot.command("start", async (ctx) => {
    if (!ctx.from) return;
    const user = db.upsertUserContact(ctx.from.id, ctx.from.username ?? null);
    if (ctx.from.id === config.adminTelegramUserId) {
      db.ensureAdmin(config.adminTelegramUserId);
      await ctx.reply(
        `Welcome, admin.\nYour Telegram id: ${ctx.from.id}\n\n${helpText(true)}`,
      );
      return;
    }
    if (user.approved) {
      await ctx.reply(`You are approved. Add wallets with /add <address>.\n\n${helpText(false)}`);
      return;
    }
    await ctx.reply(
      `Hi. Your Telegram id is ${ctx.from.id}.\nYou are not approved yet — send this id (or your @username) to the admin.`,
    );
  });

  bot.command("help", async (ctx) => {
    if (!ctx.from) return;
    db.upsertUserContact(ctx.from.id, ctx.from.username ?? null);
    const admin =
      ctx.from.id === config.adminTelegramUserId || db.isAdmin(ctx.from.id);
    await ctx.reply(helpText(admin));
  });

  bot.command("add", async (ctx) => {
    if (!(await requireApproved(ctx))) return;
    const parts = ctx.message?.text?.trim().split(/\s+/) ?? [];
    const address = parts[1];
    if (!address) {
      await ctx.reply("Usage: /add <0xAddress>");
      return;
    }
    try {
      const wallet = db.addWallet(ctx.from!.id, address);
      await ctx.reply(`Watching ${wallet.address}`);
    } catch (err) {
      await ctx.reply(err instanceof Error ? err.message : "Failed to add wallet");
    }
  });

  bot.command("remove", async (ctx) => {
    if (!(await requireApproved(ctx))) return;
    const parts = ctx.message?.text?.trim().split(/\s+/) ?? [];
    const address = parts[1];
    if (!address) {
      await ctx.reply("Usage: /remove <0xAddress>");
      return;
    }
    try {
      const ok = db.removeWallet(ctx.from!.id, address);
      await ctx.reply(ok ? `Removed ${address}` : "That address was not on your list.");
    } catch (err) {
      await ctx.reply(err instanceof Error ? err.message : "Failed to remove wallet");
    }
  });

  bot.command("list", async (ctx) => {
    if (!(await requireApproved(ctx))) return;
    const wallets = db.listWallets(ctx.from!.id);
    if (wallets.length === 0) {
      await ctx.reply("No wallets yet. Use /add <0xAddress>.");
      return;
    }
    const lines = wallets.map((w, i) => `${i + 1}. ${w.address}`);
    await ctx.reply(`Your wallets (${wallets.length}):\n${lines.join("\n")}`);
  });

  bot.command("approve", async (ctx) => {
    if (!ctx.from) return;
    db.ensureAdmin(config.adminTelegramUserId);
    if (ctx.from.id !== config.adminTelegramUserId && !db.isAdmin(ctx.from.id)) {
      await ctx.reply("Admin only.");
      return;
    }
    const parts = ctx.message?.text?.trim().split(/\s+/) ?? [];
    const targetRaw = parts[1];
    if (!targetRaw) {
      await ctx.reply("Usage: /approve <telegram_id|@username>");
      return;
    }
    try {
      const target = parseTarget(targetRaw);
      if (target.type === "id") {
        const user = db.approveById(target.id);
        await ctx.reply(`Approved user ${user.telegram_id}${user.username ? ` (@${user.username})` : ""}.`);
      } else {
        const result = db.approveByUsername(target.username);
        if (result.status === "approved") {
          await ctx.reply(`Approved @${target.username} (id ${result.telegramId}).`);
        } else {
          await ctx.reply(
            `Queued @${target.username}. They will be approved automatically on their next /start.`,
          );
        }
      }
    } catch (err) {
      await ctx.reply(err instanceof Error ? err.message : "Approve failed");
    }
  });

  bot.command("revoke", async (ctx) => {
    if (!ctx.from) return;
    if (ctx.from.id !== config.adminTelegramUserId && !db.isAdmin(ctx.from.id)) {
      await ctx.reply("Admin only.");
      return;
    }
    const parts = ctx.message?.text?.trim().split(/\s+/) ?? [];
    const targetRaw = parts[1];
    if (!targetRaw) {
      await ctx.reply("Usage: /revoke <telegram_id|@username>");
      return;
    }
    try {
      const target = parseTarget(targetRaw);
      if (target.type === "id") {
        if (target.id === config.adminTelegramUserId) {
          await ctx.reply("Cannot revoke the admin.");
          return;
        }
        const ok = db.revokeById(target.id);
        await ctx.reply(ok ? `Revoked ${target.id}.` : "User not found.");
      } else {
        const result = db.revokeByUsername(target.username);
        if (result.revoked) {
          await ctx.reply(`Revoked @${target.username}.`);
        } else if (result.pendingCleared) {
          await ctx.reply(`Cleared pending approval for @${target.username}.`);
        } else {
          await ctx.reply("User not found.");
        }
      }
    } catch (err) {
      await ctx.reply(err instanceof Error ? err.message : "Revoke failed");
    }
  });

  bot.command("users", async (ctx) => {
    if (!ctx.from) return;
    if (ctx.from.id !== config.adminTelegramUserId && !db.isAdmin(ctx.from.id)) {
      await ctx.reply("Admin only.");
      return;
    }
    const { approved, pending } = db.listUsers();
    const lines: string[] = ["Allowlist:"];
    for (const u of approved) {
      const tag = u.username ? `@${u.username}` : "(no username)";
      const role = u.is_admin ? " admin" : "";
      lines.push(`- ${u.telegram_id} ${tag}${role}`);
    }
    if (approved.length === 0) lines.push("- (empty)");
    lines.push("", "Pending usernames:");
    if (pending.length === 0) lines.push("- (none)");
    else for (const p of pending) lines.push(`- @${p.username}`);
    await ctx.reply(lines.join("\n"));
  });

  bot.catch((err) => {
    const ctx = err.ctx;
    console.error(`Telegram error on update ${ctx.update.update_id}`);
    const e = err.error;
    if (e instanceof GrammyError) {
      console.error("Grammy error:", e.description);
    } else if (e instanceof HttpError) {
      console.error("Telegram HTTP error:", e);
    } else {
      console.error("Unknown error:", e);
    }
  });

  return bot;
}

export function formatAlert(activity: DetectedActivity): string {
  const kindLabel: Record<string, string> = {
    native_transfer: "Native transfer",
    erc20_transfer: "Token transfer",
    nft_transfer: "NFT transfer",
    approval: "Approval",
    contract_call: "Contract call",
    unknown: "Activity",
  };
  return [
    `🔔 ${activity.chainName} · ${kindLabel[activity.kind] ?? "Activity"}`,
    activity.summary,
    activity.explorerUrl,
  ].join("\n");
}
