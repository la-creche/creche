// What the supervisor can say about the sandbox it woke up inside. Every
// answer here goes into `ready` (contract 03 §3), which is the first line of
// the channel and the host's only chance to refuse the sandbox before it uses
// it.
//
// Nothing in this file throws. A fact the supervisor cannot establish is
// reported as unknown, never guessed and never fatal: an image that moved a
// path must not stop a family from serving turns.

import { readdirSync, readFileSync } from "node:fs";
import { totalmem } from "node:os";

import {
  MAX_RESIDENT_CEILING,
  PI_PROCESS_RSS_MB,
  SUPERVISOR_RSS_RESERVE_MB,
} from "./constants.js";

/** Where the image installs pi as one npm project. See `Dockerfile`. */
const PI_PACKAGE_JSON = "/opt/pi/package.json";
const PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent";

/** `ready.pi` when the pin cannot be read. The host logs it; it is not fatal. */
const UNKNOWN_VERSION = "unknown";

const BYTES_PER_MB = 1024 * 1024;

/** A pi rpc process's argv always holds these two words, in this order. */
const RPC_MARKER = "--mode\0rpc";

/**
 * The pi version the image pins, read from the package file the Dockerfile
 * installs. Reading the pin beats running `pi --version`, which would cost a
 * process start on every channel open.
 */
export function piVersion(path: string = PI_PACKAGE_JSON): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return UNKNOWN_VERSION;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return UNKNOWN_VERSION;
  }

  const deps = (parsed as Record<string, unknown>)["dependencies"];
  if (typeof deps !== "object" || deps === null || Array.isArray(deps)) {
    return UNKNOWN_VERSION;
  }

  const pin = (deps as Record<string, unknown>)[PI_PACKAGE_NAME];

  return typeof pin === "string" && pin.length > 0 ? pin : UNKNOWN_VERSION;
}

/**
 * pi rpc processes already running in this VM, which by definition this
 * supervisor did not start.
 *
 * Contract 03 §3 rule 4: more than zero means an earlier channel left orphans.
 * Probe 0a proved that is the normal case after a host-side kill, not a rare
 * one (A5, refuted), so the host needs the count to decide whether to replace
 * the sandbox.
 */
export function countForeignPi(procDir = "/proc"): number {
  let found = 0;
  let entries: string[];
  try {
    entries = readdirSync(procDir);
  } catch {
    // Not Linux. The supervisor only ever runs in the image, so this is a
    // development answer, and 0 is the honest one: nothing was found.
    return 0;
  }

  for (const entry of entries) {
    if (!/^\d+$/.test(entry)) {
      continue;
    }
    if (Number(entry) === process.pid) {
      continue;
    }
    if (hasRpcMarker(`${procDir}/${entry}/cmdline`)) {
      found += 1;
    }
  }

  return found;
}

/** argv in /proc is NUL-separated, so the marker is matched on raw bytes. */
function hasRpcMarker(path: string): boolean {
  try {
    return readFileSync(path, "latin1").includes(RPC_MARKER);
  } catch {
    // The process exited between the listing and the read. Not an orphan.
    return false;
  }
}

/**
 * How many pi processes this supervisor will hold open, whatever the family
 * asks for.
 *
 * Probe 0a measured 155 to 170 MB resident per idle held-open pi process and
 * 62 MB for the supervisor (A7). The reserve is larger than 62 MB on purpose:
 * the VM also runs an init and whatever the turn itself spawns. The host sends
 * the family's own number in `hello` and the lower of the two wins
 * (contract 03 §6 rule 8).
 */
export function residentCeiling(totalBytes: number = totalmem()): number {
  const usableMb = Math.floor(totalBytes / BYTES_PER_MB) - SUPERVISOR_RSS_RESERVE_MB;
  const fits = Math.floor(usableMb / PI_PROCESS_RSS_MB);

  return Math.max(1, Math.min(MAX_RESIDENT_CEILING, fits));
}
