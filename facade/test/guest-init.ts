// A test end of the local link: a Unix socket that the test serves as
// guest init does. It keeps every byte that the facade sends up, and it
// hands the test the facade's end to write down lines on.

import { createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { join } from "node:path";

import { shortTmp } from "./facade-process.js";

/** How long `until` waits for its check. */
export const WAIT_MS = 2000;

/** The test end of the link: what guest init reads and writes. */
export interface GuestInit {
  readonly path: string;
  /** Every byte that the facade sent, as text. */
  received(): string;
  /** The facade's end, once it connected. */
  peer(): Promise<Socket>;
  /** True once the facade closed its end. */
  closedByFacade(): boolean;
}

const servers: Server[] = [];
const links: Socket[] = [];

/** Closes every link and socket that `guestInit` made. Run it after each test. */
export function closeGuestInits(): void {
  for (const socket of links.splice(0)) {
    socket.destroy();
  }
  for (const server of servers.splice(0)) {
    server.close();
  }
}

/** Waits until `check` holds, for at most `WAIT_MS`. */
export async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + WAIT_MS;
  while (!check()) {
    if (Date.now() > deadline) {
      throw new Error("the wait ran out");
    }

    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

export async function guestInit(): Promise<GuestInit> {
  const path = join(shortTmp(), "guest-init.sock");
  let received = "";
  let closed = false;
  let socket: Socket | null = null;
  const server = createServer((peer) => {
    socket = peer;
    links.push(peer);
    peer.on("data", (chunk: Buffer) => {
      received += chunk.toString("utf8");
    });
    peer.on("end", () => {
      closed = true;
    });
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(path, resolve));

  return {
    path,
    received: () => received,
    peer: async () => {
      await until(() => socket !== null);
      return socket as unknown as Socket;
    },
    closedByFacade: () => closed,
  };
}
