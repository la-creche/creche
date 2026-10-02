// Contract 04 §3. Three advisory headers ride every PEP call: the session, the
// turn, and the delegation when the call sits inside a chain.
//
// The turn is the hard one. A held-open pi process serves many turns (contract
// 03 §6 rule 4), so `AGENT_TURN` in its environment names the turn that
// STARTED the process and nothing later. The supervisor therefore rewrites one
// small file per session at each `start_turn` and `prompt`, and names it in
// `AGENT_TURN_FILE`:
//
//   <control mount>/sessions/<session>/turn.json
//   {"session": "owui-3f2a...", "turn": "01JBQ...", "delegation": null}
//
// The file is re-read per call, not cached: a cached turn id would be wrong
// from the second turn onward, and the read costs far less than the round trip
// it rides on.
//
// Nothing here is a security boundary. §3.1 says why: the agent process builds
// these headers, so it can claim any value, and no header changes a decision.

import { readFileSync } from "node:fs";

import { SESSION_ENV, TURN_ENV, TURN_FILE_ENV } from "./constants.js";

export interface TurnContext {
  readonly session: string | null;
  readonly turn: string | null;
  readonly delegation: string | null;
}

/** Contract 02 §2 and the shared identifiers. No shape here admits CR or LF. */
const SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;
const MAX_SESSION_ID_LENGTH = 200;
const MAX_TURN_FILE_BYTES = 4096;

function sessionId(value: unknown): string | null {
  if (typeof value !== "string" || value.length > MAX_SESSION_ID_LENGTH) {
    return null;
  }

  return SESSION_ID_RE.test(value) ? value : null;
}

function ulid(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }

  return ULID_RE.test(value) ? value : null;
}

/** The file, or null when it is absent, oversize, not JSON, or not an object. */
function readFile(path: string): Record<string, unknown> | null {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    return null;
  }

  if (text.length > MAX_TURN_FILE_BYTES) {
    return null;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }

  return parsed as Record<string, unknown>;
}

/**
 * The context for the call being made now.
 *
 * The file wins when it parses. The environment is the fallback, which covers
 * a process opened by `open_session` before any turn exists, and an older
 * supervisor that does not write the file at all. A missing value is omitted
 * from the request rather than guessed.
 */
export function readTurnContext(env: NodeJS.ProcessEnv = process.env): TurnContext {
  const fromEnv: TurnContext = {
    session: sessionId(env[SESSION_ENV]),
    turn: ulid(env[TURN_ENV]),
    delegation: null,
  };

  const path = env[TURN_FILE_ENV];
  if (path === undefined || path.length === 0) {
    return fromEnv;
  }

  const raw = readFile(path);
  if (raw === null) {
    return fromEnv;
  }

  return {
    session: sessionId(raw["session"]) ?? fromEnv.session,
    turn: ulid(raw["turn"]) ?? fromEnv.turn,
    // Contract 04 §7.4: only the PEP mints this id, as a ULID -- the sandbox
    // never mints one. A value that is not that shape is dropped, the same
    // outcome as no chain at all (§3.1 rule 4), never sent as loose text.
    delegation: ulid(raw["delegation"]),
  };
}
