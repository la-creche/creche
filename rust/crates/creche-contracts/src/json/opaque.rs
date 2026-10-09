//! `Opaque`: a JSON value that the code keeps whole and does not read.
//!
//! Some fields of a contract hold a value that the platform passes on. No
//! code of the platform reads a member of it. Three examples are the detail
//! of a fault, the event that a playpen sends on a channel line and the
//! arguments of a tool call. `rust/AGENTS.md`, rule 7, calls such a field
//! opaque.
//!
//! The value holds one text: its compact form. The writer of this module
//! reads that text when it writes the value, so the value goes out in the
//! style of the whole document.

use std::cell::Cell;
use std::fmt;

use serde::de::{self, Deserialize, Deserializer};
use serde::ser::{self, Serialize, SerializeMap, SerializeSeq, Serializer};
use serde_json::value::RawValue;

use super::scan::{
    FALSE, NULL, TRUE, float_value, in_number, integer_value, is_integer, is_space, pass, string_at,
};
use super::{ByteCap, Charset, Found, KeyOrder, Layout, ReadError, Style, read, write};
use crate::slot::Slot;

/// What the error of `serde` says for a member that is not strict JSON. The
/// words hold no byte of the text.
const NOT_STRICT: &str = "a JSON value that is not strict";

/// What the error of a serializer says when the text of a value is not the
/// strict text that each constructor checks.
const BROKEN: &str = "an opaque value with a text that is not strict JSON";

/// What the error of a serializer says when the serializer wrote no item.
const NOT_WRITTEN: &str = "a serializer that wrote no item of an opaque value";

/// The style of the one text that a value holds: no white space, each
/// character as it is, and the keys in the order of the value.
const COMPACT: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);

/// The value of a field that a contract calls opaque: strict JSON of each
/// kind, which the code keeps whole.
///
/// A value exists only through [`Opaque::read`] or through its
/// `Deserialize`. Each of the two checks the text against each rule of the
/// module doc, so code that holds a value needs no second check. The value
/// then keeps its compact form and not the text that it came from. The
/// compact form is the text that [`write`](super::write) gives with
/// [`Layout::Compact`], [`Charset::Utf8`] and [`KeyOrder::AsGiven`]. It has
/// each key of an object in the order of the first text.
///
/// # How the value reads
///
/// The `Deserialize` takes the text of the value from `serde_json`, as a
/// `RawValue`. [`Number`](super::Number) reads in the same way and has the
/// same limits:
///
/// - A named field of a struct can have the type. So can an item of a list
///   and a value that a map visitor reads.
/// - A field below `#[serde(flatten)]` cannot have the type. The derive
///   gives such a field a value from a buffer of `serde`, and that buffer
///   has no text of the value. `serde_json` then reports an invalid type,
///   and the read of the whole struct fails.
/// - A named `Opaque` field works beside a flattened field. For example,
///   the flattened field can be a map that ignores the value of each other
///   key.
/// - Some raw types must keep each member that they have no field for. Such
///   a raw type needs a `Deserialize` that a person writes. Its `visit_map`
///   matches the keys that the type names, and it calls
///   `next_value::<Opaque>()` for each key that is left.
///
/// In a raw type, the field is a `Slot<Opaque>`. The slot is `Slot::Null`
/// for the word `null` and `Slot::Value` for a value of each other kind.
///
/// The `Deserialize` does the check again for its own text. It thus refuses
/// a member that is not strict JSON, also when no [`check`](super::check)
/// read the document first.
///
/// # How the value writes
///
/// The `Serialize` gives each part of the value to the serializer.
/// [`write`](super::write) then forms the value as it forms each other
/// value, so the text is the text of `json.dumps` in the given [`Style`]:
///
/// - A number has the form that Python gives it, and not the form of its
///   token. The writer gives `1.5` for the token `1.50`, and `1e+21` for
///   the token `1e21`.
/// - The characters of a token decide its kind, as for
///   [`Number`](super::Number). The token `-0` is the integer 0, and its
///   text is `0`. The token `-0.0` is a float, and its text stays `-0.0`.
/// - An integer keeps each digit.
/// - A string goes out with the escapes of the charset of the style.
/// - The keys of an object go out in the order of the text. With
///   [`KeyOrder::Sorted`], the writer sorts the keys of each object. A
///   digest over the arguments of a call takes that sorted form as its
///   input.
///
/// [`Opaque::compact_len`] gives the size of the value for a size rule.
///
/// # When two values are equal
///
/// Two values are equal when their compact forms are equal. Equal values
/// thus write the same bytes in each style, and values that are not equal
/// do not.
///
/// - White space is no part of a value: `[1, 2]` and `[1,2]` are equal.
/// - The form of a token is no part of a value: `1.50` and `1.5` are equal,
///   and so are `"A"` and `"A"`.
/// - The order of the keys is a part of a value: `{"a":1,"b":2}` and
///   `{"b":2,"a":1}` are not equal.
/// - The kind of a number is a part of a value: `1` and `1.0` are not
///   equal.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, Charset, Found, KeyOrder, Layout, Opaque, Style};
///
/// const CAP: ByteCap = ByteCap::new(4096);
///
/// let event = Opaque::read(br#" {"type": "usage", "cost": 1.50, "at": -0} "#, CAP)?;
/// assert_eq!(event.kind(), Found::Table);
/// assert_eq!(event.compact_len(), 34);
/// assert_eq!(event, Opaque::read(br#"{"type":"usage","cost":1.5,"at":0}"#, CAP)?);
///
/// let line = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);
/// assert_eq!(json::write(&event, line)?, br#"{"type":"usage","cost":1.5,"at":0}"#);
///
/// let sorted = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::Sorted);
/// assert_eq!(json::write(&event, sorted)?, br#"{"at":0,"cost":1.5,"type":"usage"}"#);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot give a text that proof:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, ByteCap, Charset, Found, KeyOrder, Layout, Opaque, Style};
///
/// let event = Opaque { text: "[1,]".into(), kind: Found::List };
/// ```
#[derive(Clone, PartialEq, Eq)]
pub struct Opaque {
    /// The compact form of the value. It is strict JSON: the writer gives
    /// it for a text that passed each rule.
    text: Box<str>,
    kind: Found,
}

