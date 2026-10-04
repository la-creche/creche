// Contract 03 §7.6. `agent-pi-launch` is the TUI door's end in the sandbox: a
// terminal attached to a session the playpen also serves, inside the same
// sandbox, on the same session store.
//
//   sbx exec -it <sandbox> -- node /opt/agent-supervisor/agent-pi-launch.js \
//     --sandbox <id> --session <session id> [--cwd <dir>] [--new]
//
// Two things have to hold, and both are asserted here:
//
//  1. The terminal's pi runs with the playpen's own line minus `--mode
//     rpc`. A second builder would let a terminal quietly have a reach the
//     chat does not.
//  2. It refuses while a live rpc process holds the session (§7.5). Two
//     writers on one session store cross-contaminate context, orphan a branch,
//     and both report success with no error anywhere.

import { afterEach, describe, expect, it } from "vitest";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { CRED_DIR_ENV, CONTROL_DIR_ENV, FAMILY_CONFIG_DIR_ENV } from "../src/mounts.js";
import { launchExitCode, LaunchState, planLaunch } from "../src/pi-launch.js";
import { ProcessRecords } from "../src/process-record.js";
import { SESSIONS_MOUNT_ENV } from "../src/constants.js";
import { FIXTURE_LITELLM_KEY, FIXTURE_PEP_TOKEN, Harness, until } from "./harness.js";

const SANDBOX = "chat-s1";
const SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR";

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

/** The environment `caregiver`'s `supervisor.env` carries, for one harness. */
function mountEnv(harness: Harness): NodeJS.ProcessEnv {
  return {
    [CRED_DIR_ENV]: join(harness.root, "creds"),
    [FAMILY_CONFIG_DIR_ENV]: join(harness.root, "config"),
    [CONTROL_DIR_ENV]: join(harness.root, "control"),
    [SESSIONS_MOUNT_ENV]: join(harness.root, "sessions"),
  };
}

function launch(harness: Harness, argv: readonly string[]): ReturnType<typeof planLaunch> {
  return planLaunch({
    argv,
    env: mountEnv(harness),
    bridgePath: harness.bridgePath,
  });
}

const ATTACH = ["--sandbox", SANDBOX, "--session", SESSION];

