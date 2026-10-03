// pi learns about LiteLLM from `models.json` in `PI_CODING_AGENT_DIR` and from
// nowhere else. pi offers no flag to point at another path, so the playpen
// writes the file into each session's own store before it starts that
// session's process (contract 03 §7).
//
//   <sessions mount>/<session>/pi/models.json   one provider, the family's alias
//
// Contract 03 §7 puts `LITELLM_BASE_URL` and `LITELLM_VIRTUAL_KEY` in the
// child's environment. This file names the key as the environment reference
// `$LITELLM_VIRTUAL_KEY`, which pi resolves per request, so the document holds
// no credential and invariant 13 still stands.

import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { LITELLM_BASE_URL, MODELS_FILE, PI_PROVIDER } from "./constants.js";

/**
 * The shape this LiteLLM has answered in production. `baseUrl` carries no
 * `/v1`: pi-ai appends the completions path itself.
 */
const PROVIDER_API = "openai-completions";
const API_KEY_REFERENCE = "$LITELLM_VIRTUAL_KEY";
const CONTEXT_WINDOW = 128000;
const MAX_TOKENS = 16384;

/** Real per-alias rates are not maintained here. LiteLLM is the spend authority. */
const NO_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

/**
 * The pi model id for one LiteLLM alias.
 *
 * `model.router` is a bare alias (contract 01 §3.2) and pi addresses a model
 * as `<provider>/<id>`. An alias that already carries the prefix is left
 * alone, so a `caregiver` that writes either form lands on the same id.
 * Contract 03 §4.1.1 makes that bridge the playpen's.
 */
export function piModelId(alias: string): string {
  const prefix = `${PI_PROVIDER}/`;

  return alias.startsWith(prefix) ? alias : `${prefix}${alias}`;
}

/** The bare LiteLLM alias behind a pi model id. */
function aliasOf(modelId: string): string {
  const prefix = `${PI_PROVIDER}/`;

  return modelId.startsWith(prefix) ? modelId.slice(prefix.length) : modelId;
}

function modelEntry(alias: string): Record<string, unknown> {
  return {
    id: alias,
    name: `${alias} (LiteLLM)`,
    reasoning: false,
    input: ["text"],
    contextWindow: CONTEXT_WINDOW,
    maxTokens: MAX_TOKENS,
    cost: NO_COST,
  };
}

/**
 * Writes the document into `sessionDir` and returns nothing.
 *
 * Temp file plus `rename` for the same reason the credential file uses it: a
 * reader must never see half a document. The
 * session store is this playpen's own writable mount, so no other writer
 * competes for the name.
 *
 * Listing an alias grants nothing. The family key carries exactly one alias
 * (contract 01 §3.2), so LiteLLM refuses anything else. The perimeter stays
 * where invariant 12 puts it, outside the sandbox.
 */
export function writeModelsJson(sessionDir: string, modelIds: readonly string[]): void {
  const aliases = [...new Set(modelIds.map(aliasOf))].filter((alias) => alias.length > 0);
  const document = {
    providers: {
      [PI_PROVIDER]: {
        baseUrl: LITELLM_BASE_URL,
        api: PROVIDER_API,
        apiKey: API_KEY_REFERENCE,
        models: aliases.map(modelEntry),
      },
    },
  };

  mkdirSync(sessionDir, { recursive: true });

  const path = join(sessionDir, MODELS_FILE);
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, `${JSON.stringify(document, null, 2)}\n`, { mode: 0o644 });
  renameSync(temp, path);
}
