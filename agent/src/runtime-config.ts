// A port of the reader of `playpen/src/runtime-config.ts`. `caregiver`
// writes the family config mount:
//
//   <config dir>/runtime.json     shell, sandbox_tools, model_alias, system_prompt
//   <config dir>/instructions.md  the family's own instructions
//   <config dir>/skills/          the granted skills
//
// As in the playpen, a missing or malformed `runtime.json` gives the safe
// defaults. No value of this file is part of the perimeter (contract 01
// §3.8 rule 3). A wrong value costs behavior, not reach.

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Contract 01 §3.8. The names a grant can hold. `bash` is never one: `shell`
 * is the only door to it. Row 6 of the Phase 2 plan maps them to Flue's tools.
 */
const SANDBOX_TOOLS = new Set(["read", "write", "edit", "grep", "find", "ls", "codemode"]);

/** Contract 01 §3.8's default when the file names none. */
const DEFAULT_SANDBOX_TOOLS: readonly string[] = ["read", "grep", "find", "ls"];

/** How `instructions.md` meets the agent's own prompt. */
export type SystemPrompt = "append" | "replace";

export interface RuntimeConfig {
  readonly shell: boolean;
  readonly sandboxTools: readonly string[];
  readonly modelAlias: string | null;
  readonly systemPrompt: SystemPrompt;
  readonly instructionsPath: string | null;
  readonly skillsPath: string | null;
}

function pathIfPresent(dir: string, name: string): string | null {
  const path = join(dir, name);

  return existsSync(path) ? path : null;
}

/** Deny by default: an unknown name is dropped, not passed on. */
function toolList(raw: unknown): readonly string[] {
  if (!Array.isArray(raw)) {
    return DEFAULT_SANDBOX_TOOLS;
  }

  return raw.filter((name): name is string => typeof name === "string" && SANDBOX_TOOLS.has(name));
}

export function readRuntimeConfig(dir: string): RuntimeConfig {
  const base: RuntimeConfig = {
    shell: false,
    sandboxTools: DEFAULT_SANDBOX_TOOLS,
    modelAlias: null,
    systemPrompt: "append",
    instructionsPath: pathIfPresent(dir, "instructions.md"),
    skillsPath: pathIfPresent(dir, "skills"),
  };

  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(join(dir, "runtime.json"), "utf8"));
  } catch {
    return base;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return base;
  }

  const raw = parsed as Record<string, unknown>;
  const alias = raw["model_alias"];

  return {
    ...base,
    shell: raw["shell"] === true,
    sandboxTools: toolList(raw["sandbox_tools"]),
    modelAlias: typeof alias === "string" && alias.length > 0 ? alias : null,
    systemPrompt: raw["system_prompt"] === "replace" ? "replace" : "append",
  };
}
