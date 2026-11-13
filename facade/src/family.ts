// The family edge. The agent module takes the fixed guest paths, the guest
// planes and the state of one pull on the family-state socket, and reads
// no `AGENT_*` variable (`agent/src/guest-start.ts`, decisions CH5e, CH80,
// CH81 rule 5). A refusal exits with a status of its own and one stderr
// line. `serveFamily` starts Flue on the start's `flue.db` and opens the
// one session of `CRECHE_SESSION_ID` (decision CH21-8). Before each prompt
// the facade pulls the family state from the vmm into the agent (decision
// CH5a). A pulled `model_alias` that differs switches the model at that
// prompt (decisions CH5c, CH5c-2).

import { mkdirSync } from "node:fs";

import { start } from "@flue/runtime/node";
import { AGENT_NAME, composeAgent, FAMILY_STATE_SOCKET, readGuestStart, readModelLimits, vmmBackend } from "agent/agent";
import type { AgentStart, ComposedAgent, LimitsRead, StateBackend } from "agent/agent";
import type { Wire } from "agent/wire-read";

import { tapStores } from "./flue-history.js";
import { guestLink } from "./local-link.js";
import type { LocalLink } from "./local-link.js";
import { sessionService } from "./service.js";
import { readSessionId } from "./session-id.js";
import { say } from "./stdio.js";
import type { ServingStart } from "./supervisor.js";
import type { Refresh } from "./turns.js";

/** `CRECHE_SESSION_ID` is absent, or it is not a session id (`src/session-id.ts`). */
export const EXIT_SESSION_ID = 17;

export interface Family {
  /** The host's id of the one session of the VM. */
  readonly sessionId: Wire<"SessionId">;
  readonly composed: ComposedAgent;
  /** The model's limits from LiteLLM, or the fallback and its fault. */
  readonly limits: LimitsRead;
  /** The pull from the vmm before each prompt. */
  readonly backend: StateBackend;
  /** The local link of guest init: what holds the facade. */
  readonly link: LocalLink;
}

export type FamilyRead =
  | { readonly ok: true; readonly family: Family }
  | { readonly ok: false; readonly exitCode: number; readonly detail: string };

/** The agent of `start`, with the limits of its alias from LiteLLM. */
async function composeStart(start: AgentStart): Promise<{ readonly composed: ComposedAgent; readonly limits: LimitsRead }> {
  const { state } = start;
  const limits = await readModelLimits(start.planes, state.config.modelAlias, state.creds.litellmKey);

  return { composed: composeAgent(start, limits.limits), limits };
}

/**
 * The family of the facade. The facade reads no stdio, and the start pulls
 * until the vmm gives a state (decision CH23 rule 2).
 */
export async function readFamily(env: NodeJS.ProcessEnv): Promise<FamilyRead> {
  const read = readSessionId(env);
  if (!read.ok) {
    return { ok: false, exitCode: EXIT_SESSION_ID, detail: read.detail };
  }

  const { composed, limits } = await composeStart(await readGuestStart(env, say));
  const backend = vmmBackend(FAMILY_STATE_SOCKET, composed.state.config, say);

  return { ok: true, family: { sessionId: read.sessionId, composed, limits, backend, link: guestLink(say) } };
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
 * new alias goes to Flue before the state, so the prompt resolves it. The
 * facade waits for no `model_live_at`: the vmm holds the reply until then
 * (decision CH31 rule 1).
 */
function refreshOf(backend: StateBackend, { state, model }: ComposedAgent): Refresh {
  return async (sessionId) => {
    const pulled = await backend.pull();
    if (!pulled.ok) {
      return pulled.fault;
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

/** Flue on the composed store, and the methods of the one session. `link` is the local link of guest init. */
export async function serveFamily({ sessionId, composed, backend }: Family, link: LocalLink): Promise<ServingStart> {
  try {
    mkdirSync(composed.workspace, { recursive: true });
    const tapped = tapStores(composed.db);
    const runtime = await start({ agents: [composed.agent], db: tapped.adapter, providers: composed.providers });
    const refresh = refreshOf(backend, composed);
    const sessions = sessionService({ agent: composed.agent, agentName: AGENT_NAME, sessionId, link, store: tapped.store, refresh });

    return { ok: true, serving: { runtime, sessions } };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) };
  }
}
