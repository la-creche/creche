//! TOML: the one reader and the one writer of a TOML file.
//!
//! Four file kinds are TOML texts: the family file, the server file, the
//! component manifest and the roster. [`FileKind`] names them. Each kind has
//! one raw `serde` type, in the module of its contract (`rust/AGENTS.md`,
//! rule 1). That module calls `read`. No other code parses a TOML text.
//!
//! Only this file and its child `toml10` name the crates `toml` and
//! `toml_parser`. `bin/tests/test_rust_workspace.py` holds that rule.
//!
//! # The checks of `read`
//!
//! `read` makes seven checks, in this sequence:
//!
//! 1. The size. A file has the bytes of its kind at most. A caller cannot
//!    give another limit.
//! 2. The encoding. The bytes are UTF-8. An overlong form is not UTF-8, and
//!    the three bytes of a surrogate are not.
//! 3. The start. The first character is not U+FEFF, the byte order mark.
//! 4. The parse. The crate `toml` reads the text. It refuses a key that a
//!    table holds two times and an integer outside 64 bits.
//! 5. TOML 1.0. The crate reads TOML 1.1 and has no switch. The child module
//!    `toml10` refuses each of the six forms that TOML 1.1 added.
//! 6. The levels. The top table has level 1. A table or a list inside a
//!    table or a list of level n has level n + 1. No table and no list has
//!    a level above 8. The walk uses no recursion.
//! 7. The shape. `serde` fills the raw type from the tree of the walk.
//!
//! Each of the checks 4, 5 and 6 gives [`TomlFault::NotToml`]. The TOML
//! parser of the Python standard library stops at a depth that is no fixed
//! number. A syntax fault and a level fault thus share one text in each
//! language.
//!
//! No limit counts the values of a file. A TOML text has no alias, so the
//! size limit bounds the count. Each grammar holds its own caps.
//!
//! # What a raw type gets
//!
//! - **The keys of a table come in sorted order**, by their code points. A
//!   raw type holds a table with free keys as a `BTreeMap`. The conversion
//!   to the valid type then reports the faults of one text in one sequence,
//!   in each language.
//! - **A struct reads a table only.** The derived reader of a struct also
//!   takes a list, and gives the first item to the first field. The reader
//!   here gives a list to no struct: that read fails. A raw type that is
//!   total still puts each nested table in a `Slot`, so a list there is
//!   `Slot::Other`. A raw type whose read can fail can use `MapOnly`.
//! - **A date-time comes as the unit value.** A TOML text can hold a
//!   date-time, a date and a time. No file kind has such a field. Under a
//!   `Slot` the value is thus `Slot::Null`, and in an `Unknown` its kind
//!   is `Found::Null`. TOML has no null, so no other value reads so. The
//!   conversion to the valid type reports a wrong type for it.
//! - **A closed set comes as a text.** The reader gives no enum. A raw type
//!   holds the text, and the conversion makes the enum.
//! - **A float field takes an integer** as the nearest float, as the `Slot`
//!   of a float does.
//!
//! # The writer
//!
//! `write` gives the text of a value. It then reads that text again with
//! the checks 4, 5 and 6. A text of `write` thus passes each check of
//! `read` but the size check: the writer has no file kind.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use serde::de::value::{MapDeserializer, SeqDeserializer};
use serde::de::{self, DeserializeOwned, IntoDeserializer, MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};

use crate::slot::{Found, Slot};

mod toml10;

/// A kind of file that is a TOML text.
///
/// The set is closed, and it does not cross a process boundary. Each kind
/// has one size limit. A limit is no public number, so no caller can give
/// `read` a limit that this file does not hold.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileKind {
    /// The family file: contract 01.
    Family,
    /// The MCP server file: contract 01b.
    Server,
    /// The component manifest: contract 06.
    Manifest,
    /// The roster of the chaperone.
    Roster,
}

/// The most bytes of a family file.
const FAMILY_BYTES: usize = 65536;

/// The most bytes of a server file.
const SERVER_BYTES: usize = 65536;

/// The most bytes of a component manifest.
const MANIFEST_BYTES: usize = 16384;

/// The most bytes of a roster.
const ROSTER_BYTES: usize = 1048576;

/// The most levels of a text, for each file kind. The deepest file of the
/// four kinds in this repository has 5 levels.
const LEVEL_MAX: u8 = 8;

impl FileKind {
    /// The most bytes of a file of this kind.
    const fn byte_limit(self) -> usize {
        match self {
            Self::Family => FAMILY_BYTES,
            Self::Server => SERVER_BYTES,
            Self::Manifest => MANIFEST_BYTES,
            Self::Roster => ROSTER_BYTES,
        }
    }
}

/// Why `read` refuses a file.
///
/// The set is closed, and it does not cross a process boundary. The text of
/// a fault holds no line, no column and no byte of the file. The Python
/// reader of a file kind writes the same text for each of the first four
/// faults.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TomlFault {
    /// The file has more bytes than the limit of its kind.
    TooLarge {
        /// The limit of the kind, in bytes.
        limit: usize,
    },
    /// The bytes are not UTF-8.
    NotUtf8,
    /// The text starts with U+FEFF.
    ByteOrderMark,
    /// The text is not TOML 1.0, or a table or a list has a level above the
    /// limit.
    NotToml,
    /// The raw type refuses the text. Only a raw type that is not total can
    /// give this fault.
    Shape,
}

impl fmt::Display for TomlFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge { limit } => write!(f, "the file has more than {limit} bytes"),
            Self::NotUtf8 => f.write_str("the file is not UTF-8 text"),
            Self::ByteOrderMark => f.write_str("the file starts with a byte order mark"),
            Self::NotToml => write!(
                f,
                "the text is not TOML 1.0, or it nests deeper than {LEVEL_MAX} levels"
            ),
            Self::Shape => f.write_str("the text does not have the shape of the file"),
        }
    }
}

