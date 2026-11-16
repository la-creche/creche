// The history replay joins adjacent text deltas without crossing a semantic
// boundary or the text limit of one ACP update.

import { mkdtempSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ConversationStreamChunk } from "@flue/runtime";
import { describe, expect, it, vi } from "vitest";

import { Attachments, base64Bytes, bodyOf, FileKind } from "../src/attachments.js";
import { MAX_TEXT_BYTES } from "../src/mapper.js";
import { MAX_REPLAY_IMAGE_BYTES, replay } from "../src/replay.js";
import type { ImageReader } from "../src/replay.js";

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

const SNAPSHOT = { v: 1 as const, conversationId: "c", offset: "-1", messages: [], settlements: [] };

function image(name: string, bytes: Buffer = PNG_BYTES): string {
  return bodyOf("", ATTACHMENTS.save([{ name, mimeType: "image/png", kind: FileKind.Image, bytes }]));
}

function link(messageId: string, name: string, size: number, mimeType = "image/png"): Record<string, unknown> {
  return {
    sessionUpdate: "user_message_chunk",
    messageId,
    content: { type: "resource_link", uri: `attachment:${name}`, name, mimeType, size },
  };
}

function shown(messageId: string, name: string, bytes: Buffer = PNG_BYTES): Record<string, unknown> {
  return {
    sessionUpdate: "user_message_chunk",
    messageId,
    content: { type: "image", data: bytes.toString("base64"), mimeType: "image/png", uri: `attachment:${name}` },
  };
}

describe("the replay of the files of a prompt", () => {
  it("shows the user text, then a link for each file and the image that the workspace holds", () => {
    const notes = bodyOf("", ATTACHMENTS.save([{ name: "notes (1).txt", mimeType: "text/plain", kind: FileKind.Workspace, bytes: Buffer.from("hello\n") }]));
    const body = `Look.\n\n${image("cat.png")}\n${notes}`;
    const updates = replay(
      [userMessage(0, [{ type: "text", text: body, state: "done" }, { type: "file", mediaType: "image/png", id: "a1", size: 16, filename: "cat.png" }])],
      FILES,
    ).updates;

    expect(updates).toEqual([
      { sessionUpdate: "user_message_chunk", messageId: "u0", content: { type: "text", text: "Look." } },
      link("u0", "cat.png", 16),
      shown("u0", "cat.png"),
      link("u0", "notes (1).txt", 6, "text/plain"),
    ]);
  });

  it("replays a link only when the workspace no longer holds the image", () => {
    const body = image("gone.png");
    rmSync(join(WORKSPACE, "attachments", "gone.png"));
    const updates = replay([userText(0, body)], FILES).updates;

    expect(updates).toEqual([link("u0", "gone.png", 16)]);
  });

  it("replays a link only when the agent changed the bytes of the image", () => {
    const body = image("changed.png");
    writeFileSync(join(WORKSPACE, "attachments", "changed.png"), Buffer.from("not the scan bytes"));
    const same = image("same-size.png");
    writeFileSync(join(WORKSPACE, "attachments", "same-size.png"), Buffer.from("NOT really a png"));
    const updates = replay([userText(0, `${body}\n${same}`)], FILES).updates;

    expect(updates).toEqual([link("u0", "changed.png", 16), link("u0", "same-size.png", 16)]);
  });

  it("replays a link only when the agent made the directory a link", () => {
    const root = mkdtempSync(join(tmpdir(), "facade-replay-link-"));
    const files = new Attachments(root);
    const body = bodyOf("", files.save([{ name: "cat.png", mimeType: "image/png", kind: FileKind.Image, bytes: PNG_BYTES }]));
    renameSync(join(root, "attachments"), join(root, "moved"));
    symlinkSync(join(root, "moved"), join(root, "attachments"));
    const updates = replay([userText(0, body)], (marker) => files.readImage(marker)).updates;

    expect(updates).toEqual([link("u0", "cat.png", 16)]);
  });

  it("keeps a marker line inside the user text as text", () => {
    const text = `${image("inside.png")}\nNot a file.`;
    const updates = replay([userText(0, text)], FILES).updates;

    expect(updates).toEqual([{ sessionUpdate: "user_message_chunk", messageId: "u0", content: { type: "text", text } }]);
  });

  it("replays the files of a kept message of a rewind in the same way", () => {
    const seed = [{ id: "k1", role: "user" as const, parts: [{ type: "text" as const, text: `Old.\n\n${image("kept.png")}` }] }];
    const updates = replay([], FILES, seed).updates;

    expect(updates).toEqual([
      { sessionUpdate: "user_message_chunk", messageId: "k1", content: { type: "text", text: "Old." } },
      link("k1", "kept.png", 16),
      shown("k1", "kept.png"),
    ]);
  });

  it("reads no image of a message that a later reset replaces", () => {
    const body = image("replaced.png");
    const read = vi.fn(FILES);
    const updates = replay([userText(0, body), { ...AT, position: { batch: 2, index: 0 }, type: "conversation-reset", snapshot: SNAPSHOT }], read).updates;

    expect(updates).toEqual([]);
    expect(read).not.toHaveBeenCalled();
  });

  it("replays the images past the cap of one load as links only", () => {
    const big = Buffer.alloc(7 * 1024 * 1024, 1);
    const names = ["big-1.png", "big-2.png", "big-3.png", "big-4.png", "big-5.png"];
    const body = names.map((name) => image(name, big)).join("\n");
    const updates = replay([userText(0, body)], FILES).updates;
    const fits = Math.floor(MAX_REPLAY_IMAGE_BYTES / base64Bytes(big.length));

    expect(fits).toBeLessThan(names.length);
    expect(updates.filter((update) => (update as { content: { type: string } }).content.type === "image")).toHaveLength(fits);
    expect(updates.at(-1)).toEqual(link("u0", names.at(-1) ?? "", big.length));
  });
});
