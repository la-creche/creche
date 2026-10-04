//! The runtime that each Rust service of creche shares.
//!
//! Each Python package holds its own copy of the helper code of a service.
//! This crate holds each helper one time. A service crate calls a module here
//! in place of its copy.
//!
//! `rust/AGENTS.md`, "The rules for a service", holds the rules for a service
//! crate.

pub mod atomic;
pub mod clock;
pub mod command;
pub mod entropy;
pub mod faults;
pub mod layout;
pub mod log;
pub mod readfile;
pub mod signals;
pub mod tasks;
pub mod token;
