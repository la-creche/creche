// The built `creche-facade` as a child process, the way guest init runs it:
// no stdin, prose on stderr, and no ACP on stdio.

import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const BIN = fileURLToPath(new URL("../dist/src/bin/creche-facade.js", import.meta.url));

export interface FacadeProcess {
  readonly child: ChildProcessWithoutNullStreams;
  /** Every byte of stdout so far. */
  stdout(): Buffer;
  /** Every byte of stderr so far, as text. */
  stderr(): string;
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

export function startFacade(env: NodeJS.ProcessEnv): FacadeProcess {
  const child = spawn(process.execPath, [BIN], { env: { ...env, PATH: process.env["PATH"] ?? "" } });
  const out: Buffer[] = [];
  let err = "";

  child.stdin.end();
  child.stdout.on("data", (chunk: Buffer) => out.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => {
    err += chunk.toString("utf8");
  });
  const exit = new Promise<number | null>((resolve) => child.on("exit", (code) => resolve(code)));

  return {
    child,
    stdout: () => Buffer.concat(out),
    stderr: () => err,
    exited: () => exit,
  };
}
