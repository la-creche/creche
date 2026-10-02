// The one thing a held-open pi process cannot learn from its own environment:
// which turn is running now.
//
// Contract 03 §7 gives the child `AGENT_SESSION` and `AGENT_TURN` at process
// start, and §6 rule 4 then keeps that process for the next turn, and the
// next. From the second turn onward `AGENT_TURN` names a turn that ended. The
// PEP bridge needs the current one for contract 04 §3's `X-Turn-Id`, so the
// supervisor rewrites one small file per session at each `start_turn` and
// `prompt`, and names it to the child in `AGENT_TURN_FILE`:
//
//   <control mount>/sessions/<session id>/turn.json
//   {"session": "owui-3f2a...", "turn": "01JBQ...", "delegation": null,
//    "caller_session": null}
//
// `delegation` is the chain a thin job runs inside, and the bridge sends it as
// contract 04 §3's `X-Delegation-Id`. `caller_session` is who asked. Both are
// per TURN, so both are rewritten on every one: a held-open process that kept
// its first turn's chain would attribute every later call to it.
//
// pi's rpc offers no cleaner path: its `prompt` command carries `message`,
// `images` and `streamingBehavior` and no metadata an extension could read
// (pi 0.99.1 `docs/rpc-commands.md`).
//
// This file is not a security boundary and does not try to be one. The agent
// process runs as root in its own VM and can write anywhere the sandbox
// mounts, so it could rewrite this file. Contract 04 §3.1 already says the
// three headers are advisory and change no decision, which is exactly why
// forging one buys nothing.

import { mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { CONTROL_SESSIONS_DIR, SESSION_ID_RE, TURN_FILE } from "./constants.js";
import type { Delegation } from "./protocol.js";

/** The record the bridge reads. Both chain fields are null outside a chain. */
interface TurnRecord {
  readonly session: string;
  readonly turn: string;
  readonly delegation: string | null;
  readonly caller_session: string | null;
}

export class TurnFile {
  public constructor(private readonly controlDir: string) {}

  /**
   * The path for a session, or null when the id could not be a path segment.
   *
   * `validate.ts` has already applied the same shape. This repeats it because
   * the value is about to be joined onto a mount path, and a check beside the
   * join is the one a later reader can see.
   */
  public pathFor(session: string): string | null {
    if (!SESSION_ID_RE.test(session)) {
      return null;
    }

    return join(this.controlDir, CONTROL_SESSIONS_DIR, session, TURN_FILE);
  }

  /**
   * Writes by temp file and `rename`. The bridge reads this file while the
   * supervisor writes it, and `rename` is the only atomic replacement.
   *
   * A failure is not fatal. The mount may be read-only or gone, and the bridge
   * then falls back to the environment, which costs the audit one current turn
   * id and costs the turn itself nothing.
   */
  public write(session: string, turn: string, delegation?: Delegation): void {
    const path = this.pathFor(session);
    if (path === null) {
      return;
    }

    const record: TurnRecord = {
      session,
      turn,
      delegation: delegation?.id ?? null,
      caller_session: delegation?.caller_session ?? null,
    };
    try {
      mkdirSync(join(this.controlDir, CONTROL_SESSIONS_DIR, session), { recursive: true });
      const temp = `${path}.${process.pid}.tmp`;
      writeFileSync(temp, `${JSON.stringify(record)}\n`, { mode: 0o644 });
      renameSync(temp, path);
    } catch {
      // Nothing to report and nothing to retry. See the doc comment.
    }
  }

  /** Removes a session's directory when its pi process is gone. */
  public forget(session: string): void {
    if (!SESSION_ID_RE.test(session)) {
      return;
    }

    try {
      rmSync(join(this.controlDir, CONTROL_SESSIONS_DIR, session), {
        recursive: true,
        force: true,
      });
    } catch {
      // The control mount went away with the sandbox. Nothing left to clean.
    }
  }
}
