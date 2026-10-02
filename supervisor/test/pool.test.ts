// Contract 03 §6, §7 and §12: the hold, the reap, the cap, the per-turn
// environment and the credential rotation, each against real processes.

import { afterEach, describe, expect, it } from "vitest";
import { existsSync, readFileSync, readlinkSync } from "node:fs";
import { join } from "node:path";

import { FIXTURE_LITELLM_KEY, FIXTURE_PEP_TOKEN, Harness, until } from "./harness.js";

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

describe("the hold and reap rules", () => {
  it("holds nothing when pi_idle_ttl_s is 0", async () => {
    // §6 rule 4. 0 is the thin and autonomous default: one job, then gone.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    harness.startTurn("job-thin", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the job turn");
    expect(harness.of("turn_settled")[0]?.resident).toBe(false);

    await until(() => harness.of("process_exit").length === 1, "the reap");
    expect(harness.of("process_exit")[0]?.reason).toBe("reaped");
  });

  it("reaps a process that sat idle past the ttl", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 1 });
    harness.startTurn("owui-idle", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    expect(harness.of("process_exit")).toHaveLength(0);

    await until(() => harness.of("process_exit").length === 1, "the idle reap", 6000);
    expect(harness.of("process_exit")[0]?.session).toBe("owui-idle");
  });

  it("reaps the least recently used idle process at the cap", async () => {
    // §6 rule 5, second bullet. The cap is a memory budget: probe 0a measured
    // 155 to 170 MB per idle held-open process (A7).
    const harness = open();
    harness.start();
    harness.hello({ max_resident_processes: 2, pi_idle_ttl_s: 900 });

    for (const [index, session] of ["owui-1", "owui-2"].entries()) {
      harness.startTurn(session, turnId(index + 1));
      await until(() => harness.of("turn_settled").length === index + 1, `turn ${index + 1}`);
    }

    expect(harness.of("process_exit")).toHaveLength(0);

    harness.startTurn("owui-3", turnId(3));
    await until(() => harness.of("process_exit").length === 1, "the cap reap");

    // owui-1 settled first, so it is the least recently used of the two.
    expect(harness.of("process_exit")[0]?.session).toBe("owui-1");
    expect(harness.of("process_exit")[0]?.reason).toBe("reaped");
  });

  it("never reaps a process that is running a turn", async () => {
    const harness = open({ piEnv: { "owui-slow": { FAKE_PI_DELAY_MS: "120" } } });
    harness.start();
    harness.hello({ max_resident_processes: 1, pi_idle_ttl_s: 900 });
    harness.startTurn("owui-slow", turnId(1));

    await until(() => harness.of("event").length > 0, "the slow turn to start");
    harness.startTurn("owui-other", turnId(2));

    // The cap is a reap trigger, not an admission gate. Nothing is reapable
    // while the only resident process is busy, so both turns still settle.
    await until(() => harness.of("turn_settled").length === 2, "both turns", 20000);
  });

  it("never reaps a busy process, even for a stale epoch", async () => {
    // §6 rule 5's last line: "It never reaps a process with a running turn."
    // A prompt that arrives mid-turn is refused as busy, and the epoch is the
    // next turn's problem. Reaping here would abandon the turn in flight.
    const harness = open({ epoch: 1, piEnv: { "owui-race": { FAKE_PI_DELAY_MS: "120" } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-race", turnId(1));

    await until(() => harness.of("event").length > 0, "the turn to start");
    harness.writeCreds(2);
    harness.send({
      type: "prompt",
      turn: turnId(2),
      session: "owui-race",
      prompt: "while you are busy",
      deadline_s: 30,
      env_epoch: 2,
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.of("turn_failed")[0]?.reason).toBe("session_busy_in_sandbox");

    await until(() => harness.of("turn_settled").length === 1, "the first turn to finish", 20000);
    expect(harness.of("process_exit")).toHaveLength(0);
  });

  it("stops a process on stop_process and reports it", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-stop", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    harness.send({ type: "stop_process", session: "owui-stop", grace_ms: 500 });

    await until(() => harness.of("process_exit").length === 1, "the stop");
    expect(harness.of("process_exit")[0]?.reason).toBe("stopped");
  });
});

describe("credentials", () => {
  it("puts the key and the token in the environment and never on argv", async () => {
    // §12 rule 6. argv is world-readable through /proc/<pid>/cmdline for the
    // whole life of the process (invariant 13).
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-env", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const spec = harness.spawns[0];

    expect(spec?.env["LITELLM_VIRTUAL_KEY"]).toBe(FIXTURE_LITELLM_KEY);
    expect(spec?.env["PEP_TOKEN"]).toBe(FIXTURE_PEP_TOKEN);
    expect(JSON.stringify(spec?.args)).not.toContain(FIXTURE_LITELLM_KEY);
    expect(JSON.stringify(spec?.args)).not.toContain(FIXTURE_PEP_TOKEN);
  });

  it("carries every variable contract 03 §7 lists", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-vars", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const env = harness.spawns[0]?.env ?? {};

    expect(env["PI_CODING_AGENT_DIR"]).toBe(harness.sessionDir("owui-vars"));
    expect(env["LITELLM_BASE_URL"]).toBe("http://192.0.2.10:4000");
    expect(env["PEP_URL"]).toBe("http://192.0.2.10:8300");
    expect(env["AGENT_FAMILY"]).toBe("chat");
    expect(env["AGENT_SANDBOX"]).toBe("chat-s1");
    expect(env["AGENT_SESSION"]).toBe("owui-vars");
    expect(env["AGENT_TURN"]).toBe(turnId(1));
    expect(env["AGENT_INDEX_ROOT"]).toBe("/srv/agents/state/index");
  });

  it("replaces a process whose epoch is behind the arriving turn", async () => {
    // §12 rule 9 and §6 rule 5, third bullet. A rotation costs a process, not
    // a channel: closing the channel would kill every session of the family.
    const harness = open({ epoch: 1 });
    harness.start();
    harness.hello();
    harness.startTurn("owui-rotate", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    harness.writeCreds(2);
    harness.startTurn("owui-rotate", turnId(2), { env_epoch: 2 });

    await until(() => harness.of("process_exit").length === 1, "the replacement");
    await until(() => harness.of("turn_settled").length === 2, "the turn at the new epoch");
    expect(harness.spawns).toHaveLength(2);
  });

  it("fails the turn when the file never reaches the epoch", async () => {
    // §12 rule 5. The 250 ms retry covers the cross-mount visibility window.
    const harness = open({ epoch: 1 });
    harness.start();
    harness.hello();
    harness.startTurn("owui-stale", turnId(1), { env_epoch: 99 });

    await until(() => harness.of("turn_failed").length === 1, "the refusal", 4000);
    expect(harness.of("turn_failed")[0]?.reason).toBe("stale_credentials");
    expect(harness.spawns).toHaveLength(0);
  });
});

describe("a turn that ran without tools", () => {
  const TOOLLESS = "This turn ran without tools: the policy service did not answer.";

  function warnings(harness: Harness): string[] {
    return harness
      .of("log")
      .filter((line) => line.level === "warn")
      .map((line) => line.message);
  }

  it("says so once, in words a person reads", async () => {
    // The bridge states `tools: 0` in the control
    // mount, and this is the only way the supervisor can learn it: the
    // bridge's own note goes to stderr, which is not a protocol.
    const harness = open();
    harness.start();
    harness.hello();
    harness.writeToolState("owui-toolless", 0);
    harness.startTurn("owui-toolless", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(warnings(harness)).toEqual([TOOLLESS]);
  });

  it("says it again on the next turn, and not twice on one", async () => {
    // Once per turn. A reader who asks again during the same outage is told
    // again, because the second answer is as toolless as the first.
    const harness = open();
    harness.start();
    harness.hello();
    harness.writeToolState("owui-toolless", 0);
    harness.startTurn("owui-toolless", turnId(1));
    await until(() => harness.of("turn_settled").length === 1, "the first turn");

    harness.startTurn("owui-toolless", turnId(2));
    await until(() => harness.of("turn_settled").length === 2, "the second turn");

    expect(warnings(harness)).toEqual([TOOLLESS, TOOLLESS]);
  });

  it("stays quiet once the background retry has registered the tools", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.writeToolState("owui-armed", 4, "rev-7");
    harness.startTurn("owui-armed", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(warnings(harness)).toEqual([]);
  });

  it("stays quiet when the bridge said nothing at all", async () => {
    // An older sandbox image writes no state file. Null is not "no tools",
    // and claiming a tool set the supervisor cannot see would be a guess.
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-silent", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(warnings(harness)).toEqual([]);
  });

  it("names the grant rather than the PEP when the family was granted none", async () => {
    // A manifest that ARRIVED and offered nothing is not an outage. Saying
    // "the policy service did not answer" there sends a reader to look at a
    // service that is healthy.
    const harness = open();
    harness.start();
    harness.hello();
    harness.writeToolState("owui-ungranted", 0, "rev-7");
    harness.startTurn("owui-ungranted", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(warnings(harness)).toEqual(["This turn ran without tools: this agent is granted none."]);
  });
});

describe("a tool list that moved under a running session", () => {
  const CHANGED = "The tool list changed while this session was open: 2 tools are available now.";

  function warnings(harness: Harness): string[] {
    return harness
      .of("log")
      .filter((line) => line.level === "warn")
      .map((line) => line.message);
  }

  /** One turn, with whatever the bridge last stated about this session. */
  async function turn(harness: Harness, session: string, n: number): Promise<void> {
    harness.startTurn(session, turnId(n));
    await until(() => harness.of("turn_settled").length === n, `turn ${n}`);
  }

  it("tells the reader once, on the turn that first sees the new list", async () => {
    // The operator grants a tool, the bridge registers it under the running
    // process, and the turn that first has it says so. It is the same `log`
    // at `warn` as a toolless turn: no new event type (contract 03 §5.5).
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.writeToolState("owui-grant", 1, "rev-a");
    await turn(harness, "owui-grant", 1);

    harness.writeToolState("owui-grant", 2, "rev-b");
    await turn(harness, "owui-grant", 2);
    await turn(harness, "owui-grant", 3);

    expect(warnings(harness)).toEqual([CHANGED]);
  });

  it("says nothing about the list a session started with", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.writeToolState("owui-fresh", 3, "rev-a");
    await turn(harness, "owui-fresh", 1);

    expect(warnings(harness)).toEqual([]);
  });

  it("says nothing when a fresh process reports a revision of its own", async () => {
    // `pi_idle_ttl_s` 0 reaps after every turn, so each turn is a NEW pi
    // process with a start-up manifest. That is not a list that moved under
    // a running session, and a reader told otherwise would go looking for an
    // edit nobody made.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    harness.writeToolState("job-thin", 1, "rev-a");
    await turn(harness, "job-thin", 1);
    await until(() => harness.of("process_exit").length === 1, "the reap");

    harness.writeToolState("job-thin", 2, "rev-b");
    await turn(harness, "job-thin", 2);

    expect(warnings(harness)).toEqual([]);
  });
});

describe("the family config mount", () => {
  it("denies the built-ins runtime.json leaves out, and never allowlists", async () => {
    // `--tools` and `--no-tools` both cover
    // extension tools, so either would silently drop every PEP tool the
    // bridge registers. The seven built-ins are named to deny instead.
    const harness = open({ runtime: { shell: false, sandbox_tools: ["read", "grep"] } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-tools", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args).toContain("--exclude-tools");
    expect(args[args.indexOf("--exclude-tools") + 1]).toBe("bash,powershell,edit,write,find,ls");
    expect(args).not.toContain("--tools");
    expect(args).not.toContain("--no-tools");
  });

  it("adds the shell only when the family asked for it", async () => {
    const harness = open({ runtime: { shell: true, sandbox_tools: ["read"] } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-shell", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args[args.indexOf("--exclude-tools") + 1]).toBe("powershell,edit,write,grep,find,ls");
  });

  it("denies only powershell when the family granted every other built-in", async () => {
    const harness = open({
      runtime: { shell: true, sandbox_tools: ["read", "write", "edit", "grep", "find", "ls"] },
    });
    harness.start();
    harness.hello();
    harness.startTurn("owui-all-tools", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");

    const args = harness.spawns[0]?.args ?? [];
    expect(args[args.indexOf("--exclude-tools") + 1]).toBe("powershell");
  });

  it("denies every built-in when the family granted none", async () => {
    // Contract 01 §3.8 rule 2 allows an empty list. It must still leave the
    // bridge's PEP tools registered, so it is seven denials, not `--no-tools`.
    const harness = open({ runtime: { shell: false, sandbox_tools: [] } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-no-tools", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args[args.indexOf("--exclude-tools") + 1]).toBe("read,bash,powershell,edit,write,grep,find,ls");
    expect(args).not.toContain("--no-tools");
  });

  it("turns discovery off for extensions and for project trust", async () => {
    // The session store is agent-writable and outlives the sandbox, so
    // extension discovery would let a session leave code that runs at every
    // later session start.
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-flags", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args).toContain("--no-approve");
    expect(args).toContain("--no-extensions");
    expect(args).toContain("--offline");
    expect(args).toContain("--mode");
    expect(args).toContain("rpc");
  });

  it("writes models.json into the session store before pi starts", async () => {
    // pi reads it from PI_CODING_AGENT_DIR and nowhere else, and offers no
    // flag to point elsewhere.
    const harness = open({ runtime: { model_alias: "agent-router" } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-models", turnId(1));

    await until(() => harness.spawns.length === 1, "the spawn");
    const path = join(harness.sessionDir("owui-models"), "models.json");
    const doc = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
    const providers = doc["providers"] as Record<string, Record<string, unknown>>;

    expect(providers["litellm"]?.["baseUrl"]).toBe("http://192.0.2.10:4000");
    // An environment reference, resolved per request, so the file holds no
    // credential (invariant 13).
    expect(providers["litellm"]?.["apiKey"]).toBe("$LITELLM_VIRTUAL_KEY");
    expect(harness.spawns[0]?.args).toContain("litellm/agent-router");
  });

  it("lets the turn's own model win over the family default", async () => {
    const harness = open({ runtime: { model_alias: "agent-router" } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-model", turnId(1), { model: "litellm/agent-big" });

    await until(() => harness.spawns.length === 1, "the spawn");
    const args = harness.spawns[0]?.args ?? [];

    expect(args[args.indexOf("--model") + 1]).toBe("litellm/agent-big");
  });

  it("replaces a held process when the turn names a different model", async () => {
    // --model is a start flag, so a held process cannot change model.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-switch", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    harness.startTurn("owui-switch", turnId(2), { model: "litellm/agent-big" });

    await until(() => harness.of("turn_settled").length === 2, "the second turn");
    expect(harness.spawns).toHaveLength(2);
  });
});

describe("the code-sandbox link", () => {
  it("points the turn at a link it derives from its own mount", async () => {
    // §7.2. The host never sends a host path, so a compromised host message
    // cannot aim the link somewhere else.
    const owner = "owui-3f2a9c41";
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("job-code", turnId(1), {
      workspace: { kind: "code-sandbox", owner_session: owner },
    });

    await until(() => harness.spawns.length === 1, "the spawn");
    const cwd = harness.spawns[0]?.cwd ?? "";

    expect(cwd).toBe(join(harness.root, `link-${owner}`));
    expect(existsSync(cwd)).toBe(true);
    expect(readlinkSync(cwd)).toBe(join(harness.root, "code-sandbox", owner));
  });

  it("refuses an owner_session that is not a session id", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("job-bad", turnId(1), {
      workspace: { kind: "code-sandbox", owner_session: "../escape" },
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.spawns).toHaveLength(0);
  });
});
