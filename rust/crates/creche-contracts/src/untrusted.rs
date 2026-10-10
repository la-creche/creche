//! Readers for an answer of another service: a field of a wrong type reads as
//! empty.
//!
//! A client of a service of this platform reads the answer into a raw `serde`
//! type. Each field of that type names one function of this module:
//!
//! ```
//! use creche_contracts::{json, untrusted};
//! use serde::Deserialize;
//!
//! #[derive(Deserialize)]
//! struct RawTurn {
//!     #[serde(default, deserialize_with = "untrusted::text")]
//!     state: String,
//!     #[serde(default = "untrusted::zero", deserialize_with = "untrusted::int")]
//!     turn_seq: json::Integer,
//!     #[serde(default, deserialize_with = "untrusted::list_first::<8, _, _>")]
//!     notes: Vec<String>,
//! }
//!
//! let turn: RawTurn = untrusted::parse_object(br#"{"state": 7, "turn_seq": "x"}"#)?;
//! assert_eq!(turn.state, "");
//! assert_eq!(turn.turn_seq, untrusted::zero());
//! assert!(turn.notes.is_empty());
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
//! # How a function reads a value
//!
//! Each function reads the whole value first, into a private tree of the JSON
//! kinds. Then it takes the content when the kind is right. The steps are in
//! that order for two reasons:
//!
//! 1. A value of a wrong kind has no error. The only error of a function is
//!    the error of the deserializer, for a text that is not JSON or that nests
//!    too deep.
//! 2. [`object`], [`block`], [`list`] and [`list_first`] give the tree to the
//!    raw type `T`. An object or a member that `T` refuses is then empty or
//!    dropped, and the answer stays.
//!
//! A raw type `T` below a function of this module reads from the tree. It
//! reads there as it reads from `serde_json` itself, with these differences:
//!
//! - A key that an object holds two times has the value of its last
//!   occurrence, as a Python `dict` has. A derived `serde` type alone refuses
//!   such an object.
//! - A struct with named fields reads from an object only. A derived `serde`
//!   type alone also takes a list and fills its fields by position. No Python
//!   reader does that. The rule has one exception: a struct that a variant of
//!   an enum with `#[serde(untagged)]` holds. `serde` reads such an enum from
//!   a buffer of its own, and the struct there also reads from a list.
//! - A value nests 128 levels of lists and objects at most. `serde_json`
//!   stops one level before, so [`parse_object`] reads 127 levels.
//! - The tree owns each text. A `T` that borrows a `&str` from the input
//!   refuses each value. Give `T` a `String`.
//! - The tree keeps no JSON text. A `T` that holds a
//!   `serde_json::value::RawValue` refuses each value.
//! - The tree holds an integer past 64 bits as the nearest float. A `u128`
//!   and an `i128` refuse that float. Alone, each one reads the integer.
//! - An `f32` reads from the nearest `f64`. A number outside the range of an
//!   `f32` then reads as an infinity, and `serde_json` alone refuses it. The
//!   last bit of another number can differ. Give `T` an `f64`.
//!
//! # What is JSON here
//!
//! [`parse_object`] reads the bytes with `serde_json`. An answer is thus
//! strict JSON in UTF-8. `json.loads` of Python reads more, and each Python
//! client reads an answer with it. [`parse_object`] refuses these answers,
//! and each Python client reads them:
//!
//! - An answer with the word `NaN`, `Infinity` or `-Infinity`.
//! - An answer with a number outside the range of a float: `1e400`, or an
//!   integer of 400 digits.
//! - An answer with the escape of one half of a surrogate pair.
//! - An answer that nests 128 levels or more, up to the recursion limit of
//!   the interpreter.
//!
//! The Python clients do not read the encoding of an answer in one way. The
//! noticeboard, the delegate client of the chaperone and `caregiver` give
//! the bytes of the body to `json.loads`. The three doors give it the text
//! that `httpx` makes from the body. `httpx` reads the body as UTF-8 when the
//! header names no charset, and it reads a byte that is not UTF-8 as U+FFFD.
//! [`parse_object`] refuses each of these answers:
//!
//! - An answer that starts with a byte order mark, and an answer in UTF-16
//!   or in UTF-32. The three clients that give bytes read it. The doors
//!   refuse it.
//! - An answer with a byte that is not UTF-8. The doors read it, with U+FFFD
//!   in the place of the byte. The three clients that give bytes refuse it.
//! - An answer with the bytes of one half of a surrogate pair. The three
//!   clients that give bytes read the half. The doors read U+FFFD three
//!   times.
//!
//! The differential test at the end of this file walks the vectors of the
//! surfaces `runtime.untrusted.*` and `runtime.parse_object.*`. On each
//! vector of a surface `runtime.untrusted.*`, a reader gives what the Python
//! helper gives. Each input of those surfaces is strict JSON.
//!
//! The table `DEVIATIONS` of that test holds only vectors of
//! `runtime.parse_object.noticeboard`: each text on which [`parse_object`]
//! and the Python reader differ. A plain test holds each other difference
//! from a Python copy, with its input in the test. The doc comment of the
//! function, or this doc comment, names that difference.

use std::collections::HashMap;
use std::error::Error;
use std::fmt;

use serde::de::value::{MapAccessDeserializer, MapDeserializer, SeqDeserializer};
use serde::de::{
    self, DeserializeOwned, DeserializeSeed, IntoDeserializer, MapAccess, SeqAccess, Visitor,
};
use serde::{Deserialize, Deserializer};

use crate::json;

/// A text. A value that is not a JSON string reads as the empty text.
///
/// Use it with `#[serde(default, deserialize_with = "untrusted::text")]`. The
/// `default` gives the empty text for a field that is absent.
///
/// Use [`text_or_none`] when the reader must tell a value that is no text
/// from an empty text.
///
/// The function keeps each character of the text. Give the result to [`cut`]
/// where the Python reader has a cap.
///
/// The Python origin is `as_text` and `field_text` of each door, for example
/// `door-owui/src/agent_door_owui/untrusted.py:30-37`, and `text` and `whole`
/// of `noticeboard/src/noticeboard/jsonfiles.py:93-108`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn text<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    Ok(text_or_none(deserializer)?.unwrap_or_default())
}

/// A text, or `None`. A value that is absent, `null` or not a JSON string
/// reads as `None`.
///
/// `None` means that the field holds no text. `Some` with the empty text
/// means that the field holds a text with no character. [`text`] reads the
/// two cases as one.
///
/// Use it with
/// `#[serde(default, deserialize_with = "untrusted::text_or_none")]`. The
/// `default` gives `None` for a field that is absent.
///
/// The function keeps each character of the text. Give the text to [`cut`]
/// where the Python reader has a cap.
///
/// ```
/// use creche_contracts::untrusted;
/// use serde::Deserialize;
///
/// #[derive(Deserialize)]
/// struct RawReply {
///     #[serde(default, deserialize_with = "untrusted::text_or_none")]
///     content: Option<String>,
///     #[serde(default, deserialize_with = "untrusted::text_or_none")]
///     error: Option<String>,
/// }
///
/// let reply: RawReply = untrusted::parse_object(br#"{"content": "", "error": 7}"#)?;
/// assert_eq!(reply.content.as_deref(), Some(""));
/// assert_eq!(reply.error, None);
/// # Ok::<(), untrusted::NotAnObject>(())
/// ```
///
/// The Python origin is `_text` of
/// `chaperone/src/chaperone/delegate.py:226-232`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn text_or_none<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let Json::Text(text) = read(deserializer)? else {
        return Ok(None);
    };

    Ok(Some(text))
}

/// A whole number. A value that is not a JSON integer reads as 0.
///
/// `true` and `false` are not integers here, and each one reads as 0. In
/// Python a `bool` is an `int`, so each Python copy excludes it by name. A
/// number with a fraction or an exponent is not an integer, so `2.0` reads
/// as 0.
///
/// The result holds each integer of a strict JSON text: a value from the
/// smallest `i64` to the largest `u64`. The Python copies keep such a value
/// too. The conversion to the valid type gives a count its range.
///
/// Use it with
/// `#[serde(default = "untrusted::zero", deserialize_with = "untrusted::int")]`.
/// [`json::Integer`] has no `Default`, so [`zero`] gives the value of a field
/// that is absent.
///
/// An integer outside that range is not strict JSON (`rust/AGENTS.md`,
/// "JSON"). [`parse_object`] still reads such an integer, as a float, and
/// this function then gives 0. The Python copies keep each digit of it.
///
/// The Python origin is `field_int` of
/// `door-tui/src/agent_door_tui/untrusted.py:45-52` and `integer` of
/// `noticeboard/src/noticeboard/jsonfiles.py:111-123`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn int<'de, D>(deserializer: D) -> Result<json::Integer, D::Error>
where
    D: Deserializer<'de>,
{
    Ok(match read(deserializer)? {
        Json::Int(whole) => json::Integer::from(whole),
        Json::Large(whole) => json::Integer::from(whole),
        Json::Null
        | Json::Bool(_)
        | Json::Float(_)
        | Json::Text(_)
        | Json::List(_)
        | Json::Object(_) => zero(),
    })
}

/// The whole number 0: what [`int`] gives for a value that is no integer.
///
/// Name it as the default of a field that [`int`] reads. The field is then 0
/// when the object does not hold its key.
///
/// ```
/// use creche_contracts::{json, untrusted};
///
/// assert_eq!(untrusted::zero(), json::Integer::from(0_u64));
/// ```
#[must_use]
pub fn zero() -> json::Integer {
    json::Integer::from(0_i64)
}

/// A number: an integer or a float. A value of another type, `true` and
/// `false` read as `None`.
///
/// An integer reads as the nearest float, as `float` of Python gives it. The
/// result is always finite: the JSON reader refuses a text with a number
/// outside the range of a float.
///
/// The integer `-0` reads as `-0.0`. `serde_json` gives that text as the
/// float `-0.0`, so this function cannot tell it from the text `-0.0`.
/// `number` of the noticeboard gives `0.0` for the integer and `-0.0` for
/// the float. The two results are equal in each comparison of two floats.
/// No writer of the platform writes the text `-0`: `json.dumps` of Python,
/// `JSON.stringify` and `serde_json` each write the integer zero as `0`.
///
/// The Python origin is `number` of
/// `noticeboard/src/noticeboard/jsonfiles.py:126-137`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn number<'de, D>(deserializer: D) -> Result<Option<f64>, D::Error>
where
    D: Deserializer<'de>,
{
    Ok(match read(deserializer)? {
        Json::Int(whole) => nearest_float(whole),
        Json::Large(whole) => nearest_float(whole),
        Json::Float(float) => Some(float),
        Json::Null | Json::Bool(_) | Json::Text(_) | Json::List(_) | Json::Object(_) => None,
    })
}

/// The float that is nearest to an integer, with a tie to the even float.
///
/// The decimal text of the integer goes through the float parser of the
/// standard library, which rounds in that way. `float` of Python rounds an
/// `int` in the same way. The lint gate refuses the `as` conversion.
fn nearest_float(whole: impl fmt::Display) -> Option<f64> {
    whole.to_string().parse().ok()
}

/// A switch. It is on only for the JSON value `true`.
///
/// The number 1 and the text `"true"` read as off.
///
/// The Python origin is `flag` of
/// `noticeboard/src/noticeboard/jsonfiles.py:140-141`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn flag<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    Ok(matches!(read(deserializer)?, Json::Bool(true)))
}

/// An object of the raw type `T`. A value that is absent, `null` or not an
/// object reads as the default of `T`.
///
/// Use [`block`] when the reader must tell an absent object from an empty
/// one.
///
/// An object that `T` refuses reads as the default of `T` too. A raw type
/// refuses no object when each of its fields names a function of this module
/// and has `default`.
///
/// The Python origin is `as_object` of each door, for example
/// `door-owui/src/agent_door_owui/untrusted.py:25-27`, and `child` of
/// `noticeboard/src/noticeboard/jsonfiles.py:153-156`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn object<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    let value = read(deserializer)?;
    if !matches!(value, Json::Object(_)) {
        return Ok(T::default());
    }

    Ok(T::deserialize(value).unwrap_or_default())
}

/// An object of the raw type `T`, or `None`. A value that is absent, `null`
/// or not an object reads as `None`.
///
/// `None` means that the writer did not publish the block. `Some` with empty
/// fields means that the writer published an empty block. [`object`] reads
/// the two cases as one.
///
/// An object that `T` refuses reads as `None` too.
///
/// The Python origin is `block` of
/// `noticeboard/src/noticeboard/jsonfiles.py:144-150` and `as_object` of
/// `attendance/src/attendance/atomic.py:94-103`.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn block<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    let value = read(deserializer)?;
    if !matches!(value, Json::Object(_)) {
        return Ok(None);
    }

    Ok(T::deserialize(value).ok())
}

/// A list. A value that is not a JSON array reads as the empty list. The
/// function drops each member that is not a `T` and keeps the others in
/// order.
///
/// The result does not tell a value that is no list from an empty list.
///
/// A member is a `T` when the `serde` reader of `T` takes it. A `String`
/// takes a text only, and an `i64` takes an integer only. A struct with named
/// fields takes an object only. An `Option` also takes `null`, so a list of
/// it keeps a member that is `null`.
///
/// The Python origin is `as_list` of
/// `door-tui/src/agent_door_tui/untrusted.py:30-32` and `as_array` of
/// `attendance/src/attendance/atomic.py:106-111`. The drop of a member is the
/// `isinstance` check that each caller of the two makes on a member.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn list<'de, D, T>(deserializer: D) -> Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Ok(members_of(read(deserializer)?, usize::MAX))
}