impl Error for TomlFault {}

/// Why `write` gives no text.
///
/// The set is closed, and it does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TomlWriteFault {
    /// The value has no TOML form. Three examples are a value that is no
    /// table, a unit value and an absent item of a list.
    NoTomlForm,
    /// `read` refuses the text of the value. Two examples are an integer
    /// above the range of 64 bits with a sign, and a value of 9 levels.
    NotReadable,
}

impl fmt::Display for TomlWriteFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NoTomlForm => "the value has no TOML form",
            Self::NotReadable => "the TOML reader refuses the text of the value",
        })
    }
}

impl Error for TomlWriteFault {}

/// Reads the bytes of one file into the raw type `T`.
///
/// The doc comment of the module lists the seven checks and says what a raw
/// type gets.
///
/// # Errors
///
/// [`TomlFault`] for the first check that fails.
#[cfg_attr(
    not(test),
    expect(
        dead_code,
        reason = "no raw type calls the reader yet. The packet that adds the first caller removes this line"
    )
)]
pub(crate) fn read<T: DeserializeOwned>(bytes: &[u8], kind: FileKind) -> Result<T, TomlFault> {
    let limit = kind.byte_limit();
    if bytes.len() > limit {
        return Err(TomlFault::TooLarge { limit });
    }

    let text = std::str::from_utf8(bytes).map_err(|_| TomlFault::NotUtf8)?;
    if text.starts_with('\u{feff}') {
        return Err(TomlFault::ByteOrderMark);
    }

    T::deserialize(tree_of(text)?).map_err(|_| TomlFault::Shape)
}

/// Writes one value as a TOML text.
///
/// The crate `toml` writes the scalars of a table first, then each nested
/// table under a header, and each list of tables as one `[[header]]` block
/// for each item. It writes no line for an absent field of a struct.
///
/// # Errors
///
/// [`TomlWriteFault::NoTomlForm`] when the crate cannot write the value.
/// [`TomlWriteFault::NotReadable`] when the checks 4, 5 and 6 of `read`
/// refuse the text.
//
// CONTRACT-QUESTION: no rule says what the writer does with a value whose
// text the reader refuses. The crate writes such a value, for example an
// integer above the range of an `i64`. The reading here is the strict one:
// no text that `read` refuses leaves this function. The other reading
// writes each value that the crate writes. A change costs the call of
// `tree_of` here.
#[cfg_attr(
    not(test),
    expect(
        dead_code,
        reason = "no type calls the writer yet. The packet that adds the first caller removes this line"
    )
)]
pub(crate) fn write<T: Serialize>(value: &T) -> Result<String, TomlWriteFault> {
    let text = toml::to_string(value).map_err(|_| TomlWriteFault::NoTomlForm)?;

    match tree_of(&text) {
        Ok(_) => Ok(text),
        Err(_) => Err(TomlWriteFault::NotReadable),
    }
}

/// Each key of a table that its raw type does not name, with the kind of
/// the value.
///
/// A raw type takes it with `#[serde(flatten)]`. The conversion to the valid
/// type then reports each unknown key. The keys are in sorted order. The
/// kind of a date-time is `Found::Null`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Unknown(BTreeMap<String, Found>);

#[cfg_attr(
    not(test),
    expect(
        dead_code,
        reason = "no raw type has this field yet. The packet that adds the first one removes this line"
    )
)]
impl Unknown {
    /// Each key, with the kind of its value.
    pub(crate) const fn kinds(&self) -> &BTreeMap<String, Found> {
        &self.0
    }
}

impl<'de> Deserialize<'de> for Unknown {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Kinds;

        impl<'de> Visitor<'de> for Kinds {
            type Value = Unknown;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a table")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut table: A) -> Result<Unknown, A::Error> {
                let mut kinds = BTreeMap::new();
                // A `Slot` of a text reads a value of each kind and keeps
                // the kind of each one that is no text.
                while let Some((key, value)) = table.next_entry::<String, Slot<String>>()? {
                    let kind = match value {
                        Slot::Value(_) => Found::Text,
                        Slot::Other(kind) => kind,
                        Slot::Null | Slot::Missing => Found::Null,
                    };
                    kinds.insert(key, kind);
                }

                Ok(Unknown(kinds))
            }
        }

        deserializer.deserialize_map(Kinds)
    }
}

/// One value of a text that passed the checks 4, 5 and 6.
enum Node {
    Flag(bool),
    Integer(i64),
    Float(f64),
    Text(String),
    /// A date-time, a date or a time. `serde` gets the unit value for it.
    Moment,
    List(Vec<Node>),
    Table(BTreeMap<String, Node>),
}

/// The checks 4, 5 and 6 of `read`: the tree of a text.
//
// CONTRACT-QUESTION: TOML 1.0 gives a float 64 bits. It does not say what a
// reader does with a text outside that range, for example `1e999`. The
// crate refuses such a text in its parse, and the reading here keeps that
// refusal: a strict JSON text of a contract has the same rule for a float.
// The TOML reader of the Python standard library reads the text as an
// infinity, so no vector holds one. The other reading costs a float parser
// in this module, because the crate has no switch.
fn tree_of(text: &str) -> Result<Node, TomlFault> {
    let table: toml::Table = text.parse().map_err(|_| TomlFault::NotToml)?;
    if !toml10::holds(text, u32::from(LEVEL_MAX)) {
        return Err(TomlFault::NotToml);
    }

    levelled(table).ok_or(TomlFault::NotToml)
}

