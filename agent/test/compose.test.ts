// The one generic agent under Flue's `start()`, with the store it composes,
// `<sessions>/flue.db`. A fake model calls a fake `ch` tool, and the chunks
// show `tool-input` then `tool-output`. The model sees the family's
// instructions, the skill catalog and the whole tool roster.

import { existsSync, mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { init } from "@flue/runtime";
import type { ConversationStreamChunk } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { AGENT_NAME, composeAgent } from "../src/agent.js";
import { FakeCh, SEARCH_RESULT, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig, FIXTURE_TOKEN } from "./family.js";

const SEARCH = `mcp__ch__${SEARCH_TOOL}`;
const ARGS = { text: "weather" };
const INSTRUCTIONS = "FIXTURE-INSTRUCTIONS: answer in French.";
const SKILL = "---\nname: weather-report\ndescription: Write a short weather report.\n---\nUse the search tool first.\n";

/** One empty store in the layout of `library/`, so `index_search` mounts. */
function indexRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "agent-compose-index-"));
  mkdirSync(join(root, "house"));
  const db = new DatabaseSync(join(root, "house", "store.db"));
  db.exec(`
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE chunks(id INTEGER PRIMARY KEY, path TEXT, ord INTEGER, text TEXT);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(text);
  `);
  db.close();

  return root;
}

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([{ tool: SEARCH, args: ARGS }, { text: "done" }]);
const config = await familyConfig({
  pepUrl: await ch.start(),
  litellmUrl: await fake.start(),
  indexRoot: indexRoot(),
  runtime: { model_alias: "chat" },
  configFiles: {
    "instructions.md": INSTRUCTIONS,
    "skills/weather-report/SKILL.md": SKILL,
    "skills/broken/SKILL.md": "no frontmatter",
  },
});

const read = composeAgent(config, mkdtempSync(join(tmpdir(), "agent-workspace-")));
if (!read.ok) {
  throw new Error(read.reason);
}

const { composed } = read;
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
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

  it("refuses a family with no model_alias", async () => {
    const bare = await familyConfig({ runtime: {} });

    expect(composeAgent(bare, tmpdir())).toEqual({ ok: false, reason: "model_alias_unset" });
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
    expect(existsSync(join(config.mounts.sessions, "flue.db"))).toBe(true);
  });

  it("gives the model the family's instructions, its skill and the whole roster", () => {
    // With an empty grant: no built-in sandbox tool. Flue adds `task` and,
    // with a skill, `activate_skill` (agent/AGENTS.md, "Known gaps").
    expect([...(fake.seen[0]?.tools ?? [])].sort()).toEqual(
      ["activate_skill", "codemode", "index_search", "mcp__ch__ha__gated_echo", SEARCH, "task"].sort(),
    );
    expect(systemText()).toContain(INSTRUCTIONS);
    expect(systemText()).toContain("weather-report");
  });
});
