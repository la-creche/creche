// Each line of the channel is strict JSON. Three places hold the rule:
//
//   pi line ──► boundRecord ──► capEvent ──► Channel.send ──► stdout
//
// `boundRecord` cuts a pi record that the rule refuses. `capEvent` counts
// the bytes of the text that the channel writes. `Channel.send` is the last
// guard: it makes its own texts whole, and it sends no line that the rule
// refuses.

import { Writable } from "node:stream";

import { afterEach, describe, expect, it } from "vitest";

import { Channel } from "../src/channel.js";
import { MAX_LOG_BYTES, MAX_PI_NESTING } from "../src/constants.js";
import { byteLength, cutToBytes } from "../src/framing.js";
import { boundRecord, readResponse } from "../src/pi-record.js";
import type { PlaypenMessage } from "../src/protocol.js";
import { capEvent } from "../src/wrap.js";
import { Harness, until } from "./harness.js";
import { firstBrokenRule } from "./strict-text.js";

type PiRecord = Record<string, unknown>;

/** One half of a surrogate pair, with no partner. */
const HIGH_HALF = "\ud83d";
const LOW_HALF = "\ude00";

/** The same half as pi writes it in a line: an escape of six characters. */
const HIGH_ESCAPE = "\\ud83d";

/** 2^64: the first integer past the range of 64 bits. */
const PAST_U64 = 18446744073709551616;

const SESSION = "owui-strict";
const TURN = "01JBQ7WZ0X4T9V6K2H8M3N0001";

/** `levels` arrays, each one inside the one before. */
function nested(levels: number): unknown {
  let value: unknown = 0;
  for (let level = 0; level < levels; level += 1) {
    value = [value];
  }

  return value;
}

/** A pi line whose `partialResult` is `levels` arrays, each one inside the one before. */
function nestedLine(levels: number): string {
  const arrays = `${"[".repeat(levels)}${"]".repeat(levels)}`;

  return `{"type":"tool_execution_update","toolCallId":"call-1","partialResult":${arrays}}`;
}

function eventLine(event: PiRecord): PlaypenMessage {
  return { type: "event", session: SESSION, turn: TURN, turn_seq: 1, event };
}

/** A channel on a stream of the test, and each line that it wrote. */
function open(): { channel: Channel; lines: () => string[] } {
  const chunks: string[] = [];
  const out = new Writable({
    write: (chunk: Buffer, _encoding, done) => {
      chunks.push(chunk.toString("utf8"));
      done();
    },
  });

  return {
    channel: new Channel(out),
    lines: () => chunks.join("").split("\n").slice(0, -1),
  };
}

/** The one line that a single `send` wrote. */
function sent(message: PlaypenMessage): string {
  const { channel, lines } = open();
  channel.send(message);

  expect(lines()).toHaveLength(1);

  return lines()[0] ?? "";
}

function notStrictLog(rule: string): PlaypenMessage {
  return {
    type: "log",
    level: "error",
    session: null,
    message: `json_not_strict playpen.pi_line ${rule}`,
  };
}

describe("boundRecord", () => {
  it("returns a strict record as it is", () => {
    const record = { type: "message_end", message: { role: "assistant", text: "a\u{1F600}" } };

    expect(boundRecord(record, JSON.stringify(record))).toBe(record);
  });

  it("cuts a record with a lone surrogate to its type and its whole scalars", () => {
    const record = {
      type: "tool_execution_update",
      toolCallId: "call-1",
      count: 7,
      done: false,
      none: null,
      text: `a${HIGH_HALF}`,
      [`k${LOW_HALF}`]: 1,
      partialResult: { text: "ok" },
    };
    const line = JSON.stringify(record);

    expect(boundRecord(record, line)).toEqual({
      type: "tool_execution_update",
      toolCallId: "call-1",
      count: 7,
      done: false,
      none: null,
      truncated: true,
      original_bytes: byteLength(line),
    });
  });

  it("cuts a record that holds a lone surrogate below its first level", () => {
    const record = { type: "message_update", index: 2, message: { parts: [{ text: LOW_HALF }] } };
    const line = JSON.stringify(record);

    expect(boundRecord(record, line)).toEqual({
      type: "message_update",
      index: 2,
      truncated: true,
      original_bytes: byteLength(line),
    });
  });

  it("drops a type that holds a lone surrogate", () => {
    const record = { type: `t${HIGH_HALF}`, id: "c1" };
    const line = JSON.stringify(record);

    expect(boundRecord(record, line)).toEqual({
      id: "c1",
      truncated: true,
      original_bytes: byteLength(line),
    });
  });

  it("keeps a record of 63 levels and cuts one of 64", () => {
    expect(MAX_PI_NESTING).toBe(63);

    const kept = { type: "tool_execution_update", partialResult: nested(62) };
    expect(boundRecord(kept, "")).toBe(kept);

    const cut = { type: "tool_execution_update", partialResult: nested(63) };
    expect(boundRecord(cut, "x".repeat(140))).toEqual({
      type: "tool_execution_update",
      truncated: true,
      original_bytes: 140,
    });
  });

  it("keeps the id of a response whose error text holds a lone surrogate", () => {
    const record = {
      type: "response",
      id: "c4",
      command: "prompt",
      success: false,
      error: `no${HIGH_HALF}`,
    };

    expect(readResponse(boundRecord(record, JSON.stringify(record)))).toEqual({
      type: "response",
      id: "c4",
      command: "prompt",
      success: false,
    });
  });
});