/// A table or a list that the walk has open.
struct Open {
    /// The keys of a table, in the sequence of its values. A list has none.
    keys: Option<Vec<String>>,
    /// The values that the walk did not read yet.
    left: std::vec::IntoIter<toml::Value>,
    /// The values that the walk read, in the sequence of the table or list.
    read: Vec<Node>,
}

impl Open {
    fn table(table: toml::Table) -> Self {
        let (keys, values): (Vec<String>, Vec<toml::Value>) = table.into_iter().unzip();

        Self {
            keys: Some(keys),
            left: values.into_iter(),
            read: Vec::new(),
        }
    }

    fn list(items: Vec<toml::Value>) -> Self {
        Self {
            keys: None,
            left: items.into_iter(),
            read: Vec::new(),
        }
    }

    /// The node of a table or a list that the walk read to its end.
    fn closed(self) -> Node {
        match self.keys {
            Some(keys) => Node::Table(keys.into_iter().zip(self.read).collect()),
            None => Node::List(self.read),
        }
    }
}

/// What the walk does with one value.
enum Step {
    /// The value is no table and no list.
    Leaf(Node),
    /// The value is a table or a list, one level below.
    Below(Open),
}

impl Step {
    /// The step for one value. `None` for a date-time that [`both_take`]
    /// refuses.
    fn of(value: toml::Value) -> Option<Self> {
        let leaf = match value {
            toml::Value::Table(table) => return Some(Self::Below(Open::table(table))),
            toml::Value::Array(items) => return Some(Self::Below(Open::list(items))),
            toml::Value::Datetime(moment) if !both_take(&moment) => return None,
            toml::Value::Datetime(_) => Node::Moment,
            toml::Value::Boolean(flag) => Node::Flag(flag),
            toml::Value::Integer(integer) => Node::Integer(integer),
            toml::Value::Float(float) => Node::Float(float),
            toml::Value::String(text) => Node::Text(text),
        };

        Some(Self::Leaf(leaf))
    }
}

/// Check 6 of `read`: the tree of a table that the crate read. `None` for
/// a table or a list with a level above [`LEVEL_MAX`], and for a date-time
/// that [`both_take`] refuses.
///
/// The walk keeps each open table and list in a list of its own. It calls
/// no function for a deeper level, so the depth of a text uses no stack.
fn levelled(top: toml::Table) -> Option<Node> {
    let mut open = vec![Open::table(top)];

    loop {
        let Some(value) = open.last_mut()?.left.next() else {
            let node = open.pop()?.closed();
            match open.last_mut() {
                Some(above) => above.read.push(node),
                None => return Some(node),
            }
            continue;
        };

        match Step::of(value)? {
            Step::Leaf(node) => open.last_mut()?.read.push(node),
            Step::Below(below) => {
                if open.len() >= usize::from(LEVEL_MAX) {
                    return None;
                }
                open.push(below);
            }
        }
    }
}

/// Whether the crate and the Python reader both take a date-time.
//
// CONTRACT-QUESTION: TOML 1.0 takes the times of RFC 3339, so it takes the
// second 60 and the year 0000. The crate `toml` reads both. The TOML reader
// of the Python standard library refuses both, because the time types of
// Python hold neither. No file kind holds a date-time. The reading here
// refuses both, so the two readers refuse the same texts. `time::Timestamp`
// refuses the same two forms. A change costs this function and its four
// vectors in the surface `tomlfile.syntax`. The Python reader then needs a
// parser of its own for a date-time.
fn both_take(moment: &toml::value::Datetime) -> bool {
    const LEAP_SECOND: u8 = 60;

    let year_zero = moment.date.is_some_and(|date| date.year == 0);
    let leap_second = moment
        .time
        .is_some_and(|time| time.second == Some(LEAP_SECOND));

    !year_zero && !leap_second
}

/// The error of a raw type that refuses a value. `read` keeps no text of
/// it: a message of `serde` can hold a value of the file.
type Refused = de::value::Error;

impl<'de> IntoDeserializer<'de, Refused> for Node {
    type Deserializer = Self;

    fn into_deserializer(self) -> Self {
        self
    }
}

impl<'de> Deserializer<'de> for Node {
    type Error = Refused;

    fn deserialize_any<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Refused> {
        match self {
            Self::Flag(flag) => visitor.visit_bool(flag),
            Self::Integer(integer) => visitor.visit_i64(integer),
            Self::Float(float) => visitor.visit_f64(float),
            Self::Text(text) => visitor.visit_string(text),
            Self::Moment => visitor.visit_unit(),
            Self::List(items) => {
                let mut list = SeqDeserializer::new(items.into_iter());
                let value = visitor.visit_seq(&mut list)?;
                list.end()?;

                Ok(value)
            }
            Self::Table(entries) => {
                let mut table = MapDeserializer::new(entries.into_iter());
                let value = visitor.visit_map(&mut table)?;
                table.end()?;

                Ok(value)
            }
        }
    }

    /// TOML has no null: a value that is there is `Some`.
    fn deserialize_option<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Refused> {
        visitor.visit_some(self)
    }

    fn deserialize_newtype_struct<V: Visitor<'de>>(
        self,
        _name: &'static str,
        visitor: V,
    ) -> Result<V::Value, Refused> {
        visitor.visit_newtype_struct(self)
    }

    /// A struct reads a table only. The derived reader of a struct also
    /// takes a list, by position.
    fn deserialize_struct<V: Visitor<'de>>(
        self,
        _name: &'static str,
        _fields: &'static [&'static str],
        visitor: V,
    ) -> Result<V::Value, Refused> {
        match self {
            Self::Table(_) => self.deserialize_any(visitor),
            Self::Flag(_)
            | Self::Integer(_)
            | Self::Float(_)
            | Self::Text(_)
            | Self::Moment
            | Self::List(_) => Err(de::Error::custom("a struct reads a table only")),
        }
    }

    serde::forward_to_deserialize_any! {
        bool i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 f32 f64 char str string
        bytes byte_buf unit unit_struct seq tuple tuple_struct map enum
        identifier ignored_any
    }
}

