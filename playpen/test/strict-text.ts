// A judge for the tests of this directory: the three rules of a strict JSON
// text that a writer can break. It reads the characters of a text and never
// a value, so it shares no code with `src/strict-json.ts`.
//
// It is not a parser. Give it only a text that `JSON.parse` reads.

/** The deepest nesting of arrays and objects in a strict text. */
const DEPTH_MAX = 64;

/** The range of an integer token: the least `i64` to the largest `u64`. */
const INTEGER_MIN = -9223372036854775808n;
const INTEGER_MAX = 18446744073709551615n;

const NUMBER = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
const INTEGER = /^-?\d+$/;
const UNIT_ESCAPE = /\\u([0-9a-fA-F]{4})/y;

const HIGH_FIRST = 0xd800;
const LOW_FIRST = 0xdc00;
const LOW_LAST = 0xdfff;

/** The length of a `\u` escape: the two marks and four digits. */
const UNIT_ESCAPE_LENGTH = 6;

enum Half {
  None,
  High,
  Low,
}

function halfOf(unit: number): Half {
  if (unit < HIGH_FIRST || unit > LOW_LAST) {
    return Half.None;
  }

  return unit < LOW_FIRST ? Half.High : Half.Low;
}

interface StringScan {
  /** The index after the closing quote. */
  readonly end: number;
  /** True when an escape of one half has no escape of the other half beside it. */
  readonly lone: boolean;
}

/** Reads one string. `start` is the index of its opening quote. */
function scanString(text: string, start: number): StringScan {
  let lone = false;
  let open = false;
  let at = start + 1;

  while (at < text.length && text[at] !== '"') {
    UNIT_ESCAPE.lastIndex = at;
    const escape = UNIT_ESCAPE.exec(text);
    const half = escape === null ? Half.None : halfOf(Number.parseInt(escape[1] ?? "", 16));

    // A high half is open until the next item, which must be a low half.
    lone ||= open ? half !== Half.Low : half === Half.Low;
    open = half === Half.High;

    if (escape !== null) {
      at += UNIT_ESCAPE_LENGTH;
    } else {
      at += text[at] === "\\" ? 2 : 1;
    }
  }

  return { end: at + 1, lone: lone || open };
}

function inRange(token: string): boolean {
  const value = BigInt(token);

  return value >= INTEGER_MIN && value <= INTEGER_MAX;
}

/**
 * The word of the first rule that `text` breaks, or null.
 *
 * It knows `lone_surrogate`, `too_deep` and `integer_range`. A lone
 * surrogate as a character, with no escape, is named before each other
 * rule.
 */
export function firstBrokenRule(text: string): string | null {
  if (!text.isWellFormed()) {
    return "lone_surrogate";
  }

  let depth = 0;
  let at = 0;

  while (at < text.length) {
    const char = text[at];

    if (char === '"') {
      const scan = scanString(text, at);
      if (scan.lone) {
        return "lone_surrogate";
      }

      at = scan.end;
      continue;
    }

    if (char === "[" || char === "{") {
      depth += 1;
      if (depth > DEPTH_MAX) {
        return "too_deep";
      }
    }

    if (char === "]" || char === "}") {
      depth -= 1;
    }

    NUMBER.lastIndex = at;
    const token = NUMBER.exec(text)?.[0];
    if (token === undefined) {
      at += 1;
      continue;
    }

    if (INTEGER.test(token) && !inRange(token)) {
      return "integer_range";
    }

    at += token.length;
  }

  return null;
}
