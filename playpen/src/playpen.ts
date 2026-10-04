// Contract 03 §3, §4 and §11. The dispatcher: it owns the handshake, the
// inbound half of the channel, the heartbeat deadline and the lock.
//
//   stdin ─► LineReader ─► parseHostMessage ─► dispatch ─► SessionPool
//                                  │                           │
//                                  └── refusal ──► Channel ◄────┘
//                                                    │
//                                                  stdout
//
// Two rules shape the whole file:
//
//  1. Nothing is acted on before `validate.ts` has passed it. The playpen
//     sits inside the untrusted sandbox, and a malformed command must never
//     crash it (invariants 12 and 14).
//  2. The playpen does not trust stdin to tell it the host is gone. Probe
//     0a killed the host end of the exec mid-turn and the playpen stayed
//     alive inside a running VM with zero sessions (A5, refuted). The
//     heartbeat deadline, not EOF, is what ends this process.

import type { Readable } from "node:stream";

import { Channel, Pressure } from "./channel.js";
import {
  DEFAULT_COALESCE_MS,
  DEFAULT_HOST_DEADLINE_S,
  DEFAULT_MAX_RESIDENT,
  DEFAULT_PI_IDLE_TTL_S,
  DEFAULT_STOP_GRACE_MS,
  FIRST_TURN_SEQ,
  MAX_LOG_BYTES,
  PEP_BRIDGE_PATH,
  PROTOCOL_VERSION,
  PLAYPEN_NAME,
} from "./constants.js";
import { CredReader } from "./creds.js";
import { cutToBytes, LineReader } from "./framing.js";
import { LockState, PlaypenLock } from "./lock.js";
import type { PiLauncher } from "./pi-process.js";
import { SessionPool } from "./pool.js";
import type { ProcessRecords } from "./process-record.js";
import type {
  AbortMessage,
  GetEntriesMessage,
  HelloMessage,
  HostMessage,
  LogLevel,
  OpenSessionMessage,
  PingMessage,
  PromptMessage,
  StartTurnMessage,
  SteerMessage,
  StopProcessMessage,
  PlaypenMessage,
} from "./protocol.js";
import { countForeignPi, piVersion, residentCeiling } from "./sandbox-facts.js";
import type { ToolStateFile } from "./tool-state.js";
import type { TurnFile } from "./turn-file.js";
import { parseHostMessage } from "./validate.js";
import type { Refusal } from "./validate.js";

/**
 * Optional behaviours this playpen supports, reported in `ready`.
 *
 * `get_entries` is here so the host can tell an image that serves §4.8 from
 * one that does not. An older bundle logs an unknown type and answers
 * nothing, and a host that asked anyway would lose every terminal exchange
 * in silence, which is the failure contract 02 §10.5 exists to remove.
 */
const CAPS: readonly string[] = ["steer", "coalesce", "workspace_link", "get_entries"];

/** This package's own version. `package.json` carries the same number. */
const PLAYPEN_VERSION = "0.1.0";

/**
 * A message with no turn has no turn sequence. 0 says exactly that.
 *
 * Contract 03 §5.1: `turn_seq` is 0 on a message whose session and turn are
 * both null, such as §5.3's `line_too_large`. 0 is outside the "from 1"
 * range on purpose, so a host checking the sequence cannot mistake it for a
 * first message.
 */
const NO_TURN_SEQ = 0;

const BYTES_PER_MB = 1024 * 1024;

export const EXIT_OK = 0;
/** A handler threw and the playpen cannot serve. Node ends with this code too. */
export const EXIT_INTERNAL = 1;
export const EXIT_LOCK_HELD = 3;
export const EXIT_PROTOCOL = 4;
export const EXIT_DEADLINE = 5;
export const EXIT_CONTROL_MOUNT = 6;

export interface PlaypenOptions {
  readonly sandbox: string;
  readonly input: Readable;
  readonly channel: Channel;
  readonly launcher: PiLauncher;
  readonly lock: PlaypenLock;
  readonly creds: CredReader;
  readonly configDir: string;
  readonly turnFile: TurnFile;
  readonly toolState: ToolStateFile;
  readonly processes: ProcessRecords;
  readonly piPackageJson?: string;
  /** The image path of the PEP bridge. `PEP_BRIDGE_PATH` when absent. */
  readonly bridgePath?: string;
  readonly codeSandboxRoot?: string;
  readonly codeSandboxLinkPrefix?: string;
  readonly onExit: (code: number) => void;
}

export class Playpen {
  private pool: SessionPool | null = null;
  private deadline: NodeJS.Timeout | null = null;
  private deadlineS = DEFAULT_HOST_DEADLINE_S;
  private ceiling = 1;
  private greeted = false;
  private closing = false;

