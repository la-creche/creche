// The family edge: the agent module reads the family config at start and
// composes the agent. A refusal exits with a status of its own and one
// stderr line. Once the supervisor holds the lock, `serveFamily` starts
// Flue on `<AGENT_SESSIONS_DIR>/flue.db` and opens `creche_sessions` there.
// Before each prompt the facade pulls the family state from the mounts
// into the agent (decision CH5a).

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { start } from "@flue/runtime/node";
import { AGENT_NAME, composeAgent, fileBackend, readModelLimits, storePath } from "agent/agent";
import type { ComposedAgent, ComposeRead, LimitsRead, StateBackend, StateHolder } from "agent/agent";
import { readFamilyConfig } from "agent/config";
import type { ConfigRefusal, FamilyConfig } from "agent/config";

import { tapStores } from "./flue-history.js";
import { sessionService } from "./service.js";
import { SessionIndex } from "./session-index.js";
import { say } from "./stdio.js";
import type { ServingStart } from "./supervisor.js";
import type { Refresh } from "./turns.js";

type ComposeReason = Extract<ComposeRead, { readonly ok: false }>["reason"];

/** One exit status per refusal. The map is total: a new reason breaks the build. */
export const EXIT_BY_REFUSAL: { readonly [Reason in ConfigRefusal["reason"] | ComposeReason]: number } = {
  sandbox_unset: 2,
  mount_dir_unset: 7,
  sandbox_not_valid: 9,
  plane_unset: 10,
  plane_not_valid: 11,
  creds_not_valid: 12,
  runtime_not_valid: 13,
  model_alias_unset: 14,
  instructions_unreadable: 15,
};

export interface Family {
  readonly config: FamilyConfig;
  readonly composed: ComposedAgent;
  readonly workspace: string;
  /** The model's limits from LiteLLM, or the fallback and its fault. */
  readonly limits: LimitsRead;
}

export type FamilyRead =
  | { readonly ok: true; readonly family: Family }
  | { readonly ok: false; readonly exitCode: number; readonly detail: string };

/** The reason, then each name that the refusal holds. A refusal holds no value. */
function detailOf({ reason, ...names }: ConfigRefusal): string {
  const held = Object.values(names).flat().filter((name) => name !== null);

  return held.length === 0 ? reason : `${reason}: ${held.join(", ")}`;
}

export async function readFamily(env: NodeJS.ProcessEnv): Promise<FamilyRead> {
  const read = await readFamilyConfig(env);
  if (!read.ok) {
    return { ok: false, exitCode: EXIT_BY_REFUSAL[read.refusal.reason], detail: detailOf(read.refusal) };
  }

  const workspace = join(read.config.mounts.sessions, "workspace");
  const limits = await readModelLimits(read.config);
  const composed = composeAgent(read.config, workspace, limits.limits);

  return composed.ok
    ? { ok: true, family: { config: read.config, composed: composed.composed, workspace, limits } }
    : { ok: false, exitCode: EXIT_BY_REFUSAL[composed.reason], detail: composed.reason };
}

/**
 * Each pull sets the latest state and the config of the session's turn. A
 * kept config is written to stderr once, when the read starts to fail.
 */
function refreshOf(backend: StateBackend, state: StateHolder): Refresh {
  return async (sessionId) => {
    const pulled = await backend.pull();
    if (!pulled.ok) {
      return pulled.fault;
    }

    if (pulled.stale !== null) {
      say(`the family config did not read (${pulled.stale}); the last good config stays`);
    }

    state.apply(pulled.state, sessionId);
    return null;
  };
}

/** Flue on the composed store, and the session methods on the same file. */
export async function serveFamily({ config, composed, workspace }: Family): Promise<ServingStart> {
  try {
    mkdirSync(workspace, { recursive: true });
    const tapped = tapStores(composed.db);
    const runtime = await start({ agents: [composed.agent], db: tapped.adapter, providers: composed.providers });
    const index = new SessionIndex(new DatabaseSync(storePath(config)));
    const refresh = refreshOf(fileBackend(config.mounts, composed.state.config), composed.state);
    const sessions = sessionService({ agent: composed.agent, agentName: AGENT_NAME, index, store: tapped.store, refresh });

    return { ok: true, serving: { runtime, sessions } };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) };
  }
}
