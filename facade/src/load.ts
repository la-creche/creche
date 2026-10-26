// `session/load`: the replay of a session's history, then its live updates.
//
//   hold the peer's live items ──► read the history ──► send the replay
//     ──► send the held items after the last replayed chunk ──► answer {}

import type { ConversationStreamStore } from "@flue/runtime/adapter";

import type { LoadSessionRequest, Outcome, Peer } from "./acp.js";
import { readHistory } from "./flue-history.js";
import type { Live } from "./live.js";
import { replay } from "./replay.js";
import { sessionFault } from "./sessions.js";
import type { Sessions } from "./sessions.js";

export class Loader {
  public constructor(
    private readonly sessions: Sessions,
    private readonly live: Live,
    private readonly store: () => ConversationStreamStore | null,
    private readonly agentName: string,
  ) {}

  public async loadSession(params: LoadSessionRequest, peer: Peer): Promise<Outcome<Record<string, never>>> {
    const fault = sessionFault(params);
    if (fault !== null) {
      return { ok: false, refusal: { kind: "invalid_params", detail: fault } };
    }

    const { sessionId } = params;
    if (!this.sessions.known(sessionId)) {
      return { ok: false, refusal: { kind: "unknown_session", sessionId } };
    }

    const store = this.store();
    if (store === null) {
      return { ok: false, refusal: { kind: "failed", detail: "Flue has not started" } };
    }

    const gate = this.live.hold(sessionId, peer);
    const history = await readHistory(store, this.agentName, sessionId);
    if (!history.ok) {
      await gate.release(null);
      return { ok: false, refusal: { kind: "failed", detail: history.detail } };
    }

    const { updates, last } = replay(history.chunks);
    for (const update of updates) {
      await peer.send(sessionId, update);
    }

    await gate.release(last);

    return { ok: true, value: {} };
  }
}
