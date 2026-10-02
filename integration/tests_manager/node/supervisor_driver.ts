// Drives the REAL supervisor against the directories `apply_once` wrote.
//
//   node <bundle> <report.json>
//
// `supervisor/src/index.ts` is the composition root and hardcodes the three
// mount points a sandbox has (`/run/family`, `/run/family-config`,
// `/run/control`). A Mac has none of them, and `supervisor/test/harness.ts`
// solves that by building `Supervisor` with the same constructor arguments
// and its own temporary directories. This file is that harness with one
// change: the directories are not invented here, they are the ones
// `managerd` wrote.
//
//   apply_once ──► families/chat/creds/   ──► CredReader
//              ├─► families/chat/config/  ──► configDir (runtime.json, …)
//              └─► families/chat/control/ ──► SupervisorLock, TurnFile
//
// pi itself is `supervisor/test/fake-pi.mjs`, unchanged. Everything else --
// the framing, the validator, the pool, the environment builder, the flag
// builder -- is the real thing.

import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";

import { Channel } from "../../../supervisor/src/channel.js";
import { CredReader } from "../../../supervisor/src/creds.js";
import { LineReader } from "../../../supervisor/src/framing.js";
import { SupervisorLock } from "../../../supervisor/src/lock.js";
import type { PiSpawnSpec } from "../../../supervisor/src/pi-process.js";
import { ProcessRecords } from "../../../supervisor/src/process-record.js";
import { Supervisor } from "../../../supervisor/src/supervisor.js";
import { ToolStateFile } from "../../../supervisor/src/tool-state.js";
import { TurnFile } from "../../../supervisor/src/turn-file.js";

/** Everything the Python side needs, read from one file. */
interface Report {
  started: boolean;
  lines: Record<string, unknown>[];
  spawns: { args: string[]; env: Record<string, string> }[];
  notes: string[];
  exitCode: number | null;
}

interface Settings {
  readonly reportPath: string;
  readonly credsDir: string;
  readonly configDir: string;
  readonly controlDir: string;
  readonly sessionsRoot: string;
  readonly bridgePath: string;
  readonly fakePi: string;
  readonly sandbox: string;
  readonly family: string;
  readonly session: string;
  readonly turn: string;
}

const WAIT_STEP_MS = 5;
const WAIT_LIMIT_MS = 15000;
const EXIT_USAGE = 2;

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value.length === 0) {
    process.stderr.write(`supervisor_driver: ${name} is required\n`);
    process.exit(EXIT_USAGE);
  }

  return value;
}

function settings(): Settings {
  const reportPath = process.argv[2];
  if (reportPath === undefined) {
    process.stderr.write("usage: supervisor_driver <report.json>\n");
    process.exit(EXIT_USAGE);
  }

  return {
    reportPath,
    credsDir: required("DRIVER_CREDS_DIR"),
    configDir: required("DRIVER_CONFIG_DIR"),
    controlDir: required("DRIVER_CONTROL_DIR"),
    sessionsRoot: required("DRIVER_SESSIONS_ROOT"),
    bridgePath: required("DRIVER_BRIDGE_PATH"),
    fakePi: required("DRIVER_FAKE_PI"),
    sandbox: required("DRIVER_SANDBOX"),
    family: required("DRIVER_FAMILY"),
    session: required("DRIVER_SESSION"),
    turn: required("DRIVER_TURN"),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Polls until `check` passes, or gives up and lets the report say so. */
async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + WAIT_LIMIT_MS;
  while (!check()) {
    if (Date.now() >= deadline) {
      return;
    }

    await sleep(WAIT_STEP_MS);
  }
}

function flatEnv(env: NodeJS.ProcessEnv): Record<string, string> {
  const flat: Record<string, string> = {};
  for (const [name, value] of Object.entries(env)) {
    if (value !== undefined) {
      flat[name] = value;
    }
  }

  return flat;
}

async function main(): Promise<void> {
  const config = settings();
  const report: Report = { started: false, lines: [], spawns: [], notes: [], exitCode: null };

  const reader = new LineReader(
    (line) => report.lines.push(JSON.parse(line) as Record<string, unknown>),
    () => report.notes.push("the supervisor emitted an oversize line"),
  );

  const out = new Writable({
    write: (chunk: Buffer, _encoding, done) => {
      reader.feed(chunk.toString("utf8"));
      done();
    },
  });

  const stdin = new PassThrough();
  const supervisor = new Supervisor({
    sandbox: config.sandbox,
    input: stdin,
    channel: new Channel(out),
    launcher: (spec: PiSpawnSpec): ChildProcess => {
      report.spawns.push({ args: [...spec.args], env: flatEnv(spec.env) });

      return spawn(process.execPath, [config.fakePi, ...spec.args], {
        cwd: spec.cwd,
        env: spec.env,
        stdio: ["pipe", "pipe", "pipe"],
      });
    },
    lock: new SupervisorLock(config.controlDir),
    creds: new CredReader(config.credsDir),
    configDir: config.configDir,
    turnFile: new TurnFile(config.controlDir),
    toolState: new ToolStateFile(config.controlDir),
    // Contract 03 §7.5, beside the turn files in the same control mount.
    processes: new ProcessRecords(config.controlDir, config.sandbox),
    bridgePath: config.bridgePath,
    codeSandboxRoot: join(config.sessionsRoot, "code-sandbox"),
    codeSandboxLinkPrefix: join(config.sessionsRoot, "link-"),
    onExit: (code) => {
      report.exitCode = code;
    },
  });

  report.started = supervisor.start();
  await until(() => report.lines.some((line) => line["type"] === "ready"));

  const cwd = join(config.sessionsRoot, config.session);
  const sessionDir = join(cwd, "pi");
  mkdirSync(sessionDir, { recursive: true });

  send(stdin, {
    type: "hello",
    protocol: "1.0",
    host: "integration/tests_manager",
    family: config.family,
    sandbox: config.sandbox,
    coalesce_ms: 0,
    pi_idle_ttl_s: 900,
    max_resident_processes: 12,
    host_deadline_s: 90,
    env_epoch: 1,
  });
  send(stdin, {
    type: "start_turn",
    turn: config.turn,
    session: config.session,
    cwd,
    session_dir: sessionDir,
    prompt: "Which sensor dropped out last night?",
    deadline_s: 30,
    env_epoch: 1,
    config_rev: "reg-integration",
  });

  await until(() => report.spawns.length > 0);
  await until(() => report.lines.some((line) => line["type"] === "turn_settled"));

  await supervisor.shutdown(0, 0);
  stdin.destroy();
  writeFileSync(config.reportPath, `${JSON.stringify(report, null, 2)}\n`);
}

function send(stdin: PassThrough, message: Record<string, unknown>): void {
  stdin.write(`${JSON.stringify(message)}\n`);
}

main()
  .then(() => process.exit(0))
  .catch((error: unknown) => {
    const detail = error instanceof Error ? (error.stack ?? error.message) : String(error);
    process.stderr.write(`supervisor_driver: ${detail}\n`);
    process.exit(1);
  });
