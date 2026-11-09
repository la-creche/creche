// One prompt turn: `session/prompt` to Flue and back.
//
//   prompt ──► dispatch ──► read({onEvent}) ──► chunk ──► session/update
//     │                           │
//     │                           └─► settled ──► completed ─► end_turn
//     │                                        ├─► aborted ───► cancelled
//     │                                        └─► failed ────► JSON-RPC error
//     │
//     └─► refresh first ──► a fault ──► JSON-RPC error. The facade serves on.
//
//   session/cancel ──► abort() ──► the prompt answers `cancelled`
//
// The turn start and the turn end each go to guest init on the local link
// (`src/local-link.ts`, decision CH21). The end carries the title and
// `updatedAt` of the turn record. The same end goes to each peer as
// `session_info_update`, and only the first turn names the session.
//
// One turn runs per session at a time. A second prompt is refused: Flue
// would join it to the running response, and its reply would have no turn
// of its own.

import { AgentRunError } from "@flue/runtime";
import type { AgentInstanceHandle, ConversationStreamChunk } from "@flue/runtime";

import type {
  CancelNotification,
  ContentBlock,
  Outcome,
  Peer,
  PromptRequest,
  PromptResponse,
  StopReason,
} from "./acp.js";
import { byteLength } from "./line.js";
import type { Live } from "./live.js";
import type { LocalLink } from "./local-link.js";
import { chunkToUpdates, forward, SubmissionChunks } from "./mapper.js";
import { infoOf } from "./sessions.js";
import type { OneSession } from "./sessions.js";

/** The largest prompt text, in UTF-8 bytes: attendance's prompt cap. */
export const MAX_PROMPT_BYTES = 262_144;

export type Settlement = "completed" | "failed" | "aborted";

/**
 * Pulls the family state into the agent before a prompt of the session
 * `sessionId` (decision CH5a). Null: the prompt runs. A text: the fault
 * that fails the prompt.
 */
export type Refresh = (sessionId: string) => Promise<string | null>;

/** No pull: the agent keeps the state of its start. */
export const NO_REFRESH: Refresh = () => Promise.resolve(null);

/** The one map from a settlement to an answer. `failed` is never a stop reason. */
export function settle(settlement: Settlement, detail: string): Outcome<PromptResponse> {
  switch (settlement) {
    case "completed":
      return stop("end_turn");
    case "aborted":
      return stop("cancelled");
    case "failed":
      return { ok: false, refusal: { kind: "failed", detail: forward(`the turn failed: ${detail}`) } };
    default: {
      const unknown: never = settlement;
      return unknown;
    }
  }
}

function stop(stopReason: StopReason): Outcome<PromptResponse> {
  return { ok: true, value: { stopReason } };
}

function invalid<T>(detail: string): Outcome<T> {
  return { ok: false, refusal: { kind: "invalid_params", detail } };
}

/**
 * The prompt as one text. Text blocks are the text. A resource link is its
 * URI on a line of its own. The facade offers no image, audio or embedded
 * resource, so a client sends none.
 */
export function promptText(blocks: readonly ContentBlock[]): Outcome<string> {
  const parts: string[] = [];
  for (const block of blocks) {
    if (block.type === "text") {
      parts.push(block.text);
    } else if (block.type === "resource_link") {
      parts.push(block.uri);
    } else {
      return invalid(`the facade takes no ${block.type} block`);
    }
  }

  const text = parts.join("\n\n");
  if (text.trim().length === 0) {
    return invalid("the prompt has no text");
  }

  if (byteLength(text) > MAX_PROMPT_BYTES) {
    return invalid(`the prompt is over ${MAX_PROMPT_BYTES} bytes`);
  }

  return { ok: true, value: text };
}

interface Running {
  readonly handle: AgentInstanceHandle;
  cancelled: boolean;
}

function describe(error: unknown): string {
  const cause = error instanceof Error ? error.cause : undefined;
  if (cause instanceof Error) {
    return cause.message;
  }

  return error instanceof Error ? error.message : String(error);
}

