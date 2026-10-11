// Every number and fixed string contract 03 puts on the wire. Nothing here is
// a free parameter: each line names the section that fixes it, so a reader can
// check the code against the contract without reading the code.

import { DEPTH_MAX } from "./strict-json.js";

/** Contract 03 §3. Only `major` has to match; a `minor` difference is legal. */
export const PROTOCOL_VERSION = "1.0";

/** Contract 03 §3, the `playpen` field of `ready`. */
export const PLAYPEN_NAME = "agent-supervisor";

/** Contract 03 §2 rule 5. 1 MiB after UTF-8 encoding, LF not counted. */
export const MAX_LINE_BYTES = 1048576;

/** Contract 03 §8. A wrapped pi event over this is truncated, never dropped. */
export const MAX_EVENT_BYTES = 262144;

/**
 * Not in contract 03: see the CONTRACT-QUESTION in `pi-record.ts`. A pi line
 * nested deeper than this is cut to its scalar fields. A line of the channel
 * is strict JSON, so it nests `DEPTH_MAX` levels at most, and the line
 * object around a pi event is one of those levels. The value is 63.
 */
export const MAX_PI_NESTING = DEPTH_MAX - 1;

/**
 * Contract 03 §5.1. `turn_seq` counts from 1 in each turn. A turn that sent
 * no line fails with this number.
 */
export const FIRST_TURN_SEQ = 1;

/** Contract 03 §8. One `log` message, and one forwarded pi stderr line. */
export const MAX_LOG_BYTES = 4096;

/** Contract 03 §8. One entry's text in a §5.8 answer. */
export const MAX_ENTRY_BYTES = 65536;

/** Contract 03 §8. Entries in one §5.8 answer. More set `truncated: true`. */
export const MAX_ENTRIES_PER_READ = 64;

/** Contract 03 §8. `prompt` text. The host rejects at its door; we re-check. */
export const MAX_PROMPT_BYTES = 262144;

/** Contract 03 §8 and contract 02 §11 rule 6. Folder persona text. */
export const MAX_PERSONA_BYTES = 16384;

/** Contract 03 §9 rule 3. Outbound lines held before we slow a pi process. */
export const MAX_PENDING_LINES = 10000;

/** Resume reading a paused pi process once the queue drains to this. */
export const RESUME_PENDING_LINES = MAX_PENDING_LINES / 2;

/** Contract 03 §9 rule 4. Delta coalescing window. 0 disables coalescing. */
export const DEFAULT_COALESCE_MS = 50;

/** Contract 03 §6 rule 4. Attended default. Thin and autonomous send 0. */
export const DEFAULT_PI_IDLE_TTL_S = 900;

/** Contract 03 §6 rule 8 and contract 01 §3.9. The family's own default. */
export const DEFAULT_MAX_RESIDENT = 12;

/** Contract 01 §3.9. The highest value a family file may ask for. */
export const MAX_RESIDENT_CEILING = 32;

/** Probe 0a, A7. Resident memory of one idle held-open pi process. */
export const PI_PROCESS_RSS_MB = 170;

/** Probe 0a, A7: the playpen is 62 MB. Reserved, with room for the VM. */
export const PLAYPEN_RSS_RESERVE_MB = 512;

/** Contract 03 §11.1 rule 1. Three missed 30 s pings. */
export const DEFAULT_HOST_DEADLINE_S = 90;

/**
 * Contract 03 §11.1 rule 3, `lock_beat_s`. How often the playpen rewrites
 * its lock file with a counter one higher. The host reads that counter with
 * its own clock, because no timestamp may cross the boundary (§11.4 rule 4).
 */
export const LOCK_BEAT_MS = 5000;

/** Contract 03 §4.5 rule 2. Wait for `agent_settled` after `abort`. */
export const DEFAULT_STOP_GRACE_MS = 5000;

/** Contract 03 §4.5 rule 4. After stdin closes, before SIGTERM. */
export const STOP_SIGTERM_DELAY_MS = 2000;

/** Contract 03 §4.5 rule 5. After SIGTERM, before SIGKILL. */
export const STOP_SIGKILL_DELAY_MS = 2000;

/** Contract 03 §12 rule 5. The cross-mount visibility window, plus margin. */
export const CRED_RETRY_MS = 250;

/** Contract 03 §12 rule 5. One poll of the credential file. */
export const CRED_POLL_MS = 5;

/**
 * The site's LAN address, fixed when the image is BUILT (`build.mjs`): a
 * sandbox has no site file, and contract 03 §7 fixes the two URLs below for
 * the life of an image. A build without it fails.
 */
declare const __AGENT_LAN_ADDRESS__: string;

/** Contract 03 §7. Both services bind the LAN address, never loopback. */
export const LITELLM_PORT = 4000;
export const PEP_PORT = 8300;
export const LITELLM_BASE_URL = `http://${__AGENT_LAN_ADDRESS__}:${LITELLM_PORT}`;
export const PEP_URL = `http://${__AGENT_LAN_ADDRESS__}:${PEP_PORT}`;

/**
 * Contract 03 §7: contract 01 §5.4's index root, for the bridge's
 * `index_search`. Fixed like the two URLs above, and for the same reason: it
 * is a HOST path, so it is also its path inside the VM (§7.1). Only a family
 * that mounts an index has the directory, which is the whole test.
 */
export const INDEX_ROOT = "/srv/agents/state/index";

/**
 * Contract 03 §7.1. The files inside the mounts this process reads. The
 * DIRECTORIES are not here: sbx mounts a host directory at its own host path,
 * so there is no fixed path to name. `mounts.ts` reads them from the
 * environment `caregiver`'s `supervisor.env` carries.
 */
