// The one session of the host id, and the end of a turn, over the facade's
// own wire, on Flue with an in-memory store and the faux model.

import { randomUUID } from "node:crypto";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { validator } from "agent/wire-read";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import { sessionService } from "../src/service.js";
import { infoOf, MAX_CWD_CHARS, MAX_TITLE_BYTES, OneSession, titleOf } from "../src/sessions.js";
import { Wire } from "./acp-wire.js";
import { faux, Probe, startFlue } from "./faux-flue.js";

const INVALID_PARAMS = -32602;
const METHOD_NOT_FOUND = -32601;
const RESOURCE_NOT_FOUND = -32002;

const flue = await startFlue();
afterAll(() => flue.stop());

let id: string;
let session: OneSession;
let wire: Wire;

beforeEach(() => {
  id = randomUUID();
  session = new OneSession(Probe, id);
  wire = new Wire(facadeApp(sessionService({ agent: Probe, agentName: "Probe", sessionId: id, link: null, store: flue.store })));
});

function load(sessionId: string, cwd = "/work"): ReturnType<Wire["request"]> {
  return wire.request("session/load", { sessionId, cwd, mcpServers: [] });
}

describe("the one session", () => {
  it("opens the host's id at start, with no session/new (decision CH21-8)", async () => {
    expect((await load(id)).result).toEqual({});
    expect(wire.updates(id)).toEqual([]);
    expect(session.handle(id)?.id).toBe(id);
  });

  it("knows no other id", async () => {
    const other = randomUUID();

    expect((await load(other)).error?.code).toBe(RESOURCE_NOT_FOUND);
    expect((await wire.request("session/prompt", { sessionId: other, prompt: [{ type: "text", text: "Hi" }] })).error?.code).toBe(
      RESOURCE_NOT_FOUND,
    );
    expect(session.handle(other)).toBeNull();
  });

  it("serves no session/new and no session/list: attendance answers them (decision CH21-7)", async () => {
    const answers = [
      await wire.request("session/new", { cwd: "/work", mcpServers: [] }),
      await wire.request("session/list", {}),
    ];

    expect(answers.map((answer) => answer.error?.code)).toEqual([METHOD_NOT_FOUND, METHOD_NOT_FOUND]);
  });

  it("is fresh until its first send", async () => {
    expect(await session.fresh()).toBe(true);
    faux.setResponses([fauxAssistantMessage("done")]);
    const handle = session.handle(id);
    await handle!.read(await handle!.dispatch("Hello"));

    expect(await session.fresh()).toBe(false);
  });

  it("refuses a relative cwd, a client MCP server and another directory", async () => {
    const answers = [
      await load(id, "work"),
      await wire.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [{ name: "x", command: "x", args: [], env: [] }] }),
      await wire.request("session/load", { sessionId: id, cwd: "/work", mcpServers: [], additionalDirectories: ["/other"] }),
    ];

    expect(answers.map((answer) => answer.error?.code)).toEqual([INVALID_PARAMS, INVALID_PARAMS, INVALID_PARAMS]);
  });

  it("takes each cwd that the wire takes, and refuses a NUL and a long one", async () => {
    // An astral character is two UTF-16 units and one code point.
    const longest = `/${"𝒜".repeat(MAX_CWD_CHARS - 1)}`;
    const refused = ["/a\u0000b", `${longest}a`, `/${"a".repeat(MAX_CWD_CHARS)}`];
    const isCwd = (cwd: string): boolean => validator("SessionCwd").Check(cwd);

    expect(isCwd(longest)).toBe(true);
    expect(refused.map(isCwd)).toEqual([false, false, false]);
    expect((await load(id, longest)).result).toEqual({});
    for (const cwd of refused) {
      expect((await load(id, cwd)).error?.code).toBe(INVALID_PARAMS);
    }
  });
});

describe("the end of a turn", () => {
  it("names the session on the first turn and dates each turn by its last record", () => {
    const first = session.turnEndOf("  Plan the   garden \nsecond line", "2026-10-25T12:00:01.000Z");
    const second = session.turnEndOf("Another thing", "2026-10-25T12:00:09.000Z");

    expect(first).toEqual({ title: "Plan the garden", updatedAt: "2026-10-25T12:00:01.000Z" });
    expect(infoOf(first, true)).toEqual({ title: "Plan the garden", updatedAt: "2026-10-25T12:00:01.000Z" });
    // The update names the session once. The turn end of the local link
    // carries the title of each turn, and the vmm keeps the first (decisions
    // CH21, CH32 rule 4).
    expect(second).toEqual({ title: "Another thing", updatedAt: "2026-10-25T12:00:09.000Z" });
    expect(infoOf(second, false)).toEqual({ updatedAt: "2026-10-25T12:00:09.000Z" });
    expect(infoOf(session.turnEndOf("\n \n", null), true)).toEqual({ updatedAt: expect.any(String) });
  });

  it("cuts a title to its byte cap", () => {
    expect(Buffer.byteLength(titleOf("€".repeat(1000)))).toBeLessThanOrEqual(MAX_TITLE_BYTES);
    expect(titleOf("\n \n")).toBe("");
    expect(titleOf(`${"a".repeat(255)} b`)).toBe("a".repeat(255));
    expect(titleOf("𝒜".repeat(256))).toBe("𝒜".repeat(64));
  });

  it("makes a title that the wire takes, or none", () => {
    const spaces = "\t\v\f\r         　﻿";
    const texts = [
      `${"a".repeat(255)} b`,
      // The fold makes 258 bytes. The cut lands inside the euro sign and backs to the space.
      `${"a".repeat(254)}　€`,
      "𝒜".repeat(256),
      `${spaces}one${spaces}two${spaces}\nthree`,
      "\ud800 lone \udc00",
      "a\u0085b",
      "\n \n",
    ];
    // TypeBox takes a lone surrogate, so the check below needs `isWellFormed` too.
    const isTitle = (title: string): boolean => validator("SessionTitle").Check(title) && title.isWellFormed();

    for (const text of texts) {
      const title = titleOf(text);
      expect(title === "" || isTitle(title), JSON.stringify(text)).toBe(true);
    }
    expect(titleOf(`${spaces}one${spaces}two`)).toBe("one two");
    expect(titleOf(`${"a".repeat(254)}　€`)).toBe("a".repeat(254));
    expect(titleOf("\ud800 lone \udc00")).toBe("� lone �");
  });
});
