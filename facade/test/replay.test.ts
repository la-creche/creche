// The history replay joins adjacent text deltas without crossing a semantic
// boundary or the text limit of one ACP update.

import type { ConversationStreamChunk } from "@flue/runtime";
import { describe, expect, it } from "vitest";

import { MAX_TEXT_BYTES } from "../src/mapper.js";
import { replay } from "../src/replay.js";

const AT = { conversationId: "c", position: { batch: 1, index: 0 } };

function delta(index: number, kind: "text" | "reasoning", text: string, messageId = "m"): ConversationStreamChunk {
  return {
    ...AT,
    position: { batch: 1, index },
    type: "message-delta",
    messageId,
    kind,
    delta: text,
  };
}

describe("replay", () => {
  it("joins adjacent text updates and keeps kind and tool boundaries", () => {
    const chunks: ConversationStreamChunk[] = [
      delta(0, "text", "one"),
      delta(1, "text", " two"),
      delta(2, "reasoning", "think"),
      delta(3, "reasoning", " more"),
      {
        ...AT,
        position: { batch: 1, index: 4 },
        type: "tool-input",
        messageId: "m",
        toolCallId: "t",
        toolName: "echo",
        input: {},
      },
      delta(5, "text", "after"),
      delta(6, "text", " tool"),
    ];

    expect(replay(chunks).updates).toEqual([
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "one two" } },
      { sessionUpdate: "agent_thought_chunk", content: { type: "text", text: "think more" } },
      {
        sessionUpdate: "tool_call",
        toolCallId: "t",
        title: "echo",
        kind: "other",
        status: "in_progress",
        rawInput: {},
      },
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "after tool" } },
    ]);
  });

  it("keeps each joined text update within the UTF-8 byte limit", () => {
    const almostFull = "€".repeat((MAX_TEXT_BYTES - 1) / 3);
    const updates = replay([
      delta(0, "text", almostFull),
      delta(1, "text", "x"),
      delta(2, "text", "€"),
    ]).updates;

    expect(updates).toHaveLength(2);
    expect(Buffer.byteLength((updates[0] as { content: { text: string } }).content.text)).toBe(MAX_TEXT_BYTES);
    expect(updates[1]).toEqual({
      sessionUpdate: "agent_message_chunk",
      content: { type: "text", text: "€" },
    });
  });

  it("does not join text from different Flue messages", () => {
    const updates = replay([
      delta(0, "text", "first", "m1"),
      { ...AT, position: { batch: 1, index: 1 }, type: "message-completed", messageId: "m1" },
      { ...AT, position: { batch: 1, index: 2 }, type: "message-started", messageId: "m2", submissionId: "s" },
      delta(3, "text", "second", "m2"),
    ]).updates;

    expect(updates).toEqual([
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "first" } },
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "second" } },
    ]);
  });
});
