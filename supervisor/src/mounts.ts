// Contract 03 §7.1. The three directories this process reads, and where they
// are.
//
// sbx mounts a host directory at its own HOST path inside the VM. There is no
// target path to choose, so there is no fixed in-VM path to fall back to:
//
//   host  /srv/agents/state/rework/families/chat/control/
//   VM    /srv/agents/state/rework/families/chat/control/   (the same path)
//
// `managerd` writes the three paths into `supervisor.env` and
// `sbx exec --env-file` delivers them, so the environment is the only source.
// A missing one is `fatal` (§5.7) and never a fallback: `/run/control` cannot
// exist under sbx, so a fallback would fail every turn as `channel_lost` with
// the cause only in a log file.

import { MAX_LOG_BYTES } from "./constants.js";
import type { FatalMessage } from "./protocol.js";

/** Each names one host path, which is also the path inside the VM. */
export const CRED_DIR_ENV = "AGENT_CRED_DIR";
export const FAMILY_CONFIG_DIR_ENV = "AGENT_FAMILY_CONFIG_DIR";
export const CONTROL_DIR_ENV = "AGENT_CONTROL_DIR";

/** The three, in the order `supervisor.env` lists them. */
const REQUIRED = [CRED_DIR_ENV, FAMILY_CONFIG_DIR_ENV, CONTROL_DIR_ENV] as const;

/** Whether the environment named every directory. */
export enum MountState {
  Named = "named",
  Unset = "unset",
}

export interface MountDirs {
  readonly creds: string;
  readonly config: string;
  readonly control: string;
}

export interface MountOutcome {
  readonly state: MountState;
  /** The three paths, or null when one was not named. */
  readonly dirs: MountDirs | null;
  /** Free text for §5.7's `fatal` message. Empty when the state is `Named`. */
  readonly detail: string;
}

export function readMountDirs(env: NodeJS.ProcessEnv): MountOutcome {
  const missing = REQUIRED.filter((name) => (env[name] ?? "").length === 0);

  if (missing.length > 0) {
    return {
      state: MountState.Unset,
      dirs: null,
      detail: `${missing.join(", ")}: no directory named`,
    };
  }

  return {
    state: MountState.Named,
    dirs: {
      creds: env[CRED_DIR_ENV] ?? "",
      config: env[FAMILY_CONFIG_DIR_ENV] ?? "",
      control: env[CONTROL_DIR_ENV] ?? "",
    },
    detail: "",
  };
}

/**
 * §5.7's line for a mount the environment did not name.
 *
 * The section number is in the message on purpose. The operator reads this
 * line in the status document (§5.7 rule 4), not in the code.
 */
export function unsetMountFatal(detail: string): FatalMessage {
  return {
    type: "fatal",
    reason: "mount_dir_unset",
    message: `contract 03 §7.1: ${detail}`.slice(0, MAX_LOG_BYTES),
  };
}
