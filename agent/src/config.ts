// The edge of the agent. The environment and the mounts are read one time,
// at process start, into one `FamilyConfig`. Every fault is a typed
// refusal, never an exception, and no refusal holds a value of the input.
//
// CONTRACT-QUESTION: contract 03 §7 gives the two plane URLs as fixed values.
// The playpen takes the URL of its build for an absent or bad variable. The
// agent has no build URL, so it refuses. A fallback costs a build constant.

import { CRED_RETRY_MS, readCreds } from "./creds.js";
import type { CredField, FamilyCreds } from "./creds.js";
import { readMountDirs } from "./mounts.js";
import type { MountDirs } from "./mounts.js";
import { PlaneUrl } from "./planes.js";
import type { PlaneUrlFault } from "./planes.js";
import { readRuntimeConfig } from "./runtime-config.js";
import type { RuntimeConfig } from "./runtime-config.js";

const SANDBOX_ENV = "AGENT_SANDBOX";
const PLANE_ENV = { pep: "AGENT_PEP_URL", litellm: "AGENT_LITELLM_BASE_URL" } as const;

/** Contract 03 §3: a family name, `-s`, and a number. */
const SANDBOX_ID_RE = /^[a-z][a-z0-9-]{1,30}-s[0-9]+$/;

export interface FamilyConfig {
  readonly sandbox: string;
  readonly mounts: MountDirs;
  readonly planes: { readonly pep: PlaneUrl; readonly litellm: PlaneUrl };
  readonly creds: FamilyCreds;
  readonly runtime: RuntimeConfig;
}

export type ConfigRefusal =
  | { readonly reason: "mount_dir_unset"; readonly variables: readonly string[] }
  | { readonly reason: "sandbox_unset" }
  | { readonly reason: "sandbox_not_valid" }
  | { readonly reason: "plane_unset"; readonly variable: string }
  | { readonly reason: "plane_not_valid"; readonly variable: string; readonly fault: PlaneUrlFault }
  | { readonly reason: "creds_not_valid"; readonly field: CredField | null };

export type ConfigRead =
  | { readonly ok: true; readonly config: FamilyConfig }
  | { readonly ok: false; readonly refusal: ConfigRefusal };

type PlaneRead =
  | { readonly ok: true; readonly url: PlaneUrl }
  | { readonly ok: false; readonly refusal: ConfigRefusal };

function readPlane(env: NodeJS.ProcessEnv, variable: string): PlaneRead {
  const value = env[variable] ?? "";
  if (value.length === 0) {
    return { ok: false, refusal: { reason: "plane_unset", variable } };
  }

  const parsed = PlaneUrl.parse(value);

  return parsed.ok
    ? parsed
    : { ok: false, refusal: { reason: "plane_not_valid", variable, fault: parsed.fault } };
}

export async function readFamilyConfig(
  env: NodeJS.ProcessEnv,
  credRetryMs: number = CRED_RETRY_MS,
): Promise<ConfigRead> {
  const mounts = readMountDirs(env);
  if (!mounts.ok) {
    return { ok: false, refusal: { reason: "mount_dir_unset", variables: mounts.unset } };
  }

  const sandbox = env[SANDBOX_ENV] ?? "";
  if (sandbox.length === 0) {
    return { ok: false, refusal: { reason: "sandbox_unset" } };
  }

  if (!SANDBOX_ID_RE.test(sandbox)) {
    return { ok: false, refusal: { reason: "sandbox_not_valid" } };
  }

  const pep = readPlane(env, PLANE_ENV.pep);
  const litellm = readPlane(env, PLANE_ENV.litellm);
  if (!pep.ok) {
    return pep;
  }

  if (!litellm.ok) {
    return litellm;
  }

  const creds = await readCreds(mounts.dirs.creds, credRetryMs);
  if (!creds.ok) {
    return { ok: false, refusal: { reason: "creds_not_valid", field: creds.field } };
  }

  return {
    ok: true,
    config: {
      sandbox,
      mounts: mounts.dirs,
      planes: { pep: pep.url, litellm: litellm.url },
      creds: creds.creds,
      runtime: readRuntimeConfig(mounts.dirs.config),
    },
  };
}
