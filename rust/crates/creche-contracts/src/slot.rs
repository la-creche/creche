//! The lenient field type of a raw type, and a wrapper that takes only a
//! table.
//!
//! A raw type holds a document before a check (`rust/AGENTS.md`, rule 1). A
//! key of a document can hold a value of a kind that its field does not
//! take. Two types of this module say what the read does then:
//!
//! - [`Slot`] keeps the fact. A raw type with a `Slot` at each field is
//!   total: its read fails for no value of a field. The conversion to the
//!   valid type then reports each field.
//! - [`MapOnly`] fails the read. It is for a raw type that is not total. The
//!   read of such a document gives one refusal.
//!
//! The module names no format. `serde` fills each type from the reader of a
//! format, and the code here uses the visitors of `serde` only.

use std::collections::BTreeMap;
use std::fmt;
use std::marker::PhantomData;

use serde::de::value::MapAccessDeserializer;
use serde::de::{self, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// The kind of a value, as a `serde` reader gives it.
///
/// The set is closed: the reader of a text format gives no other kind. It
/// does not cross a process boundary.
// `pub` in a private module: a public module exports the enum, and its error
// type can then name the kind of a value. The `json` module is the first one
// (packet `decisions-json-check`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Found {
    /// No value: `null`.
    Null,
    /// `true` or `false`.
    Boolean,
    /// A number that the reader gives as an integer.
    Integer,
    /// A number that the reader gives as a float.
    Float,
    /// A text.
    Text,
    /// A list of values.
    List,
    /// A table: keys, each with a value.
    Table,
}

/// What a document holds at one key.
///
/// A raw type gives each `Slot` field the attribute `#[serde(default)]`. A
/// document with no such key then reads as [`Slot::Missing`]. Without the
/// attribute, the read of that document fails.
///
/// The read of a `Slot` does not fail for the kind of a value. A kind that
/// the field does not take is [`Slot::Other`]. The `Slot` asks the reader
/// for the kind of the value. The reader of a text format gives that kind,
/// and each kind is in [`Found`].
///
/// | Field type | What the field takes |
/// |---|---|
/// | `Slot<String>` | A text. |
/// | `Slot<bool>` | `true` or `false`. |
/// | `Slot<i64>`, `Slot<u64>` | An integer in the range of the type. `2.0` is a float and no integer. |
/// | `Slot<f64>` | A float. An integer reads as the nearest float. |
/// | `Slot<Vec<Slot<T>>>` | A list. Each item is a `Slot`, so an item of a wrong kind keeps its place. |
/// | `Slot<T>` with `T:` [`Nested`] | A table: a nested raw struct, or a `BTreeMap<String, Slot<V>>` for free keys. |
///
/// An integer that the field type cannot hold is `Other(Found::Integer)`,
/// for example `-1` under `Slot<u64>`. `Slot<f64>` holds each integer in the
/// range of `i128`.
///
/// Another module can add a field type of its own. It writes `Deserialize`
/// for `Slot` of that one type. No impl here covers each type that `serde`
/// reads, so the new impl conflicts with none.
// `Default` gives `Missing`. Rule 2 of `rust/AGENTS.md` forbids a `Default`
// on a type that holds only valid values. A `Slot` is a part of a raw form,
// and the conversion to the valid type checks a `Missing`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) enum Slot<T> {
    /// The document has no such key.
    #[default]
    Missing,
    /// The value is `null`.
    Null,
    /// The value has a kind that the field does not take.
    Other(Found),
    /// The value has the kind of the field.
    Value(T),
}

impl<T> Slot<T> {
    /// The value, when the document holds one of the kind of the field.
    pub(crate) fn value(&self) -> Option<&T> {
        match self {
            Self::Value(value) => Some(value),
            Self::Missing | Self::Null | Self::Other(_) => None,
        }
    }
}

impl Slot<String> {
    /// The text, or the empty text.
    pub(crate) fn text(&self) -> &str {
        self.value().map_or("", String::as_str)
    }
}

impl Slot<bool> {
    /// Whether the document holds `true` here. Each Python reader takes no
    /// other value as true.
    pub(crate) fn is_true(&self) -> bool {
        self.value() == Some(&true)
    }
}

/// A type whose read takes a value of each kind. Each `Slot` is one.
///
/// An item of a list is such a type, and a value of a table with free keys
/// is one too. A raw struct in that place would take a list by position. A
/// text in that place would fail the read for a number.
pub(crate) trait Lenient {}

impl<T> Lenient for Slot<T> {}

