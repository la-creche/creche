// Strict JSON, for each line that the facade writes on stdout and for each
// wire text that it reads (decision CH22 rule 1). The module doc of
// `noticeboard/src/noticeboard/strictjson.py` has the rules.
//
//   value ──► checkValue ──► null             the text of the value is strict
//                        └─► Rule             the rule that the text breaks
//
//   value ──► stringifyStrict ──► { text }    one strict JSON text
//                             └─► { rule }    no text
//
//   text  ──► parseStrict ──► { value }       the value of a strict text
//                         └─► { rule }        the ReadRule the text breaks
//
// A strict text has one value for each reader. This file holds the rules
// that a value of JavaScript can break when `JSON.stringify` writes it:
//
//   1. A text or a key holds one half of a surrogate pair with no partner.
//   2. Arrays and objects nest deeper than `DEPTH_MAX` levels.
//   3. An integer token is outside the range of 64 bits.
//   4. The value has no JSON text.
//
// `checkValue` names rule 1, 2 or 4. `stringifyStrict` writes a number of
// rule 3 with an exponent, so that rule refuses no value. `parseStrict` has
// rules of its own, at `ReadRule`.
//
// The file imports nothing. A value with a `toJSON` method is outside the
// two functions: `JSON.stringify` writes what that method gives, and
// `checkValue` reads the members of the value.

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

/** The rule that a value breaks. The words are the playpen's words. */
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

/**
 * The rule that a text breaks when `parseStrict` reads it. The first six
 * words are words of the Rust enum `Rule` of `creche_contracts::json`.
 * `IntegerRange` has a lower bound here than there. A JavaScript number
 * cannot tell `1.0` from `1`, or `-0` from `0`, so the last two rules refuse
 * at the read what the Rust and Python types refuse as a float for an
 * integer. No other reader has their words.
 */
export enum ReadRule {
  /** `JSON.parse` refuses the text. */
  Syntax = "syntax",
  /** Arrays and objects nest deeper than `DEPTH_MAX` levels. */
  TooDeep = "too_deep",
  /** One object holds a key two times, after the decode of each escape. */
  DuplicateKey = "duplicate_key",
  /** A text or a key holds one half of a surrogate pair with no partner. */
  LoneSurrogate = "lone_surrogate",
  /** An integer token is past 2^53 - 1 from zero. JavaScript reads no larger integer exactly. */
  IntegerRange = "integer_range",
  /** A number with a fraction or an exponent is not finite. */
  FloatRange = "float_range",
  /** A number with a fraction or an exponent has an integer value, as `1.0` and `1e3` have. */
  IntegralFloat = "integral_float",
  /** An integer token is `-0`. */
  NegativeZero = "negative_zero",
}

/** What `parseStrict` gives: the value of a strict text, or the rule that refuses it. */
export type StrictValue =
  | { readonly ok: true; readonly value: unknown }
  | { readonly ok: false; readonly rule: ReadRule };

/** A number token: the run of characters that `JSON.parse` read as a number. */
const NUMBER = /[-+.eE0-9]+/y;

/** The rule that the number token `token` breaks, or null. */
function numberRule(token: string): ReadRule | null {
  const value = Number(token);
  if (/^-?[0-9]+$/.test(token)) {
    if (!Number.isSafeInteger(value)) {
      return ReadRule.IntegerRange;
    }

    return Object.is(value, -0) ? ReadRule.NegativeZero : null;
  }

  if (!Number.isFinite(value)) {
    return ReadRule.FloatRange;
  }

  return Number.isInteger(value) ? ReadRule.IntegralFloat : null;
}

/** The index just past the string token that starts at `start`. */
function stringEnd(text: string, start: number): number {
  let at = start + 1;
  while (text[at] !== '"') {
    at += text[at] === "\\" ? 2 : 1;
  }

  return at + 1;
}

/**
 * Reads the tokens of `text`, a text that `JSON.parse` takes, for the rules
 * that `JSON.parse` does not hold. Each open object keeps the set of its
 * keys, and `key` is true where the next string is a key.
 */
function scanText(text: string): ReadRule | null {
  const objects: (Set<string> | null)[] = [];
  let key = false;
  let at = 0;

  while (at < text.length) {
    const char = text[at] ?? "";
    if (char === "{" || char === "[") {
      if (objects.length >= DEPTH_MAX) {
        return ReadRule.TooDeep;
      }

      objects.push(char === "{" ? new Set() : null);
      key = char === "{";
      at += 1;
    } else if (char === "}" || char === "]") {
      objects.pop();
      at += 1;
    } else if (char === ",") {
      key = objects.at(-1) instanceof Set;
      at += 1;
    } else if (char === '"') {
      const end = stringEnd(text, at);
      const decoded = JSON.parse(text.slice(at, end)) as string;
      if (!decoded.isWellFormed()) {
        return ReadRule.LoneSurrogate;
      }

      const keys = objects.at(-1);
      if (key && keys instanceof Set) {
        if (keys.has(decoded)) {
          return ReadRule.DuplicateKey;
        }

        keys.add(decoded);
      }

      key = false;
      at = end;
    } else if (char === "-" || (char >= "0" && char <= "9")) {
      NUMBER.lastIndex = at;
      const token = NUMBER.exec(text)?.[0] ?? char;
      const rule = numberRule(token);
      if (rule !== null) {
        return rule;
      }

      at += token.length;
    } else {
      // White space, `:`, and the letters of `true`, `false` and `null`.
      at += 1;
    }
  }

  return null;
}

/**
 * The value of the strict text `text`, or the first rule that it breaks.
 *
 * `JSON.parse` checks the grammar: RFC 8259, no byte order mark, no `NaN`
 * and nothing after the value. A scan of the tokens then checks the depth,
 * the keys of each object, each text and each number. The caller checks the
 * kind of the value and the size of the text.
 */
export function parseStrict(text: string): StrictValue {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { ok: false, rule: ReadRule.Syntax };
  }

  const rule = scanText(text);

  return rule === null ? { ok: true, value } : { ok: false, rule };
}