export class Turns {
  private readonly running = new Map<string, Running>();

  /** `link` is the local link of guest init, as the supervisor gives it to `serve` (`src/supervisor.ts`). */
  public constructor(
    private readonly session: OneSession,
    private readonly live: Live,
    private readonly link: LocalLink | null,
    private readonly refresh: Refresh = NO_REFRESH,
  ) {}

  /** The updates go to every peer on the session, the prompting one too. */
  public async prompt(params: PromptRequest, peer: Peer): Promise<Outcome<PromptResponse>> {
    const { sessionId } = params;
    const handle = this.session.handle(sessionId);
    if (handle === null) {
      return { ok: false, refusal: { kind: "unknown_session", sessionId } };
    }

    if (this.running.has(sessionId)) {
      return { ok: false, refusal: { kind: "busy", detail: "a prompt already runs on this session" } };
    }

    const text = promptText(params.prompt);
    if (!text.ok) {
      return text;
    }

    const running: Running = { handle, cancelled: false };
    this.running.set(sessionId, running);
    try {
      return await this.pulledTurn(running, params, peer, text.value);
    } finally {
      // A later prompt can hold the session by now. Free only this one.
      if (this.running.get(sessionId) === running) {
        this.running.delete(sessionId);
      }
    }
  }

  /** The pull, then the turn. The session is busy until the caller frees it. */
  private async pulledTurn(
    running: Running,
    params: PromptRequest,
    peer: Peer,
    text: string,
  ): Promise<Outcome<PromptResponse>> {
    const { sessionId } = params;
    // A fault fails this prompt only (decision CH23 rule 2).
    const fault = await this.refresh(sessionId);
    if (running.cancelled) {
      return settle("aborted", "");
    }

    if (fault !== null) {
      return settle("failed", `the family state did not read (${fault})`);
    }

    // The first send makes the Flue instance, so this turn names the session.
    const first = await this.session.fresh();
    this.live.attach(sessionId, peer);
    // Each start gets an end, so guest init sees each running turn
    // (decisions CH21, CH32 rule 6).
    this.link?.turnStart();
    let sent = Promise.resolve();
    let lastRecordAt: string | null = null;

    const settlement = await this.run(running, text, (chunk) => {
      lastRecordAt = ("timestamp" in chunk ? chunk.timestamp : undefined) ?? lastRecordAt;
      const items = chunkToUpdates(chunk).map((update) => ({ position: chunk.position, update }));
      sent = this.live.publish(sessionId, items);
    });
    this.running.delete(sessionId);

    const end = this.session.turnEndOf(text, lastRecordAt);
    this.link?.turnEnd(end.title, end.updatedAt);
    sent = this.live.publish(sessionId, [{ position: null, update: { sessionUpdate: "session_info_update", ...infoOf(end, first) } }]);
    await sent;

    return settle(settlement.outcome, settlement.detail);
  }

  public async cancel(params: CancelNotification): Promise<void> {
    const running = this.running.get(params.sessionId);
    if (running === undefined) {
      return;
    }

    running.cancelled = true;
    await running.handle.abort();
  }

  /** Dispatches the text and reads its chunks until the submission settles. */
  private async run(
    running: Running,
    text: string,
    onChunk: (chunk: ConversationStreamChunk) => void,
  ): Promise<{ outcome: Settlement; detail: string }> {
    try {
      const receipt = await running.handle.dispatch(text);
      // A cancel that came before the dispatch had nothing to abort.
      if (running.cancelled) {
        await running.handle.abort();
      }

      const mine = new SubmissionChunks(receipt.submissionId);
      await running.handle.read(receipt, {
        onEvent: (chunk) => {
          if (mine.accept(chunk)) {
            onChunk(chunk);
          }
        },
      });

      return { outcome: "completed", detail: "" };
    } catch (error) {
      if (error instanceof AgentRunError) {
        return { outcome: error.outcome, detail: describe(error) };
      }

      return { outcome: "failed", detail: describe(error) };
    }
  }
}