export const CRED_FILE = "creds.json";
export const LOCK_FILE = "supervisor.lock";

/** The per-session turn file under the control mount. See `turn-file.ts`. */
export const CONTROL_SESSIONS_DIR = "sessions";
export const TURN_FILE = "turn.json";

/**
 * Beside it, the file the BRIDGE writes and this side reads: how many PEP
 * tools that session's pi process holds. See `tool-state.ts`.
 */
export const TOOL_STATE_FILE = "tools.json";

/**
 * Contract 03 §7.5. The per-session process record, beside the turn files and
 * not inside them: §7.4 rule 3 removes a session's turn directory when its
 * process exits, and the record has to outlive that to say it was released.
 */
export const CONTROL_PROCESSES_DIR = "processes";

/**
 * The read-only path the image installs the PEP bridge at. It sits outside
 * every mount a session can write, which is what `--no-extensions` plus one
 * explicit `--extension` is worth: discovery would otherwise also load
 * `$PI_CODING_AGENT_DIR/extensions/`, and the session store is agent-writable.
 */
export const PEP_BRIDGE_PATH = "/opt/agent-bridge/pep-bridge.js";

/**
 * Contract 03 §7.2. The one parent the `code-sandbox` family mounts, at its
 * host path, because §7.1's identity rule holds for this mount too.
 */
export const CODE_SANDBOX_ROOT = "/srv/agents/work/code-sandbox";
export const CODE_SANDBOX_LINK_PREFIX = "/tmp/code-sandbox-";

/**
 * §7.1 rule 6's seam, for the work root. Nothing on the host sets it.
 *
 * A harness that runs this bundle off the host has no sbx mount, so the
 * contract path is not there and `mkdir` under it fails. The same argument
 * made `SESSIOND_SANDBOX_SESSIONS_MOUNT`, and the same bound applies: an
 * environment variable attaches no mount, so it widens no reach (invariant
 * 12). `attendance` names the matching directory through its own `WORK_ROOT`.
 */
export const CODE_SANDBOX_ROOT_ENV = "AGENT_CODE_SANDBOX_ROOT";

/**
 * Contract 03 §7.1 and §7.6. The sessions mount, for `agent-pi-launch`.
 *
 * The playpen is TOLD its cwd and its session store per turn. A launcher
 * has no channel, so it derives both: the identity rule makes the mount's
 * in-VM path its host path, and `<root>/<family>/<session>/` is contract 02
 * §9's layout. `SESSIOND_SANDBOX_SESSIONS_MOUNT` is §7.1 rule 6's one seam,
 * and it names the same directory for both readers.
 */
export const SESSIONS_ROOT = "/srv/agents/sessions";
export const SESSIONS_MOUNT_ENV = "SESSIOND_SANDBOX_SESSIONS_MOUNT";

/** Contract 02 §9. `PI_CODING_AGENT_DIR` sits under the session directory. */
export const PI_SESSION_DIR = "pi";

/** Contract 02 §2. Shapes every identifier on this channel must match. */
export const SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
export const TURN_ID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;
export const FAMILY_RE = /^[a-z][a-z0-9-]{1,30}$/;

/**
 * CONTRACT-QUESTION: contract 03 §3 gives `<family>-s<N>` and no count of
 * digits for the number. The playpen takes each count. The host takes 1 to
 * 9 digits, so the host sends no id that this pattern refuses. A count
 * changes this one pattern.
 */
export const SANDBOX_ID_RE = /^[a-z][a-z0-9-]{1,30}-s[0-9]+$/;

/** Contract 02 §5.4.1. One inbox file name. */
export const ATTACHMENT_NAME_RE = /^[A-Za-z0-9._-]{1,120}$/;

/** Contract 02 §2. A session id has 1 to 128 characters. */
export const MAX_SESSION_ID_LENGTH = 128;

/** Contract 02 §5.4.1 caps the names, not the count. This bounds the list. */
export const MAX_ATTACHMENTS = 64;

/** The image symlinks pi onto PATH. See `Dockerfile`. */
export const PI_BIN = "pi";

/** pi's name for the one provider a sandbox may reach. See `models-json.ts`. */
export const PI_PROVIDER = "litellm";

/** pi reads this from `PI_CODING_AGENT_DIR` only, and offers no flag to move it. */
export const MODELS_FILE = "models.json";

/**
 * pi's global settings file, read from `PI_CODING_AGENT_DIR` (0.99.1
 * `dist/config.js`, `getAgentDir`). It carries `defaultTools`, the only lever
 * that ENABLES a built-in tool, and no CLI flag replaces it. `pi-settings.ts`
 * rewrites it at every pi start and never reads it back.
 */
export const SETTINGS_FILE = "settings.json";

/**
 * pi 0.99.1's codemode tool, and the built-in extension that registers it
 * (`docs/cli.md`, "Enable codemode"). `--no-extensions` keeps it out unless
 * `--extension` names it.
 */
export const CODEMODE_TOOL = "codemode";
export const CODEMODE_EXTENSION = "builtin:codemode";

/** pi rpc facts, not free parameters (pi 0.99.1 docs/rpc.md). */
export const PI_SETTLED_EVENT = "agent_settled";
/**
 * pi 0.99.1 `docs/rpc-commands.md`: a `prompt` an extension consumed. No run
 * starts, so no `agent_settled` follows it.
 */
export const PI_PROMPT_HANDLED = "handled";
export const PI_RESPONSE_TYPE = "response";
export const PI_MESSAGE_UPDATE = "message_update";
export const PI_MESSAGE_START = "message_start";
export const PI_MESSAGE_END = "message_end";
export const PI_TEXT_DELTA = "text_delta";
export const PI_THINKING_DELTA = "thinking_delta";
