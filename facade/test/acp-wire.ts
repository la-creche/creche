// One in-process ACP link: the facade's app on a stream that the test
// writes, read back line by line. Every line passes the facade's own strict
// writer, as on stdio.

import { PassThrough } from "node:stream";

import { strictStream, strictWriter } from "../src/acp.js";
import type { AgentApp } from "../src/acp.js";

const ANSWER_WAIT_MS = 5000;

export interface Answer {
  readonly id: number;
  readonly result?: Record<string, unknown>;
  readonly error?: { readonly code: number; readonly message: string };
}

export interface Notice {
  readonly method: string;
  readonly params: Record<string, unknown>;
}

export class Wire {
  public readonly input = new PassThrough();
  public readonly notices: Notice[] = [];
  public readonly said: string[] = [];
  private readonly answers = new Map<number, Answer>();
  private readonly waiters: (() => void)[] = [];
  private nextId = 0;

  public constructor(app: AgentApp) {
    const write = strictWriter(
      (line) => this.take(line),
      (text) => this.said.push(text),
    );
    app.connect(strictStream(this.input, write));
  }

  public async request(method: string, params: unknown): Promise<Answer> {
    const id = this.nextId;
    this.nextId += 1;
    this.input.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);

    const deadline = Date.now() + ANSWER_WAIT_MS;
    for (;;) {
      const answer = this.answers.get(id);
      if (answer !== undefined) {
        return answer;
      }

      if (Date.now() > deadline) {
        throw new Error(`no answer to ${method}`);
      }

      await new Promise<void>((resolve) => {
        this.waiters.push(resolve);
        setTimeout(resolve, 50);
      });
    }
  }

  public notify(method: string, params: unknown): void {
    this.input.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
  }

  /** The `update` of each `session/update` so far, for one session. */
  public updates(sessionId: string): Record<string, unknown>[] {
    return this.notices
      .filter((notice) => notice.method === "session/update" && notice.params["sessionId"] === sessionId)
      .map((notice) => notice.params["update"] as Record<string, unknown>);
  }

  public close(): void {
    this.input.end();
  }

  private take(line: string): void {
    const message = JSON.parse(line) as Record<string, unknown>;
    if (typeof message["method"] === "string") {
      this.notices.push({ method: message["method"], params: message["params"] as Record<string, unknown> });
    } else {
      this.answers.set(message["id"] as number, message as unknown as Answer);
    }

    for (const wake of this.waiters.splice(0)) {
      wake();
    }
  }
}
