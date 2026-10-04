// Contract 03 §4.8 and §5.8. The host asks a session's pi process what its
// entries say, and gets their text back.
//
// §7.6's launcher hands a terminal straight to pi on the session store, so
// those exchanges never pass the host as turns. `get_entries` is the only way
// the host learns them, and contract 02 §10.5 is what it does with them.

import { afterEach, describe, expect, it } from "vitest";

import { MAX_ENTRY_BYTES } from "../src/constants.js";
import { readEntry } from "../src/entry-text.js";
import { Harness, until } from "./harness.js";

function turnId(n: number): string {
  return `01JBQ7WZ0X4T9V6K2H8M3N${String(n).padStart(4, "0")}`;
}

const REQUEST = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";

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

function getEntries(
  harness: Harness,
  session: string,
  extra: Record<string, unknown> = {},
): void {
  harness.send({
    type: "get_entries",
    request: REQUEST,
    session,
    cwd: harness.cwd(session),
    session_dir: harness.sessionDir(session),
    env_epoch: 1,
    config_rev: "reg-test",
    ...extra,
  });
}

describe("get_entries", () => {
  it("reads a session's entries with their text", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("tui-read", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "one turn");
    getEntries(harness, "tui-read");

    await until(() => harness.of("entries").length === 1, "the answer");

    const answer = harness.of("entries")[0];
    expect(answer).toMatchObject({ request: REQUEST, session: "tui-read", ok: true });
    expect(answer?.entries.map((one) => one.role)).toEqual(["user", "assistant"]);
    // The text is what makes this message different from §5.2's two ids.
    expect(answer?.entries[0]?.text).not.toBe("");
    expect(answer?.leaf_id).toBe(answer?.entries[1]?.id);
  });

  it("starts a pi process when none is resident", async () => {
    const harness = open();
    harness.start();
    // ttl 0 holds nothing between turns, so nothing is resident here.
    harness.hello({ pi_idle_ttl_s: 0 });
    getEntries(harness, "tui-cold");

    await until(() => harness.of("entries").length === 1, "the answer");

    expect(harness.of("entries")[0]?.ok).toBe(true);
    expect(harness.spawns).toHaveLength(1);
  });

  it("reads only what followed the cursor", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("tui-since", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "the first turn");
    harness.startTurn("tui-since", turnId(2));

    await until(() => harness.of("turn_settled").length === 2, "the second turn");
    getEntries(harness, "tui-since", { since: harness.of("turn_settled")[0]?.leaf_id });

    await until(() => harness.of("entries").length === 1, "the answer");

    const answer = harness.of("entries")[0];
    expect(answer?.since_matched).toBe(true);
    expect(answer?.entries).toHaveLength(2);
  });

  it("falls back to the whole history when the cursor is unknown", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("tui-lost", turnId(1));

    await until(() => harness.of("turn_settled").length === 1, "one turn");
    getEntries(harness, "tui-lost", { since: "no-such-entry" });

    await until(() => harness.of("entries").length === 1, "the answer");

    // §4.8 rule 4. pi refuses an unknown cursor, so the playpen retries
    // without one and says which answer this is.
    const answer = harness.of("entries")[0];
    expect(answer?.ok).toBe(true);
    expect(answer?.since_matched).toBe(false);
    expect(answer?.entries).toHaveLength(2);
  });

  it("refuses while a turn runs on that session", async () => {
    const harness = open({
      piEnv: { "tui-busy": { FAKE_PI_EVENTS: "40", FAKE_PI_DELAY_MS: "25" } },
    });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.startTurn("tui-busy", turnId(1));

    await until(() => harness.of("event").length > 0, "the turn to be under way");
    getEntries(harness, "tui-busy");

    await until(() => harness.of("entries").length === 1, "the refusal");

    // §4.8 rule 3. A read never costs a turn.
    expect(harness.of("entries")[0]).toMatchObject({
      ok: false,
      reason: "session_busy_in_sandbox",
    });
    expect(harness.of("turn_failed")).toHaveLength(0);
  });

  it("answers ok false when pi refuses the read outright", async () => {
    const harness = open({ piEnv: { "tui-refused": { FAKE_PI_NO_ENTRIES: "1" } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    getEntries(harness, "tui-refused");

    await until(() => harness.of("entries").length === 1, "the answer");

    const answer = harness.of("entries")[0];
    expect(answer?.ok).toBe(false);
    expect(answer?.entries).toHaveLength(0);
    expect(answer?.leaf_id).toBeNull();
  });

  it("refuses a message with no request id", async () => {
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 900 });
    harness.send({
      type: "get_entries",
      session: "tui-nofield",
      cwd: harness.cwd("tui-nofield"),
      session_dir: harness.sessionDir("tui-nofield"),
      env_epoch: 1,
      config_rev: "reg-test",
    });

    await until(() => harness.of("log").length > 0, "the refusal log");

    expect(harness.of("entries")).toHaveLength(0);
  });
});

describe("the text of one entry", () => {
  it("takes at most MAX_ENTRY_BYTES of UTF-8, and ends on a whole character", () => {
    // §8 gives the cap in bytes. The characters here take 2, 3 and 4 bytes.
    for (const wide of ["\u00e9", "\u20ac", "\u{1F600}"]) {
      for (let lead = 0; lead < 4; lead += 1) {
        const content = "a".repeat(MAX_ENTRY_BYTES - lead) + wide.repeat(4);
        const text = readEntry({ id: "e1", message: { role: "user", content } })?.text ?? "";

        expect(Buffer.byteLength(text, "utf8")).toBeLessThanOrEqual(MAX_ENTRY_BYTES);
        expect(content.startsWith(text)).toBe(true);
      }
    }
  });
});