  public constructor(private readonly options: PlaypenOptions) {}

  /**
   * Takes the lock, announces the sandbox, then serves.
   *
   * Returns false when a live playpen already holds the lock. Two
   * playpens in one VM is the failure §11.4 rule 4 exists to prevent, and
   * there is no ingress to sort it out afterwards.
   */
  public start(): boolean {
    const holder = this.options.lock.liveHolder();
    if (holder !== null) {
      this.note(`a live playpen holds the lock, pid ${holder.pid}`);
      this.options.onExit(EXIT_LOCK_HELD);

      return false;
    }

    this.ceiling = residentCeiling();

    if (!this.takeLock()) {
      return false;
    }

    this.sendReady();
    this.listen();

    // §3 rule 4 forbids any host message before `hello`, but a host that dies
    // between the exec and its first line must not leave this process
    // immortal, so the deadline runs from `ready`, not from `hello`.
    //
    // CONTRACT-QUESTION: §11.1 takes the deadline from `hello` and states
    // no bound on the wait for `hello` itself. The default covers it here.
    this.armDeadline();

    return true;
  }

  /**
   * §11.1 rules 3 and 4. A control mount this process cannot write is the one
   * failure that has to reach the host as protocol: without the lock a second
   * playpen could start beside this one, and there is no ingress to sort
   * two of them out afterwards.
   *
   * The `fatal` line goes out BEFORE `onExit`, so the host reads the cause
   * rather than a bare `channel_lost` (§5.7 rule 5).
   */
  private takeLock(): boolean {
    const outcome = this.options.lock.write(this.options.sandbox, this.deadlineS);
    if (outcome.state === LockState.Taken) {
      return true;
    }

    this.options.channel.send({
      type: "fatal",
      reason: "control_mount_unwritable",
      message: cutToBytes(`cannot write the playpen lock: ${outcome.detail}`, MAX_LOG_BYTES),
    });
    this.note(`control mount is not writable: ${outcome.detail}`);
    this.options.onExit(EXIT_CONTROL_MOUNT);

    return false;
  }

  private sendReady(): void {
    this.options.channel.send({
      type: "ready",
      protocol: PROTOCOL_VERSION,
      supervisor: `${PLAYPEN_NAME}/${PLAYPEN_VERSION}`,
      pi: this.readPiVersion(),
      node: process.versions.node,
      sandbox: this.options.sandbox,
      max_resident_processes: this.ceiling,
      foreign_pi_processes: countForeignPi(),
      caps: CAPS,
    });
  }

  private readPiVersion(): string {
    const path = this.options.piPackageJson;

    return path === undefined ? piVersion() : piVersion(path);
  }

  private listen(): void {
    const reader = new LineReader(
      (line) => this.takeLine(line),
      (bytes) => this.refuseOversize(bytes),
    );

    this.options.input.setEncoding("utf8");
    this.options.input.on("data", (chunk: string) => reader.feed(chunk));

    // §11.1. EOF is a real end when it arrives. It is simply not one the
    // playpen may wait for, which is what the deadline covers.
    this.options.input.on("end", () => void this.shutdown(DEFAULT_STOP_GRACE_MS, EXIT_OK));
  }

  /** §5.3. An oversize line names no turn, so the host gets the fact alone. */
  private refuseOversize(bytes: number): void {
    this.options.channel.send({
      type: "turn_failed",
      session: null,
      turn: null,
      turn_seq: NO_TURN_SEQ,
      reason: "line_too_large",
      message: `an inbound line of ${bytes} bytes passed MAX_LINE_BYTES`,
    });
  }

  private takeLine(line: string): void {
    if (this.closing) {
      return;
    }

    const parsed = parseHostMessage(line);
    if (!parsed.ok) {
      this.refuse(parsed);
      return;
    }

    const message = parsed.message;
    this.dispatch(message).catch((error: unknown) => this.dispatchFailed(message, error));
  }

  /**
   * The one handler of the dispatcher. A handler that throws is a bug in the
   * playpen, and one bug must not end the process that serves each session
   * of the family.
   *
   * The host still gets the answer that it waits for. Contract 03 §5.3 fails
   * the turn that the line named with `internal`, and §4.8 rule 1 answers a
   * `get_entries`. Each other line is reported.
   *
   * Two lines are the exception. Half a `hello` leaves no pool, and half a
   * `shutdown` leaves a process that reads no line and holds the lock.
   * Neither can serve, so the process ends.
   */
  private dispatchFailed(message: HostMessage, error: unknown): void {
    const cause = error instanceof Error ? error.message : "the handler threw no Error";
    const detail = `${message.type} failed in the playpen: ${cause}`;
    const pool = this.pool;

    if (message.type === "hello" || message.type === "shutdown") {
      this.log("error", null, detail);
      this.giveUp();
      return;
    }

    if (pool !== null && "turn" in message) {
      pool.refuseTurn(message, detail);
      return;
    }

    if (pool !== null && message.type === "get_entries") {
      pool.refuseEntries(message, detail);
      return;
    }

    this.log("error", null, detail);
  }

