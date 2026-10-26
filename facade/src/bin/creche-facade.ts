#!/usr/bin/env node
// `creche-facade`, the ACP agent of one family sandbox. attendance starts it
// with `sbx exec` and speaks ACP v1 over its stdio.
//
// This file is the composition root and nothing else.

import { readFamily, serveFamily } from "../family.js";
import { SupervisorLock } from "../lock.js";
import { relaySocketPath } from "../relay.js";
import { claimStdout, say } from "../stdio.js";
import { runSupervisor } from "../supervisor.js";
import { BEAT_DEADLINE_MS } from "../vectors.js";

const stdout = claimStdout();
const read = await readFamily(process.env);

if (read.ok) {
  const { family } = read;
  for (const name of family.composed.skippedSkills) {
    say(`skill ${name} is not valid; skipped`);
  }

  void runSupervisor({
    input: process.stdin,
    stdout,
    say,
    lock: new SupervisorLock(family.config.mounts.control, family.config.sandbox),
    deadlineMs: BEAT_DEADLINE_MS,
    serve: () => serveFamily(family),
    socketPath: relaySocketPath(),
  });
} else {
  say(read.detail);
  stdout.exit(read.exitCode);
}
