import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { z } from "zod";

const __dirname = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(__dirname, "../..");

loadDotenv({ path: resolve(process.cwd(), ".env") });
loadDotenv({ path: resolve(packageRoot, ".env") });

const chainSchema = z.object({
  id: z.string(),
  name: z.string(),
  chainId: z.number().int().positive(),
  rpcEnv: z.string(),
  defaultRpc: z.string().url(),
  rpcUrls: z.array(z.string().url()).optional(),
  explorerTx: z.string(),
  nativeSymbol: z.string(),
});

export type ChainConfig = z.infer<typeof chainSchema> & {
  rpcUrl: string;
  rpcUrlList: string[];
};

const envSchema = z.object({
  TELEGRAM_BOT_TOKEN: z.string().min(10),
  ADMIN_TELEGRAM_USER_ID: z.coerce.number().int().positive(),
  DATABASE_PATH: z.string().default("./data/bot.sqlite"),
  POLL_INTERVAL_MS: z.coerce.number().int().positive().default(10_000),
  BLOCK_BATCH_SIZE: z.coerce.number().int().positive().default(8),
  CATCHUP_BATCH_SIZE: z.coerce.number().int().positive().default(80),
  RECEIPT_CONCURRENCY: z.coerce.number().int().positive().default(10),
  CHAIN_DELAY_MS: z.coerce.number().int().nonnegative().default(0),
  REQUEST_TIMEOUT_MS: z.coerce.number().int().positive().default(12_000),
  ENABLED_CHAINS: z.string().default(
    "ethereum,base,arbitrum,optimism,polygon,bsc,avalanche,robinhood,ink",
  ),
  DRY_RUN: z
    .string()
    .optional()
    .transform((v) => v === "1" || v?.toLowerCase() === "true"),
});

function uniqueUrls(urls: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of urls) {
    const url = raw.trim();
    if (!url) continue;
    const key = url.replace(/\/+$/, "").toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(url);
  }
  return out;
}

function loadChainsFile(): z.infer<typeof chainSchema>[] {
  const candidates = [
    resolve(process.cwd(), "chains.json"),
    resolve(packageRoot, "chains.json"),
  ];
  const path = candidates.find((p) => existsSync(p));
  if (!path) {
    throw new Error("chains.json not found (expected next to package root)");
  }
  const raw = JSON.parse(readFileSync(path, "utf8"));
  return z.array(chainSchema).parse(raw);
}

export type AppConfig = {
  telegramBotToken: string;
  adminTelegramUserId: number;
  databasePath: string;
  pollIntervalMs: number;
  blockBatchSize: number;
  catchupBatchSize: number;
  receiptConcurrency: number;
  chainDelayMs: number;
  requestTimeoutMs: number;
  dryRun: boolean;
  chains: ChainConfig[];
  packageRoot: string;
};

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const parsed = envSchema.parse(env);
  const enabled = new Set(
    parsed.ENABLED_CHAINS.split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean),
  );
  const chains = loadChainsFile()
    .filter((c) => enabled.has(c.id.toLowerCase()))
    .map((c) => {
      const override = env[c.rpcEnv]?.trim();
      const fromFile = c.rpcUrls?.length
        ? c.rpcUrls
        : [c.defaultRpc];
      const rpcUrlList = uniqueUrls(
        override && override.length > 0
          ? [override, ...fromFile, c.defaultRpc]
          : [...fromFile, c.defaultRpc],
      );
      return {
        ...c,
        rpcUrl: rpcUrlList[0]!,
        rpcUrlList,
      };
    });

  if (chains.length === 0) {
    throw new Error("No chains enabled — check ENABLED_CHAINS and chains.json");
  }

  return {
    telegramBotToken: parsed.TELEGRAM_BOT_TOKEN,
    adminTelegramUserId: parsed.ADMIN_TELEGRAM_USER_ID,
    databasePath: resolve(parsed.DATABASE_PATH),
    pollIntervalMs: parsed.POLL_INTERVAL_MS,
    blockBatchSize: parsed.BLOCK_BATCH_SIZE,
    catchupBatchSize: Math.max(
      parsed.CATCHUP_BATCH_SIZE,
      parsed.BLOCK_BATCH_SIZE,
    ),
    receiptConcurrency: parsed.RECEIPT_CONCURRENCY,
    chainDelayMs: parsed.CHAIN_DELAY_MS,
    requestTimeoutMs: parsed.REQUEST_TIMEOUT_MS,
    dryRun: Boolean(parsed.DRY_RUN),
    chains,
    packageRoot,
  };
}

export function chainsConfigPath(): string {
  return join(packageRoot, "chains.json");
}
