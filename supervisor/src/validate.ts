// Contract 03 §13 read in the other direction. The host treats every byte the
// supervisor sends as untrusted; the supervisor is inside the untrusted
// sandbox, so it returns the favour. A malformed command must never crash it
// (invariants 12 and 14).
//
// Everything here is pure: a line in, either a typed message or a refusal.
// Nothing is acted on before it has passed through this file.

import {
  ATTACHMENT_NAME_RE,
  FAMILY_RE,
  MAX_ATTACHMENTS,
  MAX_PERSONA_BYTES,
  MAX_PROMPT_BYTES,
  MAX_SESSION_ID_LENGTH,
  SANDBOX_ID_RE,
  SESSION_ID_RE,
  TURN_ID_RE,
} from "./constants.js";
import { byteLength } from "./framing.js";
import type {
  AbortMessage,
  Branch,
  Delegation,
  GetEntriesMessage,
  HelloMessage,
  HostMessage,
  OpenSessionMessage,
  PingMessage,
  PromptMessage,
  ShutdownMessage,
  StartTurnMessage,
  SteerMessage,
  StopProcessMessage,
  Workspace,
} from "./protocol.js";

/** A refusal names the field, so the `log` line the host reads is actionable. */
export interface Refusal {
  readonly ok: false;
  readonly detail: string;
  /** Present only when the bad message still addressed a turn we can fail. */
  readonly session?: string;
  readonly turn?: string;
}

export interface Accepted {
  readonly ok: true;
  readonly message: HostMessage;
}

export type ParseResult = Accepted | Refusal;

const HOST_TYPES = new Set([
  "hello",
  "open_session",
  "start_turn",
  "prompt",
  "steer",
  "abort",
  "stop_process",
  "get_entries",
  "ping",
  "shutdown",
]);

/** A steering message is prompt-shaped: the same cap keeps the line bounded. */
const MAX_STEER_BYTES = MAX_PROMPT_BYTES;

/** `deadline_s` and `grace_ms` are host-set. Bound them anyway. */
const MAX_DEADLINE_S = 86400;
const MAX_GRACE_MS = 600000;

/** Contract 03 §7.1. A mount path is absolute and holds no parent segment. */
const MAX_PATH_LENGTH = 4096;

function refuse(detail: string, at?: { session?: string; turn?: string }): Refusal {
  if (!at) {
    return { ok: false, detail };
  }

  const session = at.session;
  const turn = at.turn;
  if (session === undefined || turn === undefined) {
    return { ok: false, detail };
  }

  return { ok: false, detail, session, turn };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(raw: Record<string, unknown>, key: string): string | null {
  const value = raw[key];

  return typeof value === "string" ? value : null;
}

function int(raw: Record<string, unknown>, key: string): number | null {
  const value = raw[key];
  if (typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) {
    return null;
  }

  return value;
}

function isSessionId(value: string): boolean {
  return value.length <= MAX_SESSION_ID_LENGTH && SESSION_ID_RE.test(value);
}

/** A path arriving from the host may never climb out of its mount. */
function isMountPath(value: string): boolean {
  if (value.length === 0 || value.length > MAX_PATH_LENGTH || !value.startsWith("/")) {
    return false;
  }
  if (value.includes("\0")) {
    return false;
  }

  return !value.split("/").includes("..");
}

/** Contract 03 §7.2. The link target is derived here, never sent by the host. */
function readWorkspace(raw: Record<string, unknown>): Workspace | null | Refusal {
  const value = raw["workspace"];
  if (value === undefined || value === null) {
    return null;
  }
  if (!isRecord(value)) {
    return refuse("workspace is not an object");
  }

  const kind = str(value, "kind");
  const owner = str(value, "owner_session");
  if (kind === null || owner === null) {
    return refuse("workspace needs kind and owner_session");
  }
  // §7.2 rule 1 names `/`, `.` and `..` explicitly. The session-id form
  // already excludes `/`, and `.`/`..` fail the two checks after it.
  if (!isSessionId(owner) || owner === "." || owner === "..") {
    return refuse("workspace.owner_session is not a session id");
  }

  return { kind, owner_session: owner };
}

/**
 * Contract 03 §7.4 and contract 04 §7.4. The chain a thin job runs inside.
 *
 * A malformed value fails the turn rather than being dropped. The PEP is the
 * only minter, so a value of another shape means a broken delegate door, and
 * the audit record is the one thing a thin job leaves behind (invariant 15).
 * The PEP still drops a bad header rather than denying the call: that rule is
 * about a DECISION, and this one is about a turn nobody could account for.
 *
 * `caller_session` alone is optional (§7.4 rule 4 item 5): the PEP's own
 * `claimed_session_id` is advisory and may be absent (contract 04 §7.3), so
 * an ABSENT `caller_session` is a delegation with no caller, not a malformed
 * one. A PRESENT `caller_session` of the wrong shape still fails the turn --
 * that is still a broken delegate door, the same as a bad `id`.
 */
function readDelegation(raw: Record<string, unknown>): Delegation | null | Refusal {
  const value = raw["delegation"];
  if (value === undefined || value === null) {
    return null;
  }
  if (!isRecord(value)) {
    return refuse("delegation is not an object");
  }

  const id = str(value, "id");
  if (id === null || !TURN_ID_RE.test(id)) {
    return refuse("delegation.id is not an upper-case ULID");
  }

  const rawCaller = value["caller_session"];
  if (rawCaller === undefined || rawCaller === null) {
    return { id, caller_session: null };
  }

  const caller = str(value, "caller_session");
  if (caller === null || !isSessionId(caller)) {
    return refuse("delegation.caller_session is not a session id");
  }

  return { id, caller_session: caller };
}

function readBranch(raw: Record<string, unknown>): Branch | null | Refusal {
  const value = raw["branch"];
  if (value === undefined || value === null) {
    return null;
  }
  if (!isRecord(value)) {
    return refuse("branch is not an object");
  }

  const from = str(value, "fork_from");
  if (from === null || from.length === 0 || from.length > MAX_SESSION_ID_LENGTH) {
    return refuse("branch.fork_from is not an entry id");
  }

  return { fork_from: from };
}

/** Contract 02 §5.4.1. Names only. The bytes are already in the inbox mount. */
function readAttachments(raw: Record<string, unknown>): readonly string[] | null | Refusal {
  const value = raw["attachments"];
  if (value === undefined || value === null) {
    return null;
  }
  if (!Array.isArray(value) || value.length > MAX_ATTACHMENTS) {
    return refuse("attachments is not a bounded list");
  }

  for (const name of value) {
    if (typeof name !== "string" || !ATTACHMENT_NAME_RE.test(name) || name === "." || name === "..") {
      return refuse("attachments holds a name that is not an inbox file name");
    }
  }

  return value as readonly string[];
}

function readPersona(raw: Record<string, unknown>): string | null | Refusal {
  const value = raw["persona"];
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "string" || byteLength(value) > MAX_PERSONA_BYTES) {
    return refuse("persona is missing or over 16 KiB");
  }

  return value;
}

