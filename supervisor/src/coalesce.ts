// Contract 03 §9 rule 4. Consecutive `text_delta` events, and consecutive
// `thinking_delta` events, that share one `contentIndex` inside one message
// may be merged into one event per window. Deltas concatenate, so this is
// lossless.
//
// It must NEVER be applied to any other event type and never across a
// `message_end`. Coalescing is optional: probe 0a's A1 ran 5 concurrent
// sessions over one channel with 0 gaps, 0 duplicates and 0 unparsable lines,
// so nothing forces it, and `coalesce_ms` 0 turns it off.
//
// It runs BEFORE `turn_seq` is assigned, or the sequence would have gaps.

import { PI_MESSAGE_UPDATE, PI_TEXT_DELTA, PI_THINKING_DELTA } from "./constants.js";

type Event = Record<string, unknown>;

interface Delta {
  readonly kind: string;
  readonly index: number;
  readonly text: string;
}

const MERGEABLE = new Set([PI_TEXT_DELTA, PI_THINKING_DELTA]);

/** Reads the delta this event carries, or null when it is not mergeable. */
function readDelta(event: Event): Delta | null {
  if (event["type"] !== PI_MESSAGE_UPDATE) {
    return null;
  }

  const inner = event["assistantMessageEvent"];
  if (typeof inner !== "object" || inner === null || Array.isArray(inner)) {
    return null;
  }

  const raw = inner as Record<string, unknown>;
  const kind = raw["type"];
  const index = raw["contentIndex"];
  const text = raw["delta"];
  if (typeof kind !== "string" || !MERGEABLE.has(kind)) {
    return null;
  }
  if (typeof index !== "number" || typeof text !== "string") {
    return null;
  }

  return { kind, index, text };
}

/** Builds the merged event: the newest event's fields, the joined text. */
function merge(newest: Event, text: string): Event {
  const inner = newest["assistantMessageEvent"] as Record<string, unknown>;

  return { ...newest, assistantMessageEvent: { ...inner, delta: text } };
}

export class DeltaCoalescer {
  private held: Event | null = null;
  private heldDelta: Delta | null = null;
  private text = "";
  private timer: NodeJS.Timeout | null = null;

  public constructor(
    private readonly windowMs: number,
    private readonly emit: (event: Event) => void,
  ) {}

  public push(event: Event): void {
    if (this.windowMs <= 0) {
      this.emit(event);
      return;
    }

    const delta = readDelta(event);
    if (delta === null) {
      // A `message_end` or any other event closes the run of deltas (§9 rule 4).
      this.flush();
      this.emit(event);
      return;
    }

    const held = this.heldDelta;
    if (held !== null && held.kind === delta.kind && held.index === delta.index) {
      this.text += delta.text;
      this.held = event;
      this.heldDelta = delta;
      return;
    }

    this.flush();
    this.hold(event, delta);
  }

  /** Emits whatever is held. Called on any boundary, and at turn end. */
  public flush(): void {
    this.clearTimer();

    const held = this.held;
    if (held === null) {
      return;
    }

    const text = this.text;
    this.held = null;
    this.heldDelta = null;
    this.text = "";

    this.emit(merge(held, text));
  }

  public dispose(): void {
    this.clearTimer();
    this.held = null;
    this.heldDelta = null;
    this.text = "";
  }

  private hold(event: Event, delta: Delta): void {
    this.held = event;
    this.heldDelta = delta;
    this.text = delta.text;
    this.timer = setTimeout(() => this.flush(), this.windowMs);
  }

  private clearTimer(): void {
    if (this.timer === null) {
      return;
    }

    clearTimeout(this.timer);
    this.timer = null;
  }
}
