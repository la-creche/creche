// The facade with the real agent module, in process: one family's mounts,
// the agent's fake LiteLLM and fake chaperone (`test/served-family.ts`).
// One turn calls a `ch` tool: `tool_call`, then `tool_call_update` completed.

import { existsSync, realpathSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { FakeCh, SEARCH_TOOL } from "../../agent/test/fake-ch.js";
import { FakeModel } from "../../agent/test/fake-model.js";
import { FIXTURE_TOKEN } from "../../agent/test/family.js";
import { facadeEnv } from "./facade-process.js";
import { servedFamily } from "./served-family.js";

const ch = new FakeCh(FIXTURE_TOKEN);
const model = new FakeModel([{ tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } }, { text: "Sunny." }]);
const env = facadeEnv({ pepUrl: await ch.start(), litellmUrl: await model.start() });
const served = await servedFamily(env);

afterAll(async () => {
  await served.runtime?.stop();
  await model.stop();
  await ch.stop();
});

describe("the facade with the agent module", () => {
  it("shares one Flue with the agent module", () => {
    // Flue's hooks need the one copy that runs `start()`.
    const flue = (dir: string): string => realpathSync(fileURLToPath(new URL(`${dir}/node_modules/@flue/runtime`, import.meta.url)));

    expect(flue("../../agent"), "agent/ has a Flue of its own: see facade/AGENTS.md").toBe(flue(".."));
  });

  it("runs one turn that calls a ch tool, on <sessions>/flue.db", async () => {
    const { id, wire } = served;
    const init = await wire.request("initialize", vectors.initialize.request.params);
    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Weather?" }] });
    const tools = wire.updates(id).filter((update) => String(update["sessionUpdate"]).startsWith("tool_call"));
    const sessions = String(env["AGENT_SESSIONS_DIR"]);

    expect(init.result).toEqual(vectors.initialize.response.result);
    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(tools.map((update) => `${String(update["sessionUpdate"])} ${String(update["status"])}`)).toEqual([
      "tool_call in_progress",
      "tool_call_update completed",
    ]);
    expect(ch.calls).toEqual([{ name: SEARCH_TOOL, args: { text: "weather" } }]);
    expect(existsSync(join(sessions, "flue.db"))).toBe(true);
    expect(existsSync(join(sessions, "workspace"))).toBe(true);
  });
});
