// Contract 03 §4 and §5: the wire vocabulary of the one channel, as types.
// Field names here ARE the wire, so nothing in this file may be renamed for
// taste. The host is `attendance`; the playpen is this process.

/** Contract 03 §4. Every message the host may send. */
export type HostMessageType =
  | "hello"
  | "open_session"
  | "start_turn"
  | "prompt"
  | "steer"
  | "abort"
  | "stop_process"
  | "get_entries"
  | "ping"
  | "shutdown";

/** Contract 03 §5.3. Why a turn ended without settling. */
export type TurnFailReason =
  | "no_resident_process"
  | "fork_refused"
  | "stale_credentials"
  | "pi_start_failed"
  | "pi_rejected_prompt"
  | "process_died"
  | "deadline_exceeded"
  | "session_busy_in_sandbox"
  | "line_too_large"
  | "internal";

/** Contract 03 §5.4. Why a pi process ended. */
export type ProcessExitReason = "reaped" | "stopped" | "crashed" | "shutdown";

/** Contract 03 §5.5. */
export type LogLevel = "debug" | "info" | "warn" | "error";

/** Contract 03 §7.2. The only workspace kind. */
export const CODE_SANDBOX_KIND = "code-sandbox";

export interface Workspace {
  readonly kind: string;
  readonly owner_session: string;
}

export interface Branch {
  readonly fork_from: string;
}

/**
 * Contract 03 §7.4 and contract 04 §3. The chain one turn belongs to.
 *
 * Only the PEP mints `id`, one per delegate call, as an upper-case ULID
 * (contract 04 §7.4). `caller_session` is the session that asked, which the
 * PEP passed to the delegate door as `claimed_session_id` and the door passed
 * on. Both are advisory at the PEP and neither grants anything.
 *
 * `caller_session` may be null: `claimed_session_id` is itself advisory, so
 * the PEP may send none (contract 04 §7.3). That is a delegation with no
 * caller, not a malformed one (§7.4 rule 4 item 5) -- `id` still reaches the
 * bridge, and the turn still runs.
 */
export interface Delegation {
  readonly id: string;
  readonly caller_session: string | null;
}

/** Contract 03 §3. The host's half of the handshake. */
export interface HelloMessage {
  readonly type: "hello";
  readonly protocol: string;
  readonly host?: string;
  readonly family: string;
  readonly sandbox: string;
  readonly max_line_bytes?: number;
  readonly coalesce_ms?: number;
  readonly pi_idle_ttl_s?: number;
  readonly max_resident_processes?: number;
  readonly host_deadline_s?: number;
  readonly channel_idle_ttl_s?: number;
  readonly env_epoch: number;
}

/**
 * Contract 03 §4.7. `start_turn` without the prompt: it opens the session's pi
 * process so the first turn does not pay pi's cold start (§6 rule 9).
 *
 * Contract 03 §4.7 rule 6: `model` and `workspace` are optional and accepted,
 * because a process opened without them is replaced by the first
 * `start_turn` that carries one, which is worse than not pre-starting.
 */
export interface OpenSessionMessage {
  readonly type: "open_session";
  readonly session: string;
  readonly cwd: string;
  readonly session_dir: string;
  readonly config_rev: string;
  readonly env_epoch: number;
  readonly model?: string;
  readonly workspace?: Workspace;
}

/** Contract 03 §4.1. */
export interface StartTurnMessage {
  readonly type: "start_turn";
  readonly turn: string;
  readonly session: string;
  readonly cwd: string;
  readonly session_dir: string;
  readonly prompt: string;
  readonly deadline_s: number;
  readonly env_epoch: number;
  readonly config_rev: string;
  readonly persona?: string;
  readonly model?: string;
  readonly attachments?: readonly string[];
  readonly workspace?: Workspace;
  readonly branch?: Branch;
  readonly delegation?: Delegation;
}

/** Contract 03 §4.2. `start_turn` minus the fields a resident process fixed. */
export interface PromptMessage {
  readonly type: "prompt";
  readonly turn: string;
  readonly session: string;
  readonly prompt: string;
  readonly deadline_s: number;
  readonly env_epoch: number;
  readonly persona?: string;
  readonly attachments?: readonly string[];
  readonly branch?: Branch;
  readonly delegation?: Delegation;
}

/** Contract 03 §4.3. */
export interface SteerMessage {
  readonly type: "steer";
  readonly session: string;
  readonly turn: string;
  readonly message: string;
}

/** Contract 03 §4.4. */
export interface AbortMessage {
  readonly type: "abort";
  readonly session: string;
  readonly turn: string;
}

/** Contract 03 §4.5. */
export interface StopProcessMessage {
  readonly type: "stop_process";
  readonly session: string;
  readonly grace_ms?: number;
}

/**
 * Contract 03 §4.8. Read a session's pi entries back, with their text.
 *
 * It carries §4.7's binding fields for §4.7's reason: the process may not be
 * resident, and a terminal's exchanges are exactly the case where it is not.
 */
export interface GetEntriesMessage {
  readonly type: "get_entries";
  readonly request: string;
  readonly session: string;
  readonly cwd: string;
  readonly session_dir: string;
  readonly config_rev: string;
  readonly env_epoch: number;
  readonly since?: string;
  readonly model?: string;
  readonly workspace?: Workspace;
}

/** Contract 03 §4.6. */
export interface PingMessage {
  readonly type: "ping";
  readonly nonce?: string;
}

export interface ShutdownMessage {
  readonly type: "shutdown";
  readonly grace_ms?: number;
}

export type HostMessage =
  | HelloMessage
  | OpenSessionMessage
  | StartTurnMessage
  | PromptMessage
  | SteerMessage
  | AbortMessage
  | StopProcessMessage
  | GetEntriesMessage
  | PingMessage
  | ShutdownMessage;

