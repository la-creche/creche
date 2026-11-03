// The shared wire vectors (decision CH22, wire/AGENTS.md). Each case says
// what the Rust type does with a JSON text. `read` must refuse the same
// texts, and must give the value of `written` for each other text. Never
// change a case to make this test pass: a difference is a defect in
// TypeScript.
//
// The test compares values, not bytes. TypeScript has no wire writer yet,
// and `JSON.stringify` keeps the key order of the text that `read` read,
// where Rust writes the fields of a struct in alphabetical order.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { ReadRule } from "../src/strict-json.js";
import { wire } from "../src/wire.js";
import { NOT_VALID, read, validator } from "../src/wire-read.js";
import type { WireName } from "../src/wire-read.js";

const VECTOR_FILE = new URL("../../rust/crates/wire/vectors/wire.json", import.meta.url);

interface Case {
  readonly json: string;
  readonly written: string | null;
  readonly note: string;
}

const vectors = JSON.parse(readFileSync(VECTOR_FILE, "utf8")) as Record<string, Case[]>;
const names = Object.keys(wire.$defs) as WireName[];

/** A cap past each case. The cap is a rule of a surface, and no vector has one. */
const CAP = 1 << 20;
const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);

describe("wire vectors", () => {
  it("cover each wire type, and name no other", () => {
    expect(Object.keys(vectors).sort()).toEqual([...names].sort());
  });

  it.each(names)("compile a validator for %s", (name) => {
    expect(validator(name).IsAccelerated()).toBe(true);
  });

  it("name the rule of a refusal", () => {
    expect(read("UnixMillis", bytes("1.0"), { cap: CAP })).toEqual({ ok: false, rule: ReadRule.IntegralFloat });
    expect(read("UnixMillis", bytes("-1"), { cap: CAP })).toEqual({ ok: false, rule: NOT_VALID });
    expect(read("UnixMillis", bytes("12"), { cap: 1 })).toEqual({ ok: false, rule: ReadRule.TooLarge });
  });

  it.each(names)("hold read of %s to the Rust type", (name) => {
    for (const { json, written, note } of vectors[name] ?? []) {
      const result = read(name, bytes(json), { cap: CAP });
      const message = `${name}: ${json} (${note})`;

      expect(result.ok, message).toBe(written !== null);
      if (result.ok && written !== null) {
        expect(result.value, message).toStrictEqual(JSON.parse(written));
      }
    }
  });
});
