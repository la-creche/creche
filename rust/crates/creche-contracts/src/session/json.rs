//! The JSON reader and the JSON writer of the session module.
//!
//! The reader keeps each member of an object as its JSON text. A parser then
//! reads only the members that it knows, each one by its own rule. That is how
//! the Python parsers work: a member of the wrong type is absent, and an
//! unknown member is ignored.
//!
//! The writer makes the bytes that Python's `json.dumps` makes with the
//! separators `,` and `:`. `attendance` writes each journal line, each record
//! of the event stream and each answer in that form.

use std::collections::HashMap;
use std::error::Error;
use std::fmt;
use std::io;

use serde::Serialize;
use serde::de::{Deserialize, Deserializer, MapAccess, Visitor};
use serde_json::ser::{CompactFormatter, Formatter, Serializer};
use serde_json::value::RawValue;

/// The deepest nesting of a JSON text that this module reads.
///
/// `serde_json` reads a typed value of 127 levels at most. A text of 128
/// levels is an object or an array whose members have 127 levels at most, so
/// `serde_json` reads each member of a text that this module takes.
///
// CONTRACT-QUESTION: contract 02 §3 rule 3 says that a body is JSON and gives
// no nesting limit. Python reads a text until its own recursion limit, which
// differs between two versions of the interpreter. This reader takes the limit
// above. A higher limit gives a member that `serde_json` cannot read. No
// request nests deeper than 4 levels, and the host caps a pi event at 64.
pub(super) const DEPTH_MAX: usize = 128;

/// The count of digits of the longest integer that Python reads. A text with a
/// longer integer is not JSON for the Python reader, so it is not JSON here.
pub(super) const INT_DIGITS_MAX: usize = 4300;

/// The last byte of one line.
pub(super) const LF: u8 = b'\n';

/// Why a text is not the JSON object that a parser needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Fault {
    /// The bytes are not one JSON text that this module reads.
    NotJson,
    /// The JSON text is not an object.
    NotObject,
}

/// What one JSON value is, read from its text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Kind {
    Null,
    True,
    False,
    /// A number with no fraction and no exponent. Python reads it as an `int`.
    Integer,
    /// A number with a fraction or an exponent. Python reads it as a `float`.
    Float,
    Text,
    Array,
    Object,
}

/// The kind of one JSON value.
pub(super) fn kind_of(raw: &RawValue) -> Kind {
    let text = raw.get();
    match text.as_bytes().first() {
        Some(b'"') => Kind::Text,
        Some(b'{') => Kind::Object,
        Some(b'[') => Kind::Array,
        Some(b't') => Kind::True,
        Some(b'f') => Kind::False,
        Some(b'n') => Kind::Null,
        _ if text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'-') =>
        {
            Kind::Integer
        }
        _ => Kind::Float,
    }
}

/// The text of a JSON string. `Ok(None)` for a value that is not a string.
///
/// A string with a lone surrogate has no Rust form. It is `Fault::NotJson`.
pub(super) fn text_of(raw: &RawValue) -> Result<Option<String>, Fault> {
    if kind_of(raw) != Kind::Text {
        return Ok(None);
    }

    serde_json::from_str(raw.get())
        .map(Some)
        .map_err(|_| Fault::NotJson)
}

/// The value of a JSON integer. `None` for each other value, a boolean too.
///
/// An integer outside `i128` becomes the nearest end of `i128`. Each caller
/// compares the value with a range that is far inside `i128`.
pub(super) fn int_of(raw: &RawValue) -> Option<i128> {
    if kind_of(raw) != Kind::Integer {
        return None;
    }

    let text = raw.get();
    let (negative, digits) = match text.strip_prefix('-') {
        Some(digits) => (true, digits),
        None => (false, text),
    };
    let magnitude = digits.bytes().fold(0_i128, |value, byte| {
        value
            .saturating_mul(10)
            .saturating_add(i128::from(byte.saturating_sub(b'0')))
    });

    Some(if negative {
        magnitude.saturating_neg()
    } else {
        magnitude
    })
}

/// The items of a JSON array. `None` for a value that is not an array.
pub(super) fn array_of(raw: &RawValue) -> Option<Vec<Box<RawValue>>> {
    if kind_of(raw) != Kind::Array {
        return None;
    }

    serde_json::from_str(raw.get()).ok()
}

/// The members of a JSON object. `Ok(None)` for a value that is not an object.
pub(super) fn object_of(raw: &RawValue) -> Result<Option<Object>, Fault> {
    if kind_of(raw) != Kind::Object {
        return Ok(None);
    }

    serde_json::from_str(raw.get())
        .map(Some)
        .map_err(|_| Fault::NotJson)
}

