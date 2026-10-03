// Contract 03 §6 rule 5 and §7.1. A held-open pi process reads the family
// config mount ONCE, at start. So a `config_rev` the host raised reaches that
// session only when its process is recycled, and the recycle happens at a turn
// boundary and never mid-turn.
//
// The credential epoch follows the same rule (§12 rule 9) and shares the same
// code path, so the two are asserted side by side here.

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

describe("a stale config_rev", () => {
  it("recycles the held process at the next turn boundary", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-config", turnId(1), { config_rev: "reg-9f21c4" });

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    expect(harness.of("turn_settled")[0]?.resident).toBe(true);
    expect(harness.of("process_exit")).toHaveLength(0);

    // `managerd` rewrote the mount, so the held process holds the old
    // `runtime.json`, the old `instructions.md` and the old skills.
    harness.startTurn("owui-config", turnId(2), { config_rev: "reg-newrev" });

    await until(() => harness.of("process_exit").length === 1, "the recycle");
    expect(harness.of("process_exit")[0]?.reason).toBe("reaped");

    await until(() => harness.of("turn_settled").length === 2, "the turn at the new revision");
    expect(harness.spawns).toHaveLength(2);
  });

  it("reuses the held process while the revision is unchanged", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-same", turnId(1), { config_rev: "reg-9f21c4" });

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    harness.startTurn("owui-same", turnId(2), { config_rev: "reg-9f21c4" });

    await until(() => harness.of("turn_settled").length === 2, "the second turn");
    expect(harness.spawns).toHaveLength(1);
    expect(harness.of("process_exit")).toHaveLength(0);
  });

  it("never recycles mid-turn, whatever the new revision says", async () => {
    // §6 rule 5's last line is absolute: a process running a turn is never
    // reaped. Recycling here would abandon the turn in flight.
    const harness = open({ piEnv: { "owui-busy": { FAKE_PI_DELAY_MS: "120" } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-busy", turnId(1), { config_rev: "reg-9f21c4" });

    await until(() => harness.of("event").length > 0, "the turn to start");
    harness.startTurn("owui-busy", turnId(2), { config_rev: "reg-newrev" });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.of("turn_failed")[0]?.reason).toBe("session_busy_in_sandbox");

    await until(() => harness.of("turn_settled").length === 1, "the first turn", 20000);
    expect(harness.of("process_exit")).toHaveLength(0);
    expect(harness.spawns).toHaveLength(1);
  });

  it("recycles a pre-started process when the revision moved", async () => {
    // §4.7 rule 2's reuse test is §6's, so `open_session` recycles the same way
    // `start_turn` does. One code path, two messages.
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.send({
      type: "open_session",
      session: "owui-pre",
      cwd: harness.cwd("owui-pre"),
      session_dir: harness.sessionDir("owui-pre"),
      env_epoch: 1,
      config_rev: "reg-9f21c4",
    });

    await until(() => harness.of("session_opened").length === 1, "the pre-start");
    expect(harness.of("session_opened")[0]?.resident).toBe(true);

    harness.send({
      type: "open_session",
      session: "owui-pre",
      cwd: harness.cwd("owui-pre"),
      session_dir: harness.sessionDir("owui-pre"),
      env_epoch: 1,
      config_rev: "reg-newrev",
    });

    await until(() => harness.of("process_exit").length === 1, "the recycle");
    await until(() => harness.of("session_opened").length === 2, "the second answer");
    expect(harness.of("session_opened")[1]?.resident).toBe(true);
    expect(harness.spawns).toHaveLength(2);
  });
});
