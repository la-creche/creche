// The local link of the facade (decisions CH21, CH32 rule 7, CH62 rules 9
// to 11), against a Unix socket that the test serves as guest init does:
// the turn events up, the stop line down, a fault that is not fatal, and
// the stop of the supervisor at a stop line.

import { createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { PassThrough } from "node:stream";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { read } from "agent/wire-read";
import { afterAll, afterEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";

import { CONTROL_LINE_CAP, FACADE_STOP_WAIT_MS, LOCAL_LINK_SOCKET } from "../src/guest.js";
import { FLUE_STOP_MS, LocalLink, localLinkOf, turnLine } from "../src/local-link.js";
import { SupervisorLock } from "../src/lock.js";
import { SBX } from "../src/mode.js";
import { sessionService } from "../src/service.js";
import { SessionIndex } from "../src/session-index.js";
import { EXIT_OK, runSupervisor } from "../src/supervisor.js";
import type { Runtime } from "../src/supervisor.js";
import { Wire } from "./acp-wire.js";
import { shortTmp } from "./facade-process.js";
import { faux, Probe, startFlue } from "./faux-flue.js";

const STOP = '{"stop":{}}\n';
const WAIT_MS = 2000;

/** The test end of the link: what guest init reads and writes. */
interface GuestInit {
  readonly path: string;
  /** Every byte that the facade sent, as text. */
  received(): string;
  /** The facade's end, once it connected. */
  peer(): Promise<Socket>;
  /** True once the facade closed its end. */
  closedByFacade(): boolean;
}

const flue = await startFlue();
afterAll(() => flue.stop());

const servers: Server[] = [];
const links: Socket[] = [];

afterEach(() => {
  for (const socket of links.splice(0)) {
    socket.destroy();
  }
  for (const server of servers.splice(0)) {
    server.close();
  }
});

async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + WAIT_MS;
  while (!check()) {
    if (Date.now() > deadline) {
      throw new Error("the wait ran out");
    }

    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function guestInit(): Promise<GuestInit> {
  const path = join(shortTmp(), "guest-init.sock");
  let received = "";
  let closed = false;
  let socket: Socket | null = null;
  const server = createServer((peer) => {
    socket = peer;
    links.push(peer);
    peer.on("data", (chunk: Buffer) => {
      received += chunk.toString("utf8");
    });
    peer.on("end", () => {
      closed = true;
    });
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(path, resolve));

  return {
    path,
    received: () => received,
    peer: async () => {
      await until(() => socket !== null);
      return socket as unknown as Socket;
    },
    closedByFacade: () => closed,
  };
}

function link(path: string, said: string[], stopMs = FLUE_STOP_MS): LocalLink {
  return new LocalLink(path, stopMs, (text) => said.push(text));
}

describe("the turn events", () => {
  it("go up as one wire TurnEvent line each", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const facade = link(guest.path, said);
    facade.open(() => undefined);
    facade.turnStart();
    facade.turnEnd("Plan the garden", "2026-10-25T12:00:00.000Z");
    await until(() => guest.received().split("\n").length === 3);

    const lines = guest.received().split("\n");
    expect(lines).toEqual(['{"start":{}}', '{"end":{"title":"Plan the garden","updated_at":1792929600000}}', ""]);
    for (const line of lines.slice(0, 2)) {
      expect(read("TurnEvent", Buffer.from(line), { cap: CONTROL_LINE_CAP }).ok).toBe(true);
    }
    expect(said).toEqual([]);
  });

  it("count the title in code points, as the wire does", () => {
    // 256 astral characters: 256 code points, 512 UTF-16 units, 1024
    // bytes. `SessionTitle` takes at most 256 code points, and
    // `agent/wire-read` compiles `maxLength` as a pattern of code points.
    const longest = "𝒜".repeat(256);
    const end = (title: string): ReturnType<typeof turnLine> => turnLine({ end: { title, updated_at: 0 } });

    expect(longest.length).toBe(512);
    expect(end(longest).ok).toBe(true);
    expect(end(`${longest}𝒜`).ok).toBe(false);
    expect(end("a".repeat(256)).ok).toBe(true);
    expect(end("a".repeat(257)).ok).toBe(false);
  });

  it("are dropped with a stderr line when the wire refuses them", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const facade = link(guest.path, said);
    facade.open(() => undefined);
    facade.turnEnd("ends in a space ", "2026-10-25T12:00:00.000Z");
    facade.turnEnd("no time", "not a time");
    facade.turnStart();
    await until(() => guest.received().length > 0);

    expect(guest.received()).toBe('{"start":{}}\n');
    expect(said).toEqual([
      "a turn event is dropped: the turn event is not a wire TurnEvent",
      "a turn event is dropped: the turn event is not a wire TurnEvent",
    ]);
  });
});

describe("a prompt", () => {
  it("sends a turn start, then a turn end with the title and the time of the turn record", async () => {
    const guest = await guestInit();
    const facade = link(guest.path, []);
    facade.open(() => undefined);
    const index = new SessionIndex(new DatabaseSync(":memory:"));
    const service = sessionService({ agent: Probe, agentName: "Probe", index, mode: SBX, link: facade, store: flue.store });
    const wire = new Wire(facadeApp({ onBeat: () => undefined, onShutdown: () => undefined }, service));
    const id = (await wire.request("session/new", { cwd: "/work", mcpServers: [] })).result?.["sessionId"] as string;
    faux.setResponses([fauxAssistantMessage("done"), fauxAssistantMessage("again")]);
    for (const text of ["  Plan the   garden \nsecond line", "Another thing"]) {
      await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text }] });
    }
    await until(() => guest.received().split("\n").length === 5);

    const times = wire.updates(id).flatMap((update) => (update["sessionUpdate"] === "session_info_update" ? [Date.parse(String(update["updatedAt"]))] : []));
    expect(times).toHaveLength(2);
    expect(guest.received().split("\n").slice(0, 4).map((line) => JSON.parse(line) as unknown)).toEqual([
      { start: {} },
      { end: { title: "Plan the garden", updated_at: times[0] } },
      { start: {} },
      { end: { title: "Another thing", updated_at: times[1] } },
    ]);
  });

  it("sends the turn end when the row write throws", async () => {
    const guest = await guestInit();
    const facade = link(guest.path, []);
    facade.open(() => undefined);
    const db = new DatabaseSync(":memory:");
    const service = sessionService({ agent: Probe, agentName: "Probe", index: new SessionIndex(db), mode: SBX, link: facade, store: flue.store });
    const wire = new Wire(facadeApp({ onBeat: () => undefined, onShutdown: () => undefined }, service));
    const id = (await wire.request("session/new", { cwd: "/work", mcpServers: [] })).result?.["sessionId"] as string;
    // A real SQLite fault at the turn's write, as a full disk gives one.
    db.exec("CREATE TRIGGER full BEFORE UPDATE OF updated_at ON creche_sessions BEGIN SELECT RAISE(ABORT, 'FIXTURE-FULL'); END");
    faux.setResponses([fauxAssistantMessage("done")]);

    const answer = await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text: "Plan the garden" }] });
    await until(() => guest.received().split("\n").length === 3);

    expect(answer.result).toBeUndefined();
    expect(guest.received().split("\n").slice(0, 2).map((line) => Object.keys(JSON.parse(line) as object))).toEqual([["start"], ["end"]]);
  });
});

