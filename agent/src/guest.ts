// The fixed values of the guest that the agent uses (decisions CH5e, CH62
// rules 5 and 13). `creche_wire::guest` of the Rust wire crate defines each
// one, and `rust/crates/wire/vectors/guest.json` lists them.
// `test/guest.test.ts` holds each copy here equal to that file.

/**
 * The family-state socket: guest init splices each connection to a new
 * vsock dial of host port 1100 (decision CH62 rule 13).
 */
export const FAMILY_STATE_SOCKET = "/run/creche/family-state.sock";

/** The facade database, on the session disk (decision CH62 rule 5). */
export const FLUE_DB = "/var/lib/creche/session/flue.db";

/**
 * The workspace on the session disk: the cwd of the facade, and the
 * working directory of the granted sandbox tools (decision CH62 rule 5).
 */
export const WORKSPACE = "/var/lib/creche/session/workspace";

/** The guest loopback port of the LiteLLM plane (decision CH6-2). */
export const LITELLM_PORT = 4000;

/** The guest loopback port of the chaperone plane (decision CH6-2). */
export const CHAPERONE_PORT = 8300;
