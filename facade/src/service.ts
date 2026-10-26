// The session methods of one facade, as acp.ts serves them. One service
// serves every link of the facade, so a session's live updates reach each
// peer on it.

import type { Agent } from "@flue/runtime";
import type { ConversationStreamStore } from "@flue/runtime/adapter";

import type { SessionService } from "./acp.js";
import { Live } from "./live.js";
import { Loader } from "./load.js";
import type { SessionIndex } from "./session-index.js";
import { Sessions } from "./sessions.js";
import { Turns } from "./turns.js";

export interface ServiceOptions {
  /** The one agent of the facade, as `start()` registers it. */
  readonly agent: Agent;
  /** The identity under which `start()` registers the agent. */
  readonly agentName: string;
  readonly index: SessionIndex;
  /** The conversation store of the started runtime (`tapStores`). */
  readonly store: () => ConversationStreamStore | null;
  readonly now?: () => Date;
}

export function sessionService(options: ServiceOptions): SessionService {
  const sessions = new Sessions(options.agent, options.index, options.now);
  const live = new Live();
  const turns = new Turns(sessions, live);
  const loader = new Loader(sessions, live, options.store, options.agentName);

  return {
    newSession: async (params, peer) => {
      const made = await sessions.newSession(params);
      if (made.ok) {
        live.attach(made.value.sessionId, peer);
      }

      return made;
    },
    loadSession: (params, peer) => loader.loadSession(params, peer),
    listSessions: (params) => sessions.listSessions(params),
    prompt: (params, peer) => turns.prompt(params, peer),
    cancel: (params) => turns.cancel(params),
  };
}
