// Contract 03 §6. One pi process per running session, held open after it
// settles, reaped on idle, on the cap, or on a stale credential epoch.
//
// The hold rule is what makes chat feel instant. Probe 0a measured a second
// prompt on a held-open process at 4 ms against 1195 ms for a fresh one (A3),
// and 155 to 170 MB resident per idle process (A7), which is where the cap
// comes from. Reaping is safe: pi appends each entry as it happens, so the
// session state is already on disk and the only cost is a cold start.
//
// §6 rule 9's pre-start has a message: `open_session` (§4.7). There is no
// spare process: a pi process is bound to its session at start —
// `--session-id`, `PI_CODING_AGENT_DIR` and a cwd are all per session (§7) —
// so a spare could never be handed on.

import { CredReader } from "./creds.js";
import { MAX_LOG_BYTES } from "./constants.js";
import { piModelId, writeModelsJson } from "./models-json.js";
import { bridgeAt, buildPiStart, PiMode } from "./pi-args.js";
import type { PiArgsSpec, PiStart } from "./pi-args.js";
import { writePiSettings } from "./pi-settings.js";
import type { PiLauncher } from "./pi-process.js";
import type {
  GetEntriesMessage,
  OpenSessionMessage,
  ProcessSpec,
  PromptMessage,
  SessionOpenReason,
  StartTurnMessage,
  PlaypenMessage,
  TurnFailReason,
  TurnRequest,
} from "./protocol.js";
import { readRuntimeConfig } from "./runtime-config.js";
import type { RuntimeConfig } from "./runtime-config.js";
import { buildTurnEnv } from "./env.js";
import type { ProcessRecords } from "./process-record.js";
import { SandboxSession } from "./session.js";
import type { EntryRead, SessionSpec } from "./session.js";
import type { ToolStateFile } from "./tool-state.js";
import type { TurnFile } from "./turn-file.js";
import { CodeSandboxLinks, WorkspaceLinkError } from "./workspace.js";
import { CODE_SANDBOX_KIND } from "./protocol.js";

/** How often idle processes are checked against `pi_idle_ttl_s`. */
const REAP_SWEEP_MS = 1000;

/**
 * What a turn with no PEP tools says to its reader, once, at the end of it.
 *
 * Without it a PEP outage says nothing anywhere: sessions born inside it
 * answer "I have no such tool" and sessions born before it watch every call
 * fail. The sentence is for a person, so it
 * names the effect before the cause and carries no code.
 *
 * Two causes, two sentences. `rev: null` is the outage: no manifest reached
 * this process. A revision with no tools in it is a family that was granted
 * none, and sending that reader to look at the policy service would waste
 * their time on a service that is well.
 */
const TOOLLESS_NO_ANSWER = "This turn ran without tools: the policy service did not answer.";
const TOOLLESS_NO_GRANT = "This turn ran without tools: this agent is granted none.";

/**
 * What a tool list that moved UNDER a running session says, once.
 *
 * The bridge polls the PEP for the life of its process (contract 04 §4.2), so
 * a tool the operator grants reaches the chat they are already in. The model simply
 * starts having it, and without this line the reader cannot tell a new
 * capability from a model that changed its mind.
 *
 * The count is the whole of it. The playpen knows how many tools the
 * bridge states and not which, and inventing names it cannot see would be
 * the guess `tool-state.ts` refuses everywhere else.
 */
function toolsMovedLine(count: number): string {
  return `The tool list changed while this session was open: ${count} tools are available now.`;
}

/** What a pi process start needs. `start_turn` and `open_session` both fit. */
type SpawnOutcome =
  | { readonly ok: true; readonly session: SandboxSession }
  | { readonly ok: false; readonly reason: TurnFailReason; readonly detail: string };

/**
 * Why a held-open process cannot serve the next turn (§6 rule 5).
 *
 * A pi process is born with its environment, its working directory, its model
 * and the family config it read at start (§7, §7.1), and it can change none of
 * them afterwards. Each member below names one of those four moving, and every
 * one of them ends the same way: close the process at the next turn boundary,
 * start a fresh one. One code path, so a rotated credential and a rewritten
 * config mount cannot drift apart.
 */
