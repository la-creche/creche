// The one generic agent under Flue's `start()`, with the store of its
// start. A fake model calls a fake `ch` tool, and the chunks show
// `tool-input` then `tool-output`. The model sees the family's
// instructions, the skill catalog and the whole tool roster.

import { existsSync } from "node:fs";

import { init } from "@flue/runtime";
import type { ConversationStreamChunk } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { AGENT_NAME, composeAgent } from "../src/agent.js";
import { FakeCh, SEARCH_RESULT, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { closeFakeVmms } from "./fake-vmm.js";
import { FIXTURE_TOKEN, testFamily } from "./family.js";

const SEARCH = `mcp__ch__${SEARCH_TOOL}`;
const ARGS = { text: "weather" };
const INSTRUCTIONS = "FIXTURE-INSTRUCTIONS: answer in French.";
const SKILL = "---\nname: weather-report\ndescription: Write a short weather report.\n---\nUse the search tool first.\n";

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([{ tool: SEARCH, args: ARGS }, { text: "done" }]);
const family = await testFamily({
  pepUrl: await ch.start(),
  litellmUrl: await fake.start(),
  config: { instructions: INSTRUCTIONS, skills: { "weather-report": SKILL, broken: "no frontmatter" } },
});

const composed = composeAgent(family.start);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
  closeFakeVmms();
});

function systemText(): string {
  const system = fake.seen[0]?.messages.find((message) => message.role === "system");

  return typeof system?.content === "string" ? system.content : JSON.stringify(system?.content ?? "");
}

describe("composeAgent", () => {
  it("names the agent and skips a skill that is not valid", () => {
    expect(composed.agent.agentName).toBe(AGENT_NAME);
    expect(composed.skippedSkills).toEqual(["broken"]);
  });

  it("runs a ch tool call under start(): tool-input, then tool-output", async () => {
    const chunks: ConversationStreamChunk[] = [];

    const handle = init(composed.agent);
    const receipt = await handle.dispatch("What is the weather?");
    const reply = await handle.read(receipt, { onEvent: (chunk) => chunks.push(chunk) });

    const tool = chunks.filter((chunk) => chunk.type.startsWith("tool-"));
    expect(tool.map((chunk) => chunk.type)).toEqual(["tool-input", "tool-output"]);
    expect(tool[0]).toMatchObject({ toolName: SEARCH, input: ARGS });
    expect(tool[1]).toMatchObject({ output: SEARCH_RESULT });
    expect(ch.calls).toEqual([{ name: SEARCH_TOOL, args: ARGS }]);
    expect(reply.text).toBe("done");
    expect(existsSync(family.start.store)).toBe(true);
  });

  it("gives the model the family's instructions, its skill and the whole roster", () => {
    // With an empty grant: no built-in sandbox tool. Flue adds `task` and,
    // with a skill, `activate_skill` (agent/AGENTS.md, "Known gaps").
    expect([...(fake.seen[0]?.tools ?? [])].sort()).toEqual(
      ["activate_skill", "codemode", "mcp__ch__ha__gated_echo", SEARCH, "task"].sort(),
    );
    expect(systemText()).toContain(INSTRUCTIONS);
    expect(systemText()).toContain("weather-report");
  });
});
