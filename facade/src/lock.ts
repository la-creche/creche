// The supervisor lock in the control mount. A port of the playpen's
// `lock.ts` (contract 03 §11.1 rule 3, §11.4 rule 4).
//
// A host that lost its link cannot see into the VM. The lock tells it
// whether the old facade still lives there, and it stops a second facade
// from starting beside the first.
//
// The file is a lease, not a flag. The host cannot test this pid, which
// lives in the VM's pid namespace, and it cannot read `started_at`, which a
// different clock wrote. So the facade rewrites the file every
// `lock_beat_s` with a counter one higher, and the host proves the writer
// is gone when the counter stops for its own `LOCK_STALE_S`.

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { BEAT_DEADLINE_MS, LOCK_BEAT_MS, LOCK_FILE } from "./vectors.js";

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

export type LockOutcome =
  | { readonly ok: true }
  | { readonly ok: false; readonly reason: "mount_unwritable"; readonly detail: string };

/** A pid that answers signal 0 is alive. Inside one VM this test is exact. */
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);

    return true;
  } catch {
    return false;
  }
}

/** The pid of a lock text, or null for a text that names no pid. */
function lockPid(text: string): number | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }

  const pid = (parsed as Record<string, unknown>)["pid"];

  return typeof pid === "number" && Number.isInteger(pid) && pid > 0 ? pid : null;
}

export class SupervisorLock {
  private readonly path: string;
  private readonly startedAt = new Date().toISOString();
  private beat = 0;
  private timer: NodeJS.Timeout | null = null;

  public constructor(
    private readonly dir: string,
    private readonly sandbox: string,
    private readonly beatMs: number = LOCK_BEAT_MS,
  ) {
    this.path = join(dir, LOCK_FILE);
  }

  /** The pid of a live holder that is not this process, or null. */
  public liveHolder(): number | null {
    let pid: number | null;
    try {
      pid = lockPid(readFileSync(this.path, "utf8"));
    } catch {
      return null;
    }

    // A malformed or abandoned lock never stops a start: inside the VM the
    // pid test is exact, so the file of a dead holder is stale.
    return pid !== null && pid !== process.pid && pidAlive(pid) ? pid : null;
  }

  /** Takes the lock and starts the beat. It never throws. */
  public take(): LockOutcome {
    const outcome = this.rewrite();
    if (outcome.ok && this.timer === null) {
      // `unref`: the beat alone never holds the process up.
      this.timer = setInterval(() => this.rewrite(), this.beatMs);
      this.timer.unref();
    }

    return outcome;
  }

  /** §11.1 rule 3: the facade removes the lock as it exits. */
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
      // The control mount went away with the sandbox. Nothing is left.
    }
  }

  /**
   * One beat: a temp file and `rename`, because the host reads the file
   * while the facade writes it. A failed beat is not fatal. The host reads
   * a counter that stopped as what it is: no facade writes the file.
   */
  private rewrite(): LockOutcome {
    const record: LockRecord = {
      pid: process.pid,
      sandbox: this.sandbox,
      started_at: this.startedAt,
      host_deadline_s: BEAT_DEADLINE_MS / MS_PER_S,
      lock_beat_s: this.beatMs / MS_PER_S,
      beat: this.beat + 1,
    };

    const temp = `${this.path}.${process.pid}.tmp`;
    try {
      mkdirSync(this.dir, { recursive: true });
      writeFileSync(temp, `${JSON.stringify(record)}\n`, { mode: LOCK_MODE });
      renameSync(temp, this.path);
    } catch (error) {
      return { ok: false, reason: "mount_unwritable", detail: error instanceof Error ? error.message : String(error) };
    }

    this.beat = record.beat;

    return { ok: true };
  }
}
