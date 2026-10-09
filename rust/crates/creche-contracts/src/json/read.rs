//! What a caller reads from a strict text: a typed value, and a number that
//! keeps the kind of its token.
//!
//! For the text `-0`, a visitor of `serde_json` gets `visit_f64` with
//! `-0.0`. Python `json.loads` returns the `int` 0 for the same text. A
//! number type that takes its value from a visitor thus cannot tell `-0`
//! from `-0.0`. [`Number`] and [`Integer`] ask `serde_json` for the text of
//! the token and decide from its characters.

use std::error::Error;
use std::fmt;

use serde::de::{self, DeserializeOwned, Unexpected};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::value::RawValue;

use super::scan::{float_value, integer_value, is_integer};
use super::{ByteCap, Found, NotStrict, check};
use crate::slot::Slot;

/// What a reader of this module wants, in the words of an error of `serde`.
const WANTS_NUMBER: &str = "a number";
const WANTS_INTEGER: &str = "an integer";

/// What the error of `serde` says for a number that the type cannot hold.
const INTEGER_OUTSIDE: &str = "an integer outside the range of 64 bits";
const FLOAT_OUTSIDE: &str = "a float that is not finite";

/// 2^32 as a float.
const TWO_POW_32: f64 = 4_294_967_296.0;

/// Reads `bytes` as a `T`: [`check`], then [`StrictText::parse`].
///
/// A module that needs the kind of the top-level value, or a type that
/// borrows from the text, calls the two functions itself.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, ReadError, Rule};
///
/// const CAP: ByteCap = ByteCap::new(64);
///
/// assert_eq!(json::read::<Vec<u8>>(b"[1, 2]", CAP)?, [1, 2]);
///
/// let refused = json::read::<Vec<u8>>(b"[1, 2,]", CAP).unwrap_err();
/// assert_eq!(refused.not_strict().map(|refusal| refusal.rule()), Some(Rule::Syntax));
/// assert!(matches!(json::read::<Vec<u8>>(b"[1, -2]", CAP), Err(ReadError::Shape(_))));
/// # Ok::<(), ReadError>(())
/// ```
///
/// # Errors
///
/// [`ReadError::NotStrict`] for a text that `check` refuses, and
/// [`ReadError::Shape`] for a strict text that `T` does not take.
///
/// [`StrictText::parse`]: super::StrictText::parse
pub fn read<T: DeserializeOwned>(bytes: &[u8], cap: ByteCap) -> Result<T, ReadError> {
    let text = check(bytes, cap)?;

    Ok(text.parse()?)
}

/// Where a type refuses the value of a strict text.
///
/// The value holds a line and a column of the text, each from 1, as
/// `serde_json` counts them. Both are 0 when `serde_json` gives no place.
/// That is the case when [`Integer`] or [`Number`] refuses the top-level
/// value.
///
/// `serde_json` puts a part of its input into some messages. This type
/// drops the message and keeps the two numbers only, so a log line can
/// print it.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, ReadError, Shape};
///
/// let refused = json::read::<Vec<u8>>(b"[1,\n \"two\"]", ByteCap::new(64));
/// let Err(ReadError::Shape(shape)) = refused else {
///     panic!("a list of numbers takes no text");
/// };
/// let shape: Shape = shape;
///
/// assert_eq!((shape.line(), shape.column()), (2, 6));
/// assert_eq!(shape.to_string(), "line 2, column 6");
/// ```
///
/// Only a read of this module builds a value:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, ByteCap, ReadError, Shape};
///
/// let shape = Shape { line: 2, column: 6 };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Shape {
    line: usize,
    column: usize,
}

impl Shape {
    /// The place of an error of `serde_json`, without its message.
    pub(super) fn of(error: &serde_json::Error) -> Self {
        Self {
            line: error.line(),
            column: error.column(),
        }
    }

    /// The line of the text, from 1.
    #[must_use]
    pub const fn line(&self) -> usize {
        self.line
    }

    /// The column of the line, from 1.
    #[must_use]
    pub const fn column(&self) -> usize {
        self.column
    }
}

impl fmt::Display for Shape {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}, column {}", self.line, self.column)
    }
}

impl Error for Shape {}

/// Why [`read`] gives no value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadError {
    /// The text is not strict JSON.
    NotStrict(NotStrict),
    /// The text is strict JSON, and the type does not take its value.
    Shape(Shape),
}

impl ReadError {
    /// The refusal of the check, for a text that is not strict JSON. A
    /// service builds the notice for the operator from this value.
    #[must_use]
    pub const fn not_strict(&self) -> Option<&NotStrict> {
        match self {
            Self::NotStrict(refusal) => Some(refusal),
            Self::Shape(_) => None,
        }
    }
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotStrict(refusal) => write!(f, "the text is not strict JSON: {refusal}"),
            Self::Shape(shape) => write!(f, "the JSON value has another shape: {shape}"),
        }
    }
}

