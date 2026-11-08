// The vmm backend of the family state (decisions CH5a, CH23, CH62 rule 13).
// A fake socket serves the vmm end of host port 1100 as guest init splices
// it: it reads each `FamilyStatePull` line and answers one
// `FamilyStateReply` line. Each pull dials a link of its own. A typed fault
// fails the pull with the fault word of the file backend, and nothing keeps
// a last good config.

import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { PULL_LINE, pullState, STATE_REPLY_CAP, vmmBackend } from "../src/family-state.js";
import type { LiveConfig, StatePull } from "../src/family-state.js";
import { revealSecret } from "../src/secret.js";
import { readSkills } from "../src/skills.js";
import { validator } from "../src/wire-read.js";
import { closeFakeVmms, CONFIG, CREDS, fakeVmm, replying, sayer, SKILL, socketPath, stateLine } from "./fake-vmm.js";
import { familyEnv, FIXTURE_KEY, FIXTURE_TOKEN } from "./family.js";

const START: LiveConfig = { modelAlias: "chat", instructions: null, systemPrompt: "append", sandboxTools: [], skills: [] };
const SHORT_WAIT_MS = 200;

afterEach(closeFakeVmms);

/** The config of a pull, by the parts a test compares. */
function shown(pull: StatePull): unknown {
  if (!pull.ok) {
    return pull;
  }

  const { config } = pull.state;

  return { ...config, skills: config.skills.map((skill) => skill.name), stale: pull.stale };
}

describe("PULL_LINE", () => {
  it("is one wire FamilyStatePull in the compact form and one LF", () => {
    expect(PULL_LINE).toBe("{}\n");
    expect(validator("FamilyStatePull").Check(JSON.parse(PULL_LINE))).toBe(true);
  });
});

