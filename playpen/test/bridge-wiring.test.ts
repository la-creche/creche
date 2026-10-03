// How the playpen hands the PEP bridge to each pi process: the explicit
// `--extension`, the deny list that leaves the bridge's tools standing, and
// the per-turn file the bridge reads for contract 04 §3's `X-Turn-Id`.
//
// What the bridge DOES with any of this is `test/bridge.test.ts`'s subject.
// `fake-pi.mjs` ignores `--extension`, and growing it into something that did
// not would make a wrong playpen look right (AGENTS.md rule 16).

import { afterEach, describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { Harness, until } from "./harness.js";

function turnId(n: number): string {
  return `01JBQ7WZ0X4T9V6K2H8M3N${String(n).padStart(4, "0")}`;
}

const live: Harness[] = [];

function open(options: ConstructorParameters<typeof Harness>[0] = {}): Harness {
  const harness = new Harness(options);
  live.push(harness);

  return harness;
}

afterEach(async () => {
  while (live.length > 0) {
    await live.pop()?.dispose();
  }
});

/** The turn file the playpen writes, as the bridge would read it. */
function readTurnFile(harness: Harness, session: string): Record<string, unknown> {
  const path = join(harness.root, "control", "sessions", session, "turn.json");

  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

describe("loading the bridge", () => {
  it("loads it by explicit path while discovery stays off", async () => {
    // B2 added --no-extensions on purpose: the session store is
    // agent-writable and outlives the sandbox, so discovery would run code a
    // session left behind. One named path is the whole difference.
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-bridge", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args).toContain("--no-extensions");
    expect(args[args.indexOf("--extension") + 1]).toBe(harness.bridgePath);
    expect(args.indexOf("--no-extensions")).toBeLessThan(args.indexOf("--extension"));
  });

  it("still opens the session when the image carries no bridge", async () => {
    const harness = open({ bridge: false });
    harness.start();
    harness.hello();
    harness.startTurn("owui-no-bridge", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(harness.spawns[0]?.args ?? []).not.toContain("--extension");
    expect(harness.of("log").some((line) => line.message.includes("no PEP bridge"))).toBe(true);
  });

  it("keeps the bridge's tools when sandbox_tools names only four", async () => {
    // `--tools read,grep,find,ls` would be a strict allowlist over extension
    // tools too, so every PEP tool would go. The deny list names bash and
    // leaves the bridge alone.
    const harness = open({ runtime: { sandbox_tools: ["read", "grep", "find", "ls"] } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-deny", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args[args.indexOf("--exclude-tools") + 1]).toBe("bash,powershell,edit,write");
    expect(args).not.toContain("--tools");
    expect(args).not.toContain("--no-tools");
    expect(args).toContain("--extension");
  });
});

describe("the per-turn file", () => {
  it("names the turn file in the child's environment, never on argv", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-env", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const spawn = harness.spawns[0];
    const path = join(harness.root, "control", "sessions", "owui-env", "turn.json");

    expect(spawn?.env["AGENT_TURN_FILE"]).toBe(path);
    expect(spawn?.args.join(" ")).not.toContain("turn.json");
  });

  it("rewrites it for each turn of one held-open process", async () => {
    // Contract 03 §6 rule 4 keeps the process, so AGENT_TURN goes stale from
    // the second turn onward. This file is what does not.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-held", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    expect(readTurnFile(harness, "owui-held")["turn"]).toBe(turnId(1));

    harness.send({
      type: "prompt",
      turn: turnId(2),
      session: "owui-held",
      prompt: "and again",
      deadline_s: 30,
      env_epoch: 1,
    });

    await until(() => harness.of("turn_settled").length === 2, "the second turn");

    expect(harness.spawns).toHaveLength(1);
    expect(readTurnFile(harness, "owui-held")).toEqual({
      session: "owui-held",
      turn: turnId(2),
      delegation: null,
      caller_session: null,
    });
  });

  it("removes the session's directory when its process is gone", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    harness.startTurn("owui-gone", turnId(1));

    await until(() => harness.of("process_exit").length === 1, "the reap");

    expect(existsSync(join(harness.root, "control", "sessions", "owui-gone"))).toBe(false);
  });
});
