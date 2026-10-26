// Flue under test: `start()` with an in-memory store and pi's faux
// provider, as `agent/test/settle.test.ts` runs it. No network, no key.
// One process holds one Flue runtime, so each test file starts its own.

import { fauxProvider } from "@earendil-works/pi-ai";
import { useModel } from "@flue/runtime";
import type { Agent } from "@flue/runtime";
import { sqlite, start } from "@flue/runtime/node";
import type { ConversationStreamStore, PersistenceAdapter } from "@flue/runtime/adapter";

import { tapStores } from "../src/flue-history.js";

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
}

export async function startFlue(agents: readonly Agent[] = [Probe], db: PersistenceAdapter = sqlite()): Promise<Started> {
  const tapped = tapStores(db);
  const flue = await start({ agents, db: tapped.adapter, providers: [faux.provider] });

  return { stop: () => flue.stop(), store: tapped.store };
}
