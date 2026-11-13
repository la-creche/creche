// The facade's in-VM Unix socket. guest init joins each ACP link of guest
// port 1201 to this socket (decisions CH5e, CH62 rule 5). One facade owns
// Flue, and every connection reaches it here.
//
//   guest port 1201 ──► socket ──► one AgentApp per connection ──► one service
//
// No connection has a beat deadline: the door sends no beat (decisions
// CH5d, CH19). A connection ends when its peer closes it.

import { chmodSync, mkdirSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { dirname } from "node:path";

import { strictStream, strictWriter } from "./acp.js";
import type { AgentApp } from "./acp.js";
import type { Say } from "./stdio.js";

const SOCKET_DIR_MODE = 0o700;

export type Listening = { readonly ok: true; readonly server: Server } | { readonly ok: false; readonly detail: string };

/**
 * Listens on `path`. One facade runs in a VM, so a socket file that is
 * there already belongs to a facade that is gone.
 */
export function listenRelay(path: string, appFor: () => AgentApp, say: Say): Promise<Listening> {
  try {
    mkdirSync(dirname(path), { recursive: true, mode: SOCKET_DIR_MODE });
    chmodSync(dirname(path), SOCKET_DIR_MODE);
    rmSync(path, { force: true });
  } catch (error) {
    return Promise.resolve({ ok: false, detail: error instanceof Error ? error.message : String(error) });
  }

  const server = createServer((socket) => serveConnection(socket, appFor(), say));

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

function serveConnection(socket: Socket, app: AgentApp, say: Say): void {
  const connection = app.connect(strictStream(socket, strictWriter((line) => socket.write(line), say)));

  socket.on("error", () => socket.destroy());
  socket.on("close", () => connection.close());
  void connection.closed.then(() => socket.end());
}
