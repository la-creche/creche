// The lock, the beat deadline and the stop, in process: a supervisor on a
// stream that the test writes, with a fake Flue that records its stop.

import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";

import { afterEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { SupervisorLock } from "../src/lock.js";
import { EXIT_CONTROL_MOUNT, EXIT_DEADLINE, EXIT_LOCK_HELD, EXIT_OK, runSupervisor } from "../src/supervisor.js";
import { BEAT_DEADLINE_MS, LOCK_FILE } from "../src/vectors.js";
import { facadeEnv, startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";

const DEADLINE_MS = 120;
const LOCK_BEAT_MS = 20;

interface Run {
  readonly input: PassThrough;
  readonly lines: string[];
  readonly said: string[];
  readonly events: string[];
  readonly lockPath: string;
  readonly exited: Promise<number>;
}

const locks: SupervisorLock[] = [];
let running: FacadeProcess | null = null;

afterEach(() => {
  for (const lock of locks.splice(0)) {
    lock.release();
  }
  running?.child.kill();
  running = null;
});

function controlDir(): string {
  return mkdtempSync(join(tmpdir(), "facade-control-"));
}

function supervise(dir: string = controlDir()): Run {
  const input = new PassThrough();
  const lines: string[] = [];
  const said: string[] = [];
  const events: string[] = [];
  const lock = new SupervisorLock(dir, "chat-s1", LOCK_BEAT_MS);
  locks.push(lock);

  const exited = new Promise<number>((resolve) => {
    runSupervisor({
      input,
      stdout: {
        writeLine: (line) => lines.push(line),
        exit: (code) => {
          events.push(`exit ${code}`);
          resolve(code);
        },
      },
      say: (text) => said.push(text),
      lock,
      deadlineMs: DEADLINE_MS,
      runtime: {
        stop: async () => {
          events.push("stop");
        },
      },
      sessions: null,
    });
  });

  return { input, lines, said, events, lockPath: join(dir, LOCK_FILE), exited };
}

function send(run: Run, message: unknown): void {
  run.input.write(`${JSON.stringify(message)}\n`);
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

describe("the beat deadline", () => {
  it("exits 5 with a line on stderr when no beat arrives", async () => {
    const run = supervise();

    expect(await run.exited).toBe(EXIT_DEADLINE);
    expect(run.events).toEqual(["stop", `exit ${EXIT_DEADLINE}`]);
    expect(run.said.at(-1)).toContain("no beat");
    expect(existsSync(run.lockPath)).toBe(false);
  });

  it("moves on with each beat", async () => {
    const run = supervise();
    for (let at = 0; at < 6; at += 1) {
      send(run, { ...vectors.beat.request, id: at });
      await sleep(DEADLINE_MS / 3);
    }

    expect(run.events).toEqual([]);
    expect(run.lines).toHaveLength(6);
    expect(await run.exited).toBe(EXIT_DEADLINE);
  });

  it("waits three beat windows, attendance's host deadline", () => {
    expect(BEAT_DEADLINE_MS).toBe(90_000);
  });
});

describe("_creche/shutdown", () => {
  it("stops Flue, removes the lock, then exits 0", async () => {
    const run = supervise();
    send(run, vectors.shutdown.notification);

    expect(await run.exited).toBe(vectors.shutdown.exit_status);
    expect(run.events).toEqual(["stop", `exit ${EXIT_OK}`]);
    expect(existsSync(run.lockPath)).toBe(false);
    expect(run.lines).toEqual([]);
  });

  it("ends the built facade with exit status 0", async () => {
    running = startFacade();
    running.send(vectors.initialize.request);
    await running.nextMessage();
    running.send(vectors.shutdown.notification);

    expect(await running.exited()).toBe(vectors.shutdown.exit_status);
  });
});

describe("the lock", () => {
  it("beats with a counter one higher and names this process", async () => {
    const run = supervise();
    const first = JSON.parse(readFileSync(run.lockPath, "utf8")) as Record<string, unknown>;
    await sleep(LOCK_BEAT_MS * 4);
    const later = JSON.parse(readFileSync(run.lockPath, "utf8")) as Record<string, unknown>;

    expect(first["pid"]).toBe(process.pid);
    expect(first["sandbox"]).toBe("chat-s1");
    expect(first["beat"]).toBe(1);
    expect(later["beat"]).toBeGreaterThan(1);
    send(run, vectors.shutdown.notification);
    await run.exited;
  });

  it("refuses a second facade while a live pid holds the lock", async () => {
    const dir = controlDir();
    writeFileSync(join(dir, LOCK_FILE), JSON.stringify({ pid: process.ppid, beat: 9 }));
    const run = supervise(dir);

    expect(await run.exited).toBe(EXIT_LOCK_HELD);
    expect(run.events).toEqual([`exit ${EXIT_LOCK_HELD}`]);
    expect(readFileSync(join(dir, LOCK_FILE), "utf8")).toContain('"beat":9');
  });

  it("takes the lock of a dead pid", async () => {
    const dir = controlDir();
    writeFileSync(join(dir, LOCK_FILE), JSON.stringify({ pid: 2 ** 22 + 1, beat: 9 }));
    const run = supervise(dir);
    send(run, vectors.shutdown.notification);

    expect(await run.exited).toBe(EXIT_OK);
  });

  it("exits 6 when the control mount cannot be written", async () => {
    const file = join(controlDir(), "not-a-dir");
    writeFileSync(file, "");
    const run = supervise(file);

    expect(await run.exited).toBe(EXIT_CONTROL_MOUNT);
    expect(run.said[0]).toContain("cannot write the lock");
  });

  it("exits 2 with no sandbox id and 7 with no control mount", async () => {
    const env = facadeEnv();
    const noSandbox = startFacade({ ...env, AGENT_SANDBOX: "" });
    const noControl = startFacade({ ...env, AGENT_CONTROL_DIR: "" });

    expect(await noSandbox.exited()).toBe(2);
    expect(await noControl.exited()).toBe(7);
    expect(noSandbox.stdout().length + noControl.stdout().length).toBe(0);
  });
});
