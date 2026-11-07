// The mode of the facade from `CRECHE_SESSION_ID` (decisions CH62 rule 6,
// CH52 rule 2): the read at the edge, and the built facade in each mode.

import { afterEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { EXIT_SESSION_ID } from "../src/family.js";
import { readMode, SBX } from "../src/mode.js";
import { facadeEnv, startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";

const SESSION = "0123abcd-0000-4000-8000-00000000c7c7";

/** Texts that are not a session id. Each one refuses the start. */
const BAD = [
  "",
  "0123ABCD-0000-4000-8000-00000000C7C7",
  "{0123abcd-0000-4000-8000-00000000c7c7}",
  `${SESSION}\n`,
  ` ${SESSION}`,
  SESSION.slice(1),
  SESSION.replaceAll("-", ""),
  "0123abcd_0000_4000_8000_00000000c7c7",
];

let running: FacadeProcess | null = null;

afterEach(() => {
  running?.child.kill();
  running = null;
});

describe("readMode", () => {
  it("reads sbx when the variable is absent", () => {
    expect(readMode({})).toEqual({ ok: true, mode: SBX });
  });

  it("reads CH and the host's id for a session id", () => {
    expect(readMode({ CRECHE_SESSION_ID: SESSION })).toEqual({ ok: true, mode: { kind: "ch", sessionId: SESSION } });
  });

  it.each(BAD)("refuses %j, and names the variable but not the value", (text) => {
    const read = readMode({ CRECHE_SESSION_ID: text });

    expect(read).toEqual({ ok: false, detail: "CRECHE_SESSION_ID is not a session id" });
  });
});

describe("the built facade", () => {
  it("exits 17 for a bad session id, with one stderr line and no value", async () => {
    const bad = "not-a-session-id";
    running = startFacade({ ...facadeEnv(), CRECHE_SESSION_ID: bad });

    expect(await running.exited()).toBe(EXIT_SESSION_ID);
    expect(running.stdout().length).toBe(0);
    expect(running.stderr()).toBe("creche-facade: CRECHE_SESSION_ID is not a session id\n");
  });

  it("gives session/new the host's id in CH mode, and refuses a second session", async () => {
    running = startFacade({ ...facadeEnv(), CRECHE_SESSION_ID: SESSION });
    running.send(vectors.initialize.request);
    await running.nextMessage();
    const request = { jsonrpc: "2.0", method: "session/new", params: { cwd: "/work", mcpServers: [] } };
    running.send({ ...request, id: 2 });
    running.send({ ...request, id: 3 });

    expect(await running.nextMessage()).toEqual({ jsonrpc: "2.0", id: 2, result: { sessionId: SESSION } });
    expect(await running.nextMessage()).toMatchObject({ id: 3, error: { code: -32602 } });
  });

  it("mints a fresh id on sbx", async () => {
    running = startFacade();
    running.send(vectors.initialize.request);
    await running.nextMessage();
    running.send({ jsonrpc: "2.0", id: 2, method: "session/new", params: { cwd: "/work", mcpServers: [] } });
    const answer = (await running.nextMessage()) as { result: { sessionId: string } };

    expect(answer.result.sessionId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(answer.result.sessionId).not.toBe(SESSION);
  });
});