/** A turn request, whether it arrived as `start_turn` or as `prompt`. */
export type TurnRequest = StartTurnMessage | PromptMessage;

/**
 * What starting one pi process needs. `open_session` and `start_turn` are both
 * assignable to it, and `turn` is absent for the first: it runs before any
 * turn exists.
 */
export interface ProcessSpec {
  readonly session: string;
  readonly cwd: string;
  readonly session_dir: string;
  readonly config_rev: string;
  readonly env_epoch: number;
  readonly model?: string;
  readonly workspace?: Workspace;
  readonly turn?: string;
}

/** Contract 03 §5.2. Summed usage of one turn, in the host's field names. */
export interface TurnUsage {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  cost_usd: number;
}

/** Contract 03 §5. Every message the playpen may send. */
export interface ReadyMessage {
  readonly type: "ready";
  readonly protocol: string;
  readonly supervisor: string;
  readonly pi: string;
  readonly node: string;
  readonly sandbox: string;
  readonly max_resident_processes: number;
  readonly foreign_pi_processes: number;
  readonly caps: readonly string[];
}

export interface EventMessage {
  readonly type: "event";
  readonly session: string;
  readonly turn: string;
  readonly turn_seq: number;
  readonly event: Record<string, unknown>;
}

export interface TurnSettledMessage {
  readonly type: "turn_settled";
  readonly session: string;
  readonly turn: string;
  readonly turn_seq: number;
  readonly resident: boolean;
  readonly user_entry_id: string | null;
  readonly leaf_id: string | null;
  readonly entry_count: number | null;
  readonly usage: TurnUsage;
  readonly settled_ms: number;
}

/**
 * Contract 03 §4.7. Why no process is resident. It is §5.3's set plus one:
 * `not_held` is not a failure, it is a family whose `pi_idle_ttl_s` is 0 and
 * which therefore holds nothing between turns (§6 rule 4).
 */
export type SessionOpenReason = TurnFailReason | "not_held";

/** Contract 03 §4.7. The answer to `open_session`. It names no turn. */
export interface SessionOpenedMessage {
  readonly type: "session_opened";
  readonly session: string;
  readonly resident: boolean;
  /** Null when `resident` is true. */
  readonly reason: SessionOpenReason | null;
  readonly message: string;
}

export interface TurnFailedMessage {
  readonly type: "turn_failed";
  readonly session: string | null;
  readonly turn: string | null;
  readonly turn_seq: number;
  readonly reason: TurnFailReason;
  readonly message: string;
}

export interface ProcessExitMessage {
  readonly type: "process_exit";
  readonly session: string;
  readonly pid: number | null;
  readonly code: number | null;
  readonly signal: string | null;
  readonly turn: string | null;
  readonly reason: ProcessExitReason;
  readonly rss_peak_mb: number | null;
}

export interface PongMessage {
  readonly type: "pong";
  readonly nonce: string | null;
  readonly ts_ms: number;
  readonly resident: number;
  readonly rss_mb: number | null;
}

export interface LogMessage {
  readonly type: "log";
  readonly level: LogLevel;
  readonly session: string | null;
  readonly message: string;
}

/** Contract 03 §5.8. One pi entry, as the host reads it. */
export interface SessionEntry {
  readonly id: string;
  readonly role: string;
  readonly text: string;
}

/**
 * Contract 03 §5.8. The answer to §4.8, and the only message that carries
 * conversation text back to the host.
 */
export interface EntriesMessage {
  readonly type: "entries";
  readonly request: string;
  readonly session: string;
  readonly ok: boolean;
  readonly reason: TurnFailReason | null;
  readonly since_matched: boolean;
  readonly truncated: boolean;
  readonly leaf_id: string | null;
  readonly entries: readonly SessionEntry[];
}

/**
 * Contract 03 §5.7. Why the playpen cannot serve at all. It belongs to the
 * process, not to a turn, so it names no session and no `turn_seq`.
 */
export type FatalReason = "control_mount_unwritable" | "mount_dir_unset";

/** Contract 03 §5.7. The last line a playpen sends before it exits. */
export interface FatalMessage {
  readonly type: "fatal";
  readonly reason: FatalReason;
  readonly message: string;
}

export type PlaypenMessage =
  | ReadyMessage
  | SessionOpenedMessage
  | EventMessage
  | TurnSettledMessage
  | TurnFailedMessage
  | ProcessExitMessage
  | PongMessage
  | LogMessage
  | EntriesMessage
  | FatalMessage;

/** pi rpc, 0.99.1 docs/rpc.md. One command written to a pi process's stdin. */
export interface PiCommand {
  readonly type: string;
  readonly id?: string;
  readonly message?: string;
  readonly entryId?: string;
  readonly since?: string;
}

/** pi rpc. A `response` line answers one command; every other line is an event. */
export interface PiResponse {
  readonly type: "response";
  readonly id?: string;
  readonly command?: string;
  readonly success?: boolean;
  readonly error?: string;
  readonly data?: Record<string, unknown>;
}

/**
 * pi rpc `get_entries`. An entry id is a durable cursor into the session.
 *
 * CONTRACT-QUESTION: pi 0.99.1's rpc doc fixes `id` and `message.role`
 * and names no shape for the text. `entry-text.ts` therefore reads three
 * shapes and answers "" for anything else, which loses text and never
 * invents any. Verifying it costs one `get_entries` against a real session.
 */
export interface PiEntry {
  readonly id?: string;
  readonly message?: {
    readonly role?: string;
    readonly content?: unknown;
    readonly text?: unknown;
  };
}
