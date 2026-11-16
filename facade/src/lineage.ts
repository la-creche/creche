// The lineage of the one session: which Flue instance holds its
// conversation now, and the messages that a rewind kept before it. Flue
// 2.3.0 cannot cut a conversation, so each rewind starts a new instance
// (`src/rewind.ts`), and this file on the session disk records it:
//
//   no file      ──► generation 0: the instance is the session id, no seed
//   a rewind     ──► generation n + 1, the kept messages ──► write a temp
//                    file, then rename it over the file
//   facade start ──► read the file ──► the instance and the seed of the
//                    last rewind. A file that does not read refuses the
//                    start (fail closed).
//
// The file is outside the workspace of the sandbox tools, next to
// `flue.db`. The old instances stay in `flue.db`, and the facade shows
// only the last one.

import { readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

import { parseStrict, stringifyStrict } from "agent/strict-json";

import type { Kept, KeptOutcome, KeptPart } from "./kept.js";

/** The name of the file, in the directory of `flue.db`. */
export const LINEAGE_NAME = "lineage.json";

/** The most bytes of the file. A rewind that keeps more is refused. */
export const MAX_LINEAGE_BYTES = 16 * 1024 * 1024;

/** The format of the file. */
const LINEAGE_VERSION = 1;

export interface Lineage {
  /** 0 before the first rewind. Each rewind adds 1. */
  readonly generation: number;
  /** The messages before the first message of the instance. */
  readonly seed: readonly Kept[];
}

export const FIRST_LINEAGE: Lineage = { generation: 0, seed: [] };

/**
 * The Flue instance of a generation. Generation 0 is the session id, as
 * before any rewind. A later one adds `-r<n>`: Flue keeps each instance
 * apart by its id.
 */
export function instanceOf(sessionId: string, generation: number): string {
  return generation === 0 ? sessionId : `${sessionId}-r${String(generation)}`;
}

/** The path of the file beside `flue.db` at `store`. */
export function lineagePath(store: string): string {
  return join(dirname(store), LINEAGE_NAME);
}

export type LineageRead = { readonly ok: true; readonly lineage: Lineage } | { readonly ok: false; readonly detail: string };

type Fields = Record<string, unknown>;

function fieldsOf(value: unknown): Fields | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Fields) : null;
}

function outcomeOf(value: unknown): KeptOutcome | null {
  const fields = fieldsOf(value);
  switch (fields?.["state"]) {
    case "pending":
      return { state: "pending" };
    case "done":
      return { state: "done", output: fields["output"] };
    case "failed":
      return typeof fields["errorText"] === "string" ? { state: "failed", errorText: fields["errorText"] } : null;
    default:
      return null;
  }
}

function partOf(value: unknown): KeptPart | null {
  const fields = fieldsOf(value);
  const type = fields?.["type"];
  if (fields === null || (type !== "text" && type !== "reasoning" && type !== "tool")) {
    return null;
  }

  if (type !== "tool") {
    return typeof fields["text"] === "string" ? { type, text: fields["text"] } : null;
  }

  const { toolCallId, toolName } = fields;
  const outcome = outcomeOf(fields["outcome"]);
  if (typeof toolCallId !== "string" || typeof toolName !== "string" || outcome === null) {
    return null;
  }

  return { type, toolCallId, toolName, input: fields["input"], outcome };
}

function keptOf(value: unknown): Kept | null {
  const fields = fieldsOf(value);
  const role = fields?.["role"];
  const parts = fields?.["parts"];
  if (fields === null || typeof fields["id"] !== "string" || (role !== "user" && role !== "assistant") || !Array.isArray(parts)) {
    return null;
  }

  const read = parts.map(partOf);
  if (read.some((part) => part === null)) {
    return null;
  }

  return { id: fields["id"], role, parts: read as KeptPart[] };
}

/** The lineage in the text `data`, or why it is not one. */
export function parseLineage(data: Uint8Array): LineageRead {
  const parsed = parseStrict(data, MAX_LINEAGE_BYTES);
  if (!parsed.ok) {
    return { ok: false, detail: `${LINEAGE_NAME} is not strict JSON (${parsed.rule})` };
  }

  const fields = fieldsOf(parsed.value);
  const generation = fields?.["generation"];
  const seed = fields?.["seed"];
  if (fields?.["v"] !== LINEAGE_VERSION || !Number.isSafeInteger(generation) || (generation as number) < 1 || !Array.isArray(seed)) {
    return { ok: false, detail: `${LINEAGE_NAME} is not a lineage of version ${String(LINEAGE_VERSION)}` };
  }

  const kept = seed.map(keptOf);
  if (kept.some((message) => message === null)) {
    return { ok: false, detail: `${LINEAGE_NAME} holds a seed message that does not read` };
  }

  return { ok: true, lineage: { generation: generation as number, seed: kept as Kept[] } };
}

function isMissing(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "ENOENT";
}

/** The lineage at `path`. No file is generation 0. */
export async function readLineage(path: string): Promise<LineageRead> {
  let data: Uint8Array;
  try {
    data = await readFile(path);
  } catch (error) {
    if (isMissing(error)) {
      return { ok: true, lineage: FIRST_LINEAGE };
    }

    return { ok: false, detail: `${LINEAGE_NAME} does not read: ${error instanceof Error ? error.message : String(error)}` };
  }

  return parseLineage(data);
}

export type LineageWrite = { readonly ok: true } | { readonly ok: false; readonly detail: string };

/**
 * Writes `lineage` to `path`. The rename is atomic, so a crash leaves the
 * old file or the new one, never a part.
 */
export async function writeLineage(path: string, lineage: Lineage): Promise<LineageWrite> {
  const text = stringifyStrict({ v: LINEAGE_VERSION, generation: lineage.generation, seed: lineage.seed });
  if (!text.ok) {
    return { ok: false, detail: `the kept messages are not strict JSON (${text.rule})` };
  }

  // The start reads the file back. A text that the read refuses, such as
  // an integer past 2^53 - 1 in a tool value, is never written.
  const back = parseLineage(Buffer.from(text.text));
  if (!back.ok) {
    return { ok: false, detail: `the kept messages do not read back: ${back.detail}` };
  }

  const temp = `${path}.tmp`;
  try {
    await writeFile(temp, text.text);
    await rename(temp, path);
  } catch (error) {
    return { ok: false, detail: `${LINEAGE_NAME} does not write: ${error instanceof Error ? error.message : String(error)}` };
  }

  return { ok: true };
}
