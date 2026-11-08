// The start of the agent under CH (decisions CH5a, CH5e, CH62 rules 5 and
// 13, CH80). The facade reads no `AGENT_*` variable in CH mode, and a set
// one has no effect (decision CH81 rule 5). Each one gets a source of the
// guest:
//
//   AGENT_SESSIONS_DIR      ──► FLUE_DB and WORKSPACE, on the session disk
//   AGENT_CRED_DIR          ──► the start pull on the family-state socket
//   AGENT_FAMILY_CONFIG_DIR ──► the start pull on the family-state socket
//   AGENT_PEP_URL           ──► http://127.0.0.1:8300, spliced to the host
//   AGENT_LITELLM_BASE_URL  ──► http://127.0.0.1:4000, spliced to the host
//   AGENT_CONTROL_DIR       ──► none: no lock under CH (decisions CH5d, CH19)
//   AGENT_SANDBOX           ──► none: the lock alone named the sandbox. The
//                               session id is `CRECHE_SESSION_ID`
//   AGENT_INDEX_ROOT        ──► none: no VM reads the index (decision CH4d)
//
// The start pulls until the vmm gives a state. A typed fault and a link
// with no reply each write one stderr line, then the start pulls again
// after a pause that doubles up to its cap (decision CH81 rule 4). It
// never exits on a fault, so guest init never restarts it in a loop
// (decision CH23 rule 2).

import type { AgentStart } from "./agent.js";
import type { Planes } from "./config.js";
import { liveStateOf, pullWire } from "./family-state.js";
import type { Say } from "./family-state.js";
import { CHAPERONE_PORT, FAMILY_STATE_SOCKET, FLUE_DB, LITELLM_PORT, WORKSPACE } from "./guest.js";
import { PlaneUrl } from "./planes.js";
import { readToolEnv } from "./tool-env.js";

/** The pauses of the start pull: the first one, and the cap of the doubling. */
export interface StartPauses {
  readonly firstMs: number;
  readonly capMs: number;
}

/** 250 ms, then 500 ms, and so on up to 5 s (decision CH81 rule 4). */
export const START_PAUSES: StartPauses = { firstMs: 250, capMs: 5000 };

/** A plane on guest loopback. The ports are constants, so a port that gives no plane URL is a build fault, and the load throws. */
function loopbackPlane(port: number): PlaneUrl {
  const parsed = PlaneUrl.parse(`http://127.0.0.1:${String(port)}`);
  if (!parsed.ok) {
    throw new Error(`the guest plane on port ${String(port)} is not a plane URL (${parsed.fault})`);
  }

  return parsed.url;
}

/** The guest planes: each one on guest loopback, which guest init splices to the host (decision CH6-2). */
const GUEST_PLANES: Planes = { pep: loopbackPlane(CHAPERONE_PORT), litellm: loopbackPlane(LITELLM_PORT) };

/**
 * The CH start: the fixed guest paths and planes, the tool env of `env`,
 * and the state of the first pull on `socket` that gives one. A skill gets
 * no supporting files: the state carries only the text of each `SKILL.md`.
 */
export async function readGuestStart(
  env: NodeJS.ProcessEnv,
  say: Say,
  socket: string = FAMILY_STATE_SOCKET,
  pauses: StartPauses = START_PAUSES,
): Promise<AgentStart> {
  for (let pause = pauses.firstMs; ; pause = Math.min(pause * 2, pauses.capMs)) {
    const pulled = await pullWire(socket, say);
    if (pulled.ok) {
      const state = liveStateOf(pulled.state, []);
      const kept = new Set(state.config.skills.map((skill) => skill.name));
      const skippedSkills = Object.keys(pulled.state.config.skills).filter((name) => !kept.has(name));

      return { store: FLUE_DB, workspace: WORKSPACE, planes: GUEST_PLANES, indexRoot: null, toolEnv: readToolEnv(env), state, skippedSkills };
    }

    say(`the start pull of the family state failed (${pulled.fault}); the next pull is in ${String(pause)} ms`);
    await new Promise((resolve) => setTimeout(resolve, pause));
  }
}
