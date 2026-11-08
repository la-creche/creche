// The family edge. On sbx the agent module reads the family config at
// start and composes the agent. Under CH it takes the fixed guest paths,
// the guest planes and the state of one pull on the family-state socket,
// and reads no `AGENT_*` variable (`agent/src/guest-start.ts`, decisions
// CH5e, CH80). A refusal exits with a status of its own and one stderr
// line. Once the supervisor holds the lock, `serveFamily` starts Flue on
// the start's `flue.db` and opens `creche_sessions` there. Before each
// prompt the facade pulls the family state into the agent: on sbx from the
// mounts, under CH from the vmm (decision CH5a). A pulled `model_alias`
// that differs switches the model at that prompt (decisions CH5c, CH5c-2).

import { mkdirSync } from "node:fs";
import type { Readable } from "node:stream";
import { DatabaseSync } from "node:sqlite";

import { start } from "@flue/runtime/node";
import {
  AGENT_NAME,
  composeAgent,
  FAMILY_STATE_SOCKET,
  fileBackend,
  readGuestStart,
  readModelLimits,
  startOf,
  vmmBackend,
} from "agent/agent";
import type { AgentStart, ComposedAgent, LimitsRead, StartRead, StateBackend } from "agent/agent";
import { readFamilyConfig } from "agent/config";
import type { ConfigRefusal } from "agent/config";

import { tapStores } from "./flue-history.js";
import { guestLink } from "./local-link.js";
import type { LocalLink } from "./local-link.js";
import { SupervisorLock } from "./lock.js";
import { readMode } from "./mode.js";
import type { FacadeMode } from "./mode.js";
import { sessionService } from "./service.js";
import { SessionIndex } from "./session-index.js";
import { say } from "./stdio.js";
import type { ServingStart, Supervision } from "./supervisor.js";
import type { Refresh } from "./turns.js";

type ComposeReason = Extract<StartRead, { readonly ok: false }>["reason"];

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

/** `CRECHE_SESSION_ID` is set, and it is not a session id (`src/mode.ts`). */
export const EXIT_SESSION_ID = 17;

export interface Family {
  readonly mode: FacadeMode;
  readonly composed: ComposedAgent;
  /** The model's limits from LiteLLM, or the fallback and its fault. */
  readonly limits: LimitsRead;
  /** The pull before each prompt: the mounts on sbx, the vmm under CH. */
  readonly backend: StateBackend;
  /** What holds the facade: stdio and the lock on sbx, the local link under CH. */
  readonly supervision: Supervision;
}

export type FamilyRead =
  | { readonly ok: true; readonly family: Family }
  | { readonly ok: false; readonly exitCode: number; readonly detail: string };

/** The reason, then each name that the refusal holds. A refusal holds no value. */
function detailOf({ reason, ...names }: ConfigRefusal): string {
  const held = Object.values(names).flat().filter((name) => name !== null);

  return held.length === 0 ? reason : `${reason}: ${held.join(", ")}`;
}

/** The agent of `start`, with the limits of its alias from LiteLLM. */
async function composeStart(start: AgentStart): Promise<{ readonly composed: ComposedAgent; readonly limits: LimitsRead }> {
  const { state } = start;
  const limits = await readModelLimits(start.planes, state.config.modelAlias, state.creds.litellmKey);

  return { composed: composeAgent(start, limits.limits), limits };
}

/**
 * The family of the facade. `input` is attendance's stdio on sbx. Under CH
 * the facade reads no stdio, and the start pulls until the vmm gives a
 * state (decision CH23 rule 2).
 */
export async function readFamily(env: NodeJS.ProcessEnv, input: Readable): Promise<FamilyRead> {
  const mode = readMode(env);
  if (!mode.ok) {
    return { ok: false, exitCode: EXIT_SESSION_ID, detail: mode.detail };
  }

  if (mode.mode.kind === "ch") {
    const guest = await readGuestStart(env, say);
    if (!guest.ok) {
      return { ok: false, exitCode: EXIT_BY_REFUSAL[guest.reason], detail: guest.reason };
    }

    const { composed, limits } = await composeStart(guest.start);
    const backend = vmmBackend(FAMILY_STATE_SOCKET, composed.state.config, say);
    const supervision: Supervision = { kind: "ch", link: guestLink(say) };

    return { ok: true, family: { mode: mode.mode, composed, limits, backend, supervision } };
  }

  const read = await readFamilyConfig(env);
  if (!read.ok) {
    return { ok: false, exitCode: EXIT_BY_REFUSAL[read.refusal.reason], detail: detailOf(read.refusal) };
  }

  const { config } = read;
  const started = startOf(config);
  if (!started.ok) {
    return { ok: false, exitCode: EXIT_BY_REFUSAL[started.reason], detail: started.reason };
  }

  const { composed, limits } = await composeStart(started.start);
  const backend = fileBackend(config.mounts, composed.state.config);
  const supervision: Supervision = { kind: "sbx", input, lock: new SupervisorLock(config.mounts.control, config.sandbox) };

  return { ok: true, family: { mode: mode.mode, composed, limits, backend, supervision } };
}

/** The stderr line of a limits read. `alias` names the new model of a change, and is null at start. */
export function limitsLine(read: LimitsRead, alias: string | null = null): string {
  const model = alias === null ? "model" : `model ${alias}`;
  const { contextWindow, maxTokens } = read.limits;

  return read.ok
    ? `${model} window ${contextWindow} tokens, output ${maxTokens}`
    : `${model} limits not read (${read.fault}); window ${contextWindow} tokens`;
}

/**
 * Each pull sets the latest state and the config of the session's turn. A
 * kept config is written to stderr once, when the read starts to fail. A
 * new alias goes to Flue before the state, so the prompt resolves it. The
 * facade waits for no `model_live_at`: under CH the vmm holds the reply
 * until then (decision CH31 rule 1).
 */
function refreshOf(backend: StateBackend, { state, model }: ComposedAgent): Refresh {
  return async (sessionId) => {
    const pulled = await backend.pull();
    if (!pulled.ok) {
      return pulled.fault;
    }

    if (pulled.stale !== null) {
      say(`the family config did not read (${pulled.stale}); the last good config stays`);
    }

    const { modelAlias } = pulled.state.config;
    const switched = await model.follow(modelAlias, pulled.state.creds.litellmKey);
    if (switched !== null) {
      say(limitsLine(switched, modelAlias));
    }

    state.apply(pulled.state, sessionId);
    return null;
  };
}

/**
 * Flue on the composed store, and the session methods on the same file.
 * `link` is the local link of guest init under CH, or null on sbx.
 */
export async function serveFamily({ mode, composed, backend }: Family, link: LocalLink | null): Promise<ServingStart> {
  try {
    mkdirSync(composed.workspace, { recursive: true });
    const tapped = tapStores(composed.db);
    const runtime = await start({ agents: [composed.agent], db: tapped.adapter, providers: composed.providers });
    const index = new SessionIndex(new DatabaseSync(composed.store));
    const refresh = refreshOf(backend, composed);
    const sessions = sessionService({ agent: composed.agent, agentName: AGENT_NAME, index, mode, link, store: tapped.store, refresh });

    return { ok: true, serving: { runtime, sessions } };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) };
  }
}
