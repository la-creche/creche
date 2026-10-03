// Contract 03 end to end: a real Playpen, real child processes, the fake
// pi. Each case is a rule from the contract rather than a property of this
// implementation.

import { afterEach, describe, expect, it } from "vitest";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  EXIT_CONTROL_MOUNT,
  EXIT_DEADLINE,
  EXIT_LOCK_HELD,
  EXIT_PROTOCOL,
} from "../src/playpen.js";
import { MAX_LINE_BYTES } from "../src/constants.js";
import type { EventMessage } from "../src/protocol.js";
import { Harness, until } from "./harness.js";

/** A ULID is 26 characters of Crockford base 32. These are fixtures, not ids. */
function turnId(n: number): string {
  return `01JBQ7WZ0X4T9V6K2H8M3N${String(n).padStart(4, "0")}`;
}

const live: Harness[] = [];

function open(options: ConstructorParameters<typeof Harness>[0] = {}): Harness {
  const harness = new Harness(options);
  live.push(harness);

  return harness;
}

/** Every event of one turn, in the order the channel carried them. */
function eventsOf(harness: Harness, turn: string): EventMessage[] {
  return harness.of("event").filter((event) => event.turn === turn);
}

afterEach(async () => {
  while (live.length > 0) {
    await live.pop()?.dispose();
  }
});

describe("the handshake", () => {
  it("sends ready first and sends nothing before it", async () => {
    const harness = open();
    harness.start();

    await until(() => harness.lines.length > 0, "ready");
    const ready = harness.of("ready")[0];

    expect(harness.lines[0]?.type).toBe("ready");
    expect(ready?.protocol).toBe("1.0");
    expect(ready?.sandbox).toBe("chat-s1");
    expect(ready?.node).toBe(process.versions.node);
    expect(ready?.caps).toContain("steer");
  });

  it("closes the channel on a major version mismatch", async () => {
    const harness = open();
    harness.start();
    harness.hello({ protocol: "2.0" });

    await until(() => harness.exitCode !== null, "the protocol refusal");
    expect(harness.exitCode).toBe(EXIT_PROTOCOL);
  });

  it("accepts a minor version difference", async () => {
    const harness = open();
    harness.start();
    harness.hello({ protocol: "1.7" });
    harness.startTurn("owui-minor", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "a turn under 1.7");
    expect(harness.exitCode).toBeNull();
  });

  it("refuses a hello that names another sandbox", async () => {
    // §3 rule 3. This catches a stale sbx exec left by a sandbox switch.
    const harness = open();
    harness.start();
    harness.hello({ sandbox: "chat-s9" });

    await until(() => harness.exitCode !== null, "the sandbox refusal");
    expect(harness.exitCode).toBe(EXIT_PROTOCOL);
  });

  it("ignores any host message that arrives before hello", async () => {
    const harness = open();
    harness.start();
    harness.startTurn("owui-early", turnId(1));

    await until(() => harness.of("log").length > 0, "the refusal log");
    expect(harness.of("log")[0]?.message).toContain("before hello");
    expect(harness.of("turn_settled")).toHaveLength(0);
  });
});

