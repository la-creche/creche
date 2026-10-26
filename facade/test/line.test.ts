// The writer of stdout: a strict text under the cap, or a refusal that
// never reaches the wire as it is.

import { describe, expect, it } from "vitest";

import { strictWriter } from "../src/acp.js";
import { byteLength, cutToBytes, encodeLine } from "../src/line.js";
import { checkValue, DEPTH_MAX, Rule } from "../src/strict-json.js";
import { MAX_LINE_BYTES } from "../src/vectors.js";

const LONE = "\ud800";
const WIDE = "\u{1F600}";

function nest(levels: number): unknown {
  let value: unknown = 0;
  for (let at = 0; at < levels; at += 1) {
    value = [value];
  }

  return value;
}

function written(): { lines: string[]; said: string[]; write: ReturnType<typeof strictWriter> } {
  const lines: string[] = [];
  const said: string[] = [];

  return { lines, said, write: strictWriter((line) => lines.push(line), (text) => said.push(text)) };
}

describe("encodeLine", () => {
  it("writes one strict text and its LF", () => {
    expect(encodeLine({ a: [1, "b"] })).toEqual({ ok: true, line: '{"a":[1,"b"]}\n' });
  });

  it("refuses a lone surrogate, a nesting past the limit and a value with no text", () => {
    expect(encodeLine({ text: LONE })).toEqual({ ok: false, refusal: { reason: "not_strict", rule: Rule.LoneSurrogate } });
    expect(encodeLine(nest(DEPTH_MAX + 1))).toEqual({ ok: false, refusal: { reason: "not_strict", rule: Rule.TooDeep } });
    expect(encodeLine(undefined)).toEqual({ ok: false, refusal: { reason: "not_strict", rule: Rule.Syntax } });
  });

  it("refuses a text one byte over the cap", () => {
    const fits = "a".repeat(MAX_LINE_BYTES - 2);

    expect(encodeLine(fits).ok).toBe(true);
    expect(encodeLine(`${fits}a`)).toEqual({ ok: false, refusal: { reason: "over_cap", bytes: MAX_LINE_BYTES + 1 } });
  });

  it("writes an integer past 64 bits with an exponent", () => {
    expect(encodeLine(2 ** 64)).toEqual({ ok: true, line: "1.8446744073709552e+19\n" });
  });
});

describe("strictWriter", () => {
  it("answers a refused answer with an error of the same id", () => {
    const { lines, said, write } = written();
    write({ jsonrpc: "2.0", id: 4, result: { text: LONE } });
    const answer = JSON.parse(lines[0] ?? "") as { id: number; error: { code: number; message: string } };

    expect(lines).toHaveLength(1);
    expect(answer.id).toBe(4);
    expect(answer.error.code).toBe(-32603);
    expect(answer.error.message).toContain(Rule.LoneSurrogate);
    expect(said).toHaveLength(1);
  });

  it("drops a refused notification and says so on stderr", () => {
    const { lines, said, write } = written();
    write({ jsonrpc: "2.0", method: "session/update", params: { text: "a".repeat(MAX_LINE_BYTES) } });

    expect(lines).toEqual([]);
    expect(said[0]).toContain("over the");
  });

  it("writes every accepted line as a strict text", () => {
    const { lines, write } = written();
    write({ jsonrpc: "2.0", id: "x", result: {} });

    expect(lines.map((line) => checkValue(JSON.parse(line)))).toEqual([null]);
  });
});

describe("cutToBytes", () => {
  it("returns a text that fits as it is", () => {
    const text = `café ${WIDE}`;

    expect(cutToBytes(text, byteLength(text))).toBe(text);
  });

  it("counts bytes and never splits a character", () => {
    expect(cutToBytes("é".repeat(5000), 4096)).toBe("é".repeat(2048));
    expect(cutToBytes(WIDE.repeat(5000), 4096)).toBe(WIDE.repeat(1024));
    expect(cutToBytes(`a${WIDE}`, 4)).toBe("a");
    expect(cutToBytes(WIDE, 3)).toBe("");
  });
});
