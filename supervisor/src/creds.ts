// Contract 03 §12. The family's LiteLLM key and PEP token reach pi through a
// read-only mount and the child's environment. They never touch argv, a log
// line, or the channel itself (invariant 13).
//
// The file is replaced by `rename()`, which is atomic, so a reader never sees
// half a write. What a reader CAN see is the file before its content crosses
// the mount boundary. Probe 0a measured that
// window at 1 to 4 ms over 200 of 200 trials — on an rw mount. The mount that
// actually carries this file is read-only and untested, so the retry stays.

import { readFile } from "node:fs/promises";
import { join } from "node:path";

import { CRED_FILE, CRED_POLL_MS, CRED_RETRY_MS } from "./constants.js";

export interface FamilyCreds {
  readonly epoch: number;
  readonly litellm_key: string;
  readonly pep_token: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Shape only. A credential's value is never inspected and never logged. */
function readCredsFile(text: string): FamilyCreds | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }

  const raw = parsed as Record<string, unknown>;
  const epoch = raw["epoch"];
  const key = raw["litellm_key"];
  const token = raw["pep_token"];
  if (typeof epoch !== "number" || !Number.isInteger(epoch)) {
    return null;
  }
  if (typeof key !== "string" || typeof token !== "string") {
    return null;
  }

  return { epoch, litellm_key: key, pep_token: token };
}

/**
 * Reads the credential mount fresh on every pi process start.
 *
 * There is no cache on purpose. A rotation is a new file with a higher epoch
 * and nothing announces it but the next turn's `env_epoch`, so re-reading is
 * both the cheapest correct answer and the one that cannot go stale. A process
 * start already costs about 1.2 s (probe 0a, A2); one small read is noise.
 */
export class CredReader {
  private readonly path: string;

  public constructor(dir: string, private readonly retryMs: number = CRED_RETRY_MS) {
    this.path = join(dir, CRED_FILE);
  }

  /** Returns null when no readable file reached `minEpoch` inside the retry. */
  public async read(minEpoch: number): Promise<FamilyCreds | null> {
    const deadline = Date.now() + this.retryMs;

    for (;;) {
      const creds = await this.readOnce();
      if (creds !== null && creds.epoch >= minEpoch) {
        return creds;
      }
      if (Date.now() >= deadline) {
        return null;
      }

      await sleep(CRED_POLL_MS);
    }
  }

  private async readOnce(): Promise<FamilyCreds | null> {
    try {
      return readCredsFile(await readFile(this.path, "utf8"));
    } catch {
      // Not there yet, or not readable yet. Both mean "retry", not "fail".
      return null;
    }
  }
}
