//! The core of the release signer `ci-signer`
//! (`docs/design/forgejo-release-path.md`, section 4).
//!
//! The crate holds the decisions and the data formats of the signer. It
//! does no I/O. A later PR adds the program around it: the socket listener,
//! the Forgejo client, the cosign call and the tag creation.
//!
//! ```text
//!   open line ──► RunKey ─┐
//!                         ▼
//!   GET runs/{id} ──► decide_run ──► RunChecked
//!                                       │
//!   GET commits/{sha}/pull ──► decide_merge ──► Eligible
//!                                                  │
//!   request line ──► Request::parse ──► Request    │
//!                                          │       │
//!                                          ▼       ▼
//!                               IssuedRuns::issue ──► Signable
//!                                                        │
//!                                    cosign (later PR) ──► Bundle
//!                                                        │
//!                                  TagMessage::new ◄─────┘
//!                                        │
//!                    tag message, Answer::Signed line
//! ```
//!
//! Only `IssuedRuns::issue` builds a `Statement` for signing, and only from
//! an `Eligible` value. Root reads a statement back with
//! `TagMessage::decode` and, after cosign passes, `Statement::from_canonical`.

mod base64;
pub mod eligibility;
pub mod exchange;
pub mod forgejo;
pub mod issue;
pub mod limits;
pub mod message;
pub mod names;
pub mod pairs;
#[cfg(test)]
mod schema;
pub mod signed_at;
pub mod statement;

pub use base64::{Base64Error, decode as base64_decode, encode as base64_encode};
