// `session/load` over the facade's own wire: two links on one service, as
// two door connections reach one facade.

import { DatabaseSync } from "node:sqlite";

import { fauxAssistantMessage, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import { defineTool, useModel, useTool } from "@flue/runtime";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import type { SessionService } from "../src/acp.js";
import { sessionService } from "../src/service.js";
import { SessionIndex } from "../src/session-index.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux, startFlue } from "./faux-flue.js";

const NO_HOOKS = { onBeat: () => undefined, onShutdown: () => undefined };

const echo = defineTool({ name: "echo", description: "Answers echoed.", run: () => "echoed" });

function Echoer(): string {
  useModel(FAUX_MODEL);
  useTool(echo);

  return "Use the echo tool.";
}

const flue = await startFlue([Echoer]);
afterAll(() => flue.stop());

let service: SessionService;

beforeEach(() => {
  const index = new SessionIndex(new DatabaseSync(":memory:"));
  service = sessionService({ agent: Echoer, agentName: "Echoer", index, store: flue.store });
});

function link(): Wire {
  return new Wire(facadeApp(NO_HOOKS, service));
}

function load(wire: Wire, sessionId: string): ReturnType<Wire["request"]> {
  return wire.request("session/load", { sessionId, cwd: "/work", mcpServers: [] });
}

/** Each update with its text chunks joined, so a split delta reads as one. */
function joined(updates: Record<string, unknown>[]): unknown[] {
  const out: Record<string, unknown>[] = [];
  for (const update of updates) {
    const previous = out.at(-1);
    const kind = update["sessionUpdate"];
    if (previous !== undefined && previous["sessionUpdate"] === kind && String(kind).endsWith("_chunk")) {
      const text = (previous["content"] as { text: string }).text + (update["content"] as { text: string }).text;
      out[out.length - 1] = { ...previous, content: { type: "text", text } };
    } else {
      out.push(update);
    }
  }

  return out;
}

async function oneTurn(wire: Wire): Promise<string> {
  const id = (await wire.request("session/new", { cwd: "/work", mcpServers: [] })).result?.["sessionId"] as string;
  faux.setResponses([
    fauxAssistantMessage([fauxThinking("Think first."), fauxToolCall("echo", {}, { id: "call-1" })], { stopReason: "toolUse" }),
    fauxAssistantMessage([fauxText("All done.")]),
  ]);
  await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Echo, please." }] });

  return id;
}

describe("session/load", () => {
  it("replays the history as the first link saw it, then answers {}", async () => {
    const first = link();
    const id = await oneTurn(first);
    const second = link();
    const answer = await load(second, id);
    const live = joined(first.updates(id)).filter(
      (update) => (update as Record<string, unknown>)["sessionUpdate"] !== "session_info_update",
    );

    expect(answer.result).toEqual({});
    expect(joined(second.updates(id))).toEqual([
      { sessionUpdate: "user_message_chunk", content: { type: "text", text: "Echo, please." } },
      ...live,
    ]);
  });

  it("puts the loading link on the session's live updates", async () => {
    const first = link();
    const id = await oneTurn(first);
    const second = link();
    await load(second, id);
    const replayed = second.updates(id).length;
    faux.setResponses([fauxAssistantMessage("Again.")]);
    await first.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "More." }] });

    expect(joined(second.updates(id).slice(replayed))).toEqual([
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Again." } },
      { sessionUpdate: "session_info_update", updatedAt: expect.any(String), _meta: { "creche/created_at": expect.any(String) } },
    ]);
  });

  it("replays nothing for a session with no turn", async () => {
    const first = link();
    const id = (await first.request("session/new", { cwd: "/work", mcpServers: [] })).result?.["sessionId"] as string;
    const second = link();

    expect((await load(second, id)).result).toEqual({});
    expect(second.updates(id)).toEqual([]);
  });

  it("refuses an unknown session and a client MCP server", async () => {
    const wire = link();

    expect((await load(wire, "no-such-session")).error?.code).toBe(-32002);
    expect(
      (await wire.request("session/load", { sessionId: "x", cwd: "/work", mcpServers: [{ name: "x", command: "x", args: [], env: [] }] }))
        .error?.code,
    ).toBe(-32602);
  });
});
