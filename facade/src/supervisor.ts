// One supervisor link: the facade's stdio to attendance. It holds the lock,
// the beat deadline and the stop.
//
//   start ──► lock held by a live pid ──► exit 3
//         ├─► control mount unwritable ─► exit 6
//         └─► serve ──► _creche/shutdown ──► stop Flue ──► exit 0
//                   ├─► stdin closed ──────► stop Flue ──► exit 0
//                   └─► no beat in time ───► stop Flue ──► exit 5

import type { Readable } from "node:stream";

import { facadeApp, strictStream, strictWriter } from "./acp.js";
import { SupervisorLock } from "./lock.js";
import { BeatDeadline } from "./liveness.js";
import type { Say, Stdout } from "./stdio.js";

export const EXIT_OK = 0;
export const EXIT_LOCK_HELD = 3;
export const EXIT_DEADLINE = 5;
export const EXIT_CONTROL_MOUNT = 6;

/** The agent runtime that the facade owns. Flue's `start()` gives one. */
export interface Runtime {
  stop(): Promise<void>;
}

export interface SupervisorOptions {
  readonly input: Readable;
  readonly stdout: Stdout;
  readonly say: Say;
  readonly lock: SupervisorLock;
  readonly deadlineMs: number;
  /** Null until the agent module is wired (Phase 2 plan, row 15). */
  readonly runtime: Runtime | null;
}

export function runSupervisor(options: SupervisorOptions): void {
  const { lock, say, stdout } = options;

  const holder = lock.liveHolder();
  if (holder !== null) {
    say(`pid ${holder} holds the lock; a second facade does not start`);
    stdout.exit(EXIT_LOCK_HELD);
    return;
  }

  const taken = lock.take();
  if (!taken.ok) {
    say(`cannot write the lock: ${taken.detail}`);
    stdout.exit(EXIT_CONTROL_MOUNT);
    return;
  }

  let stopping = false;
  const stop = async (reason: string, code: number): Promise<void> => {
    if (stopping) {
      return;
    }

    stopping = true;
    deadline.stop();
    say(reason);
    try {
      await options.runtime?.stop();
    } catch (error) {
      say(`Flue did not stop cleanly: ${error instanceof Error ? error.message : String(error)}`);
    }

    lock.release();
    stdout.exit(code);
  };

  const deadline = new BeatDeadline(options.deadlineMs, () => {
    void stop(`no beat for ${options.deadlineMs} ms; the link is gone`, EXIT_DEADLINE);
  });
  deadline.feed();

  const app = facadeApp({
    onBeat: () => deadline.feed(),
    onShutdown: () => void stop("shutdown", EXIT_OK),
  });
  const connection = app.connect(strictStream(options.input, strictWriter(stdout.writeLine, say)));
  say("serving ACP v1 on stdio");

  void connection.closed.then(() => stop("stdin closed", EXIT_OK));
}
