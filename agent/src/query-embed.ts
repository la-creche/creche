// The one network touch of the vector half of `index_search`. One text goes
// in. Contract 04 §4.1's answer comes back: `embedding`, `model`, `dims`.
// `index-search.ts` compares the model with the one each index recorded.
//
// CONTRACT-QUESTION: contract 04 §4.1 put `embed` behind the PEP. The owner's
// round 8 (Q38) drops `embed` from the chaperone, and no contract names the
// endpoint that replaces it. This reading: the agent takes no endpoint, so
// the vector half refuses with one note and the words half keeps serving. A
// fix costs one config field and one `EmbedEndpoint` over it.

import { EmbedState } from "./index-search.js";
import type { EmbedOutcome, QueryEmbedder } from "./index-search.js";

/** An embedding endpoint: one text in, §4.1's answer out, as data. */
export type EmbedEndpoint = (text: string) => Promise<unknown>;

/** Bounds on the answer. It crosses a process boundary (invariant 12). */
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

/** A `QueryEmbedder` over one endpoint. Null fails closed: no vector. */
export function queryEmbedder(endpoint: EmbedEndpoint | null): QueryEmbedder {
  return {
    embed: async (text: string): Promise<EmbedOutcome> => {
      if (endpoint === null) {
        return unavailable("no embedding endpoint is configured");
      }

      try {
        return readEmbedding(await endpoint(text));
      } catch {
        return unavailable("embed could not be called");
      }
    },
  };
}
