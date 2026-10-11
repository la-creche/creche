// The strict JSON rule of a channel line, on its own: `src/strict-json.ts`.
//
// The Node of the image must hold `JSON.rawJSON` and
// `String.prototype.toWellFormed`. The first block fails on a Node that
// lacks one, before a test of the rule reads a wrong text.

import { describe, expect, it } from "vitest";

import { checkValue, DEPTH_MAX, Rule, stringifyStrict } from "../src/strict-json.js";
import { firstBrokenRule } from "./strict-text.js";

/** One half of a surrogate pair, with no partner. */
const HIGH_HALF = "\ud83d";
const LOW_HALF = "\ude00";

/** 2^64: the first integer past the range of 64 bits. */
const PAST_U64 = 18446744073709551616;
/** -2^63: the least integer of the range. */
const I64_MIN = -9223372036854775808;

/** `levels` arrays, each one inside the one before, around `core`. */
function nested(levels: number, core: unknown = 0): unknown {
  let value = core;
  for (let level = 0; level < levels; level += 1) {
    value = [value];
  }

  return value;
}

/** `levels` objects, each one inside the one before. */
function nestedObjects(levels: number): unknown {
  let value: unknown = 0;
  for (let level = 0; level < levels; level += 1) {
    value = { inner: value };
  }

  return value;
}

/** The text of a value that the rule accepts. A refusal fails the test. */
function textOf(value: unknown): string {
  const written = stringifyStrict(value);
  if (!written.ok) {
    throw new Error(`refused: ${written.rule}`);
  }

  return written.text;
}

describe("the Node that runs the playpen", () => {
  it("holds JSON.rawJSON", () => {
    expect(JSON.stringify({ n: JSON.rawJSON("1e+20") })).toBe('{"n":1e+20}');
  });

  it("holds String.prototype.toWellFormed and isWellFormed", () => {
    expect(HIGH_HALF.isWellFormed()).toBe(false);
    expect(`a${HIGH_HALF}b`.toWellFormed()).toBe("a�b");
  });
});

describe("checkValue", () => {
  it("passes each kind of JSON value", () => {
    const value = { text: "a\u{1F600}b", list: [1, -2.5, true, false, null], map: { "": {} } };

    expect(checkValue(value)).toBeNull();
    expect(checkValue("text")).toBeNull();
    expect(checkValue(7)).toBeNull();
    expect(checkValue(null)).toBeNull();
  });

  it("names a lone surrogate in a text, at each place", () => {
    expect(checkValue(HIGH_HALF)).toBe(Rule.LoneSurrogate);
    expect(checkValue(LOW_HALF)).toBe(Rule.LoneSurrogate);
    expect(checkValue({ a: [`x${HIGH_HALF}`] })).toBe(Rule.LoneSurrogate);
    expect(checkValue({ a: { b: `${LOW_HALF}x` } })).toBe(Rule.LoneSurrogate);
    expect(checkValue(`${LOW_HALF}${HIGH_HALF}`)).toBe(Rule.LoneSurrogate);
  });

  it("names a lone surrogate in a key", () => {
    expect(checkValue({ [HIGH_HALF]: 1 })).toBe(Rule.LoneSurrogate);
    expect(checkValue([{ a: { [`k${LOW_HALF}`]: null } }])).toBe(Rule.LoneSurrogate);
  });

  it("passes the two halves of one pair", () => {
    expect(checkValue({ [`${HIGH_HALF}${LOW_HALF}`]: `${HIGH_HALF}${LOW_HALF}` })).toBeNull();
  });

  it("passes 64 levels and names level 65", () => {
    expect(DEPTH_MAX).toBe(64);
    expect(checkValue(nested(64))).toBeNull();
    expect(checkValue(nested(65))).toBe(Rule.TooDeep);
    expect(checkValue(nestedObjects(64))).toBeNull();
    expect(checkValue(nestedObjects(65))).toBe(Rule.TooDeep);
  });

  it("counts arrays and objects only, so a text can be 64 levels down", () => {
    expect(checkValue(nested(64, "text"))).toBeNull();
    expect(checkValue(nested(64, []))).toBe(Rule.TooDeep);
  });

  it("takes a lower limit from its caller", () => {
    expect(checkValue(nested(63), 63)).toBeNull();
    expect(checkValue(nested(64), 63)).toBe(Rule.TooDeep);
  });

  it("reads a value of a million levels with no recursion", () => {
    expect(checkValue(nested(1_000_000))).toBe(Rule.TooDeep);
  });

  it("reads a wide value that is not deep", () => {
    const wide = Array.from({ length: 200_000 }, (_, index) => ({ index, text: "x" }));

    expect(checkValue(wide)).toBeNull();
  });

  it("names the rule that the text of the value breaks first", () => {
    expect(checkValue({ a: nested(65), b: HIGH_HALF })).toBe(Rule.TooDeep);
    expect(checkValue({ a: HIGH_HALF, b: nested(65) })).toBe(Rule.LoneSurrogate);
    expect(checkValue([nested(64), HIGH_HALF])).toBe(Rule.TooDeep);
    expect(checkValue({ [HIGH_HALF]: nested(65) })).toBe(Rule.LoneSurrogate);
  });

  it("names a value that has no JSON text", () => {
    expect(checkValue(undefined)).toBe(Rule.Syntax);
    expect(checkValue(() => 1)).toBe(Rule.Syntax);
    expect(checkValue(Symbol("s"))).toBe(Rule.Syntax);
    expect(checkValue(7n)).toBe(Rule.Syntax);
    expect(checkValue({ a: [7n] })).toBe(Rule.Syntax);
  });

  it("passes a member that JSON.stringify leaves out", () => {
    expect(checkValue({ a: undefined, b: [undefined] })).toBeNull();
    expect(textOf({ a: undefined, b: [undefined] })).toBe('{"b":[null]}');
  });
});

