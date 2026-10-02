// The one place this process starts another. Everything above it takes a
// `PiLauncher` and never names a binary, which is what lets every protocol
// test run against the fake pi on a Mac with no sbx and no VM.
//
// stdio is three pipes, never `inherit`. An inherited stdout would put pi's
// output straight onto the channel, and contract 03 §1 rule 4 says stdout
// carries the protocol and nothing else.

import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";

import { PI_BIN } from "./constants.js";
import type { PiSpawnSpec } from "./pi-process.js";

/** Lets a test point the supervisor at the fake pi without touching argv. */
const PI_BIN_ENV = "AGENT_PI_BIN";

export function piBinary(): string {
  const override = process.env[PI_BIN_ENV];

  return override !== undefined && override.length > 0 ? override : PI_BIN;
}

export function spawnPi(spec: PiSpawnSpec): ChildProcess {
  return spawn(piBinary(), [...spec.args], {
    cwd: spec.cwd,
    env: spec.env,
    stdio: ["pipe", "pipe", "pipe"],
  });
}
