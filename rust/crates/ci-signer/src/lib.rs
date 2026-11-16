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
//! Only `IssuedRuns::issue` builds a `Signable`, and only from an
//! `Eligible` value. Only a `Signable` makes a `TagMessage`, and only a
//! `TagMessage` makes `Answer::Signed`. Root reads a tag message back with
//! `UnverifiedTagMessage::decode` and, after cosign passes,
//! `Statement::from_canonical`.
//!
//! # What does not compile
//!
//! A statement that a caller parsed cannot become the bytes to sign:
//!
//! ```compile_fail
//! use creche_ci_signer::message::{Bundle, TagMessage};
//! use creche_ci_signer::statement::Statement;
//!
//! fn forge(statement: Statement, bundle: Bundle) -> TagMessage {
//!     TagMessage::new(statement.signable(), bundle)
//! }
//! ```
//!
//! A statement has no serde reader. `Statement::from_canonical` is the one
//! reader, with its byte cap and its canonical check:
//!
//! ```compile_fail
//! use creche_ci_signer::statement::Statement;
//!
//! fn read(text: &str) -> Statement {
//!     serde_json::from_str::<Statement>(text).unwrap()
//! }
//! ```
//!
//! A decoded tag message cannot become a signed answer:
//!
//! ```compile_fail
//! use creche_ci_signer::exchange::Answer;
//! use creche_ci_signer::message::{MessageSource, TagMessage};
//!
//! fn forge(text: &str) -> Answer {
//!     Answer::Signed(TagMessage::decode(text, MessageSource::TagsApi).unwrap())
//! }
//! ```
//!
//! ```compile_fail
//! use creche_ci_signer::exchange::Answer;
//! use creche_ci_signer::message::{MessageSource, UnverifiedTagMessage};
//!
//! fn forge(text: &str) -> Answer {
//!     Answer::Signed(UnverifiedTagMessage::decode(text, MessageSource::TagsApi).unwrap())
//! }
//! ```
//!
//! The program holds one `IssuedRuns`. A copy would issue a second
//! statement for a run:
//!
//! ```compile_fail
//! use creche_ci_signer::issue::IssuedRuns;
//!
//! fn copy(issued: &IssuedRuns) -> IssuedRuns {
//!     issued.clone()
//! }
//! ```
//!
//! A caller cannot build or alter an `IssuedLine`. So a journal line always
//! names the run and the commit of its statement:
//!
//! ```compile_fail
//! use creche_ci_signer::issue::Issued;
//! use creche_ci_signer::names::RunId;
//!
//! fn alter(issued: &mut Issued, run: RunId) {
//!     issued.line.run = run;
//! }
//! ```
//!
//! One decision feeds one issue:
//!
//! ```compile_fail
//! use creche_ci_signer::eligibility::Eligible;
//!
//! fn twice(eligible: Eligible) -> (Eligible, Eligible) {
//!     (eligible.clone(), eligible)
//! }
//! ```

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
