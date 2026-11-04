import {
  createPublicClient,
  formatEther,
  formatUnits,
  http,
  parseAbiItem,
  type Hex,
  type Log,
  type PublicClient,
  type Transaction,
} from "viem";
import type { AppConfig, ChainConfig } from "../config/index.js";
import type { BotDatabase, WatchedAddress } from "../db/database.js";
import type { ActivityKind, DetectedActivity } from "./types.js";

const transferEvent = parseAbiItem(
  "event Transfer(address indexed from, address indexed to, uint256 value)",
);
const approvalEvent = parseAbiItem(
  "event Approval(address indexed owner, address indexed spender, uint256 value)",
);
const approvalForAllEvent = parseAbiItem(
  "event ApprovalForAll(address indexed owner, address indexed operator, bool approved)",
);

const ERC20_ABI = [
  {
    type: "function",
    name: "symbol",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "string" }],
  },
  {
    type: "function",
    name: "decimals",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "uint8" }],
  },
] as const;

function short(addr: string): string {
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function explorerUrl(chain: ChainConfig, hash: string): string {
  return chain.explorerTx.replace("{hash}", hash);
}

async function withBackoff<T>(
  label: string,
  fn: () => Promise<T>,
  attempts = 4,
): Promise<T> {
  let lastError: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      const message = err instanceof Error ? err.message : String(err);
      const retryable =
        /429|rate|timeout|ECONNRESET|503|502|fetch failed/i.test(message);
      if (!retryable || i === attempts - 1) break;
      const delay = 1000 * 2 ** i + Math.floor(Math.random() * 250);
      console.warn(`[rpc] ${label} failed (${message}); retry in ${delay}ms`);
      await sleep(delay);
    }
  }
  throw lastError;
}

export type ActivityHandler = (activity: DetectedActivity, telegramIds: number[]) => Promise<void>;

export class ChainPoller {
  private clients = new Map<string, PublicClient>();
  private stopped = false;
  private timer: NodeJS.Timeout | null = null;
  private running = false;

  constructor(
    private readonly config: AppConfig,
    private readonly db: BotDatabase,
    private readonly onActivity: ActivityHandler,
  ) {
    for (const chain of config.chains) {
      const client = createPublicClient({
        transport: http(chain.rpcUrl, {
          timeout: config.requestTimeoutMs,
          retryCount: 0,
        }),
      });
      this.clients.set(chain.id, client as PublicClient);
    }
  }

  start(): void {
    if (this.config.dryRun) {
      console.log("[poller] DRY_RUN=1 — chain polling disabled");
      return;
    }
    console.log(
      `[poller] watching ${this.config.chains.map((c) => c.id).join(", ")} every ${this.config.pollIntervalMs}ms`,
    );
    void this.tick();
    this.timer = setInterval(() => void this.tick(), this.config.pollIntervalMs);
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
  }

  private async tick(): Promise<void> {
    if (this.stopped || this.running) return;
    this.running = true;
    try {
      const watched = this.db.getWatchedAddresses();
      if (watched.length === 0) return;

      for (const chain of this.config.chains) {
        if (this.stopped) return;
        try {
          await this.pollChain(chain, watched);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          console.warn(`[poller] ${chain.id}: ${message}`);
        }
        if (this.config.chainDelayMs > 0) {
          await sleep(this.config.chainDelayMs);
        }
      }
      this.db.pruneAlerted(14);
    } finally {
      this.running = false;
    }
  }

