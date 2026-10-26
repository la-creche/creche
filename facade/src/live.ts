// The live updates of each session, sent to every peer on it. A peer is on
// a session after `session/new`, `session/load` or `session/prompt` there,
// until its link closes.
//
// A peer that loads a session gets the replay first. Live items that come
// during the replay wait, and only the ones after the last replayed chunk
// follow it, so the peer sees each chunk once.

import type { Peer, SessionUpdate } from "./acp.js";

/** Flue's ordering token of a chunk: compare `batch`, then `index`. */
export interface Position {
  readonly batch: number;
  readonly index: number;
}

/** One update, and the position of the chunk it came from, if any. */
export interface Item {
  readonly position: Position | null;
  readonly update: SessionUpdate;
}

export function after(position: Position, last: Position | null): boolean {
  if (last === null) {
    return true;
  }

  return position.batch > last.batch || (position.batch === last.batch && position.index > last.index);
}

interface Sink {
  /** Items that wait for a replay to end, or null when none runs. */
  held: Item[] | null;
  /** The sends to this peer, in order. */
  chain: Promise<void>;
}

export class Live {
  private readonly sessions = new Map<string, Map<Peer, Sink>>();

  /** Puts the peer on the session. A peer that is on it stays as it is. */
  public attach(sessionId: string, peer: Peer): void {
    this.sinks(sessionId, peer);
  }

  /**
   * Puts the peer on the session with its live items held. Call `release`
   * with the position of the last replayed chunk once the replay is sent.
   */
  public hold(sessionId: string, peer: Peer): { release(last: Position | null): Promise<void> } {
    const sink = this.sinks(sessionId, peer);
    sink.held ??= [];

    return {
      release: (last) => {
        const held = sink.held ?? [];
        sink.held = null;
        for (const item of held) {
          if (item.position === null || after(item.position, last)) {
            this.queue(sessionId, peer, sink, item.update);
          }
        }

        return sink.chain;
      },
    };
  }

  /** Sends each item to every peer on the session. Resolves once all are sent. */
  public publish(sessionId: string, items: readonly Item[]): Promise<void> {
    const sinks = this.sessions.get(sessionId) ?? new Map<Peer, Sink>();
    const sent: Promise<void>[] = [];
    for (const [peer, sink] of sinks) {
      for (const item of items) {
        if (sink.held !== null) {
          sink.held.push(item);
        } else {
          this.queue(sessionId, peer, sink, item.update);
        }
      }

      sent.push(sink.chain);
    }

    return Promise.all(sent).then(() => undefined);
  }

  private queue(sessionId: string, peer: Peer, sink: Sink, update: SessionUpdate): void {
    // A send to a closed link fails. That peer is gone, and the others
    // still get the update.
    sink.chain = sink.chain.then(() => peer.send(sessionId, update)).catch(() => undefined);
  }

  private sinks(sessionId: string, peer: Peer): Sink {
    let peers = this.sessions.get(sessionId);
    if (peers === undefined) {
      peers = new Map();
      this.sessions.set(sessionId, peers);
    }

    let sink = peers.get(peer);
    if (sink === undefined) {
      sink = { held: null, chain: Promise.resolve() };
      peers.set(peer, sink);
      const on = peers;
      void peer.closed.then(() => on.delete(peer));
    }

    return sink;
  }
}
