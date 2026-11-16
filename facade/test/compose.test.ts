// The facade with the real agent module, in process: one family on a fake
// vmm, the agent's fake LiteLLM and fake chaperone (`test/served-family.ts`).
// One turn calls a `ch` tool: `tool_call`, then `tool_call_update` completed.
// A thinking level reaches the model call, and the turn ends with the meter.
// An image of a prompt reaches the model call, and the load replays it.

import { existsSync, realpathSync, rmSync } from "node:fs";
import { join } from "node:path";
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

  it("sends the chosen thinking level, then ends the turn with the meter", async () => {
    const { id, wire } = served;
    model.keyInfo = { info: { spend: 12.4, max_budget: 60, budget_duration: "1d" } };
    const set = await wire.request("session/set_config_option", { sessionId: id, configId: "mode", value: "high" });
    const before = model.seen.length;
    await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Again?" }] });

    expect(set.result).toMatchObject({ configOptions: [{ id: "mode", currentValue: "high" }] });
    expect(model.seen[before]?.reasoningEffort).toBe("high");
    // The fake model reports 2 tokens for each call. It gives no model list,
    // so the window is the fallback of `agent/src/litellm.ts`.
    expect(wire.updates(id).at(-1)).toEqual({
      sessionUpdate: "usage_update",
      used: 2,
      size: 128000,
      _meta: { "creche/spend": { amount: 12.4, budget: 60, currency: "USD" } },
    });
  });

  it("gives an image to the model, and the load replays each file", async () => {
    const { id, wire } = served;
    const before = model.seen.length;
    const answer = await wire.request("session/prompt", {
      sessionId: id,
      prompt: [
        { type: "text", text: "What is this?" },
        { type: "image", mimeType: "image/png", data: PIXEL, uri: "attachment:pixel.png" },
        { type: "resource", resource: { uri: "attachment:notes.txt", mimeType: "text/plain", blob: NOTES } },
      ],
    });
    const user = model.seen[before]?.messages.filter((message) => message.role === "user").at(-1);

    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(user?.content).toEqual(expect.arrayContaining([{ type: "image_url", image_url: { url: `data:image/png;base64,${PIXEL}` } }]));
    expect(await replayedFiles(id)).toEqual([
      { type: "text", text: "What is this?" },
      { type: "resource_link", uri: "attachment:pixel.png", name: "pixel.png", mimeType: "image/png", size: 70 },
      { type: "image", data: PIXEL, mimeType: "image/png", uri: "attachment:pixel.png" },
      { type: "resource_link", uri: "attachment:notes.txt", name: "notes.txt", mimeType: "text/plain", size: 6 },
    ]);

    rmSync(join(served.start.workspace, "attachments", "pixel.png"));
    expect((await replayedFiles(id)).map((content) => content["type"])).toEqual(["text", "resource_link", "resource_link"]);
  });
});

/** A 1x1 PNG of 70 bytes, as base64. */
const PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==";

const NOTES = Buffer.from("hello\n").toString("base64");

/** The content of the user updates of a load, from the user message with files. */
async function replayedFiles(id: string): Promise<Record<string, unknown>[]> {
  const before = served.wire.updates(id).length;
  await served.wire.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [] });
  const users = served.wire
    .updates(id)
    .slice(before)
    .filter((update) => update["sessionUpdate"] === "user_message_chunk")
    .map((update) => update["content"] as Record<string, unknown>);
  const start = users.findIndex((content) => content["text"] === "What is this?");

  return users.slice(start);
}
