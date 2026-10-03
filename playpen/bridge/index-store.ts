// One corpus index, read over the family's read-only mount (contract 03 §7.3
// rule 6). The library writes the store and this file reads it, so the schema
// is a cross-language contract (`library/README.md`, "Store schema"):
//
//   <index root>/<name>/store.db
//     meta(key, value)            embed_model, dims
//     chunks(id, path, ord, text)
//     chunks_fts = fts5(text)     rowid = chunk id     the words
//     chunks_emb(id, embedding)   float32 LE x dims    the meaning
//
// `chunks_vec` holds the same vectors for sqlite-vec, and this file never
// touches it. `node:sqlite` loads an extension only from a native library,
// and shipping one would add a runtime dependency to the image (AGENTS.md
// rule 17). So the nearest vectors come from a full scan here, which is what
// sqlite-vec's `vec0` does too. The largest store on the host held 13 994
// chunks on 2026-09-23.
//
// `node:sqlite` is imported on first use, not at load: a Node without it then
// costs this one tool, never the bridge and its PEP tools (AGENTS.md rule 21).

import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import type { DatabaseSync, SQLOutputValue } from "node:sqlite";

import { STORE_FILE } from "./constants.js";

/** An index directory's name: a vault scope, `code-<repo>`, `house__ha`. */
export const INDEX_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** `meta.dims`, as the library writes it: a positive decimal integer. */
const DIMS_RE = /^[1-9][0-9]{0,4}$/;

/** Every component of a stored vector is one float32. */
const FLOAT_BYTES = 4;

/** The plain vector table. Its absence refuses the vector half. */
const VECTOR_TABLE = "chunks_emb";

export interface IndexRef {
  readonly name: string;
  /** The `store.db` file. */
  readonly path: string;
}

export interface ChunkHit {
  readonly id: number;
  readonly path: string;
  readonly ord: number;
  readonly text: string;
}

export interface StoreMeta {
  /** The model the vectors came from, or null when the store recorded none. */
  readonly model: string | null;
  /** Floats per vector, or null when the store recorded no usable number. */
  readonly dims: number | null;
  /** Whether the store carries `chunks_emb`. */
  readonly plainVectors: boolean;
}

export interface NearHit extends ChunkHit {
  /** Squared L2 to the query. Every index shares one model space, so it compares across them. */
  readonly distance: number;
}

export interface Nearest {
  /** Nearest first. */
  readonly hits: readonly NearHit[];
  /** Rows whose blob is not the query's length, left out of the ranking. */
  readonly skipped: number;
}

interface Scored {
  readonly id: number;
  readonly distance: number;
}

type SqliteModule = typeof import("node:sqlite");

let sqlite: Promise<SqliteModule> | null = null;

function loadSqlite(): Promise<SqliteModule> {
  sqlite ??= import("node:sqlite");

  return sqlite;
}

function isKind(path: string, kind: "file" | "directory"): boolean {
  try {
    const stat = statSync(path);

    return kind === "file" ? stat.isFile() : stat.isDirectory();
  } catch {
    return false;
  }
}

/** Whether the family mounts an index. Only a mount makes the root exist. */
export function isIndexRoot(root: string): boolean {
  return root.length > 0 && isKind(root, "directory");
}

/**
 * Every index under the root that holds a store, sorted by name.
 *
 * A directory with no `store.db` is skipped: the library leaves some behind
 * when a scope is renamed, and they hold nothing to search. So is a name the
 * library never writes, because it becomes a label the model reads.
 */
export function listIndexes(root: string): IndexRef[] {
  let names: string[];
  try {
    names = readdirSync(root);
  } catch {
    return [];
  }

  const found: IndexRef[] = [];
  for (const name of names.sort()) {
    if (!INDEX_NAME_RE.test(name)) {
      continue;
    }

    const path = join(root, name, STORE_FILE);
    if (!isKind(path, "file")) {
      continue;
    }

    found.push({ name, path });
  }

  return found;
}

/** Read-only, so nothing here can change a store even on a writable mount. */
export async function openIndex(index: IndexRef): Promise<IndexStore> {
  const { DatabaseSync } = await loadSqlite();

  return new IndexStore(new DatabaseSync(index.path, { readOnly: true }));
}

function text(row: Record<string, SQLOutputValue>, key: string): string | null {
  const value = row[key];

  return typeof value === "string" ? value : null;
}