/** The fields `start_turn` and `prompt` share, validated once. */
interface TurnCore {
  readonly turn: string;
  readonly session: string;
  readonly prompt: string;
  readonly deadline_s: number;
  readonly env_epoch: number;
}

function readTurnCore(raw: Record<string, unknown>): TurnCore | Refusal {
  const turn = str(raw, "turn");
  const session = str(raw, "session");
  if (turn === null || !TURN_ID_RE.test(turn)) {
    return refuse("turn is not a ULID");
  }
  if (session === null || !isSessionId(session)) {
    return refuse("session is not a session id");
  }

  const prompt = str(raw, "prompt");
  if (prompt === null || byteLength(prompt) > MAX_PROMPT_BYTES) {
    return refuse("prompt is missing or over 256 KiB", { session, turn });
  }

  const deadline = int(raw, "deadline_s");
  if (deadline === null || deadline <= 0 || deadline > MAX_DEADLINE_S) {
    return refuse("deadline_s is missing or out of range", { session, turn });
  }

  const epoch = int(raw, "env_epoch");
  if (epoch === null || epoch < 0) {
    return refuse("env_epoch is missing or negative", { session, turn });
  }

  return { turn, session, prompt, deadline_s: deadline, env_epoch: epoch };
}

function isRefusal(value: unknown): value is Refusal {
  return isRecord(value) && value["ok"] === false;
}

function readHello(raw: Record<string, unknown>): HelloMessage | Refusal {
  const protocol = str(raw, "protocol");
  const family = str(raw, "family");
  const sandbox = str(raw, "sandbox");
  if (protocol === null || !protocol.includes(".")) {
    return refuse("hello.protocol is missing or malformed");
  }
  if (family === null || !FAMILY_RE.test(family)) {
    return refuse("hello.family is not a family name");
  }
  if (sandbox === null || !SANDBOX_ID_RE.test(sandbox)) {
    return refuse("hello.sandbox is not a sandbox id");
  }

  const epoch = int(raw, "env_epoch");
  if (epoch === null || epoch < 0) {
    return refuse("hello.env_epoch is missing or negative");
  }

  // Every remaining field is optional and each has a default, so an unknown
  // or absent value must never be fatal (§3 version rule 2).
  return {
    type: "hello",
    protocol,
    family,
    sandbox,
    env_epoch: epoch,
    ...optionalInt(raw, "max_line_bytes"),
    ...optionalInt(raw, "coalesce_ms"),
    ...optionalInt(raw, "pi_idle_ttl_s"),
    ...optionalInt(raw, "max_resident_processes"),
    ...optionalInt(raw, "host_deadline_s"),
    ...optionalInt(raw, "channel_idle_ttl_s"),
    ...optionalStr(raw, "host"),
  };
}