  /**
   * A refusal that names a turn fails that turn, so the host is never left
   * waiting on a message the playpen threw away. One that names nothing
   * can only be reported.
   *
   * Contract 03 §5.3 fixes the reason: `internal`, the conservative outcome,
   * because the reason table has nothing narrower.
   *
   * The failure names a turn, so §5.1 numbers it from 1. The pool knows the
   * number, because it knows whether that turn runs.
   */
  private refuse(refusal: Refusal): void {
    const session = refusal.session;
    const turn = refusal.turn;
    const detail = `refused a host message: ${refusal.detail}`;
    if (session === undefined || turn === undefined) {
      this.log("error", null, detail);
      return;
    }

    if (this.pool !== null) {
      this.pool.refuseTurn({ session, turn }, detail);
      return;
    }

    // No pool exists before `hello`, so no turn runs and none sent a line.
    this.options.channel.send({
      type: "turn_failed",
      session,
      turn,
      turn_seq: FIRST_TURN_SEQ,
      reason: "internal",
      message: detail,
    });
  }

  private async dispatch(message: HostMessage): Promise<void> {
    if (message.type === "hello") {
      this.takeHello(message);
      return;
    }

    // §3 rule 4. Nothing crosses this line before the handshake completes.
    if (!this.greeted) {
      this.log("error", null, `a ${message.type} arrived before hello`);
      return;
    }

    await this.route(message);
  }

  private async route(message: Exclude<HostMessage, HelloMessage>): Promise<void> {
    switch (message.type) {
      case "open_session":
        return this.onOpenSession(message);
      case "start_turn":
        return this.onStartTurn(message);
      case "prompt":
        return this.onPrompt(message);
      case "steer":
        return this.onSteer(message);
      case "abort":
        return this.onAbort(message);
      case "stop_process":
        return this.onStopProcess(message);
      case "get_entries":
        return this.onGetEntries(message);
      case "ping":
        return this.onPing(message);
      default:
        return this.shutdown(message.grace_ms ?? DEFAULT_STOP_GRACE_MS, EXIT_OK);
    }
  }

  /**
   * §3. The handshake, and the only message that may arrive twice without
   * being a fault: a re-`hello` is refused rather than acted on, because the
   * pool it would rebuild already holds live processes.
   */
  private takeHello(message: HelloMessage): void {
    if (this.greeted) {
      this.log("error", null, "a second hello arrived and was refused");
      return;
    }

    if (!this.versionsMatch(message.protocol)) {
      this.note(`protocol mismatch: host ${message.protocol}, playpen ${PROTOCOL_VERSION}`);
      this.options.onExit(EXIT_PROTOCOL);

      return;
    }

    if (message.sandbox !== this.options.sandbox) {
      // §3 rule 3. A stale `sbx exec` left over from a sandbox switch.
      this.note(`hello named sandbox ${message.sandbox}, this is ${this.options.sandbox}`);
      this.options.onExit(EXIT_PROTOCOL);

      return;
    }

    this.deadlineS = message.host_deadline_s ?? DEFAULT_HOST_DEADLINE_S;

    // The lock is re-armed with the host's real deadline. A mount that has
    // become unwritable since `start()` ends the process the same way.
    if (!this.takeLock()) {
      return;
    }

    this.greeted = true;
    this.buildPool(message);
    this.armDeadline();
  }

  /** §3. `major` must match exactly. A `minor` difference is legal. */
  private versionsMatch(theirs: string): boolean {
    return theirs.split(".")[0] === PROTOCOL_VERSION.split(".")[0];
  }

  private buildPool(message: HelloMessage): void {
    // §6 rule 8. The effective cap is the lower of the family's number and
    // this playpen's own ceiling.
    const asked = message.max_resident_processes ?? DEFAULT_MAX_RESIDENT;
    const pool = new SessionPool(
      {
        family: message.family,
        sandbox: message.sandbox,
        coalesceMs: message.coalesce_ms ?? DEFAULT_COALESCE_MS,
        idleTtlS: message.pi_idle_ttl_s ?? DEFAULT_PI_IDLE_TTL_S,
        maxResident: Math.min(asked, this.ceiling),
      },
      {
        launcher: this.options.launcher,
        creds: this.options.creds,
        configDir: this.options.configDir,
        turnFile: this.options.turnFile,
        toolState: this.options.toolState,
        processes: this.options.processes,
        bridgePath: this.options.bridgePath ?? PEP_BRIDGE_PATH,
        ...(this.options.codeSandboxRoot === undefined
          ? {}
          : { codeSandboxRoot: this.options.codeSandboxRoot }),
        ...(this.options.codeSandboxLinkPrefix === undefined
          ? {}
          : { codeSandboxLinkPrefix: this.options.codeSandboxLinkPrefix }),
        emit: (out: PlaypenMessage) => this.options.channel.send(out),
      },
    );

    // §9 rule 3. A full outbound queue slows the pi process filling it.
    this.options.channel.onPressure((state) => {
      if (state === Pressure.Full) {
        pool.throttleBusiest();
        return;
      }

      pool.releaseThrottle();
    });

    pool.start();
    this.pool = pool;
  }

