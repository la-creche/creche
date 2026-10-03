// `playpen`, the process inside a family sandbox. The host opens ONE
// long-lived `sbx exec` and speaks contract 03 over its stdio:
//
//   sbx exec --env-file <supervisor.env> <sandbox> -- \
//     node /opt/agent-supervisor/agent-supervisor.js --sandbox <family>-s<N>
//
// `sbx exec` forwards no host environment, so `--env-file` is the only way the
// three mount paths of §7.1 arrive. `managerd` writes that file.
//
// This file is the composition root and nothing else. It reads the one
// argument and the environment, builds the real mounts and the real launcher,
// and hands them to `Playpen`. Everything a test needs to replace is a
// constructor argument there, so nothing below this line is ever mocked.

import { Channel } from "./channel.js";
import { LOCK_BEAT_MS } from "./constants.js";
import { CredReader } from "./creds.js";
import { spawnPi } from "./launcher.js";
import { PlaypenLock } from "./lock.js";
import { readMountDirs, unsetMountFatal } from "./mounts.js";
import { ProcessRecords } from "./process-record.js";
import { Playpen } from "./playpen.js";
import { ToolStateFile } from "./tool-state.js";
import { TurnFile } from "./turn-file.js";
import { codeSandboxRoot } from "./workspace.js";

/** The sandbox id, `<family>-s<N>`. It is an identifier, never a secret. */
const SANDBOX_FLAG = "--sandbox";
const SANDBOX_ENV = "AGENT_SANDBOX";

/**
 * `lock_beat_s` in milliseconds (§11.1 rule 3). A test seam, and the only one
 * left here: the host's `lock_stale_s` is four beat windows, and a suite that
 * shrinks one without the other would run at a ratio the contract forbids.
 * Unset means the contract default.
 */
const LOCK_BEAT_MS_ENV = "AGENT_LOCK_BEAT_MS";

const EXIT_NO_SANDBOX = 2;

/** §7.1: no mount path is guessable, so an unnamed one ends the process. */
const EXIT_MOUNT_UNSET = 7;

function readSandboxId(argv: readonly string[]): string | null {
  const at = argv.indexOf(SANDBOX_FLAG);
  const fromArgv = at === -1 ? undefined : argv[at + 1];
  const value = fromArgv ?? process.env[SANDBOX_ENV];

  return value === undefined || value.length === 0 ? null : value;
}

/** A positive number from the environment, or the contract's own default. */
function millis(name: string, fallback: number): number {
  const parsed = Number(process.env[name]);

  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function main(): void {
  const sandbox = readSandboxId(process.argv.slice(2));
  if (sandbox === null) {
    process.stderr.write(`playpen: ${SANDBOX_FLAG} or ${SANDBOX_ENV} is required\n`);
    process.exitCode = EXIT_NO_SANDBOX;

    return;
  }

  const channel = new Channel(process.stdout);
  const mounts = readMountDirs(process.env);

  // §5.7 rule 3. The cause reaches the host as protocol, before `ready`,
  // because a bare `channel_lost` would leave the operator with a log file.
  if (mounts.dirs === null) {
    channel.send(unsetMountFatal(mounts.detail));
    process.stderr.write(`playpen: ${mounts.detail}\n`);
    process.exitCode = EXIT_MOUNT_UNSET;

    return;
  }

  const playpen = new Playpen({
    sandbox,
    input: process.stdin,
    channel,
    launcher: spawnPi,
    lock: new PlaypenLock(mounts.dirs.control, millis(LOCK_BEAT_MS_ENV, LOCK_BEAT_MS)),
    creds: new CredReader(mounts.dirs.creds),
    configDir: mounts.dirs.config,
    turnFile: new TurnFile(mounts.dirs.control),
    toolState: new ToolStateFile(mounts.dirs.control),
    processes: new ProcessRecords(mounts.dirs.control, sandbox),
    codeSandboxRoot: codeSandboxRoot(process.env),
    onExit: (code) => {
      // Set the code and let the loop drain, so the last protocol line
      // reaches the host before the process goes.
      process.exitCode = code;
      process.stdin.pause();
    },
  });

  playpen.start();
}

main();