impl Error for ReadError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::NotStrict(refusal) => Some(refusal),
            Self::Shape(shape) => Some(shape),
        }
    }
}

impl From<NotStrict> for ReadError {
    fn from(refusal: NotStrict) -> Self {
        Self::NotStrict(refusal)
    }
}

impl From<Shape> for ReadError {
    fn from(shape: Shape) -> Self {
        Self::Shape(shape)
    }
}

/// An integer of a strict text: a value from the smallest `i64` to the
/// largest `u64`.
///
/// The type reads from a JSON text only. Its reader takes the text of the
/// token, so `-0` is the integer 0, and `1.0` and `1e3` are no integers.
/// [`Number`] has the limits of such a reader.
///
/// The type has no `Default`. A raw type gives an absent field a value of
/// its own.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, Integer};
///
/// const CAP: ByteCap = ByteCap::new(64);
///
/// let zero: Integer = json::read(b"-0", CAP)?;
/// assert_eq!(zero, Integer::from(0_u64));
/// assert_eq!(zero.to_string(), "0");
///
/// let large: Integer = json::read(b"18446744073709551615", CAP)?;
/// assert_eq!(large.to_u64(), Some(u64::MAX));
/// assert_eq!(large.to_i64(), None);
/// assert!(json::read::<Integer>(b"1.0", CAP).is_err());
/// # Ok::<(), json::ReadError>(())
/// ```
///
/// Code outside this module cannot build a value from a wider number:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, ByteCap, Integer};
///
/// let wide = Integer { value: i128::MAX };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Integer {
    /// From `i64::MIN` to `u64::MAX`.
    value: i128,
}

impl Integer {
    /// The value of an integer token. `None` for a value outside the range
    /// of the type, which is the range of rule 7 of the check.
    fn of_token(token: &str) -> Option<Self> {
        integer_value(token).map(|value| Self { value })
    }

    /// The value, when an `i64` holds it.
    #[must_use]
    pub fn to_i64(self) -> Option<i64> {
        i64::try_from(self.value).ok()
    }

    /// The value, when a `u64` holds it.
    #[must_use]
    pub fn to_u64(self) -> Option<u64> {
        u64::try_from(self.value).ok()
    }

    /// The nearest float of 64 bits. An integer past 2^53 can have no float
    /// of its own.
    #[must_use]
    pub fn to_f64(self) -> f64 {
        // The value is below 2^64 in size, so the first eight bytes are 0.
        let [_, _, _, _, _, _, _, _, a, b, c, d, e, f, g, h] =
            self.value.unsigned_abs().to_be_bytes();
        let high = f64::from(u32::from_be_bytes([a, b, c, d]));
        let low = f64::from(u32::from_be_bytes([e, f, g, h]));
        // A float holds each of the two terms with no rounding, so the sum
        // rounds one time, to the nearest float.
        let size = high * TWO_POW_32 + low;

        if self.value < 0 { -size } else { size }
    }
}

impl From<i64> for Integer {
    fn from(value: i64) -> Self {
        Self {
            value: i128::from(value),
        }
    }
}

impl From<u64> for Integer {
    fn from(value: u64) -> Self {
        Self {
            value: i128::from(value),
        }
    }
}

/// The digits of the value, with `-` for a negative value.
impl fmt::Display for Integer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.value.fmt(f)
    }
}

/// A JSON integer: the digits of the value.
impl Serialize for Integer {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match (self.to_u64(), self.to_i64()) {
            (Some(value), _) => serializer.serialize_u64(value),
            (None, Some(value)) => serializer.serialize_i64(value),
            // The type holds no such value. The digits are still right.
            (None, None) => serializer.serialize_i128(self.value),
        }
    }
}

impl<'de> Deserialize<'de> for Integer {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let token = Token::read(deserializer)?;
        match token.kind {
            Found::Integer => {
                Self::of_token(token.text).ok_or_else(|| de::Error::custom(INTEGER_OUTSIDE))
            }
            other => Err(de::Error::invalid_type(unexpected(other), &WANTS_INTEGER)),
        }
    }
}

