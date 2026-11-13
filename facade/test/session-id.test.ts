// The session id of the facade from `CRECHE_SESSION_ID` (decisions CH62
// rule 6, CH52 rule 2, CH81 rule 8): the read at the edge, and the built
// facade with no id, a bad id and a good one.

import { afterEach, describe, expect, it } from "vitest";

import { EXIT_SESSION_ID } from "../src/family.js";
import { readSessionId } from "../src/session-id.js";
import { shortTmp, startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";

const SESSION = "0123abcd-0000-4000-8000-00000000c7c7";

/** How long the test watches the start: past two start pulls. */
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

describe("readSessionId", () => {
  it("reads the host's id for a session id", () => {
    expect(readSessionId({ CRECHE_SESSION_ID: SESSION })).toEqual({ ok: true, sessionId: SESSION });
  });

  it("refuses an absent variable", () => {
    expect(readSessionId({})).toEqual({ ok: false, detail: "CRECHE_SESSION_ID is not set" });
  });

  it.each(BAD)("refuses %j, and names the variable but not the value", (text) => {
    const read = readSessionId({ CRECHE_SESSION_ID: text });

    expect(read).toEqual({ ok: false, detail: "CRECHE_SESSION_ID is not a session id" });
  });
});

describe("the built facade", () => {
  it("exits 17 with no session id, with one stderr line", async () => {
    running = startFacade({ TMPDIR: shortTmp() });

    expect(await running.exited()).toBe(EXIT_SESSION_ID);
    expect(running.stdout().length).toBe(0);
    expect(running.stderr()).toBe("creche-facade: CRECHE_SESSION_ID is not set\n");
  });

  it("exits 17 for a bad session id, with one stderr line and no value", async () => {
    running = startFacade({ TMPDIR: shortTmp(), CRECHE_SESSION_ID: "not-a-session-id" });

    expect(await running.exited()).toBe(EXIT_SESSION_ID);
    expect(running.stdout().length).toBe(0);
    expect(running.stderr()).toBe("creche-facade: CRECHE_SESSION_ID is not a session id\n");
  });

  it("pulls the start state until the vmm answers", async () => {
    // No guest init serves the family-state socket here, so each start
    // pull gets no state. The facade never exits on it (decision CH23
    // rule 2). In-process tests cover the local link:
    // `test/local-link.test.ts`.
    running = startFacade({ TMPDIR: shortTmp(), CRECHE_SESSION_ID: SESSION });
    const exited = running.exited();
    await new Promise((resolve) => setTimeout(resolve, START_WATCH_MS));

    expect(await Promise.race([exited, Promise.resolve("running")])).toBe("running");
    expect(running.stdout().length).toBe(0);
    expect(running.stderr()).toMatch(
      /^creche-facade: the family-state pull got no state: the link closed with no reply: connect E[A-Z]+ \/run\/creche\/family-state\.sock\ncreche-facade: the start pull of the family state failed \(not_valid\); the next pull is in 250 ms\n/,
    );
  });
});
