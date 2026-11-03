// The strict reader of a wire text (decision CH22 rule 1). JSON.parse holds
// the grammar, and `parseStrict` refuses what JSON.parse takes but a strict
// reader does not.

import { describe, expect, it } from "vitest";

import { DEPTH_MAX, parseStrict, ReadRule } from "../src/strict-json.js";

const nest = (levels: number): string => "[".repeat(levels) + "]".repeat(levels);

describe("parseStrict", () => {
  it.each([
    ["{} 1", ReadRule.Syntax],
    ["NaN", ReadRule.Syntax],
    ["﻿1", ReadRule.Syntax],
    [nest(DEPTH_MAX + 1), ReadRule.TooDeep],
    ['{"a":1,"a":1}', ReadRule.DuplicateKey],
    ['{"a":1,"\\u0061":2}', ReadRule.DuplicateKey],
    ['[{"a":{"b":1,"b":2}}]', ReadRule.DuplicateKey],
    ['"\\ud800"', ReadRule.LoneSurrogate],
    ['{"\\udc00":1}', ReadRule.LoneSurrogate],
    ["9007199254740992", ReadRule.IntegerRange],
    ["-9007199254740992", ReadRule.IntegerRange],
    ["1e400", ReadRule.FloatRange],
    ["1.0", ReadRule.IntegralFloat],
    ["1e3", ReadRule.IntegralFloat],
    ["1e-999", ReadRule.IntegralFloat],
    ["-0", ReadRule.NegativeZero],
  ])("refuses %j as %s", (text, rule) => {
    expect(parseStrict(text)).toEqual({ ok: false, rule });
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
    expect(parseStrict(text)).toStrictEqual({ ok: true, value: JSON.parse(text) });
  });
});
