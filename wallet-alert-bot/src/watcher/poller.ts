import {
  createPublicClient,
  fallback,
  formatEther,
  formatUnits,
  http,
  type Hex,
  type Log,
  type PublicClient,
  type Transaction,
  type TransactionReceipt,
} from "viem";
import type { AppConfig, ChainConfig } from "../config/index.js";
import type { BotDatabase, WatchedAddress } from "../db/database.js";
import type { ActivityKind, DetectedActivity } from "./types.js";

const TRANSFER_SIG =
  "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
const APPROVAL_SIG =
  "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925";
const APPROVAL_FOR_ALL_SIG =
  "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31";

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

function topicAddr(topic: Hex | undefined): string | null {
  if (!topic || topic.length < 42) return null;
  return `0x${topic.slice(-40)}`.toLowerCase();
}

function logTouchesWatched(
  log: Log | { topics: readonly Hex[]; address: Hex },
  addressSet: Map<string, WatchedAddress>,
): boolean {
  const topics = log.topics;
  const sig = topics[0];
  if (sig === TRANSFER_SIG) {
    const from = topicAddr(topics[1]);
    const to = topicAddr(topics[2]);
    return Boolean(
      (from && addressSet.has(from)) || (to && addressSet.has(to)),
    );
  }
  if (sig === APPROVAL_SIG || sig === APPROVAL_FOR_ALL_SIG) {
    const owner = topicAddr(topics[1]);
    return Boolean(owner && addressSet.has(owner));
  }
  return false;
}

async function mapPool<T, R>(
  items: T[],
  concurrency: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  const workers = Array.from(
    { length: Math.min(concurrency, Math.max(1, items.length)) },
    async () => {
      while (true) {
        const i = next++;
        if (i >= items.length) return;
        results[i] = await fn(items[i]!);
      }
    },
  );
  await Promise.all(workers);
  return results;
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
        /429|rate|timeout|ECONNRESET|503|502|fetch failed|limit exceeded|request limit/i.test(
          message,
        );
      if (!retryable || i === attempts - 1) break;
      const delay = 1000 * 2 ** i + Math.floor(Math.random() * 250);
      console.warn(`[rpc] ${label} failed (${message}); retry in ${delay}ms`);
      await sleep(delay);
    }
  }
  throw lastError;
}

