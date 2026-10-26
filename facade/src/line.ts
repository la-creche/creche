// One line of stdout: a strict JSON text under the line cap.
//
//   value ──► encodeLine ──► { line }       the text and its LF
//                        └─► { refusal }    not strict, or over the cap

import { MAX_LINE_BYTES } from "./vectors.js";
import { Rule, stringifyStrict } from "./strict-json.js";

/** The largest free-text line on stderr, in bytes. */
export const MAX_LOG_BYTES = 4096;

export type LineRefusal =
  | { readonly reason: "not_strict"; readonly rule: Rule }
  | { readonly reason: "over_cap"; readonly bytes: number };

export type Encoded =
  | { readonly ok: true; readonly line: string }
  | { readonly ok: false; readonly refusal: LineRefusal };

export function byteLength(text: string): number {
  return Buffer.byteLength(text, "utf8");
}

/** A UTF-8 byte of the form `10xxxxxx` continues a character. */
const CONTINUATION_MASK = 0xc0;
const CONTINUATION_BITS = 0x80;

/**
 * The longest start of `text` that takes at most `maxBytes` of UTF-8. A
 * port of the playpen's `cutToBytes`. The cut is at a byte that starts a
 * character, so the result holds no half of a character.
 */
export function cutToBytes(text: string, maxBytes: number): string {
  if (byteLength(text) <= maxBytes) {
    return text;
  }

  const bytes = Buffer.from(text, "utf8");
  let end = maxBytes;
  while (end > 0 && ((bytes[end] ?? 0) & CONTINUATION_MASK) === CONTINUATION_BITS) {
    end -= 1;
  }

  return bytes.subarray(0, end).toString("utf8");
}

export function encodeLine(value: unknown, maxBytes: number = MAX_LINE_BYTES): Encoded {
  const strict = stringifyStrict(value);
  if (!strict.ok) {
    return { ok: false, refusal: { reason: "not_strict", rule: strict.rule } };
  }

  const bytes = byteLength(strict.text);
  if (bytes > maxBytes) {
    return { ok: false, refusal: { reason: "over_cap", bytes } };
  }

  return { ok: true, line: `${strict.text}\n` };
}

export function describeRefusal(refusal: LineRefusal): string {
  return refusal.reason === "not_strict"
    ? `the line is not strict JSON (${refusal.rule})`
    : `the line is ${refusal.bytes} bytes, over the ${MAX_LINE_BYTES} cap`;
}
