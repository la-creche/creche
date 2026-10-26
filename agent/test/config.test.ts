// The edge of the agent: the environment and the mounts become one typed
// `FamilyConfig`, or one typed refusal. No case throws.

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { inspect } from "node:util";

import { describe, expect, it } from "vitest";

import { readFamilyConfig } from "../src/config.js";
import { revealSecret } from "../src/secret.js";

const KEY = "FIXTURE-LITELLM-KEY";
const TOKEN = "FIXTURE-PEP-TOKEN";
const NO_RETRY_MS = 0;

function mounts(creds: unknown): NodeJS.ProcessEnv {
  const root = mkdtempSync(join(tmpdir(), "agent-config-"));
  const dir = (name: string): string => mkdtempSync(join(root, name));
  const credDir = dir("creds");
  const configDir = dir("config");
  writeFileSync(join(credDir, "creds.json"), JSON.stringify(creds));
  writeFileSync(join(configDir, "runtime.json"), JSON.stringify({ model_alias: "chat" }));

  return {
    AGENT_SESSIONS_DIR: dir("sessions"),
    AGENT_CRED_DIR: credDir,
    AGENT_FAMILY_CONFIG_DIR: configDir,
    AGENT_CONTROL_DIR: dir("control"),
    AGENT_SANDBOX: "chat-s1",
    AGENT_PEP_URL: "http://192.0.2.10:8300",
    AGENT_LITELLM_BASE_URL: "http://192.0.2.10:4000",
  };
}

const VALID = { epoch: 3, litellm_key: KEY, pep_token: TOKEN };

describe("readFamilyConfig", () => {
  it("refuses an env with no AGENT_CRED_DIR as mount_dir_unset", async () => {
    const env = mounts(VALID);
    delete env["AGENT_CRED_DIR"];

    const read = await readFamilyConfig(env, NO_RETRY_MS);

    expect(read).toEqual({
      ok: false,
      refusal: { reason: "mount_dir_unset", variables: ["AGENT_CRED_DIR"] },
    });
  });

  it("refuses a creds.json with no pep_token by name and echoes no value", async () => {
    const read = await readFamilyConfig(mounts({ epoch: 3, litellm_key: KEY }), NO_RETRY_MS);

    expect(read).toEqual({ ok: false, refusal: { reason: "creds_not_valid", field: "pep_token" } });
    expect(JSON.stringify(read)).not.toContain(KEY);
  });

  it("refuses a plane variable that is not a plane URL, by fault", async () => {
    const env = { ...mounts(VALID), AGENT_PEP_URL: "http://192.0.2.10:8300/" };

    const read = await readFamilyConfig(env, NO_RETRY_MS);

    expect(read).toEqual({
      ok: false,
      refusal: { reason: "plane_not_valid", variable: "AGENT_PEP_URL", fault: "after_authority" },
    });
  });

  it("refuses a sandbox id that is not valid", async () => {
    const read = await readFamilyConfig({ ...mounts(VALID), AGENT_SANDBOX: "Chat" }, NO_RETRY_MS);

    expect(read).toEqual({ ok: false, refusal: { reason: "sandbox_not_valid" } });
  });

  it("parses a valid set", async () => {
    const env = mounts(VALID);

    const read = await readFamilyConfig(env, NO_RETRY_MS);
    if (!read.ok) {
      throw new Error(read.refusal.reason);
    }

    const { config } = read;
    expect(config.sandbox).toBe("chat-s1");
    expect(config.mounts.creds).toBe(env["AGENT_CRED_DIR"]);
    expect(config.mounts.indexRoot).toBeNull();
    expect(config.planes.pep.text).toBe("http://192.0.2.10:8300");
    expect(config.creds.epoch).toBe(3);
    expect(revealSecret(config.creds.pepToken)).toBe(TOKEN);
    expect(config.runtime.modelAlias).toBe("chat");
  });

  it("never shows a secret as text", async () => {
    const read = await readFamilyConfig(mounts(VALID), NO_RETRY_MS);
    if (!read.ok) {
      throw new Error(read.refusal.reason);
    }

    const shown = [JSON.stringify(read), String(read.config.creds.litellmKey), inspect(read)];
    for (const text of shown) {
      expect(text).not.toContain(KEY);
      expect(text).not.toContain(TOKEN);
    }
  });
});
