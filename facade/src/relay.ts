// The facade's in-VM Unix socket. attendance runs one `sbx exec acp-relay`
// per door connection, and each relay pipes its stdio to this socket. One
// facade owns Flue, and every connection reaches it here.
//
//   acp-relay ──► socket ──► one AgentApp per connection ──► one service
//
// Each connection has a beat deadline of its own, armed by its first
// `_creche/beat`. A connection whose deadline passes is closed alone.
//
// The owner's decision: attendance's door beats each relay connection, with
// string ids `creche-beat-<n>`, and takes the answers. The first beat arms
// the deadline, so an idle client is not ended before it.

import { chmodSync, mkdirSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { strictStream, strictWriter } from "./acp.js";
import type { AgentApp, SupervisorHooks } from "./acp.js";
import { BeatDeadline } from "./liveness.js";
import type { Say } from "./stdio.js";

const SOCKET_DIR_MODE = 0o700;

/**
 * The socket of the one facade of this VM. `sbx exec` gives the facade and
 * each relay the same env file, so both read the same `TMPDIR`.
 */
export function relaySocketPath(): string {
  return join(tmpdir(), "creche-facade", "acp.sock");
}

export type Listening = { readonly ok: true; readonly server: Server } | { readonly ok: false; readonly detail: string };

/**
 * Listens on `path`. The caller holds the supervisor lock, so a socket
 * file that is there already belongs to a facade that is gone.
 */
export function listenRelay(
  path: string,
  appFor: (hooks: SupervisorHooks) => AgentApp,
  deadlineMs: number,
  say: Say,
): Promise<Listening> {
  try {
    mkdirSync(dirname(path), { recursive: true, mode: SOCKET_DIR_MODE });
    chmodSync(dirname(path), SOCKET_DIR_MODE);
    rmSync(path, { force: true });
  } catch (error) {
    return Promise.resolve({ ok: false, detail: error instanceof Error ? error.message : String(error) });
  }

  const server = createServer((socket) => serveConnection(socket, appFor, deadlineMs, say));

  return new Promise((resolve) => {
    server.once("error", (error) => resolve({ ok: false, detail: error.message }));
    server.listen(path, () => resolve({ ok: true, server }));
  });
}

/** Stops the listener and removes the socket file. */
export function closeRelay(server: Server, path: string): void {
  server.close();
  rmSync(path, { force: true });
}

function serveConnection(socket: Socket, appFor: (hooks: SupervisorHooks) => AgentApp, deadlineMs: number, say: Say): void {
  const deadline = new BeatDeadline(deadlineMs, () => {
    say("a relay connection sent no beat in time; it is closed");
    socket.destroy();
  });

  const app = appFor({
    onBeat: () => deadline.feed(),
    // Only the supervisor link stops the facade.
    onShutdown: () => say("ignored _creche/shutdown on a relay connection"),
  });
  const connection = app.connect(strictStream(socket, strictWriter((line) => socket.write(line), say)));

  socket.on("error", () => socket.destroy());
  socket.on("close", () => {
    deadline.stop();
    connection.close();
  });
  void connection.closed.then(() => socket.end());
}