  /** §4.7. The pre-start §6 rule 9 asks for. */
  private async onOpenSession(message: OpenSessionMessage): Promise<void> {
    await this.pool?.openSession(message);
  }

  private async onStartTurn(message: StartTurnMessage): Promise<void> {
    await this.pool?.startTurn(message);
  }

  private async onPrompt(message: PromptMessage): Promise<void> {
    await this.pool?.prompt(message);
  }

  private async onSteer(message: SteerMessage): Promise<void> {
    await this.pool?.steer(message.session, message.message);
  }

  private async onAbort(message: AbortMessage): Promise<void> {
    await this.pool?.abort(message.session);
  }

  private async onStopProcess(message: StopProcessMessage): Promise<void> {
    await this.pool?.stopProcess(message.session, message.grace_ms ?? DEFAULT_STOP_GRACE_MS);
  }

  /** §4.8. What a terminal wrote, read back for the host. */
  private async onGetEntries(message: GetEntriesMessage): Promise<void> {
    await this.pool?.readEntries(message);
  }

  /**
   * §10 rule 4 and §11.1. A `pong` proves the channel answers, and the ping
   * that caused it re-arms the deadline that would otherwise end this process.
   */
  private onPing(message: PingMessage): Promise<void> {
    this.armDeadline();
    this.options.channel.send({
      type: "pong",
      nonce: message.nonce ?? null,
      ts_ms: Date.now(),
      resident: this.pool?.residentCount ?? 0,
      rss_mb: this.totalRssMb(),
    });

    return Promise.resolve();
  }

  /** The playpen plus every pi process it holds, which is what §5.5 shows. */
  private totalRssMb(): number {
    const own = Math.round(process.memoryUsage().rss / BYTES_PER_MB);

    return own + (this.pool?.peakRssMb() ?? 0);
  }

  private armDeadline(): void {
    if (this.deadline !== null) {
      clearTimeout(this.deadline);
    }

    this.deadline = setTimeout(() => this.onDeadline(), this.deadlineS * 1000);
  }

  /**
   * §11.1 rule 2. No grace and no waiting: the host is not reading, so an
   * orphaned pi process would keep calling the model on the family key with
   * nobody journalling its turns.
   */
  private onDeadline(): void {
    if (this.closing) {
      return;
    }

    this.closing = true;
    this.note(`no ping within ${this.deadlineS}s, killing every pi process`);
    this.pool?.killAll();
    this.options.lock.release();
    this.options.onExit(EXIT_DEADLINE);
  }

  /**
   * Ends a playpen that cannot serve, as the deadline does: no grace.
   * §11.1 rule 5 says that an exit is always correct, because the session
   * state is on the host.
   */
  private giveUp(): void {
    this.closing = true;
    if (this.deadline !== null) {
      clearTimeout(this.deadline);
      this.deadline = null;
    }

    this.pool?.killAll();
    this.options.processes.clear();
    this.options.lock.release();
    this.options.onExit(EXIT_INTERNAL);
  }

  /** §4.6. `shutdown` runs §4.5 for every session, then the playpen exits. */
  public async shutdown(graceMs: number, code: number): Promise<void> {
    if (this.closing) {
      return;
    }

    this.closing = true;
    if (this.deadline !== null) {
      clearTimeout(this.deadline);
      this.deadline = null;
    }

    await this.pool?.shutdown(graceMs);

    // §7.5. Every session this sandbox held is free once this process is gone,
    // so no record survives it to tell a terminal otherwise.
    this.options.processes.clear();
    this.options.lock.release();
    this.options.onExit(code);
  }

  private log(level: LogLevel, session: string | null, message: string): void {
    this.options.channel.send({
      type: "log",
      level,
      session,
      message: cutToBytes(message, MAX_LOG_BYTES),
    });
  }

  /** Free text for the operator's log file, never the protocol (§1 rule 4). */
  private note(text: string): void {
    this.options.channel.note(`${PLAYPEN_NAME}: ${text}`);
  }
}
