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

import type { ConversationStreamChunk } from "@flue/runtime";

import type { SessionUpdate } from "./acp.js";
import { keptOf, keptUpdates } from "./kept.js";
import type { Kept } from "./kept.js";
import type { Position } from "./live.js";
import { chunkToUpdates, MAX_TEXT_BYTES } from "./mapper.js";

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
function messageUpdates(message: Message): SessionUpdate[] {
  const kept = keptOf(message);

  return kept === null ? [] : joinText(keptUpdates(kept));
}

export function replay(chunks: readonly ConversationStreamChunk[], seed: readonly Kept[] = []): Replay {
  const seeded = seed.flatMap((kept) => joinText(keptUpdates(kept)));
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
      updates = [...seeded, ...chunk.snapshot.messages.flatMap(messageUpdates)];
    } else if (chunk.type === "message-appended") {
      flushDeltas();
      updates.push(...messageUpdates(chunk.message));
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

  return { updates, last };
}
