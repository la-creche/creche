// Flue under test: `start()` with an in-memory store and pi's faux
// provider, as `agent/test/settle.test.ts` runs it. No network, no key.
// One process holds one Flue runtime, so each test file starts its own.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { fauxProvider } from "@earendil-works/pi-ai";
import { useModel } from "@flue/runtime";
import type { Agent } from "@flue/runtime";
import { sqlite, start } from "@flue/runtime/node";
import type { ConversationStreamStore, PersistenceAdapter } from "@flue/runtime/adapter";

import { Earlier } from "agent/agent";

import { tapStores } from "../src/flue-history.js";
import { FIRST_LINEAGE, LINEAGE_NAME } from "../src/lineage.js";
import type { RewindOptions } from "../src/rewind.js";

export const faux = fauxProvider();

const model = faux.getModel();
const MODEL_SPEC = `${model.provider}/${model.id}`;

/** The agent of the tests: the faux model and no tool. */
export function Probe(): string {
  useModel(MODEL_SPEC);

  return "Answer in one word.";
}

/** The faux model's specifier, for an agent of one test file. */
export const FAUX_MODEL = MODEL_SPEC;

export interface Started {
  stop(): Promise<void>;
  /** The conversation store of this runtime. */
  store(): ConversationStreamStore | null;
  /** A temporary workspace of this runtime, for the files of the prompts. */
  readonly workspace: string;
}

export async function startFlue(agents: readonly Agent[] = [Probe], db: PersistenceAdapter = sqlite()): Promise<Started> {
  const tapped = tapStores(db);
  const flue = await start({ agents, db: tapped.adapter, providers: [faux.provider] });

  const workspace = mkdtempSync(join(tmpdir(), "facade-workspace-"));

  return { stop: () => flue.stop(), store: tapped.store, workspace };
}

/** The rewind options of a session with no rewind yet: a lineage file in a new temp directory. */
export function freshRewind(earlier: Earlier = new Earlier()): RewindOptions {
  const file = join(mkdtempSync(join(tmpdir(), "facade-lineage-")), LINEAGE_NAME);

  return { file, lineage: FIRST_LINEAGE, earlier };
}