/// A type that a [`Slot`] reads from a table and from no other kind.
///
/// A raw struct implements it when it is the value of a field of another raw
/// type: `impl Nested for RawRow {}`. A struct that derives `Deserialize`
/// also takes a list: `serde` then fills its fields by position. A `Slot`
/// asks for the kind of the value before the struct reads it, so a list is
/// `Other(Found::List)`.
pub(crate) trait Nested {}

/// A table with free keys.
impl<V: Lenient> Nested for BTreeMap<String, V> {}

/// What one field type takes from a value of each kind. Each default is the
/// answer of a type that does not take the kind.
trait FieldType<'de>: Sized {
    /// What the field takes, in the words of an error of `serde`.
    const WANTS: &'static str;

    fn flag(_flag: bool) -> Option<Self> {
        None
    }

    fn integer(_integer: i128) -> Option<Self> {
        None
    }

    fn float(_float: f64) -> Option<Self> {
        None
    }

    fn text(_text: &str) -> Option<Self> {
        None
    }

    /// The default reads the list to its end and keeps no item.
    fn list<A: SeqAccess<'de>>(list: A) -> Result<Slot<Self>, A::Error> {
        skip_list(list)?;

        Ok(Slot::Other(Found::List))
    }

    /// The default reads the table to its end and keeps no entry.
    fn table<A: MapAccess<'de>>(table: A) -> Result<Slot<Self>, A::Error> {
        skip_table(table)?;

        Ok(Slot::Other(Found::Table))
    }
}

/// Reads each item of a list and keeps none. A reader can refuse a list
/// that its visitor did not read to the end.
fn skip_list<'de, A: SeqAccess<'de>>(mut list: A) -> Result<(), A::Error> {
    while list.next_element::<IgnoredAny>()?.is_some() {}

    Ok(())
}

/// Reads each entry of a table and keeps none.
fn skip_table<'de, A: MapAccess<'de>>(mut table: A) -> Result<(), A::Error> {
    while table.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}

    Ok(())
}

fn taken<T>(value: Option<T>, kind: Found) -> Slot<T> {
    value.map_or(Slot::Other(kind), Slot::Value)
}

/// The visitor of each field type: it gives the kind of the value to `T`.
struct Reads<T>(PhantomData<T>);

impl<'de, T: FieldType<'de>> Visitor<'de> for Reads<T> {
    type Value = Slot<T>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(T::WANTS)
    }

    fn visit_bool<E: de::Error>(self, flag: bool) -> Result<Slot<T>, E> {
        Ok(taken(T::flag(flag), Found::Boolean))
    }

    fn visit_i64<E: de::Error>(self, integer: i64) -> Result<Slot<T>, E> {
        Ok(taken(T::integer(i128::from(integer)), Found::Integer))
    }

    fn visit_i128<E: de::Error>(self, integer: i128) -> Result<Slot<T>, E> {
        Ok(taken(T::integer(integer), Found::Integer))
    }

    fn visit_u64<E: de::Error>(self, integer: u64) -> Result<Slot<T>, E> {
        Ok(taken(T::integer(i128::from(integer)), Found::Integer))
    }

    fn visit_u128<E: de::Error>(self, integer: u128) -> Result<Slot<T>, E> {
        // An integer past `i128::MAX` is outside the range of each field type.
        let in_range = i128::try_from(integer).ok();

        Ok(taken(in_range.and_then(T::integer), Found::Integer))
    }

    fn visit_f64<E: de::Error>(self, float: f64) -> Result<Slot<T>, E> {
        Ok(taken(T::float(float), Found::Float))
    }

    fn visit_str<E: de::Error>(self, text: &str) -> Result<Slot<T>, E> {
        Ok(taken(T::text(text), Found::Text))
    }

    fn visit_unit<E: de::Error>(self) -> Result<Slot<T>, E> {
        Ok(Slot::Null)
    }

    fn visit_none<E: de::Error>(self) -> Result<Slot<T>, E> {
        Ok(Slot::Null)
    }

    fn visit_some<D: Deserializer<'de>>(self, value: D) -> Result<Slot<T>, D::Error> {
        value.deserialize_any(self)
    }

    fn visit_newtype_struct<D: Deserializer<'de>>(self, value: D) -> Result<Slot<T>, D::Error> {
        value.deserialize_any(self)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, list: A) -> Result<Slot<T>, A::Error> {
        T::list(list)
    }

    fn visit_map<A: MapAccess<'de>>(self, table: A) -> Result<Slot<T>, A::Error> {
        T::table(table)
    }
}

fn read<'de, T: FieldType<'de>, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Slot<T>, D::Error> {
    deserializer.deserialize_any(Reads(PhantomData))
}

