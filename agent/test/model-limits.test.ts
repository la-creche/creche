// The model limits from LiteLLM's `/v1/model/info`: the parse of one body,
// and one read against a local server on 127.0.0.1.

import { createServer } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, describe, expect, it } from "vitest";

import { FALLBACK_LIMITS, limitsOf, litellmModel, readModelLimits } from "../src/litellm.js";
import { familyConfig, FIXTURE_KEY } from "./family.js";

const ALIAS = "code-router";

function entry(name: string, info: Record<string, unknown> | null): Record<string, unknown> {
  return { model_name: name, litellm_params: { model: "auto_router/complexity_router" }, model_info: info };
}

describe("limitsOf", () => {
  it("reads the window and caps the output at the request limit", () => {
    const body = { data: [entry("other", { max_input_tokens: 1 }), entry(ALIAS, { max_input_tokens: 1048576, max_output_tokens: 128000 })] };

    expect(limitsOf(ALIAS, body)).toEqual({ ok: true, limits: { contextWindow: 1048576, maxTokens: 16384 } });
  });

  it("takes a model output limit below the request limit", () => {
    const body = { data: [entry(ALIAS, { max_input_tokens: 32000, max_output_tokens: 4096 })] };

    expect(limitsOf(ALIAS, body)).toEqual({ ok: true, limits: { contextWindow: 32000, maxTokens: 4096 } });
  });

  it("takes the smallest window of the alias's deployments", () => {
    const body = { data: [entry(ALIAS, { max_input_tokens: 200000 }), entry(ALIAS, { max_input_tokens: 64000 })] };

    expect(limitsOf(ALIAS, body)).toEqual({ ok: true, limits: { contextWindow: 64000, maxTokens: 16384 } });
  });

  it("gives the fallback and a fault for each bad body", () => {
    expect(limitsOf(ALIAS, { data: [entry("other", { max_input_tokens: 9 })] })).toEqual({
      ok: false,
      fault: "alias_absent",
      limits: FALLBACK_LIMITS,
    });
    expect(limitsOf(ALIAS, { data: [entry(ALIAS, null)] })).toMatchObject({ ok: false, fault: "window_unset" });
    expect(limitsOf(ALIAS, { data: [entry(ALIAS, { max_input_tokens: -5 })] })).toMatchObject({
      ok: false,
      fault: "not_valid",
    });
    expect(limitsOf(ALIAS, { models: [] })).toMatchObject({ ok: false, fault: "not_valid" });
  });
});

describe("readModelLimits", () => {
  const seen: { path: string; authorization: string }[] = [];
  const server = createServer((request, response) => {
    seen.push({ path: request.url ?? "", authorization: request.headers.authorization ?? "" });
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ data: [entry(ALIAS, { max_input_tokens: 1048576, max_output_tokens: 128000 })] }));
  });
  const listening = new Promise<string>((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(`http://127.0.0.1:${(server.address() as AddressInfo).port}`));
  });
  afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

  it("asks LiteLLM with the family key and feeds the model", async () => {
    const config = await familyConfig({ litellmUrl: await listening, runtime: { model_alias: `litellm/${ALIAS}` } });

    const read = await readModelLimits(config);

    expect(read).toEqual({ ok: true, limits: { contextWindow: 1048576, maxTokens: 16384 } });
    expect(seen).toEqual([{ path: "/v1/model/info", authorization: `Bearer ${FIXTURE_KEY}` }]);

    const model = litellmModel(config, read.limits);
    const [first] = model.ok ? model.model.provider.getModels() : [];
    expect(first?.contextWindow).toBe(1048576);
    expect(first?.maxTokens).toBe(16384);
  });

  it("gives the fallback when LiteLLM does not answer", async () => {
    const config = await familyConfig({ litellmUrl: "http://127.0.0.1:9", runtime: { model_alias: ALIAS } });

    expect(await readModelLimits(config)).toEqual({ ok: false, fault: "unreachable", limits: FALLBACK_LIMITS });
  });
});
