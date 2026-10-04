//! The wire types and the config types of the creche contracts.
//!
//! Untyped data becomes a value of one of these types through a parsing
//! constructor, and in no other way. Code that holds a value does not check it
//! again. `rust/AGENTS.md` holds the rules for a new type.

pub mod ids;
pub mod secret;

#[cfg(test)]
mod vectors;
