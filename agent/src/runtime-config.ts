// A port of the reader of `playpen/src/runtime-config.ts`. `caregiver`
// writes the family config mount:
//
//   <config dir>/runtime.json     shell, sandbox_tools, model_alias, system_prompt
//   <config dir>/instructions.md  the family's own instructions
//   <config dir>/skills/          the granted skills
//
// As in the playpen, a missing or malformed `runtime.json` gives the safe
// defaults. Each default grants nothing. One field is part of the
// perimeter: `sandbox_tools` grants Flue's built-in tools on the host,
// `bash` included. So a present `sandbox_tools` that is not a list of known
// names is a typed refusal, never a guess.
//
// CONTRACT-QUESTION: contract 01 §3.8 names `read`, `write`, `edit`, `grep`,
// `find`, `ls` and `codemode`, defaults to `read`, `grep`, `find`, `ls`, and
// makes `shell` the only door to `bash`. The owner's decision for row 6 of
// the Phase 2 plan: the names are Flue's six, the default is empty, and the
// edge validates the list. This reader takes the owner's reading. A file
// with a name of the contract refuses. `shell` is read and grants nothing.
// The cost: `caregiver` writes the new names (Phase 3).

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

/** The six built-in tools of Flue's `local()` sandbox, by Flue's names. */
export const SANDBOX_TOOL_NAMES = ["read", "write", "edit", "bash", "grep", "glob"] as const;

export type SandboxToolName = (typeof SANDBOX_TOOL_NAMES)[number];

/** How `instructions.md` meets the agent's own prompt. */
export type SystemPrompt = "append" | "replace";

export interface RuntimeConfig {
  readonly shell: boolean;
  /** The granted built-in tools, in grant order, each one time. */
  readonly sandboxTools: readonly SandboxToolName[];
  readonly modelAlias: string | null;
  readonly systemPrompt: SystemPrompt;
  readonly instructionsPath: string | null;
  readonly skillsPath: string | null;
}

/** The one field whose fault refuses. */
export type RuntimeField = "sandbox_tools";

export type RuntimeRead =
  | { readonly ok: true; readonly config: RuntimeConfig }
  | { readonly ok: false; readonly field: RuntimeField };

function pathIfPresent(dir: string, name: string): string | null {
  const path = join(dir, name);

  return existsSync(path) ? path : null;
}

function isToolName(name: unknown): name is SandboxToolName {
  return SANDBOX_TOOL_NAMES.some((known) => known === name);
}

/** Absent grants nothing. Anything but a list of known names is null. */
function toolList(raw: unknown): readonly SandboxToolName[] | null {
  if (raw === undefined) {
    return [];
  }

  if (!Array.isArray(raw) || !raw.every(isToolName)) {
    return null;
  }

  return [...new Set(raw)];
}

export function readRuntimeConfig(dir: string): RuntimeRead {
  const base: RuntimeConfig = {
    shell: false,
    sandboxTools: [],
    modelAlias: null,
    systemPrompt: "append",
    instructionsPath: pathIfPresent(dir, "instructions.md"),
    skillsPath: pathIfPresent(dir, "skills"),
  };

  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(join(dir, "runtime.json"), "utf8"));
  } catch {
    return { ok: true, config: base };
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: true, config: base };
  }

  const raw = parsed as Record<string, unknown>;
  const sandboxTools = toolList(raw["sandbox_tools"]);
  if (sandboxTools === null) {
    return { ok: false, field: "sandbox_tools" };
  }

  const alias = raw["model_alias"];

  return {
    ok: true,
    config: {
      ...base,
      shell: raw["shell"] === true,
      sandboxTools,
      modelAlias: typeof alias === "string" && alias.length > 0 ? alias : null,
      systemPrompt: raw["system_prompt"] === "replace" ? "replace" : "append",
    },
  };
}
