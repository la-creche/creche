// The facade pulls the family state from the vmm before each prompt
// (decision CH5a). A creds rotation and a config change between two
// prompts reach the next one, a sandbox grant too. A typed fault of the
// vmm fails that prompt, and the facade serves the next one.

import { afterAll, describe, expect, it } from "vitest";

import { FakeCh, SEARCH_TOOL } from "../../agent/test/fake-ch.js";
import { FakeModel } from "../../agent/test/fake-model.js";
import { BARE_CONFIG, CREDS, FIXTURE_TOKEN, stateLine } from "../../agent/test/fake-vmm.js";
import type { Answer } from "./acp-wire.js";
import { servedFamily } from "./served-family.js";

const NEW_KEY = "sk-FIXTURE-ROTATED-KEY";
const NEW_TOKEN = "deadbeef".repeat(8);
const SKILL = "---\nname: weather-report\ndescription: Write a short weather report.\n---\nSearch first.\n";
const NEW_CREDS = { ...CREDS, litellm_key: NEW_KEY, pep_token: NEW_TOKEN };
const NEW_CONFIG = {
  ...BARE_CONFIG,
  instructions: "FIXTURE-NEW",
  sandbox_tools: { ...BARE_CONFIG.sandbox_tools, read: true },
  skills: { "weather-report": SKILL },
};

const search = { tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } };
const ch = new FakeCh(FIXTURE_TOKEN);
const model = new FakeModel([search, { text: "One." }, search, { text: "Two." }]);
const served = await servedFamily({
  pepUrl: await ch.start(),
  litellmUrl: await model.start(),
  config: { instructions: "FIXTURE-OLD" },
});

afterAll(async () => {
  await served.stop();
  await model.stop();
  await ch.stop();
});

function prompt(): Promise<Answer> {
  return served.wire.request("session/prompt", { sessionId: served.id, prompt: [{ type: "text", text: "Weather?" }] });
}

/** The system message of the model request at `index`. */
function systemOf(index: number): string {
  const system = model.seen[index]?.messages.find((message) => message.role === "system");

  return JSON.stringify(system?.content ?? "");
}

describe("the pull of the family state before each prompt", () => {
  it("brings a rotated key, a rotated token, a new config and a new grant to the next prompt", async () => {
    expect((await prompt()).result).toEqual({ stopReason: "end_turn" });

    served.reply = stateLine(NEW_CONFIG, NEW_CREDS);
    ch.rotate(NEW_TOKEN);

    expect((await prompt()).result).toEqual({ stopReason: "end_turn" });
    expect(model.seen.map((seen) => seen.authorization)).toEqual([
      `Bearer sk-FIXTURE-LITELLM-KEY`,
      `Bearer sk-FIXTURE-LITELLM-KEY`,
      `Bearer ${NEW_KEY}`,
      `Bearer ${NEW_KEY}`,
    ]);
    expect([ch.calls.length, ch.refused]).toEqual([2, 0]);
    expect(systemOf(0)).toContain("FIXTURE-OLD");
    expect(systemOf(2)).toContain("FIXTURE-NEW");
    expect(model.seen[2]?.tools).toContain("activate_skill");
    expect([model.seen[0]?.tools.includes("read"), model.seen[2]?.tools.includes("read")]).toEqual([false, true]);
  });

  it("fails a prompt at a typed fault of the vmm, and serves the next one", async () => {
    served.reply = JSON.stringify({ creds_missing: {} });
    const failed = await prompt();

    served.reply = stateLine(NEW_CONFIG, NEW_CREDS);
    const next = await prompt();

    expect(JSON.stringify(failed.error)).toContain("creds_missing");
    expect(next.result).toEqual({ stopReason: "end_turn" });
  });
});