describe("stringifyStrict", () => {
  it("gives the text of JSON.stringify for a value with no wide integer", () => {
    const message = {
      type: "event",
      session: "owui-1",
      turn_seq: 3,
      event: { type: "message_update", delta: "a b c \u{1F600}", n: [0, -0, 1.5, 2 ** 53] },
    };

    expect(textOf(message)).toBe(JSON.stringify(message));
  });

  it("writes an integer past 64 bits with an exponent, and keeps its value", () => {
    expect(textOf({ n: 1e20 })).toBe('{"n":1e+20}');
    expect(textOf([PAST_U64])).toBe("[1.8446744073709552e+19]");
    expect(textOf(-1e20)).toBe("-1e+20");
    expect(textOf({ a: { b: [1, 1e20, "1e20"] } })).toBe('{"a":{"b":[1,1e+20,"1e20"]}}');

    for (const value of [1e20, PAST_U64, -1e20, 2 ** 70, 123456789012345680000]) {
      expect(JSON.parse(textOf(value))).toBe(value);
    }
  });

  it("keeps the digits of the largest integer below 2^64", () => {
    const largest = 18446744073709549568;

    expect(textOf(largest)).toBe("18446744073709550000");
    expect(firstBrokenRule(textOf(largest))).toBeNull();
  });

  it("writes -2^63 with an exponent, because its digits are below the range", () => {
    // `JSON.stringify` rounds the digits of -2^63 to -9223372036854776000.
    expect(JSON.stringify(I64_MIN)).toBe("-9223372036854776000");
    expect(textOf(I64_MIN)).toBe("-9.223372036854776e+18");
    expect(JSON.parse(textOf(I64_MIN))).toBe(I64_MIN);
  });

  it("keeps the digits of the least integer above -2^63", () => {
    const least = -9223372036854774784;

    expect(textOf(least)).toBe("-9223372036854775000");
    expect(firstBrokenRule(textOf(least))).toBeNull();
  });

  it("gives a text that the strict rule accepts for each number", () => {
    const numbers = [
      0,
      1,
      -1,
      2 ** 53,
      2 ** 63,
      -(2 ** 63),
      2 ** 64,
      2 ** 64 - 2048,
      1e19,
      1e20,
      1e21,
      -1e21,
      1.5e300,
      5e-324,
      0.1,
      Number.MAX_VALUE,
      -Number.MAX_VALUE,
      Number.MAX_SAFE_INTEGER,
      Number.MIN_SAFE_INTEGER,
    ];

    for (const value of numbers) {
      const text = textOf([value]);

      expect(firstBrokenRule(text), text).toBeNull();
      expect(JSON.parse(text)).toEqual([value]);
    }
  });

  it("writes a number that is not finite as null, as JSON.stringify does", () => {
    expect(textOf([NaN, Infinity, -Infinity])).toBe("[null,null,null]");
  });

  it("writes -0 as 0, as JSON.stringify does", () => {
    expect(textOf(-0)).toBe("0");
  });

  it("gives the rule and no text for a value that is not strict", () => {
    expect(stringifyStrict({ text: HIGH_HALF })).toEqual({ ok: false, rule: Rule.LoneSurrogate });
    expect(stringifyStrict(nested(65))).toEqual({ ok: false, rule: Rule.TooDeep });
    expect(stringifyStrict(nested(1_000_000))).toEqual({ ok: false, rule: Rule.TooDeep });
    expect(stringifyStrict(undefined)).toEqual({ ok: false, rule: Rule.Syntax });
    expect(stringifyStrict({ n: 7n })).toEqual({ ok: false, rule: Rule.Syntax });
  });

  it("gives the words of the rule set that the host reads", () => {
    expect(Rule.TooDeep).toBe("too_deep");
    expect(Rule.LoneSurrogate).toBe("lone_surrogate");
    expect(Rule.Syntax).toBe("syntax");
  });
});

describe("firstBrokenRule, the judge of these tests", () => {
  it("accepts a strict text", () => {
    const text = '{"a":[1,-2.5,1e+20,"\\ud83d\\ude00","\\\\ud83d"],"b":18446744073709551615}';

    expect(firstBrokenRule(text)).toBeNull();
    expect(firstBrokenRule("-9223372036854775808")).toBeNull();
    expect(firstBrokenRule(`${"[".repeat(64)}${"]".repeat(64)}`)).toBeNull();
  });

  it("refuses an integer outside 64 bits", () => {
    expect(firstBrokenRule("18446744073709551616")).toBe("integer_range");
    expect(firstBrokenRule('{"n":-9223372036854775809}')).toBe("integer_range");
    expect(firstBrokenRule("[100000000000000000000]")).toBe("integer_range");
  });

  it("refuses a lone surrogate, as an escape and as a character", () => {
    expect(firstBrokenRule('"\\ud83d"')).toBe("lone_surrogate");
    expect(firstBrokenRule('"\\ude00\\ud83d"')).toBe("lone_surrogate");
    expect(firstBrokenRule('{"\\ud83dx":1}')).toBe("lone_surrogate");
    expect(firstBrokenRule(`"${HIGH_HALF}"`)).toBe("lone_surrogate");
  });

  it("refuses level 65", () => {
    expect(firstBrokenRule(`${"[".repeat(65)}${"]".repeat(65)}`)).toBe("too_deep");
    expect(firstBrokenRule(`${'{"a":'.repeat(65)}0${"}".repeat(65)}`)).toBe("too_deep");
  });
});