describe("the down lines", () => {
  it("run the stop at a stop line, also in two pieces", async () => {
    const guest = await guestInit();
    let stops = 0;
    link(guest.path, []).open(() => (stops += 1));
    const peer = await guest.peer();
    peer.write('{"st');
    await new Promise((resolve) => setTimeout(resolve, 20));
    peer.write('op":{}}\n');
    await until(() => stops === 1);

    expect(stops).toBe(1);
  });

  it("take a line of exactly the cap, and drop one byte more", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    let stops = 0;
    link(guest.path, said).open(() => (stops += 1));
    const peer = await guest.peer();
    const pad = (bytes: number): string => `{"stop":{}${" ".repeat(bytes - STOP.length)}}\n`;
    peer.write(pad(CONTROL_LINE_CAP + 1));
    peer.write(pad(CONTROL_LINE_CAP + 2));
    await until(() => stops === 1 && said.length === 1);

    expect(said).toEqual(["a local link line is refused (too_large); it is dropped"]);
  });

  // CONTRACT-QUESTION of `src/local-link.ts`: a line that is not stop is
  // dropped, and the link stays open.
  it("drop each other line and keep the link", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    let stops = 0;
    const facade = link(guest.path, said);
    facade.open(() => (stops += 1));
    const peer = await guest.peer();
    peer.write('{"sync":{}}\nnot json\n{"stop":{},"stop":{}}\n{"stop":{"x":1}}\n');
    peer.write("x".repeat(CONTROL_LINE_CAP));
    peer.write("x".repeat(CONTROL_LINE_CAP));
    await until(() => said.length === 5);
    peer.write(`xx\n${STOP}`);
    await until(() => stops === 1);
    facade.turnStart();
    await until(() => guest.received().length > 0);

    expect(said).toEqual([
      "a local link line is not stop; it is dropped",
      "a local link line is refused (syntax); it is dropped",
      "a local link line is refused (duplicate_key); it is dropped",
      "a local link line is refused (not_valid); it is dropped",
      `a local link line is over ${String(CONTROL_LINE_CAP)} bytes; it is dropped`,
    ]);
    expect(guest.received()).toBe('{"start":{}}\n');
    expect(guest.closedByFacade()).toBe(false);
  });
});

