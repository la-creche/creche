// The tool env from the edge to a tool process. `readGuestStart` reads
// `MPLBACKEND` from the env, `composeAgent` hands it to the granted
// sandbox, and the model's `bash` call sees it. One process holds one
// Flue runtime, so this file starts its own.

import { mkdirSync } from "node:fs";

import { init } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent } from "../src/agent.js";
import { FakeCh } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { closeFakeVmms } from "./fake-vmm.js";
import { BARE_CONFIG, FIXTURE_TOKEN, testFamily } from "./family.js";

const fake = new FakeModel([{ tool: "bash", args: { command: 'printf %s "$MPLBACKEND"' } }, { text: "done" }]);
const ch = new FakeCh(FIXTURE_TOKEN);
const family = await testFamily({
  pepUrl: await ch.start(),
  litellmUrl: await fake.start(),
  config: { sandbox_tools: { ...BARE_CONFIG.sandbox_tools, bash: true } },
  env: { MPLBACKEND: "Agg" },
});

// The facade makes the workspace before Flue starts (`serveFamily`).
mkdirSync(family.start.workspace);
const composed = composeAgent(family.start);
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
  closeFakeVmms();
});

describe("the tool env from the edge", () => {
  it("reaches the granted bash tool", async () => {
    const handle = init(composed.agent);
    await handle.read(await handle.dispatch("Go."));

    expect(fake.toolResults.join("\n")).toContain("Agg");
  });
});
