// The facade pulls the family state before each prompt
// (decision CH5a). A creds rotation and a config swap between two prompts
// reach the next one, a sandbox grant too. A creds read failure fails that prompt, and the
// facade serves the next one. A config read failure keeps the last good
// config, with one stderr line.

import { rmSync } from "node:fs";
import { join } from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import { FakeCh, SEARCH_TOOL } from "../../agent/test/fake-ch.js";
import { FakeModel } from "../../agent/test/fake-model.js";
import { FIXTURE_TOKEN, swapConfig, writeCreds } from "../../agent/test/family.js";
import type { Answer } from "./acp-wire.js";
import { facadeEnv } from "./facade-process.js";
import { servedFamily } from "./served-family.js";

const NEW_KEY = "sk-FIXTURE-ROTATED-KEY";
const NEW_TOKEN = "deadbeef".repeat(8);
const SKILL = "---\nname: weather-report\ndescription: Write a short weather report.\n---\nSearch first.\n";
const KEPT_LINE = "the family config did not read (config_missing); the last good config stays";

const search = { tool: `mcp__ch__${SEARCH_TOOL}`, args: { text: "weather" } };
const ch = new FakeCh(FIXTURE_TOKEN);
const model = new FakeModel([search, { text: "One." }, search, { text: "Two." }]);
const env = facadeEnv({
  pepUrl: await ch.start(),
  litellmUrl: await model.start(),
  configFiles: { "instructions.md": "FIXTURE-OLD" },
});
const credDir = String(env["AGENT_CRED_DIR"]);
const configDir = String(env["AGENT_FAMILY_CONFIG_DIR"]);
const served = await servedFamily(env);

afterAll(async () => {
  await served.runtime?.stop();
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
  it("brings a rotated key, a rotated token, a swapped config and a new grant to the next prompt", async () => {
    expect((await prompt()).result).toEqual({ stopReason: "end_turn" });

    writeCreds(credDir, { epoch: 2, litellm_key: NEW_KEY, pep_token: NEW_TOKEN });
    ch.rotate(NEW_TOKEN);
    swapConfig(configDir, {
      "runtime.json": JSON.stringify({ model_alias: "chat", sandbox_tools: ["read"] }),
      "instructions.md": "FIXTURE-NEW",
      "skills/weather-report/SKILL.md": SKILL,
    });

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

  it("fails a prompt when creds.json does not read, and serves the next one", async () => {
    rmSync(join(credDir, "creds.json"));
    const failed = await prompt();

    writeCreds(credDir, { epoch: 3, litellm_key: NEW_KEY, pep_token: NEW_TOKEN });
    const next = await prompt();

    expect(JSON.stringify(failed.error)).toContain("creds_missing");
    expect(next.result).toEqual({ stopReason: "end_turn" });
  });

  it("keeps the last good config when the config does not read, with one stderr line", async () => {
    rmSync(configDir, { recursive: true });
    const answers = [await prompt(), await prompt()];

    expect(answers.map((answer) => answer.result)).toEqual([{ stopReason: "end_turn" }, { stopReason: "end_turn" }]);
    expect(served.stderr().split(KEPT_LINE).length - 1).toBe(1);
    expect(systemOf(model.seen.length - 1)).toContain("FIXTURE-NEW");
  });
});
