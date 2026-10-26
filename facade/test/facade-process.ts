// The built `creche-facade` as a child process, the way attendance runs it:
// JSON-RPC lines on stdin, ACP lines on stdout, prose on stderr.

import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export const BIN = fileURLToPath(new URL("../dist/src/bin/creche-facade.js", import.meta.url));
export const RELAY_BIN = fileURLToPath(new URL("../dist/src/bin/acp-relay.js", import.meta.url));

const ANSWER_WAIT_MS = 5000;

export interface FacadeProcess {
  readonly child: ChildProcessWithoutNullStreams;
  /** Every byte of stdout so far. */
  stdout(): Buffer;
  /** Every byte of stderr so far, as text. */
  stderr(): string;
  send(message: unknown): void;
  /** The next whole stdout line, parsed. */
  nextMessage(): Promise<unknown>;
  /** The exit status, once the process has exited. */
  exited(): Promise<number | null>;
}

/**
 * A fresh, short temp directory. A Unix socket path has a limit of about
 * 100 bytes, and the temp directory of macOS is long.
 */
export function shortTmp(): string {
  return mkdtempSync("/tmp/fc-");
}

/** The environment of one facade: a sandbox id, a control mount, a TMPDIR. */
export function facadeEnv(): NodeJS.ProcessEnv {
  return {
    AGENT_SANDBOX: "chat-s1",
    AGENT_CONTROL_DIR: join(mkdtempSync(join(tmpdir(), "facade-")), "control"),
    TMPDIR: shortTmp(),
  };
}

export function startFacade(env: NodeJS.ProcessEnv = facadeEnv(), bin: string = BIN): FacadeProcess {
  const child = spawn(process.execPath, [bin], { env: { ...env, PATH: process.env["PATH"] ?? "" } });
  const out: Buffer[] = [];
  let err = "";
  let consumed = 0;
  const waiters: (() => void)[] = [];

  child.stdout.on("data", (chunk: Buffer) => {
    out.push(chunk);
    for (const wake of waiters.splice(0)) {
      wake();
    }
  });
  child.stderr.on("data", (chunk: Buffer) => {
    err += chunk.toString("utf8");
  });
  const exit = new Promise<number | null>((resolve) => child.on("exit", (code) => resolve(code)));

  const stdout = (): Buffer => Buffer.concat(out);

  return {
    child,
    stdout,
    stderr: () => err,
    send: (message) => {
      child.stdin.write(`${typeof message === "string" ? message : JSON.stringify(message)}\n`);
    },
    nextMessage: async () => {
      const deadline = Date.now() + ANSWER_WAIT_MS;
      for (;;) {
        const text = stdout().toString("utf8");
        const end = text.indexOf("\n", consumed);
        if (end !== -1) {
          const line = text.slice(consumed, end);
          consumed = end + 1;
          return JSON.parse(line) as unknown;
        }

        if (Date.now() > deadline) {
          throw new Error(`no line on stdout; stderr: ${err}`);
        }

        await new Promise<void>((resolve) => {
          waiters.push(resolve);
          setTimeout(resolve, 50);
        });
      }
    },
    exited: () => exit,
  };
}
