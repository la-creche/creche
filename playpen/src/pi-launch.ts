// Contract 03 §7.6. `agent-pi-launch`: pi's own interactive UI, on a session
// the playpen serves, inside the family's sandbox.
//
//   sbx exec -it <sandbox> -- node /opt/agent-supervisor/agent-pi-launch.js \
//     --sandbox <id> --session <session id> [--cwd <dir>] [--new]
//
// The TUI door opens that exec. There is no ingress into a sandbox
// and no second protocol here: the terminal IS the client, and pi runs in the
// foreground of the exec until the person quits.
//
// This file plans the launch and nothing else. `launch-main.ts` is the
// composition root that reads the real argv and replaces this process with pi.
// Everything a test needs to move is an option there, so nothing below this
// line is ever mocked.
//
// Two promises shape it:
//
//  1. **The same command as the playpen's, minus `--mode rpc`.** One
//     builder answers both (`pi-args.ts`). A terminal whose reach differed
//     from the chat's would be a hole nobody could see from either side.
//  2. **It refuses while a live rpc process holds the session** (§7.5). Two
//     concurrent writers on one session file cross-contaminate context, orphan
//     a branch, and both report success with no error anywhere.
//     `sessiond`'s writer lease (contract 02 §7) is the first fence and this is
//     the second, inside the VM, where the lease cannot reach.

import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";

import {
  MAX_SESSION_ID_LENGTH,
  PEP_BRIDGE_PATH,
  PI_SESSION_DIR,
  SANDBOX_ID_RE,
  SESSION_ID_RE,
  SESSIONS_MOUNT_ENV,
  SESSIONS_ROOT,
} from "./constants.js";
import { CredReader } from "./creds.js";
import { buildTurnEnv } from "./env.js";
import { writeModelsJson } from "./models-json.js";
import { readMountDirs, MountState } from "./mounts.js";
import { bridgeAt, buildPiStart, PiMode } from "./pi-args.js";
import { writePiSettings } from "./pi-settings.js";
import { ProcessRecords } from "./process-record.js";
import { readRuntimeConfig } from "./runtime-config.js";

/** `<family>-s<N>`. The family is what names the sessions mount below it. */
const SANDBOX_FAMILY_RE = /^([a-z][a-z0-9-]{1,30})-s[0-9]+$/;

/** A path this process may be pointed at. It never climbs out of a mount. */
const MAX_PATH_LENGTH = 4096;

/**
 * Whether the session store is expected to exist already.
 *
 * CONTRACT-QUESTION: contract 03 §7.6 lets `--new` make the pi store, and
 * contract 02 §9 makes the session directory `sessiond`'s. `New` makes the
 * pi store and no session record, so `sessiond` still has no row for it.
 */
export enum LaunchSession {
  /** Attach to a session a door already created. */
  Existing = "existing",
  /** `--new`: this terminal is the session's first writer. */
  New = "new",
}

/** How the plan ended. Only `Ready` carries a command worth running. */
export enum LaunchState {
  Ready = "ready",
  /** The argument list named no usable sandbox, session or directory. */
  BadArguments = "bad_arguments",
  /** The environment named none of §7.1's directories. */
  MountUnset = "mount_unset",
  /** §7.5: a live rpc process holds this session. */
  SessionHeld = "session_held",
  /** No session store, and `--new` was not given. */
  NoSuchSession = "no_such_session",
  /** The credential mount answered nothing readable. */
  NoCredentials = "no_credentials",
}

/**
 * One exit code per state, for the shell around the `sbx exec`.
 *
 * The TUI door reads the code, never the sentence: "the session is busy" and
 * "there is no such session" lead an operator to different next steps, and a
 * door that had to parse `detail` would break on the next reworded message.
 *
 * 2 and 7 are `index.ts`'s own codes for the same two faults, so the two
 * programs in this bundle answer a bad argument list and an unnamed mount the
 * same way.
 */
const EXIT_BY_STATE: Readonly<Record<LaunchState, number>> = {
  [LaunchState.Ready]: 0,
  [LaunchState.BadArguments]: 2,
  [LaunchState.MountUnset]: 7,
  [LaunchState.SessionHeld]: 8,
  [LaunchState.NoSuchSession]: 9,
  [LaunchState.NoCredentials]: 10,
};

export function launchExitCode(state: LaunchState): number {
  return EXIT_BY_STATE[state];
}

export interface LaunchPlan {
  readonly state: LaunchState;
  /** One line for the operator. It never carries a credential's value. */
  readonly detail: string;
  readonly args: readonly string[];
  readonly cwd: string;
  readonly env: NodeJS.ProcessEnv;
}

export interface LaunchOptions {
  readonly argv: readonly string[];
  readonly env: NodeJS.ProcessEnv;
  /** The image's bridge bundle. `PEP_BRIDGE_PATH` when absent. */
  readonly bridgePath?: string;
}

interface LaunchArgs {
  readonly sandbox: string;
  readonly family: string;
  readonly session: string;
  readonly cwd: string | null;
  readonly mode: LaunchSession;
}

function refuse(state: LaunchState, detail: string): LaunchPlan {
  return { state, detail, args: [], cwd: "", env: {} };
}

/** One `--flag value` pair, or null. A flag with no value is not a value. */
function flag(argv: readonly string[], name: string): string | null {
  const at = argv.indexOf(name);
  if (at === -1) {
    return null;
  }

  const value = argv[at + 1];

  return value === undefined || value.startsWith("--") ? null : value;
}

