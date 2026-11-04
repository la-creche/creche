// The live model under Flue's `start()` (decisions CH5c, CH5c-2). A turn of
// session A holds on a `ch` call while session B pulls a new alias. A's
// next model request keeps A's model. B's prompt calls the new alias with
// its limits from LiteLLM, and so does A's next prompt.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { init } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent } from "../src/agent.js";
import { fileBackend } from "../src/family-state.js";
import type { StateBackend } from "../src/family-state.js";
import { FALLBACK_LIMITS } from "../src/litellm.js";
import { FakeCh, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig, FIXTURE_TOKEN, swapConfig } from "./family.js";

const NO_RETRY_MS = 0;
const NEW_ALIAS = "code-router";
const NEW_LIMITS = { contextWindow: 64000, maxTokens: 4096 };

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([
  { tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } },
  { text: "A." },
  { text: "B." },
  { text: "A again." },
]);
fake.modelInfo = { data: [{ model_name: NEW_ALIAS, model_info: { max_input_tokens: 64000, max_output_tokens: 4096 } }] };
const config = await familyConfig({ pepUrl: await ch.start(), litellmUrl: await fake.start(), runtime: { model_alias: "chat" } });

const read = composeAgent(config, mkdtempSync(join(tmpdir(), "agent-workspace-")));
if (!read.ok) {
  throw new Error(read.reason);
}

const { composed } = read;
const backend: StateBackend = fileBackend(config.mounts, composed.state.config, NO_RETRY_MS);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
});

/** The pull of the facade for `session`: the alias goes to Flue before the state. */
async function pullFor(session: string): Promise<unknown> {
  const pulled = await backend.pull();
  if (!pulled.ok) {
    throw new Error(pulled.fault);
  }

  const switched = await composed.model.follow(pulled.state.config.modelAlias);
  composed.state.apply(pulled.state, session);

  return switched;
}

async function turn(session: string): Promise<void> {
  const handle = init(composed.agent, { id: session });
  await handle.read(await handle.dispatch("Weather?"));
}

describe("the model of each session", () => {
  it("switches at the next prompt, and keeps a running turn on its model", async () => {
    let release = (): void => undefined;
    const held = ch.holdNextCall(new Promise((resolve) => (release = () => resolve())));
    expect(await pullFor("session-a")).toBeNull();
    const a = init(composed.agent, { id: "session-a" });
    const turnA = a.read(await a.dispatch("Weather?"));
    await held;

    swapConfig(config.mounts.config, { "runtime.json": JSON.stringify({ model_alias: NEW_ALIAS }) });
    const switched = await pullFor("session-b");
    release();
    await turnA;
    await turn("session-b");
    await pullFor("session-a");
    await turn("session-a");

    expect(switched).toEqual({ ok: true, limits: NEW_LIMITS });
    expect(fake.seen.map((seen) => seen.model)).toEqual(["chat", "chat", NEW_ALIAS, NEW_ALIAS]);
    expect(fake.seen.map((seen) => seen.maxTokens)).toEqual([
      FALLBACK_LIMITS.maxTokens,
      FALLBACK_LIMITS.maxTokens,
      NEW_LIMITS.maxTokens,
      NEW_LIMITS.maxTokens,
    ]);
  });
});
