//! The runtime that each Rust service of creche shares.
//!
//! Each Python package holds its own copy of the helper code of a service: a
//! file write, a token reader, a signal handler, a client of `attendance`.
//! This crate holds each helper one time. A service crate calls a module here
//! in place of its copy.
//!
//! | Module | What it holds |
//! |---|---|
//! | [`atomic`] | A file write through a temporary file, a sync and a rename. |
//! | [`readfile`] | A file read with a size cap. The read gives each failure as a value and does not panic. |
//! | [`layout`] | The paths under the state root that more than one service uses. |
//! | [`clock`], [`entropy`] | The clock and the random source, as seams that a test replaces. The mint of a ULID and of a random token. |
//! | [`log`] | One line on stderr for each event, and the panic hook. |
//! | [`token`] | A token file into a `Secret`, and the bearer of a request. |
//! | [`faults`] | The fault file of contract 05 §3.3.1, onto the disk. |
//! | [`tasks`], [`signals`] | The stop signal, the tracked tasks, a lock that a panic cannot close, and SIGTERM, SIGINT and SIGHUP. |
//! | [`command`] | A child program with exact words, a time limit and one owner. |
//! | [`http`] | HTTP/1.1 over a Unix socket or TCP: a client, the listeners and the edge layer of a router. |
//! | [`service`], [`args`] | The steps of the start of a program, and a reader for its arguments. |
//!
//! Each edge of a module takes and gives a type of `creche-contracts`: a
//! `Secret`, a `SocketPath`, a `FamilyName`. A function that can fail returns
//! `Result` with the error type of its module.
//!
//! `rust/AGENTS.md`, "The rules for a service", holds the rules for a service
//! crate. `AGENTS.md` of this crate holds the rules for a change here.

pub mod args;
pub mod atomic;
pub mod clock;
pub mod command;
pub mod entropy;
pub mod faults;
pub mod http;
pub mod layout;
pub mod log;
pub mod readfile;
pub mod service;
pub mod signals;
pub mod tasks;
pub mod token;
