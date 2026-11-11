// A conversation's history as ACP updates, for `session/load`.
//
//   chunks ──► the last conversation-reset ──► its snapshot's messages
//          └─► each chunk after it ──────────► user text, or the live map
//
// A `conversation-reset` holds the whole conversation at its batch (Flue
// sends one at creation and at each compaction), so the replay starts from
// the last one and adds what came after.

import type { ConversationStreamChunk } from "@flue/runtime";

import type { SessionUpdate } from "./acp.js";
import type { Position } from "./live.js";
import { chunkToUpdates, forward, forwardJson, MAX_NAME_BYTES, MAX_TEXT_BYTES } from "./mapper.js";

type Snapshot = Extract<ConversationStreamChunk, { type: "conversation-reset" }>["snapshot"];
type Message = Snapshot["messages"][number];
type Part = Message["parts"][number];

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

function textOf(output: unknown): string {
  return typeof output === "string" ? output : JSON.stringify(forwardJson(output ?? null));
}

function partUpdates(part: Part): SessionUpdate[] {
  switch (part.type) {
    case "text":
      return [{ sessionUpdate: "agent_message_chunk", content: { type: "text", text: forward(part.text) } }];
    case "reasoning":
      return [{ sessionUpdate: "agent_thought_chunk", content: { type: "text", text: forward(part.text) } }];
    case "dynamic-tool": {
      const toolCallId = forward(part.toolCallId, MAX_NAME_BYTES);
      const call: SessionUpdate = {
        sessionUpdate: "tool_call",
        toolCallId,
        title: forward(part.toolName, MAX_NAME_BYTES),
        kind: "other",
        status: "in_progress",
        rawInput: forwardJson(part.input),
      };
      if (part.state === "input-available") {
        return [call];
      }

      const failed = part.state === "output-error";
      const text = failed ? part.errorText : textOf(part.output);
      return [
        call,
        {
          sessionUpdate: "tool_call_update",
          toolCallId,
          status: failed ? "failed" : "completed",
          content: [{ type: "content", content: { type: "text", text: forward(text) } }],
        },
      ];
    }
    // A file, or a named data part: no ACP update carries it.
    default:
      return [];
  }
}

/** The visible user and assistant messages. Signals and plumbing stay out. */
export function messageUpdates(message: Message): SessionUpdate[] {
  if (message.display !== "visible") {
    return [];
  }

  switch (message.role) {
    case "user":
      return message.parts.flatMap((part) =>
        part.type === "text"
          ? [{ sessionUpdate: "user_message_chunk" as const, content: { type: "text" as const, text: forward(part.text) } }]
          : [],
      );
    case "assistant":
      return message.parts.flatMap(partUpdates);
    case "system":
      return [];
    default: {
      const unknown: never = message.role;
      return unknown;
    }
  }
}

export function replay(chunks: readonly ConversationStreamChunk[]): Replay {
  let updates: SessionUpdate[] = [];
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
      updates = chunk.snapshot.messages.flatMap((message) => joinText(messageUpdates(message)));
    } else if (chunk.type === "message-appended") {
      flushDeltas();
      updates.push(...joinText(messageUpdates(chunk.message)));
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
