// Drives the REAL PEP bridge bundle against the REAL PEP, and keeps it
// running while the grant file moves underneath it (contract 04 §4.2).
//
//   node grant_refresh_driver.mjs <pep-bridge.js> <report.json> <pace ms> <polls>
//
// `bridge_driver.mjs` beside this one starts the bridge, reports once and
// exits, which is every test about ONE call. This one is about the SECOND
// manifest: it stays alive, writes a fresh report line every time the tool
// set changes, and lets the Python side rewrite the grant file in between.
//
//   start ──► report #0: the tools the first manifest gave
//      │
//      │   (Python rewrites the grant file; the PEP re-reads it per call)
//      │
//      └──► report #1: what the poll made of the new revision
//
// The pace is the test's, not production's: the bridge's second argument is
// exactly this seam (`bridge/index.ts`). Nothing here fakes the PEP, the
// grant file or the bridge. This file never writes to stdout, because the
// report file is what the Python side reads.

import { writeFileSync, renameSync } from "node:fs";

const [bundlePath, reportPath, paceArg, pollsArg] = process.argv.slice(2);

if (bundlePath === undefined || reportPath === undefined || paceArg === undefined) {
  process.stderr.write("usage: grant_refresh_driver.mjs <bundle> <report> <paceMs> [polls]\n");
  process.exit(2);
}

const paceMs = Number(paceArg);
const polls = pollsArg === undefined ? 40 : Number(pollsArg);

/** `PiExposure.Hidden` of `playpen/bridge/pi-api.ts`, as pi spells it. */
const HIDDEN = "hidden";

/**
 * pi 0.99.1's three tool-set methods, with pi's own semantics.
 *
 * `playpen/test/fake-pep.ts` holds the same three and
 * `playpen/test/real-pi.test.ts` measures them against the real pi. They
 * are restated here because a driver cannot import a TypeScript test helper,
 * and getting one wrong would make a wrong bridge look right.
 *
 * `registerTool` follows `FakePi.registerTool` of that file. A registration
 * as `hidden` removes the name from the active list, and the definition
 * stays. A registration with another exposure adds the name when the name
 * is new, or when its last registration was `hidden`.
 */
class ToolSetPi {
  constructor() {
    this.byName = new Map();
    this.active = [];
  }

  registerTool(tool) {
    const before = this.byName.get(tool.name);
    this.byName.set(tool.name, tool);

    if (tool.exposure === HIDDEN) {
      this.active = this.active.filter((name) => name !== tool.name);
      return;
    }

    if (before === undefined || before.exposure === HIDDEN) {
      this.active.push(tool.name);
    }
  }

  getActiveTools() {
    return [...this.active];
  }

  setActiveTools(names) {
    this.active = names.filter((name) => this.byName.has(name));
  }

  describe() {
    return this.active.map((name) => ({
      name,
      description: this.byName.get(name)?.description ?? "",
    }));
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Temp file and rename: the Python side polls for this path and reads it. */
function publish(path, data) {
  writeFileSync(`${path}.tmp`, `${JSON.stringify(data, null, 2)}\n`);
  renameSync(`${path}.tmp`, path);
}

async function main() {
  const bridge = (await import(bundlePath)).default;
  const pi = new ToolSetPi();

  await bridge(pi, { retryMs: paceMs, watchMs: paceMs });

  const reports = [{ active: pi.describe() }];
  publish(reportPath, reports);

  // One sample per pace, so a change made by the Python side lands in a new
  // report within two polls. The loop ends on its own count, never on a
  // signal, so a hung test cannot leave this process behind.
  let last = JSON.stringify(pi.describe());
  for (let seen = 0; seen < polls; seen += 1) {
    await sleep(paceMs);
    const now = JSON.stringify(pi.describe());
    if (now === last) {
      continue;
    }

    last = now;
    reports.push({ active: pi.describe() });
    publish(reportPath, reports);
  }
}

main().catch((error) => {
  process.stderr.write(`grant_refresh_driver: ${error?.stack ?? String(error)}\n`);
  process.exit(1);
});
