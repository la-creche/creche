// The fixed values of the guest that the facade uses (decision CH62 rules 5
// and 6). `creche_wire::guest` of the Rust wire crate defines each one, and
// `rust/crates/wire/vectors/guest.json` lists them. `test/guest.test.ts`
// holds each copy here equal to that file.

/**
 * The env name that carries the session id to the facade. guest init sets
 * it, and it also marks CH mode (decision CH62 rule 6).
 */
export const SESSION_ID_ENV = "CRECHE_SESSION_ID";
