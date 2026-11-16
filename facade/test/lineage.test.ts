// The lineage file of a rewind: what reads, what the start refuses, and the
// transcript that the model reads.

import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { MAX_TRANSCRIPT_OUTPUT_BYTES, transcriptOf } from "../src/kept.js";
import type { Kept } from "../src/kept.js";
import { instanceOf, LINEAGE_NAME, parseLineage, readLineage, writeLineage } from "../src/lineage.js";

const KEPT: Kept[] = [
  { id: "u1", role: "user", parts: [{ type: "text", text: "Echo, please." }] },
  {
    id: "a1",
    role: "assistant",
    parts: [
      { type: "reasoning", text: "Think first." },
      { type: "tool", toolCallId: "call-1", toolName: "echo", input: {}, outcome: { state: "done", output: "echoed" } },
      { type: "text", text: "All done." },
    ],
  },
];

function fileIn(): string {
  return join(mkdtempSync(join(tmpdir(), "facade-lineage-")), LINEAGE_NAME);
}

function bytes(text: string): Uint8Array {
  return Buffer.from(text);
}

describe("the lineage file", () => {
  it("reads generation 0 when no file is there, and reads back what it wrote", async () => {
    const file = fileIn();
    expect(await readLineage(file)).toEqual({ ok: true, lineage: { generation: 0, seed: [] } });

    expect(await writeLineage(file, { generation: 2, seed: KEPT })).toEqual({ ok: true });
    expect(await readLineage(file)).toEqual({ ok: true, lineage: { generation: 2, seed: KEPT } });
  });

  it("refuses a text that is not a lineage", () => {
    for (const text of [
      "not json",
      '{"v":2,"generation":1,"seed":[]}',
      '{"v":1,"generation":0,"seed":[]}',
      '{"v":1,"generation":1,"seed":[{"id":"u1","role":"system","parts":[]}]}',
      '{"v":1,"generation":1,"seed":[{"id":"a1","role":"assistant","parts":[{"type":"tool","toolCallId":"c","toolName":"t"}]}]}',
    ]) {
      expect(parseLineage(bytes(text)).ok, text).toBe(false);
    }
  });

  it("refuses a write that the start could not read back, and writes no file", async () => {
    const file = fileIn();
    const huge: Kept = {
      id: "a1",
      role: "assistant",
      parts: [{ type: "tool", toolCallId: "c", toolName: "t", input: {}, outcome: { state: "done", output: 2 ** 60 } }],
    };

    expect((await writeLineage(file, { generation: 1, seed: [huge] })).ok).toBe(false);
    expect(existsSync(file)).toBe(false);
  });

  it("refuses a file that does not read at the start", async () => {
    const file = fileIn();
    writeFileSync(file, "{}");

    expect((await readLineage(file)).ok).toBe(false);
  });

  it("names the instance of each generation", () => {
    expect(instanceOf("s", 0)).toBe("s");
    expect(instanceOf("s", 3)).toBe("s-r3");
  });
});

describe("transcriptOf", () => {
  it("gives each message on its role, with tool calls and no reasoning", () => {
    expect(transcriptOf(KEPT)).toBe(
      "User: Echo, please.\n\nAssistant: [tool call echo with input {}]\n[result: echoed]\nAll done.",
    );
  });

  it("cuts a long tool output", () => {
    const long: Kept = {
      id: "a1",
      role: "assistant",
      parts: [{ type: "tool", toolCallId: "c", toolName: "t", input: {}, outcome: { state: "done", output: "x".repeat(10_000) } }],
    };

    expect(transcriptOf([long]).length).toBeLessThan(MAX_TRANSCRIPT_OUTPUT_BYTES + 100);
  });
});
