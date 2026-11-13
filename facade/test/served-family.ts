// The facade's family in process, with the real agent module, the agent's
// fake LiteLLM and fake `ch`, and a fake vmm (`agent/test/fake-vmm.ts`).
// The fixture starts the family with the real `readGuestStart`, composes
// it with the real `composeAgent` and serves it with the real
// `serveFamily`. The start takes the store, the workspace and the planes
// of the guest, which a test host lacks, so the fixture puts a temporary
// store and workspace and the planes of the fakes in their place. It
// serves the one session of a fresh id (decision CH21-8). Each pull before
// a prompt reads the fake vmm, so a test changes the family state with
// `reply`. The local link never opens, so the turns send no event.

import { randomUUID } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { composeAgent, PlaneUrl, readGuestStart, readModelLimits, vmmBackend } from "agent/agent";
import type { AgentStart } from "agent/agent";
import { vi } from "vitest";

import { BARE_CONFIG, closeFakeVmms, fakeVmm, stateLine } from "../../agent/test/fake-vmm.js";
import { facadeApp } from "../src/acp.js";
import { serveFamily } from "../src/family.js";
import { guestLink } from "../src/local-link.js";
import { say } from "../src/stdio.js";
import { Wire } from "./acp-wire.js";

export interface FamilyFixture {
  readonly pepUrl: string;
  readonly litellmUrl: string;
  /** The parts of the wire `FamilyConfig` of the start that differ from `BARE_CONFIG`. */
  readonly config?: Readonly<Record<string, unknown>>;
}

export interface ServedFamily {
  /** The id of the one session. */
  readonly id: string;
  readonly wire: Wire;
  readonly start: AgentStart;
  /** The line of the fake vmm's reply to each later pull. */
  reply: string;
  /** Stops Flue and the fake vmm. */
  stop(): Promise<void>;
  /** Every stderr line of the facade so far, as text. */
  stderr(): string;
}

function plane(url: string): PlaneUrl {
  const parsed = PlaneUrl.parse(url);
  if (!parsed.ok) {
    throw new Error(`fixture plane refused: ${parsed.fault}`);
  }

  return parsed.url;
}

export async function servedFamily(fixture: FamilyFixture): Promise<ServedFamily> {
  const write = vi.spyOn(process.stderr, "write");
  const state = { reply: stateLine({ ...BARE_CONFIG, ...fixture.config }) };
  const vmm = await fakeVmm((_link, socket) => socket.write(`${state.reply}\n`));
  const root = mkdtempSync(join(tmpdir(), "facade-family-"));
  const start: AgentStart = {
    ...(await readGuestStart({}, say, vmm.path)),
    store: join(root, "flue.db"),
    workspace: join(root, "workspace"),
    planes: { pep: plane(fixture.pepUrl), litellm: plane(fixture.litellmUrl) },
  };
  const limits = await readModelLimits(start.planes, start.state.config.modelAlias, start.state.creds.litellmKey);
  const composed = composeAgent(start, limits.limits);
  const backend = vmmBackend(vmm.path, composed.state.config, say);
  const link = guestLink(say);
  const id = randomUUID();
  const served = await serveFamily({ sessionId: id, composed, limits, backend, link }, link);
  if (!served.ok) {
    throw new Error(`the family did not serve: ${served.detail}`);
  }

  const { runtime, sessions } = served.serving;

  return Object.assign(state, {
    id,
    wire: new Wire(facadeApp(sessions)),
    start,
    stop: async () => {
      await runtime?.stop();
      closeFakeVmms();
    },
    stderr: () => write.mock.calls.map(([chunk]) => String(chunk)).join(""),
  });
}
