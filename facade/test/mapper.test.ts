// One row per Flue chunk kind: what the facade sends for it on a live turn.

import type { ConversationStreamChunk } from "@flue/runtime";
import { describe, expect, it } from "vitest";

import { chunkToUpdates, MAX_NAME_BYTES, MAX_TEXT_BYTES, SubmissionChunks } from "../src/mapper.js";
import { settle } from "../src/turns.js";

const AT = { conversationId: "c", position: { batch: 1, index: 0 } };
const SNAPSHOT = { v: 1 as const, conversationId: "c", offset: "-1", messages: [], settlements: [] };
const USER = { id: "u", role: "user" as const, purpose: "user" as const, display: "visible" as const, parts: [] };

const ROWS: [string, ConversationStreamChunk, unknown[]][] = [
  ["conversation-reset", { ...AT, type: "conversation-reset", snapshot: SNAPSHOT }, []],
  ["message-appended", { ...AT, type: "message-appended", message: USER }, []],
  ["message-started", { ...AT, type: "message-started", messageId: "m", submissionId: "s" }, []],
  ["message-metadata", { ...AT, type: "message-metadata", messageId: "m", metadata: {} }, []],
  ["data-part", { ...AT, type: "data-part", messageId: "m", name: "n", data: 1 }, []],
  [
    "message-delta text",
    { ...AT, type: "message-delta", messageId: "m", kind: "text", delta: "Hi" },
    [{ sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Hi" } }],
  ],
  [
    "message-delta reasoning",
    { ...AT, type: "message-delta", messageId: "m", kind: "reasoning", delta: "Hm" },
    [{ sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "Hm" } }],
  ],
  [
    "tool-input",
    { ...AT, type: "tool-input", messageId: "m", toolCallId: "t", toolName: "echo", input: { a: 1 } },
    [{ sessionUpdate: "tool_call", toolCallId: "t", title: "echo", kind: "other", status: "in_progress", rawInput: { a: 1 } }],
  ],
  [
    "tool-output",
    { ...AT, type: "tool-output", toolCallId: "t", output: { b: 2 } },
    [
      {
        sessionUpdate: "tool_call_update",
        toolCallId: "t",
        status: "completed",
        content: [{ type: "content", content: { type: "text", text: '{"b":2}' } }],
      },
    ],
  ],
  [
    "tool-output-error",
    { ...AT, type: "tool-output-error", toolCallId: "t", errorText: "Awaiting permission" },
    [
      {
        sessionUpdate: "tool_call_update",
        toolCallId: "t",
        status: "failed",
        content: [{ type: "content", content: { type: "text", text: "Awaiting permission" } }],
      },
    ],
  ],
  ["message-completed", { ...AT, type: "message-completed", messageId: "m" }, []],
  ["submission-settled", { ...AT, type: "submission-settled", submissionId: "s", outcome: "completed" }, []],
];

describe("chunkToUpdates", () => {
  it.each(ROWS)("maps %s", (_kind, chunk, updates) => {
    expect(chunkToUpdates(chunk)).toEqual(updates);
  });

  it("has a row for each chunk kind", () => {
    expect(new Set(ROWS.map(([, chunk]) => chunk.type)).size).toBe(11);
  });

  it("cuts every forwarded string and mends a lone surrogate", () => {
    const long = "€".repeat(MAX_TEXT_BYTES);
    const [delta] = chunkToUpdates({ ...AT, type: "message-delta", messageId: "m", kind: "text", delta: `${long}\ud800` });
    const [call] = chunkToUpdates({
      ...AT,
      type: "tool-input",
      messageId: "m",
      toolCallId: "t".repeat(1000),
      toolName: "n".repeat(1000),
      input: { big: long },
    });
    const sent = JSON.stringify([delta, call]);

    expect(Buffer.byteLength((delta as { content: { text: string } }).content.text)).toBeLessThanOrEqual(MAX_TEXT_BYTES);
    expect((call as { toolCallId: string }).toolCallId).toHaveLength(MAX_NAME_BYTES);
    expect(typeof (call as { rawInput: unknown }).rawInput).toBe("string");
    expect(sent.isWellFormed()).toBe(true);
  });
});

describe("SubmissionChunks", () => {
  it("keeps the chunks of one submission and drops the turns before it", () => {
    const mine = new SubmissionChunks("s2");
    const stream: ConversationStreamChunk[] = [
      { ...AT, type: "conversation-reset", snapshot: SNAPSHOT },
      { ...AT, type: "message-started", messageId: "m1", submissionId: "s1" },
      { ...AT, type: "message-delta", messageId: "m1", kind: "text", delta: "old" },
      { ...AT, type: "tool-input", messageId: "m1", toolCallId: "t1", toolName: "x", input: {} },
      { ...AT, type: "tool-output", toolCallId: "t1", output: "" },
      { ...AT, type: "submission-settled", submissionId: "s1", outcome: "completed" },
      { ...AT, type: "message-appended", message: USER },
      { ...AT, type: "message-started", messageId: "m2", submissionId: "s2" },
      { ...AT, type: "message-delta", messageId: "m2", kind: "text", delta: "new" },
      { ...AT, type: "tool-input", messageId: "m2", toolCallId: "t2", toolName: "x", input: {} },
      { ...AT, type: "tool-output-error", toolCallId: "t2", errorText: "no" },
      { ...AT, type: "message-completed", messageId: "m2" },
      { ...AT, type: "submission-settled", submissionId: "s2", outcome: "completed" },
    ];

    expect(stream.filter((chunk) => mine.accept(chunk)).map((chunk) => chunk.type)).toEqual([
      "message-started",
      "message-delta",
      "tool-input",
      "tool-output-error",
      "message-completed",
      "submission-settled",
    ]);
  });
});

describe("settle", () => {
  it("maps completed to end_turn, aborted to cancelled and failed to an error", () => {
    expect(settle("completed", "")).toEqual({ ok: true, value: { stopReason: "end_turn" } });
    expect(settle("aborted", "")).toEqual({ ok: true, value: { stopReason: "cancelled" } });
    expect(settle("failed", "boom")).toEqual({ ok: false, refusal: { kind: "failed", detail: "the turn failed: boom" } });
  });
});
