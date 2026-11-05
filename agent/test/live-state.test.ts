// The live state under Flue's `start()`. A turn of session A holds on a
// `ch` call while session B pulls a new config. A's next model request
// keeps A's config. B's turn gets the new one (decision CH5c).

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { init } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent } from "../src/agent.js";
import { fileBackend } from "../src/family-state.js";
import type { StateBackend, StateHolder } from "../src/family-state.js";
import { FakeCh, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig, FIXTURE_TOKEN, swapConfig } from "./family.js";

const NO_RETRY_MS = 0;

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([{ tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } }, { text: "A." }, { text: "B." }]);
const config = await familyConfig({
  pepUrl: await ch.start(),
  litellmUrl: await fake.start(),
  runtime: { model_alias: "chat" },
  configFiles: { "instructions.md": "FIXTURE-OLD" },
});

const read = composeAgent(config, mkdtempSync(join(tmpdir(), "agent-workspace-")));
if (!read.ok) {
  throw new Error(read.reason);
}

const { composed } = read;
const state: StateHolder = composed.state;
const backend: StateBackend = fileBackend(config.mounts, state.config, NO_RETRY_MS);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
});

/** The pull of the facade for `session`. */
async function pullFor(session: string): Promise<void> {
  const pulled = await backend.pull();
  if (!pulled.ok) {
    throw new Error(pulled.fault);
  }

  state.apply(pulled.state, session);
}

function systemOf(index: number): string {
  const system = fake.seen[index]?.messages.find((message) => message.role === "system");

  return JSON.stringify(system?.content ?? "");
}

describe("the config of each session", () => {
  it("does not change a turn of session A when session B pulls", async () => {
    let release = (): void => undefined;
    const held = ch.holdNextCall(new Promise((resolve) => (release = () => resolve())));
    await pullFor("session-a");
    const a = init(composed.agent, { id: "session-a" });
    const turnA = a.read(await a.dispatch("Weather?"));
    await held;

    swapConfig(config.mounts.config, { "runtime.json": JSON.stringify({ model_alias: "chat" }), "instructions.md": "FIXTURE-NEW" });
    await pullFor("session-b");
    release();
    await turnA;
    const b = init(composed.agent, { id: "session-b" });
    await b.read(await b.dispatch("Weather?"));

    expect(fake.seen.map((_, index) => systemOf(index).includes("FIXTURE-NEW"))).toEqual([false, false, true]);
    expect(systemOf(1)).toContain("FIXTURE-OLD");
  });
});
