// The live model under Flue's `start()` (decisions CH5c, CH5c-2). A turn of
// session A holds on a `ch` call while session B pulls a new alias. A's
// next model request keeps A's model. B's prompt calls the new alias with
// its limits from LiteLLM, and so does A's next prompt. A pull that rotates
// the key and changes the alias reads the limits with the new key. A
// session that pulled the old alias before a change still resolves it.

import { init } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent } from "../src/agent.js";
import { vmmBackend } from "../src/family-state.js";
import type { StateBackend } from "../src/family-state.js";
import { FALLBACK_LIMITS } from "../src/litellm.js";
import { FakeCh, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { closeFakeVmms, CREDS, stateLine } from "./fake-vmm.js";
import { BARE_CONFIG, FIXTURE_TOKEN, testFamily } from "./family.js";

const NEW_ALIAS = "code-router";
const NEW_LIMITS = { contextWindow: 64000, maxTokens: 4096 };
const ROTATED_KEY = "sk-FIXTURE-ROTATED-KEY";

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([
  { tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } },
  { text: "A." },
  { text: "B." },
  { text: "A again." },
]);
fake.modelInfo = { data: [{ model_name: NEW_ALIAS, model_info: { max_input_tokens: 64000, max_output_tokens: 4096 } }] };
const family = await testFamily({ pepUrl: await ch.start(), litellmUrl: await fake.start() });

const composed = composeAgent(family.start);
const backend: StateBackend = vmmBackend(family.vmm.path, composed.state.config, () => undefined);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
  closeFakeVmms();
});

/** The state that the vmm gives each later pull: the alias, and the key when it is given. */
function nextState(alias: string, key: string = CREDS.litellm_key): void {
  family.reply = stateLine({ ...BARE_CONFIG, model_alias: alias }, { ...CREDS, litellm_key: key });
}

/** The pull of the facade for `session`: the alias goes to Flue before the state. */
async function pullFor(session: string): Promise<unknown> {
  const pulled = await backend.pull();
  if (!pulled.ok) {
    throw new Error(pulled.fault);
  }

  const switched = await composed.model.follow(pulled.state.config.modelAlias, pulled.state.creds.litellmKey);
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

    nextState(NEW_ALIAS);
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

  it("still resolves the alias of a session that pulled it before another session's change", async () => {
    await pullFor("session-x");
    nextState("chat");
    await pullFor("session-y");
    const before = fake.seen.length;
    await turn("session-x");

    expect(fake.seen.slice(before).map((seen) => seen.model)).toEqual([NEW_ALIAS]);
  });

  it("reads the limits of a new alias with the key of the same pull", async () => {
    nextState("chat");
    await pullFor("session-a");

    nextState(NEW_ALIAS, ROTATED_KEY);
    const switched = await pullFor("session-a");

    expect(switched).toEqual({ ok: true, limits: NEW_LIMITS });
    expect(fake.infoAuthorizations.at(-1)).toBe(`Bearer ${ROTATED_KEY}`);
  });
});
