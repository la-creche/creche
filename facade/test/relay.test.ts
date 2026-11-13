// The relay socket: one facade in process, on faux Flue, under a
// supervisor on a test guest init (`test/guest-init.ts`). Each client dials
// the socket, as guest init joins each ACP link of guest port 1201 to it
// (decisions CH5e, CH62 rule 5).

import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { connect } from "node:net";
import type { Socket } from "node:net";
import { join } from "node:path";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { afterAll, afterEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { FLUE_STOP_MS, LocalLink } from "../src/local-link.js";
import { sessionService } from "../src/service.js";
import { EXIT_OK, runSupervisor } from "../src/supervisor.js";
import { shortTmp } from "./facade-process.js";
import { faux, Probe, startFlue } from "./faux-flue.js";
import { closeGuestInits, guestInit, until } from "./guest-init.js";
import type { GuestInit } from "./guest-init.js";

const STOP = '{"stop":{}}\n';

const flue = await startFlue([Probe]);
afterAll(() => flue.stop());

interface Facade {
  /** The id of the facade's one session. */
  readonly id: string;
  readonly socket: string;
  readonly guest: GuestInit;
  readonly exited: Promise<number>;
}

/** One connection to the relay socket: JSON-RPC lines out, each line in kept. */
interface Client {
  send(message: unknown): void;
  /** The next whole line, parsed. */
  nextMessage(): Promise<Record<string, unknown>>;
}

const facades: Facade[] = [];
const clients: Socket[] = [];

afterEach(async () => {
  for (const socket of clients.splice(0)) {
    socket.destroy();
  }
  for (const facade of facades.splice(0)) {
    (await facade.guest.peer()).write(STOP);
    await facade.exited;
  }
  closeGuestInits();
});

async function facade(): Promise<Facade> {
  const guest = await guestInit();
  const socket = join(shortTmp(), "creche-facade", "acp.sock");
  const id = randomUUID();
  let listening: () => void = () => undefined;
  const ready = new Promise<void>((resolve) => (listening = resolve));
  const say = (text: string): void => {
    if (text.startsWith("serving")) {
      listening();
    }
  };

  const exited = new Promise<number>((resolve) => {
    void runSupervisor({
      link: new LocalLink(guest.path, FLUE_STOP_MS, say),
      exit: resolve,
      say,
      serve: async (link) => ({
        ok: true,
        serving: { runtime: null, sessions: sessionService({ agent: Probe, agentName: "Probe", sessionId: id, link, store: flue.store }) },
      }),
      socketPath: socket,
    });
  });
  await ready;
  const made = { id, socket, guest, exited };
  facades.push(made);

  return made;
}

function client(on: Facade): Client {
  const socket = connect(on.socket);
  clients.push(socket);
  let text = "";
  socket.on("data", (chunk: Buffer) => {
    text += chunk.toString("utf8");
  });

  return {
    send: (message) => socket.write(`${JSON.stringify(message)}\n`),
    nextMessage: async () => {
      await until(() => text.includes("\n"));
      const end = text.indexOf("\n");
      const line = text.slice(0, end);
      text = text.slice(end + 1);

      return JSON.parse(line) as Record<string, unknown>;
    },
  };
}

async function request(on: Client, id: number, method: string, params: unknown): Promise<Record<string, unknown>> {
  on.send({ jsonrpc: "2.0", id, method, params });
  for (;;) {
    const message = await on.nextMessage();
    if (message["id"] === id) {
      return message;
    }
  }
}

describe("the relay socket", () => {
  it("answers initialize on each connection", async () => {
    const on = await facade();
    const one = client(on);
    one.send(vectors.initialize.request);

    expect(await one.nextMessage()).toEqual(vectors.initialize.response);
  });

  it("fans a turn out to every connection on the session", async () => {
    const on = await facade();
    const [first, second] = [client(on), client(on)];
    const sessionId = on.id;
    expect((await request(second, 1, "session/load", { sessionId, cwd: "/work", mcpServers: [] }))["result"]).toEqual({});

    faux.setResponses([fauxAssistantMessage("Hello, both.")]);
    expect((await request(first, 2, "session/prompt", { sessionId, prompt: [{ type: "text", text: "Hi" }] }))["result"]).toEqual({
      stopReason: "end_turn",
    });

    const seen: string[] = [];
    while (!seen.includes("session_info_update")) {
      const message = (await second.nextMessage()) as { params: { update: { sessionUpdate: string } } };
      seen.push(message.params.update.sessionUpdate);
    }
    expect(seen[0]).toBe("agent_message_chunk");
  });

  it("removes the socket at the stop line", async () => {
    const on = await facade();
    expect(existsSync(on.socket)).toBe(true);

    facades.splice(facades.indexOf(on), 1);
    (await on.guest.peer()).write(STOP);
    expect(await on.exited).toBe(EXIT_OK);
    expect(existsSync(on.socket)).toBe(false);
  });
});
