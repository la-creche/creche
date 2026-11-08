// The family state of each prompt (decisions CH5a, CH5c, CH10-4). The
// facade pulls it before each `session/prompt` into one `StateHolder`.
// The LiteLLM `auth` and the `ch` `auth` read the latest creds, so a
// rotation reaches a turn that runs. The agent function reads the config
// of its own session's last pull, so a pull for another session never
// changes a turn that runs.
//
//   backend.pull() ──► StatePull ──► holder.apply(state, session) ──► the next render
//
// Two backends exist. On sbx the file backend reads the mounts that the
// agent reads at start. Under CH the vmm backend sends one
// `FamilyStatePull` line on the family-state socket of guest init, which
// guest init splices to host port 1100 (decisions CH5a, CH62 rule 13). It
// reads the one `FamilyStateReply` line with `read`, takes the same fault
// words, and gives its `FamilyConfig` to `liveConfig`. The holder never
// holds the text of a credential: a credential stays a `Secret`.

import { readdirSync, readFileSync } from "node:fs";
import { createConnection } from "node:net";
import { join } from "node:path";

import { CRED_RETRY_MS, readCreds } from "./creds.js";
import type { FamilyCreds } from "./creds.js";
import { bareAlias } from "./litellm.js";
import type { MountDirs } from "./mounts.js";
import { readRuntimeConfig, SANDBOX_TOOL_NAMES } from "./runtime-config.js";
import type { SandboxToolName, SystemPrompt } from "./runtime-config.js";
import { Secret } from "./secret.js";
import { skillOf } from "./skills.js";
import type { LiveSkill } from "./skills.js";
import { read, validator } from "./wire-read.js";
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

  // The wire pattern takes `litellm/`, which names no model. The start
  // refuses it (`model_alias_unset`), so a pull keeps the last good config.
  if (!validator("FamilyConfig").Check(config) || bareAlias(modelAlias ?? "").length === 0) {
    return { ok: false, fault: "not_valid" };
  }

  // The file backend counts the reply with empty creds texts, because it
  // never holds their text. The key and the token add their own bytes,
  // which the vmm counts.
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
 * On a config read failure the file backend keeps the last good config
 * and logs one stderr line (decision CH53 rule 2). The spike of CH52
 * rule 1 shows a dead sbx mount after each config edit. sbx bind-mounts `config/` itself. After caregiver swaps the folder, each
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

/** One line of free text for stderr. */
export type Say = (line: string) => void;

/** The one pull line: a `FamilyStatePull` in the compact form, then one LF. */
const PULL: Wire<"FamilyStatePull"> = {};
export const PULL_LINE = `${JSON.stringify(PULL)}\n`;

const LF = 0x0a;

/**
 * How long one pull waits for its reply line, in milliseconds. The vmm
 * holds a `state` reply for at most 10 s (decision CH31 rule 1), and its
 * connect to host port 1100 has a 5 s timeout (decision CH62 rule 14).
 * The last 5 s is for the read. A pull past this wait closes its link
 * (decision CH81 rule 3).
 */
export const PULL_WAIT_MS = 20_000;

/** The reply line of one pull, or the fault of a link that gave none. */
type LineRead =
  | { readonly ok: true; readonly line: Buffer }
  | { readonly ok: false; readonly fault: PullFault; readonly detail: string };

/**
 * One link, one pull, one reply line (decision CH62 rule 14: each guest
 * connection dials fresh, and nothing retries). The read keeps at most
 * `STATE_REPLY_CAP` bytes and the LF. More bytes with no LF are
 * `too_large`, and the rest is not read (`wire/AGENTS.md`, "The vsock JSON
 * links", rule 2). A link that ends with no line, or that passes
 * `waitMs`, is `not_valid`: the pull got no state (decision CH81 rule 2).
 */
