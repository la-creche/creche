// The local link: the Unix socket of guest init in the VM (decisions CH21,
// CH32 rules 6 and 7, CH62 rules 9 to 11). Node cannot dial vsock (fact 31),
// so the facade tells guest init of each turn here, and guest init relays
// each event to the vmm.
//
//   CH mode ──► connect ──► guest init moves the facade to `running`
//     a turn starts ──► {"start":{}} ────────────────────────► up
//     a turn ends ────► {"end":{"title":…,"updated_at":…}} ──► up
//     {"stop":{}} ◄── down ──► stop Flue ──► exit 0, inside the stop wait
//     no open, a close or a fault outside a stop ──► stop Flue ──► exit 18
//   sbx ──► no link (decision CH10-4)
//
// The framing is the one of control link 1200: one compact JSON value per
// LF line, at most `CONTROL_LINE_CAP` bytes. Each line up is a wire
// `TurnEvent`. guest init refuses any other line, and it then closes the
// link and kills the facade (decisions CH32 rule 7, CH62 rule 11). So the
// facade checks each event with the wire validator before it writes it.
//
// guest init kills a facade whose link closes outside a stop (decision CH32
// rule 7). So the facade never closes the link, and the exit of the process
// is its only close. guest init accepts one link per facade start, so
// nothing dials again. The facade fails closed on its link (decision CH67
// rule 8): when the link does not open, or closes or faults, the link tells
// the supervisor. Outside a stop, the supervisor stops Flue and exits 18,
// and guest init starts the facade again after its 1 s restart pause
// (decisions CH62 rule 8, CH66 rule 1). Inside a stop, the stop goes on
// (decision CH66 rule 5).

import { createConnection } from "node:net";
import type { Socket } from "node:net";

import { read, validator } from "agent/wire-read";
import type { Wire } from "agent/wire-read";

import { CONTROL_LINE_CAP, FACADE_STOP_WAIT_MS, LOCAL_LINK_SOCKET } from "./guest.js";
import { describeRefusal, encodeLine } from "./line.js";
import type { Say } from "./stdio.js";

/**
 * The time that the facade keeps after Flue's stop to release the lock and
 * exit, in milliseconds. Its share of the stop wait is the rest.
 */
const EXIT_MARGIN_MS = 1000;

/** How long a stop line gives Flue to stop: under the stop wait of guest init (decision CH62 rule 9). */
export const FLUE_STOP_MS = FACADE_STOP_WAIT_MS - EXIT_MARGIN_MS;

const LF = 0x0a;
const EMPTY = Buffer.alloc(0);

/** One line of the local link, or why the event is not one. */
export type TurnLine = { readonly ok: true; readonly line: string } | { readonly ok: false; readonly detail: string };

/**
 * The line of `event`, its LF included. The wire validator checks the
 * value first: a title of 1 to 256 code points, words one space apart, and
 * a time in the range of `UnixMillis`.
 */
export function turnLine(event: Wire<"TurnEvent">): TurnLine {
  if (!validator("TurnEvent").Check(event)) {
    return { ok: false, detail: "the turn event is not a wire TurnEvent" };
  }

  const encoded = encodeLine(event, CONTROL_LINE_CAP);

  return encoded.ok ? encoded : { ok: false, detail: describeRefusal(encoded.refusal) };
}

/** What the link tells the supervisor. */
export interface LinkHandlers {
  /** Runs at each stop line. */
  readonly onStop: () => void;
  /**
   * Runs once, when the link does not open, or closes or faults. `detail`
   * says which, with the fault's message.
   */
  readonly onLost: (detail: string) => void;
}

export class LocalLink {
  private socket: Socket | null = null;
  /** The bytes of a down line with no LF yet. */
  private pending: Buffer = EMPTY;
  /** True inside a down line over the cap, up to its LF. */
  private skipping = false;

  /**
   * `path` is `LOCAL_LINK_SOCKET` under guest init. `stopMs` is how long a
   * stop line gives Flue to stop (`FLUE_STOP_MS`).
   */
  public constructor(
    public readonly path: string,
    public readonly stopMs: number,
    private readonly say: Say,
  ) {}

  /** Connects. `handlers` get each stop line and the loss of the link. */
  public open({ onStop, onLost }: LinkHandlers): void {
    const socket = createConnection(this.path);
    let opened = false;
    let fault = "";
    socket.on("connect", () => {
      opened = true;
    });
    socket.on("data", (chunk: Buffer) => this.take(chunk, onStop));
    socket.on("error", (error) => {
      fault = `: ${error.message}`;
    });
    socket.on("close", () => {
      this.socket = null;
      onLost(`the local link ${opened ? "closed" : "did not open"}${fault}`);
    });
    this.socket = socket;
  }

  public turnStart(): void {
    this.send({ start: {} });
  }

  /** `updatedAt` is the RFC 3339 time of the turn's last record. */
  public turnEnd(title: string, updatedAt: string): void {
    this.send({ end: { title, updated_at: Date.parse(updatedAt) } });
  }

  private send(event: Wire<"TurnEvent">): void {
    if (this.socket === null) {
      return;
    }

    const line = turnLine(event);
    if (!line.ok) {
      this.say(`a turn event is dropped: ${line.detail}`);
      return;
    }

    this.socket.write(line.line);
  }

  /** Splits the down bytes into lines. A line over the cap is skipped up to its LF. */
  private take(chunk: Buffer, onStop: () => void): void {
    let rest = chunk;
    for (let end = rest.indexOf(LF); end !== -1; end = rest.indexOf(LF)) {
      const line = Buffer.concat([this.pending, rest.subarray(0, end)]);
      rest = rest.subarray(end + 1);
      this.pending = EMPTY;
      if (this.skipping) {
        this.skipping = false;
      } else {
        this.command(line, onStop);
      }
    }

    if (this.skipping) {
      return;
    }

    this.pending = Buffer.concat([this.pending, rest]);
    if (this.pending.length > CONTROL_LINE_CAP) {
      this.say(`a local link line is over ${String(CONTROL_LINE_CAP)} bytes; it is dropped`);
      this.pending = EMPTY;
      this.skipping = true;
    }
  }

  // A line that is not `stop` is dropped with one stderr line, and the link
  // stays open (decision CH67 rule 7). A close gets the facade killed
  // (decision CH32 rule 7).
  private command(line: Buffer, onStop: () => void): void {
    const command = read("ControlCommand", line, { cap: CONTROL_LINE_CAP });
    if (!command.ok) {
      this.say(`a local link line is refused (${command.rule}); it is dropped`);
      return;
    }

    if ("stop" in command.value) {
      onStop();
      return;
    }

    this.say("a local link line is not stop; it is dropped");
  }
}

/** The local link of the facade under CH. On sbx the facade opens none (decision CH10-4). */
export function guestLink(say: Say): LocalLink {
  return new LocalLink(LOCAL_LINK_SOCKET, FLUE_STOP_MS, say);
}
