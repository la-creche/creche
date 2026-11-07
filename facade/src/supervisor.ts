// One supervisor link: the facade's stdio to attendance. It holds the lock,
// the beat deadline and the stop.
//
//   start ──► lock held by a live pid ──► exit 3
//         ├─► control mount unwritable ─► exit 6
//         ├─► Flue does not start ──────► exit 16
//         ├─► relay socket refused ─────► exit 8
//         └─► serve ──► _creche/shutdown ──► stop Flue ──► exit 0
//                   ├─► stdin closed ──────► stop Flue ──► exit 0
//                   ├─► no beat in time ───► stop Flue ──► exit 5
//                   └─► a stop line on the local link (CH only)
//                                  ──► stop Flue, for at most `stopMs` ──► exit 0

import type { Readable } from "node:stream";

import { facadeApp, strictStream, strictWriter } from "./acp.js";
import type { SessionService } from "./acp.js";
import { SupervisorLock } from "./lock.js";
import { BeatDeadline } from "./liveness.js";
import type { LocalLink } from "./local-link.js";
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
  /**
   * Starts Flue once the lock is held, never before: one Flue owner per
   * sandbox. It gets `link`, the one link that the supervisor opens, so
   * the turns report on that link and no other.
   */
  readonly serve: (link: LocalLink | null) => Promise<ServingStart>;
  /** The relay socket (`relaySocketPath`), or null for no relay. */
  readonly socketPath: string | null;
  /** The local link of guest init under CH (`localLinkOf`), or null on sbx. */
  readonly link: LocalLink | null;
}

/**
 * Flue's stop. With a bound, the stop ends after `boundMs` at the latest,
 * and Flue's own close may still run when the process exits. Flue 2.3.0
 * waits up to 30 s for a running submission, past the 25 s of a stop line
 * (decision CH62 rule 9).
 */
async function stopFlue(runtime: Runtime | null, boundMs: number | null, say: Say): Promise<void> {
  if (runtime === null) {
    return;
  }

  const stopped = runtime.stop().then(() => "stopped" as const);
  if (boundMs === null) {
    await stopped;
    return;
  }

  // A stop that fails after the bound has nobody to tell.
  stopped.catch(() => undefined);
  let timer: NodeJS.Timeout | undefined;
  const late = new Promise<"late">((resolve) => {
    timer = setTimeout(() => resolve("late"), boundMs);
  });
  try {
    if ((await Promise.race([stopped, late])) === "late") {
      say(`Flue did not stop in ${String(boundMs)} ms; the facade exits`);
    }
  } finally {
    clearTimeout(timer);
  }
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

  const started = await options.serve(options.link);
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
  /** `boundMs`: the most time Flue's stop takes, or null for no bound. */
  const stop = async (reason: string, code: number, boundMs: number | null = null): Promise<void> => {
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
      await stopFlue(runtime, boundMs, say);
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
  // The link opens before stdio serves, so each turn has a link to report
  // on. guest init sends the stop line only to an open link, and it moves
  // the facade to `running` at the open (decisions CH32 rule 6, CH66 rule 4).
  const { link } = options;
  link?.open(() => void stop("the stop line of guest init", EXIT_OK, link.stopMs));

  const connection = app.connect(strictStream(options.input, strictWriter(stdout.writeLine, say)));
  say("serving ACP v1 on stdio");

  void connection.closed.then(() => stop("stdin closed", EXIT_OK));
}
