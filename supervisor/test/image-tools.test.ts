// A granted built-in is only a tool when the image holds the program behind it.
//
// pi's `grep` runs ripgrep and its `find` runs fd (0.99.1
// `dist/utils/tools-manager.js`: `fd` answers to the names `fd` and `fdfind`).
// When one is missing pi DOWNLOADS it from GitHub, and a sandbox with
// `egress: []` cannot, so the tool is listed and every call of it fails with
// "The `find` helper isn't available (fd not present)". Debian's package
// (`fd-find`, binary `fdfind`, a name pi accepts) is too OLD: see the second
// case below.
//
// This reads the Dockerfile, because nothing here can run the image. The
// proof on the real host is one delegate call that uses `find`.

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const DOCKERFILE = readFileSync(join(__dirname, "..", "Dockerfile"), "utf8");

/** The `agent-sandbox` stage, from its FROM to the next one. */
function baseStage(): string {
  const start = DOCKERFILE.indexOf(" AS agent-sandbox\n");
  expect(start).toBeGreaterThan(-1);
  const next = DOCKERFILE.indexOf("\nFROM ", start);

  return DOCKERFILE.slice(start, next === -1 ? undefined : next);
}

/** Every package the stage's `apt-get install` lines name. */
function aptPackages(stage: string): string[] {
  const joined = stage.replace(/\\\n/g, " ");
  const found: string[] = [];
  for (const match of joined.matchAll(/apt-get install -y --no-install-recommends([^&\n]*)/g)) {
    found.push(...(match[1] ?? "").split(/\s+/).filter((word) => word.length > 0));
  }

  return found;
}

describe("the sandbox image holds what pi's built-ins run", () => {
  const stage = baseStage();
  const packages = aptPackages(stage);

  it("ripgrep, for `grep`", () => {
    expect(packages).toContain("ripgrep");
  });

  it("a pinned fd of major 9 or later, for `find`", () => {
    // pi's wrapper passes `--no-require-git`, which fd learned in 9.0.
    // Debian bookworm ships 8.6.0 as `fd-find`: with that one in the image, pi
    // finds it, and every `find` call fails on the flag.
    const version = /ARG FD_VERSION=(\d+)\.(\d+)\.(\d+)/.exec(stage);
    expect(version).not.toBeNull();
    expect(Number(version?.[1])).toBeGreaterThanOrEqual(9);
    expect(packages).not.toContain("fd-find");
  });

  it("checked against a digest before it is installed", () => {
    expect(stage).toMatch(/ARG FD_SHA256=[0-9a-f]{64}\n/);
    expect(stage).toContain("sha256sum -c -");
    expect(stage).toContain("/usr/local/bin/fd");
  });

  it("in the BASE stage, so the python flavor has them too", () => {
    expect(DOCKERFILE).toContain("FROM agent-sandbox AS agent-sandbox-python");
  });
});
