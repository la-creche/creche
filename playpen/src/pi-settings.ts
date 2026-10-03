// pi's own settings document, written into `PI_CODING_AGENT_DIR` before every
// pi process starts. It carries one setting and exists for one reason:
//
//   <session dir>/settings.json   {"defaultTools": [<the granted built-ins>]}
//
// `--exclude-tools` only REMOVES. pi starts with four of its eight built-ins
// active — `read`, `bash`, `edit`, `write` (0.99.1 `dist/core/settings-manager.js:35`) —
// so a family granted `grep`, `find` or `ls` was never given them. Nothing on
// pi's command line turns a built-in on: `defaultTools` is the only lever, and
// it lives in settings.json (0.99.1 `docs/settings.md`, "Tools"). It selects
// the built-ins and leaves extension tools alone, which is exactly what
// `--tools` and `--no-tools` fail to do.
//
// **This file is rewritten at EVERY pi start and its content is never read
// back.** `PI_CODING_AGENT_DIR` is the session store: agent-writable, and it
// outlives the sandbox (§7.1). pi reads settings.json from there as its GLOBAL
// settings file, and no project-trust decision gates a global file, so a
// session that left one behind would otherwise choose `packages` (pi installs
// missing npm and git packages at session start, before `--no-extensions`
// filters anything), `extensions`, `npmCommand`, `shellPath`,
// `shellCommandPrefix`, `httpProxy`, `sessionDir` and `defaultProjectTrust`
// for every later session of that family. A whole-file replacement is the
// guarantee; a merge would not be one.
//
// `sandbox_tools` itself is not part of the perimeter (contract 01 §3.8
// rule 3). A settings file that runs code at session start would be, which is
// why this writer exists at all and why it never merges.

import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { CODEMODE_TOOL, SETTINGS_FILE } from "./constants.js";
import { grantedBuiltins, grantsCodemode } from "./runtime-config.js";
import type { RuntimeConfig } from "./runtime-config.js";

/** Everything the playpen states about pi's settings, for one family. */
export interface PiSettings {
  /**
   * The built-ins active from startup, and `codemode` when granted. pi
   * registers codemode inactive, so this list is what switches it on. Other
   * extension tools are not affected.
   */
  readonly defaultTools: readonly string[];
}

/** Contract 01 §3.8 as pi's own setting. Nothing else is stated. */
export function piSettings(config: RuntimeConfig): PiSettings {
  const codemode = grantsCodemode(config) ? [CODEMODE_TOOL] : [];

  return { defaultTools: [...grantedBuiltins(config), ...codemode] };
}

/**
 * Writes the document into `sessionDir`, replacing whatever was there.
 *
 * Temp file plus `rename` for the reason `models-json.ts` uses it: a reader
 * must never see half a document. The rename also
 * makes the replacement atomic against a session that is writing its own copy.
 */
export function writePiSettings(sessionDir: string, config: RuntimeConfig): void {
  mkdirSync(sessionDir, { recursive: true });

  const path = join(sessionDir, SETTINGS_FILE);
  const temp = `${path}.${process.pid}.tmp`;
  writeFileSync(temp, `${JSON.stringify(piSettings(config), null, 2)}\n`, { mode: 0o644 });
  renameSync(temp, path);
}
