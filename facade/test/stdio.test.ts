// The process's own streams. guest init gives the facade a stdout of
// /dev/null, so `claimStdout` sends the prose of a dependency to stderr.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

describe("claimStdout", () => {
  it("sends the prose of a dependency to stderr", () => {
    const stdio = fileURLToPath(new URL("../dist/src/stdio.js", import.meta.url));
    const script = [
      `const { claimStdout } = await import(${JSON.stringify(stdio)});`,
      "claimStdout();",
      "console.log('log'); console.info('info'); console.debug('debug');",
      "process.stdout.write('write\\n');",
    ].join("\n");
    const run = spawnSync(process.execPath, ["--input-type=module", "-e", script], { encoding: "utf8" });

    expect(run.status).toBe(0);
    expect(run.stdout).toBe("");
    expect(run.stderr).toBe("log\ninfo\ndebug\nwrite\n");
  });
});