describe("capEvent", () => {
  it("counts the bytes of the text that the channel writes", () => {
    // The digits of 2^64 are 20 bytes. Its form with an exponent is 22.
    const event = { type: "x", n: PAST_U64 };
    const digits = byteLength(JSON.stringify(event));

    expect(capEvent(event, digits + 2)).toBe(event);
    expect(capEvent(event, digits + 1)).toEqual({
      type: "x",
      truncated: true,
      original_bytes: digits + 2,
    });
  });
});

describe("Channel.send", () => {
  it("writes the text of JSON.stringify for a message with no wide integer", () => {
    const message = eventLine({ type: "message_update", delta: "a b \u{1F600}", n: [1, 2.5] });

    expect(sent(message)).toBe(JSON.stringify(message));
  });

  it("writes an integer past 64 bits with an exponent, and keeps its value", () => {
    const event = { type: "usage", big: 1e20, edge: PAST_U64, low: -(2 ** 63), safe: 2 ** 53 };
    const line = sent(eventLine(event));

    expect(line).toContain('"big":1e+20');
    expect(line).toContain('"safe":9007199254740992');
    expect(firstBrokenRule(line)).toBeNull();
    expect(JSON.parse(line)).toEqual(eventLine(event));
  });

  it("writes a line of 64 levels", () => {
    const message = eventLine({ type: "deep", value: nested(62) });

    expect(sent(message)).toBe(JSON.stringify(message));
  });

  it.each<PlaypenMessage>([
    { type: "log", level: "info", session: SESSION, message: `a${HIGH_HALF}b` },
    {
      type: "turn_failed",
      session: SESSION,
      turn: TURN,
      turn_seq: 1,
      reason: "internal",
      message: `a${HIGH_HALF}b`,
    },
    { type: "fatal", reason: "control_mount_unwritable", message: `a${HIGH_HALF}b` },
    {
      type: "session_opened",
      session: SESSION,
      resident: false,
      reason: "internal",
      message: `a${HIGH_HALF}b`,
    },
  ])("puts U+FFFD in the place of a lone surrogate in the text of a $type line", (message) => {
    const line = sent(message);

    expect(firstBrokenRule(line)).toBeNull();
    expect(JSON.parse(line)).toEqual({ ...message, message: "a�b" });
  });

  it("keeps a text of 4,096 bytes whole and inside the limit", () => {
    // A lone surrogate counts as three bytes of UTF-8, so this text fits and
    // `cutToBytes` returns it as it is.
    const text = `${"x".repeat(MAX_LOG_BYTES - 3)}${HIGH_HALF}`;
    const message = cutToBytes(text, MAX_LOG_BYTES);
    expect(message.isWellFormed()).toBe(false);

    const line = sent({ type: "log", level: "warn", session: null, message });
    const written = (JSON.parse(line) as { message: string }).message;

    expect(firstBrokenRule(line)).toBeNull();
    expect(written).toBe(`${"x".repeat(MAX_LOG_BYTES - 3)}�`);
    expect(byteLength(written)).toBe(MAX_LOG_BYTES);
  });

  it("writes no lone surrogate for a text that was cut at 4,096 bytes", () => {
    const text = `${LOW_HALF}${"\u{1F600}".repeat(2000)}${HIGH_HALF}`;
    const line = sent({
      type: "turn_failed",
      session: null,
      turn: null,
      turn_seq: 0,
      reason: "internal",
      message: cutToBytes(text, MAX_LOG_BYTES),
    });
    const written = (JSON.parse(line) as { message: string }).message;

    expect(firstBrokenRule(line)).toBeNull();
    expect(written.isWellFormed()).toBe(true);
    expect(byteLength(written)).toBeLessThanOrEqual(MAX_LOG_BYTES);
  });

  it("sends one log line in the place of a line with a lone surrogate", () => {
    const { channel, lines } = open();
    channel.send({ type: "pong", nonce: HIGH_HALF, ts_ms: 1, resident: 0, rss_mb: null });
    channel.send(eventLine({ type: "note", [HIGH_HALF]: 1 }));

    expect(lines().map((line) => JSON.parse(line) as unknown)).toEqual([
      notStrictLog("lone_surrogate"),
      notStrictLog("lone_surrogate"),
    ]);
  });

  it("sends one log line in the place of a line of 65 levels", () => {
    const line = sent(eventLine({ type: "deep", value: nested(63) }));

    expect(JSON.parse(line)).toEqual(notStrictLog("too_deep"));
  });

  it("stays up for a value of a million levels", () => {
    const line = sent(eventLine({ type: "deep", value: nested(1_000_000) }));

    expect(JSON.parse(line)).toEqual(notStrictLog("too_deep"));
  });
});

