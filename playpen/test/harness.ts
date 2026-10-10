// One playpen, its four mounts and the fake pi, wired up for a test.
//
//   test ──► stdin (PassThrough) ──► Playpen ──► fake-pi.mjs (real process)
//                                        │
//        collected lines ◄── stdout ◄────┘
//
// Everything here is real except pi itself: the real framing, the real
// validator, the real pool, real child processes, real files on disk. No
// service is touched and nothing needs sbx or the host.

import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { PassThrough, Writable } from "node:stream";

import { Channel } from "../src/channel.js";
import { LOCK_BEAT_MS } from "../src/constants.js";
import { CredReader } from "../src/creds.js";
import { LineReader } from "../src/framing.js";
import { PlaypenLock } from "../src/lock.js";
import type { PiLauncher, PiSpawnSpec } from "../src/pi-process.js";
import { ProcessRecords } from "../src/process-record.js";
import type { HelloMessage, PlaypenMessage } from "../src/protocol.js";
import { Playpen } from "../src/playpen.js";
import { ToolStateFile } from "../src/tool-state.js";
import { TurnFile } from "../src/turn-file.js";

const FAKE_PI = fileURLToPath(new URL("./fake-pi.mjs", import.meta.url));

/**
 * Obvious fixtures, not credentials. Invariant 13 forbids a real secret in a
 * test fixture; these two strings exist only so a test can prove the values
 * reach the child's environment and never its argv.
 */
export const FIXTURE_LITELLM_KEY = "FIXTURE-LITELLM-KEY";
export const FIXTURE_PEP_TOKEN = "FIXTURE-PEP-TOKEN";

export type EnvBySession = Readonly<Record<string, Readonly<Record<string, string>>>>;

export interface HarnessOptions {
  readonly sandbox?: string;
  readonly epoch?: number;
  readonly runtime?: Record<string, unknown>;
  /** Extra child environment, per session id. Production env logic is untouched. */
  readonly piEnv?: EnvBySession;
  /** false stands in for a sandbox image built without the PEP bridge. */
  readonly bridge?: boolean;
  /** `lock_beat_s` in milliseconds, so no test sleeps for real seconds. */
  readonly lockBeatMs?: number;
  /** A control directory of the test's own, for the unwritable-mount case. */
  readonly controlDir?: string;
  /** A turn file of the test's own, for a handler that throws. */
  readonly turnFile?: (controlDir: string) => TurnFile;
  /** A launcher of the test's own, around the real one, for a start that throws. */
  readonly launcher?: (real: PiLauncher) => PiLauncher;
  /** How long a start waits for the credential file, for a start that a test holds open. */
  readonly credRetryMs?: number;
}

/**
 * A launcher that runs the fake pi under this same Node.
 *
 * It adds test-only variables to the child's environment rather than teaching
 * `buildTurnEnv` about them, so the environment the production path builds is
 * exactly the environment under test.
 */