function integer(row: Record<string, SQLOutputValue>, key: string): number | null {
  const value = row[key];

  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function toHit(row: Record<string, SQLOutputValue> | undefined): ChunkHit | null {
  if (row === undefined) {
    return null;
  }

  const id = integer(row, "id");
  const path = text(row, "path");
  const ord = integer(row, "ord");
  const body = text(row, "text");
  if (id === null || path === null || ord === null || body === null) {
    return null;
  }

  return { id, path, ord, text: body };
}

/** Squared euclidean distance: the ranking of L2, without the square root. */
function squaredL2(query: Float64Array, raw: Uint8Array): number {
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  let sum = 0;
  for (let index = 0; index < query.length; index += 1) {
    const delta = (query[index] ?? 0) - view.getFloat32(index * FLOAT_BYTES, true);
    sum += delta * delta;
  }

  return sum;
}

/** Nearer first, then the lower chunk id, so equal distances rank the same every time. */
function closer(a: Scored, b: Scored): number {
  return a.distance - b.distance || a.id - b.id;
}

/** Keeps `best` sorted and at most `limit` long. */
function keep(best: Scored[], candidate: Scored, limit: number): void {
  const last = best[best.length - 1];
  if (best.length >= limit && last !== undefined && closer(candidate, last) >= 0) {
    return;
  }

  best.push(candidate);
  best.sort(closer);
  if (best.length > limit) {
    best.pop();
  }
}

export class IndexStore {
  public constructor(private readonly db: DatabaseSync) {}

  public meta(): StoreMeta {
    const values = new Map<string, string>();
    for (const row of this.db.prepare("SELECT key, value FROM meta").all()) {
      const key = text(row, "key");
      const value = text(row, "value");
      if (key !== null && value !== null) {
        values.set(key, value);
      }
    }

    const dims = values.get("dims") ?? "";

    return {
      model: values.get("embed_model") ?? null,
      dims: DIMS_RE.test(dims) ? Number(dims) : null,
      plainVectors: this.hasTable(VECTOR_TABLE),
    };
  }

  /** FTS5 over the chunk text, best bm25 first. `match` is an FTS5 query. */
  public lexical(match: string, limit: number): ChunkHit[] {
    const rows = this.db
      .prepare(
        "SELECT c.id AS id, c.path AS path, c.ord AS ord, c.text AS text " +
          "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid " +
          "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
      )
      .all(match, limit);

    const hits: ChunkHit[] = [];
    for (const row of rows) {
      const hit = toHit(row);
      if (hit !== null) {
        hits.push(hit);
      }
    }

    return hits;
  }

  /**
   * The `limit` chunks whose vectors lie nearest the query, by L2.
   *
   * The library stores the vectors TEI returns, and TEI normalizes them, so
   * this is also the cosine ranking. One pass, one row at a time: the scan
   * holds `limit` candidates, never the whole table.
   */
  public nearest(vector: readonly number[], limit: number): Nearest {
    const query = Float64Array.from(vector);
    const want = query.length * FLOAT_BYTES;
    const best: Scored[] = [];
    let skipped = 0;

    for (const row of this.db.prepare(`SELECT id, embedding FROM ${VECTOR_TABLE}`).iterate()) {
      const id = integer(row, "id");
      const raw = row["embedding"];
      if (id === null || !(raw instanceof Uint8Array) || raw.byteLength !== want) {
        skipped += 1;
        continue;
      }

      keep(best, { id, distance: squaredL2(query, raw) }, limit);
    }

    return { hits: this.withChunks(best), skipped };
  }

  public close(): void {
    this.db.close();
  }

  private hasTable(name: string): boolean {
    const row = this.db
      .prepare("SELECT 1 AS found FROM sqlite_master WHERE type = 'table' AND name = ?")
      .get(name);

    return row !== undefined;
  }

  /** The chunk row behind each scored id, in the order given. */
  private withChunks(best: readonly Scored[]): NearHit[] {
    const one = this.db.prepare("SELECT id, path, ord, text FROM chunks WHERE id = ?");
    const hits: NearHit[] = [];
    for (const scored of best) {
      const hit = toHit(one.get(scored.id));
      if (hit !== null) {
        hits.push({ ...hit, distance: scored.distance });
      }
    }

    return hits;
  }
}
