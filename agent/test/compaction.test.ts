// The facade's patch of Flue (facade/patches/@flue__runtime@2.3.0.patch),
// run with pi's faux provider. No network, no model key.
//
// 1. Between two tool calls of one run, a context past the compaction
//    threshold is compacted before the next model call, and the run goes on.
// 2. A reply that stops at the output limit is continued, in the same
//    submission, with one short user prompt.
// 3. A prompt that comes after an idle time compacts the context before its
//    first model call. A prompt within the idle time, or a context under the
//    floor, does not.
//
// The faux provider counts tokens as characters / 4, so a small context
// window and a tool that returns a long text reach the threshold in a few
// calls.

import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";
import type { FauxResponseFactory } from "@earendil-works/pi-ai";
import { defineTool, init, useModel, useTool } from "@flue/runtime";
import { sqlite, start } from "@flue/runtime/node";
import * as v from "valibot";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

/** The window and output limit of the faux model. The threshold is 6000 - 1000. */
const CONTEXT_WINDOW = 6000;
const MAX_TOKENS = 1000;

/** One tool result: about 750 faux tokens. */
const PAGE = "x".repeat(3000);

/** The summary text the faux model writes when Flue asks it to compact. */
const SUMMARY = "SUMMARY-OF-EARLIER-WORK";

/** Flue's summarization system prompt starts with this. */
const SUMMARIZER = "context summarization assistant";

/** The prompt of the patch after a length stop. */
const CONTINUE_PROMPT = "Your last reply stopped at the output limit.";

const DONE = "done";

const faux = fauxProvider({
  models: [{ id: "small", contextWindow: CONTEXT_WINDOW, maxTokens: MAX_TOKENS }],
});
const model = faux.getModel();
const MODEL_SPEC = `${model.provider}/${model.id}`;

const readPage = defineTool({
  name: "read_page",
  description: "Read the next page.",
  input: v.object({}),
  run: async () => PAGE,
});

function Reader(): string {
  useModel(MODEL_SPEC);
  useTool(readPage);

  return "Read pages until told to stop.";
}

/** The idle time of the idle agent, and its floor in faux tokens. */
const IDLE_MS = 300;
const IDLE_MIN_TOKENS = 1000;

function IdleReader(): string {
  // The kept tail is small, so the few faux tokens of a test leave something to summarize.
  useModel(MODEL_SPEC, { compaction: { idleAfterMs: IDLE_MS, idleMinTokens: IDLE_MIN_TOKENS, keepRecentTokens: 200 } });
  useTool(readPage);

  return "Read pages until told to stop.";
}

const flue = await start({ agents: [Reader, IdleReader], db: sqlite(), providers: [faux.provider] });
afterAll(() => flue.stop());

/** What each model request held, in order. */
interface Seen {
  readonly summarize: boolean;
  readonly hasSummary: boolean;
  readonly hasContinue: boolean;
}

let seen: Seen[] = [];

beforeEach(() => {
  seen = [];
});

function record(text: string): Seen {
  const one = {
    summarize: text.includes(SUMMARIZER),
    hasSummary: text.includes(SUMMARY),
    hasContinue: text.includes(CONTINUE_PROMPT),
  };
  seen.push(one);

  return one;
}

describe("the Flue patch", () => {
  it("compacts between tool calls and continues the run", async () => {
    const TOOL_CALLS = 12;
    let toolCalls = 0;
    const step: FauxResponseFactory = (context) => {
      const now = record(JSON.stringify(context));
      if (now.summarize) {
        return fauxAssistantMessage(SUMMARY);
      }

      if (toolCalls < TOOL_CALLS) {
        toolCalls += 1;

        return fauxAssistantMessage(fauxToolCall("read_page", {}));
      }

      return fauxAssistantMessage(DONE);
    };
    faux.setResponses(Array.from({ length: 40 }, () => step));

    const handle = init(Reader, { id: "mid-run" });
    const receipt = await handle.dispatch("Read twelve pages, then say done.");
    const reply = await handle.read(receipt);

    expect(reply.text).toBe(DONE);
    expect(toolCalls).toBe(TOOL_CALLS);

    const firstSummary = seen.findIndex((one) => one.summarize);
    expect(firstSummary).toBeGreaterThan(0);
    // The run went on after the compaction, from the compacted context.
    const after = seen.slice(firstSummary + 1).filter((one) => !one.summarize);
    expect(after.length).toBeGreaterThan(0);
    expect(after[0]?.hasSummary).toBe(true);
  });

  it("continues a reply that stopped at the output limit", async () => {
    let calls = 0;
    const step: FauxResponseFactory = (context) => {
      const now = record(JSON.stringify(context));
      if (now.summarize) {
        return fauxAssistantMessage(SUMMARY);
      }

      calls += 1;

      return calls === 1 ? fauxAssistantMessage("partial", { stopReason: "length" }) : fauxAssistantMessage(DONE);
    };
    faux.setResponses(Array.from({ length: 10 }, () => step));

    const handle = init(Reader, { id: "length" });
    const receipt = await handle.dispatch("Say done.");
    const reply = await handle.read(receipt);

    // The reply holds the cut text, then the continuation.
    expect(reply.text).toBe(`partial\n\n${DONE}`);
    expect(calls).toBe(2);
    expect(seen.filter((one) => !one.summarize).at(-1)?.hasContinue).toBe(true);
  });
  /** Two prompts on one instance, `pause` ms apart. The first reads `pages` pages. */
  async function twoPrompts(id: string, pages: number, pause: number): Promise<Seen[]> {
    let read = 0;
    const step: FauxResponseFactory = (context) => {
      const now = record(JSON.stringify(context));
      if (now.summarize) {
        return fauxAssistantMessage(SUMMARY);
      }

      if (read < pages) {
        read += 1;

        return fauxAssistantMessage(fauxToolCall("read_page", {}));
      }

      return fauxAssistantMessage(DONE);
    };
    faux.setResponses(Array.from({ length: 20 }, () => step));

    const handle = init(IdleReader, { id });
    expect((await handle.read(await handle.dispatch(`Read ${pages} pages, then say done.`))).text).toBe(DONE);
    await new Promise((resolve) => setTimeout(resolve, pause));
    const before = seen.length;
    expect((await handle.read(await handle.dispatch("Say done."))).text).toBe(DONE);

    return seen.slice(before);
  }

  it("compacts before the first model call after an idle time", async () => {
    const second = await twoPrompts("idle", 2, IDLE_MS * 2);

    expect(second[0]?.summarize).toBe(true);
    expect(second[1]?.summarize).toBe(false);
    expect(second[1]?.hasSummary).toBe(true);
  });

  it("does not compact within the idle time", async () => {
    const second = await twoPrompts("busy", 2, 0);

    expect(second.some((one) => one.summarize)).toBe(false);
  });

  it("does not compact a context under the floor", async () => {
    const second = await twoPrompts("small", 0, IDLE_MS * 2);

    expect(second.some((one) => one.summarize)).toBe(false);
  });
});