#[cfg(test)]
mod tests {
    use creche_vectors::Outcome;
    use serde::de::SeqAccess;
    use serde_json::{Value, json};

    use super::*;
    use crate::slot::{MapOnly, Nested};

    /// A value as [`read`] gives it to `serde`. A table keeps the sequence
    /// in which its keys came.
    #[derive(Debug, Clone, PartialEq)]
    enum Seen {
        Flag(bool),
        Integer(i64),
        Float(f64),
        Text(String),
        Unit,
        List(Vec<Seen>),
        Table(Vec<(String, Seen)>),
    }

    impl<'de> Deserialize<'de> for Seen {
        fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            struct Sees;

            impl<'de> Visitor<'de> for Sees {
                type Value = Seen;

                fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                    f.write_str("a value")
                }

                fn visit_bool<E: de::Error>(self, flag: bool) -> Result<Seen, E> {
                    Ok(Seen::Flag(flag))
                }

                fn visit_i64<E: de::Error>(self, integer: i64) -> Result<Seen, E> {
                    Ok(Seen::Integer(integer))
                }

                fn visit_f64<E: de::Error>(self, float: f64) -> Result<Seen, E> {
                    Ok(Seen::Float(float))
                }

                fn visit_str<E: de::Error>(self, text: &str) -> Result<Seen, E> {
                    Ok(Seen::Text(text.to_owned()))
                }

                fn visit_unit<E: de::Error>(self) -> Result<Seen, E> {
                    Ok(Seen::Unit)
                }

                fn visit_seq<A: SeqAccess<'de>>(self, mut list: A) -> Result<Seen, A::Error> {
                    let mut items = Vec::new();
                    while let Some(item) = list.next_element()? {
                        items.push(item);
                    }

                    Ok(Seen::List(items))
                }

                fn visit_map<A: MapAccess<'de>>(self, mut table: A) -> Result<Seen, A::Error> {
                    let mut entries = Vec::new();
                    while let Some(entry) = table.next_entry()? {
                        entries.push(entry);
                    }

