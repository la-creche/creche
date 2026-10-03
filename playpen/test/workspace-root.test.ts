// Contract 03 §7.1 rule 6 and §7.2. Where the `code-sandbox` work root is.
//
// The default is the contract's path, because sbx mounts a host directory at
// that SAME path inside the VM. `AGENT_CODE_SANDBOX_ROOT` is the seam a
// harness that runs this bundle off the host needs, for the reason
// `SESSIOND_SANDBOX_SESSIONS_MOUNT` already exists: no sbx mount put the work
// root there, so both sides have to agree on where the harness did put it.

import { describe, expect, it } from "vitest";

import { CODE_SANDBOX_ROOT, CODE_SANDBOX_ROOT_ENV } from "../src/constants.js";
import { codeSandboxRoot } from "../src/workspace.js";

describe("the code-sandbox work root", () => {
  it("is the contract's path when the environment names none", () => {
    expect(codeSandboxRoot({})).toBe(CODE_SANDBOX_ROOT);
  });

  it("is the environment's path when it names one", () => {
    expect(codeSandboxRoot({ [CODE_SANDBOX_ROOT_ENV]: "/tmp/agi/work" })).toBe("/tmp/agi/work");
  });

  it("ignores an empty or blank value rather than linking into nothing", () => {
    expect(codeSandboxRoot({ [CODE_SANDBOX_ROOT_ENV]: "" })).toBe(CODE_SANDBOX_ROOT);
    expect(codeSandboxRoot({ [CODE_SANDBOX_ROOT_ENV]: "   " })).toBe(CODE_SANDBOX_ROOT);
  });

  it("takes an absolute path only: a relative one would resolve per process", () => {
    expect(codeSandboxRoot({ [CODE_SANDBOX_ROOT_ENV]: "work/code-sandbox" })).toBe(
      CODE_SANDBOX_ROOT,
    );
  });
});