/// One JSON object: each member as its JSON text, in the order of the text.
///
/// A key that occurs twice has the value of its last occurrence, at the place
/// of its first. Python's `dict` does the same.
#[derive(Debug)]
pub(super) struct Object {
    members: Vec<(String, Box<RawValue>)>,
}

impl Object {
    /// The object that a text holds. The text is one JSON object, with
    /// optional white space around it.
    pub(super) fn read(bytes: &[u8]) -> Result<Self, Fault> {
        if !within_limits(bytes) {
            return Err(Fault::NotJson);
        }

        let raw: Box<RawValue> = serde_json::from_slice(bytes).map_err(|_| Fault::NotJson)?;

        object_of(&raw)?.ok_or(Fault::NotObject)
    }

    /// The count of members.
    pub(super) fn len(&self) -> usize {
        self.members.len()
    }

    /// Each member, in the order of the text.
    pub(super) fn members(&self) -> impl Iterator<Item = (&str, &RawValue)> {
        self.members
            .iter()
            .map(|(name, value)| (name.as_str(), value.as_ref()))
    }

    /// One member. `None` for a name that the object does not hold.
    pub(super) fn get(&self, name: &str) -> Option<&RawValue> {
        self.members()
            .find(|(member, _)| *member == name)
            .map(|(_, value)| value)
    }

    /// A member that is a string. `Ok(None)` for an absent member and for a
    /// member of another type.
    pub(super) fn text(&self, name: &str) -> Result<Option<String>, Fault> {
        self.get(name).map_or(Ok(None), text_of)
    }

    /// A member that is a string with one character or more.
    pub(super) fn filled_text(&self, name: &str) -> Result<Option<String>, Fault> {
        Ok(self.text(name)?.filter(|text| !text.is_empty()))
    }

    /// A member that is an integer. `None` for an absent member and for a
    /// member of another type, a boolean too.
    pub(super) fn int(&self, name: &str) -> Option<i128> {
        self.get(name).and_then(int_of)
    }

    /// A member that is an object.
    pub(super) fn object(&self, name: &str) -> Result<Option<Self>, Fault> {
        self.get(name).map_or(Ok(None), object_of)
    }

    /// The kind of a member. `None` for an absent member.
    pub(super) fn kind(&self, name: &str) -> Option<Kind> {
        self.get(name).map(kind_of)
    }
}

impl<'de> Deserialize<'de> for Object {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Members;

        impl<'de> Visitor<'de> for Members {
            type Value = Object;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a JSON object")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Object, A::Error> {
                let mut members: Vec<(String, Box<RawValue>)> = Vec::new();
                let mut places: HashMap<String, usize> = HashMap::new();
                while let Some((name, value)) = map.next_entry::<String, Box<RawValue>>()? {
                    let first = places.get(&name).and_then(|place| members.get_mut(*place));
                    if let Some(member) = first {
                        member.1 = value;
                        continue;
                    }

                    places.insert(name.clone(), members.len());
                    members.push((name, value));
                }

                Ok(Object { members })
            }
        }

        deserializer.deserialize_map(Members)
    }
}

/// Whether a text nests `DEPTH_MAX` levels or less and holds no integer of more
/// than `INT_DIGITS_MAX` digits.
///
/// The function reads bytes and does not check the JSON grammar. The reader
/// checks the grammar afterwards.
fn within_limits(bytes: &[u8]) -> bool {
    let mut depth = 0_usize;
    let mut in_text = false;
    let mut escaped = false;
    let mut digits = 0_usize;
    let mut fraction = false;
    for byte in bytes {
        if in_text {
            match byte {
                _ if escaped => escaped = false,
                b'\\' => escaped = true,
                b'"' => in_text = false,
                _ => {}
            }
            continue;
        }

        match byte {
            b'0'..=b'9' if !fraction => {
                digits += 1;
                continue;
            }
            b'0'..=b'9' | b'+' | b'-' => continue,
            b'.' | b'e' | b'E' if digits > 0 => {
                fraction = true;
                continue;
            }
            _ => {}
        }

        if !fraction && digits > INT_DIGITS_MAX {
            return false;
        }

        digits = 0;
        fraction = false;
        match byte {
            b'"' => in_text = true,
            b'[' | b'{' => {
                depth += 1;
                if depth > DEPTH_MAX {
                    return false;
                }
            }
            b']' | b'}' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }

    fraction || digits <= INT_DIGITS_MAX
}

/// Which characters the writer keeps as they are.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Charset {
    /// Each character outside printable ASCII is a `\u` escape. That is
    /// `json.dumps` with its defaults: a journal line and a state file.
    Ascii,
    /// Each character that JSON permits is itself. That is the answer of a
    /// route: `ensure_ascii=False`.
    Utf8,
}

