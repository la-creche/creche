//! Test helpers for each crate of the creche workspace.
//!
//! No release holds this crate. Add it only under `[dev-dependencies]`.
//!
//! The lint gate holds here as in each other crate: no code of the testkit
//! panics. A function that can fail returns `Result`, and the test calls
//! `unwrap`.

pub mod root;
