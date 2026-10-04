// Contract 03 §4. The names of the host messages are in three places: the
// union `HostMessageType`, the union `HostMessage`, and the set that
// `validate.ts` reads. These cases hold the three equal. The first two are
// types, so `pnpm run typecheck` is the check that fails for them.

import { describe, expect, expectTypeOf, it } from "vitest";

import type { HostMessage, HostMessageType } from "../src/protocol.js";
import { parseHostMessage } from "../src/validate.js";

/** One entry for each name of §4. A name that the union lacks does not compile. */
const HOST_TYPES: Readonly<Record<HostMessageType, true>> = {
  hello: true,
  open_session: true,
  start_turn: true,
  prompt: true,
  steer: true,
  abort: true,
  stop_process: true,
  get_entries: true,
  ping: true,
  shutdown: true,
};

const UNKNOWN_TYPE = "type is missing or unknown";

describe("the names of a host message", () => {
  it("are the same in the name union and in the message union", () => {
    expectTypeOf<HostMessageType>().toEqualTypeOf<HostMessage["type"]>();
  });

  it("are each a type that the validator knows", () => {
    for (const type of Object.keys(HOST_TYPES)) {
      const parsed = parseHostMessage(JSON.stringify({ type }));

      expect(parsed.ok ? "" : parsed.detail, type).not.toBe(UNKNOWN_TYPE);
    }

    const other = parseHostMessage(JSON.stringify({ type: "get_everything" }));
    expect(other.ok ? "" : other.detail).toBe(UNKNOWN_TYPE);
  });
});