/// A list of the first `N` members. The function takes the first `N` members
/// of the array. Then it drops each of them that is not a `T`.
///
/// The order of the two steps is the order of the Python reader. The result
/// can thus hold less than `N` members when the array holds more.
///
/// Write the count in the path: `untrusted::list_first::<8, _, _>`.
///
/// The Python origin is `children` and `strings` of
/// `noticeboard/src/noticeboard/jsonfiles.py:159-181`. The slice
/// `entries[:limit]` comes before the `isinstance` check there.
///
/// # Errors
///
/// Only the error of the deserializer itself, for a text that is not JSON.
pub fn list_first<'de, const N: usize, D, T>(deserializer: D) -> Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Ok(members_of(read(deserializer)?, N))
}

/// The first `first` members of a list, without each of them that is not a
/// `T`. A value that is not a list has no member.
fn members_of<'de, T: Deserialize<'de>>(value: Json, first: usize) -> Vec<T> {
    let Json::List(members) = value else {
        return Vec::new();
    };

    members
        .into_iter()
        .take(first)
        .filter_map(|member| T::deserialize(member).ok())
        .collect()
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
/// The function reads the whole body into the private tree first. The rules
/// of the module documentation thus hold from the first level: the last value
/// of a key, an object for a struct, and strict JSON in each part of the
/// body, also in a member that `T` does not read.
///
/// The Python origin is `parse_object` of
/// `noticeboard/src/noticeboard/jsonfiles.py:70-90`. The doors read an
/// answer in the same two steps, for example `_json_object` of
/// `door-owui/src/agent_door_owui/attendance.py:315-323`. That reader gives
/// an empty object for each refusal. A port of it calls `unwrap_or_default`
/// on the result.
///
/// A Python door does not give `json.loads` the bytes of the body. It gives
/// the text that `httpx` makes from them, and `httpx` reads a byte that is
/// not UTF-8 as U+FFFD. This function refuses a body with such a byte. The
/// port of a door has two choices:
///
/// 1. It gives the bytes of `String::from_utf8_lossy(body)` to this function.
///    The port then reads the body that the Python door reads.
/// 2. It gives the body itself, and it names the difference in its pull
///    request.
///
/// # Errors
///
/// [`NotAnObject::NotJson`] for bytes that are not one JSON text, and
/// [`NotAnObject::NotObject`] for a JSON value that is not an object.
///
/// A raw type with a field that names no function of this module can refuse
/// an object. The function gives [`NotAnObject::NotObject`] for that object
/// too: the body is not the object of this answer.
//
// CONTRACT-QUESTION: contract 02 §3 rule 3 says that a body is JSON. It does
// not say if a reader takes what `json.loads` of Python takes past strict
// JSON in UTF-8. Each Python client takes `NaN`, a number outside the range
// of a float and the escape of one half of a surrogate pair. The noticeboard,
// the delegate client and `caregiver` give the bytes to `json.loads`, so they
// also take a byte order mark, UTF-16 and UTF-32. The three doors give
// `json.loads` the text of `httpx`, so they refuse those three, and they read
// a byte that is not UTF-8 as U+FFFD. This reader refuses each of them, as
// the `session` module does for a request. The Python services write the
// JSON body of an answer with the JSON response class of their web framework.
// That class writes no `NaN`, no `Infinity` and no half of a surrogate pair,
// and it writes an integer of each size. A reader that takes them costs a
// JSON reader of this module in place of `serde_json`.
pub fn parse_object<T: DeserializeOwned>(bytes: &[u8]) -> Result<T, NotAnObject> {
    let mut reader = serde_json::Deserializer::from_slice(bytes);
    let value = read(&mut reader).map_err(|_| NotAnObject::NotJson)?;
    reader.end().map_err(|_| NotAnObject::NotJson)?;
    if !matches!(value, Json::Object(_)) {
        return Err(NotAnObject::NotObject);
    }

    T::deserialize(value).map_err(|_| NotAnObject::NotObject)
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

    // `end` is the first byte of a character, so `get` always gives the
    // text. The other result is the empty text, which is under each cap.
    text.get(..end).unwrap_or_default()
}

// --- one JSON value ---

/// The count of levels of lists and objects that one value can have.
///
/// A list inside a list has two levels. The tree of a value that nests deeper
/// is an error of the deserializer.
///
/// `serde_json` stops a text one level before this count, so the limit of
/// `serde_json` is the limit of [`parse_object`]. This limit is for a
/// deserializer that has none. The drop of a tree and the read of a raw type
/// from a tree use the stack for each level, so the count of levels needs a
/// bound here.
//
// CONTRACT-QUESTION: contract 02 §3 rule 3 says that a body is JSON and gives
// no nesting limit. Python reads a text until its own recursion limit, which
// differs between two versions of the interpreter. This reader takes the
// limit above, and `parse_object` takes the limit of `serde_json`. The Python
// host keeps only the type of a pi event that nests more than 64 levels
// (contract 03 §13 rule 6). A higher limit costs one number here and a reader
// with no limit in place of `serde_json`.
const DEPTH_MAX: usize = 128;

/// What the deserializer error says for a value past [`DEPTH_MAX`].
const TOO_DEEP: &str = "the value nests deeper than 128 levels";

/// One JSON value, by its kind.
///
/// Each function of this module reads a value into this type first. A kind
/// that the function does not take then has no error: the function gives its
/// empty value.
///
/// The type is also a deserializer. [`object`], [`block`], [`list`] and
/// [`list_first`] give a value to the raw type of the caller. A value that
/// the raw type refuses is a [`Mismatch`], which the function drops.
#[derive(Debug, Clone, PartialEq)]
enum Json {
    Null,
    Bool(bool),
    /// An integer that fits 64 bits with a sign.
    Int(i64),
    /// An integer above the largest `i64` that fits 64 bits with no sign.
    Large(u64),
    /// A number with a fraction or an exponent, and an integer of more than
    /// 64 bits. `serde_json` gives such an integer as the nearest float.
    Float(f64),
    Text(String),
    List(Vec<Self>),
    /// Each member, in the order of the text. No key occurs two times.
    Object(Vec<(String, Self)>),
}

/// The tree of the value that a deserializer holds.
fn read<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Json, D::Error> {
    Reader {
        levels_left: DEPTH_MAX,
    }
    .deserialize(deserializer)
}

/// Reads one value into a [`Json`].
#[derive(Clone, Copy)]
struct Reader {
    /// The count of levels of lists and objects that the value can have.
    levels_left: usize,
}

impl Reader {
    /// The reader of a member of a list or of an object.
    fn inside<E: de::Error>(self) -> Result<Self, E> {
        match self.levels_left.checked_sub(1) {
            Some(levels_left) => Ok(Self { levels_left }),
            None => Err(E::custom(TOO_DEEP)),
        }
    }
}

impl<'de> DeserializeSeed<'de> for Reader {
    type Value = Json;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Json, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Reader {
    type Value = Json;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a JSON value")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Json, E> {
        Ok(Json::Bool(value))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Json, E> {
        Ok(Json::Int(value))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Json, E> {
        Ok(i64::try_from(value).map_or(Json::Large(value), Json::Int))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Json, E> {
        Ok(Json::Float(value))
    }

    fn visit_str<E>(self, value: &str) -> Result<Json, E> {
        Ok(Json::Text(value.to_owned()))
    }

    fn visit_string<E>(self, value: String) -> Result<Json, E> {
        Ok(Json::Text(value))
    }

    fn visit_unit<E>(self) -> Result<Json, E> {
        Ok(Json::Null)
    }

    fn visit_none<E>(self) -> Result<Json, E> {
        Ok(Json::Null)
    }

    fn visit_some<D: Deserializer<'de>>(self, deserializer: D) -> Result<Json, D::Error> {
        deserializer.deserialize_any(self)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Json, A::Error> {
        let inside = self.inside()?;
        let mut members = Vec::new();
        while let Some(member) = seq.next_element_seed(inside)? {
            members.push(member);
        }

        Ok(Json::List(members))
    }

    /// A key that occurs two times has the value of its last occurrence, at
    /// the place of its first. A `dict` of Python does the same.
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Json, A::Error> {
        let inside = self.inside()?;
        let mut members: Vec<(String, Json)> = Vec::new();
        let mut places: HashMap<String, usize> = HashMap::new();
        while let Some(key) = map.next_key::<String>()? {
            let value = map.next_value_seed(inside)?;
            let first = places.get(&key).and_then(|place| members.get_mut(*place));
            if let Some(member) = first {
                member.1 = value;
                continue;
            }

            places.insert(key.clone(), members.len());
            members.push((key, value));
        }

        Ok(Json::Object(members))
    }
}

/// A value is not what the raw type of a caller takes.
///
/// The type holds no text. A function of this module drops it and gives its
/// empty value, so no part of an answer goes into an error.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Mismatch;

impl fmt::Display for Mismatch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the value is not what the raw type takes")
    }
}

impl Error for Mismatch {}

impl de::Error for Mismatch {
    fn custom<T: fmt::Display>(_: T) -> Self {
        Self
    }
}

impl IntoDeserializer<'_, Mismatch> for Json {
    type Deserializer = Self;

    fn into_deserializer(self) -> Self {
        self
    }
}

/// The kinds of value that one request of a raw type takes.
#[derive(Debug, Clone, Copy)]
enum Takes {
    Null,
    Bool,
    Number,
    Text,
    List,
    Object,
    /// A request for bytes. `serde_json` gives it a text or a list.
    TextOrList,
}

impl Json {
    fn is(&self, takes: Takes) -> bool {
        matches!(
            (takes, self),
            (Takes::Null, Self::Null)
                | (Takes::Bool, Self::Bool(_))
                | (
                    Takes::Number,
                    Self::Int(_) | Self::Large(_) | Self::Float(_)
                )
                | (Takes::Text | Takes::TextOrList, Self::Text(_))
                | (Takes::List | Takes::TextOrList, Self::List(_))
                | (Takes::Object, Self::Object(_))
        )
    }

    /// Gives the value to a visitor when the value is of a kind that the
    /// request takes. Each other kind is a [`Mismatch`].
    ///
    /// `serde_json` checks the kind of a request in the same way. Without
    /// the check, a visitor that takes more than its request gets the other
    /// kind: the visitor of `serde_json::Map` reads `null` as an empty
    /// object.
    fn only<'de, V: Visitor<'de>>(self, takes: Takes, visitor: V) -> Result<V::Value, Mismatch> {
        if !self.is(takes) {
            return Err(Mismatch);
        }

        self.deserialize_any(visitor)
    }
}

/// The requests of a raw type that take one group of kinds and have no other
/// argument.
macro_rules! only {
    ($takes:expr => $($request:ident)*) => {
        $(
            fn $request<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Mismatch> {
                self.only($takes, visitor)
            }
        )*
    };
}

/// A raw type reads from the tree as it reads from `serde_json`, with the
/// differences that the module documentation lists.
impl<'de> Deserializer<'de> for Json {
    type Error = Mismatch;

    fn deserialize_any<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Mismatch> {
        match self {
            Self::Null => visitor.visit_unit(),
            Self::Bool(value) => visitor.visit_bool(value),
            // `serde_json` gives an integer that is not negative to
            // `visit_u64`, and a negative integer to `visit_i64`.
            Self::Int(value) => match u64::try_from(value) {
                Ok(whole) => visitor.visit_u64(whole),
                Err(_) => visitor.visit_i64(value),
            },
            Self::Large(value) => visitor.visit_u64(value),
            Self::Float(value) => visitor.visit_f64(value),
            Self::Text(value) => visitor.visit_string(value),
            Self::List(members) => {
                let mut seq = SeqDeserializer::new(members.into_iter());
                let value = visitor.visit_seq(&mut seq)?;
                seq.end()?;

                Ok(value)
            }
            Self::Object(members) => {
                let mut map = MapDeserializer::new(members.into_iter());
                let value = visitor.visit_map(&mut map)?;
                map.end()?;

                Ok(value)
            }
        }
    }

    only!(Takes::Null => deserialize_unit);
    only!(Takes::Bool => deserialize_bool);
    only!(Takes::Number =>
        deserialize_i8 deserialize_i16 deserialize_i32 deserialize_i64 deserialize_i128
        deserialize_u8 deserialize_u16 deserialize_u32 deserialize_u64 deserialize_u128
        deserialize_f32 deserialize_f64
    );
    only!(Takes::Text =>
        deserialize_char deserialize_str deserialize_string deserialize_identifier
    );
    only!(Takes::TextOrList => deserialize_bytes deserialize_byte_buf);
    only!(Takes::List => deserialize_seq);
    only!(Takes::Object => deserialize_map);

    fn deserialize_unit_struct<V: Visitor<'de>>(
        self,
        _: &'static str,
        visitor: V,
    ) -> Result<V::Value, Mismatch> {
        self.only(Takes::Null, visitor)
    }

    fn deserialize_tuple<V: Visitor<'de>>(
        self,
        _: usize,
        visitor: V,
    ) -> Result<V::Value, Mismatch> {
        self.only(Takes::List, visitor)
    }

    fn deserialize_tuple_struct<V: Visitor<'de>>(
        self,
        _: &'static str,
        _: usize,
        visitor: V,
    ) -> Result<V::Value, Mismatch> {
        self.only(Takes::List, visitor)
    }

    /// A struct with named fields reads from an object only.
    ///
    /// `serde_json` also gives a list to the visitor, and a derived visitor
    /// then fills the fields by position. No Python reader does that: each
    /// one takes a member only when it is a `dict`.
    fn deserialize_struct<V: Visitor<'de>>(
        self,
        _: &'static str,
        _: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Mismatch> {
        self.only(Takes::Object, visitor)
    }

    fn deserialize_option<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Mismatch> {
        match self {
            Self::Null => visitor.visit_none(),
            value => visitor.visit_some(value),
        }
    }

    fn deserialize_newtype_struct<V: Visitor<'de>>(
        self,
        _: &'static str,
        visitor: V,
    ) -> Result<V::Value, Mismatch> {
        visitor.visit_newtype_struct(self)
    }

    /// An enum reads as `serde_json` reads it: a text is a variant with no
    /// content, and an object with one member is a variant and its content.
    fn deserialize_enum<V: Visitor<'de>>(
        self,
        _: &'static str,
        _: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Mismatch> {
        match self {
            Self::Text(variant) => visitor.visit_enum(variant.into_deserializer()),
            Self::Object(members) if members.len() == 1 => {
                let map = MapDeserializer::new(members.into_iter());

                visitor.visit_enum(MapAccessDeserializer::new(map))
            }
            Self::Null
            | Self::Bool(_)
            | Self::Int(_)
            | Self::Large(_)
            | Self::Float(_)
            | Self::List(_)
            | Self::Object(_) => Err(Mismatch),
        }
    }

    fn deserialize_ignored_any<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Mismatch> {
        visitor.visit_unit()
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde::forward_to_deserialize_any;
    use serde_json::de::StrRead;

    use super::*;

    #[test]
    fn a_refusal_names_its_reason_and_no_byte_of_the_answer() {
        assert_eq!(NotAnObject::NotJson.to_string(), "the answer is not JSON");
        assert_eq!(
            NotAnObject::NotObject.to_string(),
            "the answer is not a JSON object"
        );
    }

    // --- how the tests call a function ---

    /// The reader of `serde_json` over one JSON text.
    type TextReader<'a> = serde_json::Deserializer<StrRead<'a>>;

    /// What a function of this module gives for one JSON text. The function
    /// gets the reader of `serde_json` itself, with no raw type around the
    /// value.
    fn direct<'a, T>(
        input: &'a str,
        function: impl FnOnce(&mut TextReader<'a>) -> Result<T, serde_json::Error>,
    ) -> Result<T, serde_json::Error> {
        let mut reader = serde_json::Deserializer::from_str(input);
        let value = function(&mut reader)?;
        reader.end()?;

        Ok(value)
    }

    /// The raw type of a small object. Each field names a function of this
    /// module and has `default`, so the type refuses no object.
    #[derive(Debug, PartialEq, Deserialize)]
    struct Part {
        #[serde(default, deserialize_with = "text")]
        name: String,
        #[serde(default = "zero", deserialize_with = "int")]
        count: json::Integer,
    }

    impl Default for Part {
        fn default() -> Self {
            part("", 0)
        }
    }

    fn part(name: &str, count: i64) -> Part {
        Part {
            name: name.to_owned(),
            count: whole(count),
        }
    }

    /// The value that `int` gives for the digits of `value`.
    fn whole(value: i64) -> json::Integer {
        json::Integer::from(value)
    }

    /// A raw type with a field that names no function. It refuses an object
    /// whose `id` is absent or is no number.
    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct Strict {
        id: u64,
    }

    // --- text ---

    #[test]
    fn a_text_reads_as_its_characters() {
        let texts = [
            (r#""family""#, "family"),
            (r#""""#, ""),
            (r#""  a  ""#, "  a  "),
            (r#""7""#, "7"),
            (r#""a\"b\\c\ndé""#, "a\"b\\c\nd\u{e9}"),
            (r#""😀""#, "\u{1f600}"),
        ];

        for (input, wanted) in texts {
            assert_eq!(
                direct(input, |reader| text(reader)).unwrap(),
                wanted,
                "{input}"
            );
        }
    }

    #[test]
    fn a_value_that_is_no_text_reads_as_the_empty_text() {
        let others = [
            "null",
            "true",
            "false",
            "7",
            "-3",
            "1.5",
            "[]",
            r#"["a"]"#,
            "{}",
            r#"{"a":"b"}"#,
        ];

        for input in others {
            assert_eq!(direct(input, |reader| text(reader)).unwrap(), "", "{input}");
        }
    }

    #[test]
    fn a_text_keeps_each_character_and_the_caller_cuts_it() {
        let input = format!("\"{}\"", "a".repeat(600));
        let read = direct(&input, |reader| text(reader)).unwrap();

        assert_eq!(read.len(), 600);
        assert_eq!(cut(&read, 500).len(), 500);
    }

    // --- text_or_none ---

    #[test]
    fn a_text_reads_as_some_text_with_each_character() {
        let texts = [
            (r#""family""#, "family"),
            (r#""""#, ""),
            (r#""  a  ""#, "  a  "),
            (r#""7""#, "7"),
            (r#""null""#, "null"),
            (r#""a\"b\\c\ndé""#, "a\"b\\c\nd\u{e9}"),
        ];

        for (input, wanted) in texts {
            assert_eq!(
                direct(input, |reader| text_or_none(reader)).unwrap(),
                Some(wanted.to_owned()),
                "{input}"
            );
        }
    }

    #[test]
    fn a_value_that_is_no_text_reads_as_none() {
        let others = [
            "null",
            "true",
            "false",
            "7",
            "-3",
            "1.5",
            "[]",
            r#"["a"]"#,
            "{}",
            r#"{"a":"b"}"#,
        ];

        for input in others {
            assert_eq!(
                direct(input, |reader| text_or_none(reader)).unwrap(),
                None,
                "{input}"
            );
        }
    }

    /// The raw type of a reply with one text field, as the delegate client
    /// of the chaperone reads one.
    #[derive(Debug, PartialEq, Deserialize)]
    struct Reply {
        #[serde(default, deserialize_with = "text_or_none")]
        field: Option<String>,
    }

    /// What `_text` of that client gives for one body, with a cap of 3
    /// characters: the text of the field up to the cap, or `None`.
    fn capped_at_3(body: &[u8]) -> Option<String> {
        let reply: Reply = parse_object(body).unwrap();

        reply.field.as_deref().map(|text| cut(text, 3).to_owned())
    }

    #[test]
    fn a_field_that_is_no_text_is_none_and_an_empty_text_is_some() {
        let family = capped_at_3(br#"{"field":"family"}"#);

        assert_eq!(family.as_deref(), Some("fam"));
        assert_eq!(capped_at_3(br#"{"field":"fa"}"#).as_deref(), Some("fa"));
        assert_eq!(capped_at_3(br#"{"field":""}"#).as_deref(), Some(""));
        assert_eq!(capped_at_3(br#"{"field":7}"#), None);
        assert_eq!(capped_at_3(br#"{"field":null}"#), None);
        assert_eq!(capped_at_3(br#"{"field":["family"]}"#), None);
        assert_eq!(capped_at_3(br#"{"other":"family"}"#), None);
        assert_eq!(capped_at_3(b"{}"), None);
    }

    /// `text` gives the empty text where `text_or_none` gives `None`, and
    /// the same text for each other value.
    #[test]
    fn the_two_text_readers_differ_only_for_a_value_that_is_no_text() {
        for input in EACH_KIND {
            let held = direct(input, |reader| text_or_none(reader)).unwrap();
            let read = direct(input, |reader| text(reader)).unwrap();

            assert_eq!(read, held.unwrap_or_default(), "{input}");
        }
    }

    // --- int ---

    #[test]
    fn an_integer_reads_as_its_value() {
        let integers = [
            ("7", whole(7)),
            ("0", whole(0)),
            ("-3", whole(-3)),
            ("9223372036854775807", whole(i64::MAX)),
            ("-9223372036854775808", whole(i64::MIN)),
            ("9223372036854775808", json::Integer::from(1_u64 << 63)),
            ("18446744073709551615", json::Integer::from(u64::MAX)),
        ];

        for (input, wanted) in integers {
            let read = direct(input, |reader| int(reader)).unwrap();

            assert_eq!(read, wanted, "{input}");
            // The value has the digits of the text.
            assert_eq!(read.to_string(), input);
        }
    }

    /// `serde_json` gives the text `-0` as a float. The result is still the
    /// value of that integer.
    #[test]
    fn the_integer_zero_with_a_minus_sign_reads_as_zero() {
        assert_eq!(direct("-0", |reader| int(reader)).unwrap(), zero());
    }

    #[test]
    fn a_value_that_is_no_integer_reads_as_zero() {
        let others = [
            "true",
            "false",
            "2.0",
            "1.5",
            "1e3",
            "-0.0",
            r#""7""#,
            "null",
            "[7]",
            r#"{"a":7}"#,
        ];

        for input in others {
            assert_eq!(
                direct(input, |reader| int(reader)).unwrap(),
                zero(),
                "{input}"
            );
        }
    }

    /// The difference that the doc comment of `int` names. `serde_json`
    /// gives an integer outside 64 bits as a float, and a float is no
    /// integer.
    #[test]
    fn an_integer_outside_64_bits_reads_as_zero() {
        let outside = [
            "18446744073709551616",
            "-9223372036854775809",
            "123456789012345678901234567890",
        ];

        for input in outside {
            assert_eq!(
                direct(input, |reader| int(reader)).unwrap(),
                zero(),
                "{input}"
            );
        }
    }

    #[test]
    fn the_default_of_an_integer_field_is_zero() {
        assert_eq!(zero().to_i64(), Some(0));
        assert_eq!(zero().to_string(), "0");
    }

    // --- number ---

    /// `test_a_number_reads_as_a_float` of
    /// `noticeboard/tests/test_noticeboard_jsonfiles.py`.
    #[test]
    fn a_number_reads_as_a_float() {
        assert_eq!(direct("3", |reader| number(reader)).unwrap(), Some(3.0));
        assert_eq!(direct("3.42", |reader| number(reader)).unwrap(), Some(3.42));
    }

    /// `test_a_field_that_is_not_a_number_reads_none` of the same file. The
    /// test of a raw type below covers the absent field.
    #[test]
    fn a_value_that_is_no_number_reads_as_none() {
        let others = [r#""3.42""#, "true", "false", "null", "[3]", r#"{"a":3}"#];

        for input in others {
            assert_eq!(
                direct(input, |reader| number(reader)).unwrap(),
                None,
                "{input}"
            );
        }
    }

    /// `test_an_integer_past_every_float_reads_none` of the same file. The
    /// Python reader gives `None` for the field. `serde_json` refuses the
    /// text, so the function gives the error of the deserializer.
    #[test]
    fn an_integer_past_every_float_is_no_json_text_here() {
        let past_every_float = format!("1{}", "0".repeat(400));
        let below_every_float = format!("-{past_every_float}");

        assert!(direct(&past_every_float, |reader| number(reader)).is_err());
        assert!(direct(&below_every_float, |reader| number(reader)).is_err());
        assert!(direct("1e400", |reader| number(reader)).is_err());
    }

    #[test]
    fn an_integer_reads_as_the_nearest_float_with_a_tie_to_even() {
        let two_to_63 = 9_223_372_036_854_775_808.0_f64;
        let two_to_64 = 18_446_744_073_709_551_616.0_f64;
        let integers = [
            // 2^53 + 1 and 2^53 + 3 are each in the middle of two floats.
            ("9007199254740993", 9_007_199_254_740_992.0),
            ("9007199254740995", 9_007_199_254_740_996.0),
            ("9223372036854775807", two_to_63),
            ("-9223372036854775808", -two_to_63),
            ("9223372036854775808", two_to_63),
            ("18446744073709551615", two_to_64),
            ("18446744073709551616", two_to_64),
            ("123456789012345678901234567890", 1.234_567_890_123_456_8e29),
        ];

        for (input, wanted) in integers {
            let read = direct(input, |reader| number(reader)).unwrap().unwrap();

            assert_eq!(read.to_bits(), wanted.to_bits(), "{input}");
        }
    }

    #[test]
    fn a_float_keeps_the_sign_of_its_zero() {
        let zeros = [("0.0", 0.0_f64), ("-0.0", -0.0_f64), ("0", 0.0_f64)];

        for (input, wanted) in zeros {
            let read = direct(input, |reader| number(reader)).unwrap().unwrap();

            assert_eq!(read.to_bits(), wanted.to_bits(), "{input}");
        }
    }

    /// The difference that the doc comment of `number` names: the integer
    /// zero with a minus sign. `number` of the noticeboard gives `0.0` for
    /// it.
    #[test]
    fn the_integer_zero_with_a_minus_sign_reads_as_the_float_with_that_sign() {
        let negative_zero = Some((-0.0_f64).to_bits());
        let alone = direct("-0", |reader| number(reader)).unwrap();
        let field = parse_object::<Each>(br#"{"number":-0}"#).unwrap().number;

        assert_eq!(alone.map(f64::to_bits), negative_zero);
        assert_eq!(field.map(f64::to_bits), negative_zero);
    }

    // --- flag ---

    #[test]
    fn a_switch_is_on_for_true_and_for_no_other_value() {
        let others = [
            "false",
            "1",
            "1.0",
            r#""true""#,
            "null",
            "[true]",
            r#"{"a":true}"#,
        ];

        assert!(direct("true", |reader| flag(reader)).unwrap());
        for input in others {
            assert!(!direct(input, |reader| flag(reader)).unwrap(), "{input}");
        }
    }

    // --- object and block ---

    /// Each JSON text that is no object. The last one is a list with the
    /// values of the two fields of `Part`, in the order of the fields.
    const NO_OBJECT: [&str; 8] = [
        "null",
        "true",
        "7",
        "1.5",
        r#""a""#,
        "[]",
        r#"[{"name":"a"}]"#,
        r#"["a",2]"#,
    ];

    #[test]
    fn an_object_reads_through_its_raw_type() {
        let objects = [
            (r#"{"name":"a","count":2}"#, part("a", 2)),
            (r#"{"count":2,"name":"a"}"#, part("a", 2)),
            ("{}", part("", 0)),
            (r#"{"name":7,"count":"2"}"#, part("", 0)),
            (r#"{"name":"a","other":[1,{"b":null}]}"#, part("a", 0)),
        ];

        for (input, wanted) in objects {
            let read: Part = direct(input, |reader| object(reader)).unwrap();
            let held: Option<Part> = direct(input, |reader| block(reader)).unwrap();

            assert_eq!(read, wanted, "{input}");
            assert_eq!(held, Some(wanted), "{input}");
        }
    }

    #[test]
    fn a_value_that_is_no_object_reads_as_the_default_and_as_no_block() {
        for input in NO_OBJECT {
            let read: Part = direct(input, |reader| object(reader)).unwrap();
            let held: Option<Part> = direct(input, |reader| block(reader)).unwrap();

            assert_eq!(read, Part::default(), "{input}");
            assert_eq!(held, None, "{input}");
        }
    }

    /// The kind of the value comes first. A raw type that takes another kind
    /// gets no value of that kind.
    #[test]
    fn an_object_reader_gives_no_other_kind_to_a_raw_type_that_takes_it() {
        let text: Option<String> = direct(r#""a""#, |reader| block(reader)).unwrap();
        let members: Option<Vec<String>> = direct(r#"["a"]"#, |reader| block(reader)).unwrap();
        let unset: Option<Option<Part>> = direct("null", |reader| block(reader)).unwrap();
        let read_text: String = direct(r#""a""#, |reader| object(reader)).unwrap();
        let read_members: Vec<String> = direct(r#"["a"]"#, |reader| object(reader)).unwrap();

        assert_eq!(text, None);
        assert_eq!(members, None);
        assert_eq!(unset, None);
        assert_eq!(read_text, "");
        assert!(read_members.is_empty());
    }

    #[test]
    fn an_empty_block_differs_from_no_block() {
        let empty: Option<Part> = direct("{}", |reader| block(reader)).unwrap();
        let absent: Option<Part> = direct("null", |reader| block(reader)).unwrap();

        assert_eq!(empty, Some(Part::default()));
        assert_eq!(absent, None);
    }

    #[test]
    fn an_object_that_its_raw_type_refuses_reads_as_empty() {
        let taken = r#"{"id":7}"#;
        let refused = [r#"{"id":"7"}"#, r#"{"id":-1}"#, "{}"];

        let read: Strict = direct(taken, |reader| object(reader)).unwrap();
        let held: Option<Strict> = direct(taken, |reader| block(reader)).unwrap();

        assert_eq!(read, Strict { id: 7 });
        assert_eq!(held, Some(Strict { id: 7 }));
        for input in refused {
            let read: Strict = direct(input, |reader| object(reader)).unwrap();
            let held: Option<Strict> = direct(input, |reader| block(reader)).unwrap();

            assert_eq!(read, Strict::default(), "{input}");
            assert_eq!(held, None, "{input}");
        }
    }

    #[test]
    fn a_key_that_an_object_holds_two_times_has_its_last_value() {
        let input = r#"{"name":"first","count":1,"name":"last"}"#;
        let read: Part = direct(input, |reader| object(reader)).unwrap();
        let held: Option<Part> = direct(input, |reader| block(reader)).unwrap();
        let members: Vec<Part> = direct(&format!("[{input}]"), |reader| list(reader)).unwrap();

        assert_eq!(read, part("last", 1));
        assert_eq!(held, Some(part("last", 1)));
        assert_eq!(members, [part("last", 1)]);
        // The derived type alone refuses the object.
        assert!(serde_json::from_str::<Part>(input).is_err());
    }

    // --- list and list_first ---

    /// A list with one member of each kind.
    const MIXED: &str = r#"["a",1,true,null,{"name":"n"},["x"],2.5,"b"]"#;

    #[test]
    fn a_list_drops_each_member_of_a_wrong_type_and_keeps_the_order() {
        let texts: Vec<String> = direct(MIXED, |reader| list(reader)).unwrap();
        let integers: Vec<i64> = direct(MIXED, |reader| list(reader)).unwrap();
        let numbers: Vec<f64> = direct(MIXED, |reader| list(reader)).unwrap();
        let switches: Vec<bool> = direct(MIXED, |reader| list(reader)).unwrap();
        let parts: Vec<Part> = direct(MIXED, |reader| list(reader)).unwrap();
        let lists: Vec<Vec<String>> = direct(MIXED, |reader| list(reader)).unwrap();

        assert_eq!(texts, ["a", "b"]);
        assert_eq!(integers, [1]);
        assert_eq!(numbers, [1.0, 2.5]);
        assert_eq!(switches, [true]);
        assert_eq!(parts, [part("n", 0)]);
        assert_eq!(lists, [["x"]]);
    }

    /// The reader of `T` says what a member of a wrong type is. An `Option`
    /// takes `null`, so a list of it keeps that member.
    #[test]
    fn a_list_keeps_each_member_that_its_raw_type_takes() {
        let texts: Vec<Option<String>> = direct(MIXED, |reader| list(reader)).unwrap();

        assert_eq!(texts, [Some("a".to_owned()), None, Some("b".to_owned())]);
    }

    #[test]
    fn a_value_that_is_no_list_reads_as_the_empty_list() {
        let others = ["null", "true", "7", "1.5", r#""a""#, "{}", r#"{"0":"a"}"#];

        for input in others {
            let read: Vec<String> = direct(input, |reader| list(reader)).unwrap();
            let first: Vec<String> = direct(input, |reader| list_first::<2, _, _>(reader)).unwrap();

            assert!(read.is_empty(), "{input}");
            assert!(first.is_empty(), "{input}");
        }
    }

    #[test]
    fn the_cap_of_a_list_counts_each_member_that_the_list_drops() {
        let input = r#"["a",1,"b","c"]"#;
        let none: Vec<String> = direct(input, |reader| list_first::<0, _, _>(reader)).unwrap();
        let one: Vec<String> = direct(input, |reader| list_first::<1, _, _>(reader)).unwrap();
        let two: Vec<String> = direct(input, |reader| list_first::<2, _, _>(reader)).unwrap();
        let three: Vec<String> = direct(input, |reader| list_first::<3, _, _>(reader)).unwrap();
        let each: Vec<String> = direct(input, |reader| list_first::<50, _, _>(reader)).unwrap();
        let whole: Vec<String> = direct(input, |reader| list(reader)).unwrap();

        assert!(none.is_empty());
        assert_eq!(one, ["a"]);
        // The second member is no text. The cap of 2 counts it.
        assert_eq!(two, ["a"]);
        assert_eq!(three, ["a", "b"]);
        assert_eq!(each, ["a", "b", "c"]);
        assert_eq!(whole, each);
    }

    #[test]
    fn a_member_that_is_a_list_is_no_struct() {
        let input = r#"[{"name":"a","count":1},["b",2],{"name":"c"}]"#;
        let read: Vec<Part> = direct(input, |reader| list(reader)).unwrap();

        assert_eq!(read, [part("a", 1), part("c", 0)]);
        // The derived type alone takes the list and fills each field by its
        // place.
        assert!(serde_json::from_str::<Strict>("[7]").is_ok());
    }

    // --- a raw type below a function ---

    #[derive(Debug, PartialEq, Deserialize)]
    #[serde(rename_all = "snake_case")]
    enum Shape {
        Plain,
        Named(String),
        Sized { count: u8 },
    }

    #[test]
    fn an_enum_reads_as_serde_json_reads_it() {
        let taken = r#"["plain",{"named":"a"},{"sized":{"count":2}}]"#;
        let mixed = r#"[
            "plain", "other", 7, null, {"named": 7},
            {"named": "a", "sized": {"count": 2}}, {}, {"named": "b"}
        ]"#;
        let wanted = [
            Shape::Plain,
            Shape::Named("a".to_owned()),
            Shape::Sized { count: 2 },
        ];

        let read: Vec<Shape> = direct(taken, |reader| list(reader)).unwrap();
        let kept: Vec<Shape> = direct(mixed, |reader| list(reader)).unwrap();

        assert_eq!(read, wanted);
        assert_eq!(serde_json::from_str::<Vec<Shape>>(taken).unwrap(), wanted);
        assert_eq!(kept, [Shape::Plain, Shape::Named("b".to_owned())]);
    }

    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct Plain {
        label: Option<String>,
        pair: (String, i64),
        sizes: BTreeMap<String, u8>,
        wrapped: Wrapped,
    }

    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct Wrapped(String);

    #[test]
    fn a_raw_type_with_plain_fields_reads_as_serde_json_reads_it() {
        let taken = [
            r#"{"label":"l","pair":["a",1],"sizes":{"x":1,"y":255},"wrapped":"w","more":[{}]}"#,
            r#"{"label":null,"pair":["a",1],"sizes":{},"wrapped":""}"#,
            r#"{"pair":["a",1],"sizes":{},"wrapped":""}"#,
        ];
        let refused = [
            // A pair of one member and a pair of three members.
            r#"{"pair":["a"],"sizes":{},"wrapped":""}"#,
            r#"{"pair":["a",1,2],"sizes":{},"wrapped":""}"#,
            // A size that no `u8` holds.
            r#"{"pair":["a",1],"sizes":{"x":256},"wrapped":""}"#,
            // A map that is a list, and a text that is a number.
            r#"{"pair":["a",1],"sizes":[],"wrapped":""}"#,
            r#"{"pair":["a",1],"sizes":{},"wrapped":7}"#,
            // A field that is absent.
            r#"{"pair":["a",1],"sizes":{}}"#,
        ];

        for input in taken {
            let read: Option<Plain> = direct(input, |reader| block(reader)).unwrap();

            assert_eq!(read, serde_json::from_str(input).ok(), "{input}");
            assert!(read.is_some(), "{input}");
        }
        for input in refused {
            let read: Option<Plain> = direct(input, |reader| block(reader)).unwrap();

            assert_eq!(read, None, "{input}");
            assert!(serde_json::from_str::<Plain>(input).is_err(), "{input}");
        }
    }

    /// A raw type that borrows its text from the input.
    #[derive(Debug, PartialEq, Deserialize)]
    struct Borrowed<'a> {
        name: &'a str,
    }

    /// The tree owns each text, so it has no text to lend.
    #[test]
    fn a_raw_type_that_borrows_from_the_input_refuses_each_object() {
        let input = r#"{"name":"a"}"#;
        let held: Option<Borrowed<'_>> = direct(input, |reader| block(reader)).unwrap();

        assert_eq!(held, None);
        assert_eq!(
            serde_json::from_str::<Borrowed<'_>>(input).unwrap(),
            Borrowed { name: "a" }
        );
    }

    #[derive(Debug, Default, PartialEq, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Closed {
        #[serde(default, deserialize_with = "text")]
        name: String,
    }

    #[test]
    fn a_raw_type_that_denies_an_unknown_field_refuses_the_object() {
        let known: Option<Closed> = direct(r#"{"name":"a"}"#, |reader| block(reader)).unwrap();
        let unknown: Option<Closed> =
            direct(r#"{"name":"a","other":1}"#, |reader| block(reader)).unwrap();

        assert_eq!(
            known,
            Some(Closed {
                name: "a".to_owned()
            })
        );
        assert_eq!(unknown, None);
    }

    /// A raw type with each function of this module, and the functions
    /// inside each other.
    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct Outer {
        #[serde(default, deserialize_with = "object")]
        head: Part,
        #[serde(default, deserialize_with = "block")]
        tail: Option<Part>,
        #[serde(default, deserialize_with = "list")]
        parts: Vec<Part>,
        #[serde(default, deserialize_with = "list_first::<2, _, _>")]
        first: Vec<Outer>,
    }

    #[test]
    fn the_functions_read_inside_each_other() {
        let input = r#"{
            "head": {"name": "h", "count": 1},
            "tail": {"name": "t"},
            "parts": [{"name": "p"}, 7, {"count": 2}],
            "first": [
                {"head": {"name": "inner"}, "first": [{"parts": [{}]}]},
                "dropped",
                {"head": {"name": "past the cap"}}
            ]
        }"#;
        let innermost = Outer {
            parts: vec![part("", 0)],
            ..Outer::default()
        };
        let inner = Outer {
            head: part("inner", 0),
            first: vec![innermost],
            ..Outer::default()
        };
        let wanted = Outer {
            head: part("h", 1),
            tail: Some(part("t", 0)),
            parts: vec![part("p", 0), part("", 2)],
            first: vec![inner],
        };

        let read: Outer = direct(input, |reader| object(reader)).unwrap();

        assert_eq!(read, wanted);
    }

    // --- parse_object ---

    /// A raw type with each field reader of this module.
    #[derive(Debug, PartialEq, Deserialize)]
    struct Each {
        #[serde(default, deserialize_with = "text")]
        text: String,
        #[serde(default, deserialize_with = "text_or_none")]
        text_or_none: Option<String>,
        #[serde(default = "zero", deserialize_with = "int")]
        int: json::Integer,
        #[serde(default, deserialize_with = "number")]
        number: Option<f64>,
        #[serde(default, deserialize_with = "flag")]
        flag: bool,
        #[serde(default, deserialize_with = "object")]
        object: Part,
        #[serde(default, deserialize_with = "block")]
        block: Option<Part>,
        #[serde(default, deserialize_with = "list")]
        list: Vec<String>,
        #[serde(default, deserialize_with = "list_first::<2, _, _>")]
        first: Vec<String>,
    }

    impl Default for Each {
        fn default() -> Self {
            Self {
                text: String::new(),
                text_or_none: None,
                int: zero(),
                number: None,
                flag: false,
                object: Part::default(),
                block: None,
                list: Vec::new(),
                first: Vec::new(),
            }
        }
    }

    /// The names of the fields of `Each`.
    const FIELDS: [&str; 9] = [
        "text",
        "text_or_none",
        "int",
        "number",
        "flag",
        "object",
        "block",
        "list",
        "first",
    ];

    /// A document that gives each field of `Each` the same JSON value.
    fn each_field_is(value: &str) -> Vec<u8> {
        let members: Vec<String> = FIELDS
            .iter()
            .map(|field| format!(r#""{field}":{value}"#))
            .collect();

        format!("{{{}}}", members.join(",")).into_bytes()
    }

    #[test]
    fn a_document_with_each_field_of_its_type_reads_each_value() {
        let document = br#"{
            "text": "family",
            "text_or_none": "",
            "int": -3,
            "number": 1.5,
            "flag": true,
            "object": {"name": "o", "count": 1},
            "block": {"name": "b"},
            "list": ["a", "b", "c"],
            "first": ["a", "b", "c"]
        }"#;
        let wanted = Each {
            text: "family".to_owned(),
            text_or_none: Some(String::new()),
            int: whole(-3),
            number: Some(1.5),
            flag: true,
            object: part("o", 1),
            block: Some(part("b", 0)),
            list: vec!["a".to_owned(), "b".to_owned(), "c".to_owned()],
            first: vec!["a".to_owned(), "b".to_owned()],
        };

        assert_eq!(parse_object::<Each>(document), Ok(wanted));
    }

    #[test]
    fn a_document_with_no_field_reads_as_each_empty_value() {
        assert_eq!(parse_object::<Each>(b"{}"), Ok(Each::default()));
        assert_eq!(
            parse_object::<Each>(br#"{"other": 1, "more": [{}]}"#),
            Ok(Each::default())
        );
    }

    /// Each wrong type in each field. A field is empty for a value of a
    /// wrong type, and the document stays an answer.
    #[test]
    fn a_document_with_each_wrong_type_reads_each_field_as_empty() {
        let empty = Each::default();
        let read = |value: &str| parse_object::<Each>(&each_field_is(value)).unwrap();

        assert_eq!(read("null"), empty);
        assert_eq!(read("false"), empty);
        assert_eq!(
            read("true"),
            Each {
                flag: true,
                ..Each::default()
            }
        );
        assert_eq!(
            read("7"),
            Each {
                int: whole(7),
                number: Some(7.0),
                ..Each::default()
            }
        );
        assert_eq!(
            read("1.5"),
            Each {
                number: Some(1.5),
                ..Each::default()
            }
        );
        assert_eq!(
            read(r#""true""#),
            Each {
                text: "true".to_owned(),
                text_or_none: Some("true".to_owned()),
                ..Each::default()
            }
        );
        assert_eq!(
            read(r#"["a",7,"b","c"]"#),
            Each {
                list: vec!["a".to_owned(), "b".to_owned(), "c".to_owned()],
                first: vec!["a".to_owned()],
                ..Each::default()
            }
        );
        assert_eq!(
            read(r#"{"name":"n","count":true}"#),
            Each {
                object: part("n", 0),
                block: Some(part("n", 0)),
                ..Each::default()
            }
        );
    }

    #[test]
    fn a_document_is_one_json_object() {
        let not_object = ["[1]", "7", "1.5", r#""a""#, "true", "null", " [ ] "];
        let not_json = [
            "",
            "   ",
            r#"{"a": "#,
            "nope",
            r#"{"a": 1}{"b": 2}"#,
            r#"{"a": 1,}"#,
            "{'a': 1}",
            r#"{"a": 1} x"#,
            r#"{"a": 01}"#,
            "{\"a\": \"\u{1}\"}",
        ];

        assert_eq!(parse_object::<Each>(b" \n{}\t\r\n"), Ok(Each::default()));
        for input in not_object {
            let read = parse_object::<Each>(input.as_bytes());

            assert_eq!(read, Err(NotAnObject::NotObject), "{input}");
        }
        for input in not_json {
            let read = parse_object::<Each>(input.as_bytes());

            assert_eq!(read, Err(NotAnObject::NotJson), "{input}");
        }
    }

    /// The kind of the document comes first. A raw type that takes another
    /// kind gets no document of that kind.
    #[test]
    fn a_document_of_another_kind_is_no_answer_for_a_raw_type_that_takes_it() {
        assert_eq!(
            parse_object::<Vec<i64>>(b"[1]"),
            Err(NotAnObject::NotObject)
        );
        assert_eq!(
            parse_object::<String>(br#""a""#),
            Err(NotAnObject::NotObject)
        );
        assert_eq!(
            parse_object::<Option<Part>>(b"null"),
            Err(NotAnObject::NotObject)
        );
        assert_eq!(
            parse_object::<Option<Part>>(br#"{"name":"a"}"#),
            Ok(Some(part("a", 0)))
        );
    }

    /// What `json.loads` of Python reads and `serde_json` refuses. The rule
    /// holds in a member that the raw type does not read, too.
    #[test]
    fn a_document_is_strict_json_in_each_member() {
        let deep = format!(r#"{{"other":{}{}}}"#, "[".repeat(127), "]".repeat(127));
        let past_every_float = format!(r#"{{"other":{}}}"#, "9".repeat(400));
        let refused: [&[u8]; 14] = [
            br#"{"text": NaN}"#,
            br#"{"other": NaN}"#,
            br#"{"other": [Infinity, -Infinity]}"#,
            br#"{"other": 1e400}"#,
            past_every_float.as_bytes(),
            br#"{"text": "\ud800"}"#,
            br#"{"other": "\ud800"}"#,
            br#"{"\udc00": 1}"#,
            // The bytes of one half of a surrogate pair, which are no UTF-8.
            b"{\"other\": \"\xed\xa0\x80\"}",
            b"{\"other\": \"\xff\"}",
            // The same bytes as the one member of a file.
            b"{\"a\": \"\xed\xa0\x80\"}",
            // One byte that is not UTF-8 between two letters of an answer.
            b"{\"field\": \"a\xffb\"}",
            // A byte order mark.
            b"\xef\xbb\xbf{}",
            deep.as_bytes(),
        ];

        for input in refused {
            let read = parse_object::<Each>(input);

            assert_eq!(read, Err(NotAnObject::NotJson), "{input:?}");
        }
    }

    #[test]
    fn a_document_nests_127_levels_at_most() {
        let nested =
            |levels: usize| format!("{}1{}", r#"{"object":"#.repeat(levels), "}".repeat(levels));

        assert!(parse_object::<Each>(nested(DEPTH_MAX - 1).as_bytes()).is_ok());
        assert_eq!(
            parse_object::<Each>(nested(DEPTH_MAX).as_bytes()),
            Err(NotAnObject::NotJson)
        );
    }

    #[test]
    fn a_document_keeps_the_last_value_of_a_key() {
        let document = br#"{"text": "first", "int": 1, "text": 7, "int": 2, "text": "last"}"#;
        let wanted = Each {
            text: "last".to_owned(),
            int: whole(2),
            ..Each::default()
        };

        assert_eq!(parse_object::<Each>(document), Ok(wanted));
        // The derived type alone refuses the document.
        assert!(serde_json::from_slice::<Each>(document).is_err());
    }

    #[test]
    fn a_document_that_is_a_list_is_no_struct() {
        assert_eq!(parse_object::<Strict>(b"[7]"), Err(NotAnObject::NotObject));
        // The derived type alone fills each field by its place.
        assert_eq!(
            serde_json::from_slice::<Strict>(b"[7]").unwrap(),
            Strict { id: 7 }
        );
    }

    #[test]
    fn an_object_that_the_raw_type_refuses_is_not_the_object_of_the_answer() {
        assert_eq!(
            parse_object::<Strict>(br#"{"id": 7}"#),
            Ok(Strict { id: 7 })
        );
        assert_eq!(
            parse_object::<Strict>(br#"{"id": "7"}"#),
            Err(NotAnObject::NotObject)
        );
        assert_eq!(parse_object::<Strict>(b"{}"), Err(NotAnObject::NotObject));
    }

    /// `_json_object` of each door gives an empty object for each refusal.
    #[test]
    fn a_port_of_a_door_reads_each_refusal_as_the_empty_raw_type() {
        for input in ["nope", "[1]", "null", ""] {
            let read: Each = parse_object(input.as_bytes()).unwrap_or_default();

            assert_eq!(read, Each::default(), "{input}");
        }
    }

    /// What a port of a door reads from the lossy text of a body.
    fn lossy(body: &[u8]) -> Result<Each, NotAnObject> {
        parse_object(String::from_utf8_lossy(body).as_bytes())
    }

    /// A Python door reads the text that `httpx` makes from a body. A byte
    /// that is not UTF-8 is U+FFFD there. A port that gives the lossy text of
    /// the body reads what the door reads, and it refuses what the door
    /// refuses.
    #[test]
    fn a_port_of_a_door_keeps_the_door_reading_with_a_lossy_text() {
        let one_byte = b"{\"text\": \"a\xffb\"}";
        let half_a_pair = b"{\"text\": \"\xed\xa0\x80\"}";
        let refused: [&[u8]; 4] = [
            // A byte order mark.
            b"\xef\xbb\xbf{}",
            // UTF-16 with a mark, and with none.
            b"\xff\xfe{\x00}\x00",
            b"{\x00}\x00",
            // UTF-32 with no mark.
            b"{\x00\x00\x00}\x00\x00\x00",
        ];

        assert_eq!(parse_object::<Each>(one_byte), Err(NotAnObject::NotJson));
        assert_eq!(lossy(one_byte).unwrap().text, "a\u{fffd}b");
        assert_eq!(parse_object::<Each>(half_a_pair), Err(NotAnObject::NotJson));
        assert_eq!(lossy(half_a_pair).unwrap().text, "\u{fffd}\u{fffd}\u{fffd}");
        for body in refused {
            assert_eq!(lossy(body), Err(NotAnObject::NotJson), "{body:?}");
        }
    }

    // --- the inputs that left the vectors ---

    /// A cap above the size of each input of the three tests below.
    const SAMPLE_CAP: json::ByteCap = json::ByteCap::new(1024);

    /// The rule of the strict reader of this crate that one text breaks.
    fn rule_against(text: &str) -> json::Rule {
        json::check(text.as_bytes(), SAMPLE_CAP).unwrap_err().rule()
    }

    /// Whether each field reader gives the error of the deserializer for
    /// one JSON text.
    fn each_reader_refuses(value: &str) -> bool {
        direct(value, |reader| text(reader)).is_err()
            && direct(value, |reader| text_or_none(reader)).is_err()
            && direct(value, |reader| int(reader)).is_err()
            && direct(value, |reader| number(reader)).is_err()
            && direct(value, |reader| flag(reader)).is_err()
            && direct(value, |reader| object::<_, Part>(reader)).is_err()
            && direct(value, |reader| block::<_, Part>(reader)).is_err()
            && direct(value, |reader| list::<_, String>(reader)).is_err()
            && direct(value, |reader| list_first::<2, _, String>(reader)).is_err()
    }

    /// Six inputs that left each surface `runtime.untrusted.*`, with the id
    /// that each one had there. None is strict JSON: the strict reader of
    /// this crate refuses each one for the rule of its row. A Python helper
    /// got the value that `json.loads` reads from such a text, and it gave a
    /// value for it. Each reader of this module refuses the text, alone and
    /// as a member of an object.
    #[test]
    fn six_inputs_that_left_the_vectors_are_no_json_for_each_reader() {
        let digits_400 = "9".repeat(400);
        let inputs = [
            ("float-nan", "NaN", json::Rule::Constant),
            ("float-infinity", "Infinity", json::Rule::Constant),
            ("float-negative-infinity", "-Infinity", json::Rule::Constant),
            ("float-too-large", "1e400", json::Rule::FloatRange),
            (
                "integer-400-digits",
                digits_400.as_str(),
                json::Rule::IntegerRange,
            ),
            (
                "string-lone-surrogate",
                r#""\ud800""#,
                json::Rule::LoneSurrogate,
            ),
        ];

        for (id, value, rule) in inputs {
            let document = each_field_is(value);

            assert_eq!(rule_against(value), rule, "{id}");
            assert!(each_reader_refuses(value), "{id}");
            assert_eq!(
                parse_object::<Each>(&document),
                Err(NotAnObject::NotJson),
                "{id}"
            );
        }
    }

    /// Three more inputs that left those surfaces: an integer outside the
    /// range of 64 bits. The strict reader of this crate refuses each one.
    /// `serde_json` reads such an integer as the nearest float. `number`
    /// gives that float, as `number` of the noticeboard does. Each other
    /// reader gives its empty value. `field_int` of the terminal door and
    /// `integer` of the noticeboard keep each digit of the integer.
    #[test]
    fn three_integers_that_left_the_vectors_read_as_a_float() {
        let inputs = [
            (
                "integer-i64-min-minus-one",
                "-9223372036854775809",
                -9_223_372_036_854_775_808.0_f64,
            ),
            (
                "integer-u64-max-plus-one",
                "18446744073709551616",
                18_446_744_073_709_551_616.0_f64,
            ),
            (
                "integer-30-digits",
                "123456789012345678901234567890",
                1.234_567_890_123_456_8e29_f64,
            ),
        ];

        for (id, value, float) in inputs {
            let wanted = Each {
                number: Some(float),
                ..Each::default()
            };

            assert_eq!(rule_against(value), json::Rule::IntegerRange, "{id}");
            assert_eq!(
                parse_object::<Each>(&each_field_is(value)),
                Ok(wanted),
                "{id}"
            );
        }
    }

    /// The tenth input that left those surfaces, `key-two-times`: an object
    /// with its key two times. The strict reader of this crate refuses it.
    /// `parse_object` keeps the last value of the key, as `json.loads` does,
    /// so a reader of the field gets that value.
    #[test]
    fn a_key_two_times_left_the_vectors_and_reads_as_its_last_value() {
        /// A raw type with the one field of the input.
        #[derive(Deserialize)]
        struct OneField {
            #[serde(default, deserialize_with = "text")]
            field: String,
        }

        let twice = r#"{"field":"first","field":"last"}"#;
        let read: OneField = parse_object(twice.as_bytes()).unwrap();

        assert_eq!(rule_against(twice), json::Rule::DuplicateKey);
        assert_eq!(read.field, "last");
    }

    // --- the tree of one value ---

    fn tree(input: &str) -> Json {
        direct(input, |reader| read(reader)).unwrap()
    }

    /// One JSON text of each kind, and the edges of a kind.
    const EACH_KIND: [&str; 22] = [
        "null",
        "true",
        "false",
        "0",
        "7",
        "-3",
        "255",
        "256",
        "9223372036854775807",
        "18446744073709551615",
        "18446744073709551616",
        "1.5",
        "-0.0",
        r#""""#,
        r#""a""#,
        r#""plain""#,
        "[]",
        "[7]",
        r#"["a",7]"#,
        "{}",
        r#"{"a":7}"#,
        r#"{"named":"a"}"#,
    ];

    /// Makes sure that a raw type reads the same value from the tree and from
    /// `serde_json` itself, and that the two refuse the same texts.
    fn reads_as_serde_json_reads<T>(input: &str)
    where
        T: DeserializeOwned + PartialEq + fmt::Debug,
    {
        let from_tree = T::deserialize(tree(input)).ok();
        let from_text = serde_json::from_str::<T>(input).ok();

        assert_eq!(
            from_tree,
            from_text,
            "{input} as {}",
            std::any::type_name::<T>()
        );
    }

    /// The tree checks the kind of each request, as `serde_json` does. A raw
    /// type below a function of this module thus reads what it reads alone.
    #[test]
    fn a_raw_type_reads_from_the_tree_as_it_reads_from_serde_json() {
        type FreeForm = serde_json::Map<String, serde_json::Value>;

        for input in EACH_KIND {
            reads_as_serde_json_reads::<()>(input);
            reads_as_serde_json_reads::<bool>(input);
            reads_as_serde_json_reads::<u8>(input);
            reads_as_serde_json_reads::<i64>(input);
            reads_as_serde_json_reads::<u64>(input);
            reads_as_serde_json_reads::<f64>(input);
            reads_as_serde_json_reads::<char>(input);
            reads_as_serde_json_reads::<String>(input);
            reads_as_serde_json_reads::<Option<i64>>(input);
            reads_as_serde_json_reads::<Vec<i64>>(input);
            reads_as_serde_json_reads::<(String, i64)>(input);
            reads_as_serde_json_reads::<BTreeMap<String, i64>>(input);
            reads_as_serde_json_reads::<FreeForm>(input);
            reads_as_serde_json_reads::<serde_json::Value>(input);
            reads_as_serde_json_reads::<Wrapped>(input);
            reads_as_serde_json_reads::<Shape>(input);
            reads_as_serde_json_reads::<NoSign>(input);
            reads_as_serde_json_reads::<Signed>(input);
        }
    }

    /// A visitor with one visit for a number: `visit_u64`. A visitor of the
    /// standard types has each visit, and a visitor that a person writes can
    /// have one.
    struct OnlyNoSign;

    impl Visitor<'_> for OnlyNoSign {
        type Value = u64;

        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("a whole number with no sign")
        }

        fn visit_u64<E>(self, value: u64) -> Result<u64, E> {
            Ok(value)
        }
    }

    /// A raw type that reads only through `visit_u64`.
    #[derive(Debug, PartialEq)]
    struct NoSign(u64);

    impl<'de> Deserialize<'de> for NoSign {
        fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            deserializer.deserialize_u64(OnlyNoSign).map(Self)
        }
    }

    /// A visitor with one visit for a number: `visit_i64`.
    struct OnlySigned;

    impl Visitor<'_> for OnlySigned {
        type Value = i64;

        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("a whole number with a sign")
        }

        fn visit_i64<E>(self, value: i64) -> Result<i64, E> {
            Ok(value)
        }
    }

    /// A raw type that reads only through `visit_i64`.
    #[derive(Debug, PartialEq)]
    struct Signed(i64);

    impl<'de> Deserialize<'de> for Signed {
        fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            deserializer.deserialize_i64(OnlySigned).map(Self)
        }
    }

    /// `serde_json` gives an integer that is not negative to `visit_u64` and
    /// a negative integer to `visit_i64`. The tree does the same, so a list
    /// drops no member that the raw type reads alone.
    #[test]
    fn an_integer_goes_to_the_visit_that_serde_json_calls() {
        let input = "[7,0,-3,18446744073709551615,9223372036854775807,-9223372036854775808]";
        let no_sign: Vec<NoSign> = direct(input, |reader| list(reader)).unwrap();
        let signed: Vec<Signed> = direct(input, |reader| list(reader)).unwrap();

        assert_eq!(
            no_sign,
            [
                NoSign(7),
                NoSign(0),
                NoSign(u64::MAX),
                NoSign(9_223_372_036_854_775_807)
            ]
        );
        assert_eq!(signed, [Signed(-3), Signed(i64::MIN)]);
        for member in ["7", "0", "18446744073709551615"] {
            assert!(serde_json::from_str::<NoSign>(member).is_ok(), "{member}");
            assert!(serde_json::from_str::<Signed>(member).is_err(), "{member}");
        }
        for member in ["-3", "-9223372036854775808"] {
            assert!(serde_json::from_str::<NoSign>(member).is_err(), "{member}");
            assert!(serde_json::from_str::<Signed>(member).is_ok(), "{member}");
        }
    }

    /// A raw type with a struct inside `#[serde(untagged)]`.
    #[derive(Debug, PartialEq, Deserialize)]
    #[serde(untagged)]
    enum Loose {
        Held(Strict),
    }

    /// The differences from `serde_json` that the module documentation lists
    /// for a number.
    #[test]
    fn a_wide_integer_and_a_narrow_float_differ_from_serde_json() {
        // An integer past 64 bits is a float in the tree.
        const PAST_64_BITS: [&str; 2] = ["18446744073709551616", "-9223372036854775809"];

        for input in EACH_KIND {
            reads_as_serde_json_reads::<f32>(input);
            if !PAST_64_BITS.contains(&input) {
                reads_as_serde_json_reads::<u128>(input);
                reads_as_serde_json_reads::<i128>(input);
            }
        }

        assert_eq!(
            serde_json::from_str::<u128>("18446744073709551616").unwrap(),
            18_446_744_073_709_551_616
        );
        assert_eq!(
            u128::deserialize(tree("18446744073709551616")),
            Err(Mismatch)
        );
        assert_eq!(
            serde_json::from_str::<i128>("-9223372036854775809").unwrap(),
            -9_223_372_036_854_775_809
        );
        assert_eq!(
            i128::deserialize(tree("-9223372036854775809")),
            Err(Mismatch)
        );

        // A number outside the range of an `f32`.
        assert!(serde_json::from_str::<f32>("1e39").is_err());
        assert_eq!(f32::deserialize(tree("1e39")), Ok(f32::INFINITY));

        // A text a little above the middle of two `f32` values. The nearest
        // `f64` is the middle itself, and the tie then goes to the even one.
        let above_the_middle = "1.0000000596046447753906251";
        let alone = serde_json::from_str::<f32>(above_the_middle).unwrap();
        let below_a_reader = f32::deserialize(tree(above_the_middle)).unwrap();

        assert_eq!(alone.to_bits(), 1.000_000_1_f32.to_bits());
        assert_eq!(below_a_reader.to_bits(), 1.0_f32.to_bits());
    }

    /// `serde` reads an enum with `#[serde(untagged)]` from a buffer of its
    /// own. A struct that a variant holds reads from a list there, from the
    /// tree and from `serde_json` alike.
    #[test]
    fn a_struct_inside_an_untagged_enum_reads_from_a_list() {
        let held = |id: u64| Loose::Held(Strict { id });
        let members: Vec<Loose> = direct(r#"[[7],{"id":8},[]]"#, |reader| list(reader)).unwrap();

        assert_eq!(Loose::deserialize(tree("[7]")), Ok(held(7)));
        assert_eq!(serde_json::from_str::<Loose>("[7]").unwrap(), held(7));
        assert_eq!(Loose::deserialize(tree(r#"{"id":7}"#)), Ok(held(7)));
        assert_eq!(members, [held(7), held(8)]);
    }

    /// A visitor that takes each kind of value. It gives the name of the
    /// kind that it got.
    struct TakesEach;

    impl<'de> Visitor<'de> for TakesEach {
        type Value = &'static str;

        fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            f.write_str("a value of each kind")
        }

        fn visit_unit<E>(self) -> Result<&'static str, E> {
            Ok("null")
        }

        fn visit_bool<E>(self, _: bool) -> Result<&'static str, E> {
            Ok("bool")
        }

        fn visit_i64<E>(self, _: i64) -> Result<&'static str, E> {
            Ok("number")
        }

        fn visit_u64<E>(self, _: u64) -> Result<&'static str, E> {
            Ok("number")
        }

        fn visit_f64<E>(self, _: f64) -> Result<&'static str, E> {
            Ok("number")
        }

        fn visit_str<E>(self, _: &str) -> Result<&'static str, E> {
            Ok("text")
        }

        fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<&'static str, A::Error> {
            while seq.next_element::<de::IgnoredAny>()?.is_some() {}

            Ok("list")
        }

        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<&'static str, A::Error> {
            while map
                .next_entry::<de::IgnoredAny, de::IgnoredAny>()?
                .is_some()
            {}

            Ok("object")
        }
    }

    /// One request of a raw type, and the kinds that it takes.
    type Request = (
        &'static str,
        fn(Json) -> Result<&'static str, Mismatch>,
        &'static [&'static str],
    );

    const REQUESTS: &[Request] = &[
        ("unit", |value| value.deserialize_unit(TakesEach), &["null"]),
        (
            "unit_struct",
            |value| value.deserialize_unit_struct("", TakesEach),
            &["null"],
        ),
        ("bool", |value| value.deserialize_bool(TakesEach), &["bool"]),
        ("i8", |value| value.deserialize_i8(TakesEach), &["number"]),
        ("i16", |value| value.deserialize_i16(TakesEach), &["number"]),
        ("i32", |value| value.deserialize_i32(TakesEach), &["number"]),
        ("i64", |value| value.deserialize_i64(TakesEach), &["number"]),
        (
            "i128",
            |value| value.deserialize_i128(TakesEach),
            &["number"],
        ),
        ("u8", |value| value.deserialize_u8(TakesEach), &["number"]),
        ("u16", |value| value.deserialize_u16(TakesEach), &["number"]),
        ("u32", |value| value.deserialize_u32(TakesEach), &["number"]),
        ("u64", |value| value.deserialize_u64(TakesEach), &["number"]),
        (
            "u128",
            |value| value.deserialize_u128(TakesEach),
            &["number"],
        ),
        ("f32", |value| value.deserialize_f32(TakesEach), &["number"]),
        ("f64", |value| value.deserialize_f64(TakesEach), &["number"]),
        ("char", |value| value.deserialize_char(TakesEach), &["text"]),
        ("str", |value| value.deserialize_str(TakesEach), &["text"]),
        (
            "string",
            |value| value.deserialize_string(TakesEach),
            &["text"],
        ),
        (
            "identifier",
            |value| value.deserialize_identifier(TakesEach),
            &["text"],
        ),
        (
            "bytes",
            |value| value.deserialize_bytes(TakesEach),
            &["text", "list"],
        ),
        (
            "byte_buf",
            |value| value.deserialize_byte_buf(TakesEach),
            &["text", "list"],
        ),
        ("seq", |value| value.deserialize_seq(TakesEach), &["list"]),
        (
            "tuple",
            |value| value.deserialize_tuple(1, TakesEach),
            &["list"],
        ),
        (
            "tuple_struct",
            |value| value.deserialize_tuple_struct("", 1, TakesEach),
            &["list"],
        ),
        ("map", |value| value.deserialize_map(TakesEach), &["object"]),
        (
            "struct",
            |value| value.deserialize_struct("", &[], TakesEach),
            &["object"],
        ),
        (
            "any",
            |value| value.deserialize_any(TakesEach),
            &["null", "bool", "number", "text", "list", "object"],
        ),
    ];

    /// One JSON text of each kind, with the name of the kind.
    const KINDS: &[(&str, &str)] = &[
        ("null", "null"),
        ("true", "bool"),
        ("7", "number"),
        ("-3", "number"),
        ("18446744073709551615", "number"),
        ("1.5", "number"),
        (r#""a""#, "text"),
        ("[]", "list"),
        ("[1]", "list"),
        ("{}", "object"),
        (r#"{"a":1}"#, "object"),
    ];

    /// A visitor gets a value only of a kind that its request takes, also
    /// when the visitor itself takes each kind.
    #[test]
    fn each_request_of_a_raw_type_takes_its_kinds_and_no_other_kind() {
        for (request, ask, taken) in REQUESTS {
            for (input, kind) in KINDS {
                let wanted = if taken.contains(kind) {
                    Ok(*kind)
                } else {
                    Err(Mismatch)
                };

                assert_eq!(
                    ask(tree(input)),
                    wanted,
                    "{input} for a request of {request}"
                );
            }
        }
    }

    /// The visitor of `serde_json::Map` reads `null` as an empty object. The
    /// request of the type is for an object, so the tree gives it no `null`.
    #[test]
    fn an_object_of_free_form_is_no_null() {
        type FreeForm = serde_json::Map<String, serde_json::Value>;

        let read: Vec<FreeForm> = direct(r#"[null,{},{"a":1}]"#, |reader| list(reader)).unwrap();

        assert_eq!(read.len(), 2);
        assert_eq!(read[0].len(), 0);
        assert_eq!(read[1]["a"], 1);
        assert!(FreeForm::deserialize(Json::Null).is_err());
    }

    /// A difference from `serde_json`: a struct with named fields reads from
    /// an object only.
    #[test]
    fn a_struct_with_named_fields_reads_from_no_list() {
        assert_eq!(Strict::deserialize(tree("[7]")), Err(Mismatch));
        assert_eq!(
            Strict::deserialize(tree(r#"{"id":7}"#)),
            Ok(Strict { id: 7 })
        );
        assert_eq!(
            serde_json::from_str::<Strict>("[7]").unwrap(),
            Strict { id: 7 }
        );
    }

    /// The tree keeps no JSON text. A raw type that holds the text of a
    /// member thus refuses each value below a function of this module.
    #[test]
    fn a_raw_type_that_keeps_the_text_of_a_member_refuses_each_value() {
        type Kept = Box<serde_json::value::RawValue>;

        assert_eq!(
            serde_json::from_str::<Kept>("[1, 2]").unwrap().get(),
            "[1, 2]"
        );
        assert!(Kept::deserialize(tree("[1, 2]")).is_err());
        assert!(Kept::deserialize(tree(r#""a""#)).is_err());
    }

    #[test]
    fn the_tree_holds_each_kind_of_value() {
        let input =
            r#"[null,true,false,7,-3,9223372036854775807,9223372036854775808,1.5,"a",[],{}]"#;
        let wanted = Json::List(vec![
            Json::Null,
            Json::Bool(true),
            Json::Bool(false),
            Json::Int(7),
            Json::Int(-3),
            Json::Int(i64::MAX),
            Json::Large(9_223_372_036_854_775_808),
            Json::Float(1.5),
            Json::Text("a".to_owned()),
            Json::List(Vec::new()),
            Json::Object(Vec::new()),
        ]);

        assert_eq!(tree(input), wanted);
    }

    #[test]
    fn the_tree_keeps_the_last_value_of_a_key_at_its_first_place() {
        let wanted = Json::Object(vec![
            ("a".to_owned(), Json::Int(3)),
            ("b".to_owned(), Json::Int(2)),
            (String::new(), Json::Null),
        ]);

        assert_eq!(tree(r#"{"a":1,"b":2,"a":3,"":null}"#), wanted);
    }

    #[test]
    fn a_tree_reads_again_as_the_same_tree() {
        let input = r#"{"a":[1,{"b":[null,true,1.5,"c",18446744073709551615]}],"d":{}}"#;
        let first = tree(input);
        let again = read(first.clone()).unwrap();

        assert_eq!(again, first);
    }

    #[test]
    fn a_mismatch_holds_no_text_of_the_value() {
        let error = <Mismatch as de::Error>::custom("the content of an answer");

        assert_eq!(error, Mismatch);
        assert_eq!(
            error.to_string(),
            "the value is not what the raw type takes"
        );
    }

    // --- the nesting limit ---

    /// `levels` lists, each one inside the one before, around `null`.
    ///
    /// `serde_json` reads no text that nests as deep as the limit of this
    /// module. The test of the limit thus needs a deserializer with no limit
    /// of its own.
    struct Nest(usize);

    impl<'de> Deserializer<'de> for Nest {
        type Error = de::value::Error;

        fn deserialize_any<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Self::Error> {
            match self.0.checked_sub(1) {
                Some(inside) => visitor.visit_seq(Inside(Some(inside))),
                None => visitor.visit_unit(),
            }
        }

        forward_to_deserialize_any! {
            bool i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 f32 f64 char str string
            bytes byte_buf option unit unit_struct newtype_struct seq tuple
            tuple_struct map struct enum identifier ignored_any
        }
    }

    /// The one member of a list of `Nest`.
    struct Inside(Option<usize>);

    impl<'de> SeqAccess<'de> for Inside {
        type Error = de::value::Error;

        fn next_element_seed<S: DeserializeSeed<'de>>(
            &mut self,
            seed: S,
        ) -> Result<Option<S::Value>, Self::Error> {
            self.0
                .take()
                .map(|levels| seed.deserialize(Nest(levels)))
                .transpose()
        }
    }

    #[test]
    fn a_value_at_the_nesting_limit_reads() {
        let mut deepest = &read(Nest(DEPTH_MAX)).unwrap();
        let mut levels = 0;
        while let Json::List(members) = deepest {
            deepest = &members[0];
            levels += 1;
        }

        assert_eq!(levels, DEPTH_MAX);
        assert_eq!(deepest, &Json::Null);
        assert_eq!(text(Nest(DEPTH_MAX)).unwrap(), "");
        assert_eq!(list::<_, String>(Nest(DEPTH_MAX)).unwrap(), [""; 0]);
    }

    #[test]
    fn a_value_past_the_nesting_limit_is_an_error_of_the_deserializer() {
        for levels in [DEPTH_MAX + 1, DEPTH_MAX + 2, 1_000_000, usize::MAX] {
            let failed = [
                text(Nest(levels)).unwrap_err(),
                text_or_none(Nest(levels)).unwrap_err(),
                int(Nest(levels)).unwrap_err(),
                number(Nest(levels)).unwrap_err(),
                flag(Nest(levels)).unwrap_err(),
                object::<_, Part>(Nest(levels)).unwrap_err(),
                block::<_, Part>(Nest(levels)).unwrap_err(),
                list::<_, String>(Nest(levels)).unwrap_err(),
                list_first::<2, _, String>(Nest(levels)).unwrap_err(),
            ];

            for error in failed {
                assert_eq!(error.to_string(), TOO_DEEP, "{levels}");
            }
        }
    }

    #[test]
    fn the_message_of_the_nesting_limit_names_the_limit() {
        assert!(TOO_DEEP.contains(&DEPTH_MAX.to_string()));
    }

    /// The limit of `serde_json` is under the limit of this module, so a
    /// JSON text meets the limit of `serde_json` first.
    #[test]
    fn serde_json_stops_a_text_before_the_nesting_limit() {
        let lists = |levels: usize| format!("{}{}", "[".repeat(levels), "]".repeat(levels));
        let objects =
            |levels: usize| format!("{}1{}", r#"{"a":"#.repeat(levels), "}".repeat(levels));
        let read_last = DEPTH_MAX - 1;

        assert!(direct(&lists(read_last), |reader| read(reader)).is_ok());
        assert!(direct(&objects(read_last), |reader| read(reader)).is_ok());
        assert!(direct(&lists(DEPTH_MAX), |reader| read(reader)).is_err());
        assert!(direct(&objects(DEPTH_MAX), |reader| read(reader)).is_err());
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

    // --- each reader against the Python implementation ---

    /// The differential test: each vector of each surface
    /// `runtime.untrusted.*` and `runtime.parse_object.*`, against the
    /// functions of this module.
    ///
    /// `SURFACES` names each surface and the function that replays one vector
    /// of it. A vector outside `DEVIATIONS` must be equal: the Rust reader
    /// gives the value that the Python helper gives, and it refuses what the
    /// Python helper refuses.
    mod python {
        use std::collections::{BTreeMap, HashSet};

        use creche_vectors::{self as vectors, Outcome, Vector};
        use serde::Serialize;
        use serde_json::{Value, json};

        use super::super::*;
        use super::direct;

        /// The count of characters that the noticeboard keeps of one text:
        /// `MAX_TEXT_CHARS` of `noticeboard/src/noticeboard/jsonfiles.py:33`.
        const TEXT_CAP: usize = 500;

        /// The separator between the id of an input and its cap, in the id of
        /// a vector of a helper with a cap: `list.limit-2`.
        const CAP_MARK: &str = ".limit-";

        /// What a reader did with one input.
        #[derive(Debug, Clone, PartialEq)]
        enum Did {
            /// The reader gives this value.
            Gives(Value),
            /// The reader refuses the input. A field reader refuses with the
            /// error of the deserializer, and the caller then has no answer.
            Refuses,
        }

        /// Whether two results are equal, with the sign of a zero and the
        /// kind of a number. A `Value` alone reads `-0.0` as equal to `0.0`.
        fn same(left: &Did, right: &Did) -> bool {
            match (left, right) {
                (Did::Gives(left), Did::Gives(right)) => {
                    // The text of a value has the sign of a zero. The keys of
                    // an object are in sorted order in each text.
                    let (left_text, right_text) = (left.to_string(), right.to_string());

                    left == right && left_text == right_text
                }
                (Did::Refuses, Did::Refuses) => true,
                (Did::Gives(_), Did::Refuses) | (Did::Refuses, Did::Gives(_)) => false,
            }
        }

        fn json_of(text: &str) -> Value {
            serde_json::from_str(text).unwrap()
        }

        /// How the Rust reader of a surface stands for its Python helper.
        #[derive(Debug, Clone, Copy)]
        enum Stands {
            /// The reader gives the value that the helper gives.
            Same,
            /// The helper says if a value is of one kind. The module has no
            /// such function: a raw type names the reader of that kind. For
            /// `true` the reader gives the value itself. For `false` it gives
            /// this JSON value, the empty value of the reader.
            Kind(&'static str),
            /// The helper gives `None` for a value that is no list. The
            /// reader gives the empty list.
            NoneIsNoMember,
        }

        /// One surface, and how the Rust code replays one vector of it.
        struct Against {
            surface: &'static str,
            replay: fn(&Vector) -> Did,
            stands: Stands,
        }

        const fn equal(surface: &'static str, replay: fn(&Vector) -> Did) -> Against {
            Against {
                surface,
                replay,
                stands: Stands::Same,
            }
        }

        const fn kind(
            surface: &'static str,
            replay: fn(&Vector) -> Did,
            empty: &'static str,
        ) -> Against {
            Against {
                surface,
                replay,
                stands: Stands::Kind(empty),
            }
        }

        const fn no_member(surface: &'static str, replay: fn(&Vector) -> Did) -> Against {
            Against {
                surface,
                replay,
                stands: Stands::NoneIsNoMember,
            }
        }

        /// The prefix of the surfaces of the field readers.
        const READERS: &str = "runtime.untrusted.";

        /// The prefix of the surfaces of the reader of a whole document.
        const DOCUMENTS: &str = "runtime.parse_object.";

        const SURFACES: &[Against] = &[
            kind("runtime.untrusted.door_owui.is_object", any_block, "null"),
            kind("runtime.untrusted.door_owui.is_list", any_list, "[]"),
            equal("runtime.untrusted.door_owui.as_object", any_object),
            equal("runtime.untrusted.door_owui.as_text", any_text),
            equal("runtime.untrusted.door_owui.field_text", field_text),
            kind(
                "runtime.untrusted.door_trigger.is_object",
                any_block,
                "null",
            ),
            kind("runtime.untrusted.door_trigger.is_list", any_list, "[]"),
            equal("runtime.untrusted.door_trigger.as_object", any_object),
            equal("runtime.untrusted.door_trigger.as_text", any_text),
            equal("runtime.untrusted.door_trigger.field_text", field_text),
            kind("runtime.untrusted.door_tui.is_object", any_block, "null"),
            kind("runtime.untrusted.door_tui.is_list", any_list, "[]"),
            equal("runtime.untrusted.door_tui.as_object", any_object),
            equal("runtime.untrusted.door_tui.as_list", any_list),
            equal("runtime.untrusted.door_tui.as_text", any_text),
            equal("runtime.untrusted.door_tui.field_text", field_text),
            equal("runtime.untrusted.door_tui.field_int", field_int),
            equal("runtime.untrusted.noticeboard.text", field_text_cut),
            equal("runtime.untrusted.noticeboard.whole", field_text_cut),
            equal("runtime.untrusted.noticeboard.integer", field_int),
            equal("runtime.untrusted.noticeboard.number", field_number),
            equal("runtime.untrusted.noticeboard.flag", field_flag),
            equal("runtime.untrusted.noticeboard.block", field_block),
            equal("runtime.untrusted.noticeboard.child", field_object),
            equal("runtime.untrusted.noticeboard.children", field_children),
            equal("runtime.untrusted.noticeboard.strings", field_strings),
            equal("runtime.untrusted.attendance.as_object", any_block),
            no_member("runtime.untrusted.attendance.as_array", any_list),
            equal("runtime.parse_object.noticeboard", document),
        ];

        // --- how the test replays a vector ---

        /// An object of free form. It is a raw type of this test only: a
        /// contract type holds no `Value` (`rust/AGENTS.md`, rule 7).
        ///
        /// The type is not `serde_json::Map`. That type also reads `null`, as
        /// an empty object, so a list of it keeps a member that is `null`.
        type AnyObject = BTreeMap<String, Value>;

        fn gives<T: Serialize>(value: T) -> Did {
            Did::Gives(serde_json::to_value(value).unwrap())
        }

        /// The JSON text of the input of a vector.
        fn source(vector: &Vector) -> &str {
            vector.input().text().unwrap()
        }

        /// What a function gives for a vector whose input is one JSON value.
        /// The function reads from `serde_json` itself.
        fn of_value<T: Serialize>(read: Result<T, serde_json::Error>) -> Did {
            read.map_or(Did::Refuses, gives)
        }

        fn any_text(vector: &Vector) -> Did {
            of_value(direct(source(vector), |reader| text(reader)))
        }

        fn any_object(vector: &Vector) -> Did {
            of_value(direct(source(vector), |reader| {
                object::<_, AnyObject>(reader)
            }))
        }

        fn any_block(vector: &Vector) -> Did {
            of_value(direct(source(vector), |reader| {
                block::<_, AnyObject>(reader)
            }))
        }

        fn any_list(vector: &Vector) -> Did {
            of_value(direct(source(vector), |reader| list::<_, Value>(reader)))
        }

        /// A raw type with the one field `field`, which names one reader. A
        /// vector of a helper for one field holds an object with that key.
        macro_rules! probe {
            ($name:ident, $reader:literal, $held:ty) => {
                #[derive(Deserialize)]
                struct $name {
                    #[serde(default, deserialize_with = $reader)]
                    field: $held,
                }
            };
        }

        probe!(TextField, "text", String);
        probe!(NumberField, "number", Option<f64>);
        probe!(FlagField, "flag", bool);
        probe!(ObjectField, "object", AnyObject);
        probe!(BlockField, "block", Option<AnyObject>);
        probe!(TwoObjects, "list_first::<2, _, _>", Vec<AnyObject>);
        probe!(FiftyObjects, "list_first::<50, _, _>", Vec<AnyObject>);
        probe!(TwoTexts, "list_first::<2, _, _>", Vec<String>);
        probe!(FiftyTexts, "list_first::<50, _, _>", Vec<String>);

        /// The raw type for `int`. Its default is a function of the module:
        /// `json::Integer` has no `Default`.
        #[derive(Deserialize)]
        struct IntField {
            #[serde(default = "zero", deserialize_with = "int")]
            field: json::Integer,
        }

        /// What a raw type gives for a vector whose input is one object. The
        /// raw type reads through `parse_object`.
        fn of_field<P: DeserializeOwned, T: Serialize>(vector: &Vector, held: fn(P) -> T) -> Did {
            match parse_object::<P>(&vector.input().bytes().unwrap()) {
                Ok(probe) => gives(held(probe)),
                Err(NotAnObject::NotJson) => Did::Refuses,
                Err(NotAnObject::NotObject) => panic!("{}: the input is no object", vector.id()),
            }
        }

        fn field_text(vector: &Vector) -> Did {
            of_field(vector, |probe: TextField| probe.field)
        }

        /// `text` and `whole` of the noticeboard cut the text at the cap.
        fn field_text_cut(vector: &Vector) -> Did {
            of_field(vector, |probe: TextField| {
                cut(&probe.field, TEXT_CAP).to_owned()
            })
        }

        fn field_int(vector: &Vector) -> Did {
            of_field(vector, |probe: IntField| probe.field)
        }

        fn field_number(vector: &Vector) -> Did {
            of_field(vector, |probe: NumberField| probe.field)
        }

        fn field_flag(vector: &Vector) -> Did {
            of_field(vector, |probe: FlagField| probe.field)
        }

        fn field_object(vector: &Vector) -> Did {
            of_field(vector, |probe: ObjectField| probe.field)
        }

        fn field_block(vector: &Vector) -> Did {
            of_field(vector, |probe: BlockField| probe.field)
        }

        /// The cap of a vector of a helper with a cap: `params.limit`.
        fn cap_of(vector: &Vector) -> u64 {
            vector.field("params").unwrap()["limit"].as_u64().unwrap()
        }

        fn field_children(vector: &Vector) -> Did {
            match cap_of(vector) {
                2 => of_field(vector, |probe: TwoObjects| probe.field),
                50 => of_field(vector, |probe: FiftyObjects| probe.field),
                cap => panic!(
                    "{}: no raw type of this test has the cap {cap}",
                    vector.id()
                ),
            }
        }

        /// Each text of a list, cut at the cap of a text.
        fn cut_each(members: Vec<String>) -> Vec<String> {
            members
                .iter()
                .map(|member| cut(member, TEXT_CAP).to_owned())
                .collect()
        }

        /// `strings` of the noticeboard cuts each member at the cap of a text.
        fn field_strings(vector: &Vector) -> Did {
            match cap_of(vector) {
                2 => of_field(vector, |probe: TwoTexts| cut_each(probe.field)),
                50 => of_field(vector, |probe: FiftyTexts| cut_each(probe.field)),
                cap => panic!(
                    "{}: no raw type of this test has the cap {cap}",
                    vector.id()
                ),
            }
        }

        /// The object of a whole document, with each member as it is.
        fn document(vector: &Vector) -> Did {
            parse_object::<AnyObject>(&vector.input().bytes().unwrap()).map_or(Did::Refuses, gives)
        }

        /// What the Rust reader must give when the Python helper did what the
        /// vector holds.
        fn python_did(against: &Against, vector: &Vector, at: &str) -> Did {
            if vector.result() != Outcome::Accepted {
                return Did::Refuses;
            }

            let Some(value) = vector.value() else {
                panic!("{at}: the vector holds no value");
            };

            Did::Gives(match (against.stands, value) {
                (Stands::Same, value) => value.clone(),
                (Stands::Kind(_), Value::Bool(true)) => json_of(source(vector)),
                (Stands::Kind(empty), Value::Bool(false)) => json_of(empty),
                (Stands::Kind(_), other) => panic!("{at}: {other} is no answer of a check"),
                (Stands::NoneIsNoMember, Value::Null) => json!([]),
                (Stands::NoneIsNoMember, value) => value.clone(),
            })
        }

        // --- each difference on purpose ---

        /// How the Rust code differs from the Python code on one input.
        #[derive(Debug, Clone, Copy)]
        enum Differs {
            /// The Python code gives a value. The Rust code refuses the text.
            Refuses,
            /// Both give a value. The Rust code gives this JSON value.
            Gives(&'static str),
        }

        /// The surfaces that a decision holds for.
        #[derive(Debug, Clone, Copy)]
        enum Surfaces {
            /// These surfaces.
            Named(&'static [&'static str]),
        }

        impl Surfaces {
            fn hold(self, surface: &str) -> bool {
                let Self::Named(names) = self;

                names.contains(&surface)
            }
        }

        /// Where the Python side of a decision is. Each row that is left
        /// names vectors.
        #[derive(Clone, Copy)]
        enum At {
            /// Each vector with one of these ids, in each of the surfaces. For
            /// a helper with a cap, an id stands for the vector of each cap.
            Vectors(Surfaces, &'static [&'static str]),
        }

        /// One decision to differ from the Python code.
        struct Deviation {
            at: At,
            differs: Differs,
            /// The contract section that the decision reads.
            contract: &'static str,
            /// The decision, and its reason.
            decision: &'static str,
        }

        /// The surface of the reader of a whole document.
        const DOCUMENT: &[&str] = &["runtime.parse_object.noticeboard"];

        const STRICT_JSON: &str = "contract 02 §3 rule 3";

        const NOT_FINITE: &str = "The contract says that a body is JSON. JSON has no word for a \
            number that is not finite. json.loads of Python reads NaN, Infinity and -Infinity. \
            The Rust reader is serde_json, which refuses the text.";

        const LONE_SURROGATE: &str = "The contract says that a body is JSON. Python keeps one \
            half of a surrogate pair in a text, from an escape or from its bytes. A Rust text \
            cannot hold one, so serde_json refuses the text.";

        const BYTE_ORDER_MARK: &str = "The contract says that a body is JSON and names no \
            encoding. json.loads of Python reads UTF-8 that starts with a byte order mark. \
            serde_json refuses the mark.";

        const UTF_16: &str = "The contract says that a body is JSON and names no encoding. \
            json.loads of Python finds UTF-16 from the first bytes, with a byte order mark and \
            without. serde_json reads UTF-8 only.";

        const UTF_32: &str = "The contract says that a body is JSON and names no encoding. \
            json.loads of Python finds UTF-32 from the first bytes, with a byte order mark and \
            without. serde_json reads UTF-8 only.";

        const DEPTH_LIMIT: &str = "The contract gives no nesting limit. Python reads a text \
            until the recursion limit of the interpreter, which differs between two versions. \
            serde_json stops at 128 levels.";

        const PAST_64_BITS_IS_A_FLOAT: &str = "No difference in what the reader accepts. \
            Python keeps each digit of an integer. serde_json gives an integer past 64 bits as \
            the nearest float, and the raw type of this test keeps that float.";

        /// Each input on which the Rust code differs from the Python code on
        /// purpose. A vector outside this table must be equal.
        const DEVIATIONS: &[Deviation] = &[
            Deviation {
                at: At::Vectors(Surfaces::Named(DOCUMENT), &["nan", "infinity"]),
                differs: Differs::Refuses,
                contract: STRICT_JSON,
                decision: NOT_FINITE,
            },
            Deviation {
                at: At::Vectors(Surfaces::Named(DOCUMENT), &["lone-surrogate"]),
                differs: Differs::Refuses,
                contract: STRICT_JSON,
                decision: LONE_SURROGATE,
            },
            Deviation {
                at: At::Vectors(Surfaces::Named(DOCUMENT), &["byte-order-mark"]),
                differs: Differs::Refuses,
                contract: "contract 02 §3 rule 3, contract 05 §2",
                decision: BYTE_ORDER_MARK,
            },
            Deviation {
                at: At::Vectors(
                    Surfaces::Named(DOCUMENT),
                    &["utf16-with-mark", "utf16-little-endian", "utf16-big-endian"],
                ),
                differs: Differs::Refuses,
                contract: "contract 02 §3 rule 3, contract 05 §2",
                decision: UTF_16,
            },
            Deviation {
                at: At::Vectors(
                    Surfaces::Named(DOCUMENT),
                    &["utf32-with-mark", "utf32-little-endian", "utf32-big-endian"],
                ),
                differs: Differs::Refuses,
                contract: "contract 02 §3 rule 3, contract 05 §2",
                decision: UTF_32,
            },
            Deviation {
                at: At::Vectors(Surfaces::Named(DOCUMENT), &["nested-200"]),
                differs: Differs::Refuses,
                contract: STRICT_JSON,
                decision: DEPTH_LIMIT,
            },
            Deviation {
                at: At::Vectors(Surfaces::Named(DOCUMENT), &["integer-30-digits"]),
                differs: Differs::Gives(r#"{"a":1.2345678901234568e29}"#),
                contract: "contracts 02, 04 and 05",
                decision: PAST_64_BITS_IS_A_FLOAT,
            },
        ];

        /// The id of the input of a vector: the id with no cap.
        fn input_id(vector: &str) -> &str {
            vector
                .split_once(CAP_MARK)
                .map_or(vector, |(input, _)| input)
        }

        /// Each decision that covers one vector of one surface, with its
        /// place in the table and the id that it names.
        fn deviations_of(
            surface: &str,
            vector: &str,
        ) -> Vec<(usize, &'static str, &'static Deviation)> {
            let input = input_id(vector);

            DEVIATIONS
                .iter()
                .enumerate()
                .filter_map(|(place, deviation)| {
                    let At::Vectors(surfaces, vectors) = deviation.at;
                    let named = vectors.iter().find(|named| **named == input)?;

                    surfaces.hold(surface).then_some((place, *named, deviation))
                })
                .collect()
        }

        /// Each pair of a decision and a vector id that names one surface.
        fn deviations_in(surface: &str) -> HashSet<(usize, &'static str)> {
            DEVIATIONS
                .iter()
                .enumerate()
                .filter_map(|(place, deviation)| {
                    let At::Vectors(surfaces, vectors) = deviation.at;

                    surfaces
                        .hold(surface)
                        .then(|| vectors.iter().map(move |vector| (place, *vector)))
                })
                .flatten()
                .collect()
        }

        /// Makes sure that the Rust code differs from the Python code as the
        /// decision says, and in no other way.
        fn differs_as_decided(differs: Differs, python: &Did, rust: &Did, at: &str) {
            assert!(
                matches!(python, Did::Gives(_)),
                "{at}: the Python code refuses"
            );
            match differs {
                Differs::Refuses => assert_eq!(rust, &Did::Refuses, "{at}"),
                Differs::Gives(value) => {
                    let decided = Did::Gives(json_of(value));

                    assert!(!same(python, &decided), "{at}: the row names no difference");
                    assert!(same(rust, &decided), "{at}: {rust:?} is not {decided:?}");
                }
            }
        }

        /// Walks each vector of one surface. It prints the counts, and a run
        /// with `--nocapture` shows them.
        fn walk(against: &Against) {
            let surface = vectors::surface(against.surface).unwrap();
            let mut equal = 0;
            let mut deviated = HashSet::new();
            for vector in surface.vectors() {
                let at = format!("{} {}", against.surface, vector.id());
                let rust = (against.replay)(vector);
                let decisions = deviations_of(against.surface, vector.id());

                assert!(decisions.len() <= 1, "{at}: two rows name the vector");
                if let Some((place, named, deviation)) = decisions.first() {
                    let python = Did::Gives(vector.value().cloned().unwrap_or(Value::Null));

                    assert_eq!(vector.result(), Outcome::Accepted, "{at}: the Python code");
                    differs_as_decided(deviation.differs, &python, &rust, &at);
                    deviated.insert((*place, *named));
                } else if vector.result() == Outcome::Raised {
                    assert_eq!(rust, Did::Refuses, "{at}: the Python code raises");
                    equal += 1;
                } else {
                    let python = python_did(against, vector, &at);

                    assert!(same(&rust, &python), "{at}: {rust:?} is not {python:?}");
                    equal += 1;
                }
            }

            assert_eq!(
                deviated,
                deviations_in(against.surface),
                "{}: a row names a vector that the surface does not hold",
                against.surface
            );
            println!(
                "{}: {} vectors: {equal} equal, {} inputs that differ on purpose",
                against.surface,
                surface.vectors().len(),
                deviated.len()
            );
        }

        #[test]
        fn the_table_holds_each_surface_of_the_readers_one_time() {
            let listed: Vec<&str> = SURFACES.iter().map(|against| against.surface).collect();
            let unique: HashSet<&str> = listed.iter().copied().collect();
            let index = vectors::index().unwrap();
            let in_index: HashSet<&str> = index
                .iter()
                .map(|row| row.surface())
                .filter(|surface| surface.starts_with(READERS) || surface.starts_with(DOCUMENTS))
                .collect();

            assert_eq!(
                unique.len(),
                listed.len(),
                "the table holds one surface twice"
            );
            assert_eq!(unique, in_index);
        }

        #[test]
        fn each_deviation_names_a_surface_of_the_table_and_its_reason() {
            let listed: HashSet<&str> = SURFACES.iter().map(|against| against.surface).collect();
            for deviation in DEVIATIONS {
                let At::Vectors(Surfaces::Named(surfaces), vectors) = deviation.at;

                assert!(deviation.contract.starts_with("contract"));
                assert!(!deviation.decision.is_empty());
                assert!(!surfaces.is_empty() && !vectors.is_empty());
                for surface in surfaces {
                    assert!(listed.contains(surface), "{surface}");
                }
            }
        }

        /// The walk reads the table, so each surface of the table has a walk.
        #[test]
        fn each_vector_of_each_surface_is_what_the_python_code_does() {
            for against in SURFACES {
                walk(against);
            }
        }

        /// A test of the test: a row that names no difference fails.
        #[test]
        #[should_panic(expected = "the row names no difference")]
        fn a_row_that_names_no_difference_fails() {
            let python = Did::Gives(json!(0));

            differs_as_decided(Differs::Gives("0"), &python, &python, "a row");
        }

        /// A test of the test: a value differs from the same number with the
        /// other sign of zero and from the same number of the other kind.
        #[test]
        fn two_results_are_equal_only_with_the_same_text() {
            let gives = |text: &str| Did::Gives(json_of(text));

            assert!(same(&gives("0.0"), &gives("0.0")));
            assert!(!same(&gives("0.0"), &gives("-0.0")));
            assert!(!same(&gives("7"), &gives("7.0")));
            assert!(same(&gives(r#"{"a":1,"b":2}"#), &gives(r#"{"b":2,"a":1}"#)));
            assert!(!same(&gives("null"), &Did::Refuses));
            assert!(same(&Did::Refuses, &Did::Refuses));
        }
    }
}
