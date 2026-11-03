// The shared vectors of the strict reader (surface `runtime.strict_json`).
// Each case says what Python `strictjson` does with one text. `parseStrict`
// must give the same result and the same word, with one exception that a
// rule decides, never a list of cases: JavaScript alone refuses a number
// token that it cannot hold exactly (`jsOnlyRule`). A case with such a token
// is refused, with the word of its first such token or the vector's word.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { parseStrict, ReadRule } from "../src/strict-json.js";

const VECTOR_FILE = new URL("../../vectors/data/runtime/strict_json.json", import.meta.url);

interface Vector {
  readonly id: string;
  readonly input: { readonly text?: string; readonly repeat?: [string, number][]; readonly base64?: string };
  readonly result: "accepted" | "refused";
  readonly value?: unknown;
  readonly refusal?: string;
}

const surface = JSON.parse(readFileSync(VECTOR_FILE, "utf8")) as {
  readonly context: { readonly cap: number };
  readonly vectors: Vector[];
};

/** The bytes of the input of a case. */
function bytesOf({ text, repeat, base64 }: Vector["input"]): Uint8Array {
  if (base64 !== undefined) {
    return Buffer.from(base64, "base64");
  }

  const whole = text ?? (repeat ?? []).map(([part, count]) => part.repeat(count)).join("");

  return new TextEncoder().encode(whole);
}

/** The rule of JavaScript alone that the number token `token` breaks, or null. */
function jsOnlyRule(token: string): ReadRule | null {
  if (/^-?[0-9]+$/.test(token)) {
    const size = BigInt(token) < 0n ? -BigInt(token) : BigInt(token);
    const in64 = BigInt(token) >= -(2n ** 63n) && BigInt(token) < 2n ** 64n;

    return in64 && size > BigInt(Number.MAX_SAFE_INTEGER) ? ReadRule.UnsafeInteger : null;
  }

  const value = Number(token);

  return Number.isFinite(value) && Number.isInteger(value) ? ReadRule.IntegralFloat : null;
}

/** The rule of the first number token of `data`, outside each string, that JavaScript alone refuses. */
function firstJsOnlyRule(data: Uint8Array): ReadRule | null {
  const outside = new TextDecoder().decode(data).replace(/"(?:[^"\\]|\\[\s\S])*"/g, '""');
  for (const [token] of outside.matchAll(/-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?/g)) {
    const rule = jsOnlyRule(token);
    if (rule !== null) {
      return rule;
    }
  }

  return null;
}

describe("runtime.strict_json", () => {
  it.each(surface.vectors)("holds parseStrict to strictjson: $id", (vector) => {
    const data = bytesOf(vector.input);
    const result = parseStrict(data, surface.context.cap);
    const jsOnly = firstJsOnlyRule(data);

    if (jsOnly === null) {
      const want = vector.result === "accepted" ? { ok: true, value: vector.value } : { ok: false, rule: vector.refusal };
      expect(result).toStrictEqual(want);
    } else if (vector.result === "accepted") {
      expect(result).toStrictEqual({ ok: false, rule: jsOnly });
    } else {
      expect(result.ok).toBe(false);
      expect([vector.refusal, jsOnly]).toContain(result.ok ? null : result.rule);
    }
  });
});
