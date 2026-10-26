#!/usr/bin/env node
// `creche-facade`, the ACP agent of one family sandbox. attendance starts it
// with `sbx exec` and speaks ACP v1 over its stdio.
//
// This file is the composition root and nothing else.

import { readSupervisorEnv } from "../env.js";
import { SupervisorLock } from "../lock.js";
import { relaySocketPath } from "../relay.js";
import { claimStdout, say } from "../stdio.js";
import { runSupervisor } from "../supervisor.js";
import { BEAT_DEADLINE_MS } from "../vectors.js";

const stdout = claimStdout();
const env = readSupervisorEnv(process.env);

if (env.ok) {
  void runSupervisor({
    input: process.stdin,
    stdout,
    say,
    lock: new SupervisorLock(env.controlDir, env.sandbox),
    deadlineMs: BEAT_DEADLINE_MS,
    runtime: null,
    sessions: null,
    socketPath: relaySocketPath(),
  });
} else {
  say(env.detail);
  stdout.exit(env.exitCode);
}
