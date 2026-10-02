// Contract 03 §4.7 and §6 rule 9. `open_session` is `start_turn` without the
// prompt: it pays pi's cold start before the user types.
//
// Probe 0a is the whole argument. A cold pi process reached its first event in
// 1195 ms, and a held-open one in 4 ms (A2, A3). The first message of a chat
// should not pay the difference when the door already knows the session.

import { afterEach, describe, expect, it } from "vitest";

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

function openSession(
  harness: Harness,
  session: string,
  extra: Record<string, unknown> = {},
): void {
  harness.send({
    type: "open_session",
    session,
    cwd: harness.cwd(session),
    session_dir: harness.sessionDir(session),
    env_epoch: 1,
    config_rev: "reg-test",
    ...extra,
  });
}

describe("open_session", () => {
  it("starts the pi process and acknowledges it", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "owui-pre");

    await until(() => harness.of("session_opened").length === 1, "the acknowledgement");

    expect(harness.of("session_opened")[0]).toMatchObject({
      session: "owui-pre",
      resident: true,
      reason: null,
    });
    expect(harness.spawns).toHaveLength(1);
  });

  it("leaves the process for a later start_turn to find held open", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "owui-warm");

    await until(() => harness.of("session_opened").length === 1, "the pre-start");
    harness.startTurn("owui-warm", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the first turn");

    // One process served both messages: the turn paid no cold start.
    expect(harness.spawns).toHaveLength(1);
    expect(harness.of("turn_settled")[0]?.resident).toBe(true);
  });

  it("is a no-op that succeeds for a session that already has a process", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "owui-twice");

    await until(() => harness.of("session_opened").length === 1, "the first open");
    openSession(harness, "owui-twice");

    await until(() => harness.of("session_opened").length === 2, "the second open");

    expect(harness.of("session_opened")[1]?.resident).toBe(true);
    expect(harness.spawns).toHaveLength(1);
  });

  it("counts toward the cap on held-open processes", async () => {
    // §6 rule 5, second bullet. A pre-started process is a held process and
    // costs the same 155 to 170 MB (probe 0a, A7).
    const harness = open();
    harness.start();
    harness.hello({ max_resident_processes: 2, pi_idle_ttl_s: 900 });

    for (const session of ["owui-a", "owui-b"]) {
      openSession(harness, session);
      await until(
        () => harness.of("session_opened").some((line) => line.session === session),
        `opening ${session}`,
      );
    }

    expect(harness.of("process_exit")).toHaveLength(0);
    openSession(harness, "owui-c");

    await until(() => harness.of("process_exit").length === 1, "the cap reap");

    expect(harness.of("process_exit")[0]?.session).toBe("owui-a");
  });

  it("is reaped when it sits idle past the ttl", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 1 });
    openSession(harness, "owui-idle-pre");

    await until(() => harness.of("session_opened").length === 1, "the pre-start");

    await until(() => harness.of("process_exit").length === 1, "the idle reap", 6000);
    expect(harness.of("process_exit")[0]?.reason).toBe("reaped");
  });

  it("starts nothing for a family that holds no process between turns", async () => {
    // §6 rule 4's thin and autonomous default. There is no hold to warm, and
    // a process with no turn would never be reaped.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    openSession(harness, "job-thin");

    await until(() => harness.of("session_opened").length === 1, "the answer");

    expect(harness.of("session_opened")[0]?.resident).toBe(false);
    expect(harness.of("session_opened")[0]?.reason).toBe("not_held");
    expect(harness.spawns).toHaveLength(0);
  });

  it("reports a credential epoch it cannot reach", async () => {
    const harness = open({ epoch: 1 });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "owui-stale", { env_epoch: 99 });

    await until(() => harness.of("session_opened").length === 1, "the answer", 4000);

    expect(harness.of("session_opened")[0]?.resident).toBe(false);
    expect(harness.of("session_opened")[0]?.reason).toBe("stale_credentials");
    expect(harness.spawns).toHaveLength(0);
  });

  it("gives the process no turn id, because no turn exists yet", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "owui-noturn");

    await until(() => harness.spawns.length === 1, "the spawn");

    expect(harness.spawns[0]?.env["AGENT_TURN"]).toBeUndefined();
    expect(harness.spawns[0]?.env["AGENT_SESSION"]).toBe("owui-noturn");
  });

  it("takes the workspace, so the first turn does not replace the process", async () => {
    // Contract 03 §4.7 rule 6. A process opened without the workspace would
    // have the wrong cwd and the first turn would reap it, which is worse than
    // not pre-starting at all.
    const owner = "owui-3f2a9c41";
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "job-code", {
      workspace: { kind: "code-sandbox", owner_session: owner },
    });

    await until(() => harness.of("session_opened").length === 1, "the pre-start");
    harness.startTurn("job-code", turnId(1), {
      workspace: { kind: "code-sandbox", owner_session: owner },
    });

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    expect(harness.spawns).toHaveLength(1);
  });

  it("lets a start_turn that catches the pre-start mid-flight wait for it", async () => {
    // §4.7 rules 8 to 11. The host sends `open_session` and does NOT wait for
    // `session_opened`, so the first turn of a chat can arrive while the
    // process is still spawning. It must run on that process, not fail with
    // `session_busy_in_sandbox`: an optimisation may never break a turn.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });

    openSession(harness, "owui-race");
    harness.startTurn("owui-race", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the raced turn");

    expect(harness.of("turn_failed")).toHaveLength(0);
    expect(harness.spawns).toHaveLength(1);
  });

  it("logs a malformed open_session and starts nothing", async () => {
    // §4.7: it is an optimisation, exactly as §4.2's `prompt` is. A host that
    // hears nothing still sends `start_turn`, which is always correct.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    openSession(harness, "owui-bad", { cwd: "/work/../escape" });

    await until(
      () => harness.of("log").some((line) => line.level === "error"),
      "the refusal",
    );

    expect(harness.of("session_opened")).toHaveLength(0);
    expect(harness.spawns).toHaveLength(0);
  });
});
