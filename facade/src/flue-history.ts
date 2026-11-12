// The one importer of `@flue/runtime/internal`, which is not Flue's public
// API (Phase 2 plan, Q2). `@flue/runtime` is pinned to one exact version,
// and `test/flue-history.test.ts` fails on any change of the shape below.
//
// What this module takes from the internal entry:
//
//   agentStreamPath(agentName, instanceId) ──► "agents/<name>/<id>"
//   handleAgentConversationRead({ store, path, request })
//       ──► the Response of Flue's `GET /:id?view=history`
//
// The store comes from the public adapter contract: `tapStores` wraps the
// adapter given to `start()` and keeps what its `connect()` returns. The
// chunks are the public `ConversationStreamChunk`.

import type { ConversationStreamChunk } from "@flue/runtime";
import type { ConversationStreamStore, PersistenceAdapter, PersistenceStores } from "@flue/runtime/adapter";
import type { AgentConversationSnapshot } from "@flue/runtime/internal";
import { agentStreamPath, handleAgentConversationRead } from "@flue/runtime/internal";

/** Any origin: the handler reads only the query of the request. */
const ORIGIN = "http://facade.invalid/";

const NOT_FOUND = 404;

export interface TappedAdapter {
  /** Give this adapter to `start()`. */
  readonly adapter: PersistenceAdapter;
  /** The conversation store, once `start()` connected the adapter. */
  store(): ConversationStreamStore | null;
}

export function tapStores(inner: PersistenceAdapter): TappedAdapter {
  let stores: PersistenceStores | null = null;
  const adapter: PersistenceAdapter = {
    connect: async () => {
      stores = await inner.connect();
      return stores;
    },
    ...(inner.migrate === undefined ? {} : { migrate: () => inner.migrate?.() }),
    ...(inner.close === undefined ? {} : { close: () => inner.close?.() }),
  };

  return { adapter, store: () => stores?.conversationStreamStore ?? null };
}

export type HistoryRead =
  | { readonly ok: true; readonly chunks: readonly ConversationStreamChunk[] }
  | { readonly ok: false; readonly detail: string };

/**
 * One snapshot of an instance's conversation at the durable head. The
 * snapshot is a reset chunk, so replay can send its messages without a scan
 * from the stream origin. An instance that no send has created yet has no
 * stream, and its history is empty.
 */
export async function readHistory(
  store: ConversationStreamStore,
  agentName: string,
  instanceId: string,
): Promise<HistoryRead> {
  const path = agentStreamPath(agentName, instanceId);
  const response = await handleAgentConversationRead({ store, path, request: new Request(`${ORIGIN}?view=history`) });
  if (response.status === NOT_FOUND) {
    return { ok: true, chunks: [] };
  }

  if (!response.ok) {
    return { ok: false, detail: `Flue answered the history read with ${response.status}` };
  }

  const snapshot = (await response.json()) as AgentConversationSnapshot;
  const reset: ConversationStreamChunk = {
    type: "conversation-reset",
    conversationId: snapshot.conversationId,
    snapshot,
    position: { batch: Number(snapshot.offset), index: Number.MAX_SAFE_INTEGER },
  };

  return { ok: true, chunks: [reset] };
}