impl Opaque {
    /// The value that `bytes` hold: [`check`](super::check) with the cap of
    /// the surface, and then the value. White space around the value is not
    /// a part of it.
    ///
    /// # Errors
    ///
    /// [`ReadError::NotStrict`] for a text that is not strict JSON.
    pub fn read(bytes: &[u8], cap: ByteCap) -> Result<Self, ReadError> {
        read(bytes, cap)
    }

    /// The value that has this text. `None` for a text that is not strict
    /// JSON.
    fn of_text(text: &str) -> Option<Self> {
        let kind = pass(text).ok()?;
        let compact = write(&Walk::from(text, 0), COMPACT).ok()?;

        Some(Self {
            text: String::from_utf8(compact).ok()?.into(),
            kind,
        })
    }

    /// The JSON type of the value. A number is [`Found::Integer`] when its
    /// token has no `.`, no `e` and no `E`.
    #[must_use]
    pub const fn kind(&self) -> Found {
        self.kind
    }

    /// How many bytes the compact form of the value has in UTF-8. A host
    /// holds the event of a channel line against its size limit with this
    /// count.
    ///
    /// The count is not the length of the text that the value came from.
    /// That text can hold white space, an escape for a character that needs
    /// none, or a number in another form.
    #[must_use]
    pub fn compact_len(&self) -> usize {
        self.text.len()
    }
}

/// The kind and the size of the compact form. The form shows no character
/// of the text, because an opaque value can hold a secret.
impl fmt::Debug for Opaque {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Opaque")
            .field("kind", &self.kind)
            .field("compact_len", &self.text.len())
            .finish()
    }
}

impl<'de> Deserialize<'de> for Opaque {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = <&'de RawValue>::deserialize(deserializer)?.get();

        Self::of_text(text).ok_or_else(|| de::Error::custom(NOT_STRICT))
    }
}

/// An opaque field of a raw type. The field takes a value of each kind, so
/// no value reads as `Other`: the word `null` reads as `Null`, and a value
/// of another kind reads as `Value`.
///
/// The read fails for a value that is not strict JSON. No such value is in
/// a text that [`check`](super::check) accepted.
impl<'de> Deserialize<'de> for Slot<Opaque> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Opaque::deserialize(deserializer)?;

        Ok(match value.kind {
            Found::Null => Self::Null,
            Found::Boolean
            | Found::Integer
            | Found::Float
            | Found::Text
            | Found::List
            | Found::Table => Self::Value(value),
        })
    }
}

/// Each part of the value, in the order of the text: an object as a map, an
/// array as a sequence, an integer as a `u64` or an `i64`, and a float as
/// an `f64`.
impl Serialize for Opaque {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        Walk::from(&self.text, 0).serialize(serializer)
    }
}

/// One value inside a strict text: the value that starts at `start`, after
/// optional white space.
///
/// The text is strict JSON, so the walk checks no rule again. For a text
/// that is not strict JSON, the walk gives an error and no panic. The walk
/// holds no copy of the grammar: the `scan` module gives it each byte set,
/// each word and the decode of a string.
///
/// `serde` gives a serializer a reference to a value, so the walk tells its
/// caller through a `Cell` where the value ends. The caller then finds the
/// next item with no second pass over the text.
struct Walk<'a> {
    text: &'a str,
    start: usize,
    /// The offset after the value. `None` until a serializer wrote it.
    end: Cell<Option<usize>>,
}

impl<'a> Walk<'a> {
    const fn from(text: &'a str, start: usize) -> Self {
        Self {
            text,
            start,
            end: Cell::new(None),
        }
    }

