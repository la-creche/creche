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
import type { LogMessage, PlaypenMessage } from "./protocol.js";
import { stringifyStrict } from "./strict-json.js";

/**
 * The first words of the `log` line for a record that is not strict JSON.
 * The word of the broken rule follows them, and the message holds no other
 * text. `playpen.pi_line` names the surface: a line that came from pi.
 */
const NOT_STRICT_WORDS = "json_not_strict playpen.pi_line";

/**
 * The message with a text that holds no lone surrogate.
 *
 * Four kinds of line carry a `message` text that the playpen wrote itself:
 * `log`, `turn_failed`, `fatal` and `session_opened`. U+FFFD takes the place
 * of each lone surrogate. Both count three bytes of UTF-8, so a text that
 * was cut to a byte limit stays inside it.
 */
function withWholeText(message: PlaypenMessage): PlaypenMessage {
  if (!("message" in message) || message.message.isWellFormed()) {
    return message;
  }

  return { ...message, message: message.message.toWellFormed() };
}

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
   * Each line is strict JSON, and `stringifyStrict` writes it. A record
   * that is not strict is never emitted. `boundRecord` cuts a pi record
   * upstream.
   *
   * A line the playpen knows is over `MAX_LINE_BYTES` is never emitted
   * (§2 rule 6). `capEvent` truncates an event body upstream.
   *
   * This is the last guard for both, and it reports the drop rather than
   * hiding it.
   */
  public send(message: PlaypenMessage): void {
    const written = stringifyStrict(withWholeText(message));
    if (!written.ok) {
      this.report(`${NOT_STRICT_WORDS} ${written.rule}`);
      return;
    }

    if (byteLength(written.text) > MAX_LINE_BYTES) {
      this.report(`a ${message.type} line passed MAX_LINE_BYTES and was dropped`);
      return;
    }

    this.push(`${written.text}\n`);
  }

  /** Free text for the operator. It leaves by stderr, never by the protocol. */
  public note(text: string): void {
    process.stderr.write(`${text}\n`);
  }

  /**
   * Queues one `log` line of level `error` for a record that was dropped.
   * The line names no session. `text` is fixed words of this file, so the
   * line is strict JSON and inside the size limit.
   */
  private report(text: string): void {
    const log: LogMessage = { type: "log", level: "error", session: null, message: text };
    const written = stringifyStrict(log);
    if (written.ok) {
      this.push(`${written.text}\n`);
    }
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