describe("pullState", () => {
  it("sends one pull line and reads the state of the reply", async () => {
    const vmm = await replying(stateLine());
    const pull = await pullState(vmm.path, [], sayer().say);

    expect(vmm.links).toEqual([PULL_LINE]);
    expect(shown(pull)).toEqual({
      modelAlias: "chat",
      instructions: "FIXTURE-INSTRUCTIONS",
      systemPrompt: "replace",
      sandboxTools: ["read", "bash"],
      skills: ["weather-report"],
      stale: null,
    });
    if (!pull.ok) {
      throw new Error("the pull failed");
    }

    expect(revealSecret(pull.state.creds.litellmKey)).toBe(FIXTURE_KEY);
    expect(revealSecret(pull.state.creds.pepToken)).toBe(FIXTURE_TOKEN);
  });

  it("gives each typed fault reply its fault word", async () => {
    for (const fault of ["creds_missing", "config_missing", "not_valid", "too_large"] as const) {
      const vmm = await replying(JSON.stringify({ [fault]: {} }));

      expect(await pullState(vmm.path, [], sayer().say)).toEqual({ ok: false, fault });
    }
  });

  it("refuses a reply that is not a FamilyStateReply as not_valid", async () => {
    const lines = [
      JSON.stringify({ state: { config: CONFIG } }),
      JSON.stringify({ state: { config: CONFIG, creds: { ...CREDS, litellm_key: "FIXTURE-NO-PREFIX" } } }),
      '{"creds_missing":{},"creds_missing":{}}',
      "not json",
    ];
    for (const line of lines) {
      const vmm = await replying(line);

      expect(await pullState(vmm.path, [], sayer().say)).toEqual({ ok: false, fault: "not_valid" });
    }
  });

  it("refuses the alias litellm/, which names no model, as not_valid", async () => {
    const vmm = await replying(stateLine({ ...CONFIG, model_alias: "litellm/" }));

    expect(await pullState(vmm.path, [], sayer().say)).toEqual({ ok: false, fault: "not_valid" });
  });

  it("reads a reply line of exactly the cap", async () => {
    const pad = STATE_REPLY_CAP - stateLine({ ...CONFIG, instructions: "" }).length;
    const line = stateLine({ ...CONFIG, instructions: "x".repeat(pad) });
    expect(Buffer.byteLength(line, "utf8")).toBe(STATE_REPLY_CAP);
    const vmm = await replying(line);

    expect((await pullState(vmm.path, [], sayer().say)).ok).toBe(true);
  });

  it("refuses a line over the cap as too_large, and closes the link before its LF", async () => {
    let closed = false;
    const vmm = await fakeVmm((_link, socket) => {
      socket.on("close", () => {
        closed = true;
      });
      socket.write("x".repeat(STATE_REPLY_CAP + 1));
    });
    const said = sayer();

    expect(await pullState(vmm.path, [], said.say)).toEqual({ ok: false, fault: "too_large" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(closed).toBe(true);
    expect(said.lines).toEqual([`the family-state pull got no state: a reply line over ${String(STATE_REPLY_CAP)} bytes`]);
  });

  it("fails a link that closes with no reply as not_valid, and says why", async () => {
    const vmm = await fakeVmm((_link, socket) => socket.end());
    const said = sayer();

    expect(await pullState(vmm.path, [], said.say)).toEqual({ ok: false, fault: "not_valid" });
    expect(said.lines).toEqual(["the family-state pull got no state: the link closed with no reply"]);
  });

  it("fails a pull with no listener as not_valid, and says why", async () => {
    const said = sayer();

    expect(await pullState(socketPath(), [], said.say)).toEqual({ ok: false, fault: "not_valid" });
    expect(said.lines).toHaveLength(1);
    expect(said.lines[0]).toMatch(/^the family-state pull got no state: the link closed with no reply: .*ENOENT/);
  });

  it("closes a pull that gets no reply in its wait", async () => {
    const vmm = await fakeVmm(() => undefined);
    const said = sayer();

    expect(await pullState(vmm.path, [], said.say, SHORT_WAIT_MS)).toEqual({ ok: false, fault: "not_valid" });
    expect(said.lines).toEqual([`the family-state pull got no state: no reply in ${String(SHORT_WAIT_MS)} ms`]);
  });
});

describe("vmmBackend", () => {
  it("dials a link of its own for each pull", async () => {
    const vmm = await replying(stateLine());
    const backend = vmmBackend(vmm.path, START, sayer().say);

    expect((await backend.pull()).ok).toBe(true);
    expect((await backend.pull()).ok).toBe(true);
    expect(vmm.links).toEqual([PULL_LINE, PULL_LINE]);
  });

  it("fails the pull at a typed fault and keeps no last good config", async () => {
    const replies = [stateLine(), JSON.stringify({ not_valid: {} }), stateLine({ ...CONFIG, instructions: "" })];
    const vmm = await fakeVmm((link, socket) => socket.write(`${replies[link] ?? ""}\n`));
    const backend = vmmBackend(vmm.path, START, sayer().say);

    expect(shown(await backend.pull())).toMatchObject({ instructions: "FIXTURE-INSTRUCTIONS", stale: null });
    expect(await backend.pull()).toEqual({ ok: false, fault: "not_valid" });
    expect(shown(await backend.pull())).toMatchObject({ instructions: null, stale: null });
  });

  it("keeps a skill of the start, files included, while its text does not change", async () => {
    const env = familyEnv({
      configFiles: { "skills/weather-report/SKILL.md": SKILL, "skills/weather-report/notes.md": "FIXTURE-NOTES" },
    });
    const start = readSkills(join(env["AGENT_FAMILY_CONFIG_DIR"] ?? "", "skills")).skills;
    const vmm = await replying(stateLine());
    const pull = await vmmBackend(vmm.path, { ...START, skills: start }, sayer().say).pull();
    if (!pull.ok) {
      throw new Error("the pull failed");
    }

    expect(pull.state.config.skills[0]?.definition).toBe(start[0]?.definition);
  });
});
