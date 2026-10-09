// Reads one index store as the bridge of the playpen reads it.
//
//   node reader_store.mjs <store.db> <match> <limit>
//
// The index builder writes a store with its own SQLite. The bridge reads the
// store with the SQLite of Node, on a read-only connection. A test that
// reads a store with the `sqlite3` module of Python does not prove that the
// bridge can read it. This program does: it opens one store as the bridge
// opens it, and it runs the statements of the bridge.
//
// The bridge is `playpen/bridge/index-store.ts`. The comment above each
// statement here names the lines of that file. Each statement here has the
// string literals of the bridge. The bridge builds one statement from three
// literals, and one from a template with the constant `VECTOR_TABLE`.
// `test_proc_library_reader.py` holds the literals and the constant of this
// file against the text of the bridge. Change the two files together.
//
// This program does not rank a row and it does not check a row. It prints
// what each statement gives, as one JSON object on stdout:
//
//   meta      each row of the first statement: `key` and `value`
//   table     whether the store has the plain vector table
//   lexical   the rows of <match>, <limit> at most, in the order of the
//             statement: `id`, `path`, `ord` and `text`
//   vectors   each row of the plain vector table: `id` and `embedding`.
//             Null for a store with no such table.
//   chunks    the row of `chunks` for each row of `vectors`, in that
//             order. Null for an id with no row.
//
// A blob is one object with the key `hex`. A reader of the output can then
// tell a blob from a text.
//
// The exit status is 0 for a store that each statement can read. It is 2
// for a wrong command line. A store that a statement cannot read ends this
// program with the error of Node and a status that is not 0.

import { DatabaseSync } from "node:sqlite";

const [storePath, match, limitText] = process.argv.slice(2);
const limit = Number(limitText);

if (storePath === undefined || match === undefined || !Number.isInteger(limit) || limit < 1) {
  process.stderr.write("usage: reader_store.mjs <store.db> <match> <limit>\n");
  process.exit(2);
}

// index-store.ts:37
const VECTOR_TABLE = "chunks_emb";

/** JSON has no bytes. A blob becomes its hex text under one key. */
function plain(_key, value) {
  return value instanceof Uint8Array ? { hex: Buffer.from(value).toString("hex") } : value;
}

// index-store.ts:139
const db = new DatabaseSync(storePath, { readOnly: true });

// index-store.ts:206
const meta = db.prepare("SELECT key, value FROM meta").all();

// index-store.ts:277
const table = db
  .prepare("SELECT 1 AS found FROM sqlite_master WHERE type = 'table' AND name = ?")
  .get(VECTOR_TABLE);

// index-store.ts:227-229
const lexical = db
  .prepare(
    "SELECT c.id AS id, c.path AS path, c.ord AS ord, c.text AS text " +
      "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid " +
      "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
  )
  .all(match, limit);

// index-store.ts:257. The bridge runs it only on a store that has the table.
const vectors =
  table === undefined ? null : db.prepare(`SELECT id, embedding FROM ${VECTOR_TABLE}`).all();

// index-store.ts:285
const one = db.prepare("SELECT id, path, ord, text FROM chunks WHERE id = ?");
const chunks = (vectors ?? []).map((row) => one.get(row.id) ?? null);

db.close();

const answer = { meta, table: table !== undefined, lexical, vectors, chunks };

process.stdout.write(`${JSON.stringify(answer, plain)}\n`);