  private async pollChain(
    chain: ChainConfig,
    watched: WatchedAddress[],
  ): Promise<void> {
    const client = this.clients.get(chain.id);
    if (!client) return;

    const latest = Number(
      await withBackoff(`${chain.id}:blockNumber`, () =>
        client.getBlockNumber(),
      ),
    );

    let fromBlock = this.db.getCursor(chain.id);
    if (fromBlock <= 0) {
      // First run: start near tip to avoid historical flood
      fromBlock = Math.max(0, latest - 2);
      this.db.setCursor(chain.id, fromBlock);
      console.log(`[poller] ${chain.id}: initialized cursor at ${fromBlock}`);
      return;
    }

    if (fromBlock >= latest) return;

    const toBlock = Math.min(latest, fromBlock + this.config.blockBatchSize);
    const addressSet = new Map(
      watched.map((w) => [w.address.toLowerCase(), w] as const),
    );
    const addressList = [...addressSet.keys()] as Hex[];

    const activities: DetectedActivity[] = [];

    // Native + contract calls: scan transactions in the block range
    for (let blockNum = fromBlock + 1; blockNum <= toBlock; blockNum++) {
      const block = await withBackoff(`${chain.id}:getBlock:${blockNum}`, () =>
        client.getBlock({
          blockNumber: BigInt(blockNum),
          includeTransactions: true,
        }),
      );

      for (const tx of block.transactions) {
        if (typeof tx === "string") continue;
        const candidates: string[] = [];
        if (tx.from) candidates.push(tx.from.toLowerCase());
        if (tx.to) candidates.push(tx.to.toLowerCase());
        const involved = candidates.filter((a) => addressSet.has(a));
        if (involved.length === 0) continue;

        for (const wallet of new Set(involved)) {
          activities.push(
            this.summarizeTx(chain, tx, wallet, Number(block.number)),
          );
        }
      }
    }

    // Token transfers / approvals via logs (covers txs where wallet is not outer from/to)
    const logs = await this.fetchRelevantLogs(
      client,
      chain,
      BigInt(fromBlock + 1),
      BigInt(toBlock),
      addressList,
    );

    for (const log of logs) {
      const activity = await this.summarizeLog(client, chain, log, addressSet);
      if (activity) activities.push(activity);
    }

    // Dedupe by chain+hash+wallet+kind summary before notify
    const seen = new Set<string>();
    for (const activity of activities) {
      const key = `${activity.txHash}:${activity.wallet}:${activity.kind}:${activity.summary}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const watch = addressSet.get(activity.wallet.toLowerCase());
      if (!watch) continue;

      const recipients = watch.telegramIds.filter(
        (id) =>
          !this.db.hasAlerted(chain.id, activity.txHash, activity.wallet, id),
      );
      if (recipients.length === 0) continue;

      await this.onActivity(activity, recipients);
      for (const id of recipients) {
        this.db.markAlerted(chain.id, activity.txHash, activity.wallet, id);
      }
    }

    this.db.setCursor(chain.id, toBlock);
  }

  private summarizeTx(
    chain: ChainConfig,
    tx: Transaction,
    wallet: string,
    blockNumber: number,
  ): DetectedActivity {
    const from = tx.from;
    const to = tx.to;
    const value = tx.value ?? 0n;
    const walletLower = wallet.toLowerCase();
    const isSender = from.toLowerCase() === walletLower;
    const hasInput = Boolean(tx.input && tx.input !== "0x");

    let kind: ActivityKind = "unknown";
    let summary: string;

    if (!to) {
      kind = "contract_call";
      summary = `${short(wallet)} deployed a contract`;
      if (value > 0n) {
        summary += ` with ${formatEther(value)} ${chain.nativeSymbol}`;
      }
    } else if (hasInput) {
      kind = "contract_call";
      const direction = isSender ? "called" : "was involved with";
      summary = `${short(wallet)} ${direction} ${short(to)}`;
      if (value > 0n) {
        summary += ` (+${formatEther(value)} ${chain.nativeSymbol})`;
      }
    } else if (value > 0n) {
      kind = "native_transfer";
      if (isSender) {
        summary = `${short(wallet)} sent ${formatEther(value)} ${chain.nativeSymbol} → ${to ? short(to) : "null"}`;
      } else {
        summary = `${short(wallet)} received ${formatEther(value)} ${chain.nativeSymbol} ← ${short(from)}`;
      }
    } else {
      kind = "contract_call";
      summary = `${short(wallet)} interacted with ${to ? short(to) : "unknown"}`;
    }

    return {
      chainId: chain.id,
      chainName: chain.name,
      txHash: tx.hash,
      blockNumber,
      wallet,
      kind,
      summary,
      explorerUrl: explorerUrl(chain, tx.hash),
    };
  }

  private async fetchRelevantLogs(
    client: PublicClient,
    chain: ChainConfig,
    fromBlock: bigint,
    toBlock: bigint,
    addresses: Hex[],
  ): Promise<Log[]> {
    if (addresses.length === 0) return [];

    // Public RPCs often reject huge topic filters — query per address
    const all: Log[] = [];

    for (const addr of addresses) {
      try {
        const batches = await withBackoff(`${chain.id}:logs:${addr}`, async () => {
          const [transferFrom, transferTo, approval, approvalAll] =
            await Promise.all([
              client.getLogs({
                fromBlock,
                toBlock,
                event: transferEvent,
                args: { from: addr },
              }),
              client.getLogs({
                fromBlock,
                toBlock,
                event: transferEvent,
                args: { to: addr },
              }),
              client.getLogs({
                fromBlock,
                toBlock,
                event: approvalEvent,
                args: { owner: addr },
              }),
              client.getLogs({
                fromBlock,
                toBlock,
                event: approvalForAllEvent,
                args: { owner: addr },
              }),
            ]);
          return [...transferFrom, ...transferTo, ...approval, ...approvalAll];
        });
        all.push(...batches);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        console.warn(`[poller] ${chain.id} logs for ${addr}: ${message}`);
      }
      await sleep(120);
    }

    return all;
  }

  private async summarizeLog(
    client: PublicClient,
    chain: ChainConfig,
    log: Log,
    addressSet: Map<string, WatchedAddress>,
  ): Promise<DetectedActivity | null> {
    if (!log.transactionHash || log.blockNumber == null) return null;

    const topics = log.topics;
    const eventSig = topics[0];

    const topicAddr = (topic: Hex | undefined): string | null => {
      if (!topic || topic.length < 42) return null;
      return `0x${topic.slice(-40)}`.toLowerCase();
    };

    let wallet: string | null = null;
    let kind: ActivityKind = "unknown";
    let summary = "";

    const transferSig =
      "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
    const approvalSig =
      "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925";
    const approvalForAllSig =
      "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31";

    if (eventSig === transferSig) {
      const from = topicAddr(topics[1]);
      const to = topicAddr(topics[2]);
      const isNft = topics.length >= 4; // indexed tokenId
      if (from && addressSet.has(from)) wallet = from;
      else if (to && addressSet.has(to)) wallet = to;
      if (!wallet) return null;

      const token = log.address;
      let symbol = short(token);
      try {
        symbol = (await client.readContract({
          address: token,
          abi: ERC20_ABI,
          functionName: "symbol",
        })) as string;
      } catch {
        // keep short address
      }

      if (isNft) {
        kind = "nft_transfer";
        const tokenId = topics[3] ? BigInt(topics[3]).toString() : "?";
        const direction =
          from === wallet.toLowerCase() ? "sent" : "received";
        const other =
          direction === "sent" ? (to ? short(to) : "?") : from ? short(from) : "?";
        summary = `${short(wallet)} ${direction} NFT ${symbol} #${tokenId} ${direction === "sent" ? "→" : "←"} ${other}`;
      } else {
        kind = "erc20_transfer";
        let amount = "?";
        try {
          const decimals = (await client.readContract({
            address: token,
            abi: ERC20_ABI,
            functionName: "decimals",
          })) as number;
          amount = formatUnits(BigInt(log.data), decimals);
        } catch {
          try {
            amount = BigInt(log.data).toString();
          } catch {
            amount = "?";
          }
        }
        const direction =
          from === wallet.toLowerCase() ? "sent" : "received";
        const other =
          direction === "sent" ? (to ? short(to) : "?") : from ? short(from) : "?";
        summary = `${short(wallet)} ${direction} ${amount} ${symbol} ${direction === "sent" ? "→" : "←"} ${other}`;
      }
    } else if (eventSig === approvalSig) {
      const owner = topicAddr(topics[1]);
      const spender = topicAddr(topics[2]);
      if (!owner || !addressSet.has(owner)) return null;
      wallet = owner;
      kind = "approval";
      summary = `${short(wallet)} approved ${spender ? short(spender) : "?"} on ${short(log.address)}`;
    } else if (eventSig === approvalForAllSig) {
      const owner = topicAddr(topics[1]);
      const operator = topicAddr(topics[2]);
      if (!owner || !addressSet.has(owner)) return null;
      wallet = owner;
      kind = "approval";
      const approved = BigInt(log.data) !== 0n;
      summary = `${short(wallet)} set ApprovalForAll=${approved} for ${operator ? short(operator) : "?"} on ${short(log.address)}`;
    } else {
      return null;
    }

    // Prefer checksummed wallet from watchlist
    const watched = addressSet.get(wallet.toLowerCase());
    const canonical = watched?.address ?? wallet;

    return {
      chainId: chain.id,
      chainName: chain.name,
      txHash: log.transactionHash,
      blockNumber: Number(log.blockNumber),
      wallet: canonical,
      kind,
      summary,
      explorerUrl: explorerUrl(chain, log.transactionHash),
    };
  }
}
