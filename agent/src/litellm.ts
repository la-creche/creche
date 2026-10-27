// The family's one model provider: LiteLLM's OpenAI-compatible endpoint,
// built with pi's `createProvider` and handed to Flue's `start()`. Flue takes
// the provider object, so no `models.json` exists and no credential lands on
// a disk.
//
//   FamilyConfig.planes.litellm ──► baseUrl   (the OpenAI SDK appends
//                                              `/chat/completions`)
//   FamilyConfig.creds.litellmKey ──► Authorization: Bearer, per request
//   runtime.json model_alias ──► `litellm/<alias>`, the one model
//
// Listing an alias grants nothing. The family key carries exactly one alias
// (contract 01 §3.2), so LiteLLM refuses any other. The perimeter stays
// outside the sandbox.

import { createProvider } from "@earendil-works/pi-ai";
import type { Provider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

import type { FamilyConfig } from "./config.js";
import { revealSecret } from "./secret.js";

/** The provider id, and the prefix of the model specifier `useModel` takes. */
export const LITELLM_PROVIDER = "litellm";

/** The wire protocol this LiteLLM answers in production. */
const PROVIDER_API = "openai-completions";

/** Fixed values. LiteLLM knows the real ones. */
const CONTEXT_WINDOW = 128000;
const MAX_TOKENS = 16384;

/** LiteLLM is the spend authority. No rate is kept here. */
const NO_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

export interface LitellmModel {
  readonly provider: Provider;
  /** The specifier for `useModel`: `litellm/<alias>`. */
  readonly spec: string;
}

/** `runtime.json` names no `model_alias`, so the agent has no model. */
export type LitellmRead =
  | { readonly ok: true; readonly model: LitellmModel }
  | { readonly ok: false; readonly reason: "model_alias_unset" };

/**
 * The bare alias. An alias that already carries the `litellm/` prefix loses
 * it, so a `caregiver` that writes either form lands on the same model, as
 * in the playpen.
 */
function bareAlias(alias: string): string {
  const prefix = `${LITELLM_PROVIDER}/`;

  return alias.startsWith(prefix) ? alias.slice(prefix.length) : alias;
}

export function litellmModel(config: FamilyConfig): LitellmRead {
  const alias = bareAlias(config.runtime.modelAlias ?? "");
  if (alias.length === 0) {
    return { ok: false, reason: "model_alias_unset" };
  }

  const key = config.creds.litellmKey;
  const provider = createProvider({
    id: LITELLM_PROVIDER,
    auth: {
      apiKey: {
        name: "LiteLLM family key",
        resolve: async () => ({ auth: { apiKey: revealSecret(key) } }),
      },
    },
    models: [
      {
        id: alias,
        name: `${alias} (LiteLLM)`,
        api: PROVIDER_API,
        provider: LITELLM_PROVIDER,
        baseUrl: config.planes.litellm.text,
        reasoning: false,
        input: ["text"],
        cost: NO_COST,
        contextWindow: CONTEXT_WINDOW,
        maxTokens: MAX_TOKENS,
      },
    ],
    api: openAICompletionsApi(),
  });

  return { ok: true, model: { provider, spec: `${LITELLM_PROVIDER}/${alias}` } };
}
