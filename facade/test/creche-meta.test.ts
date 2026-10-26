// The creche keys of `_meta` (vectors `session_meta`): checked, kept on the
// session row, sent back in `session_info_update`, never in the prompt.

import { DatabaseSync } from "node:sqlite";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { facadeApp } from "../src/acp.js";
import { jobOf, promptMetaOf } from "../src/creche-meta.js";
import { sessionService } from "../src/service.js";
import { SessionIndex } from "../src/session-index.js";
import { Wire } from "./acp-wire.js";
import { faux, Probe, startFlue } from "./faux-flue.js";

const { job, delegation, trigger, info_update: infoUpdate } = vectors.session_meta;
const INVALID_PARAMS = -32602;
const NO_HOOKS = { onBeat: () => undefined, onShutdown: () => undefined };

const flue = await startFlue();
afterAll(() => flue.stop());

let db: DatabaseSync;
let index: SessionIndex;
let wire: Wire;

beforeEach(() => {
  db = new DatabaseSync(":memory:");
  index = new SessionIndex(db);
  wire = new Wire(facadeApp(NO_HOOKS, sessionService({ agent: Probe, agentName: "Probe", index, store: flue.store })));
});

async function newSession(meta?: Record<string, unknown>): Promise<string> {
  const answer = await wire.request("session/new", { cwd: "/work", mcpServers: [], _meta: meta });

  return answer.result?.["sessionId"] as string;
}

function prompt(sessionId: string, meta: Record<string, unknown>): ReturnType<Wire["request"]> {
  return wire.request("session/prompt", { sessionId, prompt: [{ type: "text", text: "Take the ticket." }], _meta: meta });
}

describe("the shapes of the vector file", () => {
  it("reads each vector value", () => {
    expect(jobOf({ [job.key]: job.value })).toEqual({ ok: true, value: job.value });
    expect(promptMetaOf({ [delegation.key]: delegation.value, [trigger.key]: trigger.value })).toEqual({
      ok: true,
      value: { delegation: delegation.value.id, trigger: trigger.value },
    });
    expect(promptMetaOf({ other: 1 })).toEqual({ ok: true, value: { delegation: null, trigger: null } });
    expect(jobOf(undefined)).toEqual({ ok: true, value: null });
  });

  it("refuses a value of another shape", () => {
    const bad = [
      jobOf({ [job.key]: "job-1" }),
      promptMetaOf({ [delegation.key]: { id: delegation.value.id } }),
      promptMetaOf({ [delegation.key]: { ...delegation.value, extra: 1 } }),
      promptMetaOf({ [trigger.key]: { ...trigger.value, kind: "cron" } }),
      promptMetaOf({ [trigger.key]: { ...trigger.value, fired_at: "yesterday" } }),
      promptMetaOf({ [trigger.key]: { ...trigger.value, chain: Array.from({ length: 9 }, () => "chat") } }),
    ];

    expect(bad.map((outcome) => outcome.ok)).toEqual([false, false, false, false, false, false]);
  });
});

describe("the session row", () => {
  it("keeps each key, sends it back, and keeps it out of the prompt", async () => {
    const seen: string[] = [];
    faux.setResponses([
      (context) => {
        seen.push(JSON.stringify(context));
        return fauxAssistantMessage("Done.");
      },
    ]);

    const id = await newSession({ [job.key]: job.value });
    const answer = await prompt(id, { [delegation.key]: delegation.value, [trigger.key]: trigger.value });

    expect(answer.result).toEqual({ stopReason: "end_turn" });
    expect(index.get(id)).toMatchObject({ jobId: job.value, delegation: delegation.value.id, trigger: trigger.value });
    expect(wire.updates(id).at(-1)?.["_meta"]).toEqual(infoUpdate._meta);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toContain("Take the ticket.");
    for (const fact of [job.value, delegation.value.id, trigger.value.name, "creche"]) {
      expect(seen[0]).not.toContain(fact);
    }
  });

  it("refuses a bad key on session/new and lists no session", async () => {
    const answer = await wire.request("session/new", { cwd: "/work", mcpServers: [], _meta: { [job.key]: 7 } });

    expect(answer.error?.code).toBe(INVALID_PARAMS);
    expect((await wire.request("session/list", {})).result?.["sessions"]).toEqual([]);
  });

  it("refuses a second delegation for one session", async () => {
    faux.setResponses([fauxAssistantMessage("One.")]);
    const id = await newSession();
    await prompt(id, { [delegation.key]: delegation.value });
    const other = { ...delegation.value, id: "01K5J9QWB9R1V3T5Y7H9J2K4P6" };

    expect((await prompt(id, { [delegation.key]: other })).error?.code).toBe(INVALID_PARAMS);
    expect(index.get(id)?.delegation).toBe(delegation.value.id);
  });

  it("adds the columns to a table of an older facade", () => {
    const old = new DatabaseSync(":memory:");
    old.exec(
      "CREATE TABLE creche_sessions (id TEXT PRIMARY KEY, title TEXT, cwd TEXT NOT NULL, " +
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    );
    const upgraded = new SessionIndex(old);
    upgraded.create("s-1", "/work", "2026-10-25T12:00:00.000Z", job.value);

    expect(upgraded.get("s-1")).toMatchObject({ jobId: job.value, delegation: null, trigger: null });
  });
});
