//! The status document of a family, and each view that a reader takes of it
//! (contract 05).
//!
//! `caregiver` writes one `status.json` for each family, and five programs
//! read it. This module is the one definition of that file.
//!
//! | Module | What it holds |
//! |---|---|
//! | [`raw`] | [`raw::RawStatus`]: the document as a file holds it. It checks no field. |
//! | [`document`] | [`document::StatusDocument`]: the valid document, and the bytes that `caregiver` writes. |
//! | [`views`] | One view for each of the five readers: what that reader takes from a document. The staleness rule. |
//! | [`words`] | Each closed vocabulary of the contract, as an enum. |
//! | [`fault_file`] | The fault file of `attendance` and of the chaperone, and the view of `caregiver`. |
//! | [`outcome`] | The outcome record, as the noticeboard reads it. |
//! | [`json`] | The JSON reader and the JSON writer that give the texts of the Python components. |
//!
//! A reader goes from bytes to a view:
//!
//! ```
//! use creche_contracts::status::views;
//!
//! let bytes = br#"{"kind": "attended", "state": "in_sync"}"#;
//! assert!(views::read_door_owui(bytes).is_ok());
//! assert!(views::read_door_trigger(bytes).is_err());
//! ```
//!
//! A writer goes from valid parts to bytes: [`document::StatusDocument::new`],
//! then [`document::StatusDocument::encode`].
//!
//! Each time of the contract is a [`crate::time::Timestamp`]. This module
//! has no time type of its own.

pub mod document;
pub mod fault_file;
pub mod json;
pub mod outcome;
pub mod raw;
pub mod views;
pub mod words;

/// The older path of two items. It holds no item of its own.
///
/// New code names [`crate::time::Timestamp`] and [`views::Freshness`]. Code
/// outside this module still names the two items through this path. Packet
/// `decisions-runtime-time` changes that code and deletes this module.
pub mod time {
    pub use super::views::Freshness;
    pub use crate::time::Timestamp;
}

#[cfg(test)]
mod python;
