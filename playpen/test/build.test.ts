// The site's LAN address is fixed when the image is BUILT.
//
// The playpen runs inside a sandbox, with no site file and no such
// environment, and contract 03 §7 fixes LiteLLM's and the PEP's URLs for the
// life of an image. So `build.mjs` writes `AGENT_LAN_ADDRESS` into the bundle
// as `__AGENT_LAN_ADDRESS__`, and refuses to build without it: a default would
// be somebody's host.

import { describe, expect, it } from "vitest";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PACKAGE = join(__dirname, "..");
const VARIABLE = "AGENT_LAN_ADDRESS";

/** `node build.mjs src/constants.ts <out>` with `address` as the variable,
 * or with no variable at all. */
function build(address: string | null): { status: number | null; stderr: string; out: string } {
  const out = join(mkdtempSync(join(tmpdir(), "lan-build-")), "constants.js");
  const env: NodeJS.ProcessEnv = { ...process.env };
  delete env[VARIABLE];
  if (address !== null) {
    env[VARIABLE] = address;
  }

  const done = spawnSync(process.execPath, ["build.mjs", "src/constants.ts", out], {
    cwd: PACKAGE,
    env,
    encoding: "utf8",
  });

  return { status: done.status, stderr: done.stderr, out };
}

describe("build.mjs", () => {
  it("fixes both plane URLs on the address it is given", () => {
    const built = build("192.0.2.99");

    expect(built.status).toBe(0);
    const bundle = readFileSync(built.out, "utf8");
    expect(bundle).toContain("192.0.2.99");
    expect(bundle).not.toContain("__AGENT_LAN_ADDRESS__");
  });

  it("refuses to build without the address, and names the variable", () => {
    const built = build(null);

    expect(built.status).not.toBe(0);
    expect(built.stderr).toContain(VARIABLE);
  });

  it("is what every bundle that imports src/ is built with", () => {
    const scripts = JSON.parse(readFileSync(join(PACKAGE, "package.json"), "utf8")).scripts;

    expect(scripts["build:playpen"]).toMatch(/^node build\.mjs /);
    expect(scripts["build:launch"]).toMatch(/^node build\.mjs /);
  });

  it("gets the address from the image build's own argument", () => {
    const dockerfile = readFileSync(join(PACKAGE, "Dockerfile"), "utf8");
    const stage = dockerfile.slice(dockerfile.indexOf(" AS build\n"), dockerfile.indexOf(" AS agent-sandbox\n"));

    expect(stage).toContain(`ARG ${VARIABLE}\n`);
    expect(stage).toMatch(/^COPY .*playpen\/build\.mjs .*\.\/$/m);
  });
});
