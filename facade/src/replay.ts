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
import { chunkToUpdates, forward, forwardJson, MAX_NAME_BYTES } from "./mapper.js";

type Snapshot = Extract<ConversationStreamChunk, { type: "conversation-reset" }>["snapshot"];
type Message = Snapshot["messages"][number];
type Part = Message["parts"][number];

export interface Replay {
  readonly updates: readonly SessionUpdate[];
  /** The position of the last chunk read, or null for no chunk. */
  readonly last: Position | null;
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

  for (const chunk of chunks) {
    last = chunk.position;
    if (chunk.type === "conversation-reset") {
      updates = chunk.snapshot.messages.flatMap(messageUpdates);
    } else if (chunk.type === "message-appended") {
      updates.push(...messageUpdates(chunk.message));
    } else {
      updates.push(...chunkToUpdates(chunk));
    }
  }

  return { updates, last };
}
