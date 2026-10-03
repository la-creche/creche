// The semantic half's one network touch: contract 04 §4.1's `embed`, called
// through the PEP like any granted action, with §3's advisory headers.
//
// It is asked only when the manifest offers `embed` with no gate. A verb that
// is not granted would only be denied, and a gated one would hold the search
// for up to 15 minutes while the operator's phone rang (§8) for a query nobody asked
// them about.
//
// §4.1 returns the served model id so that a caller can compare it with the
// id its own index recorded. `search.ts` makes that comparison.

import { EMBED_VERB } from "./constants.js";
import { PepCallError } from "./pep.js";
import type { PepClient } from "./pep.js";
import { EmbedState } from "./search.js";
import type { EmbedOutcome, QueryEmbedder } from "./search.js";
import { Offer } from "./tool-set.js";
import type { OfferedTools } from "./tool-set.js";

/** Bounds on what the PEP sends back. It crosses a process boundary (invariant 12). */
const MAX_DIMS = 8192;
const MAX_MODEL_LENGTH = 256;

function unavailable(why: string): EmbedOutcome {
  return { state: EmbedState.Unavailable, why };
}

function isVector(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.length <= MAX_DIMS &&
    value.every((item) => typeof item === "number" && Number.isFinite(item))
  );
}

/** §4.1's answer: `embedding`, `model`, `dims`. Anything else is no vector. */
function readEmbedding(result: unknown): EmbedOutcome {
  if (typeof result !== "object" || result === null) {
    return unavailable("embed answered with no usable vector");
  }

  const raw = result as Record<string, unknown>;
  const vector = raw["embedding"];
  const model = raw["model"];
  if (!isVector(vector) || typeof model !== "string") {
    return unavailable("embed answered with no usable vector");
  }

  if (model.length === 0 || model.length > MAX_MODEL_LENGTH) {
    return unavailable("embed answered with no usable model id");
  }

  return { state: EmbedState.Ready, vector, model };
}

/** A `QueryEmbedder` over the family's PEP. `pep` is null when none is configured. */
export function pepEmbedder(pep: PepClient | null, offered: OfferedTools): QueryEmbedder {
  return {
    embed: async (text: string): Promise<EmbedOutcome> => {
      if (pep === null) {
        return unavailable("no policy service is configured");
      }

      const offer = offered.offer(EMBED_VERB);
      if (offer === Offer.Absent) {
        return unavailable("embed is not granted");
      }
      if (offer === Offer.Gated) {
        return unavailable("embed waits for a phone approval, and a search never asks for one");
      }

      try {
        return readEmbedding(await pep.call(EMBED_VERB, { input: text }));
      } catch (error) {
        if (error instanceof PepCallError) {
          return unavailable(`embed ended ${error.failure} (${error.reason})`);
        }

        return unavailable("embed could not be called");
      }
    },
  };
}
