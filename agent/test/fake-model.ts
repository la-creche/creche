// A stand-in for LiteLLM's OpenAI-compatible endpoint. A port of
// `playpen/test/fake-model.ts`. It binds 127.0.0.1 on an ephemeral port.
//
// It is NOT a model. It answers request N with reply N of a fixed script,
// and every request after the script with the last reply. A reply is one
// text or one tool call. Anything a test asserts against it is a statement
// about pi, Flue and the agent, never about a model. Do not grow it into one.

import { createServer } from "node:http";
import type { IncomingMessage, Server } from "node:http";
import type { AddressInfo } from "node:net";

export type FakeReply =
  | { readonly text: string }
  | { readonly tool: string; readonly args: Readonly<Record<string, unknown>> };

/** One chat completions request, as pi sent it. */
export interface SeenCompletion {
  readonly path: string;
  readonly authorization: string;
  readonly model: string;
  readonly tools: readonly string[];
  readonly messages: readonly { readonly role: string; readonly content: unknown }[];
}

interface CompletionBody {
  readonly model?: string;
  readonly tools?: readonly { readonly function?: { readonly name?: string } }[];
  readonly messages?: readonly { readonly role: string; readonly content: unknown }[];
}

const CALL_ID = "call_fake_1";

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
function chunksFor(model: string, reply: FakeReply): readonly unknown[] {
  const base = { id: "fake", object: "chat.completion.chunk", created: 0, model };
  const isText = "text" in reply;
  const delta = isText
    ? { role: "assistant", content: reply.text }
    : {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            index: 0,
            id: CALL_ID,
            type: "function",
            function: { name: reply.tool, arguments: JSON.stringify(reply.args) },
          },
        ],
      };

  return [
    { ...base, choices: [{ index: 0, delta, finish_reason: null }] },
    { ...base, choices: [{ index: 0, delta: {}, finish_reason: isText ? "stop" : "tool_calls" }] },
    { ...base, choices: [], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
  ];
}

export class FakeModel {
  public readonly seen: SeenCompletion[] = [];

  private server: Server | null = null;

  public constructor(private readonly script: readonly FakeReply[]) {}

  /** Binds 127.0.0.1 and answers with the base URL, `http://127.0.0.1:<port>`. */
  public async start(): Promise<string> {
    const server = createServer((request, response) => {
      void readBody(request).then((text) => {
        const body = JSON.parse(text || "{}") as CompletionBody;
        this.seen.push({
          path: request.url ?? "",
          authorization: request.headers.authorization ?? "",
          model: body.model ?? "",
          tools: (body.tools ?? []).map((tool) => tool.function?.name ?? ""),
          messages: body.messages ?? [],
        });

        const reply = this.script[Math.min(this.seen.length, this.script.length) - 1] ?? { text: "" };
        response.writeHead(200, { "content-type": "text/event-stream" });
        for (const chunk of chunksFor(body.model ?? "", reply)) {
          response.write(`data: ${JSON.stringify(chunk)}\n\n`);
        }
        response.end("data: [DONE]\n\n");
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    this.server = server;

    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
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