describe("the lock", () => {
  it("refuses to start while a live playpen holds it", async () => {
    // §11.4 rule 4. Two playpens in one VM cannot be sorted out afterwards:
    // there is no ingress into a sandbox.
    //
    // The holder's pid must be a live process that is not this one, because a
    // lock naming our own pid is our own lock. The parent is both.
    const harness = open();
    const record = {
      pid: process.ppid,
      sandbox: "chat-s1",
      started_at: new Date().toISOString(),
      host_deadline_s: 90,
    };
    writeFileSync(join(harness.root, "control", "supervisor.lock"), JSON.stringify(record));

    expect(harness.start()).toBe(false);
    expect(harness.exitCode).toBe(EXIT_LOCK_HELD);
    expect(harness.of("ready")).toHaveLength(0);
  });

  it("starts when the lock names a pid that is gone", async () => {
    // A stale lock only shortens the wait. It never grants one, and it never
    // blocks a playpen whose predecessor really did die.
    const harness = open();
    const record = {
      pid: 0x7ffffff,
      sandbox: "chat-s1",
      started_at: new Date().toISOString(),
      host_deadline_s: 90,
    };
    writeFileSync(join(harness.root, "control", "supervisor.lock"), JSON.stringify(record));

    expect(harness.start()).toBe(true);
    expect(harness.of("ready")).toHaveLength(1);
  });

  it("starts when the lock file is malformed", async () => {
    const harness = open();
    writeFileSync(join(harness.root, "control", "supervisor.lock"), "{not json");

    expect(harness.start()).toBe(true);
    expect(harness.of("ready")).toHaveLength(1);
  });

  it("writes its pid, sandbox and deadline where the host reads them", async () => {
    const harness = open();
    harness.start();

    const record = readLock(harness);

    expect(record["pid"]).toBe(process.pid);
    expect(record["sandbox"]).toBe("chat-s1");
    expect(record["host_deadline_s"]).toBe(90);
    expect(record["beat"]).toBe(1);
  });

  it("raises the beat counter while it serves", async () => {
    // §11.1 rule 3. The counter is the only thing the host can time: the pid
    // is in the VM's namespace and the two clocks are not the same clock.
    const harness = open({ lockBeatMs: 20 });
    harness.start();

    const first = readLock(harness)["beat"] as number;
    await until(() => (readLock(harness)["beat"] as number) >= first + 3, "three beats");

    expect(readLock(harness)["lock_beat_s"]).toBeCloseTo(0.02);
  });

  it("removes the lock as it exits, so the host never waits", async () => {
    // §11.1 rule 3 and §11.4 rule 4, step 1: an absent file means dial now.
    const harness = open({ lockBeatMs: 20 });
    harness.start();
    harness.hello();

    await harness.dispose();
    live.pop();

    expect(existsSync(join(harness.root, "control", "supervisor.lock"))).toBe(false);
  });

  it("answers an unwritable control mount with fatal, then exits", async () => {
    // `mkdirSync` under a FILE fails with ENOTDIR on every platform, and
    // needs no chmod, which a suite running as root could not rely on.
    const harness = open({ controlDir: unwritableDir() });

    expect(harness.start()).toBe(false);
    expect(harness.exitCode).toBe(EXIT_CONTROL_MOUNT);

    const fatal = harness.of("fatal")[0];

    expect(fatal?.reason).toBe("control_mount_unwritable");
    expect(fatal?.message).toContain("playpen lock");
    // §5.7 rule 3: the cause is a mount, so it can precede `ready`.
    expect(harness.of("ready")).toHaveLength(0);
  });
});

/** The lock file the playpen is writing, parsed. */
function readLock(harness: Harness): Record<string, unknown> {
  const path = join(harness.root, "control", "supervisor.lock");

  return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
}

/** A control path whose parent is a file. Every write under it is ENOTDIR. */
function unwritableDir(): string {
  const file = join(mkdtempSync(join(tmpdir(), "playpen-nomount-")), "not-a-dir");
  writeFileSync(file, "");

  return join(file, "control");
}

