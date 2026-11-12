// The contract of `src/flue-history.ts` with `@flue/runtime/internal`. The
// entry is not Flue's public API, so each fact the facade reads is pinned
// here. A change of Flue that moves one fails this file first.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { fauxAssistantMessage, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import { defineTool, init, useModel, useTool } from "@flue/runtime";
import type { ConversationStreamStore } from "@flue/runtime/adapter";
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

function coldStore(
  store: ConversationStreamStore,
  onRead: (options: Parameters<ConversationStreamStore["read"]>[1]) => void = () => undefined,
): ConversationStreamStore {
  return new Proxy(store, {
    get: (target, property) => {
      if (property === "read") {
        return (...args: Parameters<ConversationStreamStore["read"]>) => {
          onRead(args[1]);
          return target.read(...args);
        };
      }

      const value = Reflect.get(target, property) as unknown;
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

describe("@flue/runtime/internal", () => {
  it("is the internal entry of @flue/runtime 2.3.0", () => {
    const installed = new URL("../node_modules/@flue/runtime/package.json", import.meta.url);
    const manifest = JSON.parse(readFileSync(fileURLToPath(installed), "utf8")) as { version: string };

    expect(manifest.version).toBe("2.3.0");
    expect(typeof internal.handleAgentConversationRead).toBe("function");
    expect(internal.agentStreamPath("Historian", "s-1")).toBe("agents/Historian/s-1");
  });

  it("serves one head snapshot with the keys the facade reads", async () => {
    await conversation("h-1");
    const read = await readHistory(flue.store()!, "Historian", "h-1");
    const chunks = read.ok ? read.chunks : [];
    const first = chunks[0];

    expect(read.ok).toBe(true);
    expect(first?.type).toBe("conversation-reset");
    if (first?.type !== "conversation-reset") {
      throw new Error("the history read did not return a reset snapshot");
    }

    expect(Object.keys(first).sort()).toEqual(["conversationId", "position", "snapshot", "type"]);
    expect(first.position).toEqual({
      batch: Number(first.snapshot.offset),
      index: Number.MAX_SAFE_INTEGER,
    });
    expect(Object.keys(first.snapshot).sort()).toEqual([
      "conversationId",
      "incarnation",
      "messages",
      "offset",
      "settlements",
      "v",
    ]);
    expect(first.snapshot.messages[0]).toEqual({
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

  it("checkpoints a cold fold and uses it without reading the log again", async () => {
    const id = "h-checkpoint";
    await conversation(id);
    const store = flue.store()!;
    const path = internal.agentStreamPath("Historian", id);
    const head = (await store.getMeta(path))?.nextOffset;
    expect((await store.getFoldCheckpoint?.(path))?.offset).not.toBe(head);

    expect((await readHistory(coldStore(store), "Historian", id)).ok).toBe(true);
    expect(await store.getFoldCheckpoint?.(path)).toMatchObject({ offset: head });

    const offsets: (string | undefined)[] = [];
    expect((await readHistory(coldStore(store, (options) => offsets.push(options?.offset)), "Historian", id)).ok).toBe(true);
    expect(offsets).toEqual([head]);
  });

  it("reads an instance that no send created as no history", async () => {
    expect(await readHistory(flue.store()!, "Historian", "never-sent")).toEqual({ ok: true, chunks: [] });
  });
});
