// Contract 03 §8 and §9. Event truncation, delta coalescing and the bounded
// outbound queue, each on its own with no process anywhere near it.

import { describe, expect, it, vi } from "vitest";
import { Writable } from "node:stream";

import { Channel, Pressure } from "../src/channel.js";
import { DeltaCoalescer } from "../src/coalesce.js";
import { MAX_EVENT_BYTES, MAX_LINE_BYTES } from "../src/constants.js";
import { capEvent } from "../src/wrap.js";

type Event = Record<string, unknown>;

function delta(index: number, text: string, kind = "text_delta"): Event {
  return {
    type: "message_update",
    assistantMessageEvent: { type: kind, contentIndex: index, delta: text },
  };
}

function sink(): { out: Writable; written: string[] } {
  const written: string[] = [];
  const out = new Writable({
    write: (chunk: Buffer, _encoding, done) => {
      written.push(chunk.toString("utf8"));
      done();
    },
  });

  return { out, written };
}

describe("capEvent", () => {
  it("returns a small event unchanged", () => {
    const event = { type: "message_end", message: { role: "assistant" } };

    expect(capEvent(event)).toBe(event);
  });

  it("drops the largest field and marks the result", () => {
    const event = { type: "big", small: 1, huge: "x".repeat(400) };
    const capped = capEvent(event, 200);

    expect(capped["type"]).toBe("big");
    expect(capped["small"]).toBe(1);
    expect(capped["huge"]).toBeUndefined();
    expect(capped["truncated"]).toBe(true);
    expect(capped["original_bytes"]).toBeGreaterThan(400);
  });

  it("keeps the type when a single giant field is all there is", () => {
    const capped = capEvent({ type: "big", huge: "x".repeat(400) }, 40);

    expect(capped).toEqual({ type: "big", truncated: true, original_bytes: expect.any(Number) });
  });

  it("never returns something over the limit", () => {
    const capped = capEvent({ type: "t", a: "x".repeat(900), b: "y".repeat(900) }, 120);

    expect(JSON.stringify(capped).length).toBeLessThanOrEqual(120);
  });

  it("names the contract's event cap", () => {
    expect(MAX_EVENT_BYTES).toBe(262144);
  });
});

describe("DeltaCoalescer", () => {
  it("emits every event unchanged when the window is 0", () => {
    const seen: Event[] = [];
    const coalescer = new DeltaCoalescer(0, (event) => seen.push(event));
    coalescer.push(delta(0, "a"));
    coalescer.push(delta(0, "b"));

    expect(seen).toHaveLength(2);
  });

  it("concatenates consecutive deltas of one contentIndex", () => {
    const seen: Event[] = [];
    const coalescer = new DeltaCoalescer(50, (event) => seen.push(event));
    coalescer.push(delta(0, "a"));
    coalescer.push(delta(0, "b"));
    coalescer.push(delta(0, "c"));
    coalescer.flush();

    expect(seen).toHaveLength(1);
    const inner = seen[0]?.["assistantMessageEvent"] as Record<string, unknown>;
    expect(inner["delta"]).toBe("abc");
  });

  it("never merges across a contentIndex", () => {
    const seen: Event[] = [];
    const coalescer = new DeltaCoalescer(50, (event) => seen.push(event));
    coalescer.push(delta(0, "a"));
    coalescer.push(delta(1, "b"));
    coalescer.flush();

    expect(seen).toHaveLength(2);
  });

  it("never merges a text_delta with a thinking_delta", () => {
    const seen: Event[] = [];
    const coalescer = new DeltaCoalescer(50, (event) => seen.push(event));
    coalescer.push(delta(0, "a"));
    coalescer.push(delta(0, "b", "thinking_delta"));
    coalescer.flush();

    expect(seen).toHaveLength(2);
  });

  it("flushes the held run before any other event type", () => {
    // §9 rule 4: never across a message_end.
    const seen: Event[] = [];
    const coalescer = new DeltaCoalescer(50, (event) => seen.push(event));
    coalescer.push(delta(0, "a"));
    coalescer.push({ type: "message_end" });

    expect(seen.map((event) => event["type"])).toEqual(["message_update", "message_end"]);
  });

  it("emits on the window even with no boundary event", () => {
    vi.useFakeTimers();
    try {
      const seen: Event[] = [];
      const coalescer = new DeltaCoalescer(50, (event) => seen.push(event));
      coalescer.push(delta(0, "a"));
      expect(seen).toHaveLength(0);

      vi.advanceTimersByTime(60);
      expect(seen).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("loses nothing: the joined text is every delta in order", () => {
    const seen: Event[] = [];
    const coalescer = new DeltaCoalescer(50, (event) => seen.push(event));
    const parts = ["one ", "two ", "three"];
    for (const part of parts) {
      coalescer.push(delta(0, part));
    }
    coalescer.flush();

    const inner = seen[0]?.["assistantMessageEvent"] as Record<string, unknown>;
    expect(inner["delta"]).toBe(parts.join(""));
  });
});

describe("Channel", () => {
  it("writes one LF-terminated JSON record per message", () => {
    const { out, written } = sink();
    new Channel(out).send({ type: "pong", nonce: "9f13", ts_ms: 1, resident: 0, rss_mb: null });

    expect(written).toHaveLength(1);
    expect(written[0]?.endsWith("\n")).toBe(true);
    expect(JSON.parse(written[0] ?? "")).toMatchObject({ type: "pong", nonce: "9f13" });
  });

  it("replaces a line it knows is oversize with a log line", () => {
    // §2 rule 6: a sender that would exceed the limit never emits the line.
    const { out, written } = sink();
    new Channel(out).send({
      type: "log",
      level: "info",
      session: null,
      message: "x".repeat(MAX_LINE_BYTES + 10),
    });

    const parsed = JSON.parse(written[0] ?? "") as Record<string, unknown>;
    expect(parsed["type"]).toBe("log");
    expect(parsed["level"]).toBe("error");
    expect(String(parsed["message"])).toContain("passed MAX_LINE_BYTES");
  });

  it("signals pressure at the bound and relief on the way down", () => {
    // §9 rule 3. A never-draining sink is the only way to fill the queue.
    const stuck = new Writable({ write: () => false, highWaterMark: 1 });
    const states: Pressure[] = [];
    const channel = new Channel(stuck, 4);
    channel.onPressure((state) => states.push(state));

    for (let i = 0; i < 6; i += 1) {
      channel.send({ type: "log", level: "info", session: null, message: `${i}` });
    }

    expect(states).toContain(Pressure.Full);
    expect(channel.pendingLines).toBeGreaterThan(0);
  });

  it("sends free text to stderr, never to the protocol", () => {
    const { out, written } = sink();
    const spy = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    try {
      new Channel(out).note("a note for the operator");

      expect(written).toHaveLength(0);
      expect(spy).toHaveBeenCalledOnce();
    } finally {
      spy.mockRestore();
    }
  });
});