/// Why a value has no JSON form.
///
/// A type of this module always has a JSON form. The error exists because a
/// writer of bytes can fail.
///
/// ```
/// use creche_contracts::session::EncodeError;
///
/// fn text(error: &EncodeError) -> String {
///     error.to_string()
/// }
/// ```
///
/// Code outside this module cannot build one, or read the error inside:
///
/// ```compile_fail,E0616
/// use creche_contracts::session::EncodeError;
///
/// fn inner(error: &EncodeError) -> String {
///     error.0.to_string()
/// }
/// ```
#[derive(Debug)]
pub struct EncodeError(serde_json::Error);

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the value has no JSON form: {}", self.0)
    }
}

impl Error for EncodeError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.0)
    }
}

/// The JSON text of a value, as Python writes it.
pub(super) fn encode<T: Serialize>(value: &T, charset: Charset) -> Result<Vec<u8>, EncodeError> {
    let mut bytes = Vec::new();
    let mut serializer = Serializer::with_formatter(&mut bytes, Python { charset });
    value.serialize(&mut serializer).map_err(EncodeError)?;

    Ok(bytes)
}

/// The JSON text of a value and one LF: one line of a journal or of a stream.
pub(super) fn encode_line<T: Serialize>(value: &T) -> Result<Vec<u8>, EncodeError> {
    let mut bytes = encode(value, Charset::Ascii)?;
    bytes.push(LF);

    Ok(bytes)
}

/// The last character of ASCII, DEL. Python writes it as an escape.
const DEL: char = '\u{7f}';

/// The format of `json.dumps` with the separators `,` and `:`.
struct Python {
    charset: Charset,
}

impl Formatter for Python {
    fn write_f64<W: ?Sized + io::Write>(&mut self, writer: &mut W, value: f64) -> io::Result<()> {
        writer.write_all(float_repr(value)?.as_bytes())
    }

    fn write_string_fragment<W: ?Sized + io::Write>(
        &mut self,
        writer: &mut W,
        fragment: &str,
    ) -> io::Result<()> {
        if self.charset == Charset::Utf8 {
            return writer.write_all(fragment.as_bytes());
        }

        for character in fragment.chars() {
            if character.is_ascii() && character != DEL {
                writer.write_all(character.encode_utf8(&mut [0; 4]).as_bytes())?;
                continue;
            }

            for unit in character.encode_utf16(&mut [0; 2]) {
                write!(writer, "\\u{unit:04x}")?;
            }
        }

        Ok(())
    }
}

/// The least exponent of ten that Python writes with no exponent.
const FIXED_EXPONENT_MIN: i64 = -4;

/// The least exponent of ten that Python writes with an exponent.
const FIXED_EXPONENT_END: i64 = 16;

