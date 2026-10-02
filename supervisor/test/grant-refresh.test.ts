// A tool the operator GRANTS reaches the chat they are already in.
//
// Contract 03 §6 rule 4 holds an attended process for 15 minutes, so a
// manifest fetched once would never see a grant added after the process
// started. `config_rev` versions the family's config mount and not the grant
// file, so the supervisor's own staleness path never fires for a grant either.
//
//   grant file moves ──► PEP serves a new rev
//        │
//        ▼
//   bridge watch loop ──► GET /manifest, If-None-Match: "<rev held>"
//        │                   │
//        │                   ├── 304 ──► nothing happened, sleep again
//        │                   └── 200 ──► register the added and the changed,
//        │                               withdraw the removed
//        ▼
//   tools.json ──► the supervisor tells the reader, once (`pool.test.ts`)
//
// The PEP here is `test/fake-pep.ts` on 127.0.0.1. Nothing touches
// the host's PEP and nothing needs the host.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import bridge from "../bridge/index.js";
import { MANIFEST_BACKGROUND_MS, MANIFEST_WATCH_MS } from "../bridge/constants.js";
import { PiExposure } from "../bridge/pi-api.js";
import type { ToolState } from "../bridge/tool-state.js";
import { FakePep, FakePi } from "./fake-pep.js";

const SESSION = "owui-3f2a9c41";
const TURN_ONE = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";
const SEARCH = "kagi__search";
const HA_CALL = "ha_call";

/** Fast enough for a suite, and the one thing pi never passes the factory. */
const TEST_PACE = { retryMs: 20, watchMs: 20 };

const OWNED_ENV = [
  "PEP_URL",
  "PEP_TOKEN",
  "AGENT_SESSION",
  "AGENT_TURN",
  "AGENT_TURN_FILE",
  "AGENT_TOOL_STATE_FILE",
];

function entry(name: string, description = "Search the web."): Record<string, unknown> {
  return {
    name,
    description,
    schema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] },
    approval: false,
  };
}

let pep: FakePep;
let pi: FakePi;
let root: string;
let statePath: string;
const saved = new Map<string, string | undefined>();

beforeEach(async () => {
  for (const name of OWNED_ENV) {
    saved.set(name, process.env[name]);
    delete process.env[name];
  }

  root = mkdtempSync(join(tmpdir(), "grant-refresh-test-"));
  pep = new FakePep();
  pi = new FakePi();
  process.env["PEP_URL"] = await pep.start();
  // An obvious fixture, not a credential. Invariant 13 forbids a real one.
  process.env["PEP_TOKEN"] = "FIXTURE-PEP-TOKEN";
  process.env["AGENT_SESSION"] = SESSION;
  process.env["AGENT_TURN"] = TURN_ONE;

  const dir = join(root, "sessions", SESSION);
  mkdirSync(dir, { recursive: true });
  statePath = join(dir, "tools.json");
  process.env["AGENT_TOOL_STATE_FILE"] = statePath;
});

afterEach(async () => {
  await pep.stop();
  rmSync(root, { recursive: true, force: true });
  for (const [name, value] of saved) {
    if (value === undefined) {
      delete process.env[name];
      continue;
    }
    process.env[name] = value;
  }
  saved.clear();
});

/** The grant file as the PEP serves it: one revision, one tool list. */
function serve(rev: string, tools: readonly unknown[]): void {
  pep.manifest = { status: 200, body: { family: "chat", rev, tools } };
}

function state(): ToolState {
  return JSON.parse(readFileSync(statePath, "utf8")) as ToolState;
}

/** Waits for something the watch loop reaches on its own timer. */
async function until(ready: () => boolean, what: string): Promise<void> {
  const deadline = Date.now() + 5000;
  while (!ready()) {
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${what}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

/** Lets the watch loop run a handful of intervals with nothing to find. */
function idleIntervals(count: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, TEST_PACE.watchMs * count));
}

