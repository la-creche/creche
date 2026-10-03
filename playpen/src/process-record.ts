// Contract 03 §7.5. One small file per session, in the control mount, naming
// the pi rpc process the playpen holds for it:
//
//   <control mount>/processes/<session id>.json
//   {"session":"tui-01JB…","sandbox":"code-s3","pid":517,
//    "supervisor_pid":412,"started_at":"…","released":false}
//
// It exists for ONE reader: `agent-pi-launch`, which the TUI door starts in
// the same VM through `sbx exec -it`. §6 rule 1 allows one pi process per
// session and never two — two concurrent writers on one session file
// cross-contaminate context, orphan a branch, and both report success with no
// error anywhere. `attendance`'s writer lease (contract 02 §7) is
// the first fence. This file is the second, inside the VM, where the lease
// cannot reach.
//
// It is a LEASE, not a flag, for the same reason `supervisor.lock` is: a
// SIGKILLed playpen writes no release. The reader is inside the same pid
// namespace, so unlike the host it can test the pid directly, and a record
// whose process is gone is simply stale.
//
// It is not a security boundary. The agent process can write the control
// mount, so it can forge this file — and forging it buys an attacker the right
// to break their own session's context. Nothing else reads it.

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { CONTROL_PROCESSES_DIR, SESSION_ID_RE } from "./constants.js";

const RECORD_MODE = 0o644;

export interface ProcessRecord {
  readonly session: string;
  readonly sandbox: string;
  /** The pi process's pid, inside the VM's pid namespace. */
  readonly pid: number | null;
  readonly supervisor_pid: number;
  readonly started_at: string;
  readonly released: boolean;
}

/** A pid that answers signal 0 is alive. Inside one VM this is the direct test. */
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);

    return true;
  } catch {
    return false;
  }
}

function parseRecord(text: string): ProcessRecord | null {
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
  const sandbox = raw["sandbox"];
  const pid = raw["pid"];
  if (typeof session !== "string" || typeof sandbox !== "string") {
    return null;
  }
  if (pid !== null && (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0)) {
    return null;
  }

  const playpenPid = raw["supervisor_pid"];
  const startedAt = raw["started_at"];

  return {
    session,
    sandbox,
    pid: typeof pid === "number" ? pid : null,
    supervisor_pid: typeof playpenPid === "number" ? playpenPid : 0,
    started_at: typeof startedAt === "string" ? startedAt : "",
    released: raw["released"] === true,
  };
}

/** The per-session process records under one control mount. */
export class ProcessRecords {
  public constructor(
    private readonly controlDir: string,
    private readonly sandbox: string,
  ) {}

  /**
   * The path for a session, or null when the id could not be a path segment.
   *
   * `validate.ts` has already applied the same shape on the channel. This
   * repeats it because the value is about to be joined onto a mount path, and
   * a check beside the join is the one a later reader can see.
   */
  public pathFor(session: string): string | null {
    if (!SESSION_ID_RE.test(session)) {
      return null;
    }

    return join(this.controlDir, CONTROL_PROCESSES_DIR, `${session}.json`);
  }

  /** Records a live pi process. Written by temp file and `rename`, per §7.4. */
  public hold(session: string, pid: number | null): void {
    this.write(session, pid, false);
  }

  /**
   * Marks the session free. The record STAYS, rather than being removed,
   * because "released" and "never held" are different facts and the launcher's
   * message to the operator differs between them.
   */
  public release(session: string, pid: number | null): void {
    this.write(session, pid, true);
  }

  /** Drops every record this playpen wrote, as it exits. */
  public clear(): void {
    try {
      rmSync(join(this.controlDir, CONTROL_PROCESSES_DIR), { recursive: true, force: true });
    } catch {
      // The control mount went away with the sandbox. Nothing left to clean.
    }
  }

  /**
   * The live holder of one session, or null when the session is free.
   *
   * Free means any of: no record, an unreadable one, a released one, or one
   * whose pi process is gone. The last is why the pid is in the file: a
   * SIGKILLed playpen leaves a record it never released, and its pi
   * processes can outlive it (contract 03 §11.4 rule 6's orphans).
   */
  public holder(session: string): ProcessRecord | null {
    const path = this.pathFor(session);
    if (path === null) {
      return null;
    }

    let record: ProcessRecord | null;
    try {
      record = parseRecord(readFileSync(path, "utf8"));
    } catch {
      return null;
    }

    if (record === null || record.released || record.pid === null) {
      return null;
    }

    return pidAlive(record.pid) ? record : null;
  }

  private write(session: string, pid: number | null, released: boolean): void {
    const path = this.pathFor(session);
    if (path === null) {
      return;
    }

    const record: ProcessRecord = {
      session,
      sandbox: this.sandbox,
      pid,
      supervisor_pid: process.pid,
      started_at: new Date().toISOString(),
      released,
    };

    try {
      mkdirSync(join(this.controlDir, CONTROL_PROCESSES_DIR), { recursive: true });
      const temp = `${path}.${process.pid}.tmp`;
      writeFileSync(temp, `${JSON.stringify(record)}\n`, { mode: RECORD_MODE });
      renameSync(temp, path);
    } catch {
      // A control mount this process cannot write already ended it: the lock
      // is taken before the first protocol line (§11.1 rule 3).
    }
  }
}
