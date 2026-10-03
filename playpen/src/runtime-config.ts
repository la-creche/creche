// Contract 01 §6.1 and contract 03 §7.1. The family config mount is one
// read-only directory `managerd` writes and one revision counter versions.
// The playpen reads it when it STARTS a pi process, never per turn, so a
// change reaches a session when its held-open process is next recycled.
//
//   <config dir>/runtime.json     shell, sandbox_tools, model_alias, system_prompt
//   <config dir>/instructions.md  the family's own instructions
//   <config dir>/skills/          the granted skills
//
// The directory arrives in `AGENT_FAMILY_CONFIG_DIR` (`mounts.ts`), because a
// mount's in-VM path is its host path and no fixed path exists to name here.
//
// None of `shell`, `sandbox_tools` and `system_prompt` is part of the
// perimeter (contract 01 §3.8 rule 3): the perimeter is mounts, egress, the
// PEP and the model key. A wrong value here costs behaviour, not reach.

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { CODEMODE_TOOL } from "./constants.js";

/**
 * Contract 01 §3.8. `bash` may never appear here: `shell` is the only door.
 * `codemode` is pi's built-in extension, granted by name like a built-in.
 */
const SANDBOX_TOOLS = new Set(["read", "write", "edit", "grep", "find", "ls", CODEMODE_TOOL]);

/** Contract 01 §3.8. How `instructions.md` meets pi's own system prompt. */
export enum SystemPrompt {
  /** After pi's own prompt, which opens "You are an expert coding assistant". */
  Append = "append",
  /** Instead of it. */
  Replace = "replace",
}

/** Contract 01 §3.8's default when the file names none. */
const DEFAULT_SANDBOX_TOOLS: readonly string[] = ["read", "grep", "find", "ls"];

/** pi's own name for the shell tool (pi 0.99.1). */
const SHELL_TOOL = "bash";

/**
 * pi's eight built-in tools, in pi's own order (0.99.1 `docs/cli.md`).
 * `powershell` is in no grant, so it is always denied.
 */
const PI_BUILTIN_TOOLS: readonly string[] = [
  "read",
  "bash",
  "powershell",
  "edit",
  "write",
  "grep",
  "find",
  "ls",
];

const RUNTIME_FILE = "runtime.json";
const INSTRUCTIONS_FILE = "instructions.md";
const SKILLS_DIR = "skills";

export interface RuntimeConfig {
  readonly shell: boolean;
  readonly sandboxTools: readonly string[];
  readonly modelAlias: string | null;
  readonly systemPrompt: SystemPrompt;
  /**
   * Present only when the mount names it. Contract 03 §3 makes `hello` the
   * only source of both numbers and has a playpen ignore them here, so
   * nothing reads these two fields.
   */
  readonly piIdleTtlS: number | null;
  readonly maxResidentProcesses: number | null;
  readonly instructionsPath: string | null;
  readonly skillsPath: string | null;
}

function toolList(raw: unknown): readonly string[] {
  if (!Array.isArray(raw)) {
    return DEFAULT_SANDBOX_TOOLS;
  }

  // Deny by default (invariant 11): an unknown name is dropped, not passed on.
  return raw.filter((name): name is string => typeof name === "string" && SANDBOX_TOOLS.has(name));
}

/**
 * `replace` only when the file says so. `managerd` writes no key for the
 * default, and appending keeps pi's own prompt, the reading that loses least
 * when a value is one this playpen does not know.
 */
function systemPromptOf(raw: unknown): SystemPrompt {
  return raw === SystemPrompt.Replace ? SystemPrompt.Replace : SystemPrompt.Append;
}

function positiveInt(raw: unknown): number | null {
  if (typeof raw !== "number" || !Number.isInteger(raw) || raw < 0) {
    return null;
  }

  return raw;
}

function pathIfPresent(dir: string, name: string): string | null {
  const path = join(dir, name);

  return existsSync(path) ? path : null;
}

/**
 * Reads the mount. A missing or malformed file yields the safe defaults rather
 * than an exception: the family config mount is outside this process's control
 * and a bad revision must not stop a session from opening (invariant 19).
 */
export function readRuntimeConfig(dir: string): RuntimeConfig {
  const base: RuntimeConfig = {
    shell: false,
    sandboxTools: DEFAULT_SANDBOX_TOOLS,
    modelAlias: null,
    systemPrompt: SystemPrompt.Append,
    piIdleTtlS: null,
    maxResidentProcesses: null,
    instructionsPath: pathIfPresent(dir, INSTRUCTIONS_FILE),
    skillsPath: pathIfPresent(dir, SKILLS_DIR),
  };

  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(join(dir, RUNTIME_FILE), "utf8"));
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
    systemPrompt: systemPromptOf(raw["system_prompt"]),
    piIdleTtlS: positiveInt(raw["pi_idle_ttl_s"]),
    maxResidentProcesses: positiveInt(raw["max_resident_processes"]),
  };
}

/**
 * The built-ins this family may use, in pi's own order.
 *
 * `shell` is folded in here because pi's name for it is just another built-in
 * (`bash`), and contract 01 §3.8 rule 1 keeps that name out of
 * `sandbox_tools`. One list then answers both halves below.
 */
export function grantedBuiltins(config: RuntimeConfig): readonly string[] {
  const granted = new Set(config.sandboxTools);
  if (config.shell) {
    granted.add(SHELL_TOOL);
  }

  return PI_BUILTIN_TOOLS.filter((name) => granted.has(name));
}

/**
 * Whether this family runs codemode scripts. `--no-extensions` keeps pi's
 * built-in extension out, so a grant is two instructions again: load it by
 * name (`pi-args.ts`) and activate it in `defaultTools` (`pi-settings.ts`).
 */
export function grantsCodemode(config: RuntimeConfig): boolean {
  return config.sandboxTools.includes(CODEMODE_TOOL);
}

/**
 * pi's tool flags for this family: the DENY half of contract 01 §3.8.
 *
 * `sandbox_tools` governs pi's eight built-ins and says nothing about PEP
 * tools. The PEP grant governs those and the PEP enforces it, outside the
 * sandbox (invariant 12). `--tools` and `--no-tools` stay banned: both are
 * allowlists over built-in AND extension tools alike (pi 0.99.1
 * `docs/cli.md`), and the bridge's tools do not exist until it has read the
 * manifest, so either would silently drop every granted PEP tool.
 *
 * **A deny list alone is not enough.** Excluding removes and enables nothing.
 * pi activates four of its eight built-ins at startup — `read`, `bash`,
 * `edit`, `write` (0.99.1 `dist/core/settings-manager.js:35`) — so a family with
 * `shell: false` that is granted `grep`, `find` or `ls` would run without
 * them, and one with `shell: true` would reach the same ground only through
 * `bash`. `vault-oracle`, whose whole job is retrieval, would spend its job
 * timeout looking for a search tool it was granted.
 *
 * So `sandbox_tools` is TWO instructions to pi, not one:
 *
 *   enable   `defaultTools` in settings.json   `pi-settings.ts`
 *   deny     `--exclude-tools` on argv         here
 *
 * The deny half stays. It is what keeps an ungranted built-in out of pi's tool
 * registry altogether, rather than merely inactive, so nothing later in the
 * session can switch one on.
 */
export function piToolArgs(config: RuntimeConfig): readonly string[] {
  const granted = new Set(grantedBuiltins(config));
  const denied = PI_BUILTIN_TOOLS.filter((name) => !granted.has(name));
  if (denied.length === 0) {
    return [];
  }

  return ["--exclude-tools", denied.join(",")];
}
