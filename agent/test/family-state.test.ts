// The file backend of the family state (decision CH5a): it reads the
// mounts at each pull. A config folder swapped as caregiver swaps it is
// seen at the next pull. A config read failure keeps the last good config,
// and names its fault at the first failing pull only. A creds read failure
// fails the pull.

import { chmodSync, mkdirSync, rmSync, symlinkSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { fileBackend, inOrder, STATE_REPLY_CAP } from "../src/family-state.js";
import type { LiveConfig, StatePull } from "../src/family-state.js";
import { readMountDirs } from "../src/mounts.js";
import type { MountDirs } from "../src/mounts.js";
import { revealSecret } from "../src/secret.js";
import { readSkills } from "../src/skills.js";
import { familyEnv, FIXTURE_KEY, FIXTURE_TOKEN, swapConfig } from "./family.js";

const NO_RETRY_MS = 0;
const RUNS_AS_ROOT = process.getuid?.() === 0;
const NO_ACCESS = 0o000;
const OPEN = 0o700;
const START: LiveConfig = { modelAlias: "chat", instructions: null, systemPrompt: "append", sandboxTools: [], skills: [] };
const SKILL = "---\nname: weather-report\ndescription: Write a short weather report.\n---\nSearch first.\n";
const RUNTIME = { model_alias: "chat", sandbox_tools: ["bash", "read"], system_prompt: "replace" };

function mounts(): MountDirs {
  const read = readMountDirs(
    familyEnv({
      runtime: RUNTIME,
      configFiles: {
        "instructions.md": "  FIXTURE-OLD  \n",
        "skills/weather-report/SKILL.md": SKILL,
        "skills/weather-report/notes.md": "FIXTURE-NOTES",
      },
    }),
  );
  if (!read.ok) {
    throw new Error("fixture mounts refused");
  }

  return read.dirs;
}

/** The config of a pull, by the parts a test compares. */
function shown(pull: StatePull): unknown {
  if (!pull.ok) {
    return pull;
  }

  const { config } = pull.state;
  const skills = config.skills.map((skill) => skill.name);

  return { ...config, skills, stale: pull.stale };
}

describe("inOrder", () => {
  it("starts a pull only after the pull before it settles", async () => {
    const events: string[] = [];
    // The first call is slow and the second is fast.
    const delays = [30, 0];
    const pull = inOrder(async (): Promise<StatePull> => {
      const call = events.length / 2;
      events.push(`start ${call}`);
      await new Promise((resolve) => setTimeout(resolve, delays[call]));
      events.push(`end ${call}`);

      return { ok: false, fault: "creds_missing" };
    });

    await Promise.all([pull(), pull()]);

    expect(events).toEqual(["start 0", "end 0", "start 1", "end 1"]);
  });
});

describe("fileBackend", () => {
  it("reads the creds and the config of the mounts", async () => {
    const pull = await fileBackend(mounts(), START, NO_RETRY_MS).pull();
    if (!pull.ok) {
      throw new Error(pull.fault);
    }

    expect(shown(pull)).toEqual({
      modelAlias: "chat",
      instructions: "FIXTURE-OLD",
      systemPrompt: "replace",
      sandboxTools: ["read", "bash"],
      skills: ["weather-report"],
      stale: null,
    });
    expect(revealSecret(pull.state.creds.litellmKey)).toBe(FIXTURE_KEY);
    expect(revealSecret(pull.state.creds.pepToken)).toBe(FIXTURE_TOKEN);
  });

  it("sees a config folder swapped as caregiver swaps it", async () => {
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    await backend.pull();

    swapConfig(dirs.config, { "runtime.json": JSON.stringify({ model_alias: "chat" }), "instructions.md": "FIXTURE-NEW" });

    expect(shown(await backend.pull())).toEqual({
      modelAlias: "chat",
      instructions: "FIXTURE-NEW",
      systemPrompt: "append",
      sandboxTools: [],
      skills: [],
      stale: null,
    });
  });

  it("keeps the last good config when the config does not read, and names the fault once", async () => {
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    const good = shown(await backend.pull());

    rmSync(dirs.config, { recursive: true });
    const first = await backend.pull();
    const second = await backend.pull();

    expect(first.ok && first.stale).toBe("config_missing");
    expect(second.ok && second.stale).toBeNull();
    expect(shown(second)).toEqual(good);
  });

  it("keeps the last good config when the alias is the bare prefix litellm/", async () => {
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    const good = shown(await backend.pull());

    swapConfig(dirs.config, { "runtime.json": JSON.stringify({ model_alias: "litellm/" }) });
    const pull = await backend.pull();

    expect(pull.ok && pull.stale).toBe("not_valid");
    expect(shown(pull)).toEqual({ ...(good as object), stale: "not_valid" });
  });

  it("keeps the last good config when the folder gives EACCES, as a dead sbx mount does", async ({ skip }) => {
    skip(RUNS_AS_ROOT, "root reads a folder of mode 000");
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    const good = shown(await backend.pull());

    chmodSync(dirs.config, NO_ACCESS);
    const pull = await backend.pull();
    chmodSync(dirs.config, OPEN);

    expect(pull.ok && pull.stale).toBe("not_valid");
    expect({ ...(shown(pull) as object), stale: null }).toEqual(good);
  });

  it("names the fault again after a recovery and a new failure", async () => {
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    const stales: unknown[] = [];
    for (const step of ["remove", "swap", "remove"]) {
      if (step === "remove") {
        rmSync(dirs.config, { recursive: true });
      } else {
        mkdirSync(dirs.config);
        swapConfig(dirs.config, { "runtime.json": JSON.stringify({ model_alias: "chat" }) });
      }

      const pull = await backend.pull();
      stales.push(pull.ok && pull.stale);
    }

    expect(stales).toEqual(["config_missing", null, "config_missing"]);
  });

  it("refuses a listed file that does not read, so a half deleted folder is not kept", async () => {
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    rmSync(join(dirs.config, "instructions.md"));
    // Listed, and ENOENT on the read: the file vanished after the listing.
    symlinkSync(join(dirs.config, "gone.md"), join(dirs.config, "instructions.md"));

    expect(shown(await backend.pull())).toEqual({ ...START, stale: "not_valid" });
  });

  it("keeps a skill of the start, files included, while its text does not change", async () => {
    const dirs = mounts();
    const start = readSkills(join(dirs.config, "skills")).skills;
    const backend = fileBackend(dirs, { ...START, skills: start }, NO_RETRY_MS);
    const kept = await backend.pull();

    swapConfig(dirs.config, {
      "runtime.json": JSON.stringify(RUNTIME),
      "skills/weather-report/SKILL.md": `${SKILL}Then write.\n`,
    });
    const changed = await backend.pull();

    expect(kept.ok && kept.state.config.skills[0]?.definition).toBe(start[0]?.definition);
    expect(start[0]?.definition.files).toHaveProperty("notes.md");
    expect(changed.ok && changed.state.config.skills[0]?.definition.files).toBeUndefined();
  });

  it("keeps the last good config when the state is over the cap of 256 KiB", async () => {
    const dirs = mounts();
    const backend = fileBackend(dirs, START, NO_RETRY_MS);
    const good = shown(await backend.pull());

    swapConfig(dirs.config, { "runtime.json": JSON.stringify(RUNTIME), "instructions.md": "x".repeat(STATE_REPLY_CAP) });
    const pull = await backend.pull();

    expect(pull.ok && pull.stale).toBe("too_large");
    expect({ ...(shown(pull) as object), stale: null }).toEqual(good);
  });

  it("keeps the start config when the first config is not valid", async () => {
    const dirs = mounts();
    swapConfig(dirs.config, { "runtime.json": JSON.stringify({ model_alias: "Not An Alias" }) });

    const pull = await fileBackend(dirs, START, NO_RETRY_MS).pull();

    expect(shown(pull)).toEqual({ ...START, stale: "not_valid" });
  });

  it("fails the pull when creds.json is absent", async () => {
    const dirs = mounts();
    rmSync(join(dirs.creds, "creds.json"));

    expect(await fileBackend(dirs, START, NO_RETRY_MS).pull()).toEqual({ ok: false, fault: "creds_missing" });
  });
});