enum Staleness {
  /** Nothing moved. The process serves the turn. */
  None = "none",
  /** `managerd` rewrote the family config mount and raised `config_rev`. */
  Config = "config",
  /** `managerd` rotated the family key and token (§12 rule 9). */
  Credentials = "credentials",
  /** The cwd, the session store, the model, or the process itself. */
  Binding = "binding",
}

export interface PoolSettings {
  readonly family: string;
  readonly sandbox: string;
  readonly coalesceMs: number;
  readonly idleTtlS: number;
  readonly maxResident: number;
}

export interface PoolDeps {
  readonly launcher: PiLauncher;
  readonly creds: CredReader;
  readonly configDir: string;
  readonly turnFile: TurnFile;
  /** §7.5's records, the fence `agent-pi-launch` reads inside the VM. */
  readonly processes: ProcessRecords;
  /** What the bridge says about its tools. See `tool-state.ts`. */
  readonly toolState: ToolStateFile;
  /** The read-only path the image installs the PEP bridge at. */
  readonly bridgePath: string;
  readonly codeSandboxRoot?: string;
  readonly codeSandboxLinkPrefix?: string;
  readonly emit: (message: PlaypenMessage) => void;
}

export class SessionPool {
  private readonly sessions = new Map<string, SandboxSession>();
  private readonly starting = new Map<string, Promise<SpawnOutcome>>();
  /**
   * The grants revision each session's tools last came from, so a moved one
   * is a change the reader can be told about exactly once.
   *
   * It is dropped when the process exits (`forget`), so the first turn on a
   * replacement process is a start-up snapshot and not a change. That matters
   * for thin and autonomous families: `pi_idle_ttl_s` 0 reaps after every
   * turn, and without the drop every second turn would claim a change.
   */
  private readonly toolRevs = new Map<string, string>();

  private readonly links: CodeSandboxLinks;
  private sweep: NodeJS.Timeout | null = null;
  private throttled: SandboxSession | null = null;

  public constructor(
    private readonly settings: PoolSettings,
    private readonly deps: PoolDeps,
  ) {
    this.links = new CodeSandboxLinks(deps.codeSandboxRoot, deps.codeSandboxLinkPrefix);
  }

  public get residentCount(): number {
    return this.sessions.size;
  }

  public get residentIds(): readonly string[] {
    return [...this.sessions.keys()];
  }

  public start(): void {
    this.sweep ??= setInterval(() => this.reapIdle(), REAP_SWEEP_MS);
  }

  public stopSweep(): void {
    if (this.sweep === null) {
      return;
    }

    clearInterval(this.sweep);
    this.sweep = null;
  }

  public peakRssMb(): number | null {
    let total: number | null = null;
    for (const session of this.sessions.values()) {
      const mb = session.sampleRss();
      if (mb !== null) {
        total = (total ?? 0) + mb;
      }
    }

    return total;
  }

  /** Contract 03 §4.1. Start a process when none is resident, then prompt. */
  public async startTurn(message: StartTurnMessage): Promise<void> {
    if (this.busyWith(message)) {
      return;
    }

    await this.settleStart(message.session);

    const existing = this.sessions.get(message.session);
    const stale = existing === undefined ? Staleness.Binding : this.staleness(existing, message);
    if (existing !== undefined && stale === Staleness.None) {
      await this.runTurn(existing, message);
      return;
    }

    if (existing !== undefined) {
      await this.recycle(existing, stale);
    }

    const opened = await this.open(message);
    if (!opened.ok) {
      this.failTurn(message, opened.reason, opened.detail);
      return;
    }

    await this.runTurn(opened.session, message);
  }

