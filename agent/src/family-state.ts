// The family state of each prompt (decisions CH5a, CH5c, CH10-4). The
// facade pulls it before each `session/prompt` into one `StateHolder`.
// The LiteLLM `auth` and the `ch` `auth` read the latest creds, so a
// rotation reaches a turn that runs. The agent function reads the config
// of its own session's last pull, so a pull for another session never
// changes a turn that runs.
//
//   backend.pull() ──► StatePull ──► holder.apply(state, session) ──► the next render
//
// One backend exists now: the file backend reads the mounts that the agent
// reads at start. The vmm backend of the cut (port 1100) reads
// `FamilyStateReply` with `read`, takes the same fault words, and gives
// its `FamilyConfig` to `liveConfig`. The holder never holds the text of a
// credential: a credential stays a `Secret`.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { CRED_RETRY_MS, readCreds } from "./creds.js";
import type { FamilyCreds } from "./creds.js";
import { bareAlias } from "./litellm.js";
import type { MountDirs } from "./mounts.js";
import { readRuntimeConfig, SANDBOX_TOOL_NAMES } from "./runtime-config.js";
import type { SandboxToolName, SystemPrompt } from "./runtime-config.js";
import { skillOf } from "./skills.js";
import type { LiveSkill } from "./skills.js";
import { validator } from "./wire-read.js";
import type { Wire } from "./wire-read.js";

const INSTRUCTIONS_FILE = "instructions.md";
const SKILLS_DIR = "skills";
const SKILL_FILE = "SKILL.md";

/**
 * The cap of a `state` reply text in bytes (decision CH23 rule 4). Over
 * it, the reply is `too_large` (decision CH31 rule 3). The vmm backend
 * gives it to `read` as `cap`.
 */
export const STATE_REPLY_CAP = 262_144;

/** The creds part of a reply with empty texts: the creds stay a `Secret` here. */
const NO_CRED_TEXT = { litellm_key: "", pep_token: "" };

export type LiveCreds = Pick<FamilyCreds, "litellmKey" | "pepToken">;

/**
 * What the agent function renders from. `modelAlias` is the bare alias,
 * with no `litellm/` prefix. Flue reads the model at the start of each
 * submission only (decisions CH5c, CH5c-2).
 */
export interface LiveConfig {
  readonly modelAlias: string;
  readonly instructions: string | null;
  readonly systemPrompt: SystemPrompt;
  readonly sandboxTools: readonly SandboxToolName[];
  readonly skills: readonly LiveSkill[];
}

export interface LiveState {
  readonly creds: LiveCreds;
  readonly config: LiveConfig;
}

/** The fault words of `FamilyStateReply`. The vmm backend gives the same ones. */
export type PullFault = "creds_missing" | "config_missing" | "not_valid" | "too_large";

/**
 * `stale` is null, or the fault of the first pull that kept the last good
 * config. The caller writes it once to stderr.
 */
export type StatePull =
  | { readonly ok: true; readonly state: LiveState; readonly stale: PullFault | null }
  | { readonly ok: false; readonly fault: PullFault };

export interface StateBackend {
  pull(): Promise<StatePull>;
}

/**
 * The live state of the agent: the latest creds and config, and the config
 * of each session's last pull. The facade ends no session, so an entry
 * stays for the life of the process, one reference for each session.
 */
export class StateHolder {
  private readonly bySession = new Map<string, LiveConfig>();

  public constructor(private latest: LiveState) {}

  /** The creds of the latest pull. */
  public get creds(): LiveCreds {
    return this.latest.creds;
  }

  /** The config of the latest pull. */
  public get config(): LiveConfig {
    return this.latest.config;
  }

  /** The config of session `id`. A bare render, or a session with no pull, gets the latest. */
  public configFor(id: string | null): LiveConfig {
    return (id === null ? undefined : this.bySession.get(id)) ?? this.latest.config;
  }

  /** The pull for session `id`: the latest state, and the config of that session's turn. */
  public apply(state: LiveState, id: string): void {
    this.latest = state;
    this.bySession.set(id, state.config);
  }
}

/**
 * The config of the wire as the agent renders it. A skill whose text is
 * the text of a skill in `previous` keeps that value, with its supporting
 * files. A skill that is not valid is left out.
 */
export function liveConfig(config: Wire<"FamilyConfig">, previous: readonly LiveSkill[]): LiveConfig {
  const instructions = config.instructions.trim();
  const same = (name: string, text: string): LiveSkill | undefined =>
    previous.find((skill) => skill.name === name && skill.text === text);
  const skills = Object.entries(config.skills).flatMap(([name, text]) => same(name, text) ?? skillOf(name, text) ?? []);

  return {
    modelAlias: bareAlias(config.model_alias),
    instructions: instructions.length > 0 ? instructions : null,
    systemPrompt: config.system_prompt,
    sandboxTools: SANDBOX_TOOL_NAMES.filter((name) => config.sandbox_tools[name]),
    skills,
  };
}

