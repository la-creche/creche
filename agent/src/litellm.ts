// The family's one model provider: LiteLLM's OpenAI-compatible endpoint,
// built with pi's `createProvider` and handed to Flue's `start()`. Flue takes
// the provider object, so no `models.json` exists and no credential lands on
// a disk.
//
//   FamilyConfig.planes.litellm ──► baseUrl   (the OpenAI SDK appends
//                                              `/chat/completions`)
//   creds().litellmKey ──► Authorization: Bearer, per request. The facade
//                          pulls the creds before each prompt (decision CH5a).
//   runtime.json model_alias ──► `litellm/<alias>`, the model of each
//                                session's last pull (decisions CH5c, CH5c-2)
//   GET <plane>/v1/model/info ──► the alias's context window and output
//                                  limit, read at start and at each change
//
// Listing an alias grants nothing. The family key carries exactly one alias
// (contract 01 §3.2), so LiteLLM refuses any other. The perimeter stays
// outside the sandbox.

import { createProvider } from "@earendil-works/pi-ai";
import type { Provider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { setProvider } from "@flue/runtime";
import * as v from "valibot";

import type { FamilyConfig } from "./config.js";
import type { LiveCreds } from "./family-state.js";
import { revealSecret } from "./secret.js";
import type { Secret } from "./secret.js";

/** The provider id, and the prefix of the model specifier `useModel` takes. */
export const LITELLM_PROVIDER = "litellm";

/** The wire protocol this LiteLLM answers in production. */
const PROVIDER_API = "openai-completions";

/** The most output tokens one request asks for. A lower model limit wins. */
const REQUEST_MAX_TOKENS = 16384;

/** LiteLLM's model list, with the `model_info` of each deployment. */
const MODEL_INFO_PATH = "/v1/model/info";

/** The longest wait for the model list at process start. */
const MODEL_INFO_TIMEOUT_MS = 3000;

/** The context window and the output limit that pi and Flue's compaction use. */
export interface ModelLimits {
  readonly contextWindow: number;
  readonly maxTokens: number;
}

/** The limits when LiteLLM gives none for the alias. */
export const FALLBACK_LIMITS: ModelLimits = { contextWindow: 128000, maxTokens: REQUEST_MAX_TOKENS };

/** Why the read gave the fallback. */
export type LimitsFault = "model_alias_unset" | "unreachable" | "not_valid" | "alias_absent" | "window_unset";

export type LimitsRead =
  | { readonly ok: true; readonly limits: ModelLimits }
  | { readonly ok: false; readonly fault: LimitsFault; readonly limits: ModelLimits };

const ENTRIES = v.looseObject({
  data: v.array(v.looseObject({ model_name: v.optional(v.unknown()), model_info: v.optional(v.unknown()) })),
});

const COUNT = v.pipe(v.number(), v.integer(), v.minValue(1));

const INFO = v.looseObject({
  max_input_tokens: v.optional(v.nullable(COUNT)),
  max_output_tokens: v.optional(v.nullable(COUNT)),
});

function fallback(fault: LimitsFault): LimitsRead {
  return { ok: false, fault, limits: FALLBACK_LIMITS };
}

/**
 * The limits of the family's alias, from LiteLLM's `model_info`. An alias
 * with more than one deployment gets the smallest of each. Any fault gives
 * the fallback, so a LiteLLM that does not answer never stops the agent.
 */
export function limitsOf(alias: string, body: unknown): LimitsRead {
  const list = v.safeParse(ENTRIES, body);
  if (!list.success) {
    return fallback("not_valid");
  }

  const infos = list.output.data.filter((entry) => entry.model_name === alias).map((entry) => entry.model_info ?? {});
  if (infos.length === 0) {
    return fallback("alias_absent");
  }

  let contextWindow = Number.POSITIVE_INFINITY;
  let maxTokens = REQUEST_MAX_TOKENS;
  for (const raw of infos) {
    const info = v.safeParse(INFO, raw);
    if (!info.success) {
      return fallback("not_valid");
    }

    const { max_input_tokens: input, max_output_tokens: output } = info.output;
    if (input === undefined || input === null) {
      return fallback("window_unset");
    }

    contextWindow = Math.min(contextWindow, input);
    if (output !== undefined && output !== null) {
      maxTokens = Math.min(maxTokens, output);
    }
  }

  return { ok: true, limits: { contextWindow, maxTokens } };
}

/**
 * One GET of LiteLLM's model list with the family key. At start the alias
 * and the key are those of the start reads. At a model change they are the
 * pulled alias and the latest key.
 */
export async function readModelLimits(
  config: FamilyConfig,
  modelAlias: string = config.runtime.modelAlias ?? "",
  key: Secret = config.creds.litellmKey,
): Promise<LimitsRead> {
  const alias = bareAlias(modelAlias);
  if (alias.length === 0) {
    return fallback("model_alias_unset");
  }

  let body: unknown;
  try {
    const response = await fetch(`${config.planes.litellm.text}${MODEL_INFO_PATH}`, {
      headers: { authorization: `Bearer ${revealSecret(key)}` },
      signal: AbortSignal.timeout(MODEL_INFO_TIMEOUT_MS),
    });
    if (!response.ok) {
      return fallback("unreachable");
    }

    body = await response.json();
  } catch {
    return fallback("unreachable");
  }

  return limitsOf(alias, body);
}

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
export function bareAlias(alias: string): string {
  const prefix = `${LITELLM_PROVIDER}/`;

  return alias.startsWith(prefix) ? alias.slice(prefix.length) : alias;
}

/** The specifier of a bare alias for `useModel`. */
export function specOf(alias: string): string {
  return `${LITELLM_PROVIDER}/${alias}`;
}

/** One provider with one model for each alias of `models`. */
function providerOf(config: FamilyConfig, models: ReadonlyMap<string, ModelLimits>, creds: () => LiveCreds): Provider {
  return createProvider({
    id: LITELLM_PROVIDER,
    auth: {
      apiKey: {
        name: "LiteLLM family key",
        resolve: async () => ({ auth: { apiKey: revealSecret(creds().litellmKey) } }),
      },
    },
    models: [...models].map(([alias, limits]) => ({
      id: alias,
      name: `${alias} (LiteLLM)`,
      api: PROVIDER_API,
      provider: LITELLM_PROVIDER,
      baseUrl: config.planes.litellm.text,
      reasoning: false,
      input: ["text"],
      cost: NO_COST,
      contextWindow: limits.contextWindow,
      maxTokens: limits.maxTokens,
    })),
    api: openAICompletionsApi(),
  });
}

/** `creds` gives the current creds, read at each request. The default is the start creds. */
export function litellmModel(
  config: FamilyConfig,
  limits: ModelLimits = FALLBACK_LIMITS,
  creds: () => LiveCreds = () => config.creds,
): LitellmRead {
  const alias = bareAlias(config.runtime.modelAlias ?? "");
  if (alias.length === 0) {
    return { ok: false, reason: "model_alias_unset" };
  }

  return { ok: true, model: { provider: providerOf(config, new Map([[alias, limits]]), creds), spec: specOf(alias) } };
}

/**
 * The model of the latest pull (decisions CH5c, CH5c-2). The facade gives
 * `follow` the alias and the key of each pull before the agent applies
 * it. On a change it reads the new alias's limits with that key, and
 * replaces the provider in Flue with `setProvider`. Flue resolves the
 * model when a submission starts, so a turn that runs keeps its model.
 * The next prompt of each session resolves the new one.
 *
 * The provider keeps each alias it has served. So a session that pulled
 * the old alias just before the change still resolves it. LiteLLM refuses
 * an alias that the family key no longer carries.
 *
 * No wait for `model_live_at` happens here. Under CH the vmm holds the
 * `state` reply until that time (decision CH31 rule 1). On sbx caregiver
 * writes no `model_live_at` before the cut (decision CH45), and a config
 * edit never reaches a running sandbox (decision CH52 rule 1). So on sbx
 * the alias never changes after start.
 */
export class LiveModel {
  private served = new Map<string, ModelLimits>();
  private last: Promise<unknown> = Promise.resolve();
  private current: string;
  private latest: Provider;

  public constructor(
    private readonly config: FamilyConfig,
    alias: string,
    limits: ModelLimits,
    private readonly creds: () => LiveCreds,
  ) {
    this.current = alias;
    this.served.set(alias, limits);
    this.latest = providerOf(config, this.served, creds);
  }

  /** The provider of every alias served so far, for `start()`. */
  public get provider(): Provider {
    return this.latest;
  }

  /**
   * `alias` and `key` are those of the pull. The holder gets the pull only
   * after this call, so its creds are still those of the pull before.
   * Null: the alias is the alias of the last change. Else the limits read
   * of the new alias, with the fallback on a fault. The calls run one at a
   * time, so a pull applies only after its provider is in Flue.
   */
  public follow(alias: string, key: Secret): Promise<LimitsRead | null> {
    const next = this.last.then(() => this.switchTo(bareAlias(alias), key));
    this.last = next.catch(() => undefined);

    return next;
  }

  private async switchTo(alias: string, key: Secret): Promise<LimitsRead | null> {
    if (alias === this.current) {
      return null;
    }

    const read = await readModelLimits(this.config, alias, key);
    // The state changes only once the new provider is in Flue. A throw
    // leaves the old alias current, so the next pull tries again.
    const served = new Map(this.served).set(alias, read.limits);
    const provider = providerOf(this.config, served, this.creds);
    setProvider(provider);
    this.served = served;
    this.latest = provider;
    this.current = alias;

    return read;
  }
}
