// The one importer of `@flue/runtime/internal`, which is not Flue's public
// API (Phase 2 plan, Q2). `@flue/runtime` is pinned to one exact version,
// and `test/flue-history.test.ts` fails on any change of the shape below.
//
// What this module takes from the internal entry:
//
//   agentStreamPath(agentName, instanceId) ──► "agents/<name>/<id>"
//   handleAgentConversationRead({ store, path, request })
//       ──► the Response of Flue's `GET /:id?view=updates&offset=…`
//
// The store comes from the public adapter contract: `tapStores` wraps the
// adapter given to `start()` and keeps what its `connect()` returns. The
// chunks are the public `ConversationStreamChunk`.

import type { ConversationStreamChunk } from "@flue/runtime";
import type { ConversationStreamStore, PersistenceAdapter, PersistenceStores } from "@flue/runtime/adapter";
import { agentStreamPath, handleAgentConversationRead } from "@flue/runtime/internal";

/** The offset before the first batch: a read from it replays the stream. */
const FROM_START = "-1";

/** Any origin: the handler reads only the query of the request. */
const ORIGIN = "http://facade.invalid/";

const NEXT_OFFSET = "Stream-Next-Offset";
const UP_TO_DATE = "Stream-Up-To-Date";
const NOT_FOUND = 404;

/** Most pages one history read takes. Flue serves 100 batches a page. */
const MAX_PAGES = 10_000;

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
 * Every chunk of one instance's conversation, from its start to the head
 * at the time of the read. An instance that no send has created yet has no
 * stream, and its history is empty.
 */
export async function readHistory(
  store: ConversationStreamStore,
  agentName: string,
  instanceId: string,
): Promise<HistoryRead> {
  const path = agentStreamPath(agentName, instanceId);
  const chunks: ConversationStreamChunk[] = [];
  let offset = FROM_START;

  for (let page = 0; page < MAX_PAGES; page += 1) {
    const url = `${ORIGIN}?view=updates&offset=${encodeURIComponent(offset)}`;
    const response = await handleAgentConversationRead({ store, path, request: new Request(url) });
    if (response.status === NOT_FOUND && offset === FROM_START) {
      return { ok: true, chunks };
    }

    if (!response.ok) {
      return { ok: false, detail: `Flue answered the history read with ${response.status}` };
    }

    const body = (await response.json()) as readonly { readonly type: string }[];
    for (const chunk of body) {
      // A wire-only marker of the HTTP view. It is not conversation content.
      if (chunk.type !== "stream-checkpoint") {
        chunks.push(chunk as ConversationStreamChunk);
      }
    }

    const next = response.headers.get(NEXT_OFFSET);
    if (response.headers.get(UP_TO_DATE) === "true" || next === null || next === offset) {
      return { ok: true, chunks };
    }

    offset = next;
  }

  return { ok: false, detail: "the history read did not reach the head" };
}
