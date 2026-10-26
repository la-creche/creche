// The contract of `src/flue-history.ts` with `@flue/runtime/internal`. The
// entry is not Flue's public API, so each fact the facade reads is pinned
// here. A change of Flue that moves one fails this file first.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { fauxAssistantMessage, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import { defineTool, init, useModel, useTool } from "@flue/runtime";
import * as internal from "@flue/runtime/internal";
import { afterAll, describe, expect, it } from "vitest";

import { readHistory } from "../src/flue-history.js";
import { FAUX_MODEL, faux, startFlue } from "./faux-flue.js";

const echo = defineTool({ name: "echo", description: "Answers echoed.", run: () => "echoed" });
const boom = defineTool({
  name: "boom",
  description: "Fails.",
  run: () => {
    throw new Error("no");
  },
});

function Historian(): string {
  useModel(FAUX_MODEL);
  useTool(echo);
  useTool(boom);

  return "Use the tools.";
}

const flue = await startFlue([Historian]);
afterAll(() => flue.stop());

/** The keys of each chunk kind that a conversation with two tools gives. */
const CHUNK_KEYS = {
  "conversation-reset": ["conversationId", "position", "snapshot", "type"],
  "message-appended": ["conversationId", "message", "position", "type"],
  "message-started": ["conversationId", "messageId", "position", "submissionId", "timestamp", "turnId", "type"],
  "message-delta": ["conversationId", "delta", "kind", "messageId", "position", "type"],
  "tool-input": ["conversationId", "input", "messageId", "position", "timestamp", "toolCallId", "toolName", "type"],
  "tool-output": ["conversationId", "durationMs", "output", "position", "timestamp", "toolCallId", "type"],
  "tool-output-error": ["conversationId", "durationMs", "errorText", "position", "timestamp", "toolCallId", "type"],
  "message-completed": ["conversationId", "messageId", "position", "timestamp", "type"],
  "submission-settled": [
    "answeredBySubmissionId",
    "conversationId",
    "outcome",
    "position",
    "submissionId",
    "timestamp",
    "type",
  ],
};

async function conversation(id: string): Promise<void> {
  faux.setResponses([
    fauxAssistantMessage([fauxThinking("Think."), fauxToolCall("echo", {}, { id: "c1" }), fauxToolCall("boom", {}, { id: "c2" })], {
      stopReason: "toolUse",
    }),
    fauxAssistantMessage([fauxText("Done.")]),
  ]);
  const handle = init(Historian, { id });
  await handle.read(await handle.dispatch("Hi"));
}

describe("@flue/runtime/internal", () => {
  it("is the internal entry of @flue/runtime 2.3.0", () => {
    const installed = new URL("../node_modules/@flue/runtime/package.json", import.meta.url);
    const manifest = JSON.parse(readFileSync(fileURLToPath(installed), "utf8")) as { version: string };

    expect(manifest.version).toBe("2.3.0");
    expect(typeof internal.handleAgentConversationRead).toBe("function");
    expect(internal.agentStreamPath("Historian", "s-1")).toBe("agents/Historian/s-1");
  });

  it("serves the updates view with the keys the facade reads", async () => {
    await conversation("h-1");
    const read = await readHistory(flue.store()!, "Historian", "h-1");
    const chunks = read.ok ? read.chunks : [];
    const keys = Object.fromEntries(chunks.map((chunk) => [chunk.type, Object.keys(chunk).sort()]));
    const first = chunks[0];
    const user = chunks.find((chunk) => chunk.type === "message-appended");

    expect(read.ok).toBe(true);
    expect(keys).toEqual(CHUNK_KEYS);
    expect(first?.type).toBe("conversation-reset");
    expect(first?.position).toEqual({ batch: 0, index: 0 });
    expect(first?.type === "conversation-reset" && Object.keys(first.snapshot).sort()).toEqual([
      "conversationId",
      "messages",
      "offset",
      "settlements",
      "v",
    ]);
    expect(user?.type === "message-appended" && user.message).toEqual({
      id: expect.any(String),
      role: "user",
      purpose: "user",
      display: "visible",
      submissionId: expect.any(String),
      timestamp: expect.any(String),
      parts: [{ type: "text", text: "Hi", state: "done" }],
    });
  });

  it("orders the chunks by position", async () => {
    await conversation("h-2");
    const read = await readHistory(flue.store()!, "Historian", "h-2");
    const positions = read.ok ? read.chunks.map((chunk) => chunk.position.batch * 1000 + chunk.position.index) : [];

    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it("reads an instance that no send created as no history", async () => {
    expect(await readHistory(flue.store()!, "Historian", "never-sent")).toEqual({ ok: true, chunks: [] });
  });
});
