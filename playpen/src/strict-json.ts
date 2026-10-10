// Strict JSON, for each line that the playpen writes on the channel.
//
//   value ──► checkValue ──► null             the text of the value is strict
//                        └─► Rule             the rule that the text breaks
//
//   value ──► stringifyStrict ──► { text }    one strict JSON text
//                             └─► { rule }    no text
//
// A strict text has one value for each reader. `rust/AGENTS.md`, "JSON",
// holds the rules, and the module `json` of the crate `creche-contracts` is
// their one source. This file holds the rules that a value of JavaScript can
// break when `JSON.stringify` writes it:
//
//   1. A text or a key holds one half of a surrogate pair with no partner.
//   2. Arrays and objects nest deeper than `DEPTH_MAX` levels.
//   3. An integer token is outside the range of 64 bits.
//   4. The value has no JSON text.
//
// `checkValue` names rule 1, 2 or 4. `stringifyStrict` writes a number of
// rule 3 with an exponent, so that rule refuses no value.
//
// The file imports nothing. Give each function a value that `JSON.parse`
// made, or a message that the playpen made from such values. A value with a
// `toJSON` method is outside the two functions: `JSON.stringify` writes what
// that method gives, and `checkValue` reads the members of the value.

declare global {
  interface String {
    isWellFormed(): boolean;
    toWellFormed(): string;
  }

  interface JSON {
    /** A value that `JSON.stringify` writes as the characters of `text`. */
    rawJSON(text: string): unknown;
  }
}

/** The deepest nesting of arrays and objects in a strict text. */
export const DEPTH_MAX = 64;

/**
 * The rule that a value breaks. Each word is the word of the same rule in
 * the `Rule` enum of the Rust module, which is the closed set that a host
 * reads.
 */
export enum Rule {
  /** `JSON.stringify` gives no text for the value. */
  Syntax = "syntax",
  /** Arrays and objects nest deeper than the limit. */
  TooDeep = "too_deep",
  /** A text or a key holds one half of a surrogate pair with no partner. */
  LoneSurrogate = "lone_surrogate",
}

/** What `stringifyStrict` gives: one strict text, or the rule that refuses it. */
export type StrictText =
  | { readonly ok: true; readonly text: string }
  | { readonly ok: false; readonly rule: Rule };

/** 2^64. The largest integer token of a strict text is one below it. */
const PAST_UNSIGNED = 2 ** 64;

/** -2^63, the least integer token of a strict text. */
const LEAST_SIGNED = -(2 ** 63);

/**
 * True when `JSON.stringify` writes `value` as an integer token outside the
 * range of 64 bits.
 *
 * `JSON.stringify` writes the shortest digits that give the number back. For
 * -2^63 those digits are -9223372036854776000, which is below the range, so
 * the lower bound is a part of the answer. Each number between the two
 * bounds has digits inside the range. A number of 10^21 or more already has
 * an exponent, and `toExponential` gives it the same characters.
 */
function needsExponent(value: number): boolean {
  return Number.isInteger(value) && (value >= PAST_UNSIGNED || value <= LEAST_SIGNED);
}

/** `JSON.stringify` gives no text for a value of one of these kinds. */
function hasNoText(value: unknown): boolean {
  return typeof value === "undefined" || typeof value === "function" || typeof value === "symbol";
}

interface Scan {
  /** The first rule that the text of the value breaks, or null. */
  readonly rule: Rule | null;
  /** True when a number of the value needs an exponent. */
  readonly exponent: boolean;
}

/**
 * Reads each text, each key and each number of `value`, in the order of its
 * JSON text.
 *
 * It keeps the values that wait on a stack of its own and calls no function
 * for a level, so the depth of the input cannot exhaust the call stack.
 * `levels` holds, for each value that waits, the level that the value opens
 * when it is an array or an object.
 */
function scan(value: unknown, depthMax: number): Scan {
  if (hasNoText(value)) {
    return { rule: Rule.Syntax, exponent: false };
  }

  const values: unknown[] = [value];
  const levels: number[] = [1];
  let exponent = false;

  for (;;) {
    const level = levels.pop();
    if (level === undefined) {
      return { rule: null, exponent };
    }

    const item = values.pop();
    if (typeof item === "string") {
      if (!item.isWellFormed()) {
        return { rule: Rule.LoneSurrogate, exponent };
      }

      continue;
    }

    if (typeof item === "number") {
      exponent ||= needsExponent(item);
      continue;
    }

    if (typeof item === "bigint") {
      return { rule: Rule.Syntax, exponent };
    }

    if (typeof item !== "object" || item === null) {
      continue;
    }

    if (level > depthMax) {
      return { rule: Rule.TooDeep, exponent };
    }

    // The last member goes on the stack first, so the first one comes off
    // first. A key comes off before its value.
    if (Array.isArray(item)) {
      for (let at = item.length - 1; at >= 0; at -= 1) {
        values.push(item[at]);
        levels.push(level + 1);
      }

      continue;
    }

    const members = Object.entries(item);
    for (let at = members.length - 1; at >= 0; at -= 1) {
      const member = members[at];
      if (member === undefined) {
        continue;
      }

      values.push(member[1], member[0]);
      levels.push(level + 1, level + 1);
    }
  }
}

/**
 * The first rule that the JSON text of `value` breaks, or null when the text
 * is strict. "First" is the order of the characters of that text.
 *
 * `depthMax` is the count of levels that the value can nest. A caller that
 * puts the value inside a line gives a lower count than `DEPTH_MAX`.
 */
export function checkValue(value: unknown, depthMax: number = DEPTH_MAX): Rule | null {
  return scan(value, depthMax).rule;
}

/** The second argument of `JSON.stringify`: a number of rule 3 gets an exponent. */
function withExponent(_key: string, value: unknown): unknown {
  if (typeof value !== "number" || !needsExponent(value)) {
    return value;
  }

  return JSON.rawJSON(value.toExponential());
}

/**
 * The strict JSON text of `value`, or the rule that the value breaks.
 *
 * The text is the text of `JSON.stringify`, with one difference: an integer
 * outside the range of 64 bits has an exponent. The token is then a float
 * with the same value. A number that is not finite is `null`, as
 * `JSON.stringify` writes it.
 */
export function stringifyStrict(value: unknown): StrictText {
  const { rule, exponent } = scan(value, DEPTH_MAX);
  if (rule !== null) {
    return { ok: false, rule };
  }

  const text = exponent ? JSON.stringify(value, withExponent) : JSON.stringify(value);

  return { ok: true, text };
}
