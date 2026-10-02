// Contract 03 §7.4 and contract 04 §3. A thin job runs inside a delegation
// chain, and the PEP audit is the only record that outlives it (invariant 15).
// So the id the PEP minted has to reach the PEP again on every call the job
// makes:
//
//   sessiond ──start_turn{delegation}──► supervisor ──► turn.json
//                                                          │
//                              pep-bridge reads it per call ▼
//                                          PEP ◄── X-Delegation-Id
//
// Both halves run for real here: the supervisor writes the file, and the real
// bridge reads THAT file and calls `test/fake-pep.ts` on 127.0.0.1.

import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import bridge from "../bridge/index.js";
import { Harness, until } from "./harness.js";
import { FakePep, FakePi, resultText } from "./fake-pep.js";

const DELEGATION = "01K5J9QWB2M4N6Q8S0V2W4Y6A8";
const CALLER = "owui-3f2a9c41";
const JOB = "job-01K5J9QWB4XN2A7C6E0F3G5H8J";
const SEARCH = "kagi__search";

const SEARCH_ENTRY = {
  name: SEARCH,
  description: "Search the web.",
  schema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] },
  approval: false,
};

function turnId(n: number): string {
  return `01JBQ7WZ0X4T9V6K2H8M3N${String(n).padStart(4, "0")}`;
}

/** Everything the bridge reads from its process environment. */
const BRIDGE_ENV = ["PEP_URL", "PEP_TOKEN", "AGENT_SESSION", "AGENT_TURN", "AGENT_TURN_FILE"];

const live: Harness[] = [];
const peps: FakePep[] = [];
const saved = new Map<string, string | undefined>();

function open(options: ConstructorParameters<typeof Harness>[0] = {}): Harness {
  const harness = new Harness(options);
  live.push(harness);

  return harness;
}

afterEach(async () => {
  while (live.length > 0) {
    await live.pop()?.dispose();
  }
  while (peps.length > 0) {
    await peps.pop()?.stop();
  }
  for (const [name, value] of saved) {
    if (value === undefined) {
      delete process.env[name];
      continue;
    }
    process.env[name] = value;
  }
  saved.clear();
});

/** What the supervisor wrote for one session, as the bridge would read it. */
function turnFile(harness: Harness, session: string): Record<string, unknown> {
  const path = join(harness.root, "control", "sessions", session, "turn.json");

  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

/**
 * The real bridge, pointed at the file the supervisor just wrote, calling a
 * fake PEP on 127.0.0.1. Nothing here touches the host's PEP.
 */
async function callThroughBridge(harness: Harness, session: string): Promise<FakePep> {
  const pep = new FakePep();
  peps.push(pep);
  pep.manifest = { status: 200, body: { family: "chat", rev: "rev-7", tools: [SEARCH_ENTRY] } };

  for (const name of BRIDGE_ENV) {
    saved.set(name, process.env[name]);
    delete process.env[name];
  }
  process.env["PEP_URL"] = await pep.start();
  // An obvious fixture, not a credential. Invariant 13 forbids a real one.
  process.env["PEP_TOKEN"] = "FIXTURE-PEP-TOKEN";
  process.env["AGENT_SESSION"] = session;
  process.env["AGENT_TURN_FILE"] = join(harness.root, "control", "sessions", session, "turn.json");

  const pi = new FakePi();
  await bridge(pi);

  const tool = pi.named(SEARCH);
  if (tool === undefined) {
    throw new Error("the bridge registered no kagi__search tool");
  }

  resultText(await tool.execute("call-1", { q: "boiler" }));

  return pep;
}

describe("a delegated turn", () => {
  it("writes the chain into the turn file the bridge reads", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: DELEGATION, caller_session: CALLER },
    });

    await until(() => harness.of("turn_settled").length === 1, "the job turn");

    expect(turnFile(harness, JOB)).toEqual({
      session: JOB,
      turn: turnId(1),
      delegation: DELEGATION,
      caller_session: CALLER,
    });
  });

  it("puts X-Delegation-Id on the call the job makes", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: DELEGATION, caller_session: CALLER },
    });

    await until(() => harness.of("turn_settled").length === 1, "the job turn");
    const pep = await callThroughBridge(harness, JOB);
    const headers = pep.calls[0]?.headers ?? {};

    expect(headers["x-delegation-id"]).toBe(DELEGATION);
    expect(headers["x-turn-id"]).toBe(turnId(1));
    expect(headers["x-session-id"]).toBe(JOB);
  });

  it("carries a later turn's own chain, never the process's first", async () => {
    // §7.4 exists because a held-open process serves many turns. A second turn
    // of a delegated session must rewrite the chain, not inherit it.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: DELEGATION, caller_session: CALLER },
    });

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    harness.send({
      type: "prompt",
      turn: turnId(2),
      session: JOB,
      prompt: "and again",
      deadline_s: 30,
      env_epoch: 1,
    });

    await until(() => harness.of("turn_settled").length === 2, "the second turn");

    expect(turnFile(harness, JOB)).toEqual({
      session: JOB,
      turn: turnId(2),
      delegation: null,
      caller_session: null,
    });
  });

  it("writes nulls for an ordinary turn", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-plain", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(turnFile(harness, "owui-plain")).toEqual({
      session: "owui-plain",
      turn: turnId(1),
      delegation: null,
      caller_session: null,
    });
  });
});

describe("a delegation with no caller session", () => {
  // Contract 04 §7.3: the PEP's claimed_session_id is itself advisory and
  // the PEP may send none. §7.4 rule 4 item 5: that is not malformed. The turn
  // keeps the delegation id and the chain, and gets no code-sandbox link,
  // which needs an owner_session no unattributed call can supply.
  it("keeps the delegation id rather than failing the turn", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: DELEGATION },
    });

    await until(() => harness.of("turn_settled").length === 1, "the job turn");

    expect(turnFile(harness, JOB)).toEqual({
      session: JOB,
      turn: turnId(1),
      delegation: DELEGATION,
      caller_session: null,
    });
    expect(harness.of("turn_failed")).toHaveLength(0);
  });

  it("still puts X-Delegation-Id on the call the job makes", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: DELEGATION },
    });

    await until(() => harness.of("turn_settled").length === 1, "the job turn");
    const pep = await callThroughBridge(harness, JOB);
    const headers = pep.calls[0]?.headers ?? {};

    expect(headers["x-delegation-id"]).toBe(DELEGATION);
  });
});

describe("a malformed delegation", () => {
  it("fails the turn rather than running it unattributed", async () => {
    // Only the PEP mints one, as an upper-case ULID (contract 04 §7.4). A
    // value of another shape is a broken delegate door, and the audit record
    // is the one thing a thin job leaves behind, so it is not run silently.
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: "not-a-ulid", caller_session: CALLER },
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.of("turn_failed")[0]?.reason).toBe("internal");
    expect(harness.spawns).toHaveLength(0);
  });

  it("refuses a caller_session that is not a session id", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn(JOB, turnId(1), {
      delegation: { id: DELEGATION, caller_session: "../../etc" },
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.spawns).toHaveLength(0);
  });
});
