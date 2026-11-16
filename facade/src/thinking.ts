// The thinking level as an ACP session config option. The client's picker
// shows the one `select` option of category `mode`, and a choice comes back
// as `session/set_config_option`.
//
//   vectors new_session.configOptions ──► the option, with the current level
//   session/set_config_option ──► the level ──► ThinkingLevels of the agent
//
// The ACP door answers `session/new` with the same list and the default
// level, because it answers with no VM boot. A level changes the model
// call only. It widens no tool and no grant.

import { isThinkingChoice } from "agent/agent";
import type { ThinkingChoice } from "agent/agent";

import type { Outcome, SessionConfigOption, SetSessionConfigOptionRequest } from "./acp.js";
import { NEW_SESSION_CONFIG_OPTIONS } from "./vectors.js";

/** The one option, as the vector file gives it. */
const [THINKING_OPTION] = NEW_SESSION_CONFIG_OPTIONS;

/** The id of the thinking option: the id that the client's picker writes. */
export const THINKING_CONFIG_ID = THINKING_OPTION.id;

/** The config options of a session whose level is `level`. */
export function configOptionsOf(level: ThinkingChoice): SessionConfigOption[] {
  return [{ ...THINKING_OPTION, currentValue: level, options: THINKING_OPTION.options.map((choice) => ({ ...choice })) }];
}

function invalid(detail: string): Outcome<ThinkingChoice> {
  return { ok: false, refusal: { kind: "invalid_params", detail } };
}

/** The level that a `session/set_config_option` asks for, or its refusal. */
export function chosenLevel(params: SetSessionConfigOptionRequest): Outcome<ThinkingChoice> {
  if (params.configId !== THINKING_CONFIG_ID) {
    return invalid("the facade has no such config option");
  }

  if (typeof params.value !== "string") {
    return invalid("the thinking option takes a value id");
  }

  const { value } = params;
  const listed = THINKING_OPTION.options.some((choice) => choice.value === value);
  if (!listed || !isThinkingChoice(value)) {
    return invalid("the thinking option has no such value");
  }

  return { ok: true, value };
}
