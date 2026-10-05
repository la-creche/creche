//! The wire types and the config types of the creche contracts.
//!
//! Untyped data becomes a value of one of these types through a parsing
//! constructor, and in no other way. Code that holds a value does not check it
//! again. `rust/AGENTS.md` holds the rules for a new type.
//!
//! One module holds one contract. A type that more than one contract uses is
//! in [`ids`] or in [`secret`].

pub mod channel;
pub mod config;
pub mod family;
pub mod grants;
pub mod ids;
pub mod manifest;
pub mod secret;
pub mod server;
pub mod session;
mod slot;
pub mod status;
pub mod untrusted;

#[cfg(test)]
mod vectors;