describe("the launcher's command line", () => {
  it("is the pool's own, minus --mode rpc", async () => {
    // One builder, two modes. `pi-args.ts` is the whole implementation of
    // this promise, and this is the test that keeps it true.
    const harness = open({ runtime: { shell: true, sandbox_tools: ["read", "grep"] } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    harness.startTurn(SESSION, turnId(1));

    await until(() => harness.spawns.length === 1, "the playpen's own spawn");
    await until(() => harness.of("process_exit").length === 1, "the session to be released");

    const pool = harness.spawns[0]?.args ?? [];
    const plan = await launch(harness, ATTACH);

    expect(plan.state).toBe(LaunchState.Ready);
    expect(pool.slice(0, 2)).toEqual(["--mode", "rpc"]);
    expect(plan.args).toEqual(pool.slice(2));
  });

  it("carries the family's skills, instructions and tool denials", async () => {
    const harness = open({ runtime: { shell: false, sandbox_tools: ["read"] } });
    writeFileSync(join(harness.root, "config", "instructions.md"), "# the family\n");
    mkdirSync(join(harness.root, "config", "skills"), { recursive: true });
    harness.sessionDir(SESSION);

    const plan = await launch(harness, ATTACH);
    const args = plan.args;

    expect(args).not.toContain("--mode");
    expect(args[args.indexOf("--session-id") + 1]).toBe(SESSION);
    expect(args[args.indexOf("--exclude-tools") + 1]).toBe("bash,powershell,edit,write,grep,find,ls");
    expect(args[args.indexOf("--extension") + 1]).toBe(harness.bridgePath);
    expect(args[args.indexOf("--append-system-prompt") + 1]).toBe(
      join(harness.root, "config", "instructions.md"),
    );
    expect(args).toContain("--no-approve");
    expect(args).toContain("--no-extensions");
    expect(args).toContain("--offline");
  });

  it("gives pi the session's own store, its model file and no credential on argv", async () => {
    const harness = open();
    harness.sessionDir(SESSION);

    const plan = await launch(harness, ATTACH);
    const sessionDir = join(harness.root, "sessions", SESSION, "pi");

    expect(plan.cwd).toBe(join(harness.root, "sessions", SESSION));
    expect(plan.env["PI_CODING_AGENT_DIR"]).toBe(sessionDir);
    expect(plan.env["AGENT_FAMILY"]).toBe("chat");
    expect(plan.env["AGENT_SANDBOX"]).toBe(SANDBOX);
    expect(plan.env["AGENT_SESSION"]).toBe(SESSION);
    expect(plan.env["LITELLM_VIRTUAL_KEY"]).toBe(FIXTURE_LITELLM_KEY);
    expect(plan.env["PEP_TOKEN"]).toBe(FIXTURE_PEP_TOKEN);
    expect(JSON.stringify(plan.args)).not.toContain(FIXTURE_LITELLM_KEY);
    expect(JSON.stringify(plan.args)).not.toContain(FIXTURE_PEP_TOKEN);

    // pi resolves no model without this file, and it is per session.
    const models = JSON.parse(readFileSync(join(sessionDir, "models.json"), "utf8")) as {
      providers: Record<string, { models: { id: string }[] }>;
    };
    expect(models.providers["litellm"]?.models[0]?.id).toBe("agent-router");
  });

  it("names no turn, because a terminal has none", async () => {
    // §7 gives a process `AGENT_TURN` and §7.4 a turn file. Neither exists
    // here, and the bridge reads an absent value as unknown and sends no
    // header, which is honest (contract 04 §3.1 rule 4).
    const harness = open();
    harness.sessionDir(SESSION);

    const plan = await launch(harness, ATTACH);

    expect(plan.env["AGENT_TURN"]).toBeUndefined();
    expect(plan.env["AGENT_TURN_FILE"]).toBeUndefined();
  });

  it("runs in the directory --cwd names", async () => {
    const harness = open();
    harness.sessionDir(SESSION);
    const work = join(harness.root, "code-sandbox", "owui-3f2a");
    mkdirSync(work, { recursive: true });

    const plan = await launch(harness, [...ATTACH, "--cwd", work]);

    expect(plan.cwd).toBe(work);
    // The store stays the session's own, wherever the work happens (§7.2).
    expect(plan.env["PI_CODING_AGENT_DIR"]).toBe(join(harness.root, "sessions", SESSION, "pi"));
  });
});

describe("the launcher's fences", () => {
  it("refuses while a live rpc process holds the session", async () => {
    // §7.5. `attendance`'s writer lease is the first fence; this is the second,
    // inside the VM, where the lease cannot reach.
    const harness = open({ piEnv: { [SESSION]: { FAKE_PI_DELAY_MS: "150" } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn(SESSION, turnId(1));

    await until(() => harness.of("event").length > 0, "the turn to start");
    const plan = await launch(harness, ATTACH);

    expect(plan.state).toBe(LaunchState.SessionHeld);
    expect(plan.detail).toContain(SESSION);
  });

  it("starts once the playpen's record shows the session released", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    harness.startTurn(SESSION, turnId(1));

    await until(() => harness.of("process_exit").length === 1, "the reap");
    const plan = await launch(harness, ATTACH);

    expect(plan.state).toBe(LaunchState.Ready);
  });

  it("ignores a record whose pi process is gone", async () => {
    // A SIGKILLed playpen writes no release. The reader is in the same pid
    // namespace, so it tests the pid rather than waiting one out.
    const harness = open();
    harness.sessionDir(SESSION);
    const records = new ProcessRecords(join(harness.root, "control"), SANDBOX);
    records.hold(SESSION, 2147480000);

    const plan = await launch(harness, ATTACH);

    expect(records.holder(SESSION)).toBeNull();
    expect(plan.state).toBe(LaunchState.Ready);
  });

  it("refuses a session store that is not there, unless --new", async () => {
    const harness = open();
    const home = join(harness.root, "sessions", SESSION);

    const missing = await launch(harness, ATTACH);
    expect(missing.state).toBe(LaunchState.NoSuchSession);
    expect(existsSync(home)).toBe(false);

    const made = await launch(harness, [...ATTACH, "--new"]);
    expect(made.state).toBe(LaunchState.Ready);
    expect(existsSync(join(home, "pi"))).toBe(true);
  });

  it("refuses an argument list it cannot use", async () => {
    const harness = open();
    harness.sessionDir(SESSION);

    const cases: readonly (readonly string[])[] = [
      [],
      ["--sandbox", SANDBOX],
      ["--session", SESSION],
      ["--sandbox", "not a sandbox", "--session", SESSION],
      ["--sandbox", SANDBOX, "--session", "../../etc"],
      // Contract 02 §2: a session id has 128 characters at most.
      ["--sandbox", SANDBOX, "--session", `tui-${"a".repeat(125)}`],
      [...ATTACH, "--cwd", "relative/path"],
      [...ATTACH, "--cwd", "/work/../../etc"],
    ];

    for (const argv of cases) {
      expect((await launch(harness, argv)).state).toBe(LaunchState.BadArguments);
    }
  });

  it("refuses when the environment names no mount directory", async () => {
    const harness = open();
    harness.sessionDir(SESSION);
    const plan = await planLaunch({
      argv: ATTACH,
      env: { [SESSIONS_MOUNT_ENV]: join(harness.root, "sessions") },
      bridgePath: harness.bridgePath,
    });

    expect(plan.state).toBe(LaunchState.MountUnset);
    expect(plan.detail).toContain(CONTROL_DIR_ENV);
  });

  it("gives each refusal its own exit code", () => {
    // The TUI door reads the code, never the text. "the session is busy" and
    // "there is no such session" lead an operator to different next steps, so
    // a shell has to tell them apart without parsing a sentence.
    const states = Object.values(LaunchState);
    const codes = states.map((state) => launchExitCode(state));

    expect(launchExitCode(LaunchState.Ready)).toBe(0);
    expect(codes.filter((code) => code === 0)).toHaveLength(1);
    expect(new Set(codes).size).toBe(states.length);
  });
});
