// The facade switches the model at the next prompt after a
// `model_alias` edit (decisions CH5c, CH5c-2). The new alias's limits come
// from LiteLLM. A failed limits read gives the 128000-token window, with
// one stderr line.

import { afterAll, describe, expect, it } from "vitest";

import { FakeCh } from "../../agent/test/fake-ch.js";
import { FakeModel } from "../../agent/test/fake-model.js";
import { FIXTURE_TOKEN, swapConfig } from "../../agent/test/family.js";
import { facadeEnv } from "./facade-process.js";
import { servedFamily } from "./served-family.js";

const NEW_ALIAS = "code-router";
const SWITCHED_LINE = "model code-router window 64000 tokens, output 4096";
const FALLBACK_LINE = "model other limits not read (alias_absent); window 128000 tokens";

const ch = new FakeCh(FIXTURE_TOKEN);
const model = new FakeModel([{ text: "Done." }]);
model.modelInfo = { data: [{ model_name: NEW_ALIAS, model_info: { max_input_tokens: 64000, max_output_tokens: 4096 } }] };
const env = facadeEnv({ pepUrl: await ch.start(), litellmUrl: await model.start() });
const configDir = String(env["AGENT_FAMILY_CONFIG_DIR"]);
const served = await servedFamily(env);

afterAll(async () => {
  await served.runtime?.stop();
  await model.stop();
  await ch.stop();
});

async function prompt(): Promise<unknown> {
  return (await served.wire.request("session/prompt", { sessionId: served.id, prompt: [{ type: "text", text: "Weather?" }] })).result;
}

function count(line: string): number {
  return served.stderr().split(line).length - 1;
}

describe("the model at the next prompt", () => {
  it("calls the new alias with its limits after a model_alias edit", async () => {
    expect(await prompt()).toEqual({ stopReason: "end_turn" });

    swapConfig(configDir, { "runtime.json": JSON.stringify({ model_alias: NEW_ALIAS }) });
    expect(await prompt()).toEqual({ stopReason: "end_turn" });

    expect(model.seen.map((seen) => [seen.model, seen.maxTokens])).toEqual([
      ["chat", 16384],
      [NEW_ALIAS, 4096],
    ]);
    expect(count(SWITCHED_LINE)).toBe(1);
  });

  it("falls back to the 128000-token window when the limits do not read, with one stderr line", async () => {
    swapConfig(configDir, { "runtime.json": JSON.stringify({ model_alias: "other" }) });
    const answers = [await prompt(), await prompt()];

    expect(answers).toEqual([{ stopReason: "end_turn" }, { stopReason: "end_turn" }]);
    expect(model.seen.slice(2).map((seen) => [seen.model, seen.maxTokens])).toEqual([
      ["other", 16384],
      ["other", 16384],
    ]);
    expect(count(FALLBACK_LINE)).toBe(1);
  });
});
