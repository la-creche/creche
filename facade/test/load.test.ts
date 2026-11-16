// `session/load` and `session/resume` over the facade's own wire: two links
// on one service, as two door connections reach one facade.

import { randomUUID } from "node:crypto";

import { fauxAssistantMessage, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import { defineTool, useModel, useTool } from "@flue/runtime";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import type { SessionService } from "../src/acp.js";
import { sessionService } from "../src/service.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux, freshRewind, startFlue } from "./faux-flue.js";

// The echo tool waits for `held`, so a test can keep a turn running.
let held: Promise<void> = Promise.resolve();
const echo = defineTool({
  name: "echo",
  description: "Answers echoed.",
  run: async () => {
    await held;
    return "echoed";
  },
});

function Echoer(): string {
  useModel(FAUX_MODEL);
  useTool(echo);

  return "Use the echo tool.";
}

const flue = await startFlue([Echoer]);
afterAll(() => flue.stop());

let id: string;
let service: SessionService;

// Each test gets a session of its own: one VM's id, on the one Flue of the file.
beforeEach(() => {
  id = randomUUID();
  service = sessionService({ agent: Echoer, agentName: "Echoer", sessionId: id, link: null, store: flue.store, rewind: freshRewind() });
});

function link(): Wire {
  return new Wire(facadeApp(service));
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

/** Resolves when the link has an update of the kind `kind`. */
async function sawUpdate(wire: Wire, kind: string): Promise<void> {
  while (!wire.updates(id).some((update) => update["sessionUpdate"] === kind)) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

function oneTurn(wire: Wire): Promise<unknown> {
  faux.setResponses([
    fauxAssistantMessage([fauxThinking("Think first."), fauxToolCall("echo", {}, { id: "call-1" })], { stopReason: "toolUse" }),
    fauxAssistantMessage([fauxText("All done.")]),
  ]);
  return wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Echo, please." }] });
}

describe("session/load", () => {
  it("replays the history as the first link saw it, then answers {}", async () => {
    const first = link();
    await oneTurn(first);
    const second = link();
    const answer = await load(second, id);
    const live = joined(first.updates(id)).filter(
      (update) => (update as Record<string, unknown>)["sessionUpdate"] !== "session_info_update",
    );

    // The live echo and the replay name the user message with one id.
    expect(answer.result).toEqual({});
    expect(joined(second.updates(id))).toEqual(live);
    expect(live[0]).toEqual({ sessionUpdate: "user_message_chunk", messageId: expect.any(String), content: { type: "text", text: "Echo, please." } });
  });

  it("puts the loading link on the session's live updates", async () => {
    const first = link();
    await oneTurn(first);
    const second = link();
    await load(second, id);
    const replayed = second.updates(id).length;
    faux.setResponses([fauxAssistantMessage("Again.")]);
    await first.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "More." }] });

    expect(joined(second.updates(id).slice(replayed))).toEqual([
      { sessionUpdate: "user_message_chunk", messageId: expect.any(String), content: { type: "text", text: "More." } },
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Again." } },
      { sessionUpdate: "session_info_update", updatedAt: expect.any(String) },
    ]);
  });

  it("replays nothing for a session with no turn", async () => {
    const wire = link();

    expect((await load(wire, id)).result).toEqual({});
    expect(wire.updates(id)).toEqual([]);
  });

  it("refuses an unknown session and a client MCP server", async () => {
    const wire = link();

    expect((await load(wire, randomUUID())).error?.code).toBe(-32002);
    expect(
      (await wire.request("session/load", { sessionId: "x", cwd: "/work", mcpServers: [{ name: "x", command: "x", args: [], env: [] }] }))
        .error?.code,
    ).toBe(-32602);
  });
});

describe("session/resume", () => {
  // Panda's resume: no `mcpServers`, which is optional in a resume.
  function resume(wire: Wire, sessionId: string): ReturnType<Wire["request"]> {
    return wire.request("session/resume", { sessionId, cwd: "/work" });
  }

  it("answers {} and replays no history", async () => {
    await oneTurn(link());
    const second = link();

    expect((await resume(second, id)).result).toEqual({});
    expect(second.updates(id)).toEqual([]);
  });

  it("puts the resuming link on the session's live updates", async () => {
    const first = link();
    await oneTurn(first);
    const second = link();
    await resume(second, id);
    faux.setResponses([fauxAssistantMessage("Again.")]);
    await first.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "More." }] });

    expect(joined(second.updates(id))).toEqual([
      { sessionUpdate: "user_message_chunk", messageId: expect.any(String), content: { type: "text", text: "More." } },
      { sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Again." } },
      { sessionUpdate: "session_info_update", updatedAt: expect.any(String) },
    ]);
  });

  it("streams the rest of a turn that runs at the resume", async () => {
    let free = (): void => undefined;
    held = new Promise((resolve) => (free = resolve));
    const first = link();
    const turn = oneTurn(first);
    await sawUpdate(first, "tool_call");
    const second = link();
    await resume(second, id);
    const seen = first.updates(id).length;
    free();
    await turn;

    expect(second.updates(id)).toEqual(first.updates(id).slice(seen));
    expect(joined(second.updates(id))).toEqual(
      expect.arrayContaining([{ sessionUpdate: "agent_message_chunk", content: { type: "text", text: "All done." } }]),
    );
  });

  it("refuses an unknown session, a relative cwd and a client MCP server", async () => {
    const wire = link();

    expect((await resume(wire, randomUUID())).error?.code).toBe(-32002);
    expect((await wire.request("session/resume", { sessionId: id, cwd: "work" })).error?.code).toBe(-32602);
    expect(
      (await wire.request("session/resume", { sessionId: id, cwd: "/work", mcpServers: [{ name: "x", command: "x", args: [], env: [] }] }))
        .error?.code,
    ).toBe(-32602);
  });
});
