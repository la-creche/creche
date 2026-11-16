// `_creche/session/rewind` over the facade's own wire: the cut, the replay
// after it, the next turn on a new Flue instance, the lineage file after a
// restart, and each refusal.

import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

import { fauxAssistantMessage, fauxText } from "@earendil-works/pi-ai";
import type { TranscriptContext } from "@earendil-works/pi-ai";
import { useInstruction, useModel } from "@flue/runtime";
import type { AgentProps } from "@flue/runtime";
import { Earlier } from "agent/agent";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { facadeApp } from "../src/acp.js";
import type { SessionService } from "../src/acp.js";
import { readLineage } from "../src/lineage.js";
import type { RewindOptions } from "../src/rewind.js";
import { sessionService } from "../src/service.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux, freshRewind, startFlue } from "./faux-flue.js";

const INVALID_PARAMS = -32602;
const INVALID_REQUEST = -32600;
const NOT_FOUND = -32002;

const earlier = new Earlier();

/** The instance id, or null on a bare render, where `props.id` throws. */
function instanceOf(props: AgentProps | undefined): string | null {
  try {
    return props?.id ?? null;
  } catch {
    return null;
  }
}

/** The agent of the tests, with the kept transcript in its instructions as `composeAgent` puts it. */
function Rewound(props?: AgentProps): string {
  useModel(FAUX_MODEL);
  const kept = earlier.instructionFor(instanceOf(props));
  if (kept !== null) {
    useInstruction(kept);
  }

  return "Answer in one word.";
}

const flue = await startFlue([Rewound]);
afterAll(() => flue.stop());

let id: string;
let rewind: RewindOptions;
let service: SessionService;
let wire: Wire;

/** A facade start on the lineage `options` gives. */
function serve(options: RewindOptions): SessionService {
  return sessionService({ agent: Rewound, agentName: "Rewound", sessionId: id, link: null, store: flue.store, workspace: flue.workspace, rewind: options });
}

/** One more link on the service, as a second door connection. */
function link(on: SessionService = service): Wire {
  return new Wire(facadeApp(on));
}

beforeEach(() => {
  id = randomUUID();
  rewind = freshRewind(earlier);
  service = serve(rewind);
  wire = link();
});

/** One turn: the prompt `text`, and the faux model answers `reply`. */
async function turn(on: Wire, text: string, reply: string): Promise<void> {
  faux.setResponses([fauxAssistantMessage([fauxText(reply)])]);
  const answer = await on.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text }] });
  expect(answer.result).toEqual({ stopReason: "end_turn" });
}

/** The message id of the user chunk with `text`, as the live echo gave it. */
function messageIdOf(on: Wire, text: string): string {
  const echo = on
    .updates(id)
    .find((update) => update["sessionUpdate"] === "user_message_chunk" && (update["content"] as { text: string }).text === text);
  if (echo === undefined || typeof echo["messageId"] !== "string") {
    throw new Error(`no user chunk with the text ${text}`);
  }

  return echo["messageId"];
}

function cut(on: Wire, messageId: unknown, sessionId: string = id): ReturnType<Wire["request"]> {
  return on.request(vectors.rewind.method, { sessionId, messageId });
}

/** The user and agent texts of a load on a new link. */
async function loaded(on: Wire): Promise<string[]> {
  const answer = await on.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [] });
  expect(answer.error).toBeUndefined();

  return on.updates(id).map((update) => `${String(update["sessionUpdate"])} ${(update["content"] as { text: string }).text}`);
}

