// One family's mounts in a temporary directory, read through the real edge,
// `readFamilyConfig`. A test names only what it changes: the plane URLs and
// the content of `runtime.json`, the other files of the config mount and the
// index root.

import { mkdirSync, mkdtempSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { readFamilyConfig } from "../src/config.js";
import type { FamilyConfig } from "../src/config.js";

/** Fixture values in the forms of the wire types `LitellmKey` and `PepToken`. */
export const FIXTURE_KEY = "sk-FIXTURE-LITELLM-KEY";
export const FIXTURE_TOKEN = "feedface".repeat(8);

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

/** The mounts on disk, and the env that names them. The facade's tests start its bin with it. */
export function familyEnv(fixture: FamilyFixture): Record<string, string> {
  const root = mkdtempSync(join(tmpdir(), "agent-family-"));
  const dir = (name: string): string => mkdtempSync(join(root, name));
  const credDir = dir("creds");
  const configDir = dir("config");
  writeCreds(credDir, { litellm_key: FIXTURE_KEY, pep_token: FIXTURE_TOKEN });
  writeFileSync(join(configDir, "runtime.json"), JSON.stringify(fixture.runtime ?? {}));
  writeFiles(configDir, fixture.configFiles ?? {});

  return {
    AGENT_SESSIONS_DIR: dir("sessions"),
    AGENT_CRED_DIR: credDir,
    AGENT_FAMILY_CONFIG_DIR: configDir,
    AGENT_CONTROL_DIR: dir("control"),
    AGENT_SANDBOX: "chat-s1",
    AGENT_PEP_URL: fixture.pepUrl ?? UNUSED_PLANE,
    AGENT_LITELLM_BASE_URL: fixture.litellmUrl ?? UNUSED_PLANE,
    ...(fixture.indexRoot === undefined ? {} : { AGENT_INDEX_ROOT: fixture.indexRoot }),
  };
}

/** Files under `dir`, by path relative to it. */
function writeFiles(dir: string, files: Readonly<Record<string, string>>): void {
  for (const [name, text] of Object.entries(files)) {
    mkdirSync(dirname(join(dir, name)), { recursive: true });
    writeFileSync(join(dir, name), text);
  }
}

/** A new `config/` in place of `dir`, swapped as caregiver swaps it (`atomic.py`). */
export function swapConfig(dir: string, files: Readonly<Record<string, string>>): void {
  writeFiles(`${dir}.tmp`, files);
  renameSync(dir, `${dir}.old`);
  renameSync(`${dir}.tmp`, dir);
  rmSync(`${dir}.old`, { recursive: true });
}

/** A new `creds.json`, replaced by a rename as caregiver replaces it. */
export function writeCreds(dir: string, creds: Readonly<Record<string, unknown>>): void {
  writeFileSync(join(dir, "creds.json.tmp"), JSON.stringify(creds));
  renameSync(join(dir, "creds.json.tmp"), join(dir, "creds.json"));
}

export async function familyConfig(fixture: FamilyFixture): Promise<FamilyConfig> {
  const read = await readFamilyConfig(familyEnv(fixture), NO_RETRY_MS);
  if (!read.ok) {
    throw new Error(`fixture refused: ${read.refusal.reason}`);
  }

  return read.config;
}
