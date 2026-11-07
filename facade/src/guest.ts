// The fixed values of the guest that the facade uses (decision CH62 rules 5
// and 6). `creche_wire::guest` of the Rust wire crate defines each one, and
// `rust/crates/wire/vectors/guest.json` lists them. `test/guest.test.ts`
// holds each copy here equal to that file.

/**
 * The env name that carries the session id to the facade. guest init sets
 * it, and it also marks CH mode (decision CH62 rule 6).
 */
export const SESSION_ID_ENV = "CRECHE_SESSION_ID";

/**
 * The local link: the Unix socket where guest init listens for the live
 * facade (decision CH62 rule 10).
 */
export const LOCAL_LINK_SOCKET = "/run/creche/guest-init.sock";

/**
 * The most bytes of one line on the local link, the LF not counted
 * (decisions CH36 G11, CH62 rule 10). A line of exactly the cap passes.
 */
export const CONTROL_LINE_CAP = 65_536;

/**
 * How long guest init waits for the facade to exit after the stop line,
 * before SIGTERM, in milliseconds (decision CH62 rule 9).
 */
export const FACADE_STOP_WAIT_MS = 25_000;