function pullLine(path: string, waitMs: number): Promise<LineRead> {
  return new Promise((resolve) => {
    const socket = createConnection(path);
    const parts: Buffer[] = [];
    let size = 0;
    let fault = "";
    let settled = false;
    const done = (result: LineRead): void => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(result);
    };
    const lost = (why: string): void => done({ ok: false, fault: "not_valid", detail: why });
    const timer = setTimeout(() => lost(`no reply in ${String(waitMs)} ms`), waitMs);

    socket.on("connect", () => socket.write(PULL_LINE));
    socket.on("data", (chunk: Buffer) => {
      const end = chunk.indexOf(LF);
      const kept = end === -1 ? chunk : chunk.subarray(0, end);
      parts.push(kept);
      size += kept.length;
      if (size > STATE_REPLY_CAP) {
        done({ ok: false, fault: "too_large", detail: `a reply line over ${String(STATE_REPLY_CAP)} bytes` });
      } else if (end !== -1) {
        done({ ok: true, line: Buffer.concat(parts) });
      }
    });
    socket.on("error", (error) => {
      fault = `: ${error.message}`;
    });
    socket.on("close", () => lost(`the link closed with no reply${fault}`));
  });
}

/**
 * The state of one reply line. `previous` gives the skills that keep
 * their value (`liveConfig`). The wire pattern of `ModelAlias` takes
 * `litellm/`, which names no model, so a reply with it is `not_valid`,
 * as in the file backend.
 */
function stateOf(line: Buffer, previous: readonly LiveSkill[]): StatePull {
  const reply = read("FamilyStateReply", line, { cap: STATE_REPLY_CAP });
  if (!reply.ok) {
    return { ok: false, fault: reply.rule === "too_large" ? "too_large" : "not_valid" };
  }

  const value = reply.value;
  if (!("state" in value)) {
    return { ok: false, fault: faultOf(value) };
  }

  const { config, creds } = value.state;
  if (bareAlias(config.model_alias).length === 0) {
    return { ok: false, fault: "not_valid" };
  }

  return {
    ok: true,
    state: {
      creds: { litellmKey: Secret.of(creds.litellm_key), pepToken: Secret.of(creds.pep_token) },
      config: liveConfig(config, previous),
    },
    stale: null,
  };
}

/** The fault word of a typed fault reply: its one key. The map is total. */
function faultOf(reply: Exclude<Wire<"FamilyStateReply">, { readonly state: unknown }>): PullFault {
  if ("creds_missing" in reply) {
    return "creds_missing";
  }

  if ("config_missing" in reply) {
    return "config_missing";
  }

  if ("not_valid" in reply) {
    return "not_valid";
  }

  return "too_large";
}

/**
 * One pull of the family state on the socket `path`. A link that gives no
 * line writes its cause to `say` and is `not_valid` (decision CH81 rule 2).
 */
export async function pullState(
  path: string,
  previous: readonly LiveSkill[],
  say: Say,
  waitMs: number = PULL_WAIT_MS,
): Promise<StatePull> {
  const line = await pullLine(path, waitMs);
  if (!line.ok) {
    say(`the family-state pull got no state: ${line.detail}`);
    return { ok: false, fault: line.fault };
  }

  return stateOf(line.line, previous);
}

/**
 * The vmm backend: one pull on the family-state socket `path` for each
 * call (decisions CH5a, CH62 rule 13). `start` is the config of the start
 * pull. A typed fault fails the prompt, and nothing keeps a last good
 * config: the vmm keeps none either (decisions CH23 rule 2, CH53 rule 2).
 * So `stale` is always null. A skill whose text does not change keeps the
 * value of the last good pull.
 */
export function vmmBackend(path: string, start: LiveConfig, say: Say, waitMs: number = PULL_WAIT_MS): StateBackend {
  let skills = start.skills;

  return {
    pull: inOrder(async () => {
      const pulled = await pullState(path, skills, say, waitMs);
      skills = pulled.ok ? pulled.state.config.skills : skills;

      return pulled;
    }),
  };
}
