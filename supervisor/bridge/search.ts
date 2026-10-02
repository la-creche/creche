// `index_search`: the one tool this bridge serves itself (contract 03 §7.3
// rule 6). Hybrid retrieval over every corpus index the family mounts:
//
//   query ─┬─► chunks_fts MATCH, best bm25 first, per index ─────────┐
//          └─► embed (PEP) ─► chunks_emb, nearest first, all indexes ─┴─► reciprocal rank fusion
//
// The two halves rank differently on purpose. bm25 leans on each corpus's
// own word statistics, so its scores do not compare across indexes and each
// index keeps its own ranking. Every vector lives in one model space, so the
// distances do compare, and the nearest chunks of all indexes form ONE list.
//
// The fail-closed rule is the indexer's (`indexer/AGENTS.md`): an index whose
// recorded `embed_model` is not the model `embed` answered with refuses its
// vector half, because vectors from two model spaces do not compare. An index
// that recorded no model, holds no `chunks_emb` or disagrees on dims refuses
// the same way. The words half keeps serving in every case, and each refusal
// is one note the model reads.
//
// Every result is corpus text, so it goes back inside the untrusted block
// every PEP result wears (invariant 14).

import { SEARCH_TOOL } from "./constants.js";
import { INDEX_NAME_RE, listIndexes, openIndex } from "./index-store.js";
import type { ChunkHit, IndexRef, IndexStore, NearHit, StoreMeta } from "./index-store.js";
import type { PiToolDefinition, PiToolResult } from "./pi-api.js";
import { wrapUntrusted } from "./untrusted.js";

/** Reciprocal rank fusion's damping constant, the customary 60. */
const RRF_K = 60;

/** Candidates per ranking before fusion: per index for words, overall for meaning. */
const CANDIDATES = 20;

const DEFAULT_RESULTS = 6;
const MAX_RESULTS = 12;
const MIN_QUERY_LENGTH = 2;
const MAX_QUERY_LENGTH = 512;
const MAX_NAMED_INDEXES = 32;

/** Distinct words of a query that reach FTS5. More only slow the MATCH down. */
const MAX_QUERY_WORDS = 32;

/** Characters of a chunk shown per result. The model reads the file for more. */
const SNIPPET_CHARS = 500;

/** An error text a store raised lands in the model's context. Keep it short. */
const MAX_ERROR_CHARS = 200;

/** What FTS5 calls a word: letters, digits and the marks that attach to them. */
const WORD_RE = /[\p{L}\p{N}\p{M}]+/gu;

/** How the semantic half's query vector came out. */
export enum EmbedState {
  Ready = "ready",
  Unavailable = "unavailable",
}

export type EmbedOutcome =
  | { readonly state: EmbedState.Ready; readonly vector: readonly number[]; readonly model: string }
  | { readonly state: EmbedState.Unavailable; readonly why: string };

/** The one network touch: text to vector. `query-embed.ts` puts the PEP behind it. */
export interface QueryEmbedder {
  embed(text: string): Promise<EmbedOutcome>;
}

/** Which half of the search found a chunk. */
export enum Half {
  Words = "words",
  Meaning = "meaning",
}

export interface SearchRequest {
  readonly query: string;
  readonly k: number;
  /** Null searches every index. */
  readonly indexes: readonly string[] | null;
}

export interface SearchHit {
  readonly index: string;
  readonly path: string;
  readonly chunk: number;
  readonly matched: readonly Half[];
  readonly score: number;
  readonly snippet: string;
}

export interface SearchAnswer {
  readonly hits: readonly SearchHit[];
  readonly notes: readonly string[];
  /** How many indexes were read. */
  readonly searched: number;
}

interface Fused {
  readonly index: string;
  readonly hit: ChunkHit;
  score: number;
  readonly matched: Set<Half>;
}

/** One chunk of one index, in a ranking. */
interface Ranked {
  readonly index: string;
  readonly hit: ChunkHit;
}

/** One chunk the vector half found, with its distance to the query. */
interface Near {
  readonly index: string;
  readonly hit: NearHit;
}

/** One index, open, with what its `meta` says. */
interface OpenIndex {
  readonly ref: IndexRef;
  readonly store: IndexStore;
  readonly meta: StoreMeta;
}

const PARAMETERS = {
  type: "object",
  additionalProperties: false,
  required: ["query"],
  properties: {
    query: {
      type: "string",
      minLength: MIN_QUERY_LENGTH,
      maxLength: MAX_QUERY_LENGTH,
      description: "What to look for, in plain words.",
    },
    k: {
      type: "integer",
      minimum: 1,
      maximum: MAX_RESULTS,
      description: `How many results to return. Default ${DEFAULT_RESULTS}.`,
    },
    indexes: {
      type: "array",
      maxItems: MAX_NAMED_INDEXES,
      items: { type: "string", pattern: INDEX_NAME_RE.source },
      description: "Search only these indexes. Default: every index.",
    },
  },
};