/// A number of a JSON text, with the kind of its token.
///
/// A token with no `.`, no `e` and no `E` is an integer. Each other token is
/// a float. Thus `-0` is the integer 0, `-0.0` is a float and `1e3` is a
/// float.
///
/// The type is a part of a raw type. It has no check of its own: the
/// conversion to the valid type checks the value.
///
/// # The limits of the reader
///
/// The reader takes the text of the token from `serde_json`, as a
/// `RawValue`. So it reads only from a JSON text that `serde_json` holds as
/// a whole: [`StrictText::parse`] and [`read`] give it one.
///
/// - The type works as a named field of a struct, as an item of a list and
///   as the value that a map visitor reads.
/// - The type works as a named field beside a field with `#[serde(flatten)]`.
/// - The type fails under `#[serde(flatten)]` and inside an enum with
///   `#[serde(untagged)]`. `serde` reads such a value from a buffer of its
///   own, and that buffer keeps no text of a token. A raw type that keeps
///   the value of an unknown key writes its `Deserialize` by hand, with a
///   map visitor.
///
/// Without [`check`], the reader refuses an integer outside 64 bits and a
/// float that is not finite.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, Integer, Number};
///
/// const CAP: ByteCap = ByteCap::new(64);
///
/// let numbers: Vec<Number> = json::read(b"[-0, -0.0, 1e3, 7]", CAP)?;
///
/// assert_eq!(numbers[0], Number::Integer(Integer::from(0_i64)));
/// assert!(matches!(numbers[1], Number::Float(zero) if zero.is_sign_negative()));
/// assert_eq!(numbers[2], Number::Float(1000.0));
/// assert_eq!(numbers[3], Number::Integer(Integer::from(7_i64)));
/// # Ok::<(), json::ReadError>(())
/// ```
///
/// [`StrictText::parse`]: super::StrictText::parse
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Number {
    /// A token with no fraction and no exponent.
    Integer(Integer),
    /// A token with a fraction or an exponent, as the nearest float.
    Float(f64),
}

impl<'de> Deserialize<'de> for Number {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let token = Token::read(deserializer)?;
        match token.kind {
            Found::Integer => Integer::of_token(token.text)
                .map(Self::Integer)
                .ok_or_else(|| de::Error::custom(INTEGER_OUTSIDE)),
            Found::Float => float_value(token.text)
                .map(Self::Float)
                .ok_or_else(|| de::Error::custom(FLOAT_OUTSIDE)),
            other => Err(de::Error::invalid_type(unexpected(other), &WANTS_NUMBER)),
        }
    }
}

/// An integer field of a raw type. A value of another kind is `Other` with
/// that kind, and a float is `Other(Found::Float)`. The read fails for no
/// value: an integer outside the range of [`Integer`] is
/// `Other(Found::Integer)`.
impl<'de> Deserialize<'de> for Slot<Integer> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let token = Token::read(deserializer)?;

        Ok(match token.kind {
            Found::Null => Self::Null,
            Found::Integer => {
                Integer::of_token(token.text).map_or(Self::Other(Found::Integer), Self::Value)
            }
            other => Self::Other(other),
        })
    }
}

/// A number field of a raw type. A value of another kind is `Other` with
/// that kind. The read fails for no value: a number that [`Number`] cannot
/// hold is `Other` with the kind of its token.
impl<'de> Deserialize<'de> for Slot<Number> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let token = Token::read(deserializer)?;
        let number = match token.kind {
            Found::Null => return Ok(Self::Null),
            Found::Integer => Integer::of_token(token.text).map(Number::Integer),
            Found::Float => float_value(token.text).map(Number::Float),
            other => return Ok(Self::Other(other)),
        };

        Ok(number.map_or(Self::Other(token.kind), Self::Value))
    }
}

/// The text of one JSON value, with its kind.
struct Token<'a> {
    kind: Found,
    text: &'a str,
}

impl<'de> Token<'de> {
    /// Takes the text of the next value from `serde_json`.
    fn read<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = <&'de RawValue>::deserialize(deserializer)?.get();

        Ok(Self {
            kind: kind_of(text),
            text,
        })
    }
}

/// The kind of one JSON value. The first byte of its text gives the kind,
/// and the text of a number says if the number is an integer.
fn kind_of(text: &str) -> Found {
    match text.as_bytes().first() {
        Some(b'n') => Found::Null,
        Some(b't' | b'f') => Found::Boolean,
        Some(b'"') => Found::Text,
        Some(b'[') => Found::List,
        Some(b'{') => Found::Table,
        _ if is_integer(text) => Found::Integer,
        _ => Found::Float,
    }
}

