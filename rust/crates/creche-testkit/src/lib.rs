//! Test helpers for each crate of the creche workspace.
//!
//! A test of a service must not start a real program, wait for a real clock
//! or call a real service. Each module here gives one stand-in:
//!
//! | Module | What it holds |
//! |---|---|
//! | [`root`] | `TempRoot`: a directory that one test fills. |
//! | [`clock`] | `FixedClock` and `PausedClock`: a clock that the test moves. |
//! | [`entropy`] | `CountingEntropy`: random bytes that the test knows. |
//! | [`runner`] | `FakeRunner`: a `CommandRunner` that starts no program. |
//! | [`program`] | `write_program`: a script that stands in for a child program. |
//! | [`stub`] | `HttpStub` and `RawHttp`: a server and a client that a test scripts byte by byte. |
//! | [`router`] | `call`: one request through a router, with no socket. |
//! | [`vectors`] | A reader of the vector files under `vectors/data`. |
//!
//! No release holds this crate. Add it only under `[dev-dependencies]`.
//!
//! The lint gate holds here as in each other crate: no code of the testkit
//! panics. A function that can fail returns `Result`, and the test calls
//! `unwrap`.
//!
//! `AGENTS.md` of this crate holds the rules for a change here.

pub mod clock;
pub mod entropy;
pub mod program;
pub mod root;
pub mod router;
pub mod runner;
pub mod stub;
pub mod vectors;
