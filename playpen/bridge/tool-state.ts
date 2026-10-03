// What the bridge knows and the playpen cannot see: whether this pi
// process holds any PEP tools at all.
//
//   <control mount>/sessions/<session id>/tools.json
//   {"session":"owui-3f2a…","tools":7,"rev":"rev-9f21c4","at":"…"}
//
// The bridge's own note about a failed manifest goes to stderr, which the
// playpen forwards as `log` lines (contract 03 §5.5). Stderr is free text
// for a person, and parsing it would make a human-readable line a protocol.
// So the bridge states the one fact in a file instead, beside the turn file
// the playpen already writes for it (§7.4).
//
// It is written when the factory returns, with whatever the start-up fetch
// produced, and again whenever the bridge's poll finds a manifest that really
// MOVED the tool set (`tool-set.ts`). So `tools: 0` means "no tools right
// now", never "no tools ever", and `rev` names the revision the current set
// was built from rather than the newest revision the PEP has.
//
// It is not a security boundary. The agent process can write the control
// mount, so it can forge this file. Forging it buys the right to mislabel the
// process's own turns, which is what `turn-file.ts` already says about the
// file beside it.

import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { TOOL_STATE_FILE, TOOL_STATE_FILE_ENV } from "./constants.js";

/** The record the playpen reads. `rev` is the grants revision, or null. */
export interface ToolState {
  readonly session: string;
  readonly tools: number;
  readonly rev: string | null;
  readonly at: string;
}

/**
 * The path to write, or null when the playpen named none.
 *
 * The playpen names it outright in `AGENT_TOOL_STATE_FILE`. The turn file's
 * directory is the fallback, for a playpen that writes the turn file and
 * not yet this one: both files belong to one session, so the sibling path is
 * the same path the newer playpen would have named.
 */
export function toolStatePath(env: NodeJS.ProcessEnv = process.env): string | null {
  const named = env[TOOL_STATE_FILE_ENV];
  if (named !== undefined && named.length > 0) {
    return named;
  }

  const turnFile = env["AGENT_TURN_FILE"];
  if (turnFile === undefined || turnFile.length === 0) {
    return null;
  }

  return join(dirname(turnFile), TOOL_STATE_FILE);
}

/**
 * States how many tools this process holds. Temp file and `rename`, because
 * the playpen reads it while the bridge writes it.
 *
 * A failure is not fatal and is not retried. The mount may be read-only or
 * gone, and the playpen then says nothing about this turn's tools, which
 * costs a notice and costs the turn itself nothing.
 */
export function writeToolState(
  session: string,
  tools: number,
  rev: string | null,
  env: NodeJS.ProcessEnv = process.env,
): void {
  const path = toolStatePath(env);
  if (path === null) {
    return;
  }

  const state: ToolState = { session, tools, rev, at: new Date().toISOString() };
  try {
    mkdirSync(dirname(path), { recursive: true });
    const temp = `${path}.${process.pid}.tmp`;
    writeFileSync(temp, `${JSON.stringify(state)}\n`, { mode: 0o644 });
    renameSync(temp, path);
  } catch {
    // Nothing to report and nothing to retry. See the doc comment.
  }
}
