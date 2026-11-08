// The guest values of the agent, held equal to the one list of the Rust
// wire crate (decision CH62 rule 5). Never change the file to make this
// test pass: change the constant in `creche_wire::guest` and the file
// together, then the copy here.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { STATE_REPLY_CAP } from "../src/family-state.js";
import { FAMILY_STATE_SOCKET } from "../src/guest.js";

const VECTOR_FILE = new URL("../../rust/crates/wire/vectors/guest.json", import.meta.url);

const guest = JSON.parse(readFileSync(VECTOR_FILE, "utf8")) as Record<string, unknown>;

describe("the guest values", () => {
  it("name the family-state socket and its line cap as the wire crate does", () => {
    expect(FAMILY_STATE_SOCKET).toBe(guest["family_state_socket"]);
    expect(STATE_REPLY_CAP).toBe(guest["family_state_cap"]);
  });
});
