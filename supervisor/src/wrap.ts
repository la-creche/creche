// Contract 03 §8. A wrapped pi event over 256 KiB is truncated, never dropped
// and never emitted whole. The supervisor keeps `event.type` and the scalars,
// drops the largest field, and marks the result.
//
// The host applies the same cap from the other side (§13 rule 6), so an event
// that arrives truncated is recorded truncated. Nothing reconstructs it.

import { MAX_EVENT_BYTES } from "./constants.js";
import { byteLength } from "./framing.js";

type Event = Record<string, unknown>;

const KEEP = new Set(["type", "truncated", "original_bytes"]);

function sizeOf(value: unknown): number {
  return byteLength(JSON.stringify(value) ?? "");
}

/** The name of the largest droppable field, or null when none is left. */
function largestField(event: Event): string | null {
  let name: string | null = null;
  let size = -1;

  for (const [key, value] of Object.entries(event)) {
    if (KEEP.has(key)) {
      continue;
    }

    const bytes = sizeOf(value);
    if (bytes > size) {
      size = bytes;
      name = key;
    }
  }

  return name;
}

/**
 * Returns the event unchanged when it fits, or a marked, shrunken copy.
 *
 * Dropping proceeds largest first, which keeps the scalars in practice because
 * a scalar is small next to a message body. A single giant string field is the
 * one case where a scalar goes, and it has to: §8 forbids emitting a line the
 * supervisor knows is over the limit.
 */
export function capEvent(event: Event, maxBytes: number = MAX_EVENT_BYTES): Event {
  const original = sizeOf(event);
  if (original <= maxBytes) {
    return event;
  }

  const shrunk: Event = { ...event, truncated: true, original_bytes: original };
  for (;;) {
    if (sizeOf(shrunk) <= maxBytes) {
      return shrunk;
    }

    const name = largestField(shrunk);
    if (name === null) {
      return { type: shrunk["type"], truncated: true, original_bytes: original };
    }

    delete shrunk[name];
  }
}
