// `creche_sessions` and Flue's own tables share one file, `flue.db`, as in
// a sandbox: Flue's `sqlite(path)` on one connection, the index on another.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { init } from "@flue/runtime";
import { sqlite } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { SessionIndex } from "../src/session-index.js";
import { faux, Probe, startFlue } from "./faux-flue.js";

const file = join(mkdtempSync(join(tmpdir(), "facade-db-")), "flue.db");
const db = new DatabaseSync(file);
const index = new SessionIndex(db);
const flue = await startFlue([Probe], sqlite(file));

afterAll(async () => {
  await flue.stop();
  db.close();
});

describe("flue.db", () => {
  it("holds creche_sessions beside the tables of Flue", async () => {
    index.create("s-1", "/work", "2026-10-25T12:00:00.000Z");
    faux.setResponses([fauxAssistantMessage("done")]);
    const handle = init(Probe, { id: "s-1" });
    await handle.read(await handle.dispatch("Hello"));
    index.touch("s-1", "2026-10-25T12:00:09.000Z");

    const tables = db.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all() as { name: string }[];

    expect(tables.length).toBeGreaterThan(1);
    expect(tables.map((table) => table.name)).toContain("creche_sessions");
    expect(index.get("s-1")?.updatedAt).toBe("2026-10-25T12:00:09.000Z");
  });
});
