// The planes of the host, and what the model and the `ch` connection take
// of a family. The CH start fixes the planes on guest loopback
// (`src/guest-start.ts`). Each pull of the family state gives the creds and
// the model alias (`src/family-state.ts`).

import type { LiveCreds } from "./family-state.js";
import type { PlaneUrl } from "./planes.js";

/** The two planes of the host: the chaperone and LiteLLM. */
export interface Planes {
  readonly pep: PlaneUrl;
  readonly litellm: PlaneUrl;
}

/**
 * The planes, the creds and the model alias of one family. The alias may
 * carry the `litellm/` prefix.
 */
export interface FamilyConfig {
  readonly planes: Planes;
  readonly creds: LiveCreds;
  readonly modelAlias: string;
}
