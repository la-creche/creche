// The facade's family in process, with the real agent module: one family's
// mounts (`agent/test/family.ts`), the agent's fake LiteLLM and fake `ch`.
// It serves the one session of a fresh id, as under CH (decision CH21-8).
// A CH start reads the fixed guest paths, which a test host lacks, so the
// family comes from the mounts, and each pull reads them again.

import { randomUUID } from "node:crypto";
import { PassThrough } from "node:stream";

import { vi } from "vitest";

import { facadeApp } from "../src/acp.js";
import { readFamily, serveFamily } from "../src/family.js";
import type { Runtime } from "../src/supervisor.js";
import { Wire } from "./acp-wire.js";

const NO_HOOKS = { onBeat: () => undefined, onShutdown: () => undefined };

export interface ServedFamily {
  /** The id of the one session. */
  readonly id: string;
  readonly wire: Wire;
  readonly runtime: Runtime | null;
  /** Every stderr line of the facade so far, as text. */
  stderr(): string;
}

export async function servedFamily(env: NodeJS.ProcessEnv): Promise<ServedFamily> {
  const write = vi.spyOn(process.stderr, "write");
  const read = await readFamily(env, new PassThrough());
  if (!read.ok) {
    throw new Error(`the family did not read: ${read.detail}`);
  }

  const id = randomUUID();
  const served = await serveFamily({ ...read.family, mode: { kind: "ch", sessionId: id } }, null);
  if (!served.ok || served.serving.sessions === null) {
    throw new Error("the family serves no session");
  }

  return {
    id,
    wire: new Wire(facadeApp(NO_HOOKS, served.serving.sessions)),
    runtime: served.serving.runtime,
    stderr: () => write.mock.calls.map(([chunk]) => String(chunk)).join(""),
  };
}