  /**
   * Contract 03 §4.7 and §6 rule 9. Start the process before the prompt
   * arrives, so the first turn of a chat does not pay pi's cold start: probe
   * 0a measured 1195 ms for a lone one and 4 ms for a held-open process (A2,
   * A3). A session that already has a usable process is a no-op that succeeds.
   */
  public async openSession(message: OpenSessionMessage): Promise<void> {
    // A family that holds nothing between turns gains nothing from a
    // pre-start, and would leak the process: §6 rule 5's idle sweep is off at
    // ttl 0, and `afterTurn` only reaps once a turn has run.
    if (this.settings.idleTtlS <= 0) {
      this.notOpened(message.session, "not_held", "this family holds no process between turns");
      return;
    }

    const existing = this.sessions.get(message.session);
    const stale = existing === undefined ? Staleness.Binding : this.staleness(existing, message);
    if (existing !== undefined && stale === Staleness.None) {
      this.opened(message.session, "a pi process is already resident");
      return;
    }

    // §6 rule 5's last line is absolute: a process running a turn is never
    // reaped. Replacing it is the next turn's decision, not this message's.
    if (existing?.busy === true) {
      this.opened(message.session, "a turn is running on the resident process");
      return;
    }

    if (existing !== undefined) {
      await this.recycle(existing, stale);
    }

    const opened = await this.open(message);
    if (!opened.ok) {
      this.notOpened(message.session, opened.reason, opened.detail);
      return;
    }

    this.opened(message.session, "the pi process is resident");
  }

  /**
   * Contract 03 §4.8. Read a session's entries back for the host.
   *
   * A terminal wrote them straight to the session store (§7.6), so there is
   * usually no process here to ask. One is started for the read and stopped
   * again, because a process this pool holds is what §7.5's record makes the
   * terminal refuse to re-attach against (§7.6 exit 8).
   */
  public async readEntries(message: GetEntriesMessage): Promise<void> {
    const held = this.sessions.get(message.session);

    // §4.8 rule 3. Entries are a read, and a read never costs a turn.
    if (held?.busy === true) {
      this.noEntries(message, "session_busy_in_sandbox", "a turn is running on this session");
      return;
    }

    if (held !== undefined) {
      this.emitEntries(message, await held.listEntries(message.since ?? null));
      return;
    }

    const opened = await this.open(message);
    if (!opened.ok) {
      this.noEntries(message, opened.reason, opened.detail);
      return;
    }

    const read = await opened.session.listEntries(message.since ?? null);
    await this.reap(opened.session, "stopped");
    this.emitEntries(message, read);
  }

  /** Contract 03 §4.2. `prompt` is an optimisation; `start_turn` is correct. */
  public async prompt(message: PromptMessage): Promise<void> {
    const session = this.sessions.get(message.session);
    if (session === undefined) {
      this.failTurn(message, "no_resident_process", "no pi process is resident");
      return;
    }

    // Busy comes FIRST. §6 rule 5's last line is absolute: a process running a
    // turn is never reaped, whatever else is true of it. A stale epoch on a
    // busy session is the next turn's problem, and reaping here would abandon
    // the turn in flight.
    if (this.busyWith(message)) {
      return;
    }

    if (session.envEpoch < message.env_epoch) {
      // The credentials rotated under a held process. It cannot take the new
      // environment, so it goes, and the host re-sends `start_turn`.
      await this.reap(session, "reaped");
      this.failTurn(message, "no_resident_process", "the resident process held a stale epoch");
      return;
    }

    await this.runTurn(session, message);
  }

  /**
   * §7. The turn file is rewritten before pi hears the prompt, so the bridge
   * inside that process reads THIS turn's id on every PEP call rather than the
   * one the process was started for. `turn-file.ts` says why the environment
   * alone cannot carry it.
   */
  private async runTurn(session: SandboxSession, message: TurnRequest): Promise<void> {
    this.deps.turnFile.write(message.session, message.turn, message.delegation);

    await session.runTurn(message);
  }

  public async steer(session: string, message: string): Promise<void> {
    await this.sessions.get(session)?.steer(message);
  }

  public async abort(session: string): Promise<void> {
    await this.sessions.get(session)?.abort();
  }

