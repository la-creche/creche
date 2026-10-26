// One prompt turn: `session/prompt` to Flue and back.
//
//   prompt ──► dispatch ──► read({onEvent}) ──► chunk ──► session/update
//                                 │
//                                 └─► settled ──► completed ─► end_turn
//                                              ├─► aborted ───► cancelled
//                                              └─► failed ────► JSON-RPC error
//
//   session/cancel ──► abort() ──► the prompt answers `cancelled`
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
import { chunkToUpdates, forward, SubmissionChunks } from "./mapper.js";
import type { Sessions } from "./sessions.js";

/** The largest prompt text, in UTF-8 bytes: attendance's prompt cap. */
export const MAX_PROMPT_BYTES = 262_144;

export type Settlement = "completed" | "failed" | "aborted";

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

  public constructor(
    private readonly sessions: Sessions,
    private readonly live: Live,
  ) {}

  /** The updates go to every peer on the session, the prompting one too. */
  public async prompt(params: PromptRequest, peer: Peer): Promise<Outcome<PromptResponse>> {
    const { sessionId } = params;
    const handle = this.sessions.handle(sessionId);
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
    this.live.attach(sessionId, peer);
    let sent = Promise.resolve();
    let lastRecordAt: string | null = null;

    const settlement = await this.run(running, text.value, (chunk) => {
      lastRecordAt = ("timestamp" in chunk ? chunk.timestamp : undefined) ?? lastRecordAt;
      const items = chunkToUpdates(chunk).map((update) => ({ position: chunk.position, update }));
      sent = this.live.publish(sessionId, items);
    });
    this.running.delete(sessionId);

    const info = this.sessions.recordTurn(sessionId, text.value, lastRecordAt);
    if (info !== null) {
      sent = this.live.publish(sessionId, [{ position: null, update: { sessionUpdate: "session_info_update", ...info } }]);
    }

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