export type ActivityHandler = (
  activity: DetectedActivity,
  telegramIds: number[],
) => Promise<void>;

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
      const transports = chain.rpcUrlList.map((url) =>
        http(url, {
          timeout: config.requestTimeoutMs,
          retryCount: 0,
        }),
      );
      const client = createPublicClient({
        transport:
          transports.length === 1
            ? transports[0]!
            : fallback(transports, { rank: false }),
      });
      this.clients.set(chain.id, client as PublicClient);
      console.log(
        `[poller] ${chain.id} RPCs: ${chain.rpcUrlList.join(" → ")}`,
      );
    }
  }

  start(): void {
    if (this.config.dryRun) {
      console.log("[poller] DRY_RUN=1 — chain polling disabled");
      return;
    }
    console.log(
      `[poller] watching ${this.config.chains.map((c) => c.id).join(", ")} every ${this.config.pollIntervalMs}ms (batch=${this.config.blockBatchSize}, catchup=${this.config.catchupBatchSize}, parallel chains)`,
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

      // Keep chain concurrency low — public RPCs collapse under multi-chain load.
      const chainConcurrency = Math.min(
        2,
        Math.max(1, this.config.chains.length),
      );
      await mapPool(this.config.chains, chainConcurrency, async (chain) => {
        if (this.stopped) return;
        try {
          await this.pollChain(chain, watched);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          console.warn(`[poller] ${chain.id}: ${message}`);
        }
      });
      this.db.pruneAlerted(14);
    } finally {
      this.running = false;
    }
  }

  private adaptiveWindow(lag: number): number {
    if (lag <= 0) return 0;
    if (lag <= this.config.blockBatchSize) return lag;
    // Catch up in larger chunks; never snap over history.
    return Math.min(lag, this.config.catchupBatchSize);
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

    const lag = latest - fromBlock;
    const window = this.adaptiveWindow(lag);
    const toBlock = fromBlock + window;
    if (toBlock <= fromBlock) return;

    if (lag > this.config.catchupBatchSize * 2) {
      console.warn(
        `[poller] ${chain.id}: catching up lag=${lag} (scanning ${fromBlock + 1}-${toBlock}, no snap)`,
      );
    }

    const addressSet = new Map(
      watched.map((w) => [w.address.toLowerCase(), w] as const),
    );

    const activities: DetectedActivity[] = [];
    const logs: Log[] = [];
    const involvedTx = new Map<
      string,
      { hash: Hex; wallet: string; blockNumber: number }
    >();

    // Receipts-first on public RPCs: topic eth_getLogs without a contract
    // address is widely restricted and stamps endpoints when fanned out across
    // wallets × chains. Block receipts still yield Transfer / Approval logs
    // plus outer from/to for native + contract calls.
    const blockNums: number[] = [];
    for (let n = fromBlock + 1; n <= toBlock; n++) blockNums.push(n);

    const receiptResults = await mapPool(
      blockNums,
      this.config.receiptConcurrency,
      async (blockNum) => {
        if (this.stopped) {
          return {
            blockNum,
            logs: [] as Log[],
            covered: false,
            involved: [] as { hash: Hex; wallet: string }[],
          };
        }
        return this.scanBlock(client, chain, blockNum, addressSet);
      },
    );

    let blocksOk = 0;
    for (const result of receiptResults) {
      if (!result) continue;
      blocksOk += 1;
      logs.push(...result.logs);
      for (const hit of result.involved) {
        const key = `${hit.hash}:${hit.wallet}`;
        if (!involvedTx.has(key)) {
          involvedTx.set(key, {
            hash: hit.hash,
            wallet: hit.wallet,
            blockNumber: result.blockNum,
          });
        }
      }
    }

    // 3) Fetch full txs only for wallets that appear as outer from/to.
    const txHits = [...involvedTx.values()];
    const txs = await mapPool(
      txHits,
      Math.min(6, this.config.receiptConcurrency),
      async (hit) => {
        try {
          const tx = await withBackoff(`${chain.id}:tx:${hit.hash}`, () =>
            client.getTransaction({ hash: hit.hash }),
          );
          return { hit, tx };
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          console.warn(`[poller] ${chain.id} getTransaction ${hit.hash}: ${message}`);
          return null;
        }
      },
    );

    for (const item of txs) {
      if (!item) continue;
      activities.push(
        this.summarizeTx(
          chain,
          item.tx,
          item.hit.wallet,
          item.hit.blockNumber,
        ),
      );
    }

    for (const log of logs) {
      const activity = await this.summarizeLog(client, chain, log, addressSet);
      if (activity) activities.push(activity);
    }

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
    console.log(
      `[poller] ${chain.id}: blocks ${fromBlock + 1}-${toBlock} tip=${latest} scanned=${blocksOk} lag=${latest - toBlock} activities=${activities.length}`,
    );
  }

  private async scanBlock(
    client: PublicClient,
    chain: ChainConfig,
    blockNum: number,
    addressSet: Map<string, WatchedAddress>,
  ): Promise<{
    blockNum: number;
    logs: Log[];
    covered: boolean;
    involved: { hash: Hex; wallet: string }[];
  }> {
    const involved: { hash: Hex; wallet: string }[] = [];
    const pushInvolved = (hash: Hex, from?: string | null, to?: string | null) => {
      const candidates = [from, to]
        .filter(Boolean)
        .map((a) => a!.toLowerCase());
      for (const wallet of new Set(candidates)) {
        if (addressSet.has(wallet)) {
          involved.push({ hash, wallet });
        }
      }
    };

    try {
      const receipts = await withBackoff(
        `${chain.id}:blockReceipts:${blockNum}`,
        () => client.getBlockReceipts({ blockNumber: BigInt(blockNum) }),
        3,
      );
      if (receipts) {
        const outLogs: Log[] = [];
        for (const receipt of receipts) {
          pushInvolved(
            receipt.transactionHash,
            receipt.from,
            receipt.to ?? null,
          );
          outLogs.push(...this.filterReceiptLogs([receipt], addressSet));
        }
        return { blockNum, logs: outLogs, covered: true, involved };
      }
    } catch {
      // Fall through to getBlock path
    }

    // Fallback: full block with transactions
    const block = await withBackoff(`${chain.id}:getBlock:${blockNum}`, () =>
      client.getBlock({
        blockNumber: BigInt(blockNum),
        includeTransactions: true,
      }),
    );

    for (const tx of block.transactions) {
      if (typeof tx === "string") continue;
      pushInvolved(tx.hash, tx.from, tx.to ?? null);
    }

    // Per-tx receipts only for involved hashes (partial ERC-20 receive coverage)
    const outLogs: Log[] = [];
    const hashes = [...new Set(involved.map((i) => i.hash))];
    for (const hash of hashes) {
      try {
        const receipt = await withBackoff(`${chain.id}:receipt:${hash}`, () =>
          client.getTransactionReceipt({ hash }),
        );
        outLogs.push(...this.filterReceiptLogs([receipt], addressSet));
      } catch {
        // ignore
      }
    }
    return { blockNum, logs: outLogs, covered: false, involved };
  }

  private filterReceiptLogs(
    receipts: readonly TransactionReceipt[],
    addressSet: Map<string, WatchedAddress>,
  ): Log[] {
    const out: Log[] = [];
    for (const receipt of receipts) {
      for (const log of receipt.logs) {
        if (logTouchesWatched(log, addressSet)) {
          out.push(log as Log);
        }
      }
    }
    return out;
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

  private async summarizeLog(
    client: PublicClient,
    chain: ChainConfig,
    log: Log,
    addressSet: Map<string, WatchedAddress>,
  ): Promise<DetectedActivity | null> {
    if (!log.transactionHash || log.blockNumber == null) return null;

    const topics = log.topics;
    const eventSig = topics[0];

    let wallet: string | null = null;
    let kind: ActivityKind = "unknown";
    let summary = "";

    if (eventSig === TRANSFER_SIG) {
      const from = topicAddr(topics[1]);
      const to = topicAddr(topics[2]);
      const isNft = topics.length >= 4;
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
    } else if (eventSig === APPROVAL_SIG) {
      const owner = topicAddr(topics[1]);
      const spender = topicAddr(topics[2]);
      if (!owner || !addressSet.has(owner)) return null;
      wallet = owner;
      kind = "approval";
      summary = `${short(wallet)} approved ${spender ? short(spender) : "?"} on ${short(log.address)}`;
    } else if (eventSig === APPROVAL_FOR_ALL_SIG) {
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