  /** Contract 03 §4.5. */
  public async stopProcess(session: string, graceMs: number): Promise<void> {
    const held = this.sessions.get(session);
    if (held === undefined) {
      return;
    }

    this.sessions.delete(session);
    await held.stop(graceMs, "stopped");
  }

  /** Contract 03 §4.6. `shutdown` runs §4.5 for every session, then exits. */
  public async shutdown(graceMs: number): Promise<void> {
    this.stopSweep();

    const all = [...this.sessions.values()];
    this.sessions.clear();
    await Promise.all(all.map((session) => session.stop(graceMs, "shutdown")));
  }

  /** Contract 03 §11.1 rule 2. No grace: the host is gone and is not reading. */
  public killAll(): void {
    this.stopSweep();
    for (const session of this.sessions.values()) {
      session.kill();

      // Released here rather than in `forget`: this path ends the process, and
      // a close event that arrives after it would reach nobody. The family's
      // sessions must read free the moment their processes are killed.
      this.deps.processes.release(session.id, session.pid);
    }
    this.sessions.clear();
  }

  /**
   * Contract 03 §9 rule 3. Stop reading the busiest pi process. The OS pipe
   * then slows pi itself, which is the correct outcome: a slow reader must
   * slow the producer, never lose its output.
   */
  public throttleBusiest(): void {
    if (this.throttled !== null) {
      return;
    }

    let busiest: SandboxSession | null = null;
    for (const session of this.sessions.values()) {
      if (busiest === null || session.eventsProduced > busiest.eventsProduced) {
        busiest = session;
      }
    }

    if (busiest === null) {
      return;
    }

    this.throttled = busiest;
    busiest.pauseOutput();
    this.log("warn", busiest.id, "the outbound queue is full, slowing this pi process");
  }

  public releaseThrottle(): void {
    const held = this.throttled;
    if (held === null) {
      return;
    }

    this.throttled = null;
    held.resumeOutput();
    this.log("warn", held.id, "the outbound queue drained, reading this pi process again");
  }

  /** §6 rule 2. `attendance`'s writer lease should prevent it. We still check. */
  private busyWith(message: TurnRequest): boolean {
    const session = this.sessions.get(message.session);
    if (session?.busy !== true) {
      return false;
    }

    this.failTurn(message, "session_busy_in_sandbox", "a turn already runs on this session");

    return true;
  }

  /**
   * A held process keeps the environment, the working directory, the model and
   * the family config it started with (§7, §7.1), so it can serve the next turn
   * only when none of the four moved.
   *
   * `config_rev` versions the whole config mount, and the playpen reads that
   * mount when it STARTS a pi process, never per turn. A raised revision
   * therefore makes a held process stale exactly the way a rotated credential
   * does: its `runtime.json`, its `instructions.md` and its skills are the old
   * ones. The model matters for the same reason — `--model` is a start flag, so
   * without this check a turn naming a new model would run on the old one.
   */
  private staleness(session: SandboxSession, spec: ProcessSpec): Staleness {
    if (!session.alive) {
      return Staleness.Binding;
    }
    if (session.envEpoch < spec.env_epoch) {
      return Staleness.Credentials;
    }
    if (session.spec.configRev !== spec.config_rev) {
      return Staleness.Config;
    }
    if (spec.model !== undefined && session.spec.models[0] !== piModelId(spec.model)) {
      return Staleness.Binding;
    }
    if (session.spec.sessionDir !== spec.session_dir) {
      return Staleness.Binding;
    }

    // An unusable workspace answers null, which no cwd equals, so the process
    // is replaced and `spawn` reports the real reason.
    return session.spec.cwd === this.cwdFor(spec) ? Staleness.None : Staleness.Binding;
  }

  /**
   * §6 rule 5. Closes a held process that can no longer serve, at a turn
   * boundary. Every caller has already refused a busy session, because rule 5's
   * last line is absolute: a process with a running turn is never reaped.
   */
  private async recycle(session: SandboxSession, why: Staleness): Promise<void> {
    this.log("info", session.id, `recycling the held pi process: the ${why} moved`);

    await this.reap(session, "reaped");
  }

