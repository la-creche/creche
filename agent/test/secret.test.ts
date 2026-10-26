// Only the places that the Phase 2 plan names unwrap a secret: the `auth` of
// the `ch` connection and the `auth` of the LiteLLM provider. Rows 3 and 4
// of the plan add their file to `UNWRAP_SITES`. No other file gets in.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { Secret } from "../src/secret.js";

const SRC = join(import.meta.dirname, "..", "src");
const UNWRAP_SITES: readonly string[] = ["litellm.ts"];

describe("Secret", () => {
  it("has no text form of its own", () => {
    const secret = Secret.of("FIXTURE-WORD");

    expect([String(secret), JSON.stringify(secret)]).toEqual(["[object Object]", "{}"]);
    expect(Object.keys(secret)).toEqual([]);
  });

  it("is unwrapped only where the plan says", () => {
    const sites = readdirSync(SRC).filter(
      (name) => name !== "secret.ts" && readFileSync(join(SRC, name), "utf8").includes("revealSecret"),
    );

    expect(sites).toEqual(UNWRAP_SITES);
  });
});
