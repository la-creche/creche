// The local link of the facade (decisions CH21, CH32 rule 7, CH62 rules 9
// to 11, CH67 rules 7 and 8), against a Unix socket that the test serves
// as guest init does: the turn events up, the stop line down, the loss of
// the link, and the exit of the supervisor at a stop line or a lost link.

import { randomUUID } from "node:crypto";
import { join } from "node:path";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { read } from "agent/wire-read";
import { afterAll, afterEach, describe, expect, it } from "vitest";

import { facadeApp } from "../src/acp.js";

import { CONTROL_LINE_CAP, FACADE_STOP_WAIT_MS, LOCAL_LINK_SOCKET } from "../src/guest.js";
import { FLUE_STOP_MS, guestLink, LocalLink, turnLine } from "../src/local-link.js";
import type { LinkHandlers } from "../src/local-link.js";
import { sessionService } from "../src/service.js";
import { EXIT_LOCAL_LINK, EXIT_OK, runSupervisor } from "../src/supervisor.js";
import type { Runtime } from "../src/supervisor.js";
import { Wire } from "./acp-wire.js";
import { shortTmp } from "./facade-process.js";
import { faux, Probe, startFlue } from "./faux-flue.js";
import { closeGuestInits, guestInit, NO_HANDLERS, until, WAIT_MS } from "./guest-init.js";
import type { GuestInit } from "./guest-init.js";

const STOP = '{"stop":{}}\n';

/** A beat deadline that a CH supervisor must not keep. */
const SHORT_DEADLINE_MS = 30;

const flue = await startFlue();
afterAll(() => flue.stop());

afterEach(closeGuestInits);

function link(path: string, said: string[], stopMs = FLUE_STOP_MS): LocalLink {
  return new LocalLink(path, stopMs, (text) => said.push(text));
}

/** Handlers that count the stop lines. */
function counting(): { handlers: LinkHandlers; stops: () => number } {
  let stops = 0;
  return { handlers: { onStop: () => (stops += 1), onLost: () => undefined }, stops: () => stops };
}

describe("the turn events", () => {
  it("go up as one wire TurnEvent line each", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const facade = link(guest.path, said);
    facade.open(NO_HANDLERS);
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
    facade.open(NO_HANDLERS);
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
    facade.open(NO_HANDLERS);
    const id = randomUUID();
    const service = sessionService({ agent: Probe, agentName: "Probe", sessionId: id, link: facade, store: flue.store });
    const wire = new Wire(facadeApp({ onBeat: () => undefined, onShutdown: () => undefined }, service));
    // The start goes up while Flue runs the turn, not after: the vmm must
    // see a running turn (decisions CH19 rule 6, CH32 rule 6).
    let duringTurn = "";
    faux.setResponses([
      async () => {
        await until(() => guest.received().length > 0).catch(() => undefined);
        duringTurn = guest.received();
        return fauxAssistantMessage("done");
      },
      fauxAssistantMessage("again"),
    ]);
    for (const text of ["  Plan the   garden \nsecond line", "Another thing"]) {
      await wire.request("session/prompt", { sessionId: id, prompt: [{ type: "text", text }] });
    }
    await until(() => guest.received().split("\n").length === 5);

    expect(duringTurn).toBe('{"start":{}}\n');
    const times = wire.updates(id).flatMap((update) => (update["sessionUpdate"] === "session_info_update" ? [Date.parse(String(update["updatedAt"]))] : []));
    expect(times).toHaveLength(2);
    expect(guest.received().split("\n").slice(0, 4).map((line) => JSON.parse(line) as unknown)).toEqual([
      { start: {} },
      { end: { title: "Plan the garden", updated_at: times[0] } },
      { start: {} },
      { end: { title: "Another thing", updated_at: times[1] } },
    ]);
  });
});

