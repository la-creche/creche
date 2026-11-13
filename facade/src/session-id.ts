// The session id of the facade (decisions CH62 rule 6, CH52 rule 2).
//
//   CRECHE_SESSION_ID ──► a session id ─► the host's id, for the one session of the VM
//                     ├─► absent ──────► refused: the facade does not start
//                     └─► other text ──► refused: the facade does not start
//
// The facade reads the variable one time, at process start, as the wire
// type `SessionId` (decision CH25 rule 2). A refusal exits 17, the bad-id
// exit of decision CH81 rule 8.

import { validator } from "agent/wire-read";
import type { Wire } from "agent/wire-read";

import { SESSION_ID_ENV } from "./guest.js";

/** The session id, or why the env gives none. The detail names the variable, never its value. */
export type SessionIdRead =
  | { readonly ok: true; readonly sessionId: Wire<"SessionId"> }
  | { readonly ok: false; readonly detail: string };

export function readSessionId(env: NodeJS.ProcessEnv): SessionIdRead {
  const text = env[SESSION_ID_ENV];
  if (text === undefined) {
    return { ok: false, detail: `${SESSION_ID_ENV} is not set` };
  }

  if (!validator("SessionId").Check(text)) {
    return { ok: false, detail: `${SESSION_ID_ENV} is not a session id` };
  }

  return { ok: true, sessionId: text };
}
