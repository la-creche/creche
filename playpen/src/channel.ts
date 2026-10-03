// Contract 03 §1 and §9. The one stdio channel: stdin carries host commands,
// stdout carries the protocol and nothing else, stderr carries free text that
// `attendance` writes to a log file and never parses.
//
// §9's problem is that one pipe is shared by every session of a family, so a
// stall anywhere stalls the whole family. This file owns the outbound half of
// the answer: a bounded queue, and a signal when it fills so the caller can
// slow the pi process that is filling it. The OS pipe then slows pi itself,
// which is the correct outcome — a slow reader must slow the producer, never
// lose its output.

import type { Writable } from "node:stream";

import { MAX_LINE_BYTES, MAX_PENDING_LINES, RESUME_PENDING_LINES } from "./constants.js";
import { byteLength } from "./framing.js";
import type { PlaypenMessage } from "./protocol.js";

export enum Pressure {
  Full = "full",
  Drained = "drained",
}

export type PressureHandler = (state: Pressure) => void;

export class Channel {
  private readonly queue: string[] = [];
  private writing = false;
  private full = false;
  private pressure: PressureHandler | null = null;

  public constructor(
    private readonly out: Writable,
    private readonly maxPending: number = MAX_PENDING_LINES,
  ) {}

  public onPressure(handler: PressureHandler): void {
    this.pressure = handler;
  }

  public get pendingLines(): number {
    return this.queue.length;
  }

  /**
   * Queues one protocol record.
   *
   * A line the playpen knows is over `MAX_LINE_BYTES` is never emitted
   * (§2 rule 6). Event bodies are truncated upstream, in `wrapEvent`; this is
   * the last guard, and it reports the drop rather than hiding it.
   */
  public send(message: PlaypenMessage): void {
    const line = `${JSON.stringify(message)}\n`;
    if (byteLength(line) - 1 > MAX_LINE_BYTES) {
      this.push(`${JSON.stringify(this.oversizeLog(message))}\n`);
      return;
    }

    this.push(line);
  }

  /** Free text for the operator. It leaves by stderr, never by the protocol. */
  public note(text: string): void {
    process.stderr.write(`${text}\n`);
  }

  private oversizeLog(message: PlaypenMessage): PlaypenMessage {
    return {
      type: "log",
      level: "error",
      session: null,
      message: `a ${message.type} line passed MAX_LINE_BYTES and was dropped`,
    };
  }

  private push(line: string): void {
    this.queue.push(line);
    if (!this.full && this.queue.length >= this.maxPending) {
      this.full = true;
      this.pressure?.(Pressure.Full);
    }

    this.pump();
  }

  private pump(): void {
    if (this.writing) {
      return;
    }

    this.writing = true;
    for (;;) {
      const line = this.queue.shift();
      if (line === undefined) {
        break;
      }

      if (!this.out.write(line)) {
        // The pipe is full. Resume on drain, and keep the queue as it is so
        // `pendingLines` stays honest while the host catches up.
        this.out.once("drain", () => {
          this.writing = false;
          this.pump();
        });
        this.relieve();
        return;
      }
    }

    this.writing = false;
    this.relieve();
  }

  private relieve(): void {
    if (!this.full || this.queue.length > RESUME_PENDING_LINES) {
      return;
    }

    this.full = false;
    this.pressure?.(Pressure.Drained);
  }
}
