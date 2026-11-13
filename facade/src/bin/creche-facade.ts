#!/usr/bin/env node
// `creche-facade`, the ACP agent of one family VM. guest init starts it,
// and ACP comes on the relay socket alone.
//
// This file is the composition root and nothing else.

import { limitsLine, readFamily, serveFamily } from "../family.js";
import { RELAY_SOCKET } from "../guest.js";
import { claimStdout, say } from "../stdio.js";
import { runSupervisor } from "../supervisor.js";

const exit = claimStdout();
const read = await readFamily(process.env);

if (read.ok) {
  const { family } = read;
  for (const name of family.composed.skippedSkills) {
    say(`skill ${name} is not valid; skipped`);
  }

  say(limitsLine(family.limits));

  void runSupervisor({
    link: family.link,
    exit,
    say,
    serve: (link) => serveFamily(family, link),
    socketPath: RELAY_SOCKET,
  });
} else {
  say(read.detail);
  exit(read.exitCode);
}