  /** The cwd a process for this spec must already have. It creates nothing. */
  private cwdFor(spec: ProcessSpec): string | null {
    const workspace = spec.workspace;
    if (workspace === undefined || workspace.kind !== CODE_SANDBOX_KIND) {
      return spec.cwd;
    }

    return this.links.pathFor(workspace.owner_session);
  }

  /** The cwd for a NEW process. Making the §7.2 link is its one side effect. */
  private holdCwd(spec: ProcessSpec): string {
    const workspace = spec.workspace;
    if (workspace === undefined || workspace.kind !== CODE_SANDBOX_KIND) {
      return spec.cwd;
    }

    return this.links.hold(spec.session, workspace.owner_session);
  }

  private async open(spec: ProcessSpec): Promise<SpawnOutcome> {
    if (this.starting.has(spec.session)) {
      return {
        ok: false,
        reason: "session_busy_in_sandbox",
        detail: "the pi process is already starting",
      };
    }

    const started = this.spawn(spec);
    this.starting.set(spec.session, started);

    try {
      return await started;
    } finally {
      this.starting.delete(spec.session);
    }
  }

  /**
   * Waits out a start already running for one session.
   *
   * `open_session` is fire and forget on the host: `attendance` sends it at
   * create-or-find and never waits for `session_opened` (§4.7 rules 8 to 11),
   * so the first `start_turn` of a chat can arrive while that pre-start is
   * still spawning. That process is the one the turn wants, so the turn waits
   * for it. Failing here with `session_busy_in_sandbox` would make an
   * optimisation break the turn it exists to speed up.
   */
  private async settleStart(session: string): Promise<void> {
    const inFlight = this.starting.get(session);
    if (inFlight === undefined) {
      return;
    }

    // The outcome belongs to the caller that began it. This one only needs
    // the start to be over, and then re-runs its own reuse test.
    await inFlight.catch(() => undefined);
  }

  /**
   * One pi process. The outcome is returned rather than reported, because the
   * two callers answer differently: `start_turn` owes the host a `turn_failed`
   * and `open_session` owes it a `session_opened` (§4.7).
   */
  private async spawn(spec: ProcessSpec): Promise<SpawnOutcome> {
    let cwd: string;
    try {
      cwd = this.holdCwd(spec);
    } catch (error) {
      const detail = error instanceof WorkspaceLinkError ? error.message : String(error);

      return { ok: false, reason: "internal", detail: `the workspace link failed: ${detail}` };
    }

    const creds = await this.deps.creds.read(spec.env_epoch);
    if (creds === null) {
      return {
        ok: false,
        reason: "stale_credentials",
        detail: "the credential file did not reach env_epoch in time",
      };
    }

    this.makeRoom();

    // §7.1: the config mount is read when a process starts, never per turn.
    const config = readRuntimeConfig(this.deps.configDir);
    const start = this.piStart(spec, config);
    const models = start.models;

    // Both documents live in PI_CODING_AGENT_DIR, pi reads each one once at
    // start, and neither has a flag pointing elsewhere. models.json is what
    // resolves a model at all; settings.json is what ENABLES the built-ins
    // this family was granted, and rewriting it is also what removes whatever
    // the last session left in its own store (`pi-settings.ts`).
    try {
      writeModelsJson(spec.session_dir, models);
      writePiSettings(spec.session_dir, config);
    } catch (error) {
      return {
        ok: false,
        reason: "pi_start_failed",
        detail: `cannot write pi's session store: ${String(error)}`,
      };
    }

    const sessionSpec: SessionSpec = {
      session: spec.session,
      cwd,
      sessionDir: spec.session_dir,
      envEpoch: creds.epoch,
      configRev: spec.config_rev,
      models,
    };

    const turnFilePath = this.deps.turnFile.pathFor(spec.session);
    const toolStatePath = this.deps.toolState.pathFor(spec.session);
    const session = new SandboxSession(
      sessionSpec,
      this.deps.launcher,
      start.args,
      buildTurnEnv({
        sessionDir: spec.session_dir,
        family: this.settings.family,
        sandbox: this.settings.sandbox,
        session: spec.session,
        ...(spec.turn === undefined ? {} : { turn: spec.turn }),
        ...(turnFilePath === null ? {} : { turnFile: turnFilePath }),
        ...(toolStatePath === null ? {} : { toolStateFile: toolStatePath }),
        creds,
      }),
      this.settings.coalesceMs,
      {
        emit: this.deps.emit,
        residentAfterTurn: () => this.settings.idleTtlS > 0,
        onTurnEnd: (ended) => this.afterTurn(ended),
        onExit: (ended) => this.forget(ended),
      },
    );

    this.sessions.set(spec.session, session);

    // §7.5. The record is the second fence against two writers on one session
    // store, for the one reader the lease cannot reach: a terminal started
    // inside this VM.
    this.deps.processes.hold(spec.session, session.pid);

    return { ok: true, session };
  }

