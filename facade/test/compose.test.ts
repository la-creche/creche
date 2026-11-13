// The facade with the real agent module, in process: one family on a fake
// vmm, the agent's fake LiteLLM and fake chaperone (`test/served-family.ts`).
// One turn calls a `ch` tool: `tool_call`, then `tool_call_update` completed.

import { existsSync, realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { FakeCh, SEARCH_TOOL } from "../../agent/test/fake-ch.js";
import { FakeModel } from "../../agent/test/fake-model.js";
import { FIXTURE_TOKEN } from "../../agent/test/fake-vmm.js";
import { servedFamily } from "./served-family.js";

const ch = new FakeCh(FIXTURE_TOKEN);
const model = new FakeModel([{ tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } }, { text: "Sunny." }]);
const served = await servedFamily({ pepUrl: await ch.start(), litellmUrl: await model.start() });

afterAll(async () => {
  await served.stop();
  await model.stop();
  await ch.stop();
});

describe("the facade with the agent module", () => {
  it("shares one Flue with the agent module", () => {
    // Flue's hooks need the one copy that runs `start()`.
    const flue = (dir: string): string => realpathSync(fileURLToPath(new URL(`${dir}/node_modules/@flue/runtime`, import.meta.url)));

    expect(flue("../../agent"), "agent/ has a Flue of its own: see facade/AGENTS.md").toBe(flue(".."));
  });

  it("runs one turn that calls a ch tool, on the store of the start", async () => {
    const { id, wire } = served;
    const init = await wire.request("initialize", vectors.initialize.request.params);
    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Weather?" }] });
    const tools = wire.updates(id).filter((update) => String(update["sessionUpdate"]).startsWith("tool_call"));

    expect(init.result).toEqual(vectors.initialize.response.result);
    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(tools.map((update) => `${String(update["sessionUpdate"])} ${String(update["status"])}`)).toEqual([
      "tool_call in_progress",
      "tool_call_update completed",
    ]);
    expect(ch.calls).toEqual([{ name: SEARCH_TOOL, args: { text: "weather" } }]);
    expect(existsSync(served.start.store)).toBe(true);
    expect(existsSync(served.start.workspace)).toBe(true);
  });
});
