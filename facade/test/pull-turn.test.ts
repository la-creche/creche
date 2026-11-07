// The pull of the family state in a turn (decision CH23 rule 2), with a
// stand-in pull: a prompt that fails at the pull keeps no `_meta`, a
// cancel during the pull answers `cancelled`, and a pull that throws
// leaves the session free for the next prompt. A prompt that ends before
// its turn sends nothing on the local link (decisions CH21, CH32 rule 6).

import { DatabaseSync } from "node:sqlite";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { facadeApp } from "../src/acp.js";
import { FLUE_STOP_MS, LocalLink } from "../src/local-link.js";
import { SBX } from "../src/mode.js";
import { sessionService } from "../src/service.js";
import { SessionIndex } from "../src/session-index.js";
import type { Refresh } from "../src/turns.js";
import { Wire } from "./acp-wire.js";
import { faux, Probe, startFlue } from "./faux-flue.js";
import { closeGuestInits, guestInit, until } from "./guest-init.js";
import type { GuestInit } from "./guest-init.js";

const { trigger } = vectors.session_meta;
const NO_HOOKS = { onBeat: () => undefined, onShutdown: () => undefined };

const flue = await startFlue();
afterAll(() => flue.stop());
afterEach(closeGuestInits);

let index: SessionIndex;
let wire: Wire;
let refresh: Refresh;

beforeEach(() => {
  index = new SessionIndex(new DatabaseSync(":memory:"));
  const service = sessionService({ agent: Probe, agentName: "Probe", index, mode: SBX, link: null, store: flue.store, refresh: (id) => refresh(id) });
  wire = new Wire(facadeApp(NO_HOOKS, service));
});

async function newSession(): Promise<string> {
  return (await wire.request("session/new", { cwd: "/work", mcpServers: [] })).result?.["sessionId"] as string;
}

function prompt(sessionId: string, meta?: Record<string, unknown>): ReturnType<Wire["request"]> {
  return wire.request("session/prompt", { sessionId, prompt: [{ type: "text", text: "Weather?" }], _meta: meta });
}

describe("the pull in a turn", () => {
  it("keeps no _meta when the pull fails the prompt", async () => {
    refresh = () => Promise.resolve("creds_missing");
    const id = await newSession();

    const answer = await prompt(id, { [trigger.key]: trigger.value });

    expect(answer.error?.message).toContain("creds_missing");
    expect(index.get(id)?.trigger).toBeNull();
  });

  it("answers cancelled to a cancel during the pull", async () => {
    let finish = (): void => undefined;
    let entered = (): void => undefined;
    const inPull = new Promise<void>((resolve) => (entered = resolve));
    refresh = () => {
      entered();
      return new Promise((resolve) => (finish = () => resolve(null)));
    };
    const id = await newSession();

    const answer = prompt(id);
    await inPull;
    wire.notify("session/cancel", { sessionId: id });
    await new Promise((resolve) => setTimeout(resolve, 10));
    finish();

    expect((await answer).result).toEqual({ stopReason: "cancelled" });
  });

  it("frees the session when the pull throws", async () => {
    refresh = () => Promise.reject(new Error("FIXTURE-THROW"));
    const id = await newSession();
    const thrown = await prompt(id);

    refresh = () => Promise.resolve(null);
    faux.setResponses([fauxAssistantMessage("Sunny.")]);

    expect(thrown.error).toBeDefined();
    expect((await prompt(id)).result).toEqual({ stopReason: "end_turn" });
  });
});

describe("the local link in a turn", () => {
  const START_AND_END = /^\{"start":\{\}\}\n\{"end":\{[^\n]*\}\}\n$/;

  /** A service on a link to a test guest init, as under CH. */
  async function linked(): Promise<GuestInit> {
    const guest = await guestInit();
    const link = new LocalLink(guest.path, FLUE_STOP_MS, () => undefined);
    link.open(() => undefined);
    await guest.peer();
    const service = sessionService({ agent: Probe, agentName: "Probe", index, mode: SBX, link, store: flue.store, refresh: (id) => refresh(id) });
    wire = new Wire(facadeApp(NO_HOOKS, service));

    return guest;
  }

  /** One prompt that runs its turn, then the bytes that the link got. */
  async function oneTurn(guest: GuestInit, id: string): Promise<string> {
    refresh = () => Promise.resolve(null);
    faux.setResponses([fauxAssistantMessage("Sunny.")]);
    expect((await prompt(id)).result).toEqual({ stopReason: "end_turn" });
    await until(() => guest.received().split("\n").length === 3);

    return guest.received();
  }

  it("sends nothing for a prompt that fails at the pull or at _meta", async () => {
    const guest = await linked();
    const id = await newSession();
    refresh = () => Promise.resolve("creds_missing");
    expect((await prompt(id)).error?.message).toContain("creds_missing");
    refresh = () => Promise.resolve(null);
    expect((await prompt(id, { [trigger.key]: { ...trigger.value, kind: "cron" } })).error).toBeDefined();

    expect(await oneTurn(guest, id)).toMatch(START_AND_END);
  });

  it("sends nothing for a prompt that a cancel ends in the pull", async () => {
    const guest = await linked();
    let finish = (): void => undefined;
    let entered = (): void => undefined;
    const inPull = new Promise<void>((resolve) => (entered = resolve));
    refresh = () => {
      entered();
      return new Promise((resolve) => (finish = () => resolve(null)));
    };
    const id = await newSession();
    const answer = prompt(id);
    await inPull;
    wire.notify("session/cancel", { sessionId: id });
    await new Promise((resolve) => setTimeout(resolve, 10));
    finish();
    expect((await answer).result).toEqual({ stopReason: "cancelled" });

    expect(await oneTurn(guest, id)).toMatch(START_AND_END);
  });
});
