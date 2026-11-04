// The tool env from the edge to a tool process. `readFamilyConfig` reads
// `MPLBACKEND` from the env, `composeAgent` hands it to the granted
// sandbox, and the model's `bash` call sees it. One process holds one
// Flue runtime, so this file starts its own.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { init } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { composeAgent } from "../src/agent.js";
import { readFamilyConfig } from "../src/config.js";
import { FakeCh } from "./fake-ch.js";
import { FakeModel } from "./fake-model.js";
import { familyEnv, FIXTURE_TOKEN } from "./family.js";

const NO_RETRY_MS = 0;

const fake = new FakeModel([{ tool: "bash", args: { command: 'printf %s "$MPLBACKEND"' } }, { text: "done" }]);
const ch = new FakeCh(FIXTURE_TOKEN);
const runtime = { model_alias: "chat", sandbox_tools: ["bash"] };
const env = familyEnv({ pepUrl: await ch.start(), litellmUrl: await fake.start(), runtime });
const family = await readFamilyConfig({ ...env, MPLBACKEND: "Agg" }, NO_RETRY_MS);
const read = family.ok ? composeAgent(family.config, mkdtempSync(join(tmpdir(), "agent-workspace-"))) : family;
if (!read.ok) {
  throw new Error("the bash family did not compose");
}

const { composed } = read;
const flue = await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
  await ch.stop();
});

describe("the tool env from the edge", () => {
  it("reaches the granted bash tool", async () => {
    const handle = init(composed.agent);
    await handle.read(await handle.dispatch("Go."));

    expect(fake.toolResults.join("\n")).toContain("Agg");
  });
});