  /**
   * pi's argv and the models behind it, from the one builder both programs in
   * this bundle use (`pi-args.ts`). `agent-pi-launch` asks the same function
   * for the same session and gets the same line minus `--mode rpc`, which is
   * what keeps a terminal's reach equal to the chat's.
   */
  private piStart(spec: ProcessSpec, config: RuntimeConfig): PiStart {
    const bridgePath = bridgeAt(this.deps.bridgePath);
    if (bridgePath === null) {
      this.log("warn", null, `no PEP bridge at ${this.deps.bridgePath}; no action tools here`);
    }

    const args: PiArgsSpec = {
      session: spec.session,
      config,
      bridgePath,
      ...(spec.model === undefined ? {} : { model: spec.model }),
    };

    return buildPiStart(args, PiMode.Rpc);
  }

  /** §6 rule 5, second bullet: the cap reaps the least recently used idle one. */
  private makeRoom(): void {
    while (this.sessions.size >= this.settings.maxResident) {
      const victim = this.oldestIdle();
      if (victim === null) {
        // Every resident process is running a turn, and §6 rule 5 never reaps
        // one of those. The cap is a reap trigger, not an admission gate.
        return;
      }

      void this.reap(victim, "reaped");
    }
  }

  private oldestIdle(): SandboxSession | null {
    let oldest: SandboxSession | null = null;
    for (const session of this.sessions.values()) {
      if (session.busy) {
        continue;
      }
      if (oldest === null || session.idleMs > oldest.idleMs) {
        oldest = session;
      }
    }

    return oldest;
  }

  private afterTurn(session: SandboxSession): void {
    this.sayWhatTheToolsDid(session);

    // `pi_idle_ttl_s` 0 is the thin and autonomous default: nothing is held.
    if (this.settings.idleTtlS <= 0 && this.sessions.get(session.id) === session) {
      void this.reap(session, "reaped");
    }
  }

  /**
   * What this turn's reader is owed about its tools, in words a person reads.
   *
   * It is said at the END of the turn, not the start, because that is when
   * the answer is known for both cases: a fresh process is still fetching its
   * manifest when the prompt goes in, and a held-open one may gain its tools
   * mid-turn from the bridge's poll.
   *
   * `log` at `warn` is the channel message contract 03 §5.5 already gives
   * free text, and `attendance` turns a `warn` with a session into a journal
   * `note`. No new event type is invented for either line. A state the bridge
   * never wrote reads null, and null says nothing: see `tool-state.ts`.
   */
  private sayWhatTheToolsDid(session: SandboxSession): void {
    const state = this.deps.toolState.read(session.id);
    if (state === null) {
      return;
    }

    this.sayIfToolsMoved(session, state.rev, state.tools);

    if (state.tools > 0) {
      return;
    }

    this.log("warn", session.id, state.rev === null ? TOOLLESS_NO_ANSWER : TOOLLESS_NO_GRANT);
  }

