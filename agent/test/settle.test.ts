// The runtime under test: Flue's `start()` with no HTTP surface, an
// in-memory SQLite store and pi's faux provider. No network, no model key.
//
// The test asks one thing of the pins: one `dispatch` settles, and the
// `submission-settled` chunk names the outcome `completed`.

import { fauxAssistantMessage, fauxProvider } from "@earendil-works/pi-ai";
import { init, useModel } from "@flue/runtime";
import type { ConversationStreamChunk } from "@flue/runtime";
import { sqlite, start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

const REPLY = "settled";

const faux = fauxProvider();
const model = faux.getModel();
const MODEL_SPEC = `${model.provider}/${model.id}`;

function Probe(): string {
  useModel(MODEL_SPEC);

  return "Answer in one word.";
}

const flue = await start({ agents: [Probe], db: sqlite(), providers: [faux.provider] });
afterAll(() => flue.stop());

describe("start() with an in-memory store", () => {
  it("settles one dispatch as completed", async () => {
    faux.setResponses([fauxAssistantMessage(REPLY)]);
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
    expect(reply.submissionId).toBe(receipt.submissionId);
    expect(outcomes).toEqual(["completed"]);
  });
});
