// The `ch` connection under Flue's `start()`: the fake chaperone checks the
// family bearer, the model sees the upstream tools as
// `mcp__ch__<upstream>__<tool>`, and a gated call's "Awaiting permission"
// result reaches the model as a tool error.

import { createMcpConnection, init, useMcpConnection, useModel } from "@flue/runtime";
import type { ConversationStreamChunk } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { chaperoneConnection } from "../src/chaperone.js";
import { litellmModel } from "../src/litellm.js";
import { FakeCh, gateText, GATED_TOOL, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig, FIXTURE_TOKEN } from "./family.js";

const GATED = `mcp__ch__${GATED_TOOL}`;
const SEARCH = `mcp__ch__${SEARCH_TOOL}`;
const ARGS = { text: "hello" };

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([{ tool: GATED, args: ARGS }, { text: "waiting" }]);
const config = await familyConfig({
  pepUrl: await ch.start(),
  litellmUrl: await fake.start(),
  runtime: { model_alias: "chat" },
});
const read = litellmModel(config);
if (!read.ok) {
  throw new Error(read.reason);
}

const { model } = read;
const connection = chaperoneConnection(config);

function Probe(): string {
  useModel(model.spec);
  useMcpConnection(connection);

  return "Use the tools.";
}

const flue = await start({ agents: [Probe], providers: [model.provider] });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
});

describe("chaperoneConnection", () => {
  it("points at <pep>/mcp and holds no credential text", () => {
    expect(String(connection.url)).toBe(`${config.planes.pep.text}/mcp`);
    expect(connection.name).toBe("ch");
    expect(JSON.stringify(connection)).not.toContain(FIXTURE_TOKEN);
  });

  it("is refused by the chaperone with another bearer", async () => {
    const before = ch.refused;

    await expect(createMcpConnection({ ...connection, auth: "FIXTURE-WRONG" })).rejects.toThrow();
    expect(ch.refused).toBeGreaterThan(before);
  });

  it("gives the model mcp__ch__ tools, and the gate text as a tool error", async () => {
    const chunks: ConversationStreamChunk[] = [];

    const handle = init(Probe);
    const receipt = await handle.dispatch("Echo hello.");
    await handle.read(receipt, { onEvent: (chunk) => chunks.push(chunk) });

    // Flue adds its own `task` tool to every agent (agent/AGENTS.md, "Known gaps").
    expect(fake.seen[0]?.tools).toEqual(["task", SEARCH, GATED]);
    expect(ch.calls).toEqual([{ name: GATED_TOOL, args: ARGS }]);

    const tool = chunks.filter((chunk) => chunk.type.startsWith("tool-"));
    expect(tool.map((chunk) => chunk.type)).toEqual(["tool-input", "tool-output-error"]);
    expect(tool[1]).toMatchObject({ errorText: expect.stringContaining(gateText(GATED_TOOL)) });
    expect(fake.toolResults).toEqual([expect.stringContaining(gateText(GATED_TOOL))]);

    const settled = chunks.find((chunk) => chunk.type === "submission-settled");
    expect(settled).toMatchObject({ outcome: "completed" });
  });
});
