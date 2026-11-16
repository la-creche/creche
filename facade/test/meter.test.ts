// The thinking option and the meter over the facade's own wire, on Flue
// with an in-memory store and the faux model. The agent of this file writes
// the two metadata keys that the agent module writes (`agent/src/usage.ts`),
// so a second service on the same store stands for a cold start.

import { randomUUID } from "node:crypto";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { useModel, useResponseFinish, useResponseStart } from "@flue/runtime";
import type { AgentProps, ConversationStreamChunk } from "@flue/runtime";
import { CONTEXT_TOKENS_KEY, THINKING_LEVEL_KEY, ThinkingLevels } from "agent/agent";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { facadeApp } from "../src/acp.js";
import { sessionService } from "../src/service.js";
import { memoOf, usageUpdateOf } from "../src/usage.js";
import type { MeterSource, Spend } from "../src/usage.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux, freshRewind, startFlue } from "./faux-flue.js";

const INVALID_PARAMS = -32602;
const RESOURCE_NOT_FOUND = -32002;

/** The context tokens that the agent of this file reports for each response. */
const USED = 42;
const WINDOW = 1000;
const SPEND: Spend = { amount: 12.4, budget: 60 };

/** The levels of the running "process". A new holder is a cold start. */
let levels = new ThinkingLevels();

function instanceOf(props: AgentProps | undefined): string | null {
  try {
    return props?.id ?? null;
  } catch {
    return null;
  }
}

/** Records its level and a fixed count, as the agent module does. */
function Metered(props?: AgentProps): string {
  const level = levels.of(instanceOf(props));
  useModel(FAUX_MODEL, { thinkingLevel: level });
  useResponseStart(() => ({ [THINKING_LEVEL_KEY]: level }));
  useResponseFinish(() => ({ [CONTEXT_TOKENS_KEY]: USED }));

  return "Answer in one word.";
}
Metered.durability = { maxAttempts: 1 };

const flue = await startFlue([Metered]);
afterAll(() => flue.stop());

let spend: Spend | null = SPEND;
const meter: MeterSource = { window: () => WINDOW, spend: () => Promise.resolve(spend) };

let id: string;

function serve(): Wire {
  return new Wire(facadeApp(sessionService({ agent: Metered, agentName: "Metered", sessionId: id, link: null, store: flue.store, thinking: levels, meter, rewind: freshRewind() })));
}

function setLevel(wire: Wire, params: Record<string, unknown>): ReturnType<Wire["request"]> {
  return wire.request("session/set_config_option", { sessionId: id, configId: "mode", ...params });
}

async function prompt(wire: Wire): Promise<void> {
  faux.setResponses([fauxAssistantMessage("Done.")]);
  await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Hello?" }] });
}

/** The thinking option, as the facade answers it at `level`. */
function optionsAt(level: string): unknown {
  const [option] = vectors.new_session.configOptions;

  return { configOptions: [{ ...option, currentValue: level }] };
}

beforeEach(() => {
  id = randomUUID();
  levels = new ThinkingLevels();
  spend = SPEND;
});

describe("session/set_config_option", () => {
  it("sets the level of the next prompt and answers the options", async () => {
    const wire = serve();

    expect((await setLevel(wire, { value: "medium" })).result).toEqual(optionsAt("medium"));
    expect(levels.of(id)).toBe("medium");
  });

  it("refuses another option, a boolean, an unlisted value and another session", async () => {
    const wire = serve();
    const refused = [
      await wire.request("session/set_config_option", { sessionId: id, configId: "model", value: "high" }),
      await setLevel(wire, { type: "boolean", value: true }),
      await setLevel(wire, { value: "max" }),
    ];

    expect(refused.map((answer) => answer.error?.code)).toEqual([INVALID_PARAMS, INVALID_PARAMS, INVALID_PARAMS]);
    expect((await wire.request("session/set_config_option", { sessionId: randomUUID(), configId: "mode", value: "high" })).error?.code).toBe(
      RESOURCE_NOT_FOUND,
    );
    expect(levels.has(id)).toBe(false);
  });
});

describe("the meter", () => {
  it("ends each turn with the context fill and the spend", async () => {
    const wire = serve();
    await prompt(wire);

    expect(wire.updates(id).slice(-2).map((update) => update["sessionUpdate"])).toEqual(["session_info_update", "usage_update"]);
    expect(wire.updates(id).at(-1)).toEqual(usageUpdateOf(USED, WINDOW, SPEND));
    expect(usageUpdateOf(USED, WINDOW, SPEND)).toEqual({
      sessionUpdate: "usage_update",
      used: USED,
      size: WINDOW,
      _meta: { [vectors.usage.spend_meta_key]: { amount: 12.4, budget: 60, currency: "USD" } },
    });
  });

  it("leaves the spend out when it does not read", async () => {
    spend = null;
    const wire = serve();
    await prompt(wire);

    expect(wire.updates(id).at(-1)).toEqual({ sessionUpdate: "usage_update", used: USED, size: WINDOW });
  });

  it("sends no usage_update at the load of a session with no turn", async () => {
    const wire = serve();

    expect((await wire.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [] })).result).toEqual(optionsAt("off"));
    expect(wire.updates(id)).toEqual([]);
  });
});

describe("a cold start", () => {
  it("loads the level and the count of the last response", async () => {
    const first = serve();
    await setLevel(first, { value: "high" });
    await prompt(first);

    levels = new ThinkingLevels();
    const second = serve();
    const answer = await second.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [] });

    expect(answer.result).toEqual(optionsAt("high"));
    expect(second.updates(id).at(-1)).toEqual(usageUpdateOf(USED, WINDOW, SPEND));
  });

  it("gives the first prompt the level of the last response", async () => {
    const first = serve();
    await setLevel(first, { value: "low" });
    await prompt(first);

    levels = new ThinkingLevels();
    await prompt(serve());

    expect(levels.of(id)).toBe("low");
  });

  it("keeps a level set in this process over the history", async () => {
    const first = serve();
    await setLevel(first, { value: "low" });
    await prompt(first);

    levels = new ThinkingLevels();
    const second = serve();
    await setLevel(second, { value: "medium" });
    await prompt(second);

    expect(levels.of(id)).toBe("medium");
  });
});

describe("memoOf", () => {
  function started(metadata: Record<string, unknown>): ConversationStreamChunk {
    return { type: "message-started", conversationId: "c", messageId: "m", metadata, position: { batch: 1, index: 0 } };
  }

  it("keeps the last valid value of each key and skips the rest", () => {
    const chunks = [
      started({ [THINKING_LEVEL_KEY]: "medium", [CONTEXT_TOKENS_KEY]: 10 }),
      started({ [THINKING_LEVEL_KEY]: "max", [CONTEXT_TOKENS_KEY]: -1 }),
      started({ [THINKING_LEVEL_KEY]: 3, [CONTEXT_TOKENS_KEY]: 1.5 }),
      started({ [CONTEXT_TOKENS_KEY]: "20" }),
    ];

    expect(memoOf(chunks)).toEqual({ contextTokens: 10, thinking: "medium" });
    expect(memoOf([])).toEqual({ contextTokens: null, thinking: null });
  });
});