function optionalInt(raw: Record<string, unknown>, key: string): Record<string, number> {
  const value = int(raw, key);

  return value === null || value < 0 ? {} : { [key]: value };
}

function optionalStr(raw: Record<string, unknown>, key: string): Record<string, string> {
  const value = str(raw, key);

  return value === null ? {} : { [key]: value };
}

/** The fields a pi process start needs, shared by `open_session` and `start_turn`. */
interface ProcessCore {
  readonly cwd: string;
  readonly session_dir: string;
  readonly config_rev: string;
  readonly model?: string;
  readonly workspace?: Workspace;
}

function readProcessCore(
  raw: Record<string, unknown>,
  at?: { session: string; turn: string },
): ProcessCore | Refusal {
  const cwd = str(raw, "cwd");
  const sessionDir = str(raw, "session_dir");
  if (cwd === null || !isMountPath(cwd)) {
    return refuse("cwd is not an absolute sandbox path", at);
  }
  if (sessionDir === null || !isMountPath(sessionDir)) {
    return refuse("session_dir is not an absolute sandbox path", at);
  }

  const configRev = str(raw, "config_rev");
  if (configRev === null || configRev.length === 0 || configRev.length > MAX_SESSION_ID_LENGTH) {
    return refuse("config_rev is missing or malformed", at);
  }

  const workspace = readWorkspace(raw);
  if (isRefusal(workspace)) {
    return refuse(workspace.detail, at);
  }

  const model = str(raw, "model");

  return {
    cwd,
    session_dir: sessionDir,
    config_rev: configRev,
    ...(model === null ? {} : { model }),
    ...(workspace === null ? {} : { workspace }),
  };
}

/**
 * Contract 03 §4.7. `open_session` is an optimisation, exactly as §4.2's
 * `prompt` is: a host that hears nothing back still sends `start_turn`, which
 * is always correct. So a refusal here is logged and nothing is invented to
 * answer with.
 */
function readOpenSession(raw: Record<string, unknown>): OpenSessionMessage | Refusal {
  const session = str(raw, "session");
  if (session === null || !isSessionId(session)) {
    return refuse("open_session.session is not a session id");
  }

  const epoch = int(raw, "env_epoch");
  if (epoch === null || epoch < 0) {
    return refuse("open_session.env_epoch is missing or negative");
  }

  const core = readProcessCore(raw);
  if (isRefusal(core)) {
    return core;
  }

  return { type: "open_session", session, env_epoch: epoch, ...core };
}

function readStartTurn(raw: Record<string, unknown>): StartTurnMessage | Refusal {
  const core = readTurnCore(raw);
  if (isRefusal(core)) {
    return core;
  }

  const at = { session: core.session, turn: core.turn };
  const process = readProcessCore(raw, at);
  if (isRefusal(process)) {
    return process;
  }

  const persona = readPersona(raw);
  if (isRefusal(persona)) {
    return refuse(persona.detail, at);
  }

  const attachments = readAttachments(raw);
  if (isRefusal(attachments)) {
    return refuse(attachments.detail, at);
  }

  const branch = readBranch(raw);
  if (isRefusal(branch)) {
    return refuse(branch.detail, at);
  }

  const delegation = readDelegation(raw);
  if (isRefusal(delegation)) {
    return refuse(delegation.detail, at);
  }

  return {
    type: "start_turn",
    ...core,
    ...process,
    ...(persona === null ? {} : { persona }),
    ...(attachments === null ? {} : { attachments }),
    ...(branch === null ? {} : { branch }),
    ...(delegation === null ? {} : { delegation }),
  };
}

function readPrompt(raw: Record<string, unknown>): PromptMessage | Refusal {
  const core = readTurnCore(raw);
  if (isRefusal(core)) {
    return core;
  }

  const at = { session: core.session, turn: core.turn };
  const persona = readPersona(raw);
  if (isRefusal(persona)) {
    return refuse(persona.detail, at);
  }

  const attachments = readAttachments(raw);
  if (isRefusal(attachments)) {
    return refuse(attachments.detail, at);
  }

  const branch = readBranch(raw);
  if (isRefusal(branch)) {
    return refuse(branch.detail, at);
  }

  const delegation = readDelegation(raw);
  if (isRefusal(delegation)) {
    return refuse(delegation.detail, at);
  }

  return {
    type: "prompt",
    ...core,
    ...(persona === null ? {} : { persona }),
    ...(attachments === null ? {} : { attachments }),
    ...(branch === null ? {} : { branch }),
    ...(delegation === null ? {} : { delegation }),
  };
}

