// A conversation's history as ACP updates, for `session/load`.
//
//   seed ──► the messages that a rewind kept (`src/lineage.ts`)
//   chunks ──► the last conversation-reset ──► its snapshot's messages
//          └─► each chunk after it ──────────► user text, or the live map
//
// A `conversation-reset` holds the whole conversation at its batch (Flue
// sends one at creation and at each compaction), so the replay starts from
// the last one and adds what came after. The seed comes before all of it:
// the Flue instance of a rewind starts after the kept messages.
//
// A user text ends with one marker line for each file of its prompt
// (`src/attachments.ts`). The replay shows the user text without them,
// then a `resource_link` for each file (`userChunks` of `src/mapper.ts`).
// A kept message of a rewind replays its files in the same way. After the
// last reset, the replay reads the image of each image link of the final
// list, up to `MAX_REPLAY_IMAGE_BYTES` of base64 in all. An image past the
// cap, or one that the workspace no longer holds as written, stays a link.

import type { ConversationStreamChunk } from "@flue/runtime";

import type { SessionUpdate } from "./acp.js";
import { keptOf, keptUpdates } from "./kept.js";
import type { Kept } from "./kept.js";
import type { Position } from "./live.js";
import { base64Bytes } from "./attachments.js";
import type { Marker } from "./attachments.js";
import { chunkToUpdates, imageChunk, MAX_TEXT_BYTES } from "./mapper.js";
import type { ImageLinks, ImageNote } from "./mapper.js";

/** The most base64 bytes of the images of one replay, in all. */
export const MAX_REPLAY_IMAGE_BYTES = 33_554_432;

/** The base64 of the image of a marker, or null: the workspace holds no such image. */
export type ImageReader = (marker: Marker) => string | null;

type Snapshot = Extract<ConversationStreamChunk, { type: "conversation-reset" }>["snapshot"];
type Message = Snapshot["messages"][number];

export interface Replay {
  readonly updates: readonly SessionUpdate[];
  /** The position of the last chunk read, or null for no chunk. */
  readonly last: Position | null;
}

type ReplayTextUpdate = SessionUpdate & {
  readonly sessionUpdate: "agent_message_chunk" | "agent_thought_chunk";
  readonly content: { readonly type: "text"; readonly text: string };
};

interface PendingText {
  readonly first: ReplayTextUpdate;
  readonly parts: string[];
  bytes: number;
}

function isReplayText(update: SessionUpdate): update is ReplayTextUpdate {
  return (
    (update.sessionUpdate === "agent_message_chunk" || update.sessionUpdate === "agent_thought_chunk") &&
    update.content.type === "text"
  );
}

/** Join adjacent text updates without crossing an update or byte boundary. */
function joinText(updates: readonly SessionUpdate[]): SessionUpdate[] {
  const joined: SessionUpdate[] = [];
  let pending: PendingText | null = null;

  const flush = (): void => {
    if (pending === null) {
      return;
    }

    joined.push({
      ...pending.first,
      content: { ...pending.first.content, text: pending.parts.join("") },
    });
    pending = null;
  };

  for (const update of updates) {
    if (!isReplayText(update)) {
      flush();
      joined.push(update);
      continue;
    }

    const bytes = Buffer.byteLength(update.content.text);
    if (
      pending !== null &&
      pending.first.sessionUpdate === update.sessionUpdate &&
      pending.bytes + bytes <= MAX_TEXT_BYTES
    ) {
      pending.parts.push(update.content.text);
      pending.bytes += bytes;
      continue;
    }

    flush();
    pending = { first: update, parts: [update.content.text], bytes };
  }

  flush();
  return joined;
}

/** The updates of one Flue message: its kept form, or none. */
function messageUpdates(message: Message, links: ImageLinks): SessionUpdate[] {
  const kept = keptOf(message);

  return kept === null ? [] : joinText(keptUpdates(kept, links));
}

/** `updates` with the image after each image link, while the cap holds. */
function withImages(updates: readonly SessionUpdate[], notes: ReadonlyMap<SessionUpdate, ImageNote>, images: ImageReader): SessionUpdate[] {
  let room = MAX_REPLAY_IMAGE_BYTES;
  return updates.flatMap((update) => {
    const note = notes.get(update);
    if (note === undefined || base64Bytes(note.marker.size) > room) {
      return [update];
    }

    const data = images(note.marker);
    if (data === null) {
      return [update];
    }

    room -= data.length;
    return [update, imageChunk(note, data)];
  });
}

/** `images` reads the image of a marker line from the workspace (`Attachments.readImage`). */
export function replay(chunks: readonly ConversationStreamChunk[], images: ImageReader, seed: readonly Kept[] = []): Replay {
  // Each image link of the build, read only if it is in the final list.
  const notes = new Map<SessionUpdate, ImageNote>();
  const links: ImageLinks = (link, note) => notes.set(link, note);
  const seeded = seed.flatMap((kept) => joinText(keptUpdates(kept, links)));
  let updates: SessionUpdate[] = [...seeded];
  let last: Position | null = null;
  let pendingDeltas: SessionUpdate[] = [];
  let pendingMessageId: string | null = null;

  const flushDeltas = (): void => {
    updates.push(...joinText(pendingDeltas));
    pendingDeltas = [];
    pendingMessageId = null;
  };

  for (const chunk of chunks) {
    last = chunk.position;
    if (chunk.type === "conversation-reset") {
      pendingDeltas = [];
      pendingMessageId = null;
      updates = [...seeded, ...chunk.snapshot.messages.flatMap((message) => messageUpdates(message, links))];
    } else if (chunk.type === "message-appended") {
      flushDeltas();
      updates.push(...messageUpdates(chunk.message, links));
    } else if (chunk.type === "message-delta") {
      if (pendingMessageId !== null && pendingMessageId !== chunk.messageId) {
        flushDeltas();
      }

      pendingMessageId = chunk.messageId;
      pendingDeltas.push(...chunkToUpdates(chunk));
    } else {
      flushDeltas();
      updates.push(...chunkToUpdates(chunk));
    }
  }

  flushDeltas();

  return { updates: withImages(updates, notes, images), last };
}
