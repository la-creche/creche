// The runtime that started the facade (decisions CH62 rule 6, CH52 rule 2).
//
//   CRECHE_SESSION_ID ──► absent ──────► sbx: session/new mints each id
//                     ├─► a session id ─► CH: the host's id, for the one session of the VM
//                     └─► other text ───► refused: the facade does not start
//
// The facade reads the variable one time, at process start, as the wire
// type `SessionId` (decision CH25 rule 2).

import { validator } from "agent/wire-read";
import type { Wire } from "agent/wire-read";

import { SESSION_ID_ENV } from "./guest.js";

/** The runtime of the facade. */
export type FacadeMode =
  | { readonly kind: "sbx" }
  | { readonly kind: "ch"; readonly sessionId: Wire<"SessionId"> };

/** The facade under sbx: no session id from the host. */
export const SBX: FacadeMode = { kind: "sbx" };

/** The mode, or why the env refuses one. The detail names the variable, never its value. */
export type ModeRead = { readonly ok: true; readonly mode: FacadeMode } | { readonly ok: false; readonly detail: string };

export function readMode(env: NodeJS.ProcessEnv): ModeRead {
  const text = env[SESSION_ID_ENV];
  if (text === undefined) {
    return { ok: true, mode: SBX };
  }

  if (!validator("SessionId").Check(text)) {
    return { ok: false, detail: `${SESSION_ID_ENV} is not a session id` };
  }

  return { ok: true, mode: { kind: "ch", sessionId: text } };
}
