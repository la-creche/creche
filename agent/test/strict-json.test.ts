// The strict reader of a wire text (decision CH22 rule 1). JSON.parse holds
// the grammar, and `parseStrict` refuses what JSON.parse takes but a strict
// reader does not. `strict-json-vectors.test.ts` holds the words that
// Python `strictjson` shares. This file holds the rules of JavaScript alone.

import { describe, expect, it } from "vitest";

import { DEPTH_MAX, parseStrict, ReadRule } from "../src/strict-json.js";

const CAP = 1 << 20;
const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);
const nest = (levels: number): string => "[".repeat(levels) + "]".repeat(levels);

describe("parseStrict", () => {
  it.each([
    [nest(DEPTH_MAX + 1), ReadRule.TooDeep],
    ['{"a":1,"\\u0061":2}', ReadRule.DuplicateKey],
    ['[{"a":{"b":1,"b":2}}]', ReadRule.DuplicateKey],
    ['{"a":1,"a":2} x', ReadRule.DuplicateKey],
    ['{"\\udc00":1}', ReadRule.LoneSurrogate],
    ["18446744073709551616", ReadRule.IntegerRange],
    ["-9223372036854775809", ReadRule.IntegerRange],
    ["18446744073709551615", ReadRule.UnsafeInteger],
    ["9007199254740992", ReadRule.UnsafeInteger],
    ["-9007199254740992", ReadRule.UnsafeInteger],
    ["1e400", ReadRule.FloatRange],
    ["1.0", ReadRule.IntegralFloat],
    ["1e3", ReadRule.IntegralFloat],
    ["1e-999", ReadRule.IntegralFloat],
    ["1.0 x", ReadRule.IntegralFloat],
    ["-0", ReadRule.NegativeZero],
  ])("refuses %j as %s", (text, rule) => {
    expect(parseStrict(bytes(text), CAP)).toEqual({ ok: false, rule });
  });

  it("refuses a text over the cap before it reads it", () => {
    expect(parseStrict(bytes("[1] "), 3)).toEqual({ ok: false, rule: ReadRule.TooLarge });
    expect(parseStrict(bytes("[1]"), 3)).toEqual({ ok: true, value: [1] });
  });

  it.each([
    "9007199254740991",
    "-9007199254740991",
    "0",
    "1.5",
    nest(DEPTH_MAX),
    '{"a":{"a":1},"b":[{"a":2}]}',
    '{"a":"a"}',
    '{"a":"b","b":1}',
    '{"a":"x\\"y","b":"{\\"a\\":1,\\"a\\":2}"}',
    '"\\ud83d\\ude00"',
  ])("reads %j as JSON.parse reads it", (text) => {
    expect(parseStrict(bytes(text), CAP)).toStrictEqual({ ok: true, value: JSON.parse(text) });
  });
});
