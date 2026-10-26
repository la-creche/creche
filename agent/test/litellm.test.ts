// The LiteLLM provider under Flue's `start()`, against a fake chat
// completions server on 127.0.0.1. One streamed answer settles, and the
// request carries the family key, the alias and the plane URL.

import { init, useModel } from "@flue/runtime";
import type { ConversationStreamChunk } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { litellmModel } from "../src/litellm.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig, FIXTURE_KEY } from "./family.js";

const ALIAS = "chat-default";
const REPLY = "settled";

const fake = new FakeModel([{ text: REPLY }]);
const config = await familyConfig({ litellmUrl: await fake.start(), runtime: { model_alias: ALIAS } });
const read = litellmModel(config);
if (!read.ok) {
  throw new Error(read.reason);
}

const { model } = read;

function Probe(): string {
  useModel(model.spec);

  return "Answer in one word.";
}

const flue = await start({ agents: [Probe], providers: [model.provider] });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
});

describe("litellmModel", () => {
  it("names the model litellm/<alias>, with or without the prefix in the alias", async () => {
    const prefixed = await familyConfig({ runtime: { model_alias: `litellm/${ALIAS}` } });
    const second = litellmModel(prefixed);

    expect(model.spec).toBe(`litellm/${ALIAS}`);
    expect(second.ok && second.model.spec).toBe(`litellm/${ALIAS}`);
  });

  it("refuses a family with no model_alias", async () => {
    const read = litellmModel(await familyConfig({ runtime: {} }));

    expect(read).toEqual({ ok: false, reason: "model_alias_unset" });
  });

  it("settles one streamed answer from the fake endpoint", async () => {
    const outcomes: string[] = [];

    const handle = init(Probe);
    const receipt = await handle.dispatch("Say the word.");
    const reply = await handle.read(receipt, {
      onEvent: (chunk: ConversationStreamChunk) => {
        if (chunk.type === "submission-settled") {
          outcomes.push(chunk.outcome);
        }
      },
    });

    expect(reply.text).toBe(REPLY);
    expect(outcomes).toEqual(["completed"]);
    expect(fake.seen).toHaveLength(1);
    expect(fake.seen[0]).toMatchObject({
      path: "/chat/completions",
      authorization: `Bearer ${FIXTURE_KEY}`,
      model: ALIAS,
    });
  });
});