    /// The offset after this value, when a serializer wrote it.
    fn end<E: ser::Error>(&self) -> Result<usize, E> {
        self.end.get().ok_or_else(|| E::custom(NOT_WRITTEN))
    }

    fn byte(&self, at: usize) -> Option<u8> {
        self.text.as_bytes().get(at).copied()
    }

    /// The first offset from `at` that holds no white space.
    fn past_space(&self, mut at: usize) -> usize {
        while self.byte(at).is_some_and(is_space) {
            at += 1;
        }

        at
    }

    /// Gives the number that starts at `at` to `serializer`. The token is
    /// the run of the bytes that a number of JSON can hold.
    fn number<S: Serializer>(&self, at: usize, serializer: S) -> Result<S::Ok, S::Error> {
        let mut end = at;
        while self.byte(end).is_some_and(in_number) {
            end += 1;
        }
        self.end.set(Some(end));

        let token = self.text.get(at..end).unwrap_or_default();
        if !is_integer(token) {
            let float = float_value(token).ok_or_else(broken)?;

            return serializer.serialize_f64(float);
        }

        let integer = integer_value(token).ok_or_else(broken)?;
        if let Ok(value) = u64::try_from(integer) {
            return serializer.serialize_u64(value);
        }

        serializer.serialize_i64(i64::try_from(integer).map_err(|_| broken())?)
    }

    /// Gives the array whose bracket is at `at` to `serializer`.
    fn list<S: Serializer>(&self, at: usize, serializer: S) -> Result<S::Ok, S::Error> {
        let mut list = serializer.serialize_seq(None)?;
        let mut at = at + 1;
        loop {
            at = self.past_space(at);
            match self.byte(at) {
                Some(b']') => break,
                Some(b',') => at += 1,
                Some(_) => {
                    let item = Walk::from(self.text, at);
                    list.serialize_element(&item)?;
                    at = item.end()?;
                }
                None => return Err(broken()),
            }
        }
        self.end.set(Some(at + 1));

        list.end()
    }

    /// Gives the object whose bracket is at `at` to `serializer`.
    fn table<S: Serializer>(&self, at: usize, serializer: S) -> Result<S::Ok, S::Error> {
        let mut table = serializer.serialize_map(None)?;
        let mut at = at + 1;
        loop {
            at = self.past_space(at);
            match self.byte(at) {
                Some(b'}') => break,
                Some(b',') => at += 1,
                Some(b'"') => {
                    let (key, after_key) = string_at(self.text, at).ok_or_else(broken)?;
                    let colon = self.past_space(after_key);
                    if self.byte(colon) != Some(b':') {
                        return Err(broken());
                    }

                    let value = Walk::from(self.text, colon + 1);
                    table.serialize_entry(key.as_ref(), &value)?;
                    at = value.end()?;
                }
                Some(_) | None => return Err(broken()),
            }
        }
        self.end.set(Some(at + 1));

        table.end()
    }

    /// Gives one of the three words to `serializer`, when the text holds
    /// the word at `at`.
    fn word<S: Serializer>(
        &self,
        at: usize,
        word: &str,
        write: impl FnOnce(S) -> Result<S::Ok, S::Error>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        let end = at + word.len();
        if self.text.get(at..end) != Some(word) {
            return Err(broken());
        }
        self.end.set(Some(end));

        write(serializer)
    }
}

/// The error for a text that is not strict JSON. No constructor of
/// [`Opaque`] keeps such a text.
fn broken<E: ser::Error>() -> E {
    E::custom(BROKEN)
}

