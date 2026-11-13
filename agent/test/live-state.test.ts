// The live state under Flue's `start()`. A turn of session A holds on a
// `ch` call while session B pulls a new config. A's next model request
// keeps A's config. B's turn gets the new one (decision CH5c).

import { init } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent } from "../src/agent.js";
import { vmmBackend } from "../src/family-state.js";
import type { StateBackend, StateHolder } from "../src/family-state.js";
import { FakeCh, SEARCH_TOOL } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { closeFakeVmms, stateLine } from "./fake-vmm.js";
import { BARE_CONFIG, FIXTURE_TOKEN, testFamily } from "./family.js";

const ch = new FakeCh(FIXTURE_TOKEN);
const fake = new FakeModel([{ tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } }, { text: "A." }, { text: "B." }]);
const family = await testFamily({ pepUrl: await ch.start(), litellmUrl: await fake.start(), config: { instructions: "FIXTURE-OLD" } });

const composed = composeAgent(family.start);
const state: StateHolder = composed.state;
const backend: StateBackend = vmmBackend(family.vmm.path, state.config, () => undefined);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
  closeFakeVmms();
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

    family.reply = stateLine({ ...BARE_CONFIG, instructions: "FIXTURE-NEW" });
    await pullFor("session-b");
    release();
    await turnA;
    const b = init(composed.agent, { id: "session-b" });
    await b.read(await b.dispatch("Weather?"));

    expect(fake.seen.map((_, index) => systemOf(index).includes("FIXTURE-NEW"))).toEqual([false, false, true]);
    expect(systemOf(1)).toContain("FIXTURE-OLD");
  });
});
