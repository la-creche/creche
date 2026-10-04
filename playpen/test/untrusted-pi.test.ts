// Invariant 12: the agent process is untrusted, and so is every line it
// writes. The playpen serves every session of a family, so one line from one
// pi process must never end it.
//
// Each case here ended the playpen with an exception nothing caught.

import { afterEach, describe, expect, it } from "vitest";

import { Harness, until } from "./harness.js";

function turnId(n: number): string {
  return `01JBQ7WZ0X4T9V6K2H8M3N${String(n).padStart(4, "0")}`;
}

/**
 * Past where `JSON.stringify` overflows the stack: about 6000 levels on
 * Node 22 and Node 24. `JSON.parse` reads this depth on both.
 */
const DEEP = 10_000;

/** A record whose `type` has no usable `toString`, so `String()` throws on it. */
const ODD_TYPE = '{"type":{"toString":1,"valueOf":1}}';

const REQUEST = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";

function deepLine(type: string): string {
  const nest = `${"[".repeat(DEEP)}${"]".repeat(DEEP)}`;

  return `{"type":"${type}","toolCallId":"call-1","partialResult":${nest}}`;
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

describe("a line from an untrusted pi", () => {
  it("is cut to its scalars when it nests too deep, and the turn settles", async () => {
    const line = deepLine("tool_execution_update");
    const harness = open({ piEnv: { "owui-deep": { FAKE_PI_RAW_LINE: line } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-deep", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    const events = harness.of("event");
    const cut = events.find((one) => one.event["type"] === "tool_execution_update");
    expect(cut?.event).toEqual({
      type: "tool_execution_update",
      toolCallId: "call-1",
      truncated: true,
      original_bytes: Buffer.byteLength(line),
    });

    // The cut event still has its place in the sequence (§13 rule 4).
    expect(events.map((one) => one.turn_seq)).toEqual(events.map((_, index) => index + 1));
    expect(harness.exitCode).toBeNull();
  });

  it("settles the turn when the settling line itself nests too deep", async () => {
    // A dropped `agent_settled` would leave the turn to its deadline.
    const harness = open({
      piEnv: { "owui-settle": { FAKE_PI_RAW_LINE: deepLine("agent_settled") } },
    });
    harness.start();
    harness.hello();
    harness.startTurn("owui-settle", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    const settling = harness.of("event").find((one) => one.event["type"] === "agent_settled");
    expect(settling?.event).toMatchObject({ truncated: true });
    expect(harness.of("turn_failed")).toHaveLength(0);
  });

  it("is reported, not thrown on, when its type is not text", async () => {
    const harness = open({ piEnv: { "owui-odd": { FAKE_PI_RAW_BOOT: ODD_TYPE } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.send({
      type: "open_session",
      session: "owui-odd",
      cwd: harness.cwd("owui-odd"),
      session_dir: harness.sessionDir("owui-odd"),
      env_epoch: 1,
      config_rev: "reg-test",
    });

    await until(
      () => harness.of("log").some((one) => one.message.includes("outside a turn")),
      "the report",
    );

    harness.startTurn("owui-odd", turnId(1));
    await until(() => harness.of("turn_settled").length === 1, "the turn");
  });

  it("settles the turn when pi lists an entry that is null", async () => {
    const harness = open({ piEnv: { "owui-null": { FAKE_PI_NULL_ENTRY: "1" } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("owui-null", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the turn");
    expect(harness.of("turn_settled")[0]?.user_entry_id).toBe("e1");

    harness.send({
      type: "get_entries",
      request: REQUEST,
      session: "owui-null",
      cwd: harness.cwd("owui-null"),
      session_dir: harness.sessionDir("owui-null"),
      env_epoch: 1,
      config_rev: "reg-test",
    });

    await until(() => harness.of("entries").length === 1, "the answer");
    expect(harness.of("entries")[0]?.entries.map((one) => one.id)).toEqual(["e1", "e2"]);
  });

  it("fails the turn when pi refuses a prompt with an error that is not text", async () => {
    const harness = open({ piEnv: { "owui-error": { FAKE_PI_ODD_ERROR: "1" } } });
    harness.start();
    harness.hello();
    harness.startTurn("owui-error", turnId(1));

    await until(() => harness.of("turn_failed").length === 1, "the refusal");

    expect(harness.of("turn_failed")[0]).toMatchObject({
      reason: "pi_rejected_prompt",
      message: "pi rejected the prompt",
    });
  });
});
