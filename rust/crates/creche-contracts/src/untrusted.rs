//! Readers for an answer of another service: a field of a wrong type reads as
//! empty.
//!
//! A client of a service of this platform reads the answer into a raw `serde`
//! type. Each field of that type names one function of this module:
//!
//! ```no_run
//! use creche_contracts::untrusted;
//! use serde::Deserialize;
//!
//! #[derive(Deserialize)]
//! struct RawTurn {
//!     #[serde(default, deserialize_with = "untrusted::text")]
//!     state: String,
//!     #[serde(default, deserialize_with = "untrusted::int")]
//!     turn_seq: i64,
//!     #[serde(default, deserialize_with = "untrusted::list_first::<8, _, _>")]
//!     notes: Vec<String>,
//! }
//!
//! let turn: RawTurn = untrusted::parse_object(br#"{"state": 7, "turn_seq": "x"}"#)?;
//! assert_eq!(turn.state, "");
//! assert_eq!(turn.turn_seq, 0);
//! # Ok::<(), untrusted::NotAnObject>(())
//! ```
//!
//! The Python clients read an answer in this way. Each one holds a copy of
//! the same helpers: `untrusted.py` of the three doors, and
//! `noticeboard/src/noticeboard/jsonfiles.py`. A wrong type in one field does
//! not refuse the answer. The conversion to the valid type then decides what
//! an empty field means.
//!
//! No function here returns a `serde_json::Value` (`rust/AGENTS.md`, rule 7).
//!
//! These readers are for an answer of a service. A file that a lax Python
//! reader reads has a view in the module of its contract (`rust/AGENTS.md`,
//! "A reader that accepts more than the contract").
//!
//! Each body here is a stub. `rust/crates/creche-runtime/AGENTS.md` lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::fmt;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer};

/// A text. A value that is not a JSON string reads as the empty text.
///
/// Use it with `#[serde(default, deserialize_with = "untrusted::text")]`. The
/// `default` gives the empty text for a field that is absent.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn text<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    todo!()
}

/// A whole number. A value that is not a JSON integer reads as 0.
///
/// `true` and `false` are not integers here, and each one reads as 0. In
/// Python a `bool` is an `int`, so each Python copy excludes it by name.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn int<'de, D>(deserializer: D) -> Result<i64, D::Error>
where
    D: Deserializer<'de>,
{
    todo!()
}

/// A number: an integer or a float. A value of another type, `true` and
/// `false` read as `None`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn number<'de, D>(deserializer: D) -> Result<Option<f64>, D::Error>
where
    D: Deserializer<'de>,
{
    todo!()
}

/// A switch. It is on only for the JSON value `true`.
///
/// The number 1 and the text `"true"` read as off.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn flag<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    todo!()
}

/// An object of the raw type `T`. A value that is absent, `null` or not an
/// object reads as the default of `T`.
///
/// Use [`block`] when the reader must tell an absent object from an empty
/// one.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn object<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    todo!()
}

/// An object of the raw type `T`, or `None`. A value that is absent, `null`
/// or not an object reads as `None`.
///
/// `None` means that the writer did not publish the block. `Some` with empty
/// fields means that the writer published an empty block. [`object`] reads
/// the two cases as one.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn block<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    todo!()
}

/// A list. A value that is not a JSON array reads as the empty list. The
/// function drops each member that is not a `T` and keeps the others in
/// order.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn list<'de, D, T>(deserializer: D) -> Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    todo!()
}

/// A list of the first `N` members. The function takes the first `N` members
/// of the array. Then it drops each of them that is not a `T`.
///
/// The order of the two steps is the order of the Python reader. The result
/// can thus hold less than `N` members when the array holds more.
///
/// Write the count in the path: `untrusted::list_first::<8, _, _>`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn list_first<'de, const N: usize, D, T>(deserializer: D) -> Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    todo!()
}

/// Why bytes are not the JSON object of an answer.
///
/// No variant holds a byte of the answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotAnObject {
    /// The bytes are not one JSON text.
    NotJson,
    /// The JSON text is a value that is not an object: a list, a number, a
    /// text, a switch or `null`.
    NotObject,
}

impl fmt::Display for NotAnObject {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotJson => f.write_str("the answer is not JSON"),
            Self::NotObject => f.write_str("the answer is not a JSON object"),
        }
    }
}

impl Error for NotAnObject {}

/// The raw type `T` of an answer, from the bytes of its body.
///
/// The body must be one JSON object. Each field of `T` then reads through a
/// function of this module, so a field of a wrong type does not refuse the
/// answer.
///
/// # Errors
///
/// [`NotAnObject::NotJson`] for bytes that are not one JSON text, and
/// [`NotAnObject::NotObject`] for a JSON value that is not an object.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-untrusted writes this body"
)]
pub fn parse_object<T: DeserializeOwned>(bytes: &[u8]) -> Result<T, NotAnObject> {
    todo!()
}

/// The first `max_chars` characters of `text`. The function counts code
/// points, as a Python slice does, and never cuts inside one.
///
/// A reader of a page gives each text a cap, so one field cannot fill the
/// page. The Python origin is the slice `value[:limit]` of
/// `noticeboard/src/noticeboard/jsonfiles.py:101` and `:181`, and of
/// `chaperone/src/chaperone/delegate.py:232`.
///
/// ```
/// use creche_contracts::untrusted::cut;
///
/// assert_eq!(cut("family", 3), "fam");
/// assert_eq!(cut("family", 6), "family");
/// assert_eq!(cut("d\u{e9}j\u{e0} vu", 4), "d\u{e9}j\u{e0}");
/// assert_eq!(cut("family", 0), "");
/// ```
#[must_use]
pub fn cut(text: &str, max_chars: usize) -> &str {
    let Some((end, _)) = text.char_indices().nth(max_chars) else {
        return text;
    };

    // `end` is the first byte of a character, so the cut is always there.
    // The empty text keeps the cap when it is not.
    text.get(..end).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refusal_names_its_reason_and_no_byte_of_the_answer() {
        assert_eq!(NotAnObject::NotJson.to_string(), "the answer is not JSON");
        assert_eq!(
            NotAnObject::NotObject.to_string(),
            "the answer is not a JSON object"
        );
    }

    // --- cut ---

    #[test]
    fn a_cut_keeps_the_first_characters_and_counts_code_points() {
        let cuts = [
            ("", 0, ""),
            ("", 3, ""),
            ("family", 0, ""),
            ("family", 1, "f"),
            ("family", 5, "famil"),
            ("family", 6, "family"),
            ("family", 7, "family"),
            ("family", usize::MAX, "family"),
            // One character of two bytes, of three bytes and of four bytes.
            ("\u{e9}\u{20ac}\u{1f600}", 1, "\u{e9}"),
            ("\u{e9}\u{20ac}\u{1f600}", 2, "\u{e9}\u{20ac}"),
            ("\u{e9}\u{20ac}\u{1f600}", 3, "\u{e9}\u{20ac}\u{1f600}"),
            // A mark that combines is a code point of its own, as in Python.
            ("e\u{301}a", 1, "e"),
            ("e\u{301}a", 2, "e\u{301}"),
        ];

        for (text, max_chars, kept) in cuts {
            assert_eq!(cut(text, max_chars), kept, "{text:?} at {max_chars}");
        }
    }

    #[test]
    fn a_cut_of_a_long_text_has_the_cap_as_its_count_of_characters() {
        let long = "\u{1f600}".repeat(600);
        let kept = cut(&long, 500);

        assert_eq!(kept.chars().count(), 500);
        assert!(long.starts_with(kept));
    }
}
