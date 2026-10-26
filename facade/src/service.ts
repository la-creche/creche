// The session methods of one facade, as acp.ts serves them.

import type { SessionService } from "./acp.js";
import type { Sessions } from "./sessions.js";
import type { Turns } from "./turns.js";

export function sessionService(sessions: Sessions, turns: Turns): SessionService {
  return {
    newSession: (params) => sessions.newSession(params),
    listSessions: (params) => sessions.listSessions(params),
    prompt: (params, send) => turns.prompt(params, send),
    cancel: (params) => turns.cancel(params),
  };
}
