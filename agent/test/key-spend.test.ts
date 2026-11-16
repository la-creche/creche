// The family key's spend from LiteLLM's `/key/info`, against a local
// server on 127.0.0.1. The body is the shape LiteLLM answers for the key
// that calls it.

import { createServer } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, describe, expect, it } from "vitest";

import { readKeySpend } from "../src/litellm.js";
import { familyConfig, FIXTURE_KEY } from "./family.js";

describe("readKeySpend", () => {
  const seen: { path: string; authorization: string }[] = [];
  let body: unknown = null;
  const server = createServer((request, response) => {
    seen.push({ path: request.url ?? "", authorization: request.headers.authorization ?? "" });
    if (body === null) {
      response.writeHead(404).end();
      return;
    }

    response.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(body));
  });
  const listening = new Promise<string>((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(`http://127.0.0.1:${(server.address() as AddressInfo).port}`));
  });
  afterAll(() => new Promise<void>((resolve) => server.close(() => resolve())));

  async function read(): ReturnType<typeof readKeySpend> {
    const config = familyConfig({ litellmUrl: await listening });

    return readKeySpend(config.planes, config.creds.litellmKey);
  }

  it("asks LiteLLM with the family key and reads the spend and the budget", async () => {
    body = { key: "hashed", info: { spend: 12.4, max_budget: 60, budget_duration: "1d" } };

    expect(await read()).toEqual({ ok: true, spend: { spend: 12.4, budget: 60 } });
    expect(seen.at(-1)).toEqual({ path: "/key/info", authorization: `Bearer ${FIXTURE_KEY}` });
  });

  it("reads a key with no budget", async () => {
    body = { info: { spend: 0, max_budget: null } };

    expect(await read()).toEqual({ ok: true, spend: { spend: 0, budget: null } });
  });

  it("refuses a body that is not the key info", async () => {
    for (const bad of [{ info: { spend: -1 } }, { info: { spend: "1" } }, { spend: 1 }, []]) {
      body = bad;
      expect(await read()).toEqual({ ok: false, fault: "not_valid" });
    }
  });

  it("gives unreachable for a refused request and for no LiteLLM", async () => {
    body = null;
    expect(await read()).toEqual({ ok: false, fault: "unreachable" });

    const config = familyConfig({ litellmUrl: "http://127.0.0.1:9" });
    expect(await readKeySpend(config.planes, config.creds.litellmKey)).toEqual({ ok: false, fault: "unreachable" });
  });
});
