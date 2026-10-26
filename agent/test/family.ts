// One family's mounts in a temporary directory, read through the real edge,
// `readFamilyConfig`. A test names only what it changes: the plane URLs and
// the content of `runtime.json`, the other files of the config mount and the
// index root.

import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { readFamilyConfig } from "../src/config.js";
import type { FamilyConfig } from "../src/config.js";

export const FIXTURE_KEY = "FIXTURE-LITELLM-KEY";
export const FIXTURE_TOKEN = "FIXTURE-PEP-TOKEN";

const NO_RETRY_MS = 0;
const UNUSED_PLANE = "http://192.0.2.10:9";

export interface FamilyFixture {
  readonly pepUrl?: string;
  readonly litellmUrl?: string;
  readonly runtime?: Readonly<Record<string, unknown>>;
  /** Files of the config mount, by path relative to it. */
  readonly configFiles?: Readonly<Record<string, string>>;
  readonly indexRoot?: string;
}

export async function familyConfig(fixture: FamilyFixture): Promise<FamilyConfig> {
  const root = mkdtempSync(join(tmpdir(), "agent-family-"));
  const dir = (name: string): string => mkdtempSync(join(root, name));
  const credDir = dir("creds");
  const configDir = dir("config");
  const creds = { epoch: 1, litellm_key: FIXTURE_KEY, pep_token: FIXTURE_TOKEN };
  writeFileSync(join(credDir, "creds.json"), JSON.stringify(creds));
  writeFileSync(join(configDir, "runtime.json"), JSON.stringify(fixture.runtime ?? {}));
  for (const [name, text] of Object.entries(fixture.configFiles ?? {})) {
    mkdirSync(dirname(join(configDir, name)), { recursive: true });
    writeFileSync(join(configDir, name), text);
  }

  const read = await readFamilyConfig(
    {
      AGENT_SESSIONS_DIR: dir("sessions"),
      AGENT_CRED_DIR: credDir,
      AGENT_FAMILY_CONFIG_DIR: configDir,
      AGENT_CONTROL_DIR: dir("control"),
      AGENT_SANDBOX: "chat-s1",
      AGENT_PEP_URL: fixture.pepUrl ?? UNUSED_PLANE,
      AGENT_LITELLM_BASE_URL: fixture.litellmUrl ?? UNUSED_PLANE,
      ...(fixture.indexRoot === undefined ? {} : { AGENT_INDEX_ROOT: fixture.indexRoot }),
    },
    NO_RETRY_MS,
  );
  if (!read.ok) {
    throw new Error(`fixture refused: ${read.refusal.reason}`);
  }

  return read.config;
}