/// The text of a finite float, as Python's `repr` writes it: the shortest
/// digits that read back as the same float, `1.0` for a whole number, and an
/// exponent with a sign and two digits or more.
///
/// The digits are the digits of `serde_json`. Like Python, it gives the
/// shortest digits and takes the even digit when two are equally near. The
/// `{:e}` format of the standard library takes the other digit there.
fn float_repr(value: f64) -> io::Result<String> {
    let mut shortest = Vec::new();
    CompactFormatter.write_f64(&mut shortest, value)?;
    let shortest = String::from_utf8(shortest).map_err(io::Error::other)?;
    let (sign, unsigned) = match shortest.strip_prefix('-') {
        Some(unsigned) => ("-", unsigned),
        None => ("", shortest.as_str()),
    };
    let (mantissa, exponent) = unsigned.split_once(['e', 'E']).unwrap_or((unsigned, "0"));
    let exponent: i64 = exponent.parse().map_err(io::Error::other)?;
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all = format!("{whole}{fraction}");
    let significant = all.trim_start_matches('0');
    let digits = significant.trim_end_matches('0');
    if digits.is_empty() {
        return Ok(format!("{sign}0.0"));
    }

    // The exponent of ten of the first digit.
    let width = |text: &str| i64::try_from(text.len()).map_err(io::Error::other);
    let exponent = exponent + width(whole)? - 1 - (width(&all)? - width(significant)?);
    if !(FIXED_EXPONENT_MIN..FIXED_EXPONENT_END).contains(&exponent) {
        let (first, rest) = digits.split_at_checked(1).unwrap_or((digits, ""));
        let point = if rest.is_empty() { "" } else { "." };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        let exponent = exponent.unsigned_abs();

        return Ok(format!(
            "{sign}{first}{point}{rest}e{exponent_sign}{exponent:02}"
        ));
    }

    let Ok(whole_digits) = usize::try_from(exponent + 1) else {
        let zeros = "0".repeat(usize::try_from(-exponent - 1).map_err(io::Error::other)?);

        return Ok(format!("{sign}0.{zeros}{digits}"));
    };
    if whole_digits == 0 {
        return Ok(format!("{sign}0.{digits}"));
    }

    Ok(match digits.split_at_checked(whole_digits) {
        Some((whole, "")) => format!("{sign}{whole}.0"),
        Some((whole, rest)) => format!("{sign}{whole}.{rest}"),
        None => {
            let zeros = "0".repeat(whole_digits.saturating_sub(digits.len()));

            format!("{sign}{digits}{zeros}.0")
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(text: &str) -> Box<RawValue> {
        serde_json::from_str(text).unwrap()
    }

    #[test]
    fn the_kind_of_a_value_is_read_from_its_text() {
        let kinds = [
            ("null", Kind::Null),
            ("true", Kind::True),
            ("false", Kind::False),
            ("0", Kind::Integer),
            ("-12", Kind::Integer),
            ("1.0", Kind::Float),
            ("1e2", Kind::Float),
            ("-1E-2", Kind::Float),
            ("\"1\"", Kind::Text),
            ("[1]", Kind::Array),
            ("{\"a\": 1}", Kind::Object),
        ];

        for (text, kind) in kinds {
            assert_eq!(kind_of(&raw(text)), kind, "{text}");
        }
    }

    #[test]
    fn an_integer_outside_i128_is_the_nearest_end() {
        let long = "9".repeat(60);

        assert_eq!(int_of(&raw("-0")), Some(0));
        assert_eq!(int_of(&raw("3600")), Some(3600));
        assert_eq!(int_of(&raw(&long)), Some(i128::MAX));
        assert_eq!(int_of(&raw(&format!("-{long}"))), Some(-i128::MAX));
        assert_eq!(int_of(&raw("true")), None);
        assert_eq!(int_of(&raw("1.0")), None);
        assert_eq!(int_of(&raw("\"1\"")), None);
    }

    #[test]
    fn the_last_value_of_a_key_wins_at_the_place_of_the_first() {
        let object = Object::read(br#"{"a": 1, "b": 2, "a": 3}"#).unwrap();
        let members: Vec<(&str, &str)> = object
            .members()
            .map(|(name, value)| (name, value.get()))
            .collect();

        assert_eq!(members, [("a", "3"), ("b", "2")]);
        assert_eq!(object.len(), 2);
    }

    #[test]
    fn a_member_of_another_type_is_absent() {
        let object =
            Object::read(br#"{"text": 5, "count": "5", "flag": 1, "map": [], "empty": ""}"#)
                .unwrap();

        assert_eq!(object.text("text"), Ok(None));
        assert_eq!(object.int("count"), None);
        assert_eq!(object.kind("flag"), Some(Kind::Integer));
        assert!(object.object("map").unwrap().is_none());
        assert_eq!(object.text("empty"), Ok(Some(String::new())));
        assert_eq!(object.filled_text("empty"), Ok(None));
        assert_eq!(object.kind("absent"), None);
    }

    #[test]
    fn a_text_that_is_no_object_is_refused() {
        let not_json: [&[u8]; 9] = [
            b"",
            b" ",
            b"{",
            b"{} x",
            b"{'a': 1}",
            b"{\"a\": NaN}",
            b"\xef\xbb\xbf{}",
            b"{\"a\": \"\xff\"}",
            b"{\"a\": 1,}",
        ];
        let not_object: [&[u8]; 5] = [b"[]", b"null", b"\"a\"", b"5", b"true"];

        for bytes in not_json {
            assert_eq!(
                Object::read(bytes).unwrap_err(),
                Fault::NotJson,
                "{bytes:?}"
            );
        }
        for bytes in not_object {
            assert_eq!(
                Object::read(bytes).unwrap_err(),
                Fault::NotObject,
                "{bytes:?}"
            );
        }
    }

    #[test]
    fn a_lone_surrogate_is_refused_only_where_a_parser_reads_it() {
        let object = Object::read(br#"{"known": "\ud800", "pair": "\ud83d\ude00"}"#).unwrap();

        assert_eq!(object.text("known"), Err(Fault::NotJson));
        assert_eq!(object.text("pair"), Ok(Some("\u{1f600}".to_owned())));
    }

    #[test]
    fn a_text_nests_128_levels_at_most() {
        let nested = |levels: usize| format!("{}{}", "[".repeat(levels), "]".repeat(levels));
        let in_text = format!("{{\"a\": \"{}\"}}", "[".repeat(500));

        assert!(within_limits(nested(DEPTH_MAX).as_bytes()));
        assert!(!within_limits(nested(DEPTH_MAX + 1).as_bytes()));
        assert!(within_limits(in_text.as_bytes()));
        assert!(within_limits(br#"["\"[[[", "\\"]"#));
    }

    #[test]
    fn each_member_of_a_text_within_the_limit_is_a_value_that_serde_json_reads() {
        let member = |levels: usize| format!("{}{}", "[".repeat(levels), "]".repeat(levels));
        let read = |levels: usize| serde_json::from_str::<serde_json::Value>(&member(levels));
        let deepest = format!("{{\"a\": {}}}", member(DEPTH_MAX - 1));
        let object = Object::read(deepest.as_bytes()).unwrap();

        assert!(read(DEPTH_MAX - 1).is_ok());
        assert!(read(DEPTH_MAX).is_err());
        assert!(serde_json::from_str::<serde_json::Value>(object.get("a").unwrap().get()).is_ok());
    }

    #[test]
    fn an_integer_has_4300_digits_at_most() {
        let digits = |count: usize| "9".repeat(count);

        assert!(within_limits(digits(INT_DIGITS_MAX).as_bytes()));
        assert!(!within_limits(digits(INT_DIGITS_MAX + 1).as_bytes()));
        assert!(within_limits(
            format!("-{}", digits(INT_DIGITS_MAX)).as_bytes()
        ));
        assert!(!within_limits(
            format!("[{}]", digits(INT_DIGITS_MAX + 1)).as_bytes()
        ));
        assert!(within_limits(format!("{}.5", digits(5000)).as_bytes()));
        assert!(within_limits(format!("1e{}", digits(5000)).as_bytes()));
        assert!(within_limits(format!("\"{}\"", digits(5000)).as_bytes()));
    }

    #[test]
    fn a_float_is_written_as_python_writes_it() {
        let floats = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.5, "-1.5"),
            (100.0, "100.0"),
            (0.014, "0.014"),
            (0.1 + 0.2, "0.30000000000000004"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (1_234_567_890_123_456.0, "1234567890123456.0"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1.234_567_890_123_456_8e16, "1.2345678901234568e+16"),
            (1e22, "1e+22"),
            (1e100, "1e+100"),
            (f64::MAX, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
            (123_456.789_012_345, "123456.789012345"),
            (1.0 / 3.0, "0.3333333333333333"),
            // The exact value ends in .25, or in .890625. The two shortest
            // texts are equally near, and Python takes the even digit.
            (5_261_963_047_596_905.0 / 4.0, "1315490761899226.2"),
            (16_842_045_457_081.0 / 64.0, "263156960266.89062"),
        ];

        for (value, text) in floats {
            assert_eq!(float_repr(value).unwrap(), text);
        }
    }

    #[test]
    fn a_text_is_written_with_the_escapes_of_python() {
        let text = "caf\u{e9} \"q\" \\ / \u{2028} \u{1f600} \u{7f}\u{0}\u{1f}\n\r\t\u{8}\u{c}";
        let ascii = encode(&text, Charset::Ascii).unwrap();
        let utf8 = encode(&text, Charset::Utf8).unwrap();

        assert_eq!(
            String::from_utf8(ascii).unwrap(),
            r#""caf\u00e9 \"q\" \\ / \u2028 \ud83d\ude00 \u007f\u0000\u001f\n\r\t\b\f""#
        );
        assert_eq!(
            String::from_utf8(utf8).unwrap(),
            "\"caf\u{e9} \\\"q\\\" \\\\ / \u{2028} \u{1f600} \u{7f}\\u0000\\u001f\\n\\r\\t\\b\\f\""
        );
    }

    #[test]
    fn a_line_ends_with_one_lf() {
        assert_eq!(encode_line(&[1, 2]).unwrap(), b"[1,2]\n");
    }
}
