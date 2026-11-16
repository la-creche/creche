// The history replay joins adjacent text deltas without crossing a semantic
// boundary or the text limit of one ACP update.

import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ConversationStreamChunk } from "@flue/runtime";
import { describe, expect, it } from "vitest";

import { Attachments } from "../src/attachments.js";
import { MAX_TEXT_BYTES } from "../src/mapper.js";
import type { ImageReader } from "../src/mapper.js";
import { replay } from "../src/replay.js";

const WORKSPACE = mkdtempSync(join(tmpdir(), "facade-replay-"));
const ATTACHMENTS = new Attachments(WORKSPACE);
const FILES: ImageReader = (marker) => ATTACHMENTS.readImage(marker);

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

    expect(replay(chunks, FILES).updates).toEqual([
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
    ], FILES).updates;

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
    ], FILES).updates;

    expect(updates).toEqual([
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "first" } },
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "second" } },
    ]);
  });
});

/** One visible user message, as a `message-appended` chunk carries it. */
function userMessage(index: number, parts: Extract<ConversationStreamChunk, { type: "message-appended" }>["message"]["parts"]): ConversationStreamChunk {
  return {
    ...AT,
    position: { batch: 1, index },
    type: "message-appended",
    message: { id: `u${index}`, role: "user", purpose: "user", display: "visible", parts },
  };
}

function userText(index: number, text: string): ConversationStreamChunk {
  return userMessage(index, [{ type: "text", text, state: "done" }]);
}

const PNG_BYTES = Buffer.from("not really a png");

describe("the replay of the files of a prompt", () => {
  it("shows the user text, then a link for each file and the image that the workspace holds", () => {
    mkdirSync(join(WORKSPACE, "attachments"), { recursive: true });
    writeFileSync(join(WORKSPACE, "attachments", "cat.png"), PNG_BYTES);
    const body = "Look.\n\n[attached: attachments/cat.png (image/png, 16 bytes)]\n[attached: attachments/notes (1).txt (text/plain, 6 bytes)]";
    const updates = replay(
      [userMessage(0, [{ type: "text", text: body, state: "done" }, { type: "file", mediaType: "image/png", id: "a1", size: 16, filename: "cat.png" }])],
      FILES,
    ).updates;

    expect(updates).toEqual([
      { sessionUpdate: "user_message_chunk",
        messageId: "u0", content: { type: "text", text: "Look." } },
      {
        sessionUpdate: "user_message_chunk",
        messageId: "u0",
        content: { type: "resource_link", uri: "attachment:cat.png", name: "cat.png", mimeType: "image/png", size: 16 },
      },
      {
        sessionUpdate: "user_message_chunk",
        messageId: "u0",
        content: { type: "image", data: PNG_BYTES.toString("base64"), mimeType: "image/png", uri: "attachment:cat.png" },
      },
      {
        sessionUpdate: "user_message_chunk",
        messageId: "u0",
        content: { type: "resource_link", uri: "attachment:notes (1).txt", name: "notes (1).txt", mimeType: "text/plain", size: 6 },
      },
    ]);
  });

  it("replays a link only when the workspace no longer holds the image", () => {
    rmSync(join(WORKSPACE, "attachments", "gone.png"), { force: true });
    const updates = replay([userText(0, "[attached: attachments/gone.png (image/png, 3 bytes)]")], FILES).updates;

    expect(updates).toEqual([
      {
        sessionUpdate: "user_message_chunk",
        messageId: "u0",
        content: { type: "resource_link", uri: "attachment:gone.png", name: "gone.png", mimeType: "image/png", size: 3 },
      },
    ]);
  });

  it("keeps a marker line inside the user text as text", () => {
    const text = "[attached: attachments/a.png (image/png, 3 bytes)]\nNot a file.";
    const updates = replay([userText(0, text)], FILES).updates;

    expect(updates).toEqual([{ sessionUpdate: "user_message_chunk",
        messageId: "u0", content: { type: "text", text } }]);
  });

  it("replays the files of a kept message of a rewind in the same way", () => {
    const seed = [{ id: "k1", role: "user" as const, parts: [{ type: "text" as const, text: "Old.\n\n[attached: attachments/gone.png (image/png, 3 bytes)]" }] }];
    const updates = replay([], FILES, seed).updates;

    expect(updates).toEqual([
      { sessionUpdate: "user_message_chunk", messageId: "k1", content: { type: "text", text: "Old." } },
      {
        sessionUpdate: "user_message_chunk",
        messageId: "k1",
        content: { type: "resource_link", uri: "attachment:gone.png", name: "gone.png", mimeType: "image/png", size: 3 },
      },
    ]);
  });
});
