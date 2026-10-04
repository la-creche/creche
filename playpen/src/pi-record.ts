// Invariant 12: the agent process is untrusted, so every line pi writes is a
// claim. This file is what a parsed pi line passes before anything reads it.
//
//   line ──► JSON.parse ──► boundRecord ──► event      → onEvent
//                                       └─► response   → readResponse → caller
//
// Two things a parsed line can still do to the code that reads it:
//
//   1. Nest deeper than `JSON.stringify` can write. `JSON.parse` reads any
//      depth, and `JSON.stringify` overflows the stack at about 6000 levels on
//      Node 22 and Node 24. The playpen writes every event again, so a
//      20 KiB line ended the process, and with it every session of the family.
//   2. Carry a field of the wrong type. A cast to `PiResponse` checks nothing,
//      and a caller that trusted it called `slice` on an object.

import { MAX_PI_NESTING } from "./constants.js";
import { byteLength } from "./framing.js";
import type { PiEntry, PiResponse } from "./protocol.js";

type PiRecord = Record<string, unknown>;

function isRecord(value: unknown): value is PiRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * True when `value` holds a container more than `limit` levels down.
 *
 * It walks one level at a time with no recursion, because the input is
 * exactly what would overflow a recursive walk.
 */
function nestsDeeperThan(value: unknown, limit: number): boolean {
  let level: object[] = typeof value === "object" && value !== null ? [value] : [];

  for (let depth = 1; level.length > 0; depth += 1) {
    if (depth > limit) {
      return true;
    }

    const next: object[] = [];
    for (const container of level) {
      for (const child of Array.isArray(container) ? container : Object.values(container)) {
        if (typeof child === "object" && child !== null) {
          next.push(child);
        }
      }
    }
    level = next;
  }

  return false;
}

/**
 * Returns the record unchanged, or cut to its scalar fields when it nests
 * deeper than `MAX_PI_NESTING`. `line` is the text the record was parsed from.
 *
 * The cut is the shape contract 03 §8 gives an oversize event: `type` and the
 * scalars stay, `truncated` and `original_bytes` mark it. A cut line keeps
 * its place, so a deep `agent_settled` still settles its turn and a deep
 * `response` still answers its command.
 *
 * CONTRACT-QUESTION: contract 03 §8 cuts a wrapped event by size and says
 * nothing about nesting. The reading taken is that a line the playpen cannot
 * write again is cut like one that is too large, and never dropped. A
 * contract that named a depth would only change `MAX_PI_NESTING`.
 */
export function boundRecord(record: PiRecord, line: string): PiRecord {
  if (!nestsDeeperThan(record, MAX_PI_NESTING)) {
    return record;
  }

  const cut: PiRecord = {};
  for (const [key, value] of Object.entries(record)) {
    if (typeof value !== "object" || value === null) {
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
