// One `pi --mode rpc` process, serving exactly one session (contract 03 §6
// rule 1). This layer knows pi's own vocabulary and nothing about the channel.
//
//   PiProcess ── stdin  ──► {"type":"prompt","id":"c1","message":"…"}
//             ◄─ stdout ──  {"type":"response","id":"c1",…}  → resolves c1
//             ◄─ stdout ──  {"type":"message_update",…}      → onEvent
//             ◄─ stderr ──  free text                        → onLog
//
// Two pi facts drive the whole file (pi 0.99.1 docs/rpc.md):
//   1. A `prompt` response means ACCEPTED, never done. Only `agent_settled`
//      means the turn is settled.
//   2. stdin EOF terminates pi at once and abandons an in-flight turn, so
//      stdin stays open until the process is deliberately stopped.

import type { ChildProcess } from "node:child_process";
import { readFileSync } from "node:fs";

import {
  MAX_LOG_BYTES,
  PI_RESPONSE_TYPE,
  STOP_SIGKILL_DELAY_MS,
  STOP_SIGTERM_DELAY_MS,
} from "./constants.js";
import { cutToBytes, LineReader } from "./framing.js";
import { boundRecord, readResponse } from "./pi-record.js";
import type { PiCommand, PiResponse } from "./protocol.js";

/** A control command that never answers must not stall a turn forever. */
const COMMAND_TIMEOUT_MS = 30000;

/** VmRSS is reported in kilobytes. */
const KB_PER_MB = 1024;

export interface PiSpawnSpec {
  readonly args: readonly string[];
  readonly cwd: string;
  readonly env: NodeJS.ProcessEnv;
}

/** The one seam between this process and the outside world, which tests replace. */
export type PiLauncher = (spec: PiSpawnSpec) => ChildProcess;

export interface PiHandlers {
  readonly onEvent: (event: Record<string, unknown>) => void;
  readonly onLog: (message: string) => void;
  readonly onExit: (code: number | null, signal: string | null) => void;
}

/** Why a command did not get a usable answer. Callers map these to reasons. */
export class PiCommandError extends Error {}

export class PiProcess {
  private readonly child: ChildProcess;
  private readonly pending = new Map<string, (answer: PiResponse) => void>();
  private nextCommandId = 1;
  private stdinClosed = false;
  private exited = false;
  private spoken = false;
  private rssPeakMb: number | null = null;
  private stopTimers: NodeJS.Timeout[] = [];

  public constructor(launcher: PiLauncher, spec: PiSpawnSpec, private readonly handlers: PiHandlers) {
    this.child = launcher(spec);
    this.wireStdout();
    this.wireStderr();
    this.wireExit();
  }

  public get pid(): number | null {
    return this.child.pid ?? null;
  }

  public get isAlive(): boolean {
    return !this.exited;
  }

  /**
   * True once pi has written one parsable line. It separates "pi would not
   * start" from "pi died mid-turn", which contract 03 §5.3 maps to two
   * different turn reasons and contract 02 §14 to two different outcomes.
   */
  public get hasSpoken(): boolean {
    return this.spoken;
  }

  public get peakRssMb(): number | null {
    return this.rssPeakMb;
  }

  /** Contract 03 §9 rule 3. A slow reader must slow pi, never lose its output. */
  public pauseOutput(): void {
    this.child.stdout?.pause();
  }

  public resumeOutput(): void {
    this.child.stdout?.resume();
  }

  /** Samples VmRSS. Linux only; anywhere else the answer is honestly null. */
  public sampleRss(): number | null {
    const pid = this.pid;
    if (pid === null) {
      return this.rssPeakMb;
    }

    try {
      const status = readFileSync(`/proc/${pid}/status`, "utf8");
      const match = /^VmRSS:\s+(\d+) kB$/m.exec(status);
      if (match?.[1] === undefined) {
        return this.rssPeakMb;
      }

      const mb = Math.round(Number(match[1]) / KB_PER_MB);
      this.rssPeakMb = Math.max(this.rssPeakMb ?? 0, mb);
    } catch {
      // Not Linux, or the process is gone. Both are answers, not errors.
    }

    return this.rssPeakMb;
  }