/// A kind in the words of an error of `serde`. The words hold no byte of
/// the text.
const fn unexpected(kind: Found) -> Unexpected<'static> {
    Unexpected::Other(match kind {
        Found::Null => "null",
        Found::Boolean => "a boolean",
        Found::Integer => "an integer",
        Found::Float => "a float",
        Found::Text => "a text",
        Found::List => "a list",
        Found::Table => "a table",
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde::de::{IgnoredAny, MapAccess, Visitor};

    use super::super::Rule;
    use super::*;
    use crate::slot::Nested;
    use crate::slot::tests::reads_empty_table;

    const CAP: ByteCap = ByteCap::new(4096);

    fn integer(text: &str) -> Integer {
        read(text.as_bytes(), CAP).unwrap()
    }

    fn number(text: &str) -> Number {
        read(text.as_bytes(), CAP).unwrap()
    }

    /// Each integer token, with the digits that the value writes.
    const INTEGERS: [(&str, &str); 11] = [
        ("0", "0"),
        ("-0", "0"),
        ("7", "7"),
        ("-7", "-7"),
        ("10", "10"),
        ("9007199254740993", "9007199254740993"),
        ("9223372036854775807", "9223372036854775807"),
        ("9223372036854775808", "9223372036854775808"),
        ("18446744073709551615", "18446744073709551615"),
        ("-9223372036854775808", "-9223372036854775808"),
        (" 7 ", "7"),
    ];

    /// Each text that is no integer of the type, read with no check.
    const NOT_INTEGERS: [&str; 16] = [
        "18446744073709551616",
        "-9223372036854775809",
        "340282366920938463463374607431768211456",
        "1.0",
        "-0.0",
        "1e3",
        "1E3",
        "7.000",
        "1e999",
        "null",
        "true",
        "false",
        "\"7\"",
        "[7]",
        "{\"count\": 7}",
        "",
    ];

    #[test]
    fn an_integer_token_reads_as_its_value_and_writes_its_digits() {
        for (text, digits) in INTEGERS {
            let value = integer(text);

            assert_eq!(value.to_string(), digits, "{text}");
            assert_eq!(serde_json::to_string(&value).unwrap(), digits, "{text}");
            assert_eq!(number(text), Number::Integer(value), "{text}");
        }
    }

    #[test]
    fn a_text_that_is_no_integer_of_the_type_fails_the_read() {
        for text in NOT_INTEGERS {
            assert!(serde_json::from_str::<Integer>(text).is_err(), "{text}");
        }
    }

    #[test]
    fn the_token_minus_zero_is_the_integer_zero() {
        let zero = integer("-0");

        assert_eq!(zero, Integer::from(0_i64));
        assert_eq!(zero, Integer::from(0_u64));
        assert_eq!(zero.to_i64(), Some(0));
        assert_eq!(zero.to_u64(), Some(0));
        assert!(zero.to_f64().is_sign_positive());
        assert_eq!(zero.to_string(), "0");
        assert_eq!(serde_json::to_string(&zero).unwrap(), "0");
        assert_eq!(number("-0"), Number::Integer(zero));

        // `serde_json` alone gives the token as a float.
        let alone: serde_json::Value = serde_json::from_str("-0").unwrap();
        assert!(alone.is_f64());
        assert_eq!(serde_json::to_string(&alone).unwrap(), "-0.0");
    }

    #[test]
    fn a_token_with_a_fraction_or_an_exponent_is_a_float() {
        let floats = [
            ("0.0", 0.0),
            ("1.0", 1.0),
            ("1e3", 1000.0),
            ("1E3", 1000.0),
            ("1e+3", 1000.0),
            ("7.000", 7.0),
            ("2.5", 2.5),
            ("-2.5e-1", -0.25),
            ("1e-999", 0.0),
            ("18446744073709551616.0", 18_446_744_073_709_551_616.0),
            ("1.7976931348623157e308", f64::MAX),
            ("1.7976931348623158e308", f64::MAX),
            ("5e-324", f64::from_bits(1)),
            ("0.1", 0.1),
            ("9007199254740993.0", 9_007_199_254_740_992.0),
        ];

        for (text, float) in floats {
            assert_eq!(number(text), Number::Float(float), "{text}");
            // The value is the float that `serde_json` gives for the token.
            assert_eq!(
                serde_json::from_str::<f64>(text).unwrap().to_bits(),
                float.to_bits(),
                "{text}"
            );
        }

        let Number::Float(zero) = number("-0.0") else {
            panic!("-0.0 is a float");
        };
        assert!(zero == 0.0 && zero.is_sign_negative());
    }

    #[test]
    fn a_long_float_token_has_one_value_for_each_reader() {
        const LONG: ByteCap = ByteCap::new(1_048_576);

        // Each token has a long run of zeros, which moves its value by as
        // many places as the exponent moves it back. A reader that drops a
        // digit of the exponent gives 0 or no finite value.
        let rows: [(String, f64); 5] = [
            (format!("0.{}1e66001", "0".repeat(66_000)), 1.0),
            (format!("0.{}1e700001", "0".repeat(700_000)), 1.0),
            (format!("0.{}1e655660", "0".repeat(655_359)), 1e300),
            (format!("1{}e-655360", "0".repeat(66_000)), 0.0),
            (format!("-1{}e-655360", "0".repeat(655_360)), -1.0),
        ];

        for (text, float) in rows {
            let strict = check(text.as_bytes(), LONG).unwrap();
            let Ok(Number::Float(as_number)) = strict.parse::<Number>() else {
                panic!("the token is a float");
            };
            let in_a_slot: Slot<Number> = strict.parse().unwrap();

            assert_eq!(as_number.to_bits(), float.to_bits());
            assert_eq!(strict.parse::<f64>().map(f64::to_bits), Ok(float.to_bits()));
            assert_eq!(in_a_slot, Slot::Value(Number::Float(float)));
        }
    }

    #[test]
    fn a_number_reads_no_value_of_another_kind() {
        for text in ["null", "true", "\"7\"", "[7]", "{\"count\": 7}"] {
            assert!(matches!(
                read::<Number>(text.as_bytes(), CAP),
                Err(ReadError::Shape(_))
            ));
        }
    }

    #[test]
    fn a_number_outside_a_strict_text_fails_the_read_with_no_check() {
        for text in ["1e999", "-1e999", "18446744073709551616", "1e309"] {
            assert!(serde_json::from_str::<Number>(text).is_err(), "{text}");
        }
    }

    #[test]
    fn an_integer_converts_to_each_type_that_holds_it() {
        let rows = [
            ("0", Some(0_i64), Some(0_u64)),
            ("7", Some(7), Some(7)),
            ("-7", Some(-7), None),
            (
                "9223372036854775807",
                Some(i64::MAX),
                Some(9_223_372_036_854_775_807),
            ),
            ("9223372036854775808", None, Some(9_223_372_036_854_775_808)),
            ("18446744073709551615", None, Some(u64::MAX)),
            ("-9223372036854775808", Some(i64::MIN), None),
        ];

        for (text, signed, unsigned) in rows {
            let value = integer(text);

            assert_eq!(value.to_i64(), signed, "{text}");
            assert_eq!(value.to_u64(), unsigned, "{text}");
            assert_eq!(signed.map(Integer::from).unwrap_or(value), value, "{text}");
            assert_eq!(
                unsigned.map(Integer::from).unwrap_or(value),
                value,
                "{text}"
            );
        }
    }

    #[test]
    fn an_integer_converts_to_the_nearest_float() {
        // 2^53 + 1 is halfway between two floats. The nearest float is the
        // one with an even last bit: 2^53.
        let nearest = [
            ("0", 0.0),
            ("7", 7.0),
            ("-7", -7.0),
            ("4294967295", 4_294_967_295.0),
            ("4294967296", 4_294_967_296.0),
            ("9007199254740992", 9_007_199_254_740_992.0),
            ("9007199254740993", 9_007_199_254_740_992.0),
            ("9007199254740995", 9_007_199_254_740_996.0),
            ("-9007199254740993", -9_007_199_254_740_992.0),
            ("9223372036854775807", 9_223_372_036_854_775_808.0),
            ("-9223372036854775808", -9_223_372_036_854_775_808.0),
            ("18446744073709551615", 18_446_744_073_709_551_616.0),
        ];

        for (text, float) in nearest {
            assert_eq!(integer(text).to_f64(), float, "{text}");
            // The decimal text of the integer rounds to the same float.
            assert_eq!(text.parse::<f64>().unwrap(), float, "{text}");
        }
    }

    /// A nested raw struct with a number field of each of the two types.
    #[derive(Debug, Default, PartialEq, Deserialize)]
    struct RawLimit {
        #[serde(default)]
        count: Slot<Integer>,
        #[serde(default)]
        ratio: Slot<Number>,
    }

    impl Nested for RawLimit {}

    fn count(value: i64) -> Slot<Integer> {
        Slot::Value(Integer::from(value))
    }

    /// One JSON text of each kind, with the kind.
    const KINDS: [(&str, Found); 6] = [
        ("true", Found::Boolean),
        ("7", Found::Integer),
        ("2.5", Found::Float),
        (r#""7""#, Found::Text),
        ("[7]", Found::List),
        (r#"{"count": 7}"#, Found::Table),
    ];

    #[test]
    fn an_integer_field_reads_each_case() {
        #[derive(Debug, Deserialize)]
        struct Raw {
            #[serde(default)]
            missing: Slot<Integer>,
            #[serde(default)]
            null: Slot<Integer>,
            #[serde(default)]
            text: Slot<Integer>,
            #[serde(default)]
            float: Slot<Integer>,
            #[serde(default)]
            whole_float: Slot<Integer>,
            #[serde(default)]
            integer: Slot<Integer>,
            #[serde(default)]
            minus_zero: Slot<Integer>,
            #[serde(default)]
            largest: Slot<Integer>,
            #[serde(default)]
            flag: Slot<Integer>,
            #[serde(default)]
            list: Slot<Integer>,
            #[serde(default)]
            table: Slot<Integer>,
        }

        let document = br#"{
            "null": null,
            "text": "7",
            "float": 2.5,
            "whole_float": 7.0,
            "integer": 7,
            "minus_zero": -0,
            "largest": 18446744073709551615,
            "flag": true,
            "list": [7, [8, {"k": 9}]],
            "table": {"count": {"deep": [7]}}
        }"#;
        let raw: Raw = read(document, CAP).unwrap();

        assert_eq!(raw.missing, Slot::Missing);
        assert_eq!(raw.null, Slot::Null);
        assert_eq!(raw.text, Slot::Other(Found::Text));
        assert_eq!(raw.float, Slot::Other(Found::Float));
        assert_eq!(raw.whole_float, Slot::Other(Found::Float));
        assert_eq!(raw.integer, count(7));
        assert_eq!(raw.minus_zero, count(0));
        assert_eq!(raw.largest, Slot::Value(Integer::from(u64::MAX)));
        assert_eq!(raw.flag, Slot::Other(Found::Boolean));
        assert_eq!(raw.list, Slot::Other(Found::List));
        assert_eq!(raw.table, Slot::Other(Found::Table));
        assert!(reads_empty_table::<Raw>());
    }

    #[test]
    fn a_slot_of_each_number_type_reads_a_value_of_each_kind() {
        assert_eq!(read::<Slot<Integer>>(b"null", CAP), Ok(Slot::Null));
        assert_eq!(read::<Slot<Number>>(b" null ", CAP), Ok(Slot::Null));

        for (text, kind) in KINDS {
            let as_integer = read::<Slot<Integer>>(text.as_bytes(), CAP).unwrap();
            let as_number = read::<Slot<Number>>(text.as_bytes(), CAP).unwrap();

            match kind {
                Found::Integer => {
                    assert_eq!(as_integer, count(7));
                    assert_eq!(
                        as_number,
                        Slot::Value(Number::Integer(Integer::from(7_i64)))
                    );
                }
                Found::Float => {
                    assert_eq!(as_integer, Slot::Other(Found::Float));
                    assert_eq!(as_number, Slot::Value(Number::Float(2.5)));
                }
                other => {
                    assert_eq!(as_integer, Slot::Other(other), "{text}");
                    assert_eq!(as_number, Slot::Other(other), "{text}");
                }
            }
        }
    }

    #[test]
    fn a_slot_keeps_the_kind_of_a_number_that_its_type_cannot_hold() {
        // `check` refuses each of these texts. The read alone fails for none.
        let integers = ["18446744073709551616", "-9223372036854775809"];
        let floats = ["1e999", "-1e999"];

        for text in integers {
            let as_integer: Slot<Integer> = serde_json::from_str(text).unwrap();
            let as_number: Slot<Number> = serde_json::from_str(text).unwrap();

            assert_eq!(as_integer, Slot::Other(Found::Integer), "{text}");
            assert_eq!(as_number, Slot::Other(Found::Integer), "{text}");
        }
        for text in floats {
            let as_integer: Slot<Integer> = serde_json::from_str(text).unwrap();
            let as_number: Slot<Number> = serde_json::from_str(text).unwrap();

            assert_eq!(as_integer, Slot::Other(Found::Float), "{text}");
            assert_eq!(as_number, Slot::Other(Found::Float), "{text}");
        }
    }

    #[test]
    fn a_number_field_reads_inside_a_list_and_inside_a_nested_table() {
        #[derive(Debug, Deserialize)]
        struct Raw {
            #[serde(default)]
            counts: Slot<Vec<Slot<Integer>>>,
            #[serde(default)]
            limit: Slot<RawLimit>,
            #[serde(default)]
            rates: Slot<BTreeMap<String, Slot<Number>>>,
            #[serde(default)]
            rows: Slot<Vec<Slot<RawLimit>>>,
        }

        let document = br#"{
            "counts": [-0, 1.0, "2", null, 3],
            "limit": {"count": -0, "ratio": -0.0, "later": [1]},
            "rates": {"a": -0, "b": 1e3, "c": []},
            "rows": [{"count": 4}, 5, {"ratio": 6}]
        }"#;
        let raw: Raw = read(document, CAP).unwrap();

        assert_eq!(
            raw.counts,
            Slot::Value(vec![
                count(0),
                Slot::Other(Found::Float),
                Slot::Other(Found::Text),
                Slot::Null,
                count(3),
            ])
        );
        assert_eq!(
            raw.limit,
            Slot::Value(RawLimit {
                count: count(0),
                ratio: Slot::Value(Number::Float(-0.0)),
            })
        );
        assert_eq!(
            raw.rates,
            Slot::Value(BTreeMap::from([
                (
                    "a".to_owned(),
                    Slot::Value(Number::Integer(Integer::from(0_i64)))
                ),
                ("b".to_owned(), Slot::Value(Number::Float(1000.0))),
                ("c".to_owned(), Slot::Other(Found::List)),
            ]))
        );
        assert_eq!(
            raw.rows,
            Slot::Value(vec![
                Slot::Value(RawLimit {
                    count: count(4),
                    ratio: Slot::Missing,
                }),
                Slot::Other(Found::Integer),
                Slot::Value(RawLimit {
                    count: Slot::Missing,
                    ratio: Slot::Value(Number::Integer(Integer::from(6_i64))),
                }),
            ])
        );
        assert!(reads_empty_table::<RawLimit>());
    }

    #[test]
    fn a_named_field_beside_a_flattened_field_reads_the_text_of_its_token() {
        #[derive(Debug, Deserialize)]
        struct WithRest {
            number: Number,
            #[serde(default)]
            count: Slot<Integer>,
            #[serde(default)]
            ratio: Slot<Number>,
            #[serde(default)]
            absent: Slot<Integer>,
            #[serde(flatten)]
            rest: BTreeMap<String, IgnoredAny>,
        }

        let document = br#"{
            "zz": [1, {"deep": null}],
            "number": -0,
            "count": -0,
            "ratio": -0.0,
            "aa": -0
        }"#;
        let raw: WithRest = read(document, CAP).unwrap();
        let rest: Vec<&str> = raw.rest.keys().map(String::as_str).collect();

        assert_eq!(raw.number, Number::Integer(Integer::from(0_i64)));
        assert_eq!(raw.count, count(0));
        assert_eq!(raw.ratio, Slot::Value(Number::Float(-0.0)));
        assert_eq!(raw.absent, Slot::Missing);
        assert_eq!(rest, ["aa", "zz"]);
    }

    #[test]
    fn a_number_under_a_flattened_field_fails_the_read() {
        #[derive(Debug, Deserialize)]
        struct Flat {
            #[expect(dead_code, reason = "the test reads only whether the read fails")]
            name: String,
            #[serde(flatten)]
            #[expect(dead_code, reason = "the test reads only whether the read fails")]
            rest: BTreeMap<String, Number>,
        }

        #[derive(Debug, Deserialize)]
        #[serde(untagged)]
        enum Either {
            #[expect(dead_code, reason = "the test reads only whether the read fails")]
            Number(Number),
        }

        let document = br#"{"name": "thin", "count": 7}"#;

        assert!(matches!(
            read::<Flat>(document, CAP),
            Err(ReadError::Shape(_))
        ));
        assert!(matches!(
            read::<Either>(b"7", CAP),
            Err(ReadError::Shape(_))
        ));
        // The same value reads where `serde` uses no buffer.
        assert!(read::<BTreeMap<String, IgnoredAny>>(document, CAP).is_ok());
        assert_eq!(number("7"), Number::Integer(Integer::from(7_i64)));
    }

    /// A raw type with a `Deserialize` by hand: it keeps the number of each
    /// key, in the order of the text.
    #[derive(Debug, PartialEq)]
    struct InOrder(Vec<(String, Number)>);

    impl<'de> Deserialize<'de> for InOrder {
        fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            struct Members;

            impl<'de> Visitor<'de> for Members {
                type Value = InOrder;

                fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                    f.write_str("an object of numbers")
                }

                fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<InOrder, A::Error> {
                    let mut members = Vec::new();
                    while let Some(key) = map.next_key::<String>()? {
                        members.push((key, map.next_value::<Number>()?));
                    }

                    Ok(InOrder(members))
                }
            }

            deserializer.deserialize_map(Members)
        }
    }

    #[test]
    fn a_map_visitor_reads_the_text_of_each_token() {
        let read: InOrder = read(br#"{"z": -0, "a": -0.0, "m": 1e3}"#, CAP).unwrap();

        assert_eq!(
            read,
            InOrder(vec![
                ("z".to_owned(), Number::Integer(Integer::from(0_i64))),
                ("a".to_owned(), Number::Float(-0.0)),
                ("m".to_owned(), Number::Float(1000.0)),
            ])
        );
        let InOrder(members) = read;
        assert!(matches!(members[1].1, Number::Float(zero) if zero.is_sign_negative()));
    }

    #[test]
    fn a_reader_that_holds_no_json_text_fails_the_read() {
        type Signed = de::value::I64Deserializer<de::value::Error>;

        // The token of the value is `-0.0` after the value tree: the tree
        // keeps no text. The read fails, and gives no wrong kind.
        let tree: serde_json::Value = serde_json::from_str("-0").unwrap();

        assert!(Integer::deserialize(Signed::new(7)).is_err());
        assert!(Number::deserialize(Signed::new(7)).is_err());
        assert!(Slot::<Integer>::deserialize(Signed::new(7)).is_err());
        assert!(serde_json::from_value::<Number>(tree).is_err());
    }

    #[test]
    fn a_lone_surrogate_in_a_member_that_the_type_ignores_is_refused() {
        #[derive(Debug, Deserialize)]
        struct Kept {
            #[expect(dead_code, reason = "the test reads only whether the read fails")]
            kept: u8,
        }

        let document = br#"{"kept": 1, "other": "\ud800"}"#;
        let refused = read::<Kept>(document, CAP).unwrap_err();

        assert_eq!(
            refused
                .not_strict()
                .map(|refusal| (refusal.rule(), refusal.at())),
            Some((Rule::LoneSurrogate, 22))
        );
        // `serde_json` alone does not decode a member that the type ignores.
        assert!(serde_json::from_slice::<Kept>(document).is_ok());
    }

    #[test]
    fn a_strict_text_of_another_shape_is_a_shape_error() {
        #[derive(Debug, Deserialize)]
        struct Named {
            #[expect(dead_code, reason = "the test reads only whether the read fails")]
            name: String,
        }

        let rows: [(&[u8], usize, usize); 4] = [
            (br#"{"name": 7}"#, 1, 10),
            (b"{\n  \"name\": [\"hunter2\"]\n}", 2, 10),
            (b"[]", 1, 2),
            (b"{}", 1, 2),
        ];

        for (text, line, column) in rows {
            let ReadError::Shape(shape) = read::<Named>(text, CAP).unwrap_err() else {
                panic!("the text is strict");
            };

            assert_eq!((shape.line(), shape.column()), (line, column));
            assert_eq!(shape.to_string(), format!("line {line}, column {column}"));
        }

        // `serde_json` gives no place for a top-level value that a number
        // type of this module refuses.
        for text in ["1.5", "\"7\""] {
            let ReadError::Shape(shape) = read::<Integer>(text.as_bytes(), CAP).unwrap_err() else {
                panic!("the text is strict");
            };

            assert_eq!((shape.line(), shape.column()), (0, 0), "{text}");
        }
    }

    #[test]
    fn no_error_of_a_read_holds_a_byte_of_the_text() {
        #[derive(Debug, Deserialize)]
        struct Named {
            #[expect(dead_code, reason = "the test reads only whether the read fails")]
            name: u8,
        }

        let wrong_shape = read::<Named>(br#"{"name": "hunter2"}"#, CAP).unwrap_err();
        let not_strict = read::<Named>(br#"{"name": "hunter2",}"#, CAP).unwrap_err();

        assert_eq!(
            wrong_shape.to_string(),
            "the JSON value has another shape: line 1, column 18"
        );
        assert_eq!(
            not_strict.to_string(),
            "the text is not strict JSON: syntax at byte 19"
        );
        for error in [&wrong_shape, &not_strict] {
            assert!(!format!("{error} {error:?}").contains("hunter2"));
            assert!(error.source().is_some());
        }
        assert_eq!(wrong_shape.not_strict(), None);
        assert_eq!(
            not_strict.not_strict().map(NotStrict::rule),
            Some(Rule::Syntax)
        );
    }

    #[test]
    fn the_error_of_a_number_reader_holds_no_byte_of_the_text() {
        let errors = [
            serde_json::from_str::<Integer>(r#""hunter2""#).unwrap_err(),
            serde_json::from_str::<Integer>("1.5").unwrap_err(),
            serde_json::from_str::<Integer>("18446744073709551616").unwrap_err(),
            serde_json::from_str::<Number>(r#"["hunter2"]"#).unwrap_err(),
            serde_json::from_str::<Number>("1e999").unwrap_err(),
        ];
        let messages: Vec<String> = errors.iter().map(ToString::to_string).collect();

        assert_eq!(
            messages,
            [
                "invalid type: a text, expected an integer",
                "invalid type: a float, expected an integer",
                "an integer outside the range of 64 bits",
                "invalid type: a list, expected a number",
                "a float that is not finite",
            ]
        );
    }
}
