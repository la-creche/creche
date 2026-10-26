// A port of `playpen/src/creds.ts`. `caregiver` replaces `creds.json` with
// `rename()`, so a reader never sees half a write. A reader can still see
// the file before its content crosses the mount boundary, so the read
// retries for a short time. Keep the retry.
//
// A refusal names a field, never a value.

import { readFile } from "node:fs/promises";
import { join } from "node:path";

import { Secret } from "./secret.js";

const CRED_FILE = "creds.json";

/** The cross-mount visibility window, plus margin, and one poll. */
export const CRED_RETRY_MS = 250;
const CRED_POLL_MS = 5;

export type CredField = "epoch" | "litellm_key" | "pep_token";

export interface FamilyCreds {
  readonly epoch: number;
  readonly litellmKey: Secret;
  readonly pepToken: Secret;
}

/** `field` is null when the file is absent or holds no JSON object. */
export type CredsRead =
  | { readonly ok: true; readonly creds: FamilyCreds }
  | { readonly ok: false; readonly field: CredField | null };

function parseCreds(text: string): CredsRead {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, field: null };
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, field: null };
  }

  const raw = parsed as Record<string, unknown>;
  const epoch = raw["epoch"];
  const key = raw["litellm_key"];
  const token = raw["pep_token"];
  if (typeof epoch !== "number" || !Number.isSafeInteger(epoch) || epoch < 0) {
    return { ok: false, field: "epoch" };
  }

  if (typeof key !== "string" || key.length === 0) {
    return { ok: false, field: "litellm_key" };
  }

  if (typeof token !== "string" || token.length === 0) {
    return { ok: false, field: "pep_token" };
  }

  return { ok: true, creds: { epoch, litellmKey: Secret.of(key), pepToken: Secret.of(token) } };
}

async function readOnce(path: string): Promise<CredsRead> {
  try {
    return parseCreds(await readFile(path, "utf8"));
  } catch {
    return { ok: false, field: null };
  }
}

/** The last refusal when no valid file arrived inside `retryMs`. */
export async function readCreds(dir: string, retryMs: number): Promise<CredsRead> {
  const path = join(dir, CRED_FILE);
  const deadline = Date.now() + retryMs;

  for (;;) {
    const read = await readOnce(path);
    if (read.ok || Date.now() >= deadline) {
      return read;
    }

    await new Promise((resolve) => setTimeout(resolve, CRED_POLL_MS));
  }
}