function readSteer(raw: Record<string, unknown>): SteerMessage | Refusal {
  const session = str(raw, "session");
  const turn = str(raw, "turn");
  if (session === null || !isSessionId(session)) {
    return refuse("steer.session is not a session id");
  }
  if (turn === null || !TURN_ID_RE.test(turn)) {
    return refuse("steer.turn is not a ULID");
  }

  const message = str(raw, "message");
  if (message === null || byteLength(message) > MAX_STEER_BYTES) {
    return refuse("steer.message is missing or over 256 KiB", { session, turn });
  }

  return { type: "steer", session, turn, message };
}

function readAbort(raw: Record<string, unknown>): AbortMessage | Refusal {
  const session = str(raw, "session");
  const turn = str(raw, "turn");
  if (session === null || !isSessionId(session)) {
    return refuse("abort.session is not a session id");
  }
  if (turn === null || !TURN_ID_RE.test(turn)) {
    return refuse("abort.turn is not a ULID");
  }

  return { type: "abort", session, turn };
}

function readStopProcess(raw: Record<string, unknown>): StopProcessMessage | Refusal {
  const session = str(raw, "session");
  if (session === null || !isSessionId(session)) {
    return refuse("stop_process.session is not a session id");
  }

  const grace = int(raw, "grace_ms");
  if (grace !== null && (grace < 0 || grace > MAX_GRACE_MS)) {
    return refuse("stop_process.grace_ms is out of range");
  }

  return { type: "stop_process", session, ...(grace === null ? {} : { grace_ms: grace }) };
}

/**
 * Contract 03 §4.8. A read of one session's pi entries.
 *
 * It takes §4.7's binding fields, so `readProcessCore` validates them once for
 * both messages. `request` is the host's correlation id and the one field with
 * no counterpart there: without it an answer names no question.
 */
function readGetEntries(raw: Record<string, unknown>): GetEntriesMessage | Refusal {
  const session = str(raw, "session");
  if (session === null || !isSessionId(session)) {
    return refuse("get_entries.session is not a session id");
  }

  const request = str(raw, "request");
  if (request === null || !TURN_ID_RE.test(request)) {
    return refuse("get_entries.request is not a ULID");
  }

  const epoch = int(raw, "env_epoch");
  if (epoch === null || epoch < 0) {
    return refuse("get_entries.env_epoch is missing or negative");
  }

  const since = str(raw, "since");
  if (since !== null && (since.length === 0 || since.length > MAX_SESSION_ID_LENGTH)) {
    return refuse("get_entries.since is malformed");
  }

  const core = readProcessCore(raw);
  if (isRefusal(core)) {
    return core;
  }

  return {
    type: "get_entries",
    request,
    session,
    env_epoch: epoch,
    ...(since === null ? {} : { since }),
    ...core,
  };
}

function readPing(raw: Record<string, unknown>): PingMessage | Refusal {
  const nonce = str(raw, "nonce");
  if (nonce !== null && nonce.length > MAX_SESSION_ID_LENGTH) {
    return refuse("ping.nonce is over the length limit");
  }

  return { type: "ping", ...(nonce === null ? {} : { nonce }) };
}

function readShutdown(raw: Record<string, unknown>): ShutdownMessage | Refusal {
  const grace = int(raw, "grace_ms");
  if (grace !== null && (grace < 0 || grace > MAX_GRACE_MS)) {
    return refuse("shutdown.grace_ms is out of range");
  }

  return { type: "shutdown", ...(grace === null ? {} : { grace_ms: grace }) };
}

/** One inbound record, validated. The only entry point of this file. */
export function parseHostMessage(line: string): ParseResult {
  let raw: unknown;
  try {
    raw = JSON.parse(line);
  } catch {
    return refuse("line is not JSON");
  }

  if (!isRecord(raw)) {
    return refuse("line is not a JSON object");
  }

  const type = str(raw, "type");
  if (type === null || !HOST_TYPES.has(type)) {
    return refuse("type is missing or unknown");
  }

  const parsed = readByType(type, raw);

  return isRefusal(parsed) ? parsed : { ok: true, message: parsed };
}

function readByType(type: string, raw: Record<string, unknown>): HostMessage | Refusal {
  switch (type) {
    case "hello":
      return readHello(raw);
    case "open_session":
      return readOpenSession(raw);
    case "start_turn":
      return readStartTurn(raw);
    case "prompt":
      return readPrompt(raw);
    case "steer":
      return readSteer(raw);
    case "abort":
      return readAbort(raw);
    case "stop_process":
      return readStopProcess(raw);
    case "get_entries":
      return readGetEntries(raw);
    case "ping":
      return readPing(raw);
    default:
      return readShutdown(raw);
  }
}
