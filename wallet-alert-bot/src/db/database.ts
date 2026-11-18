import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { getAddress, isAddress } from "viem";

export type UserRow = {
  telegram_id: number;
  username: string | null;
  is_admin: number;
  approved: number;
  created_at: string;
};

export type WalletRow = {
  id: number;
  telegram_id: number;
  address: string;
  label: string | null;
  created_at: string;
};

export type WatchedAddress = {
  address: string;
  telegramIds: number[];
};

function normalizeUsername(input: string | null | undefined): string | null {
  if (!input) return null;
  const cleaned = input.trim().replace(/^@/, "").toLowerCase();
  return cleaned.length > 0 ? cleaned : null;
}

export function normalizeAddress(input: string): string {
  const trimmed = input.trim();
  if (!isAddress(trimmed)) {
    throw new Error("Invalid EVM address");
  }
  return getAddress(trimmed);
}

export class BotDatabase {
  readonly db: Database.Database;

  constructor(path: string) {
    mkdirSync(dirname(path), { recursive: true });
    this.db = new Database(path);
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("foreign_keys = ON");
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        is_admin INTEGER NOT NULL DEFAULT 0,
        approved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS pending_approvals (
        username TEXT PRIMARY KEY,
        requested_by INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS wallets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        address TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(telegram_id, address),
        FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
      );

      CREATE INDEX IF NOT EXISTS idx_wallets_address ON wallets(address);

      CREATE TABLE IF NOT EXISTS chain_cursors (
        chain_id TEXT PRIMARY KEY,
        last_block INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
      );

      CREATE TABLE IF NOT EXISTS alerted_txs (
        chain_id TEXT NOT NULL,
        tx_hash TEXT NOT NULL,
        address TEXT NOT NULL,
        telegram_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (chain_id, tx_hash, address, telegram_id)
      );
    `);
  }

  ensureAdmin(adminId: number): void {
    this.db
      .prepare(
        `INSERT INTO users (telegram_id, is_admin, approved)
         VALUES (?, 1, 1)
         ON CONFLICT(telegram_id) DO UPDATE SET is_admin = 1, approved = 1`,
      )
      .run(adminId);
  }

  upsertUserContact(telegramId: number, username: string | null): UserRow {
    const uname = normalizeUsername(username);
    this.db
      .prepare(
        `INSERT INTO users (telegram_id, username, approved, is_admin)
         VALUES (?, ?, 0, 0)
         ON CONFLICT(telegram_id) DO UPDATE SET
           username = COALESCE(excluded.username, users.username)`,
      )
      .run(telegramId, uname);

    if (uname) {
      const pending = this.db
        .prepare(`SELECT username FROM pending_approvals WHERE username = ?`)
        .get(uname) as { username: string } | undefined;
      if (pending) {
        this.db
          .prepare(`UPDATE users SET approved = 1 WHERE telegram_id = ?`)
          .run(telegramId);
        this.db
          .prepare(`DELETE FROM pending_approvals WHERE username = ?`)
          .run(uname);
      }
    }

    return this.getUser(telegramId)!;
  }

  getUser(telegramId: number): UserRow | undefined {
    return this.db
      .prepare(`SELECT * FROM users WHERE telegram_id = ?`)
      .get(telegramId) as UserRow | undefined;
  }

  isApproved(telegramId: number): boolean {
    const row = this.getUser(telegramId);
    return Boolean(row?.approved);
  }

  isAdmin(telegramId: number): boolean {
    const row = this.getUser(telegramId);
    return Boolean(row?.is_admin);
  }

  approveById(telegramId: number): UserRow {
    this.db
      .prepare(
        `INSERT INTO users (telegram_id, approved)
         VALUES (?, 1)
         ON CONFLICT(telegram_id) DO UPDATE SET approved = 1`,
      )
      .run(telegramId);
    return this.getUser(telegramId)!;
  }

  approveByUsername(username: string): { status: "approved" | "pending"; telegramId?: number } {
    const uname = normalizeUsername(username);
    if (!uname) throw new Error("Invalid username");

    const existing = this.db
      .prepare(`SELECT * FROM users WHERE lower(username) = ?`)
      .get(uname) as UserRow | undefined;

    if (existing) {
      this.db
        .prepare(`UPDATE users SET approved = 1 WHERE telegram_id = ?`)
        .run(existing.telegram_id);
      this.db
        .prepare(`DELETE FROM pending_approvals WHERE username = ?`)
        .run(uname);
      return { status: "approved", telegramId: existing.telegram_id };
    }

    this.db
      .prepare(
        `INSERT INTO pending_approvals (username, requested_by)
         VALUES (?, 0)
         ON CONFLICT(username) DO NOTHING`,
      )
      .run(uname);
    return { status: "pending" };
  }

  revokeById(telegramId: number): boolean {
    const user = this.getUser(telegramId);
    if (!user) return false;
    if (user.is_admin) throw new Error("Cannot revoke the admin");
    this.db.prepare(`UPDATE users SET approved = 0 WHERE telegram_id = ?`).run(telegramId);
    this.db.prepare(`DELETE FROM wallets WHERE telegram_id = ?`).run(telegramId);
    return true;
  }

  revokeByUsername(username: string): { revoked: boolean; pendingCleared: boolean } {
    const uname = normalizeUsername(username);
    if (!uname) throw new Error("Invalid username");
    const pending = this.db
      .prepare(`DELETE FROM pending_approvals WHERE username = ?`)
      .run(uname);
    const user = this.db
      .prepare(`SELECT * FROM users WHERE lower(username) = ?`)
      .get(uname) as UserRow | undefined;
    if (user) {
      if (user.is_admin) throw new Error("Cannot revoke the admin");
      this.db.prepare(`UPDATE users SET approved = 0 WHERE telegram_id = ?`).run(user.telegram_id);
      this.db.prepare(`DELETE FROM wallets WHERE telegram_id = ?`).run(user.telegram_id);
      return { revoked: true, pendingCleared: pending.changes > 0 };
    }
    return { revoked: false, pendingCleared: pending.changes > 0 };
  }

  listUsers(): {
    approved: UserRow[];
    pending: { username: string; created_at: string }[];
  } {
    const approved = this.db
      .prepare(`SELECT * FROM users WHERE approved = 1 ORDER BY telegram_id`)
      .all() as UserRow[];
    const pending = this.db
      .prepare(`SELECT username, created_at FROM pending_approvals ORDER BY username`)
      .all() as { username: string; created_at: string }[];
    return { approved, pending };
  }

  addWallet(telegramId: number, addressInput: string, label?: string): WalletRow {
    if (!this.isApproved(telegramId)) {
      throw new Error("User is not approved");
    }
    const address = normalizeAddress(addressInput);
    this.db
      .prepare(
        `INSERT INTO wallets (telegram_id, address, label)
         VALUES (?, ?, ?)
         ON CONFLICT(telegram_id, address) DO UPDATE SET label = COALESCE(excluded.label, wallets.label)`,
      )
      .run(telegramId, address, label ?? null);
    return this.db
      .prepare(`SELECT * FROM wallets WHERE telegram_id = ? AND address = ?`)
      .get(telegramId, address) as WalletRow;
  }

  removeWallet(telegramId: number, addressInput: string): boolean {
    const address = normalizeAddress(addressInput);
    const result = this.db
      .prepare(`DELETE FROM wallets WHERE telegram_id = ? AND address = ?`)
      .run(telegramId, address);
    return result.changes > 0;
  }

  listWallets(telegramId: number): WalletRow[] {
    return this.db
      .prepare(`SELECT * FROM wallets WHERE telegram_id = ? ORDER BY id`)
      .all(telegramId) as WalletRow[];
  }

  getWatchedAddresses(): WatchedAddress[] {
    const rows = this.db
      .prepare(
        `SELECT w.address, w.telegram_id
         FROM wallets w
         JOIN users u ON u.telegram_id = w.telegram_id
         WHERE u.approved = 1`,
      )
      .all() as { address: string; telegram_id: number }[];

    const map = new Map<string, number[]>();
    for (const row of rows) {
      const list = map.get(row.address) ?? [];
      list.push(row.telegram_id);
      map.set(row.address, list);
    }
    return [...map.entries()].map(([address, telegramIds]) => ({
      address,
      telegramIds,
    }));
  }

  getCursor(chainId: string): number {
    const row = this.db
      .prepare(`SELECT last_block FROM chain_cursors WHERE chain_id = ?`)
      .get(chainId) as { last_block: number } | undefined;
    return row?.last_block ?? 0;
  }

  setCursor(chainId: string, lastBlock: number): void {
    this.db
      .prepare(
        `INSERT INTO chain_cursors (chain_id, last_block, updated_at)
         VALUES (?, ?, datetime('now'))
         ON CONFLICT(chain_id) DO UPDATE SET
           last_block = excluded.last_block,
           updated_at = excluded.updated_at`,
      )
      .run(chainId, lastBlock);
  }

  hasAlerted(
    chainId: string,
    txHash: string,
    address: string,
    telegramId: number,
  ): boolean {
    const row = this.db
      .prepare(
        `SELECT 1 AS ok FROM alerted_txs
         WHERE chain_id = ? AND tx_hash = ? AND address = ? AND telegram_id = ?`,
      )
      .get(chainId, txHash.toLowerCase(), address, telegramId);
    return Boolean(row);
  }

  markAlerted(
    chainId: string,
    txHash: string,
    address: string,
    telegramId: number,
  ): void {
    this.db
      .prepare(
        `INSERT OR IGNORE INTO alerted_txs (chain_id, tx_hash, address, telegram_id)
         VALUES (?, ?, ?, ?)`,
      )
      .run(chainId, txHash.toLowerCase(), address, telegramId);
  }

  pruneAlerted(olderThanDays = 14): void {
    this.db
      .prepare(
        `DELETE FROM alerted_txs
         WHERE created_at < datetime('now', ?)`,
      )
      .run(`-${olderThanDays} days`);
  }

  close(): void {
    this.db.close();
  }
}
