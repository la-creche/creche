// A fake vmm end of host port 1100, as guest init splices it: a Unix
// socket that reads each `FamilyStatePull` line and answers as the test
// says. Each link that the agent dials is one entry of `links`.

import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { join } from "node:path";

import { FIXTURE_KEY, FIXTURE_TOKEN } from "./family.js";

/** A valid `SKILL.md` text. */
export const SKILL = "---\nname: weather-report\ndescription: Write a short weather report.\n---\nSearch first.\n";

/** A wire `FamilyConfig` with each part set. */
export const CONFIG = {
  instructions: "  FIXTURE-INSTRUCTIONS  \n",
  model_alias: "litellm/chat",
  sandbox_tools: { bash: true, edit: false, glob: false, grep: false, read: true, write: false },
  skills: { "weather-report": SKILL },
  system_prompt: "replace",
};

export const CREDS = { litellm_key: FIXTURE_KEY, pep_token: FIXTURE_TOKEN };

/** The compact text of a `state` reply, as the vmm writes it. */
export function stateLine(config: Readonly<Record<string, unknown>> = CONFIG): string {
  return JSON.stringify({ state: { config, creds: CREDS } });
}

/** The vmm end: the lines of each link, and the reply that the test gives each pull. */
export interface FakeVmm {
  readonly path: string;
  /** The bytes that each link sent, one entry per link. */
  readonly links: string[];
}

const servers: Server[] = [];
const sockets: Socket[] = [];
const dirs: string[] = [];

/** Closes every link, listener and directory that `fakeVmm` made. Run it after each test. */
export function closeFakeVmms(): void {
  for (const socket of sockets.splice(0)) {
    socket.destroy();
  }

  for (const server of servers.splice(0)) {
    server.close();
  }

  for (const dir of dirs.splice(0)) {
    rmSync(dir, { recursive: true, force: true });
  }
}

/** A path short enough for a Unix socket on every platform. */
export function socketPath(): string {
  const dir = mkdtempSync("/tmp/fs-");
  dirs.push(dir);

  return join(dir, "family-state.sock");
}

/**
 * A fake vmm. `answer` gets the index of the link and writes what the vmm
 * sends: a reply line, or nothing, or a close.
 */
export async function fakeVmm(answer: (link: number, socket: Socket) => void): Promise<FakeVmm> {
  const path = socketPath();
  const links: string[] = [];
  const server = createServer((socket) => {
    sockets.push(socket);
    const link = links.length;
    links.push("");
    socket.on("data", (chunk: Buffer) => {
      links[link] += chunk.toString("utf8");
      if (links[link]?.endsWith("\n")) {
        answer(link, socket);
      }
    });
    socket.on("error", () => undefined);
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(path, resolve));

  return { path, links };
}

/** A fake vmm that answers each pull with `line` and an LF. */
export function replying(line: string): Promise<FakeVmm> {
  return fakeVmm((_link, socket) => socket.write(`${line}\n`));
}

/** The lines that a test hands as `say`, and the `say` that keeps them. */
export function sayer(): { readonly lines: string[]; readonly say: (line: string) => void } {
  const lines: string[] = [];

  return { lines, say: (line) => lines.push(line) };
}
