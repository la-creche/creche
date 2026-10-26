// One supervisor link: the facade's stdio to attendance. It holds the lock,
// the beat deadline and the stop.
//
//   start ──► lock held by a live pid ──► exit 3
//         ├─► control mount unwritable ─► exit 6
//         ├─► Flue does not start ──────► exit 16
//         ├─► relay socket refused ─────► exit 8
//         └─► serve ──► _creche/shutdown ──► stop Flue ──► exit 0
//                   ├─► stdin closed ──────► stop Flue ──► exit 0
//                   └─► no beat in time ───► stop Flue ──► exit 5

import type { Readable } from "node:stream";

import { facadeApp, strictStream, strictWriter } from "./acp.js";
import type { SessionService } from "./acp.js";
import { SupervisorLock } from "./lock.js";
import { BeatDeadline } from "./liveness.js";
import { closeRelay, listenRelay } from "./relay.js";
import type { Say, Stdout } from "./stdio.js";

export const EXIT_OK = 0;
export const EXIT_LOCK_HELD = 3;
export const EXIT_DEADLINE = 5;
export const EXIT_CONTROL_MOUNT = 6;
export const EXIT_RELAY_SOCKET = 8;
export const EXIT_NOT_STARTED = 16;

/** The agent runtime that the facade owns. Flue's `start()` gives one. */
export interface Runtime {
  stop(): Promise<void>;
}

/** What the facade serves once it holds the lock. */
export interface Serving {
  readonly runtime: Runtime | null;
  readonly sessions: SessionService | null;
}

export type ServingStart = { readonly ok: true; readonly serving: Serving } | { readonly ok: false; readonly detail: string };

export interface SupervisorOptions {
  readonly input: Readable;
  readonly stdout: Stdout;
  readonly say: Say;
  readonly lock: SupervisorLock;
  readonly deadlineMs: number;
  /** Starts Flue once the lock is held, never before: one Flue owner per sandbox. */
  readonly serve: () => Promise<ServingStart>;
  /** The relay socket (`relaySocketPath`), or null for no relay. */
  readonly socketPath: string | null;
}

export async function runSupervisor(options: SupervisorOptions): Promise<void> {
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

  const started = await options.serve();
  if (!started.ok) {
    say(`Flue did not start: ${started.detail}`);
    lock.release();
    stdout.exit(EXIT_NOT_STARTED);
    return;
  }

  const { runtime, sessions } = started.serving;
  const relay =
    options.socketPath === null
      ? null
      : await listenRelay(options.socketPath, (hooks) => facadeApp(hooks, sessions), options.deadlineMs, say);
  if (relay !== null && !relay.ok) {
    say(`cannot listen on the relay socket: ${relay.detail}`);
    lock.release();
    stdout.exit(EXIT_RELAY_SOCKET);
    return;
  }

  let stopping = false;
  const stop = async (reason: string, code: number): Promise<void> => {
    if (stopping) {
      return;
    }

    stopping = true;
    deadline.stop();
    if (relay !== null && options.socketPath !== null) {
      closeRelay(relay.server, options.socketPath);
    }

    say(reason);
    try {
      await runtime?.stop();
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

  const app = facadeApp(
    {
      onBeat: () => deadline.feed(),
      onShutdown: () => void stop("shutdown", EXIT_OK),
    },
    sessions,
  );
  const connection = app.connect(strictStream(options.input, strictWriter(stdout.writeLine, say)));
  say("serving ACP v1 on stdio");

  void connection.closed.then(() => stop("stdin closed", EXIT_OK));
}
