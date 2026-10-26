// The process's own streams. stdout carries ACP lines and nothing else.
// Free text goes to stderr, cut to `MAX_LOG_BYTES`.

import { cutToBytes, MAX_LOG_BYTES } from "./line.js";

const PREFIX = "creche-facade: ";

export type Say = (text: string) => void;

export const say: Say = (text) => {
  process.stderr.write(`${PREFIX}${cutToBytes(text, MAX_LOG_BYTES)}\n`);
};

/** The one writer of stdout. */
export interface Stdout {
  /** Writes one whole line. */
  readonly writeLine: (line: string) => void;
  /** Exits with `code` once every line written before has left the process. */
  readonly exit: (code: number) => void;
}

/**
 * Takes stdout for ACP. After this call, `process.stdout.write` and the
 * `console` methods that print to stdout write to stderr, so the prose of a
 * dependency never reaches the client.
 */
export function claimStdout(): Stdout {
  const write = process.stdout.write.bind(process.stdout);

  process.stdout.write = process.stderr.write.bind(process.stderr);
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;

  return {
    writeLine: (line) => {
      write(line);
    },
    // Writes to one stream finish in order, so the callback of an empty
    // write runs after the last line.
    exit: (code) => {
      write("", () => process.exit(code));
    },
  };
}
