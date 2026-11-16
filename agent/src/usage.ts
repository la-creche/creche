// What a response used, kept in its message metadata, so a later
// `session/load` reads it back from `flue.db`.
//
//   Flue `turn` event ──► ContextTokens.see ──► the last count of the instance
//   response start    ──► { thinkingChoice }  the level of this response
//   response finish   ──► { contextTokens }   the tokens of its last model call
//
// The context fill is the size of the last model call of the agent: its
// input, its cache reads and writes, and its output. Flue's compaction
// counts the context the same way (`calculateContextTokens`). The
// aggregate of `useResponseFinish` sums every call of the response, so it
// is not the fill.

import { observe } from "@flue/runtime";
import type { FlueEvent, PromptUsage } from "@flue/runtime";

/** The metadata key of the context tokens of a response. */
export const CONTEXT_TOKENS_KEY = "contextTokens";

/**
 * The metadata key of the thinking level of a response. The first release
 * wrote `thinkingLevel`, whose `off` meant what `auto` means now, so the
 * facade reads only this key and an old session gets the default.
 */
export const THINKING_LEVEL_KEY = "thinkingChoice";

/** The tokens in context after a model call, as Flue's compaction counts them. */
export function contextTokensOf(usage: PromptUsage): number {
  return usage.totalTokens || usage.input + usage.output + usage.cacheRead + usage.cacheWrite;
}

/** The context tokens of the last model call of each instance. */
export class ContextTokens {
  private readonly last = new Map<string, number>();

  /** Subscribes to Flue's events. One holder serves the whole process. */
  public constructor() {
    observe((event) => {
      this.see(event);
    });
  }

  /** The count of the last model call of `id`, or null before one. */
  public of(id: string | null): number | null {
    return (id === null ? undefined : this.last.get(id)) ?? null;
  }

  /** Keeps the count of an agent call. A compaction call or a failed call counts nothing. */
  public see(event: FlueEvent): void {
    if (event.type !== "turn" || event.purpose !== "agent" || event.isError) {
      return;
    }

    const { usage } = event.response;
    if (event.instanceId === undefined || usage === undefined) {
      return;
    }

    this.last.set(event.instanceId, contextTokensOf(usage));
  }
}
