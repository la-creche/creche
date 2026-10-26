// `session/prompt` and `session/cancel` over the facade's own wire, on Flue
// with an in-memory store and the faux model.

import { DatabaseSync } from "node:sqlite";

import { fauxAssistantMessage, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import { defineTool, useModel, useTool } from "@flue/runtime";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import { sessionService } from "../src/service.js";
import { SessionIndex } from "../src/session-index.js";
import { Sessions } from "../src/sessions.js";
import { Turns } from "../src/turns.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux } from "./faux-flue.js";
import { startFlue } from "./faux-flue.js";

const NO_HOOKS = { onBeat: () => undefined, onShutdown: () => undefined };
const INVALID_PARAMS = -32602;
const INVALID_REQUEST = -32600;
const INTERNAL_ERROR = -32603;

const echo = defineTool({ name: "echo", description: "Answers echoed.", run: () => "echoed" });

/** The agent of this file: the faux model and one tool. */
function Echoer(): string {
  useModel(FAUX_MODEL);
  useTool(echo);

  return "Use the echo tool.";
}
Echoer.durability = { maxAttempts: 1 };

const flue = await startFlue([Echoer]);
afterAll(() => flue.stop());

let wire: Wire;

beforeEach(() => {
  const sessions = new Sessions(Echoer, new SessionIndex(new DatabaseSync(":memory:")));
  wire = new Wire(facadeApp(NO_HOOKS, sessionService(sessions, new Turns(sessions))));
});

async function newSession(): Promise<string> {
  return (await wire.request("session/new", { cwd: "/work", mcpServers: [] })).result?.["sessionId"] as string;
}

function prompt(sessionId: string, text: string): ReturnType<Wire["request"]> {
  return wire.request("session/prompt", { sessionId, prompt: [{ type: "text", text }] });
}

/** The kinds of the updates, with neighbouring text chunks of one kind joined. */
function story(updates: Record<string, unknown>[]): string[] {
  const kinds: string[] = [];
  for (const update of updates) {
    const kind = String(update["sessionUpdate"]);
    const status = update["status"] === undefined ? "" : ` ${String(update["status"])}`;
    if (kinds.at(-1) !== kind || !kind.endsWith("_chunk")) {
      kinds.push(`${kind}${status}`);
    }
  }

  return kinds;
}

describe("session/prompt", () => {
  it("streams the turn as updates and ends with end_turn", async () => {
    const id = await newSession();
    faux.setResponses([
      fauxAssistantMessage([fauxThinking("Think first."), fauxToolCall("echo", {}, { id: "call-1" })], { stopReason: "toolUse" }),
      fauxAssistantMessage([fauxText("All done.")]),
    ]);

    const answer = await prompt(id, "Echo, please.");
    const updates = wire.updates(id);
    const said = updates
      .filter((update) => update["sessionUpdate"] === "agent_message_chunk")
      .map((update) => (update["content"] as { text: string }).text)
      .join("");

    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(story(updates)).toEqual([
      "agent_thought_chunk",
      "tool_call in_progress",
      "tool_call_update completed",
      "agent_message_chunk",
      "session_info_update",
    ]);
    expect(said).toBe("All done.");
    expect(updates.at(-1)).toEqual({ sessionUpdate: "session_info_update", title: "Echo, please.", updatedAt: expect.any(String) });
  });

  it("sends only the second turn's updates on the second turn", async () => {
    const id = await newSession();
    faux.setResponses([fauxAssistantMessage("One."), fauxAssistantMessage("Two.")]);
    await prompt(id, "First.");
    const before = wire.updates(id).length;
    await prompt(id, "Second.");
    const second = wire.updates(id).slice(before);

    expect(story(second)).toEqual(["agent_message_chunk", "session_info_update"]);
    expect(second.at(-1)).toEqual({ sessionUpdate: "session_info_update", updatedAt: expect.any(String) });
  });

  it("answers a failed turn with a JSON-RPC error, never a stop reason", async () => {
    const id = await newSession();
    faux.setResponses([fauxAssistantMessage("", { stopReason: "error", errorMessage: "model down" })]);
    const answer = await prompt(id, "Fail.");

    expect(answer.result).toBeUndefined();
    expect(answer.error?.code).toBe(INTERNAL_ERROR);
  });

  it("refuses an unknown session, an empty prompt and an image", async () => {
    const id = await newSession();
    const answers = [
      await prompt("no-such-session", "Hi"),
      await prompt(id, "  "),
      await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "image", data: "", mimeType: "image/png" }] }),
    ];

    expect(answers.map((answer) => answer.error?.code)).toEqual([-32002, INVALID_PARAMS, INVALID_PARAMS]);
  });
});

describe("session/cancel", () => {
  it("aborts the running turn, and the prompt answers cancelled", async () => {
    const id = await newSession();
    let release: () => void = () => undefined;
    let started: () => void = () => undefined;
    const running = new Promise<void>((resolve) => (started = resolve));
    faux.setResponses([
      async () => {
        started();
        await new Promise<void>((resolve) => (release = resolve));
        return fauxAssistantMessage("Too late.");
      },
    ]);

    const answer = prompt(id, "Take your time.");
    await running;
    const second = await prompt(id, "Me too.");
    wire.notify("session/cancel", { sessionId: id });
    setTimeout(() => release(), 200);

    expect(second.error?.code).toBe(INVALID_REQUEST);
    expect((await answer).result).toEqual({ stopReason: "cancelled" });
  });
});
