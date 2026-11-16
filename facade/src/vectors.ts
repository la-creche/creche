// The ACP v1 values of the facade, read from the one vector file that the
// facade tests read too. The protocol version and the line cap have no
// second copy in this package.

import vectors from "../vectors/creche-extensions.json" with { type: "json" };

/** The one ACP version that the facade speaks. */
export const PROTOCOL_VERSION = vectors.protocol_version;

/** The largest line in UTF-8 bytes, the LF not counted. Each side checks it. */
export const MAX_LINE_BYTES = vectors.max_line_bytes;

/** One `select` config option, as the vector file holds it. */
export interface SelectOption {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly category: string;
  readonly type: "select";
  readonly currentValue: string;
  readonly options: readonly { readonly value: string; readonly name: string }[];
}

type VectorOption = Omit<SelectOption, "type"> & { readonly type: string };

/**
 * The one option of `options`. JSON gives `type` as a string, so a vector
 * that is not exactly one `select` throws when the module loads.
 */
function onlySelect(options: readonly VectorOption[]): readonly [SelectOption] {
  const [option, ...rest] = options;
  if (option === undefined || rest.length > 0) {
    throw new Error("vectors: new_session holds not exactly one config option");
  }

  const { type } = option;
  if (type !== "select") {
    throw new Error(`vectors: the config option ${option.id} is not a select`);
  }

  return [{ ...option, type }];
}

/** The config options of a new session: the thinking option at its default. */
export const NEW_SESSION_CONFIG_OPTIONS = onlySelect(vectors.new_session.configOptions);

/** The `_meta` key of a `usage_update` that holds the family spend. */
export const SPEND_META_KEY = vectors.usage.spend_meta_key;

/** The currency of the family spend: LiteLLM counts in USD. */
export const SPEND_CURRENCY = vectors.usage.currency;

/** The creche extension that cuts a session back (`src/rewind.ts`). */
export const REWIND_METHOD = vectors.rewind.method;