describe("the down lines", () => {
  it("run the stop at a stop line, also in two pieces", async () => {
    const guest = await guestInit();
    const { handlers, stops } = counting();
    link(guest.path, []).open(handlers);
    const peer = await guest.peer();
    peer.write('{"st');
    await new Promise((resolve) => setTimeout(resolve, 20));
    peer.write('op":{}}\n');
    await until(() => stops() === 1);

    expect(stops()).toBe(1);
  });

  it("take a line of exactly the cap, and drop one byte more", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const { handlers, stops } = counting();
    link(guest.path, said).open(handlers);
    const peer = await guest.peer();
    const pad = (bytes: number): string => `{"stop":{}${" ".repeat(bytes - STOP.length)}}\n`;
    peer.write(pad(CONTROL_LINE_CAP + 1));
    peer.write(pad(CONTROL_LINE_CAP + 2));
    await until(() => stops() === 1 && said.length === 1);

    expect(said).toEqual(["a local link line is refused (too_large); it is dropped"]);
  });

  // Decision CH67 rule 7: a line that is not stop is dropped, and the link
  // stays open.
  it("drop each other line and keep the link", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const { handlers, stops } = counting();
    const facade = link(guest.path, said);
    facade.open(handlers);
    const peer = await guest.peer();
    peer.write('{"sync":{}}\nnot json\n{"stop":{},"stop":{}}\n{"stop":{"x":1}}\n');
    peer.write("x".repeat(CONTROL_LINE_CAP));
    peer.write("x".repeat(CONTROL_LINE_CAP));
    await until(() => said.length === 5);
    peer.write(`xx\n${STOP}`);
    await until(() => stops() === 1);
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

// Decision CH67 rule 8: the link tells the supervisor once when it does
// not open, or closes or faults, and drops each later turn event.
describe("a lost link", () => {
  /** Opens a link on `path`, and keeps each loss that it reports. */
  function watched(path: string): { facade: LocalLink; lost: string[]; said: string[] } {
    const lost: string[] = [];
    const said: string[] = [];
    const facade = link(path, said);
    facade.open({ onStop: () => undefined, onLost: (detail) => lost.push(detail) });

    return { facade, lost, said };
  }

  it("is reported when no guest init listens", async () => {
    const { facade, lost, said } = watched(join(shortTmp(), "none.sock"));
    await until(() => lost.length === 1);
    facade.turnStart();

    expect(lost[0]).toMatch(/^the local link did not open: connect ENOENT .*none\.sock$/);
    expect(lost).toHaveLength(1);
    expect(said).toEqual([]);
  });

  it("is reported when guest init closes the link", async () => {
    const guest = await guestInit();
    const { facade, lost, said } = watched(guest.path);
    (await guest.peer()).destroy();
    await until(() => lost.length === 1);
    facade.turnStart();
    facade.turnEnd("a turn", "2026-10-25T12:00:00.000Z");

    expect(lost).toEqual(["the local link closed"]);
    expect(said).toEqual([]);
  });

  it("is reported with the fault when a write fails", async () => {
    const guest = await guestInit();
    const { facade, lost } = watched(guest.path);
    // The peer is gone before the facade reads its end, so the write
    // fails with EPIPE, a real fault of the socket.
    (await guest.peer()).destroy();
    facade.turnStart();
    await until(() => lost.length === 1);

    expect(lost).toEqual(["the local link closed: write EPIPE"]);
  });
});

describe("guestLink", () => {
  it("dials the guest socket, with a stop under the stop wait", () => {
    const ch = guestLink(() => undefined);

    expect(ch.path).toBe(LOCAL_LINK_SOCKET);
    expect(ch.stopMs).toBe(FLUE_STOP_MS);
    expect(FLUE_STOP_MS).toBeLessThan(FACADE_STOP_WAIT_MS);
  });
});

describe("the supervisor", () => {
  it("hands serve the link that it opens", async () => {
    const guest = await guestInit();
    const facade = link(guest.path, []);
    const served = new Promise<LocalLink | null>((resolve) => {
      void runSupervisor({
        supervision: { kind: "ch", link: facade },
        stdout: { writeLine: () => undefined, exit: () => undefined },
        say: () => undefined,
        deadlineMs: 60_000,
        serve: async (given) => {
          resolve(given);
          return { ok: false, detail: "FIXTURE-NO-FLUE" };
        },
        socketPath: null,
      });
    });

    expect(await served).toBe(facade);
  });
});

/**
 * A supervisor on a link to a test guest init, or to `path` when it is
 * given. It keeps the link, each exit and each stderr line.
 */
async function supervise(
  runtime: Runtime,
  stopMs: number,
  path: string | null = null,
): Promise<{ guest: GuestInit; facade: LocalLink; events: string[]; said: string[]; exited: Promise<number> }> {
  const guest = await guestInit();
  const events: string[] = [];
  const said: string[] = [];
  const facade = link(path ?? guest.path, said, stopMs);
  const exited = new Promise<number>((resolve) => {
    void runSupervisor({
      supervision: { kind: "ch", link: facade },
      stdout: {
        writeLine: () => undefined,
        exit: (code) => {
          events.push(`exit ${code}`);
          resolve(code);
        },
      },
      say: (text) => said.push(text),
      deadlineMs: 60_000,
      serve: async () => ({ ok: true, serving: { runtime, sessions: null } }),
      socketPath: null,
    });
  });

  return { guest, facade, events, said, exited };
}

// Decisions CH5d, CH19, CH80: under CH no beat deadline runs and no stdio
// serves, so only the stop line and a lost link end the facade.
describe("the CH supervisor", () => {
  it("keeps serving past the beat deadline, then exits 0 at the stop line", async () => {
    const guest = await guestInit();
    const said: string[] = [];
    const events: string[] = [];
    const exited = new Promise<number>((resolve) => {
      void runSupervisor({
        supervision: { kind: "ch", link: link(guest.path, said) },
        stdout: {
          writeLine: (line) => events.push(`stdout ${line}`),
          exit: (code) => {
            events.push(`exit ${code}`);
            resolve(code);
          },
        },
        say: (text) => said.push(text),
        deadlineMs: SHORT_DEADLINE_MS,
        serve: async () => ({ ok: true, serving: { runtime: null, sessions: null } }),
        socketPath: null,
      });
    });
    const peer = await guest.peer();
    await new Promise((resolve) => setTimeout(resolve, SHORT_DEADLINE_MS * 5));

    expect(events).toEqual([]);
    peer.write(STOP);
    expect(await exited).toBe(EXIT_OK);
    expect(events).toEqual([`exit ${EXIT_OK}`]);
    expect(said).toEqual(["serving ACP v1 on the relay socket", "the stop line of guest init"]);
  });
});

describe("the stop line", () => {
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

// Decision CH67 rule 8: outside a stop, a lost link stops Flue and exits
// 18, so that guest init starts the facade again. Inside a stop, the stop
// goes on (decision CH66 rule 5).
describe("a lost link at the supervisor", () => {
  const LOST = ["stop", `exit ${EXIT_LOCAL_LINK}`];

  /** A runtime that records its stop in `events`. */
  function recorded(events: string[]): Runtime {
    return { stop: async () => void events.push("stop") };
  }

  it("exits 18 when the link does not open", async () => {
    const events: string[] = [];
    const run = await supervise(recorded(events), FLUE_STOP_MS, join(shortTmp(), "none.sock"));

    expect(await run.exited).toBe(EXIT_LOCAL_LINK);
    expect([...events, ...run.events]).toEqual(LOST);
    expect(run.said.at(-1)).toMatch(/^the local link did not open: connect ENOENT .*none\.sock; the facade exits$/);
  });

  it("exits 18 at the bound when Flue does not stop", async () => {
    const sent = Date.now();
    const run = await supervise({ stop: () => new Promise<void>(() => undefined) }, 50, join(shortTmp(), "none.sock"));

    expect(await run.exited).toBe(EXIT_LOCAL_LINK);
    expect(Date.now() - sent).toBeLessThan(WAIT_MS);
    expect(run.events).toEqual([`exit ${EXIT_LOCAL_LINK}`]);
    expect(run.said).toContain("Flue did not stop in 50 ms; the facade exits");
  });

  it("exits 18 when guest init closes the link outside a stop", async () => {
    const events: string[] = [];
    const run = await supervise(recorded(events), FLUE_STOP_MS);
    (await run.guest.peer()).destroy();

    expect(await run.exited).toBe(EXIT_LOCAL_LINK);
    expect([...events, ...run.events]).toEqual(LOST);
    expect(run.said.at(-1)).toBe("the local link closed; the facade exits");
  });

  it("exits 18 when the link faults", async () => {
    const events: string[] = [];
    const run = await supervise(recorded(events), FLUE_STOP_MS);
    (await run.guest.peer()).destroy();
    run.facade.turnStart();

    expect(await run.exited).toBe(EXIT_LOCAL_LINK);
    expect([...events, ...run.events]).toEqual(LOST);
    expect(run.said.at(-1)).toBe("the local link closed: write EPIPE; the facade exits");
  });

  it("keeps the stop path when the link closes during a stop", async () => {
    let release = (): void => undefined;
    const stopping = new Promise<void>((resolve) => {
      release = resolve;
    });
    let stops = 0;
    const run = await supervise(
      {
        stop: () => {
          stops += 1;
          return stopping;
        },
      },
      FLUE_STOP_MS,
    );
    const peer = await run.guest.peer();
    peer.write(STOP);
    await until(() => stops === 1);
    peer.destroy();
    await new Promise((resolve) => setTimeout(resolve, 50));
    release();

    expect(await run.exited).toBe(EXIT_OK);
    expect(run.events).toEqual([`exit ${EXIT_OK}`]);
    expect(stops).toBe(1);
    expect(run.said).toEqual(["serving ACP v1 on the relay socket", "the stop line of guest init"]);
  });
});
