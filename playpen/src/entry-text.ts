// Contract 03 §5.8. One pi entry becomes one `{id, role, text}` for the host.
//
// pi's rpc doc fixes `id` and `message.role` and says nothing about where the
// text sits, so this file reads the three shapes an assistant SDK uses and
// answers "" for anything else:
//
//   message.content = "hello"                          <- a plain string
//   message.content = [{type:"text", text:"hello"}]    <- content blocks
//   message.text    = "hello"                          <- a flat field
//
// Losing text is the conservative failure. Inventing it would put words in
// the operator's Open WebUI transcript that they never typed (contract 02 §10.5).

import { MAX_ENTRY_BYTES } from "./constants.js";
import type { PiEntry, SessionEntry } from "./protocol.js";

const BLOCK_SEPARATOR = "\n";

/** An entry with no usable id is not a cursor, so it is not an entry. */
export function readEntry(raw: PiEntry): SessionEntry | null {
  if (typeof raw.id !== "string" || raw.id === "") {
    return null;
  }

  const role = typeof raw.message?.role === "string" ? raw.message.role : "";

  return { id: raw.id, role, text: cap(readText(raw)) };
}

/** §8. The cap is on bytes, and a cut must not split a UTF-8 sequence. */
export function cap(text: string): string {
  const bytes = Buffer.from(text, "utf8");
  if (bytes.byteLength <= MAX_ENTRY_BYTES) {
    return text;
  }

  // `toString` on a cut buffer replaces a split sequence with U+FFFD rather
  // than throwing, so the answer stays valid JSON either way.
  return bytes.subarray(0, MAX_ENTRY_BYTES).toString("utf8");
}

function readText(raw: PiEntry): string {
  const content = raw.message?.content;
  if (typeof content === "string") {
    return content;
  }

  if (Array.isArray(content)) {
    return readBlocks(content);
  }

  return typeof raw.message?.text === "string" ? raw.message.text : "";
}

/** Text blocks joined, and every other block kind dropped. */
function readBlocks(blocks: readonly unknown[]): string {
  const parts: string[] = [];

  for (const block of blocks) {
    if (typeof block === "string") {
      parts.push(block);
      continue;
    }

    const text = asRecord(block)?.["text"];
    if (typeof text === "string") {
      parts.push(text);
    }
  }

  return parts.join(BLOCK_SEPARATOR);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }

  return value as Record<string, unknown>;
}
