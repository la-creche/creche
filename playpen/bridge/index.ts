// The PEP bridge: a pi extension that turns the family's manifest into pi
// tools (contract 04 §4). It is the sandbox image's only path to an action.
//
//   pi --no-extensions --extension /opt/agent-bridge/pep-bridge.js
//        │
//        ├─ GET /manifest ── one tool per entry, named as the manifest names it
//        │       ▲
//        │       └── asked again for the LIFE of the process (§4.2), fast
//        │           while it holds none, slowly once it holds one
//        │
//        ├─ each call ────── POST /call, with contract 04 §3's advisory headers
//        ├─ tools.json ───── how many tools this process holds, for the playpen
//        └─ index_search ─── its own tool, over the index mount (`search.ts`)
//
// Five rules shape this file:
//
//  1. **The factory never throws.** A rejected factory aborts pi's startup, so
//     a PEP that is down would take the whole session with it. Registering no
//     tools lets the session converse without acting, which is the outcome a
//     user can work with.
//  2. **Never write to stdout.** In rpc mode a pi process's stdout IS the
//     protocol the playpen parses. One stray line corrupts the session's
//     event stream. Free text goes to stderr, which the playpen forwards as
//     `log` lines (contract 03 §5.5).
//  3. **No manifest is never final.** A pi process serves many turns (contract
//     03 §6 rule 4), so "no tools at start" would otherwise mean "no tools for
//     the life of this process", long after the PEP came back. pi takes a tool
//     registered after start-up and activates it, proven against the real pi
//     in `test/real-pi.test.ts`.
//  4. **A manifest already taken is never final either.** The grant file moves
//     under a running process, and nothing else tells this process so:
//     `config_rev` versions the family's CONFIG mount, not its grants. So the
//     same loop keeps asking after its first answer, four times slower, with
//     the revision it holds in `If-None-Match`. That is what makes "a
//     permission change is one action" true for the model's tool list and not
//     only for the PEP's decision.
//  5. **One tool is the bridge's own, and it needs no manifest.**
//     `index_search` reads the corpus indexes a family mounts, and only
//     those, so it is offered before the first fetch and with no PEP at all.
//     The PEP still decides its semantic half: `embed` is a granted verb
//     (contract 03 §7.3 rule 6).

import {
  INDEX_ROOT_ENV,
  MANIFEST_ATTEMPTS,
  MANIFEST_BACKGROUND_MS,
  MANIFEST_RETRY_MS,
  MANIFEST_WATCH_MS,
  PEP_TOKEN_ENV,
  PEP_URL_ENV,
  SEARCH_TOOL,
  SESSION_ENV,
} from "./constants.js";
import { frameCodemode } from "./codemode.js";
import { isIndexRoot, listIndexes } from "./index-store.js";
import { ManifestState, PepClient } from "./pep.js";
import type { ManifestAnswer, Manifest } from "./pep.js";
import type { PiExtensionApi } from "./pi-api.js";
import { pepEmbedder } from "./query-embed.js";
import { searchTool } from "./search.js";
import { movedAnything, OfferedTools } from "./tool-set.js";
import { readTurnContext } from "./turn-context.js";
import { writeToolState } from "./tool-state.js";

const NAME = "pep-bridge";

/** How often the loop asks, in each of the two states it can be in. */
export interface BridgePace {
  /** While no manifest has arrived. The process holds no tools at all. */
  readonly retryMs: number;
  /** Once one has. A poll for a moved grant, not for a missing manifest. */
  readonly watchMs: number;
}

const PRODUCTION_PACE: BridgePace = {
  retryMs: MANIFEST_BACKGROUND_MS,
  watchMs: MANIFEST_WATCH_MS,
};

/** stderr, never stdout. Rule 2 above says why. */
function note(message: string): void {
  process.stderr.write(`${NAME}: ${message}\n`);
}

/**
 * `unref` so a pending wait never keeps a process alive on its own. The loop
 * sleeps for up to a minute at a time, and a pi process that has read stdin
 * EOF must still exit at once (contract 03 §6 rule 7).
 */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms).unref());
}

/** Contract 04 §4. Once, with a short retry: the PEP is on the same host. */
async function fetchManifest(pep: PepClient): Promise<Manifest> {
  let last: unknown = null;
  for (let attempt = 1; attempt <= MANIFEST_ATTEMPTS; attempt += 1) {
    try {
      const answer = await pep.manifest();
      if (answer.state === ManifestState.Fresh) {
        return answer.manifest;
      }

      // Unreachable: nothing is held yet, so no conditional was sent.
      throw new Error("the PEP answered 304 to an unconditional fetch");
    } catch (error) {
      last = error;
      if (attempt < MANIFEST_ATTEMPTS) {
        await sleep(MANIFEST_RETRY_MS);
      }
    }
  }

  throw last instanceof Error ? last : new Error(String(last));
}

/** The session this process serves, for the state file's own record. */
function sessionId(): string {
  return readTurnContext().session ?? process.env[SESSION_ENV] ?? "";
}

