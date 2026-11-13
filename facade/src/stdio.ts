// The process's own streams. guest init gives the facade a stdout of
// /dev/null and its own stderr (`rust/crates/guest-init/src/supervisor.rs`).
// Free text goes to stderr, cut to `MAX_LOG_BYTES`.

import { cutToBytes, MAX_LOG_BYTES } from "./line.js";

const PREFIX = "creche-facade: ";

export type Say = (text: string) => void;

export const say: Say = (text) => {
  process.stderr.write(`${PREFIX}${cutToBytes(text, MAX_LOG_BYTES)}\n`);
};

/** Exits with `code` once every line written to stderr before has left the process. */
export type Exit = (code: number) => void;

/**
 * Sends stdout to stderr. After this call, `process.stdout.write` and the
 * `console` methods that print to stdout write to stderr, so the prose of
 * a dependency is not lost in /dev/null. Returns the exit of the process.
 */
export function claimStdout(): Exit {
  const stderr = process.stderr.write.bind(process.stderr);

  process.stdout.write = stderr;
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;

  // Writes to one stream finish in order, so the callback of an empty
  // write runs after the last line.
  return (code) => {
    stderr("", () => process.exit(code));
  };
}
