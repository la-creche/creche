// A stand-in for the chaperone's front door: MCP streamable HTTP on
// 127.0.0.1, `POST /mcp`, JSON answers only. It is NOT the chaperone. It
// checks the bearer, lists two upstream tools and answers each call from a
// fixed table:
//
//   kagi__search      ──► one text result
//   ha__gated_echo    ──► an error result with the gate text, as the
//                         chaperone's `mcp-http` plugin renders a waiting gate
//
// A request with another bearer gets 401 and is counted, never answered.

import { createServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

export const SEARCH_TOOL = "kagi__search";
export const GATED_TOOL = "ha__gated_echo";
export const SEARCH_RESULT = "one search result";

/** The text of `chaperone/plugins/default/mcp-http`, for a waiting gate. */
export function gateText(tool: string): string {
  return (
    `Awaiting permission for tool call ${tool} (gate g-1). Call again ` +
    "with the exact same arguments to gather the result."
  );
}

const MCP_PATH = "/mcp";
const HTTP_OK = 200;
const HTTP_ACCEPTED = 202;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_NOT_ALLOWED = 405;
const METHOD_NOT_FOUND = -32601;

const INPUT_SCHEMA = {
  type: "object",
  properties: { text: { type: "string" } },
  required: ["text"],
};

const TOOLS = [
  { name: SEARCH_TOOL, description: "Search the web.", inputSchema: INPUT_SCHEMA },
  { name: GATED_TOOL, description: "Echo the text after an approval.", inputSchema: INPUT_SCHEMA },
];

interface RpcRequest {
  readonly id?: number | string;
  readonly method: string;
  readonly params?: Record<string, unknown>;
}

export interface SeenCall {
  readonly name: string;
  readonly args: unknown;
}

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

function callResult(name: string): unknown {
  if (name === SEARCH_TOOL) {
    return { content: [{ type: "text", text: SEARCH_RESULT }] };
  }

  return { content: [{ type: "text", text: gateText(name) }], isError: true };
}

export class FakeCh {
  public readonly calls: SeenCall[] = [];
  public refused = 0;

  private server: Server | null = null;

  public constructor(private readonly token: string) {}

  /** Binds 127.0.0.1 and answers with the plane URL, `http://127.0.0.1:<port>`. */
  public async start(): Promise<string> {
    const server = createServer((request, response) => {
      void this.answer(request, response);
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
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  private async answer(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const body = await readBody(request);
    if (request.url !== MCP_PATH) {
      response.writeHead(HTTP_NOT_FOUND).end();
      return;
    }

    if (request.headers.authorization !== `Bearer ${this.token}`) {
      this.refused += 1;
      response.writeHead(HTTP_UNAUTHORIZED).end();
      return;
    }

    if (request.method !== "POST") {
      response.writeHead(request.method === "DELETE" ? HTTP_OK : HTTP_NOT_ALLOWED).end();
      return;
    }

    const rpc = JSON.parse(body) as RpcRequest;
    if (rpc.id === undefined) {
      response.writeHead(HTTP_ACCEPTED).end();
      return;
    }

    response.writeHead(HTTP_OK, { "content-type": "application/json" });
    response.end(JSON.stringify({ jsonrpc: "2.0", id: rpc.id, ...this.reply(rpc) }));
  }

  private reply(rpc: RpcRequest): Record<string, unknown> {
    const params = rpc.params ?? {};
    switch (rpc.method) {
      case "initialize":
        return {
          result: {
            protocolVersion: params["protocolVersion"],
            capabilities: { tools: {} },
            serverInfo: { name: "fake-ch", version: "0.0.0" },
          },
        };
      case "tools/list":
        return { result: { tools: TOOLS } };
      case "tools/call": {
        const name = String(params["name"]);
        this.calls.push({ name, args: params["arguments"] });

        return { result: callResult(name) };
      }
      default:
        return { error: { code: METHOD_NOT_FOUND, message: "method not found" } };
    }
  }
}
