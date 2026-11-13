// The start of the agent under CH (decisions CH5a, CH5e, CH23 rule 2,
// CH80). The start reads no `AGENT_*` variable: the store and the
// workspace are the fixed guest paths, the planes are guest loopback, and
// the creds and the config come from the first pull that gives a state. A
// fault pulls again after a pause that doubles up to its cap.

import { createServer } from "node:net";

import { afterEach, describe, expect, it } from "vitest";

import { readGuestStart } from "../src/guest-start.js";
import { FLUE_DB, WORKSPACE } from "../src/guest.js";
import { revealSecret } from "../src/secret.js";
import { closeFakeVmms, CONFIG, fakeVmm, replying, sayer, SKILL, socketPath, stateLine } from "./fake-vmm.js";
import { FIXTURE_KEY, FIXTURE_TOKEN } from "./family.js";

const QUICK = { firstMs: 1, capMs: 4 };

afterEach(closeFakeVmms);

describe("readGuestStart", () => {
  it("takes the fixed guest paths, the guest planes and the state of the pull", async () => {
    const vmm = await replying(stateLine({ ...CONFIG, skills: { "weather-report": SKILL, broken: "no frontmatter" } }));
    const said = sayer();
    const start = await readGuestStart({ MPLBACKEND: "Agg" }, said.say, vmm.path, QUICK);
    expect(start.store).toBe(FLUE_DB);
    expect(start.workspace).toBe(WORKSPACE);
    expect([start.planes.litellm.text, start.planes.pep.text]).toEqual(["http://127.0.0.1:4000", "http://127.0.0.1:8300"]);
    expect(start.toolEnv).toEqual({ MPLBACKEND: "Agg" });
    expect(start.state.config.modelAlias).toBe("chat");
    expect(start.state.config.skills.map((skill) => skill.name)).toEqual(["weather-report"]);
    expect(start.skippedSkills).toEqual(["broken"]);
    expect(revealSecret(start.state.creds.litellmKey)).toBe(FIXTURE_KEY);
    expect(revealSecret(start.state.creds.pepToken)).toBe(FIXTURE_TOKEN);
    expect(said.lines).toEqual([]);
  });

  it("reads no AGENT_* variable", async () => {
    const vmm = await replying(stateLine());
    const env = { AGENT_SESSIONS_DIR: "/nonexistent/sessions", AGENT_PEP_URL: "http://192.0.2.1:1", AGENT_LITELLM_BASE_URL: "http://192.0.2.2:2" };
    const start = await readGuestStart(env, sayer().say, vmm.path, QUICK);

    expect(start.store).toBe(FLUE_DB);
    expect(start.planes.pep.text).toBe("http://127.0.0.1:8300");
    expect(start.planes.litellm.text).toBe("http://127.0.0.1:4000");
    expect(start.toolEnv).toEqual({});
  });

  it("pulls again after each fault, with a pause that doubles up to its cap", async () => {
    const replies = [
      JSON.stringify({ creds_missing: {} }),
      JSON.stringify({ config_missing: {} }),
      JSON.stringify({ not_valid: {} }),
      JSON.stringify({ too_large: {} }),
      stateLine(),
    ];
    const vmm = await fakeVmm((link, socket) => socket.write(`${replies[link] ?? ""}\n`));
    const said = sayer();
    const start = await readGuestStart({}, said.say, vmm.path, QUICK);

    expect(start.store).toBe(FLUE_DB);
    expect(vmm.links).toHaveLength(5);
    expect(said.lines).toEqual([
      "the start pull of the family state failed (creds_missing); the next pull is in 1 ms",
      "the start pull of the family state failed (config_missing); the next pull is in 2 ms",
      "the start pull of the family state failed (not_valid); the next pull is in 4 ms",
      "the start pull of the family state failed (too_large); the next pull is in 4 ms",
    ]);
  });

  it("pulls again while no listener answers, and says why", async () => {
    const path = socketPath();
    const said = sayer();
    const reading = readGuestStart({}, said.say, path, QUICK);
    await new Promise((resolve) => setTimeout(resolve, 30));
    const late = createServer((socket) => socket.on("data", () => socket.end(`${stateLine()}\n`)));
    await new Promise<void>((resolve) => late.listen(path, resolve));
    const start = await reading;
    late.close();

    expect(start.store).toBe(FLUE_DB);
    expect(said.lines[0]).toMatch(/^the family-state pull got no state: the link closed with no reply: .*ENOENT/);
    expect(said.lines[1]).toBe("the start pull of the family state failed (not_valid); the next pull is in 1 ms");
  });
});
