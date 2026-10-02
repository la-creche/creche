// The supervisor's reader for the two things only the bridge knows: whether a
// pi process holds any PEP tools, and which grants revision they came from.
//
//   <control mount>/sessions/<session id>/tools.json
//   {"session":"owui-3f2a…","tools":0,"rev":null,"at":"…"}
//
// The bridge writes it (`bridge/tool-state.ts`), beside the turn file this
// side already writes. A turn that ran on a process holding no tools is a turn
// that could not act, and nobody outside the sandbox could see that before:
// the bridge's own note goes to stderr, and §5.5's `log` lines are free text
// for a person, never a protocol to parse.
//
// `rev` is the second one. The bridge polls the PEP for the life of its
// process (contract 04 §4.2), so a grant the operator adds lands under a running
// session, and `pool.ts` compares this field across turns to say so once.
//
// Everything here crosses a process boundary, so every field is validated for
// shape before use (invariants 12 and 14), and an unreadable file answers
// null rather than a guess.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { CONTROL_SESSIONS_DIR, SESSION_ID_RE, TOOL_STATE_FILE } from "./constants.js";

/** A file larger than this is not the one small record the bridge writes. */
const MAX_STATE_BYTES = 4096;

export interface ToolState {
  readonly session: string;
  readonly tools: number;
  /** The grants revision the tools came from, or null when none arrived. */
  readonly rev: string | null;
}

function parseState(text: string): ToolState | null {
  if (text.length > MAX_STATE_BYTES) {
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

  const raw = parsed as Record<string, unknown>;
  const session = raw["session"];
  const tools = raw["tools"];
  const rev = raw["rev"];
  if (typeof session !== "string" || typeof tools !== "number" || !Number.isInteger(tools)) {
    return null;
  }

  return { session, tools, rev: typeof rev === "string" ? rev : null };
}

/** The per-session tool state files under one control mount. */
export class ToolStateFile {
  public constructor(private readonly controlDir: string) {}

  /** The path for a session, or null when the id could not be a path segment. */
  public pathFor(session: string): string | null {
    if (!SESSION_ID_RE.test(session)) {
      return null;
    }

    return join(this.controlDir, CONTROL_SESSIONS_DIR, session, TOOL_STATE_FILE);
  }

  /**
   * What the bridge last said, or null when it has said nothing.
   *
   * Null is not "no tools". A bridge that never ran, a mount it could not
   * write and an older image all answer null, and the supervisor stays quiet
   * for all three rather than claiming a tool set it cannot see.
   */
  public read(session: string): ToolState | null {
    const path = this.pathFor(session);
    if (path === null) {
      return null;
    }

    try {
      return parseState(readFileSync(path, "utf8"));
    } catch {
      return null;
    }
  }
}
