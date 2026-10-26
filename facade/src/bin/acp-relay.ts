#!/usr/bin/env node
// `acp-relay`: one door connection's pipe to the facade. attendance starts
// one per connection with `sbx exec`. stdin goes to the facade's socket,
// and the socket goes to stdout, byte for byte. The facade writes every
// line strict. The relay adds none and parses none.

import { connect } from "node:net";

import { relaySocketPath } from "../relay.js";
import { say } from "../stdio.js";

const EXIT_OK = 0;
const EXIT_NO_FACADE = 1;

const socket = connect(relaySocketPath());

socket.once("connect", () => {
  process.stdin.pipe(socket);
  socket.pipe(process.stdout);
});

socket.once("error", (error) => {
  say(`no facade at the relay socket: ${error.message}`);
  process.exitCode = EXIT_NO_FACADE;
  process.stdin.destroy();
});

socket.once("close", () => {
  process.exitCode ??= EXIT_OK;
  process.stdin.destroy();
});
