// The guest values of the facade, held equal to the one list of the Rust
// wire crate (decision CH62 rule 5). Never change the file to make this
// test pass: change the constant in `creche_wire::guest` and the file
// together, then the copy here.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { CONTROL_LINE_CAP, FACADE_STOP_WAIT_MS, LOCAL_LINK_SOCKET, SESSION_ID_ENV } from "../src/guest.js";

const VECTOR_FILE = new URL("../../rust/crates/wire/vectors/guest.json", import.meta.url);

const guest = JSON.parse(readFileSync(VECTOR_FILE, "utf8")) as Record<string, unknown>;

describe("the guest values", () => {
  it("name the env of the session id as the wire crate does", () => {
    expect(SESSION_ID_ENV).toBe(guest["session_id_env"]);
  });

  it("name the local link, its line cap and the stop wait as the wire crate does", () => {
    expect(LOCAL_LINK_SOCKET).toBe(guest["local_link_socket"]);
    expect(CONTROL_LINE_CAP).toBe(guest["control_line_cap"]);
    expect(FACADE_STOP_WAIT_MS).toBe(guest["facade_stop_wait_ms"]);
  });
});
