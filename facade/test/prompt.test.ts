// `session/prompt` and `session/cancel` over the facade's own wire, on Flue
// with an in-memory store and the faux model.

import { createHash, randomUUID } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { fauxAssistantMessage, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import { defineTool, useModel, useTool } from "@flue/runtime";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import { MAX_FILE_BYTES } from "../src/attachments.js";
import { sessionService } from "../src/service.js";
import { Wire } from "./acp-wire.js";
import { FAUX_MODEL, faux, freshRewind } from "./faux-flue.js";
import { startFlue } from "./faux-flue.js";

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

let id: string;
let wire: Wire;

// Each test gets a session of its own: one VM's id, on the one Flue of the file.
beforeEach(() => {
  id = randomUUID();
  wire = new Wire(facadeApp(sessionService({ agent: Echoer, agentName: "Echoer", sessionId: id, link: null, store: flue.store, workspace: flue.workspace, rewind: freshRewind() })));
});

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
      "user_message_chunk",
      "agent_thought_chunk",
      "tool_call in_progress",
      "tool_call_update completed",
      "agent_message_chunk",
      "session_info_update",
    ]);
    expect(said).toBe("All done.");
    // The echo of the prompt carries Flue's message id, which a rewind names.
    expect(updates[0]).toEqual({
      sessionUpdate: "user_message_chunk",
      messageId: expect.any(String),
      content: { type: "text", text: "Echo, please." },
    });
    expect(updates.at(-1)).toEqual({ sessionUpdate: "session_info_update", title: "Echo, please.", updatedAt: expect.any(String) });
  });

  it("sends only the second turn's updates on the second turn, with no title", async () => {
    faux.setResponses([fauxAssistantMessage("One."), fauxAssistantMessage("Two.")]);
    await prompt(id, "First.");
    const before = wire.updates(id).length;
    await prompt(id, "Second.");
    const second = wire.updates(id).slice(before);

    expect(story(second)).toEqual(["user_message_chunk", "agent_message_chunk", "session_info_update"]);
    expect(second.at(-1)).toEqual({ sessionUpdate: "session_info_update", updatedAt: expect.any(String) });
  });

  it("answers a failed turn with a JSON-RPC error, never a stop reason", async () => {
    faux.setResponses([fauxAssistantMessage("", { stopReason: "error", errorMessage: "model down" })]);
    const answer = await prompt(id, "Fail.");

    expect(answer.result).toBeUndefined();
    expect(answer.error?.code).toBe(INTERNAL_ERROR);
  });

  it("refuses an unknown session, an empty prompt and an image", async () => {
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

/** A 1x1 PNG, as base64. */
const PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==";
const PIXEL_BYTES = Buffer.from(PIXEL, "base64").length;

function image(name: string, data = PIXEL): Record<string, unknown> {
  return { type: "image", mimeType: "image/png", data, uri: `attachment:${name}` };
}

function resource(name: string, mimeType: string, bytes: Buffer): Record<string, unknown> {
  return { type: "resource", resource: { uri: `attachment:${name}`, mimeType, blob: bytes.toString("base64") } };
}

function sha256(bytes: string | Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function escaped(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The user content of each model call of the faux model. */
let calls: unknown[][] = [];

function seeing(reply: string): void {
  calls = [];
  faux.setResponses([
    (context) => {
      const user = context.messages.filter((message) => message.role === "user").at(-1);
      calls.push(Array.isArray(user?.content) ? user.content : [user?.content]);
      return fauxAssistantMessage(reply);
    },
  ]);
}

describe("the files of a prompt", () => {
  it("dispatches a text prompt as before", async () => {
    seeing("Hi.");
    await prompt(id, "Hello.");

    expect(calls).toEqual([[{ type: "text", text: "Hello." }]]);
  });

  it("writes each file to the workspace, names it in the text, and gives the image to the model", async () => {
    seeing("A cat.");
    const pdf = Buffer.from("%PDF-1.7 pages");
    const answer = await wire.request("session/prompt", {
      sessionId: id,
      prompt: [
        { type: "text", text: "What is this?" },
        image("cat.png"),
        resource("paper.pdf", "application/pdf", pdf),
        resource("paper.pdf.txt", "text/plain", Buffer.from("words")),
        { type: "resource", resource: { uri: "attachment:cat.png", mimeType: "text/markdown", text: "# Cat" } },
      ],
    });
    const dir = join(flue.workspace, "attachments");

    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(readFileSync(join(dir, "paper.pdf"))).toEqual(pdf);
    expect(readFileSync(join(dir, "paper.pdf.txt"), "utf8")).toBe("words");
    expect(readFileSync(join(dir, "cat-1.png"), "utf8")).toBe("# Cat");
    // Flue adds its own list of the images after the text.
    expect(calls[0]?.[0]).toEqual({
      type: "text",
      text: expect.stringMatching(
        new RegExp(
          `^${escaped(
            [
              "What is this?",
              "",
              `[attached: attachments/cat.png (image/png, ${PIXEL_BYTES} bytes, sha256 ${sha256(Buffer.from(PIXEL, "base64"))})]`,
              `[attached: attachments/paper.pdf (application/pdf, ${pdf.length} bytes, sha256 ${sha256(pdf)})]`,
              `[attached: attachments/paper.pdf.txt (text/plain, 5 bytes, sha256 ${sha256("words")})]`,
              `[attached: attachments/cat-1.png (text/markdown, 5 bytes, sha256 ${sha256("# Cat")})]`,
            ].join("\n"),
          )}`,
        ),
      ),
    });
    expect(calls[0]?.[1]).toEqual({ type: "image", data: PIXEL, mimeType: "image/png" });
    // The echo of the prompt shows the user text, then a link for each file.
    const echo = wire
      .updates(id)
      .filter((update) => update["sessionUpdate"] === "user_message_chunk")
      .map((update) => (update["content"] as Record<string, unknown>)["type"]);
    expect(echo).toEqual(["text", "resource_link", "resource_link", "resource_link", "resource_link"]);
    // The title is the user text, never a marker line.
    expect(wire.updates(id).at(-1)).toMatchObject({ sessionUpdate: "session_info_update", title: "What is this?" });
  });

  it("takes a prompt of files only", async () => {
    seeing("A pixel.");
    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [image("only.png")] });

    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(wire.updates(id).at(-1)).toEqual({ sessionUpdate: "session_info_update", updatedAt: expect.any(String) });
  });

  it.each([
    ["an image with no uri", [{ type: "image", mimeType: "image/png", data: PIXEL }]],
    ["a name with a slash", [image("../flue.db")]],
    ["an image type outside the scan kinds", [{ ...image("a.svg"), mimeType: "image/svg+xml" }]],
    ["a resource of no allowed type", [resource("a.zip", "application/zip", Buffer.from("PK"))]],
    ["data that is not base64", [image("a.png", "not base64!")]],
    ["five files", ["a", "b", "c", "d", "e"].map((name) => resource(`${name}.txt`, "text/plain", Buffer.from("x")))],
    ["a file over 8 MiB", [resource("big.txt", "text/plain", Buffer.alloc(MAX_FILE_BYTES + 1))]],
    ["an audio block", [{ type: "audio", mimeType: "audio/wav", data: "" }]],
  ])("refuses %s, and writes no file", async (_case, blocks) => {
    seeing("Never.");
    const before = readdirSync(flue.workspace, { recursive: true }).length;
    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Hi." }, ...blocks] });

    expect(answer.error?.code).toBe(INVALID_PARAMS);
    expect(calls).toEqual([]);
    expect(readdirSync(flue.workspace, { recursive: true })).toHaveLength(before);
  });

  it("counts a PDF and its text as one file", async () => {
    seeing("Read.");
    const blocks = ["a", "b", "c", "d"].flatMap((name) => [
      resource(`${name}.pdf`, "application/pdf", Buffer.from("%PDF")),
      resource(`${name}.pdf.txt`, "text/plain", Buffer.from("t")),
    ]);
    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Four." }, ...blocks] });

    expect(answer.result).toEqual({ stopReason: "end_turn" });
  });
});