impl FieldType<'_> for String {
    const WANTS: &'static str = "a text";

    fn text(text: &str) -> Option<Self> {
        Some(text.to_owned())
    }
}

impl<'de> Deserialize<'de> for Slot<String> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

impl FieldType<'_> for bool {
    const WANTS: &'static str = "a boolean";

    fn flag(flag: bool) -> Option<Self> {
        Some(flag)
    }
}

impl<'de> Deserialize<'de> for Slot<bool> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

impl FieldType<'_> for i64 {
    const WANTS: &'static str = "an integer";

    fn integer(integer: i128) -> Option<Self> {
        Self::try_from(integer).ok()
    }
}

impl<'de> Deserialize<'de> for Slot<i64> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

impl FieldType<'_> for u64 {
    const WANTS: &'static str = "an integer that is not negative";

    fn integer(integer: i128) -> Option<Self> {
        Self::try_from(integer).ok()
    }
}

impl<'de> Deserialize<'de> for Slot<u64> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

impl FieldType<'_> for f64 {
    const WANTS: &'static str = "a number";

    /// The nearest float. The decimal text of an integer always parses, and
    /// the parse rounds one time, to the nearest float.
    fn integer(integer: i128) -> Option<Self> {
        integer.to_string().parse().ok()
    }

    fn float(float: f64) -> Option<Self> {
        Some(float)
    }
}

impl<'de> Deserialize<'de> for Slot<f64> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

impl<'de, T: Lenient + Deserialize<'de>> FieldType<'de> for Vec<T> {
    const WANTS: &'static str = "a list";

    fn list<A: SeqAccess<'de>>(mut list: A) -> Result<Slot<Self>, A::Error> {
        let mut items = Self::new();
        while let Some(item) = list.next_element()? {
            items.push(item);
        }

        Ok(Slot::Value(items))
    }
}

/// A list. Each item is a [`Lenient`] type, so an item of a wrong kind keeps
/// its place and does not fail the read.
impl<'de, T: Lenient + Deserialize<'de>> Deserialize<'de> for Slot<Vec<T>> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

impl<'de, T: Nested + Deserialize<'de>> FieldType<'de> for T {
    const WANTS: &'static str = "a table";

    fn table<A: MapAccess<'de>>(table: A) -> Result<Slot<Self>, A::Error> {
        Self::deserialize(MapAccessDeserializer::new(table)).map(Slot::Value)
    }
}

/// A table. `T` reads the keys, so the read of the table fails when the read
/// of `T` fails. The read of a raw struct fails for no table when each field
/// is a `Slot` with `#[serde(default)]` and the struct refuses no key.
impl<'de, T: Nested + Deserialize<'de>> Deserialize<'de> for Slot<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        read(deserializer)
    }
}

/// A `T` that deserializes from a mapping and from no other form.
///
/// A struct that derives `Deserialize` also takes a sequence: `serde` then
/// fills the fields by position. No Python reader does that. Each one
/// refuses a list where the contract gives a mapping. `MapOnly` refuses the
/// sequence form, and the read of the document then fails. A raw type that
/// is total uses a [`Slot`] there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct MapOnly<T>(pub(crate) T);

impl<'de, T: Deserialize<'de>> Deserialize<'de> for MapOnly<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct OnlyMap<T>(PhantomData<T>);

        impl<'de, T: Deserialize<'de>> Visitor<'de> for OnlyMap<T> {
            type Value = MapOnly<T>;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a mapping")
            }

            fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Self::Value, A::Error> {
                T::deserialize(MapAccessDeserializer::new(map)).map(MapOnly)
            }
        }

        deserializer.deserialize_map(OnlyMap(PhantomData))
    }
}

