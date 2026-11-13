// One family for a test, started through the real CH start,
// `readGuestStart`, on a fake vmm (`test/fake-vmm.ts`). The start takes
// the store, the workspace and the planes of the guest, which a test host
// lacks, so the fixture puts a temporary store and workspace and the
// planes of the test's fakes in their place. A test names only what it
// changes: the plane URLs, the parts of the wire config and the tool env.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { AgentStart } from "../src/agent.js";
import type { FamilyConfig, Planes } from "../src/config.js";
import { readGuestStart } from "../src/guest-start.js";
import { PlaneUrl } from "../src/planes.js";
import { Secret } from "../src/secret.js";
import { BARE_CONFIG, CREDS, fakeVmm, FIXTURE_KEY, FIXTURE_TOKEN, stateLine } from "./fake-vmm.js";
import type { FakeVmm } from "./fake-vmm.js";

export { BARE_CONFIG, FIXTURE_KEY, FIXTURE_TOKEN };

const UNUSED_PLANE = "http://192.0.2.10:9";

export interface FamilyFixture {
  readonly pepUrl?: string;
  readonly litellmUrl?: string;
  /** The parts of the wire `FamilyConfig` that differ from `BARE_CONFIG`. */
  readonly config?: Readonly<Record<string, unknown>>;
  /** The env of the facade, which gives the tool env. */
  readonly env?: NodeJS.ProcessEnv;
}

/** A family on a fake vmm. The vmm answers each pull with `reply`, the state of the fixture until a test sets another. */
export interface TestFamily {
  readonly start: AgentStart;
  readonly vmm: FakeVmm;
  reply: string;
}

function plane(url: string): PlaneUrl {
  const parsed = PlaneUrl.parse(url);
  if (!parsed.ok) {
    throw new Error(`fixture plane refused: ${parsed.fault}`);
  }

  return parsed.url;
}

function planesOf(fixture: FamilyFixture): Planes {
  return { pep: plane(fixture.pepUrl ?? UNUSED_PLANE), litellm: plane(fixture.litellmUrl ?? UNUSED_PLANE) };
}

/** The wire config of `fixture`, for a reply of the fake vmm. */
function configOf(fixture: FamilyFixture): Readonly<Record<string, unknown>> {
  return { ...BARE_CONFIG, ...fixture.config };
}

/** The family of `fixture`, from its start pull. Run `closeFakeVmms` when the test file ends. */
export async function testFamily(fixture: FamilyFixture = {}): Promise<TestFamily> {
  const family = { reply: stateLine(configOf(fixture)) };
  const vmm = await fakeVmm((_link, socket) => socket.write(`${family.reply}\n`));
  const pulled = await readGuestStart(fixture.env ?? {}, () => undefined, vmm.path);
  const root = mkdtempSync(join(tmpdir(), "agent-family-"));
  const start: AgentStart = {
    ...pulled,
    store: join(root, "flue.db"),
    workspace: join(root, "workspace"),
    planes: planesOf(fixture),
  };

  return Object.assign(family, { start, vmm });
}

/** The planes, the creds and the alias of `fixture`, for a test of one edge. */
export function familyConfig(fixture: FamilyFixture = {}): FamilyConfig {
  const alias = configOf(fixture)["model_alias"];

  return {
    planes: planesOf(fixture),
    creds: { litellmKey: Secret.of(CREDS.litellm_key), pepToken: Secret.of(CREDS.pep_token) },
    modelAlias: typeof alias === "string" ? alias : "",
  };
}
