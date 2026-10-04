// Contract 03 §2. The record layer, and the one defect it exists to prevent.

import { describe, expect, it } from "vitest";

import { MAX_LINE_BYTES } from "../src/constants.js";
import { byteLength, cutToBytes, LineReader } from "../src/framing.js";

function collect(maxBytes?: number): {
  reader: LineReader;
  records: string[];
  oversize: number[];
} {
  const records: string[] = [];
  const oversize: number[] = [];
  const reader = new LineReader(
    (line) => records.push(line),
    (bytes) => oversize.push(bytes),
    maxBytes,
  );

  return { reader, records, oversize };
}

describe("LineReader", () => {
  it("splits on LF and on nothing else", () => {
    const { reader, records } = collect();
    reader.feed('{"a":1}\n{"b":2}\n');

    expect(records).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("keeps U+2028 and U+2029 inside a record", () => {
    // §2 rule 4. Node's readline also splits on these two, and both are legal
    // inside a JSON string, so a readline-based reader turns this one legal pi
    // delta into three unparsable fragments.
    const { reader, records } = collect();
    const delta = JSON.stringify({ delta: "a b c" });
    reader.feed(`${delta}\n`);

    expect(records).toHaveLength(1);
    expect(JSON.parse(records[0] ?? "{}")).toEqual({ delta: "a b c" });
  });

  it("strips one trailing CR and no more", () => {
    const { reader, records } = collect();
    reader.feed("one\r\ntwo\r\r\n");

    expect(records).toEqual(["one", "two\r"]);
  });

  it("joins a record split across chunks", () => {
    const { reader, records } = collect();
    reader.feed('{"long":"a');
    reader.feed('bc"}\n');

    expect(records).toEqual(['{"long":"abc"}']);
  });

  it("measures the limit in UTF-8 bytes, not characters", () => {
    const { reader, records, oversize } = collect(8);
    // Four 3-byte characters are 12 bytes, though only 4 characters.
    reader.feed("日本語だ\n");

    expect(records).toEqual([]);
    expect(oversize).toEqual([12]);
  });

  it("refuses an oversize record and resynchronises at the next one", () => {
    // Probe 0a, A8: one byte over was refused and the channel stayed usable.
    const { reader, records, oversize } = collect(10);
    reader.feed(`${"x".repeat(11)}\nshort\n`);

    expect(oversize).toEqual([11]);
    expect(records).toEqual(["short"]);
  });

  it("does not grow its buffer on a multi-chunk oversize record", () => {
    const { reader, records, oversize } = collect(10);
    reader.feed("x".repeat(9));
    reader.feed("x".repeat(9));
    reader.feed("\nafter\n");

    expect(oversize).toHaveLength(1);
    expect(records).toEqual(["after"]);
  });

  it("drops empty records", () => {
    const { reader, records } = collect();
    reader.feed("\n\nreal\n\n");

    expect(records).toEqual(["real"]);
  });

  it("flushes a trailing record at end of stream", () => {
    const { reader, records } = collect();
    reader.feed("tail");
    reader.end();

    expect(records).toEqual(["tail"]);
  });
});

describe("byteLength", () => {
  it("counts UTF-8 bytes", () => {
    expect(byteLength("abc")).toBe(3);
    expect(byteLength("日")).toBe(3);
  });

  it("names the contract's limit", () => {
    expect(MAX_LINE_BYTES).toBe(1048576);
  });
});

describe("cutToBytes", () => {
  // Contract 03 §8 gives each cap in bytes. One character of UTF-8 takes
  // 1 to 4 bytes, and a character past U+FFFF is two units of a JS string.
  const WIDE = "\u{1F600}";

  it("returns a text that fits as it is", () => {
    const text = `caf\u00e9 ${WIDE}`;

    expect(cutToBytes(text, byteLength(text))).toBe(text);
    expect(cutToBytes("", 8)).toBe("");
  });

  it("counts bytes, not units of the string", () => {
    expect(cutToBytes("a".repeat(5000), 4096)).toBe("a".repeat(4096));
    expect(cutToBytes("\u00e9".repeat(5000), 4096)).toBe("\u00e9".repeat(2048));
    expect(cutToBytes("\u20ac".repeat(5000), 4096)).toBe("\u20ac".repeat(1365));
    expect(cutToBytes(WIDE.repeat(5000), 4096)).toBe(WIDE.repeat(1024));
  });

  it("never splits a character", () => {
    for (let lead = 0; lead < 4; lead += 1) {
      const cut = cutToBytes("a".repeat(lead) + WIDE.repeat(8), 10);
      const whole = Math.floor((10 - lead) / 4);

      expect(cut).toBe("a".repeat(lead) + WIDE.repeat(whole));
      expect(byteLength(cut)).toBeLessThanOrEqual(10);
    }
  });

  it("answers the empty text when no character fits", () => {
    expect(cutToBytes(WIDE, 3)).toBe("");
    expect(cutToBytes("abc", 0)).toBe("");
  });
});