const live: Harness[] = [];

/** A playpen whose fake pi writes `line` as one record in each turn. */
function withPiLine(line: string): Harness {
  const harness = new Harness({ piEnv: { [SESSION]: { FAKE_PI_RAW_LINE: line } } });
  live.push(harness);
  harness.start();
  harness.hello();

  return harness;
}

function eventOf(harness: Harness, type: string): PiRecord | undefined {
  return harness.of("event").find((one) => one.event["type"] === type)?.event;
}

/** Each line that the playpen wrote passes the judge of these tests. */
function expectStrictLines(harness: Harness): void {
  expect(harness.rawLines.map(firstBrokenRule)).toEqual(harness.rawLines.map(() => null));
}

afterEach(async () => {
  while (live.length > 0) {
    await live.pop()?.dispose();
  }
});

describe("a pi line that is not strict JSON", () => {
  it("is cut to its type and its whole scalars when a text holds a lone surrogate", async () => {
    const line =
      '{"type":"tool_execution_update","toolCallId":"call-1","count":7,' +
      `"text":"a${HIGH_ESCAPE}"}`;
    const harness = withPiLine(line);
    harness.startTurn(SESSION, TURN);

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(eventOf(harness, "tool_execution_update")).toEqual({
      type: "tool_execution_update",
      toolCallId: "call-1",
      count: 7,
      truncated: true,
      original_bytes: Buffer.byteLength(line),
    });

    // The cut event still has its place in the sequence (§13 rule 4).
    const events = harness.of("event");
    expect(events.map((one) => one.turn_seq)).toEqual(events.map((_, index) => index + 1));
    expectStrictLines(harness);
    expect(harness.of("log").filter((one) => one.level === "error")).toEqual([]);
    expect(harness.exitCode).toBeNull();
  });

  it("settles the turn when the settling line holds a lone surrogate", async () => {
    const harness = withPiLine(`{"type":"agent_settled","note":"${HIGH_ESCAPE}"}`);
    harness.startTurn(SESSION, TURN);

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(eventOf(harness, "agent_settled")).toMatchObject({ truncated: true });
    expect(harness.of("turn_failed")).toHaveLength(0);
  });

  it.each([63, 64])("is cut when it nests %i levels below its first one", async (levels) => {
    const line = nestedLine(levels);
    const harness = withPiLine(line);
    harness.startTurn(SESSION, TURN);

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(eventOf(harness, "tool_execution_update")).toEqual({
      type: "tool_execution_update",
      toolCallId: "call-1",
      truncated: true,
      original_bytes: Buffer.byteLength(line),
    });
    expectStrictLines(harness);
  });

  it("goes out whole when its event makes a line of 64 levels", async () => {
    const line = nestedLine(62);
    const harness = withPiLine(line);
    harness.startTurn(SESSION, TURN);

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    expect(eventOf(harness, "tool_execution_update")).toEqual(JSON.parse(line));
    expectStrictLines(harness);
  });
});

describe("a pi line with an integer past 64 bits", () => {
  it("goes out as a line that the strict rule accepts, with each value kept", async () => {
    const line =
      '{"type":"tool_execution_update","big":100000000000000000000,' +
      '"edge":18446744073709551616,"low":-9223372036854775809,"top":18446744073709551615}';
    expect(firstBrokenRule(line)).toBe("integer_range");

    const harness = withPiLine(line);
    harness.startTurn(SESSION, TURN);

    await until(() => harness.of("turn_settled").length === 1, "the turn");

    const written = harness.rawLines.find((one) => one.includes("tool_execution_update")) ?? "";
    expect(written).toContain('"big":1e+20');
    expect(eventOf(harness, "tool_execution_update")).toEqual(JSON.parse(line));
    expectStrictLines(harness);
  });
});

describe("a line that the playpen cannot make strict", () => {
  it("is not sent, and one log line of level error names the rule", async () => {
    const harness = new Harness();
    live.push(harness);
    harness.start();
    harness.hello();
    harness.sendRaw(`{"type":"ping","nonce":"${HIGH_ESCAPE}"}`);
    harness.send({ type: "ping", nonce: "after" });

    await until(() => harness.of("pong").length === 1, "the second pong");

    expect(harness.of("pong").map((one) => one.nonce)).toEqual(["after"]);
    expect(harness.of("log")).toEqual([notStrictLog("lone_surrogate")]);
    expectStrictLines(harness);
  });
});
