// The mode of the facade from `CRECHE_SESSION_ID` (decisions CH62 rule 6,
// CH52 rule 2): the read at the edge, and the built facade in each mode.

import { afterEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { EXIT_SESSION_ID } from "../src/family.js";
import { readMode, SBX } from "../src/mode.js";
import { facadeEnv, shortTmp, startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";

const SESSION = "0123abcd-0000-4000-8000-00000000c7c7";
const METHOD_NOT_FOUND = -32601;

/** How long the CH test watches the start: past two start pulls. */
const START_WATCH_MS = 1000;

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

  it("in CH mode reads no AGENT_* variable and pulls the start state until the vmm answers", async () => {
    // No guest init serves the family-state socket here, so each start
    // pull gets no state. The facade never exits on it (decision CH23
    // rule 2). With no `AGENT_*` variable, sbx would exit 7. In-process
    // tests cover the local link: `test/local-link.test.ts`.
    running = startFacade({ TMPDIR: shortTmp(), CRECHE_SESSION_ID: SESSION });
    const exited = running.exited();
    await new Promise((resolve) => setTimeout(resolve, START_WATCH_MS));

    expect(await Promise.race([exited, Promise.resolve("running")])).toBe("running");
    expect(running.stdout().length).toBe(0);
    expect(running.stderr()).toMatch(
      /^creche-facade: the family-state pull got no state: the link closed with no reply: connect E[A-Z]+ \/run\/creche\/family-state\.sock\ncreche-facade: the start pull of the family state failed \(not_valid\); the next pull is in 250 ms\n/,
    );
  });

  it("serves no session on sbx after the cut", async () => {
    // attendance answers session/new from the host list (decision CH21-7),
    // and on sbx the facade serves no session (decision CH84 rule 10). So
    // only initialize, the beat and the shutdown answer.
    running = startFacade();
    running.send(vectors.initialize.request);
    await running.nextMessage();
    running.send({ jsonrpc: "2.0", id: 2, method: "session/new", params: { cwd: "/work", mcpServers: [] } });
    const answer = (await running.nextMessage()) as { error: { code: number } };

    expect(answer.error.code).toBe(METHOD_NOT_FOUND);
    // On sbx the facade opens no local link (decision CH10-4).
    expect(running.stderr()).not.toContain("local link");
  });
});
