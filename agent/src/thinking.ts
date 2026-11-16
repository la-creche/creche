// The thinking level of each session: how much the model reasons before it
// answers. The facade sets it from the client's `session/set_config_option`.
// Each render gives it to `useModel`, so a new level reaches the next
// submission, never a turn that runs. The level changes the model call only.
// It widens no tool and no grant.
//
//   set_config_option ──► ThinkingLevels.set(id, level)
//   render            ──► useModel(spec, { thinkingLevel: levels.of(id) })
//
// `off` is the default. pi sends no `reasoning_effort` for `off`, so a
// session that never sets a level calls the model as before.

import type { ThinkingLevel } from "@flue/runtime";

/** The levels a client can choose, in the order a picker shows them. */
export const THINKING_CHOICES = ["off", "low", "medium", "high"] as const satisfies readonly ThinkingLevel[];

export type ThinkingChoice = (typeof THINKING_CHOICES)[number];

/** The level of a session that never set one. */
export const DEFAULT_THINKING: ThinkingChoice = "off";

/** True when `value` is one of `THINKING_CHOICES`. */
export function isThinkingChoice(value: unknown): value is ThinkingChoice {
  return THINKING_CHOICES.some((choice) => choice === value);
}

/** The level of each session, by its instance id. */
export class ThinkingLevels {
  private readonly levels = new Map<string, ThinkingChoice>();

  /** The level of `id`. A bare render, with no id, gets the default. */
  public of(id: string | null): ThinkingChoice {
    return (id === null ? undefined : this.levels.get(id)) ?? DEFAULT_THINKING;
  }

  /** True when `id` has a level set in this process. */
  public has(id: string): boolean {
    return this.levels.has(id);
  }

  public set(id: string, level: ThinkingChoice): void {
    this.levels.set(id, level);
  }
}
