// `_creche/session/rewind`: cut the session back to before one user message.
// ACP has no rewind method, so this is a creche extension
// (`vectors/creche-extensions.json`). Flue 2.3.0 refuses a branch ("Appends
// are linear"), so the cut starts a new Flue instance in the same VM:
//
//   hold the session (no prompt starts) ──► read the history of the instance
//     ──► the kept messages of the last rewind, then the history
//     ──► find the user message `messageId` ──► keep the messages before it
//     ──► write the next lineage ──► adopt its instance
//     ──► give the agent the transcript of the kept messages ──► answer {}
//
// The old instance stays in `flue.db`. The workspace files stay as they are.
// The client cuts its own view, and its next prompt is the first send of
// the new instance.

import type { ConversationStreamStore } from "@flue/runtime/adapter";

import type { Outcome, RewindRequest } from "./acp.js";
import { readHistory } from "./flue-history.js";
import { keptOf, transcriptOf } from "./kept.js";
import type { Kept } from "./kept.js";
import { writeLineage } from "./lineage.js";
import type { Lineage } from "./lineage.js";
import type { OneSession } from "./sessions.js";
import type { Turns } from "./turns.js";
import type { SessionMeter } from "./usage.js";

/** Where the facade gives the agent the transcript of a rewind (`agent/src/earlier.ts`). */
export interface EarlierSink {
  set(instance: string, transcript: string): void;
}

/** What a rewind needs from the facade start. */
export interface RewindOptions {
  /** The lineage file beside `flue.db` (`src/lineage.ts`). */
  readonly file: string;
  /** The lineage that the start read from `file`. */
  readonly lineage: Lineage;
  readonly earlier: EarlierSink;
}

export class Rewinder {
  public constructor(
    private readonly session: OneSession,
    private readonly turns: Turns,
    private readonly meter: SessionMeter,
    private readonly store: () => ConversationStreamStore | null,
    private readonly agentName: string,
    private readonly options: RewindOptions,
  ) {
    options.earlier.set(session.instance, transcriptOf(session.seed));
  }

  public async rewind(params: RewindRequest): Promise<Outcome<Record<string, never>>> {
    const { sessionId, messageId } = params;
    if (!this.session.known(sessionId)) {
      return { ok: false, refusal: { kind: "unknown_session", sessionId } };
    }

    const store = this.store();
    if (store === null) {
      return { ok: false, refusal: { kind: "failed", detail: "Flue has not started" } };
    }

    return this.turns.between(sessionId, () => this.cut(store, messageId));
  }

  private async cut(store: ConversationStreamStore, messageId: string): Promise<Outcome<Record<string, never>>> {
    const history = await readHistory(store, this.agentName, this.session.instance);
    if (!history.ok) {
      return { ok: false, refusal: { kind: "failed", detail: history.detail } };
    }

    const messages: Kept[] = [...this.session.seed];
    for (const chunk of history.chunks) {
      if (chunk.type === "conversation-reset") {
        messages.push(...chunk.snapshot.messages.flatMap((message) => keptOf(message) ?? []));
      }
    }

    const at = messages.findIndex((message) => message.role === "user" && message.id === messageId);
    if (at < 0) {
      return { ok: false, refusal: { kind: "invalid_params", detail: "the session has no user message with this messageId" } };
    }

    const next = this.session.next(messages.slice(0, at));
    const written = await writeLineage(this.options.file, next);
    if (!written.ok) {
      return { ok: false, refusal: { kind: "failed", detail: written.detail } };
    }

    // The agent reads the thinking level by the instance id, so the chosen
    // level moves to the new instance.
    const level = this.meter.level;
    this.session.adopt(next);
    this.meter.choose(level);
    this.options.earlier.set(this.session.instance, transcriptOf(next.seed));

    return { ok: true, value: {} };
  }
}