impl Serialize for Walk<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let at = self.past_space(self.start);
        match self.byte(at) {
            Some(b'{') => self.table(at, serializer),
            Some(b'[') => self.list(at, serializer),
            Some(b'"') => {
                let (text, end) = string_at(self.text, at).ok_or_else(broken)?;
                self.end.set(Some(end));

                serializer.serialize_str(&text)
            }
            Some(b't') => self.word(at, TRUE, |to| to.serialize_bool(true), serializer),
            Some(b'f') => self.word(at, FALSE, |to| to.serialize_bool(false), serializer),
            Some(b'n') => self.word(at, NULL, S::serialize_unit, serializer),
            Some(_) => self.number(at, serializer),
            None => Err(broken()),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde::Deserialize;
    use serde::de::{IgnoredAny, MapAccess, Visitor};

    use super::super::write::tests::SAMPLE_TEXTS;
    use super::super::{DEPTH_MAX, Rule, WriteError};
    use super::*;
    use crate::slot::tests::reads_empty_table;

    const CAP: ByteCap = ByteCap::new(4096);

    const COMPACT_ASCII: Style = Style::new(Layout::Compact, Charset::Ascii, KeyOrder::AsGiven);
    const SORTED: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::Sorted);
    const SPACED: Style = Style::new(Layout::Spaced, Charset::Ascii, KeyOrder::AsGiven);
    const INDENT2: Style = Style::new(Layout::Indent2, Charset::Ascii, KeyOrder::AsGiven);

    fn opaque(text: &str) -> Opaque {
        Opaque::read(text.as_bytes(), CAP).unwrap()
    }

    fn text_of<T: Serialize + ?Sized>(value: &T, style: Style) -> String {
        String::from_utf8(write(value, style).unwrap()).unwrap()
    }

    /// The compact text in UTF-8 that the value of `text` writes.
    fn compact(text: &str) -> String {
        text_of(&opaque(text), COMPACT)
    }

    #[test]
    fn a_value_has_the_kind_of_its_text() {
        let kinds = [
            ("null", Found::Null),
            ("true", Found::Boolean),
            ("false", Found::Boolean),
            ("7", Found::Integer),
            ("-0", Found::Integer),
            ("-0.0", Found::Float),
            ("1e3", Found::Float),
            ("\"7\"", Found::Text),
            ("[7]", Found::List),
            ("{\"count\": 7}", Found::Table),
            (" \t\r\n[7] \t\r\n", Found::List),
        ];

        for (text, kind) in kinds {
            assert_eq!(opaque(text).kind(), kind, "{text}");
        }
    }

    /// A number token, and what `json.dumps` gives for its value.
    const NUMBERS: [(&str, &str); 23] = [
        ("1.50", "1.5"),
        ("1e21", "1e+21"),
        ("-0", "0"),
        ("-0.0", "-0.0"),
        ("-0e0", "-0.0"),
        ("0", "0"),
        ("0.0", "0.0"),
        ("7", "7"),
        ("-7", "-7"),
        ("9007199254740993", "9007199254740993"),
        ("18446744073709551615", "18446744073709551615"),
        ("-9223372036854775808", "-9223372036854775808"),
        ("9007199254740993.0", "9007199254740992.0"),
        ("1E2", "100.0"),
        ("1e+2", "100.0"),
        ("0.1e1", "1.0"),
        ("1.0", "1.0"),
        ("12.340", "12.34"),
        ("4.0e3", "4000.0"),
        ("1e-5", "1e-05"),
        ("-1E-7", "-1e-07"),
        ("1e-999", "0.0"),
        ("1.7976931348623157e308", "1.7976931348623157e+308"),
    ];

    #[test]
    fn a_number_goes_out_in_the_form_of_python() {
        for (token, written) in NUMBERS {
            assert_eq!(compact(token), written, "{token}");
            assert_eq!(opaque(token).compact_len(), written.len(), "{token}");
        }

        let tokens: Vec<&str> = NUMBERS.iter().map(|(token, _)| *token).collect();
        let texts: Vec<&str> = NUMBERS.iter().map(|(_, written)| *written).collect();
        let list = opaque(&format!("[{}]", tokens.join(", ")));

        assert_eq!(text_of(&list, COMPACT), format!("[{}]", texts.join(",")));
        assert_eq!(text_of(&list, SPACED), format!("[{}]", texts.join(", ")));
        assert_eq!(
            text_of(&list, INDENT2),
            format!("[\n  {}\n]", texts.join(",\n  "))
        );
    }

    #[test]
    fn an_integer_and_a_float_differ_by_the_characters_of_the_token() {
        assert_eq!(compact("-0"), "0");
        assert_eq!(compact("-0.0"), "-0.0");
        assert_eq!(opaque("-0").kind(), Found::Integer);
        assert_eq!(opaque("-0.0").kind(), Found::Float);

        // `serde_json` alone writes the two tokens in one form.
        let alone = |text: &str| {
            let value: serde_json::Value = serde_json::from_str(text).unwrap();

            serde_json::to_string(&value).unwrap()
        };
        assert_eq!(alone("-0"), "-0.0");
        assert_eq!(alone("-0.0"), "-0.0");
    }

    const KEYED: &str = r#"{"zone": {"y": 1, "x": [{"b": null, "a": true}]}, "at": "now", "Zone": 0, "\u00e9": 1, "e": 2}"#;

    #[test]
    fn a_value_keeps_the_order_of_its_keys() {
        assert_eq!(
            compact(KEYED),
            "{\"zone\":{\"y\":1,\"x\":[{\"b\":null,\"a\":true}]},\"at\":\"now\",\"Zone\":0,\"\u{e9}\":1,\"e\":2}"
        );
        assert_eq!(opaque(KEYED).compact_len(), 75);
        assert_eq!(
            text_of(&opaque(KEYED), SPACED),
            r#"{"zone": {"y": 1, "x": [{"b": null, "a": true}]}, "at": "now", "Zone": 0, "\u00e9": 1, "e": 2}"#
        );
    }

    #[test]
    fn the_sorted_style_gives_the_sorted_form_of_a_value() {
        assert_eq!(
            text_of(&opaque(KEYED), SORTED),
            "{\"Zone\":0,\"at\":\"now\",\"e\":2,\"zone\":{\"x\":[{\"a\":true,\"b\":null}],\"y\":1},\"\u{e9}\":1}"
        );
    }

    #[test]
    fn a_string_goes_out_with_the_escapes_of_the_charset() {
        let text = "\"\\u0041\\ud83d\\ude00\\n\\u007f\\/\\u00e9 caf\u{e9} \\u2028\\\\\\\"\\u0000\\b\\f\\r\\t\\u001F\"";

        assert_eq!(
            compact(text),
            "\"A\u{1f600}\\n\u{7f}/\u{e9} caf\u{e9} \u{2028}\\\\\\\"\\u0000\\b\\f\\r\\t\\u001f\""
        );
        assert_eq!(opaque(text).compact_len(), 47);
        assert_eq!(
            text_of(&opaque(text), COMPACT_ASCII),
            "\"A\\ud83d\\ude00\\n\\u007f/\\u00e9 caf\\u00e9 \\u2028\\\\\\\"\\u0000\\b\\f\\r\\t\\u001f\""
        );
    }

    /// The value of the style table of the writer, as a text with white
    /// space, with escapes and with numbers in other forms.
    const SAMPLE: &str = " { \"b\" : [1, -2.50, \"caf\\u00e9 \\u007f\\n\", [ ], { }],\r\n\t\"a\": {\"d\": null, \"c\": true, \"\u{e9}\": false}, \"\\ud83d\\uDE00\": 1E21 } ";

    #[test]
    fn each_style_gives_the_bytes_of_json_dumps_for_a_value() {
        let sample = opaque(SAMPLE);

        for (layout, charset, key_order, text) in SAMPLE_TEXTS {
            let style = Style::new(layout, charset, key_order);

            assert_eq!(text_of(&sample, style), text, "{style:?}");
        }
    }

    #[test]
    fn the_compact_length_counts_the_bytes_of_the_compact_form() {
        let lengths = [
            ("null", 4),
            (" [ ] ", 2),
            ("{ }", 2),
            ("\"\\u00e9\"", 4),
            ("\"\\/\"", 3),
            ("[1.0e0, 2]", 7),
            ("{\"a\" : \"\\ud83d\\ude00\"}", 12),
            ("\"\\u007f\"", 3),
            ("\"\\u0001\"", 8),
            ("1e21", 5),
            ("-0", 1),
            (SAMPLE, 80),
        ];

        for (text, length) in lengths {
            let value = opaque(text);

            assert_eq!(value.compact_len(), length, "{text}");
            assert_eq!(write(&value, COMPACT).unwrap().len(), length, "{text}");
        }
    }

    #[test]
    fn the_compact_form_of_a_value_reads_back_as_the_same_form() {
        for text in [SAMPLE, KEYED, "[1.50, -0, 1e21]", "\"\\u0000\""] {
            let first = compact(text);

            assert_eq!(compact(&first), first);
            assert_eq!(opaque(&first).compact_len(), first.len());
        }
    }

    #[test]
    fn two_values_are_equal_when_their_compact_forms_are_equal() {
        let equal = [
            (" \t\r\n[1, 2] \t\r\n", "[1,2]"),
            (" 7 ", "7"),
            ("1.50", "1.5"),
            ("1e2", "100.0"),
            ("-0", "0"),
            ("\"\\u0041\\/\"", "\"A/\""),
            ("{ \"\\u0061\" : [ ] }", "{\"a\":[]}"),
            (SAMPLE, SAMPLE_TEXTS[2].3),
        ];
        let not_equal = [
            // The order of the keys is a part of a value.
            (r#"{"a":1,"b":2}"#, r#"{"b":2,"a":1}"#),
            // An integer and a float are two kinds.
            ("1", "1.0"),
            ("0", "-0.0"),
            ("\"1\"", "1"),
            ("[]", "{}"),
            ("null", "false"),
        ];

        for (one, other) in equal {
            assert_eq!(opaque(one), opaque(other), "{one}");
            for (layout, charset, key_order, _) in SAMPLE_TEXTS {
                let style = Style::new(layout, charset, key_order);

                assert_eq!(text_of(&opaque(one), style), text_of(&opaque(other), style));
            }
        }
        for (one, other) in not_equal {
            assert_ne!(opaque(one), opaque(other), "{one}");
            assert_ne!(compact(one), compact(other), "{one}");
        }
        assert_eq!(opaque(KEYED).clone(), opaque(KEYED));
    }

    #[test]
    fn a_value_holds_its_compact_form_and_no_other_text() {
        for text in [SAMPLE, KEYED, " [1.50, -0, 1e21] ", "\"\\u00e9\""] {
            let value = opaque(text);

            assert_eq!(&*value.text, compact(text));
            assert_eq!(value.compact_len(), compact(text).len());
            // The compact form passes the check of the reader by itself.
            assert_eq!(Opaque::read(value.text.as_bytes(), CAP), Ok(value));
        }
    }

    #[test]
    fn a_text_that_is_not_strict_is_refused() {
        let refused: [(&str, Rule); 9] = [
            ("", Rule::Syntax),
            ("[1,]", Rule::Syntax),
            ("[NaN]", Rule::Constant),
            ("[1] 2", Rule::TrailingData),
            (r#"{"a": 1, "a": 2}"#, Rule::DuplicateKey),
            (r#"["\ud800"]"#, Rule::LoneSurrogate),
            ("[18446744073709551616]", Rule::IntegerRange),
            ("[1e999]", Rule::FloatRange),
            ("\u{feff}[]", Rule::ByteOrderMark),
        ];

        for (text, rule) in refused {
            let error = Opaque::read(text.as_bytes(), CAP).unwrap_err();

            assert_eq!(
                error.not_strict().map(|refusal| refusal.rule()),
                Some(rule),
                "{text}"
            );
        }

        let large = Opaque::read(b"[1, 2]", ByteCap::new(5)).unwrap_err();
        assert_eq!(
            large.not_strict().map(|refusal| refusal.rule()),
            Some(Rule::TooLarge)
        );
        let bytes = Opaque::read(b"[\"\xff\"]", CAP).unwrap_err();
        assert_eq!(
            bytes.not_strict().map(|refusal| refusal.rule()),
            Some(Rule::NotUtf8)
        );
    }

    #[test]
    fn a_member_that_is_not_strict_fails_the_read_with_no_check() {
        let deep = format!("{}{}", "[".repeat(DEPTH_MAX + 1), "]".repeat(DEPTH_MAX + 1));
        let texts = [
            r#"{"a": 1, "a": 2}"#,
            r#"[{"a": 1, "a": 2}]"#,
            r#"["\ud800"]"#,
            "[18446744073709551616]",
            "[-9223372036854775809]",
            "[1e999]",
            deep.as_str(),
        ];

        for text in texts {
            assert!(serde_json::from_str::<Opaque>(text).is_err(), "{text}");
            assert!(
                serde_json::from_str::<Slot<Opaque>>(text).is_err(),
                "{text}"
            );
            assert!(
                serde_json::from_str::<Vec<Opaque>>(&format!("[1, {text}]")).is_err(),
                "{text}"
            );
        }

        // Each of them reads when the text is strict.
        assert!(serde_json::from_str::<Opaque>(r#"{"a": 1, "b": 2}"#).is_ok());
        assert!(serde_json::from_slice::<Opaque>(br#"["\ud83d\ude00"]"#).is_ok());
    }

    #[test]
    fn a_value_of_the_deepest_text_reads_and_writes() {
        let deepest = format!("{}{}", "[".repeat(DEPTH_MAX), "]".repeat(DEPTH_MAX));
        let tables = format!("{}1{}", "{\"a\":".repeat(DEPTH_MAX), "}".repeat(DEPTH_MAX));

        assert_eq!(compact(&deepest), deepest);
        assert_eq!(compact(&tables), tables);
        assert_eq!(text_of(&opaque(&tables), SORTED), tables);
    }

    #[test]
    fn a_value_that_makes_a_text_too_deep_has_no_json_form() {
        let deepest = opaque(&format!(
            "{}{}",
            "[".repeat(DEPTH_MAX),
            "]".repeat(DEPTH_MAX)
        ));
        let one_less = opaque(&format!(
            "{}{}",
            "[".repeat(DEPTH_MAX - 1),
            "]".repeat(DEPTH_MAX - 1)
        ));

        for style in [COMPACT, SORTED, INDENT2] {
            // One array or one object around the deepest value is level 65.
            assert_eq!(write(&[&deepest], style), Err(WriteError::NoJsonForm));
            assert_eq!(
                write(&BTreeMap::from([("event", &deepest)]), style),
                Err(WriteError::NoJsonForm)
            );

            // A text with one item on each line has more bytes than `CAP`.
            let written = write(&BTreeMap::from([("event", &one_less)]), style).unwrap();
            let again: BTreeMap<String, Opaque> = read(&written, ByteCap::new(65_536)).unwrap();
            assert_eq!(again.get("event"), Some(&one_less));
        }
    }

    #[test]
    fn the_debug_form_holds_no_byte_of_the_text() {
        let value = opaque(r#" {"token": "hunter2"} "#);

        assert_eq!(
            format!("{value:?}"),
            "Opaque { kind: Table, compact_len: 19 }"
        );
    }

    #[test]
    fn a_value_gives_its_parts_to_each_serializer() {
        let value = opaque(r#"{"b": [1.50, -0, 2.5e-1, "x", null, true], "a": -7}"#);

        // The form of `serde_json` for the same parts.
        assert_eq!(
            serde_json::to_string(&value).unwrap(),
            r#"{"b":[1.5,0,0.25,"x",null,true],"a":-7}"#
        );
        assert_eq!(
            serde_json::to_value(&value).unwrap(),
            serde_json::json!({"b": [1.5, 0, 0.25, "x", null, true], "a": -7})
        );
    }

    /// A raw type with an opaque field of each state.
    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct RawCall {
        #[serde(default)]
        arguments: Slot<Opaque>,
        #[serde(default)]
        nothing: Slot<Opaque>,
        #[serde(default)]
        absent: Slot<Opaque>,
    }

    #[test]
    fn an_opaque_field_of_a_raw_type_takes_a_value_of_each_kind() {
        let raw: RawCall = read(
            br#"{"arguments": {"path": "/tmp", "depth": 2.0}, "nothing": null}"#,
            CAP,
        )
        .unwrap();

        assert_eq!(raw.nothing, Slot::Null);
        assert_eq!(raw.absent, Slot::Missing);
        let Slot::Value(arguments) = &raw.arguments else {
            panic!("the field holds a value");
        };
        assert_eq!(arguments.kind(), Found::Table);
        assert_eq!(text_of(arguments, SORTED), r#"{"depth":2.0,"path":"/tmp"}"#);
        assert!(reads_empty_table::<RawCall>());

        for text in ["true", "7", "-0.0", "\"text\"", "[1]", "{}"] {
            let slot: Slot<Opaque> = read(text.as_bytes(), CAP).unwrap();

            assert_eq!(slot, Slot::Value(opaque(text)), "{text}");
        }
        assert_eq!(read::<Slot<Opaque>>(b" null ", CAP), Ok(Slot::Null));
        assert_eq!(read::<Option<Opaque>>(b"null", CAP), Ok(None));
        assert_eq!(read::<Option<Opaque>>(b"[1]", CAP), Ok(Some(opaque("[1]"))));
        assert_eq!(opaque("null").kind(), Found::Null);
    }

    #[test]
    fn a_value_reads_as_an_item_of_a_list_and_as_a_value_of_a_table() {
        let items: Vec<Opaque> = read(b"[1.50, [2], null, {\"a\": -0}]", CAP).unwrap();
        let texts: Vec<String> = items.iter().map(|item| text_of(item, COMPACT)).collect();
        assert_eq!(texts, ["1.5", "[2]", "null", "{\"a\":0}"]);

        // The list and the table of a raw type: each item is a `Slot`.
        let list: Slot<Vec<Slot<Opaque>>> = read(b"[1.50, null, [2]]", CAP).unwrap();
        assert_eq!(
            list,
            Slot::Value(vec![
                Slot::Value(opaque("1.50")),
                Slot::Null,
                Slot::Value(opaque("[2]")),
            ])
        );
        let table: Slot<BTreeMap<String, Slot<Opaque>>> =
            read(b"{\"b\": null, \"a\": [1.50]}", CAP).unwrap();
        assert_eq!(
            table,
            Slot::Value(BTreeMap::from([
                (String::from("a"), Slot::Value(opaque("[1.50]"))),
                (String::from("b"), Slot::Null),
            ]))
        );
        assert_eq!(
            read::<Slot<Vec<Slot<Opaque>>>>(b"{}", CAP),
            Ok(Slot::Other(Found::Table))
        );
    }

    /// A raw type with one named field. It keeps each other member too.
    /// Its `Deserialize` is a map visitor, because an `Opaque` does not read
    /// below `#[serde(flatten)]`.
    #[derive(Debug, PartialEq)]
    struct RawFault {
        code: Slot<String>,
        detail: Vec<(String, Opaque)>,
    }

    impl<'de> Deserialize<'de> for RawFault {
        fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            struct Members;

            impl<'de> Visitor<'de> for Members {
                type Value = RawFault;

                fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                    f.write_str("a table")
                }

                fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<RawFault, A::Error> {
                    let mut code = Slot::Missing;
                    let mut detail = Vec::new();
                    while let Some(key) = map.next_key::<String>()? {
                        if key == "code" {
                            code = map.next_value()?;
                        } else {
                            detail.push((key, map.next_value::<Opaque>()?));
                        }
                    }

                    Ok(RawFault { code, detail })
                }
            }

            deserializer.deserialize_map(Members)
        }
    }

    /// The detail of a fault as an object, in the order of its keys.
    struct Detail<'a>(&'a [(String, Opaque)]);

    impl Serialize for Detail<'_> {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            serializer.collect_map(self.0.iter().map(|(key, value)| (key, value)))
        }
    }

    #[test]
    fn a_map_visitor_keeps_each_unknown_key_in_its_place() {
        let raw: RawFault = read(
            br#"{"z": [1.50, {"b": 1, "a": 2}], "code": "disk_full", "a": "caf\u00e9", "m": null, "k": -0}"#,
            CAP,
        )
        .unwrap();

        assert_eq!(raw.code, Slot::Value(String::from("disk_full")));

        // What `json.dumps` gives for each value, with and without a space
        // after a comma and a colon.
        let wanted = [
            (
                "z",
                "[1.5,{\"b\":1,\"a\":2}]",
                "[1.5, {\"b\": 1, \"a\": 2}]",
            ),
            ("a", "\"caf\u{e9}\"", "\"caf\\u00e9\""),
            ("m", "null", "null"),
            ("k", "0", "0"),
        ];
        let keys: Vec<&str> = raw.detail.iter().map(|(key, _)| key.as_str()).collect();
        assert_eq!(keys, ["z", "a", "m", "k"]);
        for ((_, value), (key, compact, spaced)) in raw.detail.iter().zip(wanted) {
            assert_eq!(text_of(value, COMPACT), compact, "{key}");
            assert_eq!(text_of(value, SPACED), spaced, "{key}");
        }

        assert_eq!(
            text_of(&Detail(&raw.detail), COMPACT),
            "{\"z\":[1.5,{\"b\":1,\"a\":2}],\"a\":\"caf\u{e9}\",\"m\":null,\"k\":0}"
        );
        assert_eq!(
            text_of(&Detail(&raw.detail), SORTED),
            "{\"a\":\"caf\u{e9}\",\"k\":0,\"m\":null,\"z\":[1.5,{\"a\":2,\"b\":1}]}"
        );
    }

    #[test]
    fn a_value_does_not_read_below_a_flattened_field() {
        #[derive(Debug, Deserialize)]
        struct Flat {
            #[serde(default)]
            code: Slot<String>,
            #[serde(flatten)]
            rest: BTreeMap<String, Opaque>,
        }

        let error = serde_json::from_str::<Flat>(r#"{"code": "disk_full", "path": "/tmp"}"#)
            .map(|flat| (flat.code, flat.rest))
            .unwrap_err();
        assert!(
            error
                .to_string()
                .starts_with("invalid type: newtype struct"),
            "{error}"
        );
        assert!(read::<Flat>(br#"{"code": "disk_full", "path": "/tmp"}"#, CAP).is_err());

        // With no key for the flattened field, no value reads from the
        // buffer of `serde`.
        let none = read::<Flat>(br#"{"code": "disk_full"}"#, CAP).unwrap();
        assert_eq!(none.code, Slot::Value(String::from("disk_full")));
        assert!(none.rest.is_empty());
    }

    #[test]
    fn a_named_value_reads_beside_a_flattened_field_of_ignored_keys() {
        #[derive(Debug, Deserialize)]
        struct Beside {
            #[serde(default)]
            event: Slot<Opaque>,
            detail: Opaque,
            #[serde(flatten)]
            rest: BTreeMap<String, IgnoredAny>,
        }

        let line: Beside = read(
            br#"{"later": [1, 2], "event": {"cost": 1.50}, "more": {"a": 1}, "detail": -0}"#,
            CAP,
        )
        .unwrap();
        let keys: Vec<&str> = line.rest.keys().map(String::as_str).collect();

        assert_eq!(line.event, Slot::Value(opaque(r#"{"cost": 1.50}"#)));
        assert_eq!(text_of(&line.detail, COMPACT), "0");
        assert_eq!(keys, ["later", "more"]);
    }

    #[test]
    fn a_value_that_no_serializer_wrote_has_no_end() {
        let walk = Walk::from("[1]", 0);

        assert_eq!(walk.end::<WriteError>(), Err(WriteError::NoJsonForm));
        assert_eq!(write(&walk, COMPACT).unwrap(), b"[1]");
        assert_eq!(walk.end::<WriteError>(), Ok(3));
    }

    #[test]
    fn the_walk_gives_an_error_and_no_panic_for_a_text_that_is_not_strict() {
        // No constructor keeps such a text. The walk still must not panic.
        let broken = [
            "",
            " ",
            "[",
            "[1",
            "[1,",
            "{",
            "{\"a\"",
            "{\"a\" 1}",
            "{\"a\":",
            "{\"a\":}",
            "{1: 2}",
            "\"abc",
            "\"abc\\",
            "\"\\u12\"",
            "\"\\ud800\"",
            "tru",
            "fals",
            "nul",
            "nope",
            "-",
            "1e",
            "1.5.2",
            "18446744073709551616",
            "-9223372036854775809",
            "1e999",
            "[\"\u{e9}",
            "x",
            "\u{e9}",
        ];

        for text in broken {
            assert_eq!(
                write(&Walk::from(text, 0), COMPACT),
                Err(WriteError::NoJsonForm),
                "{text:?}"
            );
            assert_eq!(
                write(&Walk::from(text, 0), SORTED),
                Err(WriteError::NoJsonForm),
                "{text:?}"
            );
            assert!(Opaque::of_text(text).is_none(), "{text:?}");
        }

        // A start past the end of the text, and one inside a character.
        assert!(write(&Walk::from("[1]", 9), COMPACT).is_err());
        assert!(write(&Walk::from("\"\u{e9}\"", 2), COMPACT).is_err());
    }
}
