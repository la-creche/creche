// A stand-in for LiteLLM's OpenAI-compatible endpoint, for the one test that
// runs the real pi with codemode. It binds 127.0.0.1 on an ephemeral port.
//
// It is NOT a model. It answers the first request with ONE `codemode` call
// carrying a fixed script, and every later request with a fixed sentence.
// Anything a test asserts against it is a statement about pi and the bridge,
// never about a model. Do not grow it into one.
//
//   request 1 (no tool result yet) ──► tool call: codemode { code: SCRIPT }
//   request 2 (the script's result) ──► "done", and the turn ends

import { createServer } from "node:http";
import type { IncomingMessage, Server } from "node:http";
import type { AddressInfo } from "node:net";

/** One chat completions request, as pi sent it. */
export interface SeenCompletion {
  readonly tools: readonly string[];
  readonly messages: readonly { readonly role: string; readonly content: unknown }[];
}

const CODEMODE_TOOL = "codemode";
const CALL_ID = "call_fake_1";
const FINAL_TEXT = "done";

function readBody(request: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let text = "";
    request.setEncoding("utf8");
    request.on("data", (chunk: string) => {
      text += chunk;
    });
    request.on("end", () => resolve(text));
  });
}

/** The streamed chunks of one answer, in OpenAI's chat completions shape. */
function chunksFor(model: string, script: string | null): readonly unknown[] {
  const base = { id: "fake", object: "chat.completion.chunk", created: 0, model };
  const delta =
    script === null
      ? { role: "assistant", content: FINAL_TEXT }
      : {
          role: "assistant",
          content: null,
          tool_calls: [
            {
              index: 0,
              id: CALL_ID,
              type: "function",
              function: { name: CODEMODE_TOOL, arguments: JSON.stringify({ code: script }) },
            },
          ],
        };

  return [
    { ...base, choices: [{ index: 0, delta, finish_reason: null }] },
    { ...base, choices: [{ index: 0, delta: {}, finish_reason: script === null ? "stop" : "tool_calls" }] },
    { ...base, choices: [], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
  ];
}

export class FakeModel {
  public readonly seen: SeenCompletion[] = [];

  private server: Server | null = null;

  public constructor(private readonly script: string) {}

  /** Binds 127.0.0.1 and answers with the base URL pi's provider takes. */
  public async start(): Promise<string> {
    const server = createServer((request, response) => {
      void readBody(request).then((text) => {
        const body = JSON.parse(text || "{}") as {
          model?: string;
          tools?: { function?: { name?: string } }[];
          messages?: { role: string; content: unknown }[];
        };
        const messages = body.messages ?? [];
        this.seen.push({
          tools: (body.tools ?? []).map((tool) => tool.function?.name ?? ""),
          messages,
        });

        const answered = messages.some((message) => message.role === "tool");
        response.writeHead(200, { "content-type": "text/event-stream" });
        for (const chunk of chunksFor(body.model ?? "", answered ? null : this.script)) {
          response.write(`data: ${JSON.stringify(chunk)}\n\n`);
        }
        response.end("data: [DONE]\n\n");
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    this.server = server;

    return `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`;
  }

  public async stop(): Promise<void> {
    const server = this.server;
    if (server === null) {
      return;
    }

    this.server = null;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  /** The text of every tool result pi sent back, in order. */
  public get toolResults(): readonly string[] {
    return this.seen.flatMap((one) =>
      one.messages
        .filter((message) => message.role === "tool")
        .map((message) => (typeof message.content === "string" ? message.content : JSON.stringify(message.content))),
    );
  }
}
