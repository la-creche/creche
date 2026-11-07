// The guest values of the facade, held equal to the one list of the Rust
// wire crate (decision CH62 rule 5). Never change the file to make this
// test pass: change the constant in `creche_wire::guest` and the file
// together, then the copy here.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { SESSION_ID_ENV } from "../src/guest.js";

const VECTOR_FILE = new URL("../../rust/crates/wire/vectors/guest.json", import.meta.url);

const guest = JSON.parse(readFileSync(VECTOR_FILE, "utf8")) as Record<string, unknown>;

describe("the guest values", () => {
  it("name the env of the session id as the wire crate does", () => {
    expect(SESSION_ID_ENV).toBe(guest["session_id_env"]);
  });
});
