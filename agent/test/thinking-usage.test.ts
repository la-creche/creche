// The thinking level of a session reaches the model call, and each
// response keeps its level and its context tokens in its metadata. A fake
// LiteLLM answers, and a fake vmm gives the start.

import { init } from "@flue/runtime";
import type { ConversationStreamChunk } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent, CONTEXT_TOKENS_KEY, THINKING_LEVEL_KEY } from "../src/agent.js";
import { DEFAULT_THINKING, isThinkingChoice, THINKING_CHOICES, ThinkingLevels } from "../src/thinking.js";
import { contextTokensOf } from "../src/usage.js";
import { FakeCh } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { closeFakeVmms } from "./fake-vmm.js";
import { FIXTURE_TOKEN, testFamily } from "./family.js";

/** The fake model reports 1 prompt token and 1 output token, 2 in all. */
const FAKE_CONTEXT_TOKENS = 2;

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([{ text: "done" }]);
const family = await testFamily({ pepUrl: await ch.start(), litellmUrl: await fake.start() });
const composed = composeAgent(family.start);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
  closeFakeVmms();
});

/** One turn of session `id`, and the metadata its chunks carried, merged. */
async function turn(id: string): Promise<Record<string, unknown>> {
  const chunks: ConversationStreamChunk[] = [];
  const handle = init(composed.agent, { id });
  await handle.read(await handle.dispatch("Hello?"), { onEvent: (chunk) => chunks.push(chunk) });

  const metadata: Record<string, unknown> = {};
  for (const chunk of chunks) {
    if (chunk.type === "message-started" || chunk.type === "message-metadata") {
      Object.assign(metadata, chunk.metadata);
    }
  }

  return metadata;
}

describe("the thinking level", () => {
  it("sends no reasoning effort and keeps the system role by default", async () => {
    const before = fake.seen.length;
    const metadata = await turn("session-default");
    const request = fake.seen[before];

    expect(request?.reasoningEffort).toBeNull();
    expect(request?.messages[0]?.role).toBe("system");
    expect(metadata).toEqual({ [THINKING_LEVEL_KEY]: DEFAULT_THINKING, [CONTEXT_TOKENS_KEY]: FAKE_CONTEXT_TOKENS });
  });

  it("sends the level of the session as the reasoning effort", async () => {
    composed.thinking.set("session-high", "high");
    const before = fake.seen.length;
    const metadata = await turn("session-high");

    expect(fake.seen[before]?.reasoningEffort).toBe("high");
    expect(metadata[THINKING_LEVEL_KEY]).toBe("high");
  });
});

describe("ThinkingLevels", () => {
  it("gives the default to a session with no level and to a bare render", () => {
    const levels = new ThinkingLevels();
    levels.set("a", "low");

    expect([levels.of("a"), levels.of("b"), levels.of(null)]).toEqual(["low", DEFAULT_THINKING, DEFAULT_THINKING]);
    expect([levels.has("a"), levels.has("b")]).toEqual([true, false]);
  });

  it("takes only the listed levels", () => {
    expect(THINKING_CHOICES.every(isThinkingChoice)).toBe(true);
    expect(["max", "xhigh", "", null, 1].some(isThinkingChoice)).toBe(false);
  });
});

describe("contextTokensOf", () => {
  const cost = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };

  it("takes the total, or the sum of the parts when the provider gives no total", () => {
    expect(contextTokensOf({ input: 5, output: 2, cacheRead: 90, cacheWrite: 3, totalTokens: 100, cost })).toBe(100);
    expect(contextTokensOf({ input: 5, output: 2, cacheRead: 90, cacheWrite: 3, totalTokens: 0, cost })).toBe(100);
  });
});
