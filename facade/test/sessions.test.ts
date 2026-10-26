// `session/new`, `session/list` and the turn record, over the facade's own
// wire, on Flue with an in-memory store and the faux model.

import { DatabaseSync } from "node:sqlite";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";
import { LIST_PAGE_SIZE, SessionIndex } from "../src/session-index.js";
import { sessionService } from "../src/service.js";
import { MAX_TITLE_BYTES, Sessions, titleOf } from "../src/sessions.js";
import { Wire } from "./acp-wire.js";
import { faux, Probe, startFlue } from "./faux-flue.js";

const CREATED = "creche/created_at";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const INVALID_PARAMS = -32602;
const NO_HOOKS = { onBeat: () => undefined, onShutdown: () => undefined };

const flue = await startFlue();
afterAll(() => flue.stop());

let clock = 0;
let sessions: Sessions;
let wire: Wire;

beforeEach(() => {
  clock = Date.parse("2026-10-25T12:00:00Z");
  const index = new SessionIndex(new DatabaseSync(":memory:"));
  const now = (): Date => new Date((clock += 1000));
  sessions = new Sessions(Probe, index, now);
  wire = new Wire(facadeApp(NO_HOOKS, sessionService({ agent: Probe, agentName: "Probe", index, store: flue.store, now })));
});

async function newSession(cwd = "/work"): Promise<string> {
  const answer = await wire.request("session/new", { cwd, mcpServers: [] });

  return answer.result?.["sessionId"] as string;
}

async function list(params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const answer = await wire.request("session/list", params);

  return answer.result ?? {};
}

describe("session/new", () => {
  it("mints a fresh id for each session", async () => {
    const first = await newSession();
    const second = await newSession();

    expect(first).toMatch(UUID);
    expect(second).toMatch(UUID);
    expect(second).not.toBe(first);
    expect(sessions.handle(first)?.id).toBe(first);
  });

  it("refuses a relative cwd, a client MCP server and another directory", async () => {
    const answers = [
      await wire.request("session/new", { cwd: "work", mcpServers: [] }),
      await wire.request("session/new", { cwd: "/work", mcpServers: [{ name: "x", command: "x", args: [], env: [] }] }),
      await wire.request("session/new", { cwd: "/work", mcpServers: [], additionalDirectories: ["/other"] }),
    ];

    expect(answers.map((answer) => answer.error?.code)).toEqual([INVALID_PARAMS, INVALID_PARAMS, INVALID_PARAMS]);
    expect((await list())["sessions"]).toEqual([]);
  });
});

describe("session/list", () => {
  it("lists each session, newest first, with no title before a turn", async () => {
    const first = await newSession("/a");
    const second = await newSession("/b");

    expect(await list()).toEqual({
      sessions: [
        { sessionId: second, cwd: "/b", title: null, updatedAt: "2026-10-25T12:00:02.000Z", _meta: { [CREATED]: "2026-10-25T12:00:02.000Z" } },
        { sessionId: first, cwd: "/a", title: null, updatedAt: "2026-10-25T12:00:01.000Z", _meta: { [CREATED]: "2026-10-25T12:00:01.000Z" } },
      ],
      nextCursor: null,
    });
    expect(((await list({ cwd: "/a" }))["sessions"] as unknown[]).length).toBe(1);
  });

  it("pages with a cursor and refuses a cursor it did not give", async () => {
    const ids: string[] = [];
    for (let at = 0; at <= LIST_PAGE_SIZE; at += 1) {
      ids.push(await newSession());
    }

    const first = await list();
    const second = await list({ cursor: first["nextCursor"] });
    const pages = [...(first["sessions"] as { sessionId: string }[]), ...(second["sessions"] as { sessionId: string }[])];

    expect(first["nextCursor"]).toEqual(expect.any(String));
    expect(second["nextCursor"]).toBeNull();
    expect(pages.map((info) => info.sessionId)).toEqual(ids.reverse());
    expect((await wire.request("session/list", { cursor: "x" })).error?.code).toBe(INVALID_PARAMS);
  });
});

describe("the record of a turn", () => {
  it("names the session from the first user text and dates it by the last record", async () => {
    const id = await newSession();
    const _meta = { [CREATED]: "2026-10-25T12:00:01.000Z" };
    const handle = sessions.handle(id);
    const settledAt: string[] = [];
    faux.setResponses([fauxAssistantMessage("done"), fauxAssistantMessage("again")]);

    for (const text of ["  Plan the   garden \nsecond line", "Another thing"]) {
      const receipt = await handle!.dispatch(text);
      await handle!.read(receipt, {
        onEvent: (chunk) => {
          if (chunk.type === "submission-settled" && chunk.timestamp !== undefined) {
            settledAt.push(chunk.timestamp);
          }
        },
      });
    }

    expect(sessions.recordTurn(id, "  Plan the   garden \nsecond line", settledAt[0] ?? null)).toEqual({
      title: "Plan the garden",
      updatedAt: settledAt[0],
      _meta,
    });
    expect(sessions.recordTurn(id, "Another thing", settledAt[1] ?? null)).toEqual({ updatedAt: settledAt[1], _meta });
    expect((await list())["sessions"]).toEqual([{ sessionId: id, cwd: "/work", title: "Plan the garden", updatedAt: settledAt[1], _meta }]);
  });

  it("cuts a title to its byte cap and knows no other session", () => {
    expect(Buffer.byteLength(titleOf("€".repeat(1000)))).toBeLessThanOrEqual(MAX_TITLE_BYTES);
    expect(titleOf("\n \n")).toBe("");
    expect(sessions.recordTurn("no-such-session", "text", null)).toBeNull();
    expect(sessions.handle("no-such-session")).toBeNull();
  });
});
