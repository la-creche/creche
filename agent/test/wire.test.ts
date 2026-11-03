// The TypeScript copy of the wire schema (decision CH22). The schema test of
// rust/crates/wire writes `src/wire.ts` and fails when it differs, so this
// file checks only what TypeBox makes of it: a validator for each wire type,
// and a static type that follows each `$ref`.

import { readFileSync } from "node:fs";

import type { Static } from "typebox";
import Schema from "typebox/schema";
import { describe, expect, expectTypeOf, it } from "vitest";

import { wire } from "../src/wire.js";

const SCHEMA_FILE = new URL("../../rust/crates/wire/schema/wire.json", import.meta.url);

type Defs = typeof wire.$defs;
type Wire<N extends keyof Defs> = Static<{ readonly $defs: Defs; readonly $ref: `#/$defs/${N}` }>;

const ROW: Wire<"SessionRow"> = {
  created_at: 1_700_000_000_000,
  cwd: "/home/agent",
  family: "chat",
  id: "0190a8f2-5b1c-4e3d-9f2a-1b2c3d4e5f60",
  origin: { auto: { job: "auto-01J9Z8Y7X6W5V4T3S2R1Q0P9N8", trigger: { timer: {} } } },
  title: null,
  updated_at: 1_700_000_000_000,
};

// tsc checks these lines. A static type that falls back to `unknown` or
// `any` fails them, so the positive `ROW` alone is no proof.
expectTypeOf<Wire<"UnixMillis">>().toEqualTypeOf<number>();
expectTypeOf<Wire<"SessionRow">["title"]>().toEqualTypeOf<string | null>();
// @ts-expect-error created_at is a number
const BAD_TIME: Wire<"SessionRow"> = { ...ROW, created_at: "1700000000000" };
// @ts-expect-error a row has no other key
const BAD_KEY: Wire<"SessionRow"> = { ...ROW, extra: 1 };

describe("wire", () => {
  it("is the committed wire.json", () => {
    expect(wire).toEqual(JSON.parse(readFileSync(SCHEMA_FILE, "utf8")));
  });

  it.each(Object.keys(wire.$defs))("compiles a validator for %s", (name) => {
    const validator = Schema.Compile({ ...wire, $ref: `#/$defs/${name}` });

    expect(validator.IsAccelerated()).toBe(true);
  });

  it("checks a value of the static type", () => {
    const validator = Schema.Compile({ ...wire, $ref: "#/$defs/SessionRow" });

    expect(validator.Check(ROW)).toBe(true);
    expect(validator.Check({ ...ROW, created_at: 1.5 })).toBe(false);
  });
});