/** The text of a file, or undefined when it does not read. */
function textAt(path: string): string | undefined {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return undefined;
  }
}

/**
 * Each `<name>/SKILL.md` under `dir`, by name in ascending order.
 * Undefined: the folder or a listed skill does not read.
 */
function skillTexts(dir: string): Record<string, string> | undefined {
  const texts: Record<string, string> = {};
  try {
    const names = readdirSync(dir, { withFileTypes: true }).filter((entry) => entry.isDirectory());
    for (const name of names.map((entry) => entry.name).sort()) {
      const text = textAt(join(dir, name, SKILL_FILE));
      if (text === undefined) {
        return undefined;
      }

      texts[name] = text;
    }
  } catch {
    return undefined;
  }

  return texts;
}

type ConfigRead = { readonly ok: true; readonly config: LiveConfig } | { readonly ok: false; readonly fault: PullFault };

/**
 * `config/` as the wire type `FamilyConfig`. The folder must list, so a
 * dead mount fails here. The one listing says which files are present. A
 * listed file that does not read, an ENOENT too, is a folder that
 * caregiver removes during the read, so the config is not valid. A half
 * deleted folder never becomes the last good config.
 */
function readConfigDir(dir: string, previous: readonly LiveSkill[]): ConfigRead {
  let listed: ReadonlySet<string>;
  try {
    listed = new Set(readdirSync(dir));
  } catch (error) {
    const missing = (error as NodeJS.ErrnoException).code === "ENOENT";

    return { ok: false, fault: missing ? "config_missing" : "not_valid" };
  }

  // A `runtime.json` that does not read gives no `model_alias`, so the check fails.
  const runtime = readRuntimeConfig(dir);
  if (!runtime.ok) {
    return { ok: false, fault: "not_valid" };
  }

  const { modelAlias, sandboxTools, systemPrompt } = runtime.config;
  const config = {
    instructions: listed.has(INSTRUCTIONS_FILE) ? textAt(join(dir, INSTRUCTIONS_FILE)) : "",
    model_alias: modelAlias,
    sandbox_tools: Object.fromEntries(SANDBOX_TOOL_NAMES.map((name) => [name, sandboxTools.includes(name)])),
    skills: listed.has(SKILLS_DIR) ? skillTexts(join(dir, SKILLS_DIR)) : {},
    system_prompt: systemPrompt,
  };

  if (!validator("FamilyConfig").Check(config)) {
    return { ok: false, fault: "not_valid" };
  }

  // CONTRACT-QUESTION: the file backend counts the reply with empty creds
  // texts, because it never holds their text. The key and the token add
  // their own bytes. The vmm counts them.
  const reply = JSON.stringify({ state: { config, creds: NO_CRED_TEXT } });
  if (Buffer.byteLength(reply, "utf8") > STATE_REPLY_CAP) {
    return { ok: false, fault: "too_large" };
  }

  return { ok: true, config: liveConfig(config, previous) };
}

/**
 * `pull`, one call at a time, in the order of the calls. A call starts
 * when the call before it has settled. So the caller applies the pulls in
 * the order they started, and an older creds read never lands after a
 * newer one. Each backend wraps its pull in it.
 */
export function inOrder(pull: () => Promise<StatePull>): () => Promise<StatePull> {
  let last: Promise<unknown> = Promise.resolve();

  return () => {
    const next = last.then(pull);
    last = next.catch(() => undefined);

    return next;
  };
}

/**
 * The file backend: `creds.json` on the creds mount, `config/` on the
 * config mount. `lastGood` is the config of the start.
 *
 * CONTRACT-QUESTION: on a config read failure the file backend keeps the
 * last good config instead of failing the prompt (CH23 rule 2), because
 * the spike (CH52 rule 1) shows a dead sbx mount after each config edit.
 * sbx bind-mounts `config/` itself. After caregiver swaps the folder, each
 * read in the VM gives EACCES until the sandbox is made again. A failed
 * prompt each time would turn a stale config into an outage. A creds
 * failure still fails the prompt: a file replaced in the creds mount is
 * seen at once.
 */
export function fileBackend(mounts: MountDirs, lastGood: LiveConfig, retryMs: number = CRED_RETRY_MS): StateBackend {
  let kept = lastGood;
  let failing = false;

  return {
    pull: inOrder(async () => {
      // `creds_missing` wins when both are absent (decision CH31 rule 4).
      const creds = await readCreds(mounts.creds, retryMs);
      if (!creds.ok) {
        return { ok: false, fault: creds.field === null ? "creds_missing" : "not_valid" };
      }

      const config = readConfigDir(mounts.config, kept.skills);
      const stale = config.ok || failing ? null : config.fault;
      failing = !config.ok;
      kept = config.ok ? config.config : kept;

      return { ok: true, state: { creds: creds.creds, config: kept }, stale };
    }),
  };
}
