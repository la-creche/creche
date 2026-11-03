// The one read of a wire text of the VM owner (decision CH22). `src/wire.ts`
// is the schema, and TypeBox gives each wire type its static type and its
// runtime check from it.
//
//   bytes ──► parseStrict ──► Validator.Check ──► { value: Wire<N> }
//                        └─► { rule }        └─► { rule: "not_valid" }
//
// A refusal names a rule, never a character of the text, so a log line can
// print it for a text that holds a secret.

import type { Static } from "typebox";
import Schema from "typebox/schema";
import type { Validator } from "typebox/schema";

import { parseStrict, ReadRule } from "./strict-json.js";
import { wire } from "./wire.js";

type Defs = typeof wire.$defs;

/** The name of a wire type: a key of `$defs` in the schema. */
export type WireName = keyof Defs;

/** The schema of the wire type `N`: the whole schema, with a `$ref` to `N`. */
type Root<N extends WireName> = typeof wire & { readonly $ref: `#/$defs/${N}` };

/** The static type of the wire type `N`. */
export type Wire<N extends WireName> = Static<Root<N>>;

/** The rule of a value that the schema of its wire type refuses. */
export const NOT_VALID = "not_valid";

/** What `read` gives: a value of the wire type, or the rule that refuses the text. */
export type WireRead<N extends WireName> =
  | { readonly ok: true; readonly value: Wire<N> }
  | { readonly ok: false; readonly rule: ReadRule | typeof NOT_VALID };

/**
 * The schema `schema` with each `minLength` and `maxLength` as a pattern.
 * JSON Schema, Rust and Python count a text in code points. TypeBox 1.3.27
 * counts grapheme clusters, so `"e"` and 300 combining accents would pass a
 * cap of 256. TypeBox compiles each pattern with the `u` flag, where
 * `[\s\S]` is one code point. Both keywords, and a pattern, apply to a
 * string only.
 */
function codePointLengths(schema: unknown): unknown {
  if (Array.isArray(schema)) {
    return schema.map(codePointLengths);
  }

  if (typeof schema !== "object" || schema === null) {
    return schema;
  }

  const members = Object.entries(schema).map(([key, value]) => [key, codePointLengths(value)]);
  const whole = Object.fromEntries(members) as Record<string, unknown>;
  const { minLength, maxLength, ...copy } = whole;
  if (typeof minLength !== "number" && typeof maxLength !== "number") {
    return whole;
  }

  const pattern = `^[\\s\\S]{${String(minLength ?? 0)},${String(maxLength ?? "")}}$`;
  const allOf = Array.isArray(copy["allOf"]) ? copy["allOf"] : [];

  return { ...copy, allOf: [...allOf, { pattern }] };
}

/** The defs of the schema, as `codePointLengths` gives them. */
const defs = codePointLengths(wire.$defs);

/** The validator of each wire type, compiled at its first read. */
const validators = new Map<WireName, Validator>();

/** The compiled check of the wire type `name`. */
export function validator<N extends WireName>(name: N): Validator<Root<N>> {
  const compiled = validators.get(name) ?? Schema.Compile({ ...wire, $defs: defs, $ref: `#/$defs/${name}` });
  validators.set(name, compiled);

  // The map holds, at `name`, the validator compiled from `Root<N>`.
  return compiled as Validator<Root<N>>;
}

/**
 * The value of the wire text `data` as the wire type `name`, or the rule that
 * refuses it. `cap` is the most bytes that a text of the caller's surface
 * has. The caller gives it, because the cap is a rule of the surface, not of
 * the type.
 */
export function read<N extends WireName>(name: N, data: Uint8Array, { cap }: { readonly cap: number }): WireRead<N> {
  const parsed = parseStrict(data, cap);
  if (!parsed.ok) {
    return parsed;
  }

  const value = parsed.value;
  if (!validator(name).Check(value)) {
    return { ok: false, rule: NOT_VALID };
  }

  return { ok: true, value };
}
