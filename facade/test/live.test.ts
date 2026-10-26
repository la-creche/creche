// The fan-out of live updates, and the hand-over from a replay to them.

import { describe, expect, it } from "vitest";

import type { Peer, SessionUpdate } from "../src/acp.js";
import { after, Live } from "../src/live.js";

function peer(): { peer: Peer; texts: string[]; close(): void } {
  const texts: string[] = [];
  let close: () => void = () => undefined;
  const closed = new Promise<void>((resolve) => (close = resolve));

  return {
    peer: {
      send: async (_sessionId, update) => {
        texts.push((update as { content: { text: string } }).content.text);
      },
      closed,
    },
    texts,
    close,
  };
}

function say(text: string): SessionUpdate {
  return { sessionUpdate: "agent_message_chunk", content: { type: "text", text } };
}

describe("Live", () => {
  it("sends each item to every peer on the session, and none to another session", async () => {
    const live = new Live();
    const [a, b, c] = [peer(), peer(), peer()];
    live.attach("s", a.peer);
    live.attach("s", b.peer);
    live.attach("s", a.peer);
    live.attach("other", c.peer);
    await live.publish("s", [{ position: null, update: say("one") }]);

    expect([a.texts, b.texts, c.texts]).toEqual([["one"], ["one"], []]);
  });

  it("holds a loading peer's items and sends only those after the replay", async () => {
    const live = new Live();
    const loading = peer();
    const gate = live.hold("s", loading.peer);
    await live.publish("s", [
      { position: { batch: 3, index: 0 }, update: say("replayed") },
      { position: { batch: 3, index: 1 }, update: say("new") },
      { position: null, update: say("info") },
    ]);

    expect(loading.texts).toEqual([]);
    await gate.release({ batch: 3, index: 0 });
    await live.publish("s", [{ position: { batch: 4, index: 0 }, update: say("later") }]);

    expect(loading.texts).toEqual(["new", "info", "later"]);
  });

  it("drops a peer when its link closes", async () => {
    const live = new Live();
    const gone = peer();
    live.attach("s", gone.peer);
    gone.close();
    await gone.peer.closed;
    await live.publish("s", [{ position: null, update: say("after") }]);

    expect(gone.texts).toEqual([]);
  });

  it("orders positions by batch, then index", () => {
    expect(after({ batch: 2, index: 0 }, { batch: 1, index: 9 })).toBe(true);
    expect(after({ batch: 1, index: 2 }, { batch: 1, index: 1 })).toBe(true);
    expect(after({ batch: 1, index: 1 }, { batch: 1, index: 1 })).toBe(false);
    expect(after({ batch: 0, index: 0 }, null)).toBe(true);
  });
});
