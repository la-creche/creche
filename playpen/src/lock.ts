// Contract 03 §11.1 rule 3 and §11.4 rule 4. The lock file in the control
// mount is how a host that lost its channel learns whether the old playpen
// is still alive inside the VM, and how a second playpen refuses to start.
//
// Probe 0a refuted the assumption this exists for (A5): killing the host end
// of the `sbx exec` left the VM running and the playpen alive inside it
// with zero sessions. There is no ingress into a sandbox, so a new channel
// cannot reach that orphan. Two playpens in one VM is the failure this
// file prevents.
//
// The file is a LEASE, not a flag. The host cannot test this pid, which lives
// in the VM's pid namespace, and it cannot read `started_at`, which is written
// by a different clock. So the playpen rewrites the file every
// `lock_beat_s` with a counter one higher, and the host proves the writer is
// gone by reading that counter twice with its own clock (§11.4 rule 4).

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { LOCK_BEAT_MS, LOCK_FILE } from "./constants.js";

const MS_PER_S = 1000;
const LOCK_MODE = 0o644;

export interface LockRecord {
  readonly pid: number;
  readonly sandbox: string;
  readonly started_at: string;
  readonly host_deadline_s: number;
  readonly lock_beat_s: number;
  readonly beat: number;
}

/** Whether the lock was taken. A mount the process cannot write is fatal. */
export enum LockState {
  Taken = "taken",
  MountUnwritable = "mount_unwritable",
}

export interface LockOutcome {
  readonly state: LockState;
  /** Free text for §5.7's `fatal` message. Empty when the lock was taken. */
  readonly detail: string;
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

function parseLock(text: string): LockRecord | null {
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
  const pid = raw["pid"];
  const sandbox = raw["sandbox"];
  const startedAt = raw["started_at"];
  const deadline = raw["host_deadline_s"];
  if (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0) {
    return null;
  }
  if (typeof sandbox !== "string" || typeof startedAt !== "string") {
    return null;
  }
  if (typeof deadline !== "number" || !Number.isInteger(deadline)) {
    return null;
  }

  // A file written by a playpen older than this contract carries no beat.
  // It still names a live pid, which is the only thing THIS side reads.
  const beatS = raw["lock_beat_s"];
  const beat = raw["beat"];

  return {
    pid,
    sandbox,
    started_at: startedAt,
    host_deadline_s: deadline,
    lock_beat_s: typeof beatS === "number" ? beatS : LOCK_BEAT_MS / MS_PER_S,
    beat: typeof beat === "number" && Number.isInteger(beat) ? beat : 0,
  };
}

export class PlaypenLock {
  private readonly path: string;
  private readonly beatMs: number;
  private beat = 0;
  private sandbox = "";
  private deadlineS = 0;
  private startedAt = "";
  private timer: NodeJS.Timeout | null = null;

  public constructor(
    private readonly dir: string,
    beatMs: number = LOCK_BEAT_MS,
  ) {
    this.path = join(dir, LOCK_FILE);
    this.beatMs = beatMs;
  }

  /** The live holder of this lock, or null when the lock is free or stale. */
  public liveHolder(): LockRecord | null {
    let record: LockRecord | null;
    try {
      record = parseLock(readFileSync(this.path, "utf8"));
    } catch {
      return null;
    }

    if (record === null || record.pid === process.pid) {
      return null;
    }

    // A malformed or abandoned lock only shortens the wait; it never grants
    // one (§11.4 rule 4). Inside the VM the pid check is exact, so a dead
    // holder's file is simply stale.
    return pidAlive(record.pid) ? record : null;
  }

  /**
   * Takes the lock and starts the beat.
   *
   * Nothing here throws. An unwritable control mount is a fact the playpen
   * reports on the channel and exits on (§11.1 rule 4, §5.7), never a stack
   * trace on stderr with no protocol line at all.
   *
   * Calling it again re-arms the beat with a new `host_deadline_s`, which is
   * what `hello` does once it carries the host's real number.
   */
  public write(sandbox: string, hostDeadlineS: number): LockOutcome {
    this.sandbox = sandbox;
    this.deadlineS = hostDeadlineS;

    if (this.startedAt === "") {
      this.startedAt = new Date().toISOString();
    }

    const outcome = this.rewrite(this.beat + 1);
    if (outcome.state !== LockState.Taken) {
      return outcome;
    }

    this.startBeat();

    return outcome;
  }

  /** §11.1 rule 3: the playpen removes the lock as it exits. */
  public release(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }

    if (this.beat === 0) {
      return;
    }

    this.beat = 0;
    try {
      rmSync(this.path, { force: true });
    } catch {
      // The control mount went away with the sandbox. Nothing left to clean.
    }
  }

  private startBeat(): void {
    if (this.timer !== null) {
      return;
    }

    // `unref` so the beat alone never holds this process up. stdin is what
    // keeps a serving playpen alive, and the deadline is what ends it.
    this.timer = setInterval(() => this.onBeat(), this.beatMs);
    this.timer.unref();
  }

  /**
   * One beat. A rewrite that fails is not fatal here: the lock was taken, the
   * process is serving, and the host reads a counter that stopped moving as
   * exactly what it is — no playpen writing the file (§11.4 rule 4).
   */
  private onBeat(): void {
    this.rewrite(this.beat + 1);
  }

  /**
   * Replaces the file by temp file and `rename`. Unlink plus recreate is not
   * atomic, and the host reads this file while
   * the playpen writes it.
   */
  private rewrite(beat: number): LockOutcome {
    const record: LockRecord = {
      pid: process.pid,
      sandbox: this.sandbox,
      started_at: this.startedAt,
      host_deadline_s: this.deadlineS,
      lock_beat_s: this.beatMs / MS_PER_S,
      beat,
    };

    const temp = `${this.path}.${process.pid}.tmp`;
    try {
      mkdirSync(this.dir, { recursive: true });
      writeFileSync(temp, `${JSON.stringify(record)}\n`, { mode: LOCK_MODE });
      renameSync(temp, this.path);
    } catch (error) {
      return { state: LockState.MountUnwritable, detail: describe(error) };
    }

    this.beat = beat;

    return { state: LockState.Taken, detail: "" };
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