function byName(a: string, b: string): number {
  if (a === b) {
    return 0;
  }

  return a < b ? -1 : 1;
}

/** Reciprocal rank fusion over every ranking one half produced. */
class Fusion {
  private readonly byChunk = new Map<string, Fused>();

  public add(ranking: readonly Ranked[], half: Half): void {
    ranking.forEach(({ index, hit }, rank) => {
      const key = `${index}\u0000${hit.id}`;
      const entry = this.byChunk.get(key) ?? { index, hit, score: 0, matched: new Set<Half>() };
      entry.score += 1 / (RRF_K + rank + 1);
      entry.matched.add(half);
      this.byChunk.set(key, entry);
    });
  }

  /**
   * Best first. A tie falls to the index name, by code point and never by
   * locale, then to the chunk id, so every rerun ranks alike.
   */
  public top(k: number): SearchHit[] {
    return [...this.byChunk.values()]
      .sort((a, b) => b.score - a.score || byName(a.index, b.index) || a.hit.id - b.hit.id)
      .slice(0, k)
      .map((entry) => ({
        index: entry.index,
        path: entry.hit.path,
        chunk: entry.hit.ord,
        matched: [Half.Words, Half.Meaning].filter((half) => entry.matched.has(half)),
        score: Math.round(entry.score * 10000) / 10000,
        snippet: snippet(entry.hit.text),
      }));
  }
}

function snippet(chunk: string): string {
  const flat = chunk.replace(/\s+/g, " ").trim();

  return flat.length <= SNIPPET_CHARS ? flat : `${flat.slice(0, SNIPPET_CHARS)}…`;
}

function oneLine(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);

  return message.replace(/[\x00-\x1f\x7f]+/g, " ").trim().slice(0, MAX_ERROR_CHARS);
}

/** The query as an FTS5 expression: each distinct word quoted, any may match. */
export function ftsMatch(query: string): string | null {
  const words = [...new Set(query.toLowerCase().match(WORD_RE) ?? [])].slice(0, MAX_QUERY_WORDS);
  if (words.length === 0) {
    return null;
  }

  return words.map((word) => `"${word}"`).join(" OR ");
}

/** Why this index cannot answer with vectors whatever `embed` says, or null. */
function storeRefusal(meta: StoreMeta): string | null {
  if (meta.model === null) {
    return "the index recorded no embedding model";
  }

  if (!meta.plainVectors) {
    return "the index has no chunks_emb table yet";
  }

  if (meta.dims === null) {
    return "the index recorded no vector dimension";
  }

  return null;
}

/**
 * Why this index's vectors do not compare with the query's, or null.
 *
 * The model is the rule itself: vectors from another model would not fail,
 * they would rank the wrong chunks first and look like an answer.
 */
function queryRefusal(meta: StoreMeta, vector: readonly number[], model: string): string | null {
  if (meta.model !== model) {
    return `the index was built with ${meta.model ?? "no model"} and embed serves ${model}`;
  }

  if (meta.dims !== vector.length) {
    return `the index holds ${meta.dims ?? "unrecorded"}-dimension vectors and embed answered ${vector.length}`;
  }

  return null;
}

/** The indexes to read: every one, or the ones named, with a note per unknown name. */
function choose(all: readonly IndexRef[], named: readonly string[] | null, notes: string[]): IndexRef[] {
  if (named === null) {
    return [...all];
  }

  const wanted = new Set(named);
  for (const name of wanted) {
    if (!all.some((index) => index.name === name)) {
      notes.push(`no index named "${name}"`);
    }
  }

  return all.filter((index) => wanted.has(index.name));
}

/** Opens each index and reads its meta. An index that will not open is one note. */
async function openAll(chosen: readonly IndexRef[], notes: string[]): Promise<OpenIndex[]> {
  const open: OpenIndex[] = [];
  for (const ref of chosen) {
    let store: IndexStore | null = null;
    try {
      store = await openIndex(ref);
      open.push({ ref, store, meta: store.meta() });
    } catch (error) {
      store?.close();
      notes.push(`${ref.name}: unreadable: ${oneLine(error)}`);
    }
  }

  return open;
}

/** Nearer first, then the index name, then the chunk id, so a rerun ranks alike. */
function nearer(a: Near, b: Near): number {
  return a.hit.distance - b.hit.distance || byName(a.index, b.index) || a.hit.id - b.hit.id;
}

/**
 * One index's words ranking into the fusion, and its vector candidates back
 * to the caller, which ranks them against every other index's. A failure is
 * one note, never the search. `query` is null when no index could use a
 * vector, so none was asked.
 */
