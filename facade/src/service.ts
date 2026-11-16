// The session methods of one facade, as acp.ts serves them. One service
// serves every link of the facade, so the session's live updates reach each
// peer on it.

import type { Agent } from "@flue/runtime";
import type { ConversationStreamStore } from "@flue/runtime/adapter";
import { ThinkingLevels } from "agent/agent";
import type { Wire } from "agent/wire-read";

import type { Outcome, SessionService, SetSessionConfigOptionRequest, SetSessionConfigOptionResponse } from "./acp.js";
import { readHistory } from "./flue-history.js";
import { Live } from "./live.js";
import { Loader } from "./load.js";
import type { LocalLink } from "./local-link.js";
import { OneSession } from "./sessions.js";
import { chosenLevel, configOptionsOf } from "./thinking.js";
import { Turns } from "./turns.js";
import type { Refresh } from "./turns.js";
import { SessionMeter } from "./usage.js";
import type { HistoryChunks, MeterSource } from "./usage.js";

export interface ServiceOptions {
  /** The one agent of the facade, as `start()` registers it. */
  readonly agent: Agent;
  /** The identity under which `start()` registers the agent. */
  readonly agentName: string;
  /** The host's id of the one session, from `CRECHE_SESSION_ID` (`src/session-id.ts`). */
  readonly sessionId: Wire<"SessionId">;
  /** The local link of guest init, as the supervisor gives it (`src/local-link.ts`). */
  readonly link: LocalLink | null;
  /** The conversation store of the started runtime (`tapStores`). */
  readonly store: () => ConversationStreamStore | null;
  readonly now?: () => Date;
  /** The pull of the family state before each prompt. None: the state of the start stays. */
  readonly refresh?: Refresh;
  /** The thinking levels that the agent's render reads. None: levels that no render reads. */
  readonly thinking?: ThinkingLevels;
  /** The window and the spend of the meter. None: the facade sends no `usage_update`. */
  readonly meter?: MeterSource;
}

/** The chunks of the session's history, or null when Flue has not started or the read fails. */
function historyOf({ store, agentName, sessionId }: ServiceOptions): HistoryChunks {
  return async () => {
    const started = store();
    if (started === null) {
      return null;
    }

    const read = await readHistory(started, agentName, sessionId);
    return read.ok ? read.chunks : null;
  };
}

/** `session/set_config_option`: the level of the next prompt, and the options that show it. */
function setLevel(
  session: OneSession,
  meter: SessionMeter,
  params: SetSessionConfigOptionRequest,
): Outcome<SetSessionConfigOptionResponse> {
  if (!session.known(params.sessionId)) {
    return { ok: false, refusal: { kind: "unknown_session", sessionId: params.sessionId } };
  }

  const level = chosenLevel(params);
  if (!level.ok) {
    return level;
  }

  meter.choose(level.value);
  return { ok: true, value: { configOptions: configOptionsOf(level.value) } };
}

export function sessionService(options: ServiceOptions): SessionService {
  const session = new OneSession(options.agent, options.sessionId, options.now);
  const live = new Live();
  const meter = new SessionMeter(options.sessionId, options.thinking ?? new ThinkingLevels(), options.meter ?? null, historyOf(options));
  const turns = new Turns(session, live, options.link, meter, options.refresh);
  const loader = new Loader(session, live, options.store, options.agentName, meter);

  return {
    loadSession: (params, peer) => loader.loadSession(params, peer),
    prompt: (params, peer) => turns.prompt(params, peer),
    setConfigOption: async (params) => setLevel(session, meter, params),
    cancel: (params) => turns.cancel(params),
  };
}
