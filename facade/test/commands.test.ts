// The slash commands of a session (`src/commands.ts`): one for each granted
// skill, sent on `session/load`, on `session/resume` and at each prompt.

import { randomUUID } from "node:crypto";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { defineSkill, useModel } from "@flue/runtime";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import type { AvailableCommand } from "../src/acp.js";
import { commandsOf } from "../src/commands.js";
import { sessionService } from "../src/service.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux, freshRewind, startFlue } from "./faux-flue.js";

function Plain(): string {
  useModel(FAUX_MODEL);

  return "Answer.";
}

const flue = await startFlue([Plain]);
afterAll(() => flue.stop());

const GRILL = { name: "grill-me", description: "Ask hard questions about a plan." };
const TDD = { name: "tdd", description: "Write the test first." };

let id: string;
let granted: AvailableCommand[];
let wire: Wire;

beforeEach(() => {
  id = randomUUID();
  granted = [GRILL];
  const service = sessionService({
    agent: Plain,
    agentName: "Plain",
    sessionId: id,
    link: null,
    store: flue.store,
    workspace: flue.workspace,
    commands: () => granted,
    rewind: freshRewind(),
  });
  wire = new Wire(facadeApp(service));
});

function listed(commands: AvailableCommand[]): Record<string, unknown> {
  return { sessionUpdate: "available_commands_update", availableCommands: commands };
}

function prompt(text: string): Promise<unknown> {
  faux.setResponses([fauxAssistantMessage("Done.")]);
  return wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text }] });
}

describe("commandsOf", () => {
  it("gives one command for each skill, by name", () => {
    const skills = [TDD, GRILL].map((skill) => defineSkill({ ...skill, instructions: "Do it." }));

    expect(commandsOf(skills)).toEqual([GRILL, TDD]);
  });
});

describe("the command list", () => {
  it("goes to the link on session/load, before the answer", async () => {
    const answer = await wire.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [] });

    expect(answer.error).toBeUndefined();
    expect(wire.updates(id)).toEqual([listed([GRILL])]);
  });

  it("goes to the link on session/resume, before the answer", async () => {
    const answer = await wire.request("session/resume", { sessionId: id, cwd: "/work" });

    expect(answer.result).toEqual({});
    expect(wire.updates(id)).toEqual([listed([GRILL])]);
  });

  it("opens each turn with the list of that turn's pull", async () => {
    await prompt("/grill-me my plan");
    granted = [GRILL, TDD];
    const seen = wire.updates(id).length;
    await prompt("More.");

    expect(wire.updates(id)[0]).toEqual(listed([GRILL]));
    expect(wire.updates(id)[seen]).toEqual(listed([GRILL, TDD]));
  });
});
