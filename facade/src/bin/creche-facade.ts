#!/usr/bin/env node
// `creche-facade`, the ACP agent of one family sandbox. attendance starts it
// with `sbx exec` and speaks ACP v1 over its stdio.
//
// This file is the composition root and nothing else.

import { facadeApp, strictStream, strictWriter } from "../acp.js";
import { claimStdout, say } from "../stdio.js";

const stdout = claimStdout();
const connection = facadeApp().connect(strictStream(process.stdin, strictWriter(stdout.writeLine, say)));

say("serving ACP v1 on stdio");

void connection.closed.then(() => {
  say("stdin closed");
  stdout.exit(0);
});