describe("turns", () => {
  it("runs five concurrent sessions with no loss and gapless order", async () => {
    // Probe 0a, A1: 5 concurrent sessions over one channel, 0 gaps, 0
    // duplicates, 0 unparsable lines. coalesce_ms 0 so every delta is its own
    // event and the count is exact.
    const harness = open();
    harness.start();
    harness.hello({ coalesce_ms: 0 });

    const sessions = ["owui-a", "owui-b", "owui-c", "owui-d", "owui-e"];
    sessions.forEach((session, index) => harness.startTurn(session, turnId(index + 1)));

    await until(() => harness.of("turn_settled").length === 5, "five settled turns", 20000);

    for (const [index, session] of sessions.entries()) {
      const turn = turnId(index + 1);
      const events = eventsOf(harness, turn);
      const settled = harness.of("turn_settled").find((line) => line.turn === turn);

      // The fake emits 6 deltas plus 7 other events, then agent_settled.
      expect(events.length).toBeGreaterThanOrEqual(13);
      expect(events.every((event) => event.session === session)).toBe(true);

      const seqs = events.map((event) => event.turn_seq);
      expect(seqs).toEqual(seqs.map((_, at) => at + 1));
      expect(settled?.turn_seq).toBe(seqs.length + 1);
    }
  });

  it("holds the process open and answers the second prompt on it", async () => {
    // §6 rule 4. Probe 0a, A3: 4 ms against 1195 ms. The observable part here
    // is that no process exits between the two turns.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-held", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    expect(harness.of("turn_settled")[0]?.resident).toBe(true);

    harness.send({
      type: "prompt",
      turn: turnId(2),
      session: "owui-held",
      prompt: "and the night before?",
      deadline_s: 30,
      env_epoch: 1,
    });

    await until(() => harness.of("turn_settled").length === 2, "the held-open second turn");
    expect(harness.of("process_exit")).toHaveLength(0);
  });

  it("fails a prompt with no resident process instead of starting one", async () => {
    // §4.2. prompt is an optimisation; start_turn is always correct.
    const harness = open();
    harness.start();
    harness.hello();
    harness.send({
      type: "prompt",
      turn: turnId(1),
      session: "owui-cold",
      prompt: "hello?",
      deadline_s: 30,
      env_epoch: 1,
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.of("turn_failed")[0]?.reason).toBe("no_resident_process");
  });

  it("reports the entry ids the host needs to map a message", async () => {
    // §5.2. attendance maps an Open WebUI message to a pi entry with these two.
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-entries", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    const settled = harness.of("turn_settled")[0];

    expect(settled?.user_entry_id).toBe("e1");
    expect(settled?.leaf_id).toBe("e2");
    expect(settled?.usage.output).toBeGreaterThan(0);
  });

  it("reports entry_count null when the cursor matched no entry", async () => {
    const harness = open({ piEnv: { "owui-noentries": { FAKE_PI_NO_ENTRIES: "1" } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-noentries", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    const settled = harness.of("turn_settled")[0];

    expect(settled?.entry_count).toBeNull();
    expect(settled?.user_entry_id).toBeNull();
  });

  it("settles a prompt pi handled without starting a run", async () => {
    // pi 0.99 answers `disposition: "handled"` when an extension consumed the
    // prompt. No run starts, so no `agent_settled` ever arrives to settle it.
    const harness = open({ piEnv: { "owui-handled": { FAKE_PI_HANDLED: "1" } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-handled", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    expect(harness.of("turn_failed")).toHaveLength(0);
  });

  it("refuses a second turn on a session already running one", async () => {
    // §6 rule 2. attendance's writer lease should prevent it; we still check.
    const harness = open({ piEnv: { "owui-busy": { FAKE_PI_DELAY_MS: "80" } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-busy", turnId(1));

    await until(() => eventsOf(harness, turnId(1)).length > 0, "the first turn to start");
    harness.startTurn("owui-busy", turnId(2));

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.of("turn_failed")[0]?.reason).toBe("session_busy_in_sandbox");
  });
});

describe("steer and abort", () => {
  it("passes a steering message into the running turn", async () => {
    const harness = open({ piEnv: { "owui-steer": { FAKE_PI_DELAY_MS: "60" } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-steer", turnId(1));

    await until(() => eventsOf(harness, turnId(1)).length > 0, "the turn to start");
    harness.send({
      type: "steer",
      session: "owui-steer",
      turn: turnId(1),
      message: "check the logbook first",
    });

    await until(() => harness.of("turn_settled").length === 1, "the steered turn");
    const settled = eventsOf(harness, turnId(1)).at(-1);

    expect(settled?.event["steered"]).toBe("check the logbook first");
  });

  it("aborts the turn and keeps the process", async () => {
    // §4.4 and contract 02 §5.7: the process stays for the next turn.
    const harness = open({ piEnv: { "owui-abort": { FAKE_PI_DELAY_MS: "80" } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-abort", turnId(1));

    await until(() => eventsOf(harness, turnId(1)).length > 0, "the turn to start");
    harness.send({ type: "abort", session: "owui-abort", turn: turnId(1) });

    await until(() => harness.of("turn_settled").length === 1, "the aborted turn to settle");
    expect(harness.of("process_exit")).toHaveLength(0);
  });
});

describe("a process that dies", () => {
  it("fails only its own turn and reports process_exit", async () => {
    // Probe 0a, A4: one pi process killed mid-turn, the other turns settled.
    const harness = open({
      piEnv: { "owui-doomed": { FAKE_PI_DIE_AT: "2", FAKE_PI_DELAY_MS: "30" } },
    });
    harness.start();
    harness.hello();

    const sessions = ["owui-doomed", "owui-ok1", "owui-ok2", "owui-ok3", "owui-ok4"];
    sessions.forEach((session, index) => harness.startTurn(session, turnId(index + 1)));

    await until(() => harness.of("turn_settled").length === 4, "the four healthy turns", 20000);
    await until(() => harness.of("process_exit").length === 1, "the exit report");

    const exit = harness.of("process_exit")[0];
    expect(exit?.session).toBe("owui-doomed");
    expect(exit?.reason).toBe("crashed");
    expect(exit?.turn).toBe(turnId(1));

    const failed = harness.of("turn_failed");
    expect(failed).toHaveLength(1);
    expect(failed[0]?.reason).toBe("process_died");
    expect(failed[0]?.session).toBe("owui-doomed");
  });
});

describe("liveness", () => {
  it("answers ping with the nonce and the resident count", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("owui-ping", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    harness.send({ type: "ping", nonce: "9f13" });

    await until(() => harness.of("pong").length === 1, "the pong");
    const pong = harness.of("pong")[0];

    expect(pong?.nonce).toBe("9f13");
    expect(pong?.resident).toBe(1);
  });

  it("kills every process and exits when no ping arrives in time", async () => {
    // §11.1. Probe 0a, A5 refuted: a host-side kill does not reach the in-VM
    // process, so stdin EOF is not a signal the playpen may wait for.
    const harness = open();
    harness.start();
    harness.hello({ host_deadline_s: 1 });
    harness.startTurn("owui-deadline", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    await until(() => harness.exitCode !== null, "the deadline", 4000);
    expect(harness.exitCode).toBe(EXIT_DEADLINE);

    // The kill is a signal, so the exit report arrives after it, not with it.
    await until(() => harness.of("process_exit").length === 1, "the exit report");
    expect(harness.of("process_exit")[0]?.signal).toBe("SIGKILL");
  });

  it("keeps running while pings keep arriving", async () => {
    const harness = open();
    harness.start();
    harness.hello({ host_deadline_s: 1 });

    for (let i = 0; i < 6; i += 1) {
      harness.send({ type: "ping", nonce: `n${i}` });
      await new Promise((resolve) => setTimeout(resolve, 250));
    }

    expect(harness.exitCode).toBeNull();
    expect(harness.of("pong")).toHaveLength(6);
  });
});

describe("ending the channel", () => {
  it("stops every session on shutdown and exits 0", async () => {
    // §4.6. shutdown runs §4.5 for every session, then the playpen exits.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });

    for (const [index, session] of ["owui-x", "owui-y"].entries()) {
      harness.startTurn(session, turnId(index + 1));
      await until(() => harness.of("turn_settled").length === index + 1, `turn ${index + 1}`);
    }

    harness.send({ type: "shutdown", grace_ms: 500 });

    await until(() => harness.exitCode !== null, "the exit");
    expect(harness.exitCode).toBe(0);

    await until(() => harness.of("process_exit").length === 2, "both processes");
    expect(harness.of("process_exit").map((exit) => exit.reason)).toEqual(["shutdown", "shutdown"]);
  });

  it("ends the same way on stdin EOF", async () => {
    // §11.1. EOF is a real end when it arrives. It is simply not one the
    // playpen may wait for, which is what the deadline covers.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-eof", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    harness.endStdin();

    await until(() => harness.exitCode !== null, "the exit");
    expect(harness.exitCode).toBe(0);
    await until(() => harness.of("process_exit").length === 1, "the process to go");
  });
});

describe("untrusted input", () => {
  it("refuses an oversize line and stays usable", async () => {
    // Probe 0a, A8: one byte over was refused and the channel stayed usable.
    const harness = open();
    harness.start();
    harness.hello();
    harness.sendRaw("x".repeat(MAX_LINE_BYTES + 1));

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.of("turn_failed")[0]?.reason).toBe("line_too_large");

    harness.startTurn("owui-after-big", turnId(1));
    await until(() => harness.of("turn_settled").length === 1, "a turn after the refusal");
  });

  it("does not crash on a malformed command", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.sendRaw("not json at all");
    harness.sendRaw("[1,2,3]");
    harness.send({ type: "nonsense" });
    harness.send({ type: "start_turn" });

    await until(() => harness.of("log").length >= 4, "four refusals");

    harness.startTurn("owui-after-junk", turnId(1));
    await until(() => harness.of("turn_settled").length === 1, "a turn after the junk");
    expect(harness.exitCode).toBeNull();
  });

  it("fails the turn a malformed command still addressed", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.send({
      type: "start_turn",
      turn: turnId(1),
      session: "owui-bad-deadline",
      cwd: harness.cwd("owui-bad-deadline"),
      session_dir: harness.sessionDir("owui-bad-deadline"),
      prompt: "hello",
      deadline_s: -5,
      env_epoch: 1,
      config_rev: "reg-test",
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    const failed = harness.of("turn_failed")[0];

    expect(failed?.session).toBe("owui-bad-deadline");
    expect(failed?.turn).toBe(turnId(1));
    expect(failed?.message).toContain("deadline_s");
  });
});
