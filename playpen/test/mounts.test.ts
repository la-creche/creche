// Contract 03 §7.1. Where the three read-mounts are, and what happens when
// the environment does not say.
//
// sbx mounts a host directory at its own HOST path inside the VM, so `/run`
// never exists there. A fallback to `/run/family` would name a directory that
// cannot exist, and every turn would fail as `channel_lost` with the cause
// only in a log file. These tests keep the fallback out.

import { describe, expect, it } from "vitest";

import {
  CONTROL_DIR_ENV,
  CRED_DIR_ENV,
  FAMILY_CONFIG_DIR_ENV,
  MountState,
  readMountDirs,
  unsetMountFatal,
} from "../src/mounts.js";

const CREDS = "/srv/agents/state/rework/families/chat/creds";
const CONFIG = "/srv/agents/state/rework/families/chat/config";
const CONTROL = "/srv/agents/state/rework/families/chat/control";

function named(): NodeJS.ProcessEnv {
  return {
    [CRED_DIR_ENV]: CREDS,
    [FAMILY_CONFIG_DIR_ENV]: CONFIG,
    [CONTROL_DIR_ENV]: CONTROL,
  };
}

describe("readMountDirs", () => {
  it("takes all three directories from the environment", () => {
    const outcome = readMountDirs(named());

    expect(outcome.state).toBe(MountState.Named);
    expect(outcome.dirs).toEqual({ creds: CREDS, config: CONFIG, control: CONTROL });
    expect(outcome.detail).toBe("");
  });

  it("keeps the host path as the in-VM path", () => {
    // The whole rule in one assertion: nothing rewrites the path, because the
    // mount's in-VM path IS its host path.
    const outcome = readMountDirs(named());

    expect(outcome.dirs?.control).toBe(CONTROL);
  });

  it.each([CRED_DIR_ENV, FAMILY_CONFIG_DIR_ENV, CONTROL_DIR_ENV])(
    "refuses to guess when %s is absent",
    (name) => {
      const env = named();
      delete env[name];

      const outcome = readMountDirs(env);

      expect(outcome.state).toBe(MountState.Unset);
      expect(outcome.dirs).toBeNull();
      expect(outcome.detail).toContain(name);
    },
  );

  it("treats an empty value as absent", () => {
    const outcome = readMountDirs({ ...named(), [CRED_DIR_ENV]: "" });

    expect(outcome.state).toBe(MountState.Unset);
    expect(outcome.detail).toContain(CRED_DIR_ENV);
  });

  it("names every missing variable, not only the first", () => {
    const outcome = readMountDirs({});

    expect(outcome.detail).toContain(CRED_DIR_ENV);
    expect(outcome.detail).toContain(FAMILY_CONFIG_DIR_ENV);
    expect(outcome.detail).toContain(CONTROL_DIR_ENV);
  });

  it("never falls back to a path under /run", () => {
    const outcome = readMountDirs({});

    expect(outcome.dirs).toBeNull();
    expect(outcome.detail).not.toContain("/run");
  });
});

describe("unsetMountFatal", () => {
  it("is the fatal line of §5.7, with its own reason", () => {
    const line = unsetMountFatal(readMountDirs({}).detail);

    expect(line.type).toBe("fatal");
    expect(line.reason).toBe("mount_dir_unset");
    expect(line.message).toContain(CRED_DIR_ENV);
  });

  it("says which contract section the operator should read", () => {
    const line = unsetMountFatal("AGENT_CRED_DIR names no directory");

    expect(line.message).toContain("7.1");
  });
});