function searchOne(
  one: OpenIndex,
  match: string | null,
  query: EmbedOutcome | null,
  fusion: Fusion,
  notes: string[],
): Near[] {
  const index = one.ref.name;
  try {
    if (match !== null) {
      fusion.add(
        one.store.lexical(match, CANDIDATES).map((hit) => ({ index, hit })),
        Half.Words,
      );
    }

    const ready = query !== null && query.state === EmbedState.Ready ? query : null;
    const refusal =
      storeRefusal(one.meta) ?? (ready === null ? null : queryRefusal(one.meta, ready.vector, ready.model));
    if (refusal !== null) {
      notes.push(`${index}: vector half refused: ${refusal}. Words only until the index is rebuilt`);
      return [];
    }

    if (ready === null) {
      return [];
    }

    const nearest = one.store.nearest(ready.vector, CANDIDATES);
    if (nearest.skipped > 0) {
      notes.push(`${index}: ${nearest.skipped} stored vectors of the wrong length were skipped`);
    }

    return nearest.hits.map((hit) => ({ index, hit }));
  } catch (error) {
    notes.push(`${index}: unreadable: ${oneLine(error)}`);

    return [];
  }
}

/** The search, without the tool around it. */
export async function searchIndexes(
  root: string,
  request: SearchRequest,
  embedder: QueryEmbedder,
): Promise<SearchAnswer> {
  const notes: string[] = [];
  const open = await openAll(choose(listIndexes(root), request.indexes, notes), notes);

  try {
    if (open.length === 0) {
      return { hits: [], notes: [...notes, "no index could be read"], searched: 0 };
    }

    // One embed per search, and none when no index could use the vector: a
    // PEP call is audited and counts against the family's rate limit.
    let query: EmbedOutcome | null = null;
    if (open.some((one) => storeRefusal(one.meta) === null)) {
      query = await embedder.embed(request.query);
    }
    if (query !== null && query.state === EmbedState.Unavailable) {
      notes.unshift(`vector half unavailable: ${query.why}. Words only`);
    }

    const match = ftsMatch(request.query);
    if (match === null) {
      notes.push("the query holds no word the text index can match");
    }

    const fusion = new Fusion();
    const near: Near[] = [];
    for (const one of open) {
      near.push(...searchOne(one, match, query, fusion, notes));
    }
    fusion.add(near.sort(nearer).slice(0, CANDIDATES), Half.Meaning);

    return { hits: fusion.top(request.k), notes, searched: open.length };
  } finally {
    for (const one of open) {
      one.store.close();
    }
  }
}

/** What the model reads, before the untrusted frame goes around it. */
export function render(request: SearchRequest, answer: SearchAnswer): string {
  const where = answer.searched === 1 ? "1 index" : `${answer.searched} indexes`;
  const lines: string[] = [];

  if (answer.hits.length === 0) {
    lines.push(`No match for "${request.query}" in ${where}.`);
  } else {
    lines.push(`Results for "${request.query}" from ${where}:`);
  }

  answer.hits.forEach((hit, position) => {
    lines.push("");
    lines.push(`${position + 1}. ${hit.path}`);
    lines.push(`   index ${hit.index}, chunk ${hit.chunk}, matched: ${hit.matched.join(" + ")}`);
    lines.push(`   ${hit.snippet}`);
  });

  if (answer.notes.length > 0) {
    lines.push("");
    lines.push("Notes:");
    for (const note of answer.notes) {
      lines.push(`- ${note}.`);
    }
  }

  return lines.join("\n");
}

/** pi validated the arguments against `PARAMETERS`. This only fills the defaults. */
function readRequest(params: unknown): SearchRequest {
  const raw = typeof params === "object" && params !== null ? (params as Record<string, unknown>) : {};
  const query = typeof raw["query"] === "string" ? raw["query"].trim() : "";
  const k = typeof raw["k"] === "number" && Number.isInteger(raw["k"]) ? raw["k"] : DEFAULT_RESULTS;
  const named = Array.isArray(raw["indexes"])
    ? raw["indexes"].filter((name): name is string => typeof name === "string")
    : null;

  return { query, k: Math.min(Math.max(k, 1), MAX_RESULTS), indexes: named };
}

function describe(indexes: readonly string[]): string {
  const known = indexes.length === 0 ? "none yet" : indexes.join(", ");

  return (
    "Search the corpus indexes this agent can read, by words and by meaning. " +
    "Returns file paths, each with the passage that matched. Read the file for full context. " +
    `Indexes: ${known}.`
  );
}

/**
 * The pi tool. `indexes` names what the root held at process start, for the
 * description only: every call lists the root again.
 */
export function searchTool(root: string, indexes: readonly string[], embedder: QueryEmbedder): PiToolDefinition {
  return {
    name: SEARCH_TOOL,
    label: SEARCH_TOOL,
    description: describe(indexes),
    parameters: PARAMETERS,
    execute: async (_toolCallId: string, params: unknown): Promise<PiToolResult> => {
      const request = readRequest(params);
      const answer = await searchIndexes(root, request, embedder);

      return {
        content: [{ type: "text", text: wrapUntrusted(SEARCH_TOOL, render(request, answer)) }],
        details: answer,
      };
    },
  };
}
