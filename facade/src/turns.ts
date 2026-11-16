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
// The files of a prompt go to `WORKSPACE/attachments/` right before the
// dispatch, and the text names each one on a marker line
// (`src/attachments.ts`). Each image goes to the model too.
//
// The turn start and the turn end each go to guest init on the local link
// (`src/local-link.ts`, decision CH21). The end carries the title and
// `updatedAt` of the turn record. The same end goes to each peer as
// `session_info_update`, and only the first turn names the session. A
// `usage_update` follows it, when the meter has a count (`src/usage.ts`).
// After the pull, each peer gets the commands of the pulled skills
// (`src/commands.ts`), so a granted or removed skill reaches the menu.
//
// One turn runs per session at a time. A second prompt is refused: Flue
// would join it to the running response, and its reply would have no turn
// of its own. A rewind holds the session as a turn does (`between`), so a
// prompt never runs on the instance that a rewind replaces.

import { AgentRunError } from "@flue/runtime";
import type { AgentHandleDispatchRequest, AgentInstanceHandle, ConversationStreamChunk, DeliveredAttachment } from "@flue/runtime";

import type {
  CancelNotification,
  ContentBlock,
  Outcome,
  Peer,
  PromptRequest,
  PromptResponse,
  StopReason,
} from "./acp.js";
import { bodyOf, checkFiles, fileOf, FileKind } from "./attachments.js";
import type { Attachments, PromptFile } from "./attachments.js";
import { commandsUpdate } from "./commands.js";
import type { Commands } from "./commands.js";
import { byteLength } from "./line.js";
import type { Item, Live } from "./live.js";
import type { LocalLink } from "./local-link.js";
import { chunkToUpdates, forward, SubmissionChunks } from "./mapper.js";
import { infoOf } from "./sessions.js";
import type { OneSession } from "./sessions.js";
import type { SessionMeter } from "./usage.js";

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

/** A checked prompt: the user text and the files of its file blocks. */
export interface Prompt {
  /** The text of the user. The title and the turn end read it. */
  readonly text: string;
  readonly files: readonly PromptFile[];
}

/**
 * The prompt as one text and its files. Text blocks are the text. A
 * resource link is its URI on a line of its own. An image block and a
 * resource block are files: attendance scanned and rebuilt each one
 * (docs/design/attachments.md). The facade offers no audio, so a client
 * sends none.
 */
export function promptText(blocks: readonly ContentBlock[]): Outcome<Prompt> {
  const parts: string[] = [];
  const files: PromptFile[] = [];
  for (const block of blocks) {
    const file = fileOf(block);
    if (file !== null) {
      if (!file.ok) {
        return file;
      }

      files.push(file.value);
    } else if (block.type === "text") {
      parts.push(block.text);
    } else if (block.type === "resource_link") {
      parts.push(block.uri);
    } else {
      return invalid(`the facade takes no ${block.type} block`);
    }
  }

  const text = parts.join("\n\n");
  if (text.trim().length === 0 && files.length === 0) {
    return invalid("the prompt has no text");
  }

  if (byteLength(text) > MAX_PROMPT_BYTES) {
    return invalid(`the prompt is over ${MAX_PROMPT_BYTES} bytes`);
  }

  const checked = checkFiles(files);
  if (!checked.ok) {
    return checked;
  }

  return { ok: true, value: { text, files: checked.value } };
}

/**
 * The message of a prompt for Flue. It writes each file to the workspace
 * first, so call it right before the dispatch. Each image goes to the
 * model as an attachment too.
 */
function messageOf(attachments: Attachments, prompt: Prompt): AgentHandleDispatchRequest {
  const saved = attachments.save(prompt.files);
  const images: DeliveredAttachment[] = saved
    .filter((file) => file.kind === FileKind.Image)
    .map((file) => ({ type: "image", data: file.bytes.toString("base64"), mimeType: file.mimeType, filename: file.savedName }));
  const body = bodyOf(prompt.text, saved);

  return { message: images.length === 0 ? { kind: "user", body } : { kind: "user", body, attachments: images } };
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
  /** The sessions that a rewind holds. */
  private readonly held = new Set<string>();

  /** `link` is the local link of guest init, as the supervisor gives it to `serve` (`src/supervisor.ts`). */
  public constructor(
    private readonly session: OneSession,
    private readonly live: Live,
    private readonly link: LocalLink | null,
    private readonly meter: SessionMeter,
    private readonly attachments: Attachments,
    private readonly refresh: Refresh = NO_REFRESH,
    /** The session's commands. Null: the facade sends no `available_commands_update`. */
    private readonly commands: Commands | null = null,
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

    if (this.held.has(sessionId)) {
      return { ok: false, refusal: { kind: "busy", detail: "a rewind runs on this session" } };
    }

    const read = promptText(params.prompt);
    if (!read.ok) {
      return read;
    }

    const running: Running = { handle, cancelled: false };
    this.running.set(sessionId, running);
    try {
      return await this.pulledTurn(running, params, peer, read.value);
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
    prompt: Prompt,
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

    // After a cold start, the render reads the level of the last response.
    await this.meter.restore();

    // The first send makes the Flue instance, so this turn names the session.
    const first = await this.session.fresh();
    this.live.attach(sessionId, peer);
    if (this.commands !== null) {
      await this.live.publish(sessionId, [{ position: null, update: commandsUpdate(this.commands) }]);
    }

    // Each start gets an end, so guest init sees each running turn
    // (decisions CH21, CH32 rule 6).
    this.link?.turnStart();
    let sent = Promise.resolve();
    let lastRecordAt: string | null = null;

    const settlement = await this.run(running, prompt, (chunk) => {
      lastRecordAt = ("timestamp" in chunk ? chunk.timestamp : undefined) ?? lastRecordAt;
      this.meter.see(chunk);
      const items = chunkToUpdates(chunk).map((update) => ({ position: chunk.position, update }));
      sent = this.live.publish(sessionId, items);
    });
    this.running.delete(sessionId);

    const end = this.session.turnEndOf(prompt.text, lastRecordAt);
    this.link?.turnEnd(end.title, end.updatedAt);
    const info: Item = { position: null, update: { sessionUpdate: "session_info_update", ...infoOf(end, first) } };
    const usage = await this.meter.usageUpdate();
    sent = this.live.publish(sessionId, usage === null ? [info] : [info, { position: null, update: usage }]);
    await sent;

    return settle(settlement.outcome, settlement.detail);
  }

  /**
   * Runs `change` with the session held, so no prompt starts until it
   * ends. A running prompt or another change refuses it as busy.
   */
  public async between<T>(sessionId: string, change: () => Promise<Outcome<T>>): Promise<Outcome<T>> {
    if (this.running.has(sessionId) || this.held.has(sessionId)) {
      return { ok: false, refusal: { kind: "busy", detail: "a prompt or a rewind runs on this session" } };
    }

    this.held.add(sessionId);
    try {
      return await change();
    } finally {
      this.held.delete(sessionId);
    }
  }

  public async cancel(params: CancelNotification): Promise<void> {
    const running = this.running.get(params.sessionId);
    if (running === undefined) {
      return;
    }

    running.cancelled = true;
    await running.handle.abort();
  }

  /** Writes the files, dispatches the prompt and reads its chunks until the submission settles. */
  private async run(
    running: Running,
    prompt: Prompt,
    onChunk: (chunk: ConversationStreamChunk) => void,
  ): Promise<{ outcome: Settlement; detail: string }> {
    try {
      const receipt = await running.handle.dispatch(messageOf(this.attachments, prompt));
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