  /** Sends a command and waits for the response pi correlates by `id`. */
  public async call(command: PiCommand): Promise<PiResponse> {
    if (this.exited || this.stdinClosed) {
      throw new PiCommandError("pi process is not accepting commands");
    }

    const id = `c${this.nextCommandId}`;
    this.nextCommandId += 1;

    const answer = new Promise<PiResponse>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new PiCommandError(`pi did not answer ${command.type} in time`));
      }, COMMAND_TIMEOUT_MS);

      this.pending.set(id, (response) => {
        clearTimeout(timer);
        resolve(response);
      });
    });

    this.write({ ...command, id });

    return answer;
  }

  /**
   * Contract 03 §4.5 rules 3 to 5. Close stdin, then escalate. NEVER call this
   * while a turn runs: stdin EOF abandons the in-flight turn.
   */
  public closeStdin(): void {
    if (this.stdinClosed || this.exited) {
      return;
    }

    this.stdinClosed = true;
    try {
      this.child.stdin?.end();
    } catch {
      // Already gone. Closing a closed pipe is not a failure.
    }

    this.stopTimers.push(setTimeout(() => this.signal("SIGTERM"), STOP_SIGTERM_DELAY_MS));
    this.stopTimers.push(
      setTimeout(() => this.signal("SIGKILL"), STOP_SIGTERM_DELAY_MS + STOP_SIGKILL_DELAY_MS),
    );
  }

  /** Contract 03 §11.1 rule 2. The heartbeat deadline does not wait politely. */
  public kill(): void {
    this.signal("SIGKILL");
  }

  private signal(name: NodeJS.Signals): void {
    if (this.exited) {
      return;
    }

    try {
      this.child.kill(name);
    } catch {
      // Already reaped between the check and the call.
    }
  }

  private write(command: PiCommand): void {
    try {
      this.child.stdin?.write(`${JSON.stringify(command)}\n`);
    } catch {
      throw new PiCommandError("pi stdin is closed");
    }
  }

  private wireStdout(): void {
    const reader = new LineReader(
      (line) => this.takeLine(line),
      (bytes) => this.handlers.onLog(`pi produced a ${bytes} byte line and it was dropped`),
    );

    this.child.stdout?.setEncoding("utf8");
    this.child.stdout?.on("data", (chunk: string) => reader.feed(chunk));
  }

  private takeLine(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      // pi wrote something that is not a record. Report it, never forward it.
      this.handlers.onLog("pi wrote a line that is not JSON");
      return;
    }

    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      this.handlers.onLog("pi wrote a line that is not a JSON object");
      return;
    }

    this.spoken = true;

    const record = boundRecord(parsed as Record<string, unknown>, line);
    if (record["type"] !== PI_RESPONSE_TYPE) {
      this.handlers.onEvent(record);
      return;
    }

    const response = readResponse(record);
    const id = response.id;
    if (id === undefined) {
      return;
    }

    const waiting = this.pending.get(id);
    this.pending.delete(id);
    waiting?.(response);
  }

  private wireStderr(): void {
    // Contract 03 §5.5. pi writes real warnings here, including the
    // "creating a new session with that id" one on a first turn. It must
    // never reach the channel's stdout directly.
    const reader = new LineReader(
      (line) => this.handlers.onLog(cutToBytes(line, MAX_LOG_BYTES)),
      (bytes) => this.handlers.onLog(`pi wrote a ${bytes} byte stderr line and it was dropped`),
    );

    this.child.stderr?.setEncoding("utf8");
    this.child.stderr?.on("data", (chunk: string) => reader.feed(chunk));
  }

  private wireExit(): void {
    this.child.on("error", (error: Error) => {
      this.handlers.onLog(`pi process error: ${error.message}`);
      this.finish(null, null);
    });

    this.child.on("close", (code: number | null, signal: NodeJS.Signals | null) => {
      this.finish(code, signal);
    });
  }

  private finish(code: number | null, signal: string | null): void {
    if (this.exited) {
      return;
    }

    this.exited = true;
    for (const timer of this.stopTimers) {
      clearTimeout(timer);
    }
    this.stopTimers = [];

    // Every caller still waiting learns the process is gone instead of hanging.
    for (const [, waiting] of this.pending) {
      waiting({ type: "response", success: false, error: "pi process exited" });
    }
    this.pending.clear();

    this.handlers.onExit(code, signal);
  }
}