                    Ok(Seen::Table(entries))
                }
            }

            deserializer.deserialize_any(Sees)
        }
    }

    fn seen(text: &str) -> Result<Seen, TomlFault> {
        read(text.as_bytes(), FileKind::Roster)
    }

    fn table<const N: usize>(entries: [(&str, Seen); N]) -> Seen {
        Seen::Table(
            entries
                .into_iter()
                .map(|(key, value)| (key.to_owned(), value))
                .collect(),
        )
    }

    // --- checks 1 to 3: the size, the encoding and the start ----------------

    /// The four file kinds, each with its limit in bytes.
    const LIMITS: [(FileKind, usize); 4] = [
        (FileKind::Family, 65536),
        (FileKind::Server, 65536),
        (FileKind::Manifest, 16384),
        (FileKind::Roster, 1048576),
    ];

    #[test]
    fn each_kind_takes_a_file_of_its_limit_and_no_larger_one() {
        for (kind, limit) in LIMITS {
            // One comment with no line end is a TOML text of each size.
            let at_limit = "#".repeat(limit);
            let over_limit = "#".repeat(limit + 1);

            assert_eq!(read::<Seen>(at_limit.as_bytes(), kind), Ok(table([])));
            assert_eq!(
                read::<Seen>(over_limit.as_bytes(), kind),
                Err(TomlFault::TooLarge { limit })
            );
        }
    }

    #[test]
    fn the_level_limit_is_8() {
        assert_eq!(LEVEL_MAX, 8);
    }

    #[test]
    fn each_fault_has_its_fixed_text() {
        let texts = [
            (
                TomlFault::TooLarge { limit: 16384 },
                "the file has more than 16384 bytes",
            ),
            (TomlFault::NotUtf8, "the file is not UTF-8 text"),
            (
                TomlFault::ByteOrderMark,
                "the file starts with a byte order mark",
            ),
            (
                TomlFault::NotToml,
                "the text is not TOML 1.0, or it nests deeper than 8 levels",
            ),
            (
                TomlFault::Shape,
                "the text does not have the shape of the file",
            ),
        ];

        for (fault, text) in texts {
            assert_eq!(fault.to_string(), text);
        }

        assert_eq!(
            TomlWriteFault::NoTomlForm.to_string(),
            "the value has no TOML form"
        );
        assert_eq!(
            TomlWriteFault::NotReadable.to_string(),
            "the TOML reader refuses the text of the value"
        );
    }

    #[test]
    fn bytes_that_are_not_utf8_are_refused() {
        let not_utf8: [&[u8]; 6] = [
            b"a = \"\xff\"\n",
            // An overlong form of `/`.
            b"a = \"\xc0\xaf\"\n",
            // The three bytes of the surrogate U+D800.
            b"a = \"\xed\xa0\x80\"\n",
            // A character that ends too early.
            b"a = \"\xe2\x82\"\n",
            // UTF-16, with its byte order mark and with none.
            b"\xff\xfea\x00 \x00=\x00 \x001\x00",
            b"a\x00 \x00=\x00 \x001\x00\xd8\x00",
        ];

        for bytes in not_utf8 {
            assert_eq!(
                read::<Seen>(bytes, FileKind::Family),
                Err(TomlFault::NotUtf8),
                "{bytes:?}"
            );
        }
    }

    #[test]
    fn a_text_that_starts_with_a_byte_order_mark_is_refused() {
        // The crate reads such a text. Check 3 refuses it.
        assert!("\u{feff}a = 1\n".parse::<toml::Table>().is_ok());

        assert_eq!(seen("\u{feff}a = 1\n"), Err(TomlFault::ByteOrderMark));
        assert_eq!(seen("\u{feff}"), Err(TomlFault::ByteOrderMark));
        assert_eq!(
            seen("a = \"\u{feff}\"\n"),
            Ok(table([("a", Seen::Text("\u{feff}".to_owned()))]))
        );
    }

    #[test]
    fn the_first_check_that_fails_names_the_fault() {
        let (kind, limit) = (FileKind::Manifest, 16384);
        let large_and_not_utf8 = vec![0xff; limit + 1];
        let marked_and_not_utf8 = b"\xef\xbb\xbfa = \"\xff\"\n";
        let marked_and_no_toml = "\u{feff}a = \n";

        assert_eq!(
            read::<Seen>(&large_and_not_utf8, kind),
            Err(TomlFault::TooLarge { limit })
        );
        assert_eq!(
            read::<Seen>(marked_and_not_utf8, kind),
            Err(TomlFault::NotUtf8)
        );
        assert_eq!(
            read::<Seen>(marked_and_no_toml.as_bytes(), kind),
            Err(TomlFault::ByteOrderMark)
        );
    }

    // --- checks 4 to 6: the parse, TOML 1.0 and the levels -------------------

    /// Reads `text`. The fault of a text that the reader refuses must be
    /// `NotToml`.
    fn is_toml(text: &str) -> bool {
        match seen(text) {
            Ok(_) => true,
            Err(fault) => {
                assert_eq!(fault, TomlFault::NotToml, "{text:?}");
                false
            }
        }
    }

    #[test]
    fn an_integer_outside_64_bits_is_refused() {
        let in_range = [
            "a = 9223372036854775807\n",
            "a = -9223372036854775808\n",
            "a = 0x7FFFFFFFFFFFFFFF\n",
        ];
        let outside = [
            "a = 9223372036854775808\n",
            "a = -9223372036854775809\n",
            "a = 18446744073709551615\n",
            "a = 0x8000000000000000\n",
            "a = 0o1000000000000000000000\n",
            "a = 123456789012345678901234567890\n",
        ];

        for text in in_range {
            assert!(is_toml(text), "{text:?}");
        }
        for text in outside {
            assert!(!is_toml(text), "{text:?}");
        }
    }

    #[test]
    fn a_float_outside_the_range_of_a_float_is_refused() {
        // No vector holds such a text: the TOML reader of the Python standard
        // library reads it as an infinity.
        for text in ["a = 1e999\n", "a = -1e999\n", "a = 1e309\n", "a = 2e308\n"] {
            assert!(!is_toml(text), "{text:?}");
        }

        assert_eq!(
            seen("a = 1.7976931348623157e308\n"),
            Ok(table([("a", Seen::Float(f64::MAX))]))
        );
        assert_eq!(seen("a = 1e-999\n"), Ok(table([("a", Seen::Float(0.0))])));
    }

    #[test]
    fn each_form_of_toml_11_is_refused() {
        let newer = [
            "a = {b = 1\n}\n",
            "a = {b = 1,}\n",
            "a = \"\\e\"\n",
            "a = \"\\x41\"\n",
            "a = 07:32\n",
            "a = 1979-05-27T07:32Z\n",
        ];

        for text in newer {
            assert!(
                text.parse::<toml::Table>().is_ok(),
                "the crate reads {text:?}"
            );
            assert!(!is_toml(text), "{text:?}");
        }
    }

    /// `count` lists, one inside the other, as the value of the key `a`.
    fn lists(count: usize) -> String {
        format!("a = {}{}\n", "[".repeat(count), "]".repeat(count))
    }

    /// `count` inline tables, one inside the other, as the value of `a`.
    fn inline_tables(count: usize) -> String {
        format!("a = {}1{}\n", "{a = ".repeat(count), "}".repeat(count))
    }

    /// A table header with `count` parts.
    fn header(count: usize) -> String {
        format!("[{}]\n", vec!["a"; count].join("."))
    }

    /// A dotted key with `count` parts.
    fn dotted_key(count: usize) -> String {
        format!("{} = 1\n", vec!["a"; count].join("."))
    }

    #[test]
    fn a_text_of_8_levels_is_read_and_a_text_of_9_levels_is_refused() {
        // The top table has level 1.
        let of_8_levels = [
            lists(7),
            inline_tables(7),
            header(7),
            dotted_key(8),
            // A list of tables is two levels: the list and each table.
            "[[a]]\n[[a.b]]\n[[a.b.c]]\nd = []\n".to_owned(),
            format!("{}b = []\n", header(6)),
        ];
        let of_9_levels = [
            lists(8),
            inline_tables(8),
            header(8),
            dotted_key(9),
            "[[a]]\n[[a.b]]\n[[a.b.c]]\n[[a.b.c.d]]\n".to_owned(),
            "[[a]]\n[[a.b]]\n[[a.b.c]]\nd = [[]]\n".to_owned(),
            // An empty table and an empty list have a level too.
            format!("{}b = {{}}\n", header(7)),
            format!("{}b = []\n", header(7)),
        ];

        for text in of_8_levels {
            assert!(is_toml(&text), "{text:?}");
        }
        for text in of_9_levels {
            assert!(
                text.parse::<toml::Table>().is_ok(),
                "the crate reads {text:?}"
            );
            assert!(!is_toml(&text), "{text:?}");
        }
    }

    #[test]
    fn a_very_deep_text_is_a_refusal_on_a_small_stack() {
        const STACK_BYTES: usize = 2 * 1024 * 1024;
        const PARTS: usize = 30_000;

        let refused = std::thread::Builder::new()
            .stack_size(STACK_BYTES)
            .spawn(|| {
                // The crate stops at 80 levels of each of the four forms.
                for count in [81, PARTS] {
                    for text in [
                        lists(count),
                        inline_tables(count),
                        header(count),
                        dotted_key(count),
                    ] {
                        assert!(!is_toml(&text), "{count} levels");
                    }
                }
            })
            .unwrap()
            .join();

        assert!(refused.is_ok(), "the reader used more than the stack");
    }

    #[test]
    fn a_date_time_that_the_python_reader_refuses_is_refused() {
        // The crate reads the four texts. The vectors hold them as refused.
        let refused = [
            "a = 23:59:60\n",
            "a = 1979-05-27T23:59:60Z\n",
            "a = 0000-01-01\n",
            "a = 0000-12-31T23:59:59Z\n",
        ];

        for text in refused {
            assert!(
                text.parse::<toml::Table>().is_ok(),
                "the crate reads {text:?}"
            );
            assert!(!is_toml(text), "{text:?}");
        }

        assert!(is_toml("a = 0001-01-01T23:59:59Z\n"));
    }

    // --- check 7: what a raw type gets ---------------------------------------

    #[test]
    fn the_keys_of_a_table_come_in_sorted_order() {
        let read = seen("zeta = 1\nalpha = 2\n\n[middle]\nz = 3\n\"\u{e9}\" = 4\na = 5\n");

        assert_eq!(
            read,
            Ok(table([
                ("alpha", Seen::Integer(2)),
                (
                    "middle",
                    table([
                        ("a", Seen::Integer(5)),
                        ("z", Seen::Integer(3)),
                        ("\u{e9}", Seen::Integer(4)),
                    ])
                ),
                ("zeta", Seen::Integer(1)),
            ]))
        );
    }

    /// A row with a plain field: its read fails for a value of a wrong kind.
    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Row {
        command: String,
    }

    /// A row with a `Slot` at its field: its read fails for no table.
    #[derive(Debug, Default, PartialEq, Deserialize)]
    #[serde(default)]
    struct RawRow {
        command: Slot<String>,
        #[serde(flatten)]
        unknown: Unknown,
    }

    impl Nested for RawRow {}

    fn rows<T: DeserializeOwned>(text: &str) -> Result<BTreeMap<String, T>, TomlFault> {
        read(text.as_bytes(), FileKind::Roster)
    }

    #[test]
    fn a_struct_reads_a_table_only() {
        let as_table = "[a]\ncommand = \"run\"\n";
        let as_list = "a = [\"run\"]\n";
        let run = || Row {
            command: "run".to_owned(),
        };

        assert_eq!(rows::<Row>(as_table).unwrap().get("a"), Some(&run()));
        assert_eq!(
            rows::<MapOnly<Row>>(as_table).unwrap().get("a"),
            Some(&MapOnly(run()))
        );

        // The derived reader of `Row` takes a list by position. The reader
        // of this module gives it none.
        assert_eq!(rows::<Row>(as_list), Err(TomlFault::Shape));
        assert_eq!(rows::<MapOnly<Row>>(as_list), Err(TomlFault::Shape));
        assert_eq!(
            rows::<Slot<RawRow>>(as_list).unwrap().get("a"),
            Some(&Slot::Other(Found::List))
        );

        for no_table in ["a = \"run\"\n", "a = 1\n", "a = 1979-05-27\n"] {
            assert_eq!(rows::<Row>(no_table), Err(TomlFault::Shape), "{no_table:?}");
        }
    }

    #[test]
    fn a_date_time_is_a_wrong_type_and_no_table() {
        #[derive(Debug, Deserialize)]
        struct RawFile {
            #[serde(default)]
            row: Slot<RawRow>,
            #[serde(default)]
            rows: Slot<Vec<Slot<String>>>,
            #[serde(flatten)]
            unknown: Unknown,
        }

        let text = "later = 1979-05-27T07:32:00Z\nrows = [\"a\", 1979-05-27]\n\n\
                    [row]\ncommand = 2026-10-04\nat = 07:32:00\n";
        let file: RawFile = read(text.as_bytes(), FileKind::Family).unwrap();
        let Slot::Value(row) = file.row else {
            panic!("the row is no table: {:?}", file.row);
        };

        assert_eq!(row.command, Slot::Null);
        assert_eq!(
            row.unknown.kinds(),
            &BTreeMap::from([("at".to_owned(), Found::Null)])
        );
        assert_eq!(
            file.rows,
            Slot::Value(vec![Slot::Value("a".to_owned()), Slot::Null])
        );
        assert_eq!(
            file.unknown.kinds(),
            &BTreeMap::from([("later".to_owned(), Found::Null)])
        );

        // A plain text field refuses a date-time.
        assert_eq!(
            rows::<Row>("[a]\ncommand = 2026-10-04\n"),
            Err(TomlFault::Shape)
        );
    }

    #[test]
    fn unknown_keeps_each_key_that_the_raw_type_does_not_name() {
        let text = "command = \"run\"\nflag = true\ncount = 7\nratio = 2.5\nname = \"n\"\n\
                    list = [1]\ntable = {a = 1}\nmoment = 1979-05-27\n";
        let row: RawRow = read(text.as_bytes(), FileKind::Server).unwrap();
        let kinds = [
            ("count", Found::Integer),
            ("flag", Found::Boolean),
            ("list", Found::List),
            ("moment", Found::Null),
            ("name", Found::Text),
            ("ratio", Found::Float),
            ("table", Found::Table),
        ];

        assert_eq!(row.command, Slot::Value("run".to_owned()));
        assert_eq!(
            row.unknown.kinds(),
            &kinds.map(|(key, kind)| (key.to_owned(), kind)).into()
        );

        let none: RawRow = read(b"command = \"run\"\n", FileKind::Server).unwrap();

        assert_eq!(none.unknown, Unknown::default());
        assert!(crate::slot::tests::reads_empty_table::<RawRow>());
    }

    #[test]
    fn a_raw_type_that_is_not_total_gives_the_shape_fault() {
        #[derive(Debug, PartialEq, Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Closed {
            command: String,
            retries: Option<u32>,
        }

        #[derive(Debug, PartialEq, Deserialize)]
        enum Mode {
            Fast,
        }

        let closed = |text: &str| read::<Closed>(text.as_bytes(), FileKind::Manifest);

        assert_eq!(
            closed("command = \"run\"\n"),
            Ok(Closed {
                command: "run".to_owned(),
                retries: None,
            })
        );
        assert_eq!(
            closed("command = \"run\"\nretries = 3\n"),
            Ok(Closed {
                command: "run".to_owned(),
                retries: Some(3),
            })
        );

        let refused = [
            "",
            "command = 5\n",
            "command = \"run\"\nretries = -1\n",
            "command = \"run\"\nretries = \"3\"\n",
            "command = \"run\"\nother = 1\n",
        ];
        for text in refused {
            assert_eq!(closed(text), Err(TomlFault::Shape), "{text:?}");
        }

        // The reader gives no enum: a raw type holds a closed set as a text.
        assert_eq!(rows::<Mode>("a = \"Fast\"\n"), Err(TomlFault::Shape));
        assert_eq!(
            rows::<String>("a = \"Fast\"\n").unwrap().get("a"),
            Some(&"Fast".to_owned())
        );
    }

    #[test]
    fn a_float_field_takes_an_integer() {
        let read = rows::<f64>("a = 7\nb = 2.5\n").unwrap();

        assert_eq!(
            read,
            BTreeMap::from([("a".to_owned(), 7.0), ("b".to_owned(), 2.5)])
        );
        assert_eq!(rows::<i64>("a = 2.0\n"), Err(TomlFault::Shape));
    }

    // --- the writer ----------------------------------------------------------

    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Fence {
        tool: String,
        deny: Vec<String>,
    }

    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Server {
        command: String,
        args: Vec<String>,
        tools: Option<Vec<String>>,
        ratio: f64,
        count: i64,
        on: bool,
        env: BTreeMap<String, String>,
        fences: Vec<Fence>,
    }

    fn server(command: &str) -> Server {
        Server {
            command: command.to_owned(),
            args: vec!["--flag".to_owned(), String::new()],
            tools: None,
            ratio: -0.5,
            count: i64::MIN,
            on: true,
            env: BTreeMap::from([("KEY".to_owned(), "a value".to_owned())]),
            fences: vec![Fence {
                tool: "read".to_owned(),
                deny: vec!["/etc".to_owned()],
            }],
        }
    }

    #[test]
    fn the_reader_reads_each_text_of_the_writer() {
        let commands = [
            "run",
            "",
            "two\nlines\r\nand a \t tab",
            "quotes \" ' \"\"\" ''' and a \\ backslash",
            "controls \u{0} \u{1b} \u{7f} \u{85}",
            "caf\u{e9} \u{1f600} \u{feff}",
            "07:32 {b = 1,} \\e \\x41 # no comment",
        ];

        for command in commands {
            let mut one = server(command);
            one.tools = Some(vec![]);
            let roster = BTreeMap::from([
                ("zeta".to_owned(), server(command)),
                ("alpha beta".to_owned(), one),
            ]);

            let text = write(&roster).unwrap();
            let back: BTreeMap<String, Server> = read(text.as_bytes(), FileKind::Roster).unwrap();

            assert_eq!(back, roster, "{text}");
        }
    }

    #[test]
    fn the_writer_writes_the_scalars_of_a_table_before_its_tables() {
        let roster = BTreeMap::from([("one".to_owned(), server("run"))]);

        assert_eq!(
            write(&roster).unwrap(),
            "[one]\ncommand = \"run\"\nargs = [\"--flag\", \"\"]\nratio = -0.5\n\
             count = -9223372036854775808\non = true\n\n[one.env]\nKEY = \"a value\"\n\n\
             [[one.fences]]\ntool = \"read\"\ndeny = [\"/etc\"]\n"
        );
        assert_eq!(write(&BTreeMap::<String, i64>::new()).unwrap(), "");
    }

    #[test]
    fn the_writer_gives_no_text_that_the_reader_refuses() {
        // The crate writes each of these values.
        let above_64_bits = BTreeMap::from([("a", u64::MAX)]);
        let of_9_levels =
            BTreeMap::from([("a", vec![vec![vec![vec![vec![vec![vec![vec![1]]]]]]]])]);
        let of_8_levels = BTreeMap::from([("a", vec![vec![vec![vec![vec![vec![vec![1]]]]]]])]);

        assert!(toml::to_string(&above_64_bits).is_ok());
        assert!(toml::to_string(&of_9_levels).is_ok());

        assert_eq!(write(&above_64_bits), Err(TomlWriteFault::NotReadable));
        assert_eq!(write(&of_9_levels), Err(TomlWriteFault::NotReadable));
        assert_eq!(write(&of_8_levels).unwrap(), "a = [[[[[[[1]]]]]]]\n");
        assert_eq!(
            write(&BTreeMap::from([("a", i64::MAX)])).unwrap(),
            "a = 9223372036854775807\n"
        );
    }

    #[test]
    fn the_writer_refuses_a_value_with_no_toml_form() {
        assert_eq!(write(&vec![1, 2]), Err(TomlWriteFault::NoTomlForm));
        assert_eq!(write(&"text"), Err(TomlWriteFault::NoTomlForm));
        assert_eq!(
            write(&BTreeMap::from([("a", ())])),
            Err(TomlWriteFault::NoTomlForm)
        );
        assert_eq!(
            write(&BTreeMap::from([("a", vec![Some(1), None])])),
            Err(TomlWriteFault::NoTomlForm)
        );
    }

    // --- the differential test ------------------------------------------------

    /// Each surface of the vectors that this module implements. The reader
    /// and the Python reader must give the same result for each vector.
    const SURFACES: [&str; 1] = ["tomlfile.syntax"];

    /// The start of the name of each surface of this module.
    const SURFACE_PREFIX: &str = "tomlfile.";

    /// A float in the form of a vector: a float that is not finite is a
    /// `$float` marker.
    fn float_json(float: f64) -> Value {
        if float.is_nan() {
            json!({"$float": "NaN"})
        } else if float.is_infinite() {
            json!({"$float": if float > 0.0 { "Infinity" } else { "-Infinity" }})
        } else {
            json!(float)
        }
    }

    /// What [`read`] gave, in the form of a vector. A date-time is `null`:
    /// the reader gives no kind of it.
    fn read_json(seen: &Seen) -> Value {
        match seen {
            Seen::Flag(flag) => json!(flag),
            Seen::Integer(integer) => json!(integer),
            Seen::Float(float) => float_json(*float),
            Seen::Text(text) => json!(text),
            Seen::Unit => Value::Null,
            Seen::List(items) => items.iter().map(read_json).collect(),
            Seen::Table(entries) => Value::Object(
                entries
                    .iter()
                    .map(|(key, value)| (key.clone(), read_json(value)))
                    .collect(),
            ),
        }
    }

    /// The kind of a date-time, in the words of a vector.
    fn kind_of(moment: &toml::value::Datetime) -> &'static str {
        match (moment.date, moment.time, moment.offset) {
            (Some(_), Some(_), Some(_)) => "offset date-time",
            (Some(_), Some(_), None) => "local date-time",
            (Some(_), None, None) => "local date",
            (None, Some(_), None) => "local time",
            _ => panic!("{moment} has no kind"),
        }
    }

    /// What the crate read, in the form of a vector. A date-time is an
    /// object with its kind as the one key, and `null` as the value.
    fn crate_json(value: &toml::Value) -> Value {
        match value {
            toml::Value::Boolean(flag) => json!(flag),
            toml::Value::Integer(integer) => json!(integer),
            toml::Value::Float(float) => float_json(*float),
            toml::Value::String(text) => json!(text),
            toml::Value::Datetime(moment) => json!({kind_of(moment): null}),
            toml::Value::Array(items) => items.iter().map(crate_json).collect(),
            toml::Value::Table(entries) => Value::Object(
                entries
                    .iter()
                    .map(|(key, value)| (key.clone(), crate_json(value)))
                    .collect(),
            ),
        }
    }

    /// The value of a vector, with `null` in the place of each date-time.
    /// No other object of a value has `null` as the value of its one key.
    fn without_kinds(value: &Value) -> Value {
        match value {
            Value::Object(members)
                if members.len() == 1 && members.values().all(Value::is_null) =>
            {
                Value::Null
            }
            Value::Object(members) => Value::Object(
                members
                    .iter()
                    .map(|(key, value)| (key.clone(), without_kinds(value)))
                    .collect(),
            ),
            Value::Array(items) => items.iter().map(without_kinds).collect(),
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => value.clone(),
        }
    }

    /// The text of a value. Two values are equal when their texts are: the
    /// text holds the sign of a zero, and `==` of two values does not.
    fn text_of(value: &Value) -> String {
        serde_json::to_string(value).unwrap()
    }

    #[test]
    fn the_reader_agrees_with_the_python_reader_on_each_vector() {
        let unnamed: Vec<String> = creche_vectors::index()
            .unwrap()
            .iter()
            .map(|row| row.surface().to_owned())
            .filter(|name| name.starts_with(SURFACE_PREFIX) && !SURFACES.contains(&name.as_str()))
            .collect();
        assert!(unnamed.is_empty(), "no table names {unnamed:?}");

        for name in SURFACES {
            let surface = creche_vectors::surface(name).unwrap();

            for vector in surface.vectors() {
                let id = vector.id();
                let bytes = vector.input().bytes().unwrap();
                let read = read::<Seen>(&bytes, FileKind::Roster);

                if vector.result() != Outcome::Accepted {
                    assert!(read.is_err(), "{id}: the reader takes the text");
                    continue;
                }

                let wanted = vector.value().unwrap();
                let seen = read.unwrap_or_else(|fault| panic!("{id}: {fault}"));
                assert_eq!(
                    text_of(&read_json(&seen)),
                    text_of(&without_kinds(wanted)),
                    "{id}"
                );

                // The reader gives no kind of a date-time. The crate does.
                let table: toml::Table = std::str::from_utf8(&bytes).unwrap().parse().unwrap();
                assert_eq!(
                    text_of(&crate_json(&toml::Value::Table(table))),
                    text_of(wanted),
                    "{id}: the kind of a date-time"
                );
            }
        }
    }

    #[test]
    fn the_sign_of_a_zero_is_a_part_of_a_compared_value() {
        assert_eq!(json!(0.0), json!(-0.0));
        assert_ne!(text_of(&json!(0.0)), text_of(&json!(-0.0)));
        assert_ne!(text_of(&json!(1)), text_of(&json!(1.0)));
    }
}