/**
 * Applies one manifest and states the outcome where the playpen can read
 * it (contract 03 §7.7).
 *
 * The file is rewritten only when the offered set MOVED, or when this is the
 * process's first manifest. `caregiver` mints a fresh revision on every grant
 * write, including a token rotation that changes no tool, and a file that
 * moved for that would have the playpen tell a reader about a change that
 * did not happen.
 */
function take(pi: PiExtensionApi, offered: OfferedTools, manifest: Manifest, pep: PepClient): void {
  const first = offered.empty;
  const change = offered.take(pi, manifest, pep);

  if (change.dropped.length > 0) {
    note(`dropped ${change.dropped.length} manifest entries with unusable names: ` +
      change.dropped.join(", "));
  }

  if (!first && !movedAnything(change)) {
    return;
  }

  writeToolState(sessionId(), change.count, manifest.rev);

  if (first) {
    note(`registered ${change.count} PEP tools from grants ${manifest.rev}`);
    return;
  }

  note(
    `grants ${manifest.rev}: ${change.added.length} tools added, ` +
      `${change.removed.length} removed, ${change.changed.length} redescribed`,
  );
}

/**
 * Asks for the manifest until this process ends.
 *
 * ONE request per interval and never two at once: the loop awaits its own
 * fetch before it sleeps again, so a PEP that answers slowly cannot stack
 * requests behind itself. The timer is not held: `sleep` uses `setTimeout`,
 * and the process exits on stdin EOF whatever this loop is doing (§6 rule 7).
 */
async function watchManifest(
  pi: PiExtensionApi,
  pep: PepClient,
  offered: OfferedTools,
  pace: BridgePace,
): Promise<void> {
  for (;;) {
    await sleep(offered.empty ? pace.retryMs : pace.watchMs);

    let answer: ManifestAnswer;
    try {
      answer = await pep.manifest(offered.rev);
    } catch {
      // Down, or refusing this family. The next interval asks again, and the
      // state file already says what this process holds.
      continue;
    }

    if (answer.state === ManifestState.Unchanged) {
      continue;
    }

    const recovered = offered.empty;
    take(pi, offered, answer.manifest, pep);

    if (recovered) {
      note(`the PEP answered again: grants ${answer.manifest.rev}`);
    }
  }
}

/**
 * Rule 5. Offered when the family mounts an index, and at no other time: the
 * playpen names the root for every family, and only a mount makes the
 * directory exist. A failure here costs this tool and never the PEP's.
 */
function offerSearch(pi: PiExtensionApi, pep: PepClient | null, offered: OfferedTools): void {
  const root = process.env[INDEX_ROOT_ENV] ?? "";
  if (!isIndexRoot(root)) {
    return;
  }

  try {
    const indexes = listIndexes(root).map((index) => index.name);
    pi.registerTool(searchTool(root, indexes, pepEmbedder(pep, offered)));
    note(`offered ${SEARCH_TOOL} over ${indexes.length} indexes under ${root}`);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    note(`${SEARCH_TOOL} could not be offered: ${detail}`);
  }
}

async function register(pi: PiExtensionApi, pace: BridgePace): Promise<void> {
  // First, and whatever the PEP says: a codemode script's output is untrusted
  // even in a session with no PEP tools (`codemode.ts`).
  pi.on?.("tool_result", frameCodemode);

  const baseUrl = process.env[PEP_URL_ENV] ?? "";
  const token = process.env[PEP_TOKEN_ENV] ?? "";
  const offered = new OfferedTools();
  if (baseUrl.length === 0 || token.length === 0) {
    offerSearch(pi, null, offered);
    note(`no ${PEP_URL_ENV} or no ${PEP_TOKEN_ENV}; this session has no action tools`);
    writeToolState(sessionId(), 0, null);
    return;
  }

  // The context is read per call, not captured: a held-open process serves
  // many turns and the turn id changes under it (`turn-context.ts`).
  const pep = new PepClient(baseUrl, token, () => readTurnContext());
  offerSearch(pi, pep, offered);

  try {
    take(pi, offered, await fetchManifest(pep), pep);
  } catch (error) {
    // Rule 1. Converse now, act later. The factory returns so the session can
    // start, and rule 3's loop carries on without it.
    const detail = error instanceof Error ? error.message : String(error);
    note(`no manifest after ${MANIFEST_ATTEMPTS} attempts; no action tools yet: ${detail}`);
    writeToolState(sessionId(), 0, null);
  }

  // Rules 3 and 4 are one loop: a process with no manifest is asking for its
  // first, and a process with one is asking whether the grants moved.
  void watchManifest(pi, pep, offered, pace);
}

/**
 * pi calls this with one argument (`loader.js`: `await factory(api)`), so the
 * second is the tests' way to run the loop at a pace a suite can wait for.
 * Production always takes the default.
 */
export default async function (
  pi: PiExtensionApi,
  pace: BridgePace = PRODUCTION_PACE,
): Promise<void> {
  try {
    await register(pi, pace);
  } catch (error) {
    // Rule 1, for anything `register` did not foresee. The manifest is a
    // snapshot anyway (contract 04 §4 rule 2), so no tool is lost that the PEP
    // would have allowed on this attempt.
    const detail = error instanceof Error ? error.message : String(error);
    note(`the bridge could not start; no action tools: ${detail}`);
    writeToolState(sessionId(), 0, null);
  }
}
