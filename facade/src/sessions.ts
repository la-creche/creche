// The one session of the facade. A VM serves one session, and its id is the
// host's: guest init passes it as `CRECHE_SESSION_ID` (`src/mode.ts`,
// decisions CH1, CH25 rule 2). The session is one Flue agent instance, and
// its ACP session id is the instance id.
//
//   start        ──► init(agent, {id}) ──► the one handle (decision CH21-8)
//   session/load ──► the one id, or resourceNotFound
//   a turn ends  ──► the title of the first turn, `updatedAt` from the last
//                    record ──► session_info_update
//
// The facade opens the session at start. Flue makes the instance at the
// first send, so a session with no turn yet loads an empty history. A send
// with no `uid` condition creates the instance or continues it.
//
// attendance answers `session/new` and `session/list` from the host list
// (decision CH21-7), so the facade serves neither. The host row carries the
// session's cwd, title, times and origin, so the facade keeps no table of
// its own and reads no creche key of `_meta` (decision CH21-4).
//
// CONTRACT-QUESTION: the owner asked to forward the delegation to the
// chaperone as `_meta["creche.delegation"]` on each `tools/call` of the
// session, so that the chaperone can bound a delegation to one hop at run
// time. The delegation now rides in the origin of the host row (decision
// CH21-4), and nothing gives it to the facade. Flue 2.3.0 also gives no way
// to send it: its MCP adapter calls `client.callTool({name, arguments},
// requestOptions)` with no `_meta` (`createMcpTools` in
// `@flue/runtime/dist/mcp-*.mjs`), and `McpConnectionDefinition` has no
// per-call hook. The chaperone reads no such key. The facade sends nothing
// until a decision gives the path.

import { isAbsolute } from "node:path";

import { getAgentInstance, init } from "@flue/runtime";
import type { Agent, AgentInstanceHandle } from "@flue/runtime";
import type { Wire } from "agent/wire-read";

import type { LoadSessionRequest, SessionInfoUpdate } from "./acp.js";
import { cutToBytes } from "./line.js";

/** The longest title, in UTF-8 bytes. */
export const MAX_TITLE_BYTES = 256;

/** The longest cwd, in code points: `SessionCwd` of the wire (decision CH21). */
export const MAX_CWD_CHARS = 4096;

/**
 * The first line of `text` with words, its white space folded, cut, or "".
 * A title is a `SessionTitle` of the wire (decision CH21). JavaScript's `\s`
 * and `trim` hold the white space of its pattern. The cut can end in a
 * space, so the end is trimmed after the cut. A lone surrogate becomes
 * U+FFFD, because no wire text holds one.
 */
export function titleOf(text: string): string {
  const line = text.toWellFormed().split("\n").find((one) => one.trim().length > 0) ?? "";

  return cutToBytes(line.trim().replace(/\s+/g, " "), MAX_TITLE_BYTES).trimEnd();
}

/**
 * What the facade refuses in `session/load`, or null. The family decides
 * the agent's tools. A client's offer would be dropped in silence, so the
 * facade refuses it.
 */
export function sessionFault(params: LoadSessionRequest): string | null {
  if (!isAbsolute(params.cwd)) {
    return "cwd is not an absolute path";
  }

  // `SessionCwd` of the wire (decision CH21): no NUL, at most 4096 code points.
  if (params.cwd.includes("\0")) {
    return "cwd holds a NUL";
  }

  if ([...params.cwd].length > MAX_CWD_CHARS) {
    return `cwd is over ${String(MAX_CWD_CHARS)} characters`;
  }

  if (params.mcpServers.length > 0) {
    return "the facade takes no client MCP server";
  }

  return (params.additionalDirectories ?? []).length > 0 ? "the facade takes no additional directory" : null;
}

/** The end of one turn, as the turn end event of the local link takes it. */
export interface TurnEnd {
  /** `titleOf` of the turn's prompt: the title of a turn end (decision CH21). */
  readonly title: string;
  /** The time of the turn's last record, or the clock when it wrote none. */
  readonly updatedAt: string;
}

/**
 * The `session_info_update` of a turn end. The first turn names the
 * session, as the vmm keeps the first title of the row (decision CH32
 * rule 4). Each turn moves `updatedAt`.
 */
export function infoOf({ title, updatedAt }: TurnEnd, first: boolean): SessionInfoUpdate {
  return first && title.length > 0 ? { title, updatedAt } : { updatedAt };
}

export class OneSession {
  private readonly opened: AgentInstanceHandle;

  public constructor(
    private readonly agent: Agent,
    /** The host's id of the one session (decision CH25 rule 2). */
    public readonly id: Wire<"SessionId">,
    private readonly now: () => Date = () => new Date(),
  ) {
    this.opened = init(agent, { id });
  }

  /** True for the id of the one session. */
  public known(id: string): boolean {
    return id === this.id;
  }

  /** The Flue handle of the one session, or null for another id. */
  public handle(id: string): AgentInstanceHandle | null {
    return this.known(id) ? this.opened : null;
  }

  /** True before the first send of the session: Flue holds no instance of it yet. */
  public async fresh(): Promise<boolean> {
    return (await getAgentInstance(this.agent, this.id)) === null;
  }

  /** The end of a turn on the prompt `userText`. */
  public turnEndOf(userText: string, lastRecordAt: string | null): TurnEnd {
    return { title: titleOf(userText), updatedAt: lastRecordAt ?? this.now().toISOString() };
  }
}