function fakeLauncher(piEnv: EnvBySession, seen: PiSpawnSpec[]): PiLauncher {
  return (spec: PiSpawnSpec): ChildProcess => {
    seen.push(spec);
    const session = spec.env["AGENT_SESSION"] ?? "";
    const env = { ...spec.env, ...(piEnv[session] ?? {}) };

    return spawn(process.execPath, [FAKE_PI, ...spec.args], {
      cwd: spec.cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
  };
}

export class Harness {
  public readonly root: string;
  public readonly lines: PlaypenMessage[] = [];
  /** The text of each line, as the playpen wrote it. */
  public readonly rawLines: string[] = [];
  public readonly notes: string[] = [];
  /** Every pi process the pool asked for, in order. Its argv and its env. */
  public readonly spawns: PiSpawnSpec[] = [];
  /** Where a sandbox image would carry the bridge bundle. See `bridgePath`. */
  public readonly bridgePath: string;
  public exitCode: number | null = null;

  private readonly stdin = new PassThrough();
  private readonly playpen: Playpen;
  private readonly sandboxId: string;

  public constructor(options: HarnessOptions = {}) {
    this.sandboxId = options.sandbox ?? "chat-s1";
    this.root = mkdtempSync(join(tmpdir(), "playpen-test-"));

    for (const dir of ["creds", "config", "control", "sessions", "code-sandbox"]) {
      mkdirSync(join(this.root, dir), { recursive: true });
    }

    this.writeCreds(options.epoch ?? 1);
    this.writeRuntime(options.runtime ?? {});

    // The file only has to EXIST: `fake-pi.mjs` ignores `--extension`, and
    // what the bridge itself does is `test/bridge.test.ts`'s subject.
    this.bridgePath = join(this.root, "config", "pep-bridge.js");
    if (options.bridge !== false) {
      writeFileSync(this.bridgePath, "export default function () {}\n");
    }

    const reader = new LineReader(
      (line) => {
        this.rawLines.push(line);
        this.lines.push(JSON.parse(line) as PlaypenMessage);
      },
      () => this.notes.push("the playpen emitted an oversize line"),
    );

    const out = new Writable({
      write: (chunk: Buffer, _encoding, done) => {
        reader.feed(chunk.toString("utf8"));
        done();
      },
    });

    const control = options.controlDir ?? join(this.root, "control");
    const launcher = fakeLauncher(options.piEnv ?? {}, this.spawns);

    this.playpen = new Playpen({
      sandbox: this.sandboxId,
      input: this.stdin,
      channel: new Channel(out),
      launcher: options.launcher?.(launcher) ?? launcher,
      lock: new PlaypenLock(control, options.lockBeatMs ?? LOCK_BEAT_MS),
      creds: new CredReader(join(this.root, "creds"), options.credRetryMs),
      configDir: join(this.root, "config"),
      turnFile: options.turnFile?.(control) ?? new TurnFile(control),
      toolState: new ToolStateFile(control),
      processes: new ProcessRecords(control, this.sandboxId),
      bridgePath: this.bridgePath,
      piPackageJson: join(this.root, "config", "pi-package.json"),
      codeSandboxRoot: join(this.root, "code-sandbox"),
      codeSandboxLinkPrefix: join(this.root, "link-"),
      onExit: (code) => {
        this.exitCode = code;
      },
    });
  }

  public start(): boolean {
    return this.playpen.start();
  }

  public writeCreds(epoch: number): void {
    const path = join(this.root, "creds", "creds.json");
    const doc = {
      epoch,
      litellm_key: FIXTURE_LITELLM_KEY,
      pep_token: FIXTURE_PEP_TOKEN,
      written_at: new Date().toISOString(),
    };
    writeFileSync(path, `${JSON.stringify(doc)}\n`);
  }

  public writeRuntime(runtime: Record<string, unknown>): void {
    const path = join(this.root, "config", "runtime.json");
    writeFileSync(path, `${JSON.stringify({ model_alias: "agent-router", ...runtime })}\n`);
  }

  /**
   * Stands in for the bridge's own `tools.json` (`bridge/tool-state.ts`).
   *
   * `fake-pi.mjs` loads no extension, so nothing in this harness writes the
   * file the real bridge writes. A test that is about what the PLAYPEN
   * does with it writes it here.
   */
  public writeToolState(session: string, tools: number, rev: string | null = null): void {
    const dir = join(this.root, "control", "sessions", session);
    mkdirSync(dir, { recursive: true });
    const doc = { session, tools, rev, at: new Date().toISOString() };
    writeFileSync(join(dir, "tools.json"), `${JSON.stringify(doc)}\n`);
  }

  /** The session store the host would have created, as `start_turn` names it. */
  public sessionDir(session: string): string {
    const dir = join(this.root, "sessions", session, "pi");
    mkdirSync(dir, { recursive: true });

    return dir;
  }

  public cwd(session: string): string {
    const dir = join(this.root, "sessions", session);
    mkdirSync(dir, { recursive: true });

    return dir;
  }

  public send(message: Record<string, unknown>): void {
    this.stdin.write(`${JSON.stringify(message)}\n`);
  }

  /** Writes a raw record, for the tests that are about the framing itself. */
  public sendRaw(line: string): void {
    this.stdin.write(`${line}\n`);
  }

  public hello(overrides: Partial<HelloMessage> = {}): void {
    this.send({
      type: "hello",
      protocol: "1.0",
      host: "sessiond/test",
      family: "chat",
      sandbox: this.sandboxId,
      coalesce_ms: 0,
      pi_idle_ttl_s: 900,
      max_resident_processes: 12,
      host_deadline_s: 90,
      env_epoch: 1,
      ...overrides,
    });
  }

  public startTurn(session: string, turn: string, extra: Record<string, unknown> = {}): void {
    this.send({
      type: "start_turn",
      turn,
      session,
      cwd: this.cwd(session),
      session_dir: this.sessionDir(session),
      prompt: `prompt for ${session}`,
      deadline_s: 30,
      env_epoch: 1,
      config_rev: "reg-test",
      ...extra,
    });
  }

  public of<T extends PlaypenMessage["type"]>(
    type: T,
  ): Extract<PlaypenMessage, { type: T }>[] {
    return this.lines.filter((line) => line.type === type) as Extract<
      PlaypenMessage,
      { type: T }
    >[];
  }

  public endStdin(): void {
    this.stdin.end();
  }

  public async dispose(): Promise<void> {
    await this.playpen.shutdown(0, 0);
    this.stdin.destroy();
    rmSync(this.root, { recursive: true, force: true });
  }
}

/** Polls until `check` passes, or fails the test with what it last saw. */
export async function until(check: () => boolean, what: string, ms = 5000): Promise<void> {
  const deadline = Date.now() + ms;
  for (;;) {
    if (check()) {
      return;
    }
    if (Date.now() >= deadline) {
      throw new Error(`timed out waiting for ${what}`);
    }

    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
