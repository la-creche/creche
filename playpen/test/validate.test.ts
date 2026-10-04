// Contract 03 §13 read in the other direction: each line of the host passes
// `validate.ts` before the playpen acts on it. These cases are the caps of an
// id, which contract 02 §2 gives.

import { describe, expect, it } from "vitest";

import { readTurnContext } from "../bridge/turn-context.js";
import { parseHostMessage } from "../src/validate.js";

const TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";

/** Contract 02 §2: a session id has 1 to 128 characters. */
const LONGEST_SESSION = `owui-${"a".repeat(123)}`;
const TOO_LONG_SESSION = `owui-${"a".repeat(124)}`;

/** A `config_rev`, a pi entry id and a nonce keep the cap of the playpen. */
const LONGEST_OPAQUE = "r".repeat(200);
const TOO_LONG_OPAQUE = "r".repeat(201);

function parse(message: Record<string, unknown>): ReturnType<typeof parseHostMessage> {
  return parseHostMessage(JSON.stringify(message));
}

function startTurn(extra: Record<string, unknown>): Record<string, unknown> {
  return {
    type: "start_turn",
    turn: TURN,
    session: "owui-a",
    cwd: "/work/owui-a",
    session_dir: "/work/owui-a/pi",
    prompt: "hello",
    deadline_s: 30,
    env_epoch: 1,
    config_rev: "reg-test",
    ...extra,
  };
}

describe("the cap of a session id", () => {
  it("takes a session id of 128 characters", () => {
    expect(LONGEST_SESSION).toHaveLength(128);

    expect(parse({ type: "abort", session: LONGEST_SESSION, turn: TURN }).ok).toBe(true);
    expect(parse(startTurn({ session: LONGEST_SESSION })).ok).toBe(true);
  });

  it("refuses a session id of 129 characters in each message", () => {
    const session = TOO_LONG_SESSION;
    const process = { cwd: "/work/a", session_dir: "/work/a/pi", env_epoch: 1, config_rev: "r" };

    const lines: readonly Record<string, unknown>[] = [
      startTurn({ session }),
      { type: "prompt", turn: TURN, session, prompt: "hello", deadline_s: 30, env_epoch: 1 },
      { type: "steer", session, turn: TURN, message: "hello" },
      { type: "abort", session, turn: TURN },
      { type: "stop_process", session },
      { type: "open_session", session, ...process },
      { type: "get_entries", request: TURN, session, ...process },
    ];

    for (const line of lines) {
      expect(parse(line).ok, String(line["type"])).toBe(false);
    }
  });

  it("holds the owner session and the caller session to the same cap", () => {
    const workspace = (owner: string): Record<string, unknown> =>
      startTurn({ workspace: { kind: "code-sandbox", owner_session: owner } });
    const delegation = (caller: string): Record<string, unknown> =>
      startTurn({ delegation: { id: TURN, caller_session: caller } });

    expect(parse(workspace(LONGEST_SESSION)).ok).toBe(true);
    expect(parse(workspace(TOO_LONG_SESSION)).ok).toBe(false);
    expect(parse(delegation(LONGEST_SESSION)).ok).toBe(true);
    expect(parse(delegation(TOO_LONG_SESSION)).ok).toBe(false);
  });
});

describe("the cap of a text with no grammar in a contract", () => {
  it("stays at 200 for config_rev, an entry id and a nonce", () => {
    const process = { session: "owui-a", cwd: "/work/a", session_dir: "/work/a/pi", env_epoch: 1 };
    const entries = { type: "get_entries", request: TURN, config_rev: "r", ...process };

    expect(parse(startTurn({ config_rev: LONGEST_OPAQUE })).ok).toBe(true);
    expect(parse(startTurn({ config_rev: TOO_LONG_OPAQUE })).ok).toBe(false);
    expect(parse(startTurn({ branch: { fork_from: LONGEST_OPAQUE } })).ok).toBe(true);
    expect(parse(startTurn({ branch: { fork_from: TOO_LONG_OPAQUE } })).ok).toBe(false);
    expect(parse({ ...entries, since: LONGEST_OPAQUE }).ok).toBe(true);
    expect(parse({ ...entries, since: TOO_LONG_OPAQUE }).ok).toBe(false);
    expect(parse({ type: "ping", nonce: LONGEST_OPAQUE }).ok).toBe(true);
    expect(parse({ type: "ping", nonce: TOO_LONG_OPAQUE }).ok).toBe(false);
  });
});

describe("the bridge's copy of the session id cap", () => {
  it("sends a session id of 128 characters and drops a longer one", () => {
    // `bridge/` imports nothing from `src/`, so it holds its own copy.
    expect(readTurnContext({ AGENT_SESSION: LONGEST_SESSION }).session).toBe(LONGEST_SESSION);
    expect(readTurnContext({ AGENT_SESSION: TOO_LONG_SESSION }).session).toBeNull();
  });
});
