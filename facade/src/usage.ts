// The meter and the thinking level of the one session.
//
//   a live chunk's metadata ──► see ────► the context tokens of the session
//   the history of the session ─► seed ─► the tokens and the level of its
//                                         last response, after a cold start
//   a turn end, a load ──► usageUpdate ──► usage_update to the client
//
// The agent keeps both values in the metadata of each response
// (`agent/src/usage.ts`), so they live in `flue.db`. The facade keeps them
// in memory and reads the history once per process to seed them. A value
// that the process saw wins over the history.
//
// `used` and `size` are the context fill of the last model call. The
// family spend of the day rides in `_meta`, because ACP's `cost` is the
// cost of the session, which LiteLLM does not give.

import type { ConversationStreamChunk } from "@flue/runtime";
import { CONTEXT_TOKENS_KEY, isThinkingChoice, THINKING_LEVEL_KEY } from "agent/agent";
import type { ThinkingChoice, ThinkingLevels } from "agent/agent";

import type { SessionUpdate } from "./acp.js";
import { SPEND_CURRENCY, SPEND_META_KEY } from "./vectors.js";

/** The family spend of the budget day, in USD. */
export interface Spend {
  readonly amount: number;
  /** `model.budget_usd_per_day`, or null when the key has no budget. */
  readonly budget: number | null;
}

/** Where the meter reads what it does not keep. */
export interface MeterSource {
  /** The context window of the model of the last pull, in tokens. */
  window(): number;
  /** The family spend of the budget day, or null when it does not read. */
  spend(): Promise<Spend | null>;
}

/** The history of the session, or null when it does not read. */
export type HistoryChunks = () => Promise<readonly ConversationStreamChunk[] | null>;

/** The last values that the responses of a history recorded. */
export interface Memo {
  readonly contextTokens: number | null;
  readonly thinking: ThinkingChoice | null;
}

const NO_MEMO: Memo = { contextTokens: null, thinking: null };

/** The metadata objects of a chunk, in the order Flue merges them. */
function metadataOf(chunk: ConversationStreamChunk): readonly Record<string, unknown>[] {
  switch (chunk.type) {
    case "conversation-reset":
      return chunk.snapshot.messages.flatMap((message) => (message.metadata === undefined ? [] : [message.metadata]));
    case "message-appended":
      return chunk.message.metadata === undefined ? [] : [chunk.message.metadata];
    case "message-started":
      return chunk.metadata === undefined ? [] : [chunk.metadata];
    case "message-metadata":
      return [chunk.metadata];
    default:
      return [];
  }
}

function tokensOf(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

/** `memo` with the values of `metadata` that are valid. Data from `flue.db` is untrusted. */
function merged(memo: Memo, metadata: Record<string, unknown>): Memo {
  const contextTokens = tokensOf(metadata[CONTEXT_TOKENS_KEY]) ?? memo.contextTokens;
  const level = metadata[THINKING_LEVEL_KEY];

  return { contextTokens, thinking: isThinkingChoice(level) ? level : memo.thinking };
}

/** The last values that the responses of `chunks` recorded. */
export function memoOf(chunks: readonly ConversationStreamChunk[]): Memo {
  let memo = NO_MEMO;
  for (const chunk of chunks) {
    for (const metadata of metadataOf(chunk)) {
      memo = merged(memo, metadata);
    }
  }

  return memo;
}

/** The `usage_update` of a context fill and a spend. */
export function usageUpdateOf(used: number, size: number, spend: Spend | null): SessionUpdate {
  const update: SessionUpdate = { sessionUpdate: "usage_update", used, size };
  if (spend === null) {
    return update;
  }

  return { ...update, _meta: { [SPEND_META_KEY]: { amount: spend.amount, budget: spend.budget, currency: SPEND_CURRENCY } } };
}

export class SessionMeter {
  private contextTokens: number | null = null;
  private seeded = false;

  /** `source` null: the facade sends no `usage_update`. */
  public constructor(
    private readonly sessionId: string,
    private readonly levels: ThinkingLevels,
    private readonly source: MeterSource | null,
    private readonly history: HistoryChunks,
  ) {}

  /** The thinking level of the session. */
  public get level(): ThinkingChoice {
    return this.levels.of(this.sessionId);
  }

  /** Sets the level of the next prompt. A turn that runs keeps its level. */
  public choose(level: ThinkingChoice): void {
    this.levels.set(this.sessionId, level);
  }

  /** Reads the count of a live chunk of the session. */
  public see(chunk: ConversationStreamChunk): void {
    for (const metadata of metadataOf(chunk)) {
      this.contextTokens = tokensOf(metadata[CONTEXT_TOKENS_KEY]) ?? this.contextTokens;
    }
  }

  /** Seeds what this process has not seen from the chunks of the history. */
  public seed(chunks: readonly ConversationStreamChunk[]): void {
    this.seeded = true;
    const memo = memoOf(chunks);
    this.contextTokens ??= memo.contextTokens;
    if (memo.thinking !== null && !this.levels.has(this.sessionId)) {
      this.levels.set(this.sessionId, memo.thinking);
    }
  }

  /** Reads the history once per process, before the first prompt. A read that fails tries again at the next prompt. */
  public async restore(): Promise<void> {
    if (this.seeded) {
      return;
    }

    const chunks = await this.history();
    if (chunks !== null) {
      this.seed(chunks);
    }
  }

  /** The `usage_update` of the session, or null with no source or before a first count. */
  public async usageUpdate(): Promise<SessionUpdate | null> {
    if (this.source === null || this.contextTokens === null) {
      return null;
    }

    return usageUpdateOf(this.contextTokens, this.source.window(), await this.source.spend());
  }
}
