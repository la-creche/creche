// An in-process stand-in for the PEP, for the bridge's tests. It binds
// 127.0.0.1 on an ephemeral port, so no test touches the host's PEP and
// nothing here needs the host.
//
// It is NOT a PEP. It has no grant file, no token check, no rate limit and no
// audit. Anything a test asserts against it is a statement about the BRIDGE's
// logic, never about the PEP. Do not grow it into a mock that makes a wrong
// bridge look right.

import { createServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import { PiExposure } from "../bridge/pi-api.js";
import type { PiToolDefinition } from "../bridge/pi-api.js";

export interface SeenRequest {
  readonly method: string;
  readonly path: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: Record<string, unknown>;
}

export interface Answer {
  readonly status: number;
  readonly body: unknown;
  /** Sent verbatim instead of `body`, for the tests about a malformed answer. */
  readonly raw?: string;
}

const HTTP_OK = 200;
const HTTP_NOT_FOUND = 404;

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

function flatHeaders(request: IncomingMessage): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const [name, value] of Object.entries(request.headers)) {
    headers[name.toLowerCase()] = Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
  }

  return headers;
}

export class FakePep {
  public readonly seen: SeenRequest[] = [];
  public manifest: Answer = { status: HTTP_OK, body: { family: "chat", rev: "rev-1", tools: [] } };
  public answer: Answer = { status: HTTP_OK, body: { result: "fixture result" } };
  /**
   * Whether `If-None-Match` is honoured (contract 04 §4.2).
   *
   * The comparison is the real PEP's: one tag against the `rev` of the
   * manifest this fake is serving. Turning it OFF is how a test stands an
   * older PEP up, which ignores the header and answers the whole manifest.
   */
  public revalidates = true;

  private server: Server | null = null;

  /**
   * Binds 127.0.0.1 and answers with the base URL.
   *
   * `at` re-binds the port a previous `start` handed out, which is what a
   * restarted unit does: a test about a PEP that comes back needs the
   * sandbox's `PEP_URL` to keep pointing at it.
   */
  public async start(at?: string): Promise<string> {
    const server = createServer((request, response) => void this.serve(request, response));
    await new Promise<void>((resolve) => server.listen(portOf(at), "127.0.0.1", resolve));
    this.server = server;
    const port = (server.address() as AddressInfo).port;

    return `http://127.0.0.1:${port}`;
  }

  public async stop(): Promise<void> {
    const server = this.server;
    if (server === null) {
      return;
    }

    this.server = null;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  /** Every request that named `/call`, in order. */
  public get calls(): readonly SeenRequest[] {
    return this.seen.filter((request) => request.path === "/call");
  }

  /** Every request that named `/manifest`, in order. */
  public get fetches(): readonly SeenRequest[] {
    return this.seen.filter((request) => request.path === "/manifest");
  }

  private async serve(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const path = (request.url ?? "").split("?")[0] ?? "";
    const text = await readBody(request);
    const headers = flatHeaders(request);
    this.seen.push({
      method: request.method ?? "",
      path,
      headers,
      body: parseOrEmpty(text),
    });

    if (path === "/manifest") {
      send(response, this.stillCurrent(headers) ? NOT_MODIFIED : this.manifest);
      return;
    }
    if (path === "/call") {
      send(response, this.answer);
      return;
    }

    send(response, { status: HTTP_NOT_FOUND, body: { reason: "no such path" } });
  }

  /** Contract 04 §4.2: the tag the caller sent against the rev being served. */
  private stillCurrent(headers: Readonly<Record<string, string>>): boolean {
    const tag = headers["if-none-match"];
    if (!this.revalidates || tag === undefined || this.manifest.status !== HTTP_OK) {
      return false;
    }

    const body = this.manifest.body;
    const rev = typeof body === "object" && body !== null ? (body as { rev?: unknown }).rev : null;

    return typeof rev === "string" && tag.replace(/^W\//, "").replace(/"/g, "") === rev;
  }
}

/** What the PEP answers a caller that already holds the current revision. */
const NOT_MODIFIED: Answer = { status: 304, body: null, raw: "" };

/** The port of a base URL, or 0 for "any free one". */
function portOf(base: string | undefined): number {
  if (base === undefined) {
    return 0;
  }

  const port = Number(new URL(base).port);

  return Number.isInteger(port) ? port : 0;
}

function parseOrEmpty(text: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(text);

    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function send(response: ServerResponse, answer: Answer): void {
  response.writeHead(answer.status, { "Content-Type": "application/json" });
  response.end(answer.raw ?? JSON.stringify(answer.body));
}

/**
 * The three methods the bridge calls on pi, with pi 0.99.1's own semantics.
 *
 * Every rule here was measured against the real pi in `real-pi.test.ts`, and
 * a fake that got one of them wrong would make a wrong bridge look right:
 *
 *  1. `registerTool` keys by NAME. A second call under one name replaces the
 *     definition rather than adding a second tool.
 *  2. Registering refreshes the tool set, and the refresh activates a name
 *     the registry did not already hold.
 *  3. pi has no unregister. Registering a name again as `hidden` takes it
 *     out of the active set and keeps the definition, and registering it
 *     again as `direct` puts it back.
 *  4. `setActiveTools` keeps only names the registry knows.
 */
export class FakePi {
  /** Every `registerTool` call in order, so a test can count replacements. */
  public readonly registrations: string[] = [];

  private readonly byName = new Map<string, PiToolDefinition>();
  private activeNames: string[] = [];

  /** Every tool this process holds, in first-registration order. */
  public get tools(): readonly PiToolDefinition[] {
    return [...this.byName.values()];
  }

  public registerTool(tool: PiToolDefinition): void {
    const before = this.byName.get(tool.name);
    this.byName.set(tool.name, tool);
    this.registrations.push(tool.name);

    if (tool.exposure === PiExposure.Hidden) {
      this.activeNames = this.activeNames.filter((name) => name !== tool.name);
      return;
    }

    if (before === undefined || before.exposure === PiExposure.Hidden) {
      this.activeNames.push(tool.name);
    }
  }

  public getActiveTools(): string[] {
    return [...this.activeNames];
  }

  public setActiveTools(toolNames: string[]): void {
    this.activeNames = toolNames.filter((name) => this.byName.has(name));
  }

  public named(name: string): PiToolDefinition | undefined {
    return this.byName.get(name);
  }
}

/** The text of a tool result, which is always one untrusted block. */
export function resultText(result: { content: readonly { text: string }[] }): string {
  return result.content.map((part) => part.text).join("");
}
