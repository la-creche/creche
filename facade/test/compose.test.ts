// The built `creche-facade` with the real agent module, as attendance runs
// it: one family's mounts, the agent's fake LiteLLM and fake chaperone.
// One turn calls a `ch` tool: `tool_call`, then `tool_call_update` completed.

import { existsSync, realpathSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { FakeCh, SEARCH_TOOL } from "../../agent/test/fake-ch.js";
import { FakeModel } from "../../agent/test/fake-model.js";
import { FIXTURE_TOKEN } from "../../agent/test/family.js";
import { facadeEnv, startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";

type Message = Record<string, unknown>;

const ch = new FakeCh(FIXTURE_TOKEN);
const model = new FakeModel([{ tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } }, { text: "Sunny." }]);
const env = facadeEnv({ pepUrl: await ch.start(), litellmUrl: await model.start() });
let proc: FacadeProcess | null = null;

afterAll(async () => {
  proc?.child.kill();
  await model.stop();
  await ch.stop();
});

/** Sends one request, and gathers each update until its answer. */
async function exchange(to: FacadeProcess, id: number, method: string, params: unknown): Promise<[Message, Message[]]> {
  to.send({ jsonrpc: "2.0", id, method, params });
  const updates: Message[] = [];
  for (;;) {
    const message = (await to.nextMessage()) as Message;
    if (message["id"] === id) {
      return [message, updates];
    }

    updates.push((message["params"] as { update: Message }).update);
  }
}

describe("creche-facade with the agent module", () => {
  it("shares one Flue with the agent module", () => {
    // Flue's hooks need the one copy that runs `start()`.
    const flue = (dir: string): string => realpathSync(fileURLToPath(new URL(`${dir}/node_modules/@flue/runtime`, import.meta.url)));

    expect(flue("../../agent"), "agent/ has a Flue of its own: see facade/AGENTS.md").toBe(flue(".."));
  });

  it("runs one turn that calls a ch tool, on <sessions>/flue.db", async () => {
    proc = startFacade(env);
    const [init] = await exchange(proc, 0, "initialize", vectors.initialize.request.params);
    const [made] = await exchange(proc, 1, "session/new", { cwd: "/work", mcpServers: [] });
    const sessionId = (made["result"] as { sessionId: string }).sessionId;
    const [answer, updates] = await exchange(proc, 2, "session/prompt", { sessionId, prompt: [{ type: "text", text: "Weather?" }] });
    const tools = updates.filter((update) => String(update["sessionUpdate"]).startsWith("tool_call"));
    const sessions = String(env["AGENT_SESSIONS_DIR"]);

    expect(init["result"]).toEqual(vectors.initialize.response.result);
    expect(answer["result"]).toEqual({ stopReason: "end_turn" });
    expect(tools.map((update) => `${String(update["sessionUpdate"])} ${String(update["status"])}`)).toEqual([
      "tool_call in_progress",
      "tool_call_update completed",
    ]);
    expect(ch.calls).toEqual([{ name: SEARCH_TOOL, args: { text: "weather" } }]);
    expect(new DatabaseSync(join(sessions, "flue.db")).prepare("SELECT id FROM creche_sessions").all()).toEqual([{ id: sessionId }]);
    expect(existsSync(join(sessions, "workspace"))).toBe(true);
  });
});
