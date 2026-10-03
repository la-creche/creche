#!/usr/bin/env node
// A stand-in for `pi --mode rpc`, ported from `probes/0a-one-channel`, so
// every protocol test runs on this Mac with no sbx, no VM and no model call.
//
// It copies the rpc behaviours the playpen depends on
// (pi 0.99.1 docs/rpc.md):
//   1. LF is the only record delimiter, in both directions.
//   2. A `prompt` response means ACCEPTED. The result arrives as events.
//   3. The event order of one clean turn ends at `agent_settled`.
//   4. stdin EOF terminates the process at once, abandoning an in-flight turn.
//   5. `get_entries` answers `success: false` when `since` matches no entry.
//
// It is NOT a pi emulator. It has no model, no tools and no session file.
// Anything a test asserts against this file is a statement about the
// PLAYPEN's logic, never about pi.
//
// Beyond the probe's copy it answers `get_entries`, `steer` and `fork`,
// because the playpen calls all three and the probe's spike did not.
//
// Tunables, all read from the environment the test launcher adds:
//   FAKE_PI_EVENTS    message_update deltas per turn            default 6
//   FAKE_PI_DELAY_MS  delay between deltas                      default 8
//   FAKE_PI_BOOT_MS   startup delay before the first read       default 0
//   FAKE_PI_DIE_AT    exit hard after this many deltas          default off
//   FAKE_PI_NO_ENTRIES  refuse every get_entries                default off
//   FAKE_PI_FORK_LOG  append one JSON line per fork call        default off
//   FAKE_PI_HANDLED   answer every prompt as handled, run nothing  default off

const DELTAS = Number(process.env.FAKE_PI_EVENTS || 6);
const DELAY_MS = Number(process.env.FAKE_PI_DELAY_MS || 8);
const BOOT_MS = Number(process.env.FAKE_PI_BOOT_MS || 0);
const DIE_AT = Number(process.env.FAKE_PI_DIE_AT || 0);
const NO_ENTRIES = process.env.FAKE_PI_NO_ENTRIES === "1";
const FORK_LOG = process.env.FAKE_PI_FORK_LOG || "";
const HANDLED = process.env.FAKE_PI_HANDLED === "1";

import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const args = process.argv.slice(2);
const sessionId = args[args.indexOf("--session-id") + 1] || "unknown";

let aborted = false;
let running = false;
let turns = 0;
let steered = null;

// A growing entry list, so `since` has something to be a cursor into.
//
// It is kept on disk under PI_CODING_AGENT_DIR, because the real pi's store
// is on disk too and contract 02 §10.5 depends on that: a terminal's pi
// process writes entries, exits, and a LATER rpc process on the same session
// has to read them back. An in-memory list made every process start empty,
// so the behaviour under test could not exist.
const STORE_DIR = process.env.PI_CODING_AGENT_DIR || "";
const STORE_FILE = STORE_DIR ? join(STORE_DIR, `fake-pi-entries-${sessionId}.json`) : "";

const entries = loadEntries();

