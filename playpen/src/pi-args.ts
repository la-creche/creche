// The ONE place a pi command line is built, for both programs in this bundle.
//
//   playpen  ──► PiMode.Rpc          `pi --mode rpc …`, one per session
//   agent-pi-launch ──► PiMode.Interactive   the same line, minus `--mode rpc`
//
// The TUI door attaches a terminal to a session the playpen also
// serves, on the same session store. Two commands built in two places would
// drift — a flag added here, a skill directory missed there — and the terminal
// would quietly run with a different reach from the chat. So the mode is the
// only difference, and `test/playpen-launch.test.ts` asserts that by
// subtracting the two flags from one argv and comparing the rest.
//
// Contract 03 §7.1, §7.3 and §4.1.1 fix every flag below.

import { existsSync } from "node:fs";

import { CODEMODE_EXTENSION } from "./constants.js";
import { piModelId } from "./models-json.js";
import type { RuntimeConfig } from "./runtime-config.js";
import { grantsCodemode, piToolArgs, SystemPrompt } from "./runtime-config.js";

/** How pi is driven. The playpen speaks rpc; a person drives the TUI. */
export enum PiMode {
  /** Contract 03 §1 rule 3. One JSON protocol over the child's stdio. */
  Rpc = "rpc",
  /** pi's own interactive UI, in the foreground of an `sbx exec -it`. */
  Interactive = "interactive",
}

/**
 * pi's own start flags, fixed for every family and every mode. Each closes a
 * hole rather than tuning behaviour:
 *
 * - `--no-approve`: decide project trust here, never by prompting. In rpc mode
 *   nobody can answer a dialog, so a workspace carrying `.pi/` would otherwise
 *   block the session forever.
 * - `--no-extensions`: discovery off. Without it pi also loads
 *   `$PI_CODING_AGENT_DIR/extensions/` and whatever that directory's
 *   `settings.json` names — and the session store is agent-writable and
 *   survives the sandbox, so a session could leave code that runs at every
 *   later session start, before project trust resolves. Verified 2026-09-08:
 *   a probe dropped there ran on the next session, and this flag stopped it.
 *   Since pi 0.99 it also keeps the built-in `mcp`, `codemode`, `tool-search`
 *   and `llama.cpp` extensions out, so each one is loaded by name or not at all.
 * - `--offline`: no model catalog refresh and no package fetch. A sandbox has
 *   no route to pi.dev, so each attempt could only fail or wait.
 */
const PI_FIXED_ARGS: readonly string[] = ["--no-approve", "--no-extensions", "--offline"];

/** Contract 03 §1 rule 3. The two argv entries `PiMode.Interactive` drops. */
export const RPC_MODE_ARGS: readonly string[] = ["--mode", "rpc"];

export interface PiArgsSpec {
  readonly session: string;
  /** The turn's own model. The family's alias leads when this is absent. */
  readonly model?: string;
  readonly config: RuntimeConfig;
  /** The PEP bridge bundle, or null when the image carries none. */
  readonly bridgePath: string | null;
}

export interface PiStart {
  readonly args: readonly string[];
  /** The pi model ids this process may address. Entry 0 is the one it runs. */
  readonly models: readonly string[];
}

/**
 * The models this process may address: the turn's own first, then the family's
 * alias. Entry 0 is always the model pi will run with, and both the reuse test
 * and `--model` depend on that order.
 *
 * Listing an alias grants nothing. The family key carries exactly one alias
 * (contract 01 §3.2), so LiteLLM refuses everything else and the perimeter
 * stays outside the sandbox (invariant 12).
 */
export function piModels(spec: PiArgsSpec): readonly string[] {
  const ids: string[] = [];
  if (spec.model !== undefined) {
    ids.push(piModelId(spec.model));
  }
  if (spec.config.modelAlias !== null) {
    ids.push(piModelId(spec.config.modelAlias));
  }

  return [...new Set(ids)];
}

/** The bridge bundle when the image carries it, or null. §7.3 rule 5. */
export function bridgeAt(path: string): string | null {
  return existsSync(path) ? path : null;
}

/**
 * pi's argv and the models behind it.
 *
 * It carries no credential: §12 rule 6 keeps those in the child's environment,
 * because argv is world-readable through /proc/<pid>/cmdline for the whole life
 * of the process.
 *
 * `instructions.md` travels as a PATH, not as text. pi reads the file when the
 * argument is a path, which keeps a 64 KiB document off argv and lets
 * `managerd` rewrite the file without the playpen re-reading anything.
 *
 * Contract 03 §7.1 states the path form. It makes a rewrite reach a held
 * process on its NEXT turn rather than its next process, which is stronger
 * than contract 01 §6.1 rule 4 promises.
 */
export function buildPiStart(spec: PiArgsSpec, mode: PiMode): PiStart {
  const models = piModels(spec);
  const args = [
    ...(mode === PiMode.Rpc ? RPC_MODE_ARGS : []),
    "--session-id",
    spec.session,
    ...PI_FIXED_ARGS,
    ...bridgeArgs(spec.bridgePath),
    ...codemodeArgs(spec.config),
    ...piToolArgs(spec.config),
  ];

  const chosen = models[0];
  if (chosen !== undefined) {
    args.push("--model", chosen, "--models", models.join(","));
  }

  // The read-only mount is the only skill source: `--no-skills` turns
  // discovery off and `--skill` adds back exactly what the family granted.
  if (spec.config.skillsPath !== null) {
    args.push("--no-skills", "--skill", spec.config.skillsPath);
  }

  // `replace` drops pi's own prompt, which opens "You are an expert coding
  // assistant" and adds coding rules and a pi docs section. A chat family is
  // better served by its instructions alone (contract 01 §3.8).
  if (spec.config.instructionsPath !== null) {
    const flag =
      spec.config.systemPrompt === SystemPrompt.Replace ? "--system-prompt" : "--append-system-prompt";
    args.push(flag, spec.config.instructionsPath);
  }

  return { args, models };
}

/**
 * pi's codemode, by name, for a family that grants it. `--no-extensions`
 * keeps every built-in extension out otherwise, so no family gets it by
 * default, and none ever gets `builtin:mcp`: its `mcp.json` would live in the
 * agent-writable session store, and a direct MCP connection skips the PEP.
 */
function codemodeArgs(config: RuntimeConfig): readonly string[] {
  return grantsCodemode(config) ? ["--extension", CODEMODE_EXTENSION] : [];
}

/**
 * The PEP bridge, by explicit path, while discovery stays off.
 *
 * `--no-extensions` is in `PI_FIXED_ARGS` on purpose: discovery would also load
 * `$PI_CODING_AGENT_DIR/extensions/`, and the session store is agent-writable
 * and outlives the sandbox, so a session could leave code there that runs at
 * every later session start. One named path is the whole difference between
 * loading a bridge and loading whatever a session left behind (pi 0.99.1
 * `docs/cli.md`: explicit `-e` paths still load under `--no-extensions`).
 *
 * A missing bundle means a sandbox image without the bridge. The session still
 * opens and still converses; it simply has no action tools.
 */
function bridgeArgs(path: string | null): readonly string[] {
  return path === null ? [] : ["--extension", path];
}
