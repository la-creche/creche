#!/usr/bin/env node
// `creche-facade`, the ACP agent of one family sandbox. On sbx attendance
// starts it with `sbx exec` and speaks ACP v1 over its stdio. Under CH
// guest init starts it, and ACP comes on the relay socket alone.
//
// This file is the composition root and nothing else.

import { limitsLine, readFamily, serveFamily } from "../family.js";
import { RELAY_SOCKET } from "../guest.js";
import { relaySocketPath } from "../relay.js";
import { claimStdout, say } from "../stdio.js";
import { runSupervisor } from "../supervisor.js";
import { BEAT_DEADLINE_MS } from "../vectors.js";

const stdout = claimStdout();
const read = await readFamily(process.env, process.stdin);

if (read.ok) {
  const { family } = read;
  for (const name of family.composed.skippedSkills) {
    say(`skill ${name} is not valid; skipped`);
  }

  say(limitsLine(family.limits));

  void runSupervisor({
    supervision: family.supervision,
    stdout,
    say,
    deadlineMs: BEAT_DEADLINE_MS,
    serve: (link) => serveFamily(family, link),
    socketPath: family.mode.kind === "ch" ? RELAY_SOCKET : relaySocketPath(),
  });
} else {
  say(read.detail);
  stdout.exit(read.exitCode);
}