function loadEntries() {
  if (!STORE_FILE) {
    return [];
  }

  try {
    const parsed = JSON.parse(readFileSync(STORE_FILE, "utf8"));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveEntries() {
  if (!STORE_FILE) {
    return;
  }

  try {
    // No mkdir. `sessiond` makes the session directory and DELETES it when a
    // thin job ends (contract 02 §12 rules 5 and 6), while this process is
    // still being killed. A mkdir here recreated that directory after the
    // delete, about one run in five, and the job looked like it never went.
    writeFileSync(STORE_FILE, JSON.stringify(entries));
  } catch {
    // A store this fake cannot write is a test-rig problem, never a pi one.
  }
}

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function addEntry(role, text) {
  const id = `e${entries.length + 1}`;
  entries.push({ id, message: { role, content: text } });
  saveEntries();

  return id;
}

// One clean turn, in the order observed from the real thing.
async function runTurn(text, prompt) {
  running = true;
  aborted = false;
  turns += 1;
  addEntry("user", prompt);
  // What the deltas spell, so the assistant entry's content matches the
  // stream a door saw. Contract 02 §10.5 copies both into Open WebUI.
  let answer = "";

  send({ type: "agent_start" });
  send({ type: "turn_start" });
  send({ type: "message_start", message: { role: "assistant", session: sessionId } });

  for (let i = 0; i < DELTAS; i += 1) {
    await sleep(DELAY_MS);
    if (aborted) {
      break;
    }
    // A killed process: exit without settling, mid-stream, so the playpen
    // has to report process_exit and fail the turn on its own.
    if (DIE_AT > 0 && i + 1 >= DIE_AT) {
      process.exit(9);
    }
    answer += i === 0 ? `${text} mid end ` : `${text}#${i} `;
    send({
      type: "message_update",
      usage: { input: 10, output: 1, cacheRead: 0, cacheWrite: 0, cost: { total: 0.001 } },
      assistantMessageEvent: {
        type: "text_delta",
        contentIndex: 0,
        // The first delta carries U+2028 and U+2029. JSON.stringify leaves
        // both literal in the output, so any reader that treats them as line
        // breaks turns this one line into three unparsable fragments. That is
        // the readline defect the rpc doc warns about, and it now fails a
        // local test instead of surviving to the host.
        delta: i === 0 ? `${text} mid end ` : `${text}#${i} `,
      },
    });
  }

  addEntry("assistant", answer);
  send({ type: "message_end", message: { role: "assistant", session: sessionId } });
  send({ type: "turn_end", message: { role: "assistant" }, toolResults: [] });
  send({ type: "agent_end", messages: [], willRetry: false });
  send({ type: "agent_settled", steered, turns });
  steered = null;
  running = false;
}

function onGetEntries(command) {
  if (NO_ENTRIES) {
    send({ type: "response", id: command.id, command: "get_entries", success: false });
    return;
  }

  const since = command.since;
  const at = since === undefined ? -1 : entries.findIndex((entry) => entry.id === since);
  if (since !== undefined && at === -1) {
    // Rule 5: an unknown cursor is a refusal, not an empty list.
    send({ type: "response", id: command.id, command: "get_entries", success: false });
    return;
  }

  const slice = entries.slice(at + 1);
  send({
    type: "response",
    id: command.id,
    command: "get_entries",
    success: true,
    data: { entries: slice, leafId: entries[entries.length - 1]?.id ?? null },
  });
}

// Contract 03 §4.1. pi branches from a previous user message on the ACTIVE
// branch, and refuses a target that is not on it. A branch drops what
// followed the fork point, so the next prompt is a sibling of the old turn
// rather than its continuation.
function onFork(command) {
  const at = entries.findIndex((entry) => entry.id === command.entryId);
  logFork(command.entryId, at !== -1);

  if (at === -1) {
    send({
      type: "response",
      id: command.id,
      command: "fork",
      success: false,
      error: "no such entry on the active branch",
    });
    return;
  }

  entries.length = at;
  saveEntries();
  send({ type: "response", id: command.id, command: "fork", success: true });
}

// No channel message reports a fork, and `buildTurnEnv` passes five names
// through, so a harness that has to see one reads this file. One path per pi
// process: two appends to one file lose a row under load.
function logFork(entryId, ok) {
  if (!FORK_LOG) {
    return;
  }

  appendFileSync(FORK_LOG, JSON.stringify({ entryId: entryId ?? null, ok }) + "\n");
}

function onCommand(command) {
  if (command.type === "prompt") {
    // Rule 2: accept first, then stream. A prompt that arrives mid-stream
    // without a streamingBehavior is rejected, exactly as pi rejects it.
    if (running && !command.streamingBehavior) {
      send({ type: "response", id: command.id, command: "prompt", success: false });
      return;
    }
    // pi 0.99.1 `docs/rpc-commands.md`: `handled` means an extension consumed
    // the prompt and no run started, so no `agent_settled` follows.
    if (HANDLED) {
      send({ type: "response", id: command.id, command: "prompt", success: true, data: { disposition: "handled" } });
      return;
    }
    const message = String(command.message || "");
    send({ type: "response", id: command.id, command: "prompt", success: true });
    void runTurn(message.slice(0, 24) || "FIXTURE", message);
    return;
  }

  if (command.type === "abort") {
    aborted = true;
    send({ type: "response", id: command.id, command: "abort", success: true });
    return;
  }

  if (command.type === "steer") {
    steered = String(command.message || "");
    send({ type: "response", id: command.id, command: "steer", success: true });
    return;
  }

  if (command.type === "get_entries") {
    onGetEntries(command);
    return;
  }

  if (command.type === "fork") {
    onFork(command);
    return;
  }

  send({ type: "response", id: command.id, command: String(command.type), success: false });
}

// Split on \n only. The real protocol forbids generic line readers, and the
// stand-in must not be more forgiving than the thing it stands in for.
let inbox = "";
function feed(chunk) {
  inbox += chunk;
  const parts = inbox.split("\n");
  inbox = parts.pop();

  for (const part of parts) {
    const line = part.replace(/\r$/, "");
    if (line.length === 0) {
      continue;
    }
    try {
      onCommand(JSON.parse(line));
    } catch {
      send({ type: "response", command: "unknown", success: false });
    }
  }
}

async function main() {
  await sleep(BOOT_MS);

  process.stdin.setEncoding("utf8");
  process.stdin.on("data", feed);

  // Rule 4: EOF kills the process immediately, in-flight turn and all.
  process.stdin.on("end", () => process.exit(0));
}

void main();