impl<T: Serialize> Serialize for MapOnly<T> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.0.serialize(serializer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A nested raw struct. Each field is a `Slot`, so it reads each table.
    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct RawLimit {
        #[serde(default)]
        count: Slot<i64>,
        #[serde(default)]
        unit: Slot<String>,
    }

    impl Nested for RawLimit {}

    /// A raw type with a field of each field type of the module.
    #[derive(Debug, PartialEq, Deserialize)]
    struct RawDocument {
        #[serde(default)]
        absent: Slot<String>,
        #[serde(default)]
        nothing: Slot<String>,
        #[serde(default)]
        name: Slot<String>,
        #[serde(default)]
        enabled: Slot<bool>,
        #[serde(default)]
        count: Slot<i64>,
        #[serde(default)]
        size: Slot<u64>,
        #[serde(default)]
        ratio: Slot<f64>,
        #[serde(default)]
        words: Slot<Vec<Slot<String>>>,
        #[serde(default)]
        limit: Slot<RawLimit>,
        #[serde(default)]
        labels: Slot<BTreeMap<String, Slot<String>>>,
        #[serde(default)]
        name_as_flag: Slot<String>,
        #[serde(default)]
        name_as_integer: Slot<String>,
        #[serde(default)]
        name_as_float: Slot<String>,
        #[serde(default)]
        name_as_list: Slot<String>,
        #[serde(default)]
        name_as_table: Slot<String>,
        #[serde(default)]
        count_as_text: Slot<i64>,
        #[serde(default)]
        limit_as_list: Slot<RawLimit>,
    }

    const DOCUMENT: &str = r#"{
        "nothing": null,
        "name": "thin",
        "enabled": true,
        "count": -3,
        "size": 18446744073709551615,
        "ratio": 2.5,
        "words": ["a", 5, null],
        "limit": {"count": 2, "later": [1, {"x": null}]},
        "labels": {"tier": "low", "rank": 4},
        "name_as_flag": false,
        "name_as_integer": 7,
        "name_as_float": 2.5,
        "name_as_list": ["thin", ["deep", {"k": 1}]],
        "name_as_table": {"thin": {"k": [1, 2]}},
        "count_as_text": "7",
        "limit_as_list": [2, "s"]
    }"#;

    fn text(value: &str) -> Slot<String> {
        Slot::Value(value.to_owned())
    }

    #[test]
    fn a_derived_struct_reads_each_case_of_a_field() {
        let read: RawDocument = serde_json::from_str(DOCUMENT).unwrap();

        assert_eq!(read.absent, Slot::Missing);
        assert_eq!(read.nothing, Slot::Null);

        assert_eq!(read.name, text("thin"));
        assert_eq!(read.enabled, Slot::Value(true));
        assert_eq!(read.count, Slot::Value(-3));
        assert_eq!(read.size, Slot::Value(u64::MAX));
        assert_eq!(read.ratio, Slot::Value(2.5));
        assert_eq!(
            read.words,
            Slot::Value(vec![text("a"), Slot::Other(Found::Integer), Slot::Null])
        );
        assert_eq!(
            read.limit,
            Slot::Value(RawLimit {
                count: Slot::Value(2),
                unit: Slot::Missing,
            })
        );
        assert_eq!(
            read.labels,
            Slot::Value(BTreeMap::from([
                ("rank".to_owned(), Slot::Other(Found::Integer)),
                ("tier".to_owned(), text("low")),
            ]))
        );

        assert_eq!(read.name_as_flag, Slot::Other(Found::Boolean));
        assert_eq!(read.name_as_integer, Slot::Other(Found::Integer));
        assert_eq!(read.name_as_float, Slot::Other(Found::Float));
        assert_eq!(read.name_as_list, Slot::Other(Found::List));
        assert_eq!(read.name_as_table, Slot::Other(Found::Table));
        assert_eq!(read.count_as_text, Slot::Other(Found::Text));
        assert_eq!(read.limit_as_list, Slot::Other(Found::List));
    }

    #[test]
    fn a_plain_derived_struct_takes_the_list_that_a_slot_refuses() {
        let by_place: RawLimit = serde_json::from_str(r#"[2, "s"]"#).unwrap();
        let slot: Slot<RawLimit> = serde_json::from_str(r#"[2, "s"]"#).unwrap();

        assert_eq!(by_place.count, Slot::Value(2));
        assert_eq!(by_place.unit, text("s"));
        assert_eq!(slot, Slot::Other(Found::List));
    }

    #[test]
    fn the_default_is_a_missing_key() {
        assert_eq!(Slot::<String>::default(), Slot::Missing);
        assert_eq!(Slot::<RawLimit>::default(), Slot::Missing);
        assert_eq!(
            RawLimit::default(),
            RawLimit {
                count: Slot::Missing,
                unit: Slot::Missing,
            }
        );
    }

    #[test]
    fn a_field_with_no_default_attribute_fails_for_an_absent_key() {
        #[derive(Debug, Deserialize)]
        struct NoDefault {
            #[expect(dead_code, reason = "the test reads only whether the parse fails")]
            name: Slot<String>,
        }

        assert!(serde_json::from_str::<NoDefault>(r#"{"name": null}"#).is_ok());
        assert!(serde_json::from_str::<NoDefault>("{}").is_err());
    }

    /// One JSON text of each kind but `null`.
    const KINDS: [(&str, Found); 6] = [
        ("true", Found::Boolean),
        ("7", Found::Integer),
        ("2.5", Found::Float),
        (r#""7""#, Found::Text),
        ("[7]", Found::List),
        (r#"{"count": 7}"#, Found::Table),
    ];

    /// Reads `text` as a `Slot<T>` and compares it with `expected`.
    fn holds<T>(text: &str, expected: &Slot<T>)
    where
        T: fmt::Debug + PartialEq,
        Slot<T>: for<'de> Deserialize<'de>,
    {
        let read: Slot<T> = serde_json::from_str(text).unwrap();

        assert_eq!(&read, expected, "{text}");
    }

    /// `null` is `Null` under a `Slot<T>`. Each kind outside `takes` is
    /// `Other` with that kind.
    fn takes_only<T>(takes: &[Found])
    where
        T: fmt::Debug + PartialEq,
        Slot<T>: for<'de> Deserialize<'de>,
    {
        holds("null", &Slot::<T>::Null);

        for (text, kind) in KINDS {
            if !takes.contains(&kind) {
                holds(text, &Slot::<T>::Other(kind));
            }
        }
    }

    #[test]
    fn a_text_field_takes_a_text_only() {
        takes_only::<String>(&[Found::Text]);

        holds(r#""7""#, &text("7"));
        holds(r#""""#, &text(""));
        holds(r#""caf\u00e9 \ud83d\ude00""#, &text("caf\u{e9} \u{1f600}"));
    }

    #[test]
    fn a_flag_field_takes_a_boolean_only() {
        takes_only::<bool>(&[Found::Boolean]);

        holds("true", &Slot::Value(true));
        holds("false", &Slot::Value(false));
        holds("1", &Slot::<bool>::Other(Found::Integer));
        holds("0", &Slot::<bool>::Other(Found::Integer));
        holds(r#""true""#, &Slot::<bool>::Other(Found::Text));
    }

    #[test]
    fn an_integer_field_takes_an_integer_of_its_range() {
        takes_only::<i64>(&[Found::Integer]);
        takes_only::<u64>(&[Found::Integer]);

        holds("7", &Slot::Value(7_i64));
        holds("0", &Slot::Value(0_i64));
        holds("-9223372036854775808", &Slot::Value(i64::MIN));
        holds("9223372036854775807", &Slot::Value(i64::MAX));
        holds("9223372036854775808", &Slot::<i64>::Other(Found::Integer));
        holds("18446744073709551615", &Slot::<i64>::Other(Found::Integer));

        holds("7", &Slot::Value(7_u64));
        holds("0", &Slot::Value(0_u64));
        holds("18446744073709551615", &Slot::Value(u64::MAX));
        holds("-1", &Slot::<u64>::Other(Found::Integer));
        holds("-9223372036854775808", &Slot::<u64>::Other(Found::Integer));
    }

    #[test]
    fn a_float_is_no_integer() {
        for float in ["2.0", "2e0", "-0.0", "1e3", "7.000"] {
            holds(float, &Slot::<i64>::Other(Found::Float));
            holds(float, &Slot::<u64>::Other(Found::Float));
        }

        // The kind is the kind that the reader gives. This reader gives the
        // token `-0` as a float.
        holds("-0", &Slot::<i64>::Other(Found::Float));
    }

    #[test]
    fn a_float_field_takes_a_float_and_an_integer() {
        takes_only::<f64>(&[Found::Float, Found::Integer]);

        holds("2.5", &Slot::Value(2.5));
        holds("1e-3", &Slot::Value(0.001));
        holds("7", &Slot::Value(7.0));
        holds("-3", &Slot::Value(-3.0));
        holds("0", &Slot::Value(0.0));
    }

    #[test]
    fn an_integer_under_a_float_field_is_the_nearest_float() {
        // 2^53 + 1 is halfway between two floats. The nearest float is the
        // one with an even last bit: 2^53.
        let nearest = [
            ("9007199254740993", 9_007_199_254_740_992.0),
            ("9007199254740995", 9_007_199_254_740_996.0),
            ("-9007199254740993", -9_007_199_254_740_992.0),
            ("9223372036854775807", 9_223_372_036_854_775_808.0),
            ("-9223372036854775808", -9_223_372_036_854_775_808.0),
            ("18446744073709551615", 18_446_744_073_709_551_616.0),
        ];

        for (integer, float) in nearest {
            holds(integer, &Slot::Value(float));
        }
    }

    #[test]
    fn a_float_that_is_not_finite_is_a_float() {
        type Float = de::value::F64Deserializer<de::value::Error>;

        let infinite = Slot::<f64>::deserialize(Float::new(f64::INFINITY)).unwrap();
        let not_a_number = Slot::<f64>::deserialize(Float::new(f64::NAN)).unwrap();
        let as_count = Slot::<i64>::deserialize(Float::new(f64::INFINITY)).unwrap();

        assert_eq!(infinite, Slot::Value(f64::INFINITY));
        assert!(not_a_number.value().is_some_and(|float| float.is_nan()));
        assert_eq!(as_count, Slot::Other(Found::Float));
    }

    #[test]
    fn an_integer_of_128_bits_is_an_integer() {
        type Signed = de::value::I128Deserializer<de::value::Error>;
        type Unsigned = de::value::U128Deserializer<de::value::Error>;

        let small = Slot::<i64>::deserialize(Signed::new(-5)).unwrap();
        let wide = Slot::<i64>::deserialize(Signed::new(i128::MIN)).unwrap();
        let wide_float = Slot::<f64>::deserialize(Signed::new(i128::MAX)).unwrap();
        let past_range = Slot::<f64>::deserialize(Unsigned::new(u128::MAX)).unwrap();
        let as_text = Slot::<String>::deserialize(Unsigned::new(5)).unwrap();
        let as_count = Slot::<u64>::deserialize(Unsigned::new(5)).unwrap();

        assert_eq!(small, Slot::Value(-5));
        assert_eq!(wide, Slot::Other(Found::Integer));
        assert_eq!(wide_float, Slot::Value(2.0_f64.powi(127)));
        assert_eq!(past_range, Slot::Other(Found::Integer));
        assert_eq!(as_text, Slot::Other(Found::Integer));
        assert_eq!(as_count, Slot::Value(5));
    }

    #[test]
    fn a_list_field_takes_a_list_only() {
        type Words = Vec<Slot<String>>;

        takes_only::<Words>(&[Found::List]);

        holds("[]", &Slot::Value(Words::new()));
        holds(
            r#"["a", 7]"#,
            &Slot::Value(vec![text("a"), Slot::Other(Found::Integer)]),
        );
    }

    #[test]
    fn an_item_of_a_wrong_kind_keeps_its_place() {
        let rows: Slot<Vec<Slot<RawLimit>>> =
            serde_json::from_str(r#"["s1", {"count": 2}, null, [3, "s"], {}]"#).unwrap();

        assert_eq!(
            rows,
            Slot::Value(vec![
                Slot::Other(Found::Text),
                Slot::Value(RawLimit {
                    count: Slot::Value(2),
                    unit: Slot::Missing,
                }),
                Slot::Null,
                Slot::Other(Found::List),
                Slot::Value(RawLimit::default()),
            ])
        );
    }

    #[test]
    fn a_nested_field_takes_a_table_only() {
        takes_only::<RawLimit>(&[Found::Table]);

        holds("{}", &Slot::Value(RawLimit::default()));
        holds(
            r#"{"count": 7}"#,
            &Slot::Value(RawLimit {
                count: Slot::Value(7),
                unit: Slot::Missing,
            }),
        );
        holds(
            r#"{"count": "7", "unit": [], "other": {"deep": [1]}}"#,
            &Slot::Value(RawLimit {
                count: Slot::Other(Found::Text),
                unit: Slot::Other(Found::List),
            }),
        );
    }

    #[test]
    fn a_table_with_free_keys_is_a_nested_field() {
        type Labels = BTreeMap<String, Slot<String>>;

        takes_only::<Labels>(&[Found::Table]);

        holds("{}", &Slot::Value(Labels::new()));
        holds(
            r#"{"b": "low", "a": 4, "c": null}"#,
            &Slot::Value(Labels::from([
                ("a".to_owned(), Slot::Other(Found::Integer)),
                ("b".to_owned(), text("low")),
                ("c".to_owned(), Slot::Null),
            ])),
        );
    }

    /// Each type has the impl with the marker `()`. A [`Lenient`] type also
    /// has the impl with the marker `u8`. A call that names no marker then
    /// builds only for a type that is not `Lenient`.
    trait NotLenient<Marker> {
        fn holds() {}
    }

    impl<T> NotLenient<()> for T {}
    impl<T: Lenient> NotLenient<u8> for T {}

    /// The same pair of impls for [`Nested`].
    trait NotNested<Marker> {
        fn holds() {}
    }

    impl<T> NotNested<()> for T {}
    impl<T: Nested> NotNested<u8> for T {}

    /// The same pair of impls for a type that `serde` reads.
    trait NoReader<Marker> {
        fn holds() {}
    }

    impl<T> NoReader<()> for T {}
    impl<T: for<'de> Deserialize<'de>> NoReader<u8> for T {}

    /// Each line fails the build when its type gets the trait that the line
    /// names. The test has no assertion: the build is the check.
    #[test]
    fn a_list_item_and_a_free_value_must_be_a_slot() {
        <String as NotLenient<_>>::holds();
        <RawLimit as NotLenient<_>>::holds();
        <Option<RawLimit> as NotLenient<_>>::holds();
        <BTreeMap<String, String> as NotNested<_>>::holds();

        <Slot<Vec<String>> as NoReader<_>>::holds();
        <Slot<Vec<RawLimit>> as NoReader<_>>::holds();
        <Slot<BTreeMap<String, String>> as NoReader<_>>::holds();
        // `Row` derives its reader and has no impl of `Nested`.
        <Slot<Row> as NoReader<_>>::holds();
    }

    #[test]
    fn a_nested_struct_that_refuses_a_key_fails_the_read() {
        #[derive(Debug, PartialEq, Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Strict {
            #[serde(default)]
            count: Slot<i64>,
        }

        impl Nested for Strict {}

        holds(
            r#"{"count": 1}"#,
            &Slot::Value(Strict {
                count: Slot::Value(1),
            }),
        );

        assert!(serde_json::from_str::<Slot<Strict>>(r#"{"other": 1}"#).is_err());
    }

    #[test]
    fn a_value_that_a_field_does_not_take_is_read_to_its_end() {
        #[derive(Debug, PartialEq, Deserialize)]
        struct Pair {
            #[serde(default)]
            first: Slot<String>,
            #[serde(default)]
            second: Slot<String>,
        }

        let texts = [
            (
                r#"{"first": [[1, {"a": [true, null]}], {"b": {"c": "x"}}], "second": "kept"}"#,
                Found::List,
            ),
            (
                r#"{"first": {"a": [1, {"b": []}], "c": {}}, "second": "kept"}"#,
                Found::Table,
            ),
        ];

        for (document, kind) in texts {
            let read: Pair = serde_json::from_str(document).unwrap();

            assert_eq!(read.first, Slot::Other(kind), "{document}");
            assert_eq!(read.second, text("kept"), "{document}");
        }
    }

    #[test]
    fn a_text_that_is_no_document_fails_the_read() {
        for broken in [
            r#"{"name": tru}"#,
            r#"{"name": [1, }"#,
            r#"{"name": {"a": }}"#,
            r#"{"name": "thin""#,
        ] {
            assert!(
                serde_json::from_str::<RawDocument>(broken).is_err(),
                "{broken}"
            );
        }
    }

    #[test]
    fn a_field_beside_a_flattened_field_reads_each_case() {
        #[derive(Debug, Deserialize)]
        struct WithRest {
            #[serde(default)]
            name: Slot<String>,
            #[serde(default)]
            count: Slot<i64>,
            #[serde(default)]
            ratio: Slot<f64>,
            #[serde(default)]
            limit: Slot<RawLimit>,
            #[serde(default)]
            absent: Slot<bool>,
            #[serde(default)]
            nothing: Slot<bool>,
            #[serde(flatten)]
            rest: BTreeMap<String, IgnoredAny>,
        }

        let document = r#"{
            "zz": 1,
            "name": {"k": [1, {"deep": null}]},
            "count": -2,
            "ratio": 3,
            "limit": [1],
            "nothing": null,
            "aa": []
        }"#;
        let read: WithRest = serde_json::from_str(document).unwrap();
        let rest: Vec<&str> = read.rest.keys().map(String::as_str).collect();

        assert_eq!(read.name, Slot::Other(Found::Table));
        assert_eq!(read.count, Slot::Value(-2));
        assert_eq!(read.ratio, Slot::Value(3.0));
        assert_eq!(read.limit, Slot::Other(Found::List));
        assert_eq!(read.absent, Slot::Missing);
        assert_eq!(read.nothing, Slot::Null);
        assert_eq!(rest, ["aa", "zz"]);
    }

    /// A reader that gives a value in a form that no JSON text has.
    enum Wrapped {
        Some(&'static str),
        None,
        Newtype(&'static str),
    }

    impl<'de> Deserializer<'de> for Wrapped {
        type Error = de::value::Error;

        fn deserialize_any<V: Visitor<'de>>(self, visitor: V) -> Result<V::Value, Self::Error> {
            type Inner = de::value::StrDeserializer<'static, de::value::Error>;

            match self {
                Self::Some(text) => visitor.visit_some(Inner::new(text)),
                Self::None => visitor.visit_none(),
                Self::Newtype(text) => visitor.visit_newtype_struct(Inner::new(text)),
            }
        }

        serde::forward_to_deserialize_any! {
            bool i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 f32 f64 char str string
            bytes byte_buf option unit unit_struct newtype_struct seq tuple
            tuple_struct map struct enum identifier ignored_any
        }
    }

    #[test]
    fn a_value_inside_a_wrapper_reads_as_the_value() {
        let some = Slot::<String>::deserialize(Wrapped::Some("thin")).unwrap();
        let some_flag = Slot::<bool>::deserialize(Wrapped::Some("thin")).unwrap();
        let none = Slot::<String>::deserialize(Wrapped::None).unwrap();
        let newtype = Slot::<String>::deserialize(Wrapped::Newtype("thin")).unwrap();
        let newtype_flag = Slot::<bool>::deserialize(Wrapped::Newtype("thin")).unwrap();

        assert_eq!(some, text("thin"));
        assert_eq!(some_flag, Slot::Other(Found::Text));
        assert_eq!(none, Slot::Null);
        assert_eq!(newtype, text("thin"));
        assert_eq!(newtype_flag, Slot::Other(Found::Text));
    }

    #[test]
    fn a_kind_outside_the_set_fails_the_read() {
        type Bytes = de::value::BytesDeserializer<'static, de::value::Error>;

        let error = Slot::<String>::deserialize(Bytes::new(b"thin")).unwrap_err();

        assert_eq!(
            error.to_string(),
            "invalid type: byte array, expected a text"
        );
    }

    /// A field type of another module, with a reader of its own.
    mod elsewhere {
        use serde::{Deserialize, Deserializer};

        use crate::slot::{Found, Slot};

        /// An integer that is a multiple of two.
        #[derive(Debug, PartialEq)]
        pub(super) struct Even(pub(super) u64);

        impl<'de> Deserialize<'de> for Slot<Even> {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                Ok(match Slot::<u64>::deserialize(deserializer)? {
                    Slot::Value(integer) if integer % 2 == 0 => Slot::Value(Even(integer)),
                    Slot::Value(_) => Slot::Other(Found::Integer),
                    Slot::Missing => Slot::Missing,
                    Slot::Null => Slot::Null,
                    Slot::Other(kind) => Slot::Other(kind),
                })
            }
        }
    }

    #[test]
    fn another_module_adds_a_field_type_of_its_own() {
        use elsewhere::Even;

        #[derive(Debug, Deserialize)]
        struct Row {
            #[serde(default)]
            pairs: Slot<Even>,
            #[serde(default)]
            odd: Slot<Even>,
            #[serde(default)]
            absent: Slot<Even>,
            #[serde(default)]
            word: Slot<Even>,
            #[serde(default)]
            each: Slot<Vec<Slot<Even>>>,
        }

        let document = r#"{"pairs": 4, "odd": 3, "word": "four", "each": [2, 5, null]}"#;
        let read: Row = serde_json::from_str(document).unwrap();

        assert_eq!(read.pairs, Slot::Value(Even(4)));
        assert_eq!(read.odd, Slot::Other(Found::Integer));
        assert_eq!(read.absent, Slot::Missing);
        assert_eq!(read.word, Slot::Other(Found::Text));
        assert_eq!(
            read.each,
            Slot::Value(vec![
                Slot::Value(Even(2)),
                Slot::Other(Found::Integer),
                Slot::Null,
            ])
        );
    }

    #[test]
    fn a_slot_with_no_value_is_the_empty_text_and_not_true() {
        assert_eq!(text("thin").text(), "thin");
        assert_eq!(Slot::<String>::Null.text(), "");
        assert_eq!(Slot::<String>::Missing.text(), "");
        assert_eq!(Slot::<String>::Other(Found::Integer).text(), "");

        assert!(Slot::Value(true).is_true());
        assert!(!Slot::Value(false).is_true());
        assert!(!Slot::<bool>::Null.is_true());
        assert!(!Slot::<bool>::Missing.is_true());
        assert!(!Slot::<bool>::Other(Found::Text).is_true());
    }

    #[derive(Debug, PartialEq, Deserialize, Serialize)]
    struct Row {
        command: String,
        #[serde(default)]
        args: Vec<String>,
    }

    #[test]
    fn a_derived_struct_takes_a_sequence() {
        let row: Row = serde_json::from_str(r#"["run", ["-v"]]"#).unwrap();

        assert_eq!(row.command, "run");
    }

    #[test]
    fn a_map_only_struct_takes_a_mapping_and_no_sequence() {
        let row: MapOnly<Row> = serde_json::from_str(r#"{"command": "run"}"#).unwrap();

        assert_eq!(row.0.command, "run");
        assert_eq!(
            serde_json::to_string(&row).unwrap(),
            r#"{"command":"run","args":[]}"#
        );
        for text in [r#"["run", ["-v"]]"#, r#""run""#, "null", "7", "true"] {
            assert!(
                serde_json::from_str::<MapOnly<Row>>(text).is_err(),
                "{text}"
            );
        }
    }
}
