// The sessions of the facade. A session is one Flue agent instance: its id
// is the instance id, and `creche_sessions` lists it.
//
//   session/new  ──► mint an id ──► init() ──► one row
//   session/list ──► one page of rows, newest first
//   a turn ends  ──► the title from the first user text, `updatedAt` from
//                    the last record, the creche keys ──► session_info_update
//
// The creche keys of `_meta` (`src/creche-meta.ts`) go on the row: the job
// id from `session/new`, the delegation and the trigger from the first
// `session/prompt` that sends them. None of them reaches the model.
//
// CONTRACT-QUESTION: the owner asked to forward `creche/delegation` to the
// chaperone as `_meta["creche.delegation"]` on each `tools/call` of the
// session, so that the chaperone can bound a delegation to one hop at run
// time. Flue 2.3.0 gives no way to do it. Its MCP adapter calls
// `client.callTool({name, arguments}, requestOptions)` with no `_meta`
// (`createMcpTools` in `@flue/runtime/dist/mcp-*.mjs`), and
// `McpConnectionDefinition` has no per-call hook: `headers` are static and
// read at the first connect, and `auth` sets only the bearer. Only a custom
// `fetch` that rewrites each JSON-RPC body could add the key, and that is
// not a contract. Flue keeps one connection per instance, so a static
// header per session can exist, but only if the agent module reads the
// delegation per instance. The chaperone reads no such header. Until Flue or the owner gives a path, the facade sends the
// three keys in the `_meta` of `session_info_update`, for attendance and
// the noticeboard to read.

import { randomUUID } from "node:crypto";
import { isAbsolute } from "node:path";

import { init } from "@flue/runtime";
import type { Agent, AgentInstanceHandle } from "@flue/runtime";

import type {
  ListSessionsRequest,
  ListSessionsResponse,
  LoadSessionRequest,
  NewSessionRequest,
  NewSessionResponse,
  Outcome,
  SessionInfo,
  SessionInfoUpdate,
} from "./acp.js";
import { jobOf, promptMetaOf } from "./creche-meta.js";
import type { Meta } from "./creche-meta.js";
import { cutToBytes } from "./line.js";
import type { SessionIndex, SessionRow } from "./session-index.js";
import { META_DELEGATION, META_JOB, META_TRIGGER } from "./vectors.js";

/** The longest title, in UTF-8 bytes. */
export const MAX_TITLE_BYTES = 256;

function refuse<T>(detail: string): Outcome<T> {
  return { ok: false, refusal: { kind: "invalid_params", detail } };
}

/** The first line of `text` with words, its white space folded, cut. */
export function titleOf(text: string): string {
  const line = text.split("\n").find((one) => one.trim().length > 0) ?? "";

  return cutToBytes(line.trim().replace(/\s+/g, " "), MAX_TITLE_BYTES);
}

/**
 * What the facade refuses in `session/new` and `session/load`, or null.
 * The family decides the agent's tools. A client's offer would be dropped
 * in silence, so the facade refuses it.
 */
export function sessionFault(params: NewSessionRequest | LoadSessionRequest): string | null {
  if (!isAbsolute(params.cwd)) {
    return "cwd is not an absolute path";
  }

  if (params.mcpServers.length > 0) {
    return "the facade takes no client MCP server";
  }

  return (params.additionalDirectories ?? []).length > 0 ? "the facade takes no additional directory" : null;
}

/** The creche keys of a row, as `session_info_update` sends them, or null. */
function metaOf(row: SessionRow): Record<string, unknown> | null {
  const kept = [
    [META_JOB, row.jobId],
    [META_DELEGATION, row.delegation],
    [META_TRIGGER, row.trigger],
  ].filter(([, value]) => value !== null);

  return kept.length > 0 ? Object.fromEntries(kept) : null;
}

function toInfo(row: SessionRow): SessionInfo {
  return { sessionId: row.id, cwd: row.cwd, title: row.title, updatedAt: row.updatedAt };
}

export class Sessions {
  private readonly handles = new Map<string, AgentInstanceHandle>();

  public constructor(
    private readonly agent: Agent,
    private readonly index: SessionIndex,
    private readonly now: () => Date = () => new Date(),
  ) {}

  public async newSession(params: NewSessionRequest): Promise<Outcome<NewSessionResponse>> {
    const fault = sessionFault(params);
    if (fault !== null) {
      return refuse(fault);
    }

    const job = jobOf(params._meta);
    if (!job.ok) {
      return refuse(job.detail);
    }

    const id = randomUUID();
    // `uid: null`: the first send creates the instance, and never joins one.
    this.handles.set(id, init(this.agent, { id, uid: null }));
    this.index.create(id, params.cwd, this.now().toISOString(), job.value);

    return { ok: true, value: { sessionId: id } };
  }

  public async listSessions(params: ListSessionsRequest): Promise<Outcome<ListSessionsResponse>> {
    const page = this.index.list(params.cwd ?? null, params.cursor ?? null);
    if (page === null) {
      return refuse("the cursor is not one that session/list gave");
    }

    return { ok: true, value: { sessions: page.rows.map(toInfo), nextCursor: page.nextCursor } };
  }

  /** True when `creche_sessions` lists the session. */
  public known(id: string): boolean {
    return this.index.get(id) !== null;
  }

  /** The Flue handle of a listed session, or null. */
  public handle(id: string): AgentInstanceHandle | null {
    const known = this.handles.get(id);
    if (known !== undefined) {
      return known;
    }

    if (this.index.get(id) === null) {
      return null;
    }

    const handle = init(this.agent, { id });
    this.handles.set(id, handle);

    return handle;
  }

  /**
   * Keeps the delegation and the trigger of a prompt on the session row,
   * before the turn runs. A session keeps its first value of each, so a
   * prompt that sends another one is refused.
   */
  public keepPromptMeta(id: string, meta: Meta): Outcome<null> {
    const sent = promptMetaOf(meta);
    if (!sent.ok) {
      return refuse(sent.detail);
    }

    const row = this.index.get(id);
    if (row === null) {
      return { ok: false, refusal: { kind: "unknown_session", sessionId: id } };
    }

    const { delegation, trigger } = sent.value;
    if (delegation !== null && row.delegation !== null && delegation !== row.delegation) {
      return refuse("the session already has another delegation");
    }

    if (trigger !== null && row.trigger !== null && JSON.stringify(trigger) !== JSON.stringify(row.trigger)) {
      return refuse("the session already has another trigger");
    }

    this.index.keepOnce(id, delegation, trigger);

    return { ok: true, value: null };
  }

  /**
   * One turn ended. The first turn names the session. Each turn moves
   * `updatedAt` to the time of its last record. Null for an unknown id.
   */
  public recordTurn(id: string, userText: string, lastRecordAt: string | null): SessionInfoUpdate | null {
    const row = this.index.get(id);
    if (row === null) {
      return null;
    }

    const updatedAt = lastRecordAt ?? this.now().toISOString();
    this.index.touch(id, updatedAt);

    const meta = metaOf(row);
    const tail = meta === null ? { updatedAt } : { updatedAt, _meta: meta };
    const title = titleOf(userText);
    if (row.title !== null || title.length === 0) {
      return tail;
    }

    this.index.nameOnce(id, title);

    return { title, ...tail };
  }
}
