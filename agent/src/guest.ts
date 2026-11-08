// The fixed values of the guest that the agent uses (decisions CH5e, CH62
// rules 5 and 13). `creche_wire::guest` of the Rust wire crate defines each
// one, and `rust/crates/wire/vectors/guest.json` lists them.
// `test/guest.test.ts` holds each copy here equal to that file.

/**
 * The family-state socket: guest init splices each connection to a new
 * vsock dial of host port 1100 (decision CH62 rule 13).
 */
export const FAMILY_STATE_SOCKET = "/run/creche/family-state.sock";
