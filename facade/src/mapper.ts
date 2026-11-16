// Flue chunks to ACP updates. A Flue record is data: every string that the
// facade forwards is made well formed and cut to a byte cap.
//
//   ConversationStreamChunk ──► chunkToUpdates ──► SessionUpdate[]
//
// The switch has a `never` default, so a chunk kind that a new Flue adds
// breaks the build here, not the wire.

import type { ConversationStreamChunk } from "@flue/runtime";
import { stringifyStrict } from "agent/strict-json";

import type { SessionUpdate } from "./acp.js";
import { cutToBytes } from "./line.js";

/** The largest forwarded text, in UTF-8 bytes: the playpen's entry cap. */
export const MAX_TEXT_BYTES = 65_536;

/** The largest forwarded id or name, in UTF-8 bytes. */
export const MAX_NAME_BYTES = 256;

/** `text`, well formed and cut to `maxBytes`. */
export function forward(text: string, maxBytes: number = MAX_TEXT_BYTES): string {
  return cutToBytes(text.toWellFormed(), maxBytes);
}

/** A JSON value that fits the cap as it is, or its text, cut. */
export function forwardJson(value: unknown): unknown {
  const strict = stringifyStrict(value ?? null);
  if (!strict.ok) {
    return forward(String(value));
  }

  return Buffer.byteLength(strict.text) <= MAX_TEXT_BYTES ? value : forward(strict.text);
}

/** The text of a tool's output: a string as it is, else its JSON text. */
function outputText(output: unknown): string {
  if (typeof output === "string") {
    return output;
  }

  const strict = stringifyStrict(output ?? null);

  return strict.ok ? strict.text : "";
}

function text(sessionUpdate: "agent_message_chunk" | "agent_thought_chunk", delta: string): SessionUpdate {
  return { sessionUpdate, content: { type: "text", text: forward(delta) } };
}

/**
 * One text of a user message. The `messageId` is Flue's id of the message:
 * a client names it to rewind the session to before that message
 * (`src/rewind.ts`).
 */
export function userChunk(messageId: string, userText: string): SessionUpdate {
  return {
    sessionUpdate: "user_message_chunk",
    messageId: forward(messageId, MAX_NAME_BYTES),
    content: { type: "text", text: forward(userText) },
  };
}

type Appended = Extract<ConversationStreamChunk, { type: "message-appended" }>["message"];

/** The visible user text of an appended message: the echo of a prompt, with its id. */
function echoOf(message: Appended): SessionUpdate[] {
  if (message.role !== "user" || message.display !== "visible") {
    return [];
  }

  return message.parts.flatMap((part) => (part.type === "text" ? [userChunk(message.id, part.text)] : []));
}

export function chunkToUpdates(chunk: ConversationStreamChunk): SessionUpdate[] {
  switch (chunk.type) {
    case "message-delta":
      return [text(chunk.kind === "text" ? "agent_message_chunk" : "agent_thought_chunk", chunk.delta)];
    case "tool-input":
      return [
        {
          sessionUpdate: "tool_call",
          toolCallId: forward(chunk.toolCallId, MAX_NAME_BYTES),
          title: forward(chunk.toolName, MAX_NAME_BYTES),
          kind: "other",
          status: "in_progress",
          rawInput: forwardJson(chunk.input),
        },
      ];
    case "tool-output":
      return [
        {
          sessionUpdate: "tool_call_update",
          toolCallId: forward(chunk.toolCallId, MAX_NAME_BYTES),
          status: "completed",
          content: [{ type: "content", content: { type: "text", text: forward(outputText(chunk.output)) } }],
        },
      ];
    case "tool-output-error":
      return [
        {
          sessionUpdate: "tool_call_update",
          toolCallId: forward(chunk.toolCallId, MAX_NAME_BYTES),
          status: "failed",
          content: [{ type: "content", content: { type: "text", text: forward(chunk.errorText) } }],
        },
      ];
    // The echo of the prompt gives the client the id of its message.
    case "message-appended":
      return echoOf(chunk.message);
    // A reset or a boundary carries no new content for a live turn.
    case "conversation-reset":
    case "message-started":
    case "message-metadata":
    case "data-part":
    case "message-completed":
    case "submission-settled":
      return [];
    default: {
      const unknown: never = chunk;
      return unknown;
    }
  }
}

/**
 * The chunks of one submission. `handle.read()` replays the whole
 * conversation from its start, so the chunks of earlier turns pass by too.
 * A message is this submission's when its `message-started` names it, and
 * a tool call is when its `tool-input` sits on such a message. The user
 * message of the submission is its own when Flue appends it.
 */
export class SubmissionChunks {
  private readonly messages = new Set<string>();
  private readonly toolCalls = new Set<string>();

  public constructor(private readonly submissionId: string) {}

  public accept(chunk: ConversationStreamChunk): boolean {
    switch (chunk.type) {
      case "message-started":
        if (chunk.submissionId === this.submissionId) {
          this.messages.add(chunk.messageId);
        }
        return this.messages.has(chunk.messageId);
      case "message-delta":
      case "message-metadata":
      case "data-part":
      case "message-completed":
        return this.messages.has(chunk.messageId);
      case "tool-input":
        if (this.messages.has(chunk.messageId)) {
          this.toolCalls.add(chunk.toolCallId);
        }
        return this.toolCalls.has(chunk.toolCallId);
      case "tool-output":
      case "tool-output-error":
        return this.toolCalls.has(chunk.toolCallId);
      case "submission-settled":
        return chunk.submissionId === this.submissionId;
      // The prompt of this submission: its echo carries its message id.
      case "message-appended":
        return chunk.message.role === "user" && chunk.message.submissionId === this.submissionId;
      case "conversation-reset":
        return false;
      default: {
        const unknown: never = chunk;
        return unknown;
      }
    }
  }
}
