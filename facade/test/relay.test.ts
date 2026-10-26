// The relay socket: one facade in process, on faux Flue, and the built
// `acp-relay` bin as each door connection's pipe to it.

import { existsSync, mkdtempSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { PassThrough } from "node:stream";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { afterEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { SupervisorLock } from "../src/lock.js";
import { sessionService } from "../src/service.js";
import { SessionIndex } from "../src/session-index.js";
import { EXIT_OK, runSupervisor } from "../src/supervisor.js";
import { RELAY_BIN, shortTmp, startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";
import { faux, Probe, startFlue } from "./faux-flue.js";

const DEADLINE_MS = 300;

const flue = await startFlue([Probe]);

interface Facade {
  readonly tmp: string;
  readonly socket: string;
  readonly input: PassThrough;
  readonly said: string[];
  readonly exited: Promise<number>;
}

const running: FacadeProcess[] = [];
const facades: Facade[] = [];

afterEach(async () => {
  for (const relay of running.splice(0)) {
    relay.child.kill();
  }
  for (const facade of facades.splice(0)) {
    facade.input.write(`${JSON.stringify(vectors.shutdown.notification)}\n`);
    await facade.exited;
  }
});

async function facade(): Promise<Facade> {
  const tmp = shortTmp();
  const socket = join(tmp, "creche-facade", "acp.sock");
  const input = new PassThrough();
  const said: string[] = [];
  const index = new SessionIndex(new DatabaseSync(":memory:"));
  let listening: () => void = () => undefined;
  const ready = new Promise<void>((resolve) => (listening = resolve));

  const exited = new Promise<number>((resolve) => {
    void runSupervisor({
      input,
      stdout: { writeLine: () => undefined, exit: resolve },
      say: (text) => {
        said.push(text);
        if (text.startsWith("serving")) {
          listening();
        }
      },
      lock: new SupervisorLock(mkdtempSync(join(tmpdir(), "relay-control-")), "chat-s1"),
      deadlineMs: DEADLINE_MS,
      serve: async () => ({
        ok: true,
        serving: { runtime: null, sessions: sessionService({ agent: Probe, agentName: "Probe", index, store: flue.store }) },
      }),
      socketPath: socket,
    });
  });
  await ready;
  const made = { tmp, socket, input, said, exited };
  facades.push(made);

  return made;
}

function relay(on: Facade): FacadeProcess {
  const proc = startFacade({ TMPDIR: on.tmp }, RELAY_BIN);
  running.push(proc);

  return proc;
}

async function request(proc: FacadeProcess, id: number, method: string, params: unknown): Promise<Record<string, unknown>> {
  proc.send({ jsonrpc: "2.0", id, method, params });
  for (;;) {
    const message = (await proc.nextMessage()) as Record<string, unknown>;
    if (message["id"] === id) {
      return message;
    }
  }
}

describe("acp-relay", () => {
  it("pipes a client to the facade's socket", async () => {
    const on = await facade();
    const client = relay(on);
    client.send(vectors.initialize.request);

    expect(await client.nextMessage()).toEqual(vectors.initialize.response);
    client.send(vectors.beat.request);
    expect(await client.nextMessage()).toEqual(vectors.beat.response);
  });

  it("exits 1 with a line on stderr when no facade listens", async () => {
    const proc = startFacade({ TMPDIR: shortTmp() }, RELAY_BIN);

    expect(await proc.exited()).toBe(1);
    expect(proc.stdout().length).toBe(0);
    expect(proc.stderr()).toContain("no facade at the relay socket");
  });

  it("exits 0 when its client closes stdin", async () => {
    const on = await facade();
    const client = relay(on);
    client.send(vectors.initialize.request);
    await client.nextMessage();
    client.child.stdin.end();

    expect(await client.exited()).toBe(EXIT_OK);
  });
});

describe("the relay socket", () => {
  it("fans a turn out to every connection on the session", async () => {
    const on = await facade();
    const [first, second] = [relay(on), relay(on)];
    const made = await request(first, 1, "session/new", { cwd: "/work", mcpServers: [] });
    const sessionId = (made["result"] as { sessionId: string }).sessionId;
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

  it("closes a connection whose beats stop, and only that one", async () => {
    const on = await facade();
    const quiet = connect(on.socket);
    const closed = new Promise<void>((resolve) => quiet.on("close", () => resolve()));
    quiet.resume();
    quiet.write(`${JSON.stringify(vectors.beat.request)}\n`);
    const client = relay(on);

    await closed;
    client.send(vectors.initialize.request);
    expect(await client.nextMessage()).toEqual(vectors.initialize.response);
    expect(on.said).toContain("a relay connection sent no beat in time; it is closed");
  });

  it("keeps the facade up on _creche/shutdown from a relay, and removes the socket at the stop", async () => {
    const on = await facade();
    const client = relay(on);
    client.send(vectors.shutdown.notification);
    client.send(vectors.initialize.request);

    expect(await client.nextMessage()).toEqual(vectors.initialize.response);
    expect(existsSync(on.socket)).toBe(true);

    facades.splice(facades.indexOf(on), 1);
    on.input.write(`${JSON.stringify(vectors.shutdown.notification)}\n`);
    expect(await on.exited).toBe(EXIT_OK);
    expect(existsSync(on.socket)).toBe(false);
  });
});
