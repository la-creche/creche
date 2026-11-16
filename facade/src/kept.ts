// The visible messages of a conversation, in the facade's own small shape.
// The replay of `session/load` maps each Flue message through it, and a
// rewind keeps a list of them on the session disk (`src/lineage.ts`).
//
//   Flue message ──► keptOf ──► Kept ──► keptUpdates ──► ACP updates
//                                    └─► transcriptOf ──► the text the model reads
//
// Signals, system messages and parts that no ACP update carries stay out.

import type { ConversationStreamChunk } from "@flue/runtime";
import { stringifyStrict } from "agent/strict-json";

import type { SessionUpdate } from "./acp.js";
import { cutToBytes } from "./line.js";
import { forward, forwardJson, MAX_NAME_BYTES, userChunk } from "./mapper.js";

type Snapshot = Extract<ConversationStreamChunk, { type: "conversation-reset" }>["snapshot"];
type Message = Snapshot["messages"][number];
type Part = Message["parts"][number];

/** How a tool call of a kept message ended. */
export type KeptOutcome =
  | { readonly state: "pending" }
  | { readonly state: "done"; readonly output: unknown }
  | { readonly state: "failed"; readonly errorText: string };

export type KeptPart =
  | { readonly type: "text"; readonly text: string }
  | { readonly type: "reasoning"; readonly text: string }
  | {
      readonly type: "tool";
      readonly toolCallId: string;
      readonly toolName: string;
      readonly input: unknown;
      readonly outcome: KeptOutcome;
    };

export interface Kept {
  /** Flue's message id: the `messageId` of a user chunk. */
  readonly id: string;
  readonly role: "user" | "assistant";
  readonly parts: readonly KeptPart[];
}

/** The most bytes of one tool output in the transcript. The replay keeps the whole output. */
export const MAX_TRANSCRIPT_OUTPUT_BYTES = 4096;

/**
 * Each text is well formed and cut, and each JSON value fits the cap, as
 * the replay forwards them. So a kept list writes as strict JSON.
 */
function keptPart(part: Part): KeptPart | null {
  switch (part.type) {
    case "text":
      return { type: "text", text: forward(part.text) };
    case "reasoning":
      return { type: "reasoning", text: forward(part.text) };
    case "dynamic-tool": {
      const call = {
        type: "tool" as const,
        toolCallId: forward(part.toolCallId, MAX_NAME_BYTES),
        toolName: forward(part.toolName, MAX_NAME_BYTES),
        input: forwardJson(part.input),
      };
      if (part.state === "input-available") {
        return { ...call, outcome: { state: "pending" } };
      }

      return part.state === "output-error"
        ? { ...call, outcome: { state: "failed", errorText: forward(part.errorText) } }
        : { ...call, outcome: { state: "done", output: forwardJson(part.output) } };
    }
    // A file, or a named data part: no ACP update carries it.
    default:
      return null;
  }
}

/** The kept form of a visible user or assistant message, or null. A user message keeps its text only. */
export function keptOf(message: Message): Kept | null {
  if (message.display !== "visible") {
    return null;
  }

  switch (message.role) {
    case "user":
      return {
        id: forward(message.id, MAX_NAME_BYTES),
        role: "user",
        parts: message.parts.flatMap((part) => (part.type === "text" ? [{ type: "text" as const, text: forward(part.text) }] : [])),
      };
    case "assistant":
      return { id: forward(message.id, MAX_NAME_BYTES), role: "assistant", parts: message.parts.flatMap((part) => keptPart(part) ?? []) };
    case "system":
      return null;
    default: {
      const unknown: never = message.role;
      return unknown;
    }
  }
}

function textOf(output: unknown): string {
  return typeof output === "string" ? output : JSON.stringify(forwardJson(output ?? null));
}

function partUpdates(part: KeptPart): SessionUpdate[] {
  switch (part.type) {
    case "text":
      return [{ sessionUpdate: "agent_message_chunk", content: { type: "text", text: forward(part.text) } }];
    case "reasoning":
      return [{ sessionUpdate: "agent_thought_chunk", content: { type: "text", text: forward(part.text) } }];
    case "tool": {
      const toolCallId = forward(part.toolCallId, MAX_NAME_BYTES);
      const call: SessionUpdate = {
        sessionUpdate: "tool_call",
        toolCallId,
        title: forward(part.toolName, MAX_NAME_BYTES),
        kind: "other",
        status: "in_progress",
        rawInput: forwardJson(part.input),
      };
      const { outcome } = part;
      if (outcome.state === "pending") {
        return [call];
      }

      const failed = outcome.state === "failed";
      const text = failed ? outcome.errorText : textOf(outcome.output);
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
    default: {
      const unknown: never = part;
      return unknown;
    }
  }
}

/** The ACP updates of one kept message. Each user chunk carries the message id. */
export function keptUpdates(kept: Kept): SessionUpdate[] {
  if (kept.role === "user") {
    return kept.parts.flatMap((part) => (part.type === "text" ? [userChunk(kept.id, part.text)] : []));
  }

  return kept.parts.flatMap(partUpdates);
}

function jsonText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }

  const strict = stringifyStrict(value ?? null);
  return strict.ok ? strict.text : String(value);
}

function partLine(part: KeptPart): string {
  switch (part.type) {
    case "text":
      return part.text;
    case "reasoning":
      return "";
    case "tool": {
      const call = `[tool call ${part.toolName} with input ${jsonText(part.input)}]`;
      const { outcome } = part;
      if (outcome.state === "pending") {
        return `${call}\n[no result]`;
      }

      const result = outcome.state === "failed" ? `failed: ${outcome.errorText}` : jsonText(outcome.output);
      return `${call}\n[result: ${cutToBytes(result, MAX_TRANSCRIPT_OUTPUT_BYTES)}]`;
    }
    default: {
      const unknown: never = part;
      return unknown;
    }
  }
}

/**
 * The kept messages as one text, for the model. Reasoning stays out: the
 * model did not read its own thoughts in the next turn either.
 *
 *   User: Echo, please.
 *
 *   Assistant: [tool call echo with input {}]
 *   [result: echoed]
 *   All done.
 */
export function transcriptOf(kept: readonly Kept[]): string {
  return kept
    .map((message) => {
      const body = message.parts.map(partLine).filter((line) => line.length > 0).join("\n");
      return `${message.role === "user" ? "User" : "Assistant"}: ${body}`;
    })
    .join("\n\n");
}