// CONTRACT-QUESTION of `LocalLink.open`: a link that does not open or
// faults costs one stderr line, and the facade serves on with no link.
describe("a link fault", () => {
  it("is not fatal when guest init closes the link", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const facade = link(guest.path, said);
    facade.open(() => undefined);
    (await guest.peer()).destroy();
    await until(() => said.length === 1);
    facade.turnStart();
    facade.turnEnd("a turn", "2026-10-25T12:00:00.000Z");

    expect(said).toEqual(["the local link closed; the turn events are dropped"]);
  });

  it("is not fatal when no guest init listens", async () => {
    const said: string[] = [];
    const facade = link(join(shortTmp(), "none.sock"), said);
    facade.open(() => undefined);
    facade.turnStart();
    await until(() => said.length === 1);

    expect(said[0]).toMatch(/^the local link closed: connect ENOENT .*; the turn events are dropped$/);
  });
});

describe("localLinkOf", () => {
  it("opens no link on sbx, and the guest socket under CH", () => {
    const ch = localLinkOf({ kind: "ch", sessionId: "0123abcd-0000-4000-8000-00000000c7c7" }, () => undefined);

    expect(localLinkOf(SBX, () => undefined)).toBeNull();
    expect(ch?.path).toBe(LOCAL_LINK_SOCKET);
    expect(ch?.stopMs).toBe(FLUE_STOP_MS);
    expect(FLUE_STOP_MS).toBeLessThan(FACADE_STOP_WAIT_MS);
  });
});

describe("the supervisor", () => {
  it("hands serve the link that it opens", async () => {
    const guest = await guestInit();
    const facade = link(guest.path, []);
    const served = new Promise<LocalLink | null>((resolve) => {
      void runSupervisor({
        input: new PassThrough(),
        stdout: { writeLine: () => undefined, exit: () => undefined },
        say: () => undefined,
        lock: new SupervisorLock(shortTmp(), "chat-s1"),
        deadlineMs: 60_000,
        serve: async (given) => {
          resolve(given);
          return { ok: false, detail: "FIXTURE-NO-FLUE" };
        },
        socketPath: null,
        link: facade,
      });
    });

    expect(await served).toBe(facade);
  });
});

describe("the stop line", () => {
  async function supervise(runtime: Runtime, stopMs: number): Promise<{ guest: GuestInit; events: string[]; said: string[]; exited: Promise<number> }> {
    const guest = await guestInit();
    const events: string[] = [];
    const said: string[] = [];
    const lock = new SupervisorLock(shortTmp(), "chat-s1");
    const exited = new Promise<number>((resolve) => {
      void runSupervisor({
        input: new PassThrough(),
        stdout: {
          writeLine: () => undefined,
          exit: (code) => {
            events.push(`exit ${code}`);
            resolve(code);
          },
        },
        say: (text) => said.push(text),
        lock,
        deadlineMs: 60_000,
        serve: async () => ({ ok: true, serving: { runtime, sessions: null } }),
        socketPath: null,
        link: link(guest.path, said, stopMs),
      });
    });

    return { guest, events, said, exited };
  }

  it("stops Flue, then exits 0", async () => {
    const events: string[] = [];
    const run = await supervise({ stop: async () => void events.push("stop") }, FLUE_STOP_MS);
    (await run.guest.peer()).write(STOP);

    expect(await run.exited).toBe(EXIT_OK);
    expect([...events, ...run.events]).toEqual(["stop", `exit ${EXIT_OK}`]);
    expect(run.said).toContain("the stop line of guest init");
  });

  it("exits at the bound when Flue does not stop", async () => {
    const run = await supervise({ stop: () => new Promise<void>(() => undefined) }, 50);
    const peer = await run.guest.peer();
    const sent = Date.now();
    peer.write(STOP);
    peer.write(STOP);

    expect(await run.exited).toBe(EXIT_OK);
    expect(Date.now() - sent).toBeLessThan(WAIT_MS);
    expect(run.events).toEqual([`exit ${EXIT_OK}`]);
    expect(run.said).toContain("Flue did not stop in 50 ms; the facade exits");
  });
});
