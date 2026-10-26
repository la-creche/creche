// The facade's one table in `flue.db`: `creche_sessions`. Flue keeps no
// list of conversations, so `session/list` reads this table. No other
// program of ours writes the file.
//
// The store sits on a mount that the agent can write. A row is data: a
// tampered row can mislead a reader, and nothing here runs it.

import type { DatabaseSync } from "node:sqlite";

/** How long a write waits while Flue holds the file, in milliseconds. */
const BUSY_TIMEOUT_MS = 5000;

/** How many sessions one `session/list` page holds. */
export const LIST_PAGE_SIZE = 50;

export interface SessionRow {
  readonly id: string;
  readonly title: string | null;
  readonly cwd: string;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface SessionPage {
  readonly rows: readonly SessionRow[];
  /** The cursor of the next page, or null on the last page. */
  readonly nextCursor: string | null;
}

/** A cursor is the row number of the last row of a page, in decimal. */
const CURSOR = /^[1-9][0-9]{0,15}$/;

const SCHEMA = `
  CREATE TABLE IF NOT EXISTS creche_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )`;

interface Raw {
  readonly seq: number;
  readonly id: string;
  readonly title: string | null;
  readonly cwd: string;
  readonly created_at: string;
  readonly updated_at: string;
}

function toRow(raw: Raw): SessionRow {
  return { id: raw.id, title: raw.title, cwd: raw.cwd, createdAt: raw.created_at, updatedAt: raw.updated_at };
}

const COLUMNS = "rowid AS seq, id, title, cwd, created_at, updated_at";

export class SessionIndex {
  /** `db` is `flue.db`, the file that Flue's `sqlite()` holds too. */
  public constructor(private readonly db: DatabaseSync) {
    db.exec(`PRAGMA busy_timeout = ${BUSY_TIMEOUT_MS}`);
    db.exec(SCHEMA);
  }

  public create(id: string, cwd: string, at: string): void {
    this.db.prepare("INSERT INTO creche_sessions (id, cwd, created_at, updated_at) VALUES (?, ?, ?, ?)").run(id, cwd, at, at);
  }

  public get(id: string): SessionRow | null {
    const raw = this.db.prepare(`SELECT ${COLUMNS} FROM creche_sessions WHERE id = ?`).get(id) as Raw | undefined;

    return raw === undefined ? null : toRow(raw);
  }

  /** Sets the title once. A later call keeps the first title. */
  public nameOnce(id: string, title: string): void {
    this.db.prepare("UPDATE creche_sessions SET title = ? WHERE id = ? AND title IS NULL").run(title, id);
  }

  public touch(id: string, at: string): void {
    this.db.prepare("UPDATE creche_sessions SET updated_at = ? WHERE id = ?").run(at, id);
  }

  /**
   * One page, newest session first. The order is the order of creation, so
   * a session that changes during a walk keeps its place. Null for a cursor
   * that this table did not give.
   */
  public list(cwd: string | null, cursor: string | null): SessionPage | null {
    if (cursor !== null && !CURSOR.test(cursor)) {
      return null;
    }

    const before = cursor === null ? Number.MAX_SAFE_INTEGER : Number(cursor);
    const raws = (
      cwd === null
        ? this.db
            .prepare(`SELECT ${COLUMNS} FROM creche_sessions WHERE rowid < ? ORDER BY rowid DESC LIMIT ?`)
            .all(before, LIST_PAGE_SIZE + 1)
        : this.db
            .prepare(`SELECT ${COLUMNS} FROM creche_sessions WHERE rowid < ? AND cwd = ? ORDER BY rowid DESC LIMIT ?`)
            .all(before, cwd, LIST_PAGE_SIZE + 1)
    ) as unknown as Raw[];

    const page = raws.slice(0, LIST_PAGE_SIZE);
    const last = page.at(-1);
    const nextCursor = raws.length > LIST_PAGE_SIZE && last !== undefined ? String(last.seq) : null;

    return { rows: page.map(toRow), nextCursor };
  }
}