describe("_creche/session/rewind", () => {
  it("cuts the session to before the message and replays the kept messages", async () => {
    await turn(wire, "First.", "One.");
    await turn(wire, "Second.", "Two.");
    const first = messageIdOf(wire, "First.");

    expect((await cut(wire, messageIdOf(wire, "Second."))).result).toEqual({});
    expect(await loaded(link())).toEqual(["user_message_chunk First.", "agent_message_chunk One."]);

    // The replay names the kept user message with the id of its echo.
    const again = link();
    await loaded(again);
    expect(messageIdOf(again, "First.")).toBe(first);
  });

  it("runs the next turn on a new instance that reads the kept messages", async () => {
    await turn(wire, "First.", "One.");
    await turn(wire, "Second.", "Two.");
    await cut(wire, messageIdOf(wire, "Second."));

    let seen = "";
    faux.setResponses([
      (context: TranscriptContext) => {
        seen = JSON.stringify(context.messages);
        return fauxAssistantMessage([fauxText("Three.")]);
      },
    ]);
    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Other." }] });

    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(seen).toContain("User: First.");
    expect(seen).toContain("Assistant: One.");
    expect(seen).not.toContain("Second.");
    expect(await loaded(link())).toEqual([
      "user_message_chunk First.",
      "agent_message_chunk One.",
      "user_message_chunk Other.",
      "agent_message_chunk Three.",
    ]);
  });

  it("keeps nothing for a rewind to the first message, and the next turn names the session", async () => {
    await turn(wire, "First.", "One.");
    await cut(wire, messageIdOf(wire, "First."));

    expect(await loaded(link())).toEqual([]);
    await turn(wire, "Fresh start.", "Yes.");
    expect(wire.updates(id).at(-1)).toEqual({ sessionUpdate: "session_info_update", title: "Fresh start.", updatedAt: expect.any(String) });
  });

  it("writes the lineage, so a restarted facade serves the same cut", async () => {
    await turn(wire, "First.", "One.");
    await turn(wire, "Second.", "Two.");
    await cut(wire, messageIdOf(wire, "Second."));
    await turn(wire, "Third.", "Three.");

    const read = await readLineage(rewind.file);
    if (!read.ok) {
      throw new Error(read.detail);
    }

    expect(read.lineage.generation).toBe(1);
    expect(JSON.parse(readFileSync(rewind.file, "utf8"))).toMatchObject({ v: 1, generation: 1 });
    const restarted = link(serve({ ...rewind, lineage: read.lineage }));
    expect(await loaded(restarted)).toEqual([
      "user_message_chunk First.",
      "agent_message_chunk One.",
      "user_message_chunk Third.",
      "agent_message_chunk Three.",
    ]);
  });

  it("cuts into the kept messages of an earlier rewind", async () => {
    await turn(wire, "First.", "One.");
    await turn(wire, "Second.", "Two.");
    const first = messageIdOf(wire, "First.");
    await cut(wire, messageIdOf(wire, "Second."));
    await turn(wire, "Third.", "Three.");

    expect((await cut(wire, first)).result).toEqual({});
    expect(await loaded(link())).toEqual([]);
  });

  it("keeps the thinking level that the session chose", async () => {
    await turn(wire, "First.", "One.");
    await turn(wire, "Second.", "Two.");
    const set = await wire.request("session/set_config_option", { sessionId: id, configId: "mode", value: "high" });
    expect(set.error).toBeUndefined();
    await cut(wire, messageIdOf(wire, "Second."));

    const answer = await link().request("session/load", { sessionId: id, cwd: "/work", mcpServers: [] });
    const [option] = (answer.result?.["configOptions"] ?? []) as { currentValue: string }[];
    expect(option?.currentValue).toBe("high");
  });

  it("refuses a messageId of no user message, an unknown session and bad params", async () => {
    await turn(wire, "First.", "One.");

    expect((await cut(wire, "no-such-message")).error?.code).toBe(INVALID_PARAMS);
    expect((await cut(wire, messageIdOf(wire, "First."), randomUUID())).error?.code).toBe(NOT_FOUND);
    expect((await cut(wire, 7)).error?.code).toBe(INVALID_PARAMS);
    expect((await cut(wire, "x".repeat(257))).error?.code).toBe(INVALID_PARAMS);
    expect(await loaded(link())).toEqual(["user_message_chunk First.", "agent_message_chunk One."]);
  });

  it("refuses a rewind while a prompt runs", async () => {
    await turn(wire, "First.", "One.");
    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    faux.setResponses([
      async () => {
        await held;
        return fauxAssistantMessage([fauxText("Late.")]);
      },
    ]);
    const running = wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Slow." }] });
    while (!wire.updates(id).some((update) => update["sessionUpdate"] === "user_message_chunk" && (update["content"] as { text: string }).text === "Slow.")) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }

    expect((await cut(wire, messageIdOf(wire, "First."))).error?.code).toBe(INVALID_REQUEST);
    release();
    expect((await running).result).toEqual({ stopReason: "end_turn" });
  });

  it("answers as the vector says", () => {
    expect(vectors.rewind.response.result).toEqual({});
    expect(vectors.rewind.request.method).toBe(vectors.rewind.method);
  });
});
