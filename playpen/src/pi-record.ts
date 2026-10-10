// Invariant 12: the agent process is untrusted, so every line pi writes is a
// claim. This file is what a parsed pi line passes before anything reads it.
//
//   line ──► JSON.parse ──► boundRecord ──► event      → onEvent
//                                       └─► response   → readResponse → caller
//
// Two things a parsed line can still do to the code that reads it:
//
//   1. Have no strict JSON text. `JSON.parse` reads a lone surrogate from an
//      escape, and it reads each depth. The playpen writes each event again,
//      and each line of the channel is strict JSON.
//   2. Carry a field of the wrong type. A cast to `PiResponse` checks nothing,
//      and a caller that trusted it called `slice` on an object.

import { MAX_PI_NESTING } from "./constants.js";
import { byteLength } from "./framing.js";
import type { PiEntry, PiResponse } from "./protocol.js";
import { checkValue } from "./strict-json.js";

type PiRecord = Record<string, unknown>;

function isRecord(value: unknown): value is PiRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** True for a field that a cut record keeps: a scalar, strict with its key. */
function staysInCut(key: string, value: unknown): boolean {
  if (typeof value === "object" && value !== null) {
    return false;
  }

  return checkValue(key) === null && checkValue(value) === null;
}

/**
 * Returns the record unchanged, or cut to its scalar fields. `line` is the
 * text the record was parsed from.
 *
 * A record is cut in two cases: a text or a key of it holds a lone
 * surrogate, or it nests deeper than `MAX_PI_NESTING`. The line that wraps
 * such a record is not strict JSON.
 *
 * The cut is the shape contract 03 §8 gives an oversize event: `type` and the
 * scalars stay, `truncated` and `original_bytes` mark it. A scalar that holds
 * a lone surrogate does not stay, and neither does a field whose key holds
 * one. A cut line keeps its place, so a cut `agent_settled` still settles its
 * turn and a cut `response` still answers its command.
 *
 * CONTRACT-QUESTION: contract 03 §8 cuts a wrapped event by size. It names
 * no depth and no lone surrogate. The reading taken is that a line the
 * playpen cannot write again as strict JSON is cut like one that is too
 * large, and never dropped. The other reading puts U+FFFD in the place of a
 * lone surrogate, which changes a text that §13 rule 5 keeps unchanged. A
 * contract that named a depth would only change `MAX_PI_NESTING`.
 */
export function boundRecord(record: PiRecord, line: string): PiRecord {
  if (checkValue(record, MAX_PI_NESTING) === null) {
    return record;
  }

  const cut: PiRecord = {};
  for (const [key, value] of Object.entries(record)) {
    if (staysInCut(key, value)) {
      cut[key] = value;
    }
  }

  return { ...cut, truncated: true, original_bytes: byteLength(line) };
}

/** A `response` line with every field checked. A wrong type reads as absent. */
export function readResponse(record: PiRecord): PiResponse {
  const id = record["id"];
  const command = record["command"];
  const error = record["error"];
  const data = record["data"];

  return {
    type: "response",
    ...(typeof id === "string" ? { id } : {}),
    ...(typeof command === "string" ? { command } : {}),
    success: record["success"] === true,
    ...(typeof error === "string" ? { error } : {}),
    ...(isRecord(data) ? { data } : {}),
  };
}

/** The entries of a `get_entries` answer. Anything that is not an object is no entry. */
export function entryList(data: PiRecord): PiEntry[] {
  const raw = data["entries"];

  return Array.isArray(raw) ? raw.filter(isRecord) : [];
}