describe("a granted tool reaches a running pi process", () => {
  it("registers a tool the grant file gained", async () => {
    serve("rev-a", [entry(SEARCH)]);
    await bridge(pi, TEST_PACE);
    expect(pi.getActiveTools()).toEqual([SEARCH]);

    // The operator edits the family file. `managerd` rewrites the grant file with a
    // fresh revision, and the PEP serves it on the next ask.
    serve("rev-b", [entry(SEARCH), entry(HA_CALL, "Call Home Assistant.")]);
    await until(() => pi.named(HA_CALL) !== undefined, "the watch loop to register ha_call");

    expect([...pi.getActiveTools()].sort()).toEqual([HA_CALL, SEARCH].sort());
    expect(state()).toMatchObject({ tools: 2, rev: "rev-b" });
  }, 20000);

  it("withdraws a tool the grant file lost", async () => {
    serve("rev-a", [entry(SEARCH), entry(HA_CALL)]);
    await bridge(pi, TEST_PACE);

    serve("rev-b", [entry(SEARCH)]);
    await until(() => !pi.getActiveTools().includes(HA_CALL), "the watch loop to drop ha_call");

    // pi has no unregister (`real-pi.test.ts`), so the definition survives,
    // hidden from the model and from every codemode script. The PEP denies
    // the call either way.
    expect(pi.getActiveTools()).toEqual([SEARCH]);
    expect(pi.named(HA_CALL)?.exposure).toBe(PiExposure.Hidden);
    expect(state()).toMatchObject({ tools: 1, rev: "rev-b" });
  }, 20000);

  it("gives a withdrawn tool back when the grant returns", async () => {
    serve("rev-a", [entry(SEARCH), entry(HA_CALL)]);
    await bridge(pi, TEST_PACE);

    serve("rev-b", [entry(SEARCH)]);
    await until(() => !pi.getActiveTools().includes(HA_CALL), "the watch loop to drop ha_call");

    serve("rev-c", [entry(SEARCH), entry(HA_CALL)]);
    await until(() => pi.getActiveTools().includes(HA_CALL), "the watch loop to restore ha_call");

    expect(pi.named(HA_CALL)?.exposure).toBe(PiExposure.Direct);
    expect(state()).toMatchObject({ tools: 2, rev: "rev-c" });
  }, 20000);

  it("replaces the definition of a tool whose description moved", async () => {
    serve("rev-a", [entry(SEARCH, "The first grant's words.")]);
    await bridge(pi, TEST_PACE);

    serve("rev-b", [entry(SEARCH, "The second grant's words.")]);
    await until(
      () => pi.named(SEARCH)?.description === "The second grant's words.",
      "the watch loop to re-register kagi__search",
    );

    // One tool, registered twice: replaced, never duplicated.
    expect(pi.tools).toHaveLength(1);
    expect(pi.registrations).toEqual([SEARCH, SEARCH]);
  }, 20000);

  it("registers nothing again when only the revision moved", async () => {
    // `managerd` mints a fresh `rev` on every grant write, and a token
    // rotation writes the file without touching the tool list
    // (`managerd/src/agent_managerd/steps.py`, `grant_revision`). A reader
    // must not be told the tool list changed when it did not.
    serve("rev-a", [entry(SEARCH)]);
    await bridge(pi, TEST_PACE);
    const wroteAt = state().at;

    serve("rev-b", [entry(SEARCH)]);
    await until(() => pep.fetches.length > 2, "the watch loop to see the new revision");

    expect(pi.registrations).toEqual([SEARCH]);
    expect(state()).toMatchObject({ tools: 1, rev: "rev-a", at: wroteAt });
  }, 20000);
});

describe("the watch poll is cheap enough to run for ever", () => {
  it("sends the revision it holds, and does nothing with a 304", async () => {
    serve("rev-a", [entry(SEARCH)]);
    await bridge(pi, TEST_PACE);
    const wroteAt = state().at;

    await until(() => pep.fetches.length >= 3, "two watch polls");
    const conditional = pep.fetches.slice(1);

    expect(conditional.every((one) => one.headers["if-none-match"] === '"rev-a"')).toBe(true);
    expect(pi.registrations).toEqual([SEARCH]);
    expect(state().at).toBe(wroteAt);
  }, 20000);

  it("asks once per interval and never two at once", async () => {
    // The bound to state fleet-wide: N pi processes times one request per
    // interval. The loop awaits its own fetch before it sleeps again.
    serve("rev-a", [entry(SEARCH)]);
    await bridge(pi, TEST_PACE);
    const afterStart = pep.fetches.length;
    const intervals = 4;

    await idleIntervals(intervals);
    const added = pep.fetches.length - afterStart;

    expect(afterStart).toBe(1);
    expect(added).toBeGreaterThan(0);
    expect(added).toBeLessThanOrEqual(intervals + 1);
  }, 20000);

  it("watches slowly once it holds a manifest and quickly while it holds none", async () => {
    // pi calls `factory(api)` with one argument (0.99.1 `loader.js`), so the
    // pace the loop really runs at is the DEFAULT parameter. This one watches
    // `setTimeout` instead, and names the constants rather than the numbers.
    const delays: unknown[] = [];
    const real = globalThis.setTimeout;
    const spy = vi
      .spyOn(globalThis, "setTimeout")
      .mockImplementation(((handler: () => void, ms?: number, ...rest: unknown[]) => {
        delays.push(ms);

        return (real as (...args: unknown[]) => unknown)(handler, ms, ...rest);
      }) as unknown as typeof globalThis.setTimeout);

    try {
      serve("rev-a", [entry(SEARCH)]);
      await bridge(pi);
    } finally {
      spy.mockRestore();
    }

    expect(delays).toContain(MANIFEST_WATCH_MS);
    expect(delays).not.toContain(MANIFEST_BACKGROUND_MS);
  }, 20000);

  it("still works against a PEP that ignores the conditional header", async () => {
    // An older PEP answers 200 with the same revision. That costs a body per
    // poll and nothing else: the bridge compares the revision it holds.
    pep.revalidates = false;
    serve("rev-a", [entry(SEARCH)]);
    await bridge(pi, TEST_PACE);
    const wroteAt = state().at;

    await until(() => pep.fetches.length >= 3, "two watch polls");

    expect(pi.registrations).toEqual([SEARCH]);
    expect(state().at).toBe(wroteAt);
  }, 20000);

  it("keeps watching after the PEP comes back from an outage", async () => {
    // The loop does not stop at its first answer, so a process born inside
    // an outage gets every LATER grant too.
    serve("rev-a", [entry(SEARCH)]);
    const url = process.env["PEP_URL"];
    await pep.stop();

    await bridge(pi, TEST_PACE);
    expect(pi.tools).toHaveLength(0);

    await pep.start(url);
    await until(() => pi.tools.length > 0, "the retry to register the first manifest");

    serve("rev-b", [entry(SEARCH), entry(HA_CALL)]);
    await until(() => pi.named(HA_CALL) !== undefined, "the watch loop to register ha_call");

    expect(state()).toMatchObject({ tools: 2, rev: "rev-b" });
  }, 20000);

  it("holds no tools and no state when the PEP never answers", async () => {
    pep.manifest = { status: 503, body: { reason: "down" } };

    await bridge(pi, TEST_PACE);
    await idleIntervals(3);

    expect(pi.tools).toHaveLength(0);
    expect(state()).toMatchObject({ tools: 0, rev: null });
  }, 20000);
});
