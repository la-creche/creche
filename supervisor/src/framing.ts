// Contract 03 §2. The record layer: bytes in, whole records out.
//
//   chunk ──► LineReader ──► onRecord(line)        a complete record
//                        └─► onOversize(bytes)     a record past the limit
//
// LF is the ONLY delimiter. Node's `readline` also splits on U+2028 and
// U+2029, which are legal inside JSON strings, so a readline-based reader
// turns one legal pi delta into three unparsable fragments (§2 rule 4).
// Probe 0a's fake pi emits exactly that delta so the defect fails a test here
// instead of surviving to the host.

import { MAX_LINE_BYTES } from "./constants.js";

const LF = "\n";
const CR = "\r";

export type RecordHandler = (line: string) => void;
export type OversizeHandler = (bytes: number) => void;

/** UTF-8 byte length, which is what §2 rule 5's limit is measured in. */
export function byteLength(text: string): number {
  return Buffer.byteLength(text, "utf8");
}

/**
 * Splits a stream into LF-delimited records and refuses oversize ones.
 *
 * An oversize record is discarded up to and including its LF, so the channel
 * resynchronises at the next record: one bad line must never cost the other
 * sessions their turns. Probe 0a measured exactly this, A8: one byte over was
 * refused and the channel stayed usable.
 */
export class LineReader {
  private buffer = "";
  private discarding = false;

  public constructor(
    private readonly onRecord: RecordHandler,
    private readonly onOversize: OversizeHandler,
    private readonly maxBytes: number = MAX_LINE_BYTES,
  ) {}

  public feed(chunk: string): void {
    const parts = (this.buffer + chunk).split(LF);
    this.buffer = parts.pop() ?? "";

    for (const part of parts) {
      this.take(part);
    }

    // The fragment still waiting for its LF counts toward the limit too, or a
    // multi-chunk oversize line grows this buffer without bound.
    if (this.discarding || byteLength(this.buffer) <= this.maxBytes) {
      if (this.discarding) {
        this.buffer = "";
      }

      return;
    }

    this.onOversize(byteLength(this.buffer));
    this.buffer = "";
    this.discarding = true;
  }

  /** Flushes a trailing record that arrived without its LF, at end of stream. */
  public end(): void {
    if (this.discarding || this.buffer.length === 0) {
      this.buffer = "";
      return;
    }

    this.take(this.buffer);
    this.buffer = "";
  }

  private take(part: string): void {
    // The tail of a line already refused. Its LF ends the discard, nothing else.
    if (this.discarding) {
      this.discarding = false;
      return;
    }

    const line = part.endsWith(CR) ? part.slice(0, -1) : part;
    if (line.length === 0) {
      return;
    }

    const bytes = byteLength(line);
    if (bytes > this.maxBytes) {
      this.onOversize(bytes);
      return;
    }

    this.onRecord(line);
  }
}
