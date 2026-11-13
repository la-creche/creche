// The session methods of one facade, as acp.ts serves them. One service
// serves every link of the facade, so the session's live updates reach each
// peer on it.

import type { Agent } from "@flue/runtime";
import type { ConversationStreamStore } from "@flue/runtime/adapter";
import type { Wire } from "agent/wire-read";

import type { SessionService } from "./acp.js";
import { Live } from "./live.js";
import { Loader } from "./load.js";
import type { LocalLink } from "./local-link.js";
import { OneSession } from "./sessions.js";
import { Turns } from "./turns.js";
import type { Refresh } from "./turns.js";

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
}

export function sessionService(options: ServiceOptions): SessionService {
  const session = new OneSession(options.agent, options.sessionId, options.now);
  const live = new Live();
  const turns = new Turns(session, live, options.link, options.refresh);
  const loader = new Loader(session, live, options.store, options.agentName);

  return {
    loadSession: (params, peer) => loader.loadSession(params, peer),
    prompt: (params, peer) => turns.prompt(params, peer),
    cancel: (params) => turns.cancel(params),
  };
}
