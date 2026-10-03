// Contract 03 §7. The environment one pi process is born with.
//
// A resident process KEEPS the environment it started with, which is why §6's
// reap rule replaces a process whose `env_epoch` is stale. `AGENT_TURN` is
// therefore the turn that started the process, not the turn now running: it
// rides into PEP calls as an advisory header (contract 04) and grants nothing.
// `AGENT_TURN_FILE` is what names the CURRENT turn, and `turn-file.ts` says
// why the environment alone cannot.
//
// Credentials go here and nowhere else. argv is world-readable through
// /proc/<pid>/cmdline for the whole life of the process (invariant 13).

import { INDEX_ROOT, LITELLM_BASE_URL, PEP_URL } from "./constants.js";
import type { FamilyCreds } from "./creds.js";

export interface TurnEnvSpec {
  readonly sessionDir: string;
  readonly family: string;
  readonly sandbox: string;
  readonly session: string;
  /** Absent for a process `open_session` started: no turn exists yet. */
  readonly turn?: string;
  /** Absent when the control mount gives no writable path. */
  readonly turnFile?: string;
  /** Where the bridge states its tool count. See `tool-state.ts`. */
  readonly toolStateFile?: string;
  readonly creds: FamilyCreds;
}

/** The image supplies these; the playpen passes them through unchanged. */
const INHERITED = ["HOME", "PATH", "TMPDIR", "LANG", "NODE_ENV"] as const;

export function buildTurnEnv(spec: TurnEnvSpec): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const name of INHERITED) {
    const value = process.env[name];
    if (value !== undefined) {
      env[name] = value;
    }
  }

  env["PI_CODING_AGENT_DIR"] = spec.sessionDir;
  env["LITELLM_BASE_URL"] = LITELLM_BASE_URL;
  env["LITELLM_VIRTUAL_KEY"] = spec.creds.litellm_key;
  env["PEP_URL"] = PEP_URL;
  env["PEP_TOKEN"] = spec.creds.pep_token;
  env["AGENT_FAMILY"] = spec.family;
  env["AGENT_SANDBOX"] = spec.sandbox;
  env["AGENT_SESSION"] = spec.session;
  env["AGENT_INDEX_ROOT"] = INDEX_ROOT;

  // Both are omitted rather than set empty: the bridge reads an absent value
  // as "unknown" and sends no header, which is honest.
  if (spec.turn !== undefined) {
    env["AGENT_TURN"] = spec.turn;
  }
  if (spec.turnFile !== undefined) {
    env["AGENT_TURN_FILE"] = spec.turnFile;
  }
  if (spec.toolStateFile !== undefined) {
    env["AGENT_TOOL_STATE_FILE"] = spec.toolStateFile;
  }

  return env;
}