  private sayIfToolsMoved(session: SandboxSession, rev: string | null, count: number): void {
    if (rev === null) {
      return;
    }

    const before = this.toolRevs.get(session.id);
    this.toolRevs.set(session.id, rev);

    if (before === undefined || before === rev) {
      return;
    }

    this.log("warn", session.id, toolsMovedLine(count));
  }

  private reapIdle(): void {
    if (this.settings.idleTtlS <= 0) {
      return;
    }

    const limit = this.settings.idleTtlS * 1000;
    for (const session of [...this.sessions.values()]) {
      if (!session.busy && session.idleMs >= limit) {
        void this.reap(session, "reaped");
      }
    }
  }

  /** §6 rule 7. Reaping closes stdin. It never sends a signal first. */
  private async reap(session: SandboxSession, reason: "reaped" | "stopped"): Promise<void> {
    if (this.sessions.get(session.id) === session) {
      this.sessions.delete(session.id);
    }

    await session.stop(0, reason);
  }

  private forget(session: SandboxSession): void {
    if (this.sessions.get(session.id) === session) {
      this.sessions.delete(session.id);
    }
    if (this.throttled === session) {
      this.throttled = null;
    }

    this.deps.turnFile.forget(session.id);

    // The next process for this session fetches its own manifest, so its
    // revision is a start-up snapshot and never a list that moved under a
    // reader. Keeping the old one would make every second thin job claim a
    // change nobody made.
    this.toolRevs.delete(session.id);

    // §7.2. The link goes with the process that ran in it. The directory it
    // points at stays: it belongs to the calling chat (contract 02 §12.1).
    this.links.release(session.id);

    // §7.5. The session is free, so a terminal may take it.
    this.deps.processes.release(session.id, session.pid);
  }

  /** Contract 03 §4.7's acknowledgement. A no-op open still answers this way. */
  private opened(session: string, detail: string): void {
    this.deps.emit({
      type: "session_opened",
      session,
      resident: true,
      reason: null,
      message: detail,
    });
  }

  /**
   * Contract 03 §5.8, when the playpen got as far as asking pi.
   *
   * `reason` stays null even when pi refused, because no §5.3 row describes
   * a refused READ and inventing one would give the host a turn reason for
   * something that is not a turn. The `log` line below is where an operator
   * learns it happened.
   */
  private emitEntries(message: GetEntriesMessage, read: EntryRead): void {
    if (!read.ok) {
      this.log("warn", message.session, "pi refused to list this session's entries");
    }

    this.deps.emit({
      type: "entries",
      request: message.request,
      session: message.session,
      ok: read.ok,
      reason: null,
      since_matched: read.sinceMatched,
      truncated: read.truncated,
      leaf_id: read.leafId,
      entries: read.entries,
    });
  }

  /** §4.8 rule 1. Every `get_entries` is answered, refusals included. */
  private noEntries(
    message: GetEntriesMessage,
    reason: TurnFailReason,
    detail: string,
  ): void {
    this.log("warn", message.session, `no entries read: ${detail}`);
    this.deps.emit({
      type: "entries",
      request: message.request,
      session: message.session,
      ok: false,
      reason,
      since_matched: false,
      truncated: false,
      leaf_id: null,
      entries: [],
    });
  }

  private notOpened(session: string, reason: SessionOpenReason, detail: string): void {
    this.deps.emit({
      type: "session_opened",
      session,
      resident: false,
      reason,
      message: detail.slice(0, MAX_LOG_BYTES),
    });
  }

  private failTurn(message: TurnRequest, reason: TurnFailReason, detail: string): void {
    this.deps.emit({
      type: "turn_failed",
      session: message.session,
      turn: message.turn,
      turn_seq: 1,
      reason,
      message: detail.slice(0, MAX_LOG_BYTES),
    });
  }

  private log(level: "warn" | "info", session: string | null, message: string): void {
    this.deps.emit({ type: "log", level, session, message });
  }
}
