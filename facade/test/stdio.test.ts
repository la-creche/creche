// The built facade on stdio, as attendance's link drives it. Each exchange
// comes from the vector file that the attendance tests read too.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import vectors from "../vectors/creche-extensions.json" with { type: "json" };
import { checkValue } from "../src/strict-json.js";
import { startFacade } from "./facade-process.js";
import type { FacadeProcess } from "./facade-process.js";

let running: FacadeProcess | null = null;

afterEach(() => {
  running?.child.kill();
  running = null;
});

function facade(): FacadeProcess {
  running = startFacade();
  return running;
}

describe("creche-facade on stdio", () => {
  it("answers initialize with protocol 1, loadSession and a session list", async () => {
    const proc = facade();
    proc.send(vectors.initialize.request);

    expect(await proc.nextMessage()).toEqual(vectors.initialize.response);
  });

  it("answers _creche/beat with an empty result", async () => {
    const proc = facade();
    proc.send(vectors.initialize.request);
    await proc.nextMessage();
    proc.send(vectors.beat.request);

    expect(await proc.nextMessage()).toEqual(vectors.beat.response);
  });

  it("writes nothing but strict JSON lines under the cap on stdout", async () => {
    const proc = facade();
    proc.send(vectors.initialize.request);
    proc.send("not json");
    proc.send({ jsonrpc: "2.0", id: 7, method: "_no/such", params: {} });
    proc.send({ ...vectors.beat.request, params: { extra: true } });
    proc.send(vectors.beat.request);
    proc.child.stdin.end();

    expect(await proc.exited()).toBe(0);
    const bytes = proc.stdout();
    const lines = bytes.toString("utf8").split("\n");

    expect(bytes.at(-1)).toBe(0x0a);
    expect(lines.pop()).toBe("");
    expect(lines).toHaveLength(5);
    for (const line of lines) {
      const value = JSON.parse(line) as Record<string, unknown>;

      expect(Buffer.byteLength(line)).toBeLessThanOrEqual(vectors.max_line_bytes);
      expect(checkValue(value)).toBeNull();
      expect(value["jsonrpc"]).toBe("2.0");
    }
  });

  it("writes its prose to stderr", async () => {
    const proc = facade();
    proc.child.stdin.end();

    expect(await proc.exited()).toBe(0);
    expect(proc.stdout().length).toBe(0);
    expect(proc.stderr()).toContain("creche-facade: serving ACP v1 on stdio");
  });

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
