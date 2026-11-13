// The supervisor of the facade: the local link of guest init alone. No
// lock, no beat deadline and no ACP on stdio (decisions CH5d, CH19, CH80).
//
//   start ──► Flue does not start ──────► exit 16
//         ├─► relay socket refused ─────► exit 8
//         └─► serve ──► a stop line on the local link
//                   │          ──► stop Flue, for at most `stopMs` ──► exit 0
//                   └─► the local link does not open, closes or faults,
//                       outside a stop
//                              ──► stop Flue, for at most `stopMs` ──► exit 18
//
// The exits 16 and 8 come before the facade serves, and guest init starts
// it again after its 1 s restart pause (decisions CH62 rule 8, CH81 rule 8).

import { facadeApp } from "./acp.js";
import type { SessionService } from "./acp.js";
import type { LocalLink } from "./local-link.js";
import { closeRelay, listenRelay } from "./relay.js";
import type { Exit, Say } from "./stdio.js";

export const EXIT_OK = 0;
export const EXIT_RELAY_SOCKET = 8;
export const EXIT_NOT_STARTED = 16;
export const EXIT_LOCAL_LINK = 18;

/** The agent runtime that the facade owns. Flue's `start()` gives one. */
export interface Runtime {
  stop(): Promise<void>;
}

/** What the facade serves. */
export interface Serving {
  readonly runtime: Runtime | null;
  readonly sessions: SessionService;
}

export type ServingStart = { readonly ok: true; readonly serving: Serving } | { readonly ok: false; readonly detail: string };

export interface SupervisorOptions {
  /** The local link of guest init: the one thing that holds the facade (decisions CH5d, CH19, CH67 rule 8). */
  readonly link: LocalLink;
  readonly exit: Exit;
  readonly say: Say;
  /**
   * Starts Flue. It gets `link`, so the turns report on that link and no
   * other.
   */
  readonly serve: (link: LocalLink) => Promise<ServingStart>;
  /** The relay socket, or null for no relay. */
  readonly socketPath: string | null;
}

/**
 * Flue's stop. The stop ends after `boundMs` at the latest, and Flue's own
 * close may still run when the process exits. Flue 2.3.0 waits up to 30 s
 * for a running submission, past the 25 s of a stop line (decision CH62
 * rule 9).
 */
async function stopFlue(runtime: Runtime | null, boundMs: number, say: Say): Promise<void> {
  if (runtime === null) {
    return;
  }

  const stopped = runtime.stop().then(() => "stopped" as const);
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
  const { exit, link, say } = options;

  const started = await options.serve(link);
  if (!started.ok) {
    say(`Flue did not start: ${started.detail}`);
    exit(EXIT_NOT_STARTED);
    return;
  }

  const { runtime, sessions } = started.serving;
  const relay = options.socketPath === null ? null : await listenRelay(options.socketPath, () => facadeApp(sessions), say);
  if (relay !== null && !relay.ok) {
    say(`cannot listen on the relay socket: ${relay.detail}`);
    exit(EXIT_RELAY_SOCKET);
    return;
  }

  let stopping = false;
  const stop = async (reason: string, code: number): Promise<void> => {
    if (stopping) {
      return;
    }

    stopping = true;
    if (relay !== null && options.socketPath !== null) {
      closeRelay(relay.server, options.socketPath);
    }

    say(reason);
    try {
      await stopFlue(runtime, link.stopMs, say);
    } catch (error) {
      say(`Flue did not stop cleanly: ${error instanceof Error ? error.message : String(error)}`);
    }

    exit(code);
  };

  // The link opens once the start pull, the Flue start and the relay
  // listen, so `running` means that the facade serves (decision CH81 rule
  // 6). guest init sends the stop line only to an open link, and it moves
  // the facade to `running` at the open (decisions CH32 rule 6, CH66 rule
  // 4). A lost link outside a stop exits 18, so that guest init starts the
  // facade again (decision CH67 rule 8). Inside a stop, `stop` returns at
  // once and the stop goes on (decision CH66 rule 5). Nothing else ends the
  // process: stdin is /dev/null, and no beat deadline runs (decisions CH5d,
  // CH19).
  link.open({
    onStop: () => void stop("the stop line of guest init", EXIT_OK),
    onLost: (detail) => void stop(`${detail}; the facade exits`, EXIT_LOCAL_LINK),
  });
  say("serving ACP v1 on the relay socket");
}