/**
 * An absolute path with no parent segment.
 *
 * This is hygiene, not a boundary. The launcher runs as root inside the VM,
 * so a caller able to pass argv is already inside the perimeter (invariant
 * 12). The check keeps a typo from becoming a working directory somewhere
 * surprising.
 */
function isUsablePath(value: string): boolean {
  if (value.length === 0 || value.length > MAX_PATH_LENGTH || !value.startsWith("/")) {
    return false;
  }
  if (value.includes("\0")) {
    return false;
  }

  return !value.split("/").includes("..");
}

function isSessionId(value: string): boolean {
  return value.length > 0 && value.length <= MAX_SESSION_ID_LENGTH && SESSION_ID_RE.test(value);
}

/** Contract 03 §7.6. The argument list, or null when it is not usable. */
function readArgs(argv: readonly string[]): LaunchArgs | null {
  const sandbox = flag(argv, "--sandbox");
  const session = flag(argv, "--session");
  if (sandbox === null || !SANDBOX_ID_RE.test(sandbox)) {
    return null;
  }
  if (session === null || !isSessionId(session)) {
    return null;
  }

  const family = SANDBOX_FAMILY_RE.exec(sandbox)?.[1];
  if (family === undefined) {
    return null;
  }

  const cwd = flag(argv, "--cwd");
  if (cwd !== null && !isUsablePath(cwd)) {
    return null;
  }

  return {
    sandbox,
    family,
    session,
    cwd,
    mode: argv.includes("--new") ? LaunchSession.New : LaunchSession.Existing,
  };
}

/**
 * Where this family's sessions are mounted.
 *
 * A mount's path inside the VM IS its host path (§7.1), so the default needs
 * no rewrite. `SESSIOND_SANDBOX_SESSIONS_MOUNT` is §7.1 rule 6's one seam, and
 * it names this same directory for `sessiond` too, which is what lets a
 * harness run this bundle off the host.
 */
function sessionsMount(env: NodeJS.ProcessEnv, family: string): string {
  const override = (env[SESSIONS_MOUNT_ENV] ?? "").trim();

  return override.length > 0 ? override : join(SESSIONS_ROOT, family);
}

/**
 * The command, the working directory and the environment for one terminal.
 *
 * It reads the same mounts the playpen reads and builds the same command,
 * so the only thing a caller supplies is which session and where to work.
 */
export async function planLaunch(options: LaunchOptions): Promise<LaunchPlan> {
  const args = readArgs(options.argv);
  if (args === null) {
    return refuse(
      LaunchState.BadArguments,
      "--sandbox <family>-s<N> and --session <session id> are required, and --cwd is absolute",
    );
  }

  const mounts = readMountDirs(options.env);
  if (mounts.state !== MountState.Named || mounts.dirs === null) {
    return refuse(LaunchState.MountUnset, `contract 03 §7.1: ${mounts.detail}`);
  }

  // §7.5. The playpen's own record, read from the control mount it shares.
  const holder = new ProcessRecords(mounts.dirs.control, args.sandbox).holder(args.session);
  if (holder !== null) {
    return refuse(
      LaunchState.SessionHeld,
      `the playpen holds pi ${String(holder.pid)} for ${args.session}; close the session first`,
    );
  }

  const home = join(sessionsMount(options.env, args.family), args.session);
  const sessionDir = join(home, PI_SESSION_DIR);
  if (args.mode === LaunchSession.Existing && !existsSync(sessionDir)) {
    return refuse(LaunchState.NoSuchSession, `no session store at ${sessionDir}; pass --new to make one`);
  }

  // A cold read of the credential mount, with no epoch to wait for: nothing
  // here has a turn, so whatever `managerd` wrote last is the current one.
  const creds = await new CredReader(mounts.dirs.creds).read(0);
  if (creds === null) {
    return refuse(LaunchState.NoCredentials, `no readable credential file under ${mounts.dirs.creds}`);
  }

  // The same builder the pool uses, so the only difference is the mode. No
  // `model` is passed: a terminal names no turn, so the family alias leads.
  //
  // CONTRACT-QUESTION: §4.1's `persona` is a field of `start_turn` that
  // `session.ts` fences into that turn's PROMPT text. It is not a start flag,
  // so a launcher outside any turn has none to carry.
  const config = readRuntimeConfig(mounts.dirs.config);
  const start = buildPiStart(
    {
      session: args.session,
      config,
      bridgePath: bridgeAt(options.bridgePath ?? PEP_BRIDGE_PATH),
    },
    PiMode.Interactive,
  );

  // The same two documents the pool writes, in the same directory, from the
  // same family config: a terminal whose `defaultTools` differed would have a
  // different set of built-ins from the chat, which is the hole `pi-args.ts`
  // exists to close. Neither file has a flag pointing elsewhere, so both have
  // to be there before pi starts.
  mkdirSync(sessionDir, { recursive: true });
  writeModelsJson(sessionDir, start.models);
  writePiSettings(sessionDir, config);

  return {
    state: LaunchState.Ready,
    detail: `pi on ${args.session} in ${args.sandbox}`,
    args: start.args,
    cwd: args.cwd ?? home,
    // No turn exists, so `AGENT_TURN` and `AGENT_TURN_FILE` are both absent.
    // The bridge reads an absent value as unknown and sends no header, which
    // is honest: a stale turn id in the audit is worse than none.
    env: buildTurnEnv({
      sessionDir,
      family: args.family,
      sandbox: args.sandbox,
      session: args.session,
      creds,
    }),
  };
}
