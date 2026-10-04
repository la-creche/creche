//! A JSON reader that takes what Python `json.loads` takes, and the writers
//! that give what Python `json.dumps` gives.
//!
//! The release tool reads a request file and a live-state document with
//! Python `json.loads`. That reader is not strict JSON:
//!
//! - It takes `NaN`, `Infinity` and `-Infinity`.
//! - It takes a key more than one time. The last value stays, at the place of
//!   the first.
//! - It takes an integer of each size up to 4300 digits.
//! - It takes a lone surrogate escape in a string.
//!
//! This reader does the same, so that one text gives one refusal in both
//! languages. A float is read with the correct rounding, and a float is
//! written as Python `repr` writes it.
//!
//! The reader keeps what a caller here looks at. It keeps no item of an array.
//! It keeps the entries of an object only down to [`KEEP_LEVELS`] levels. No
//! caller looks deeper. The reader then needs no stack for a deep text.

use std::collections::BTreeMap;
use std::fmt::Write;

/// The deepest level of objects whose entries the reader keeps. The value of
/// a document is at level 1.
const KEEP_LEVELS: usize = 3;

/// The largest count of digits that Python reads as an integer.
const INT_DIGITS_MAX: usize = 4300;

// CONTRACT-QUESTION: `stage7-releases.md` §3.2 and contract 06 §11 do not say
// what a lone surrogate escape is. Python keeps it as one code point of the
// string. A Rust string cannot hold one, so this reader writes U+FFFD. Each
// caller refuses the string in both languages, because each field is ASCII.
// Two keys of one object that differ only in such a code point become one
// key here, so the detail of the refusal can differ. To keep the code point,
// a string must be a list of code units.
/// What a lone surrogate of a Python string is in a Rust string.
const REPLACEMENT: char = '\u{fffd}';

const BYTE_ORDER_MARK: char = '\u{feff}';

/// One value of a JSON text, as Python holds it.
#[derive(Debug)]
pub(super) enum Value {
    /// `None`.
    Null,
    /// `True` or `False`. No caller reads which one.
    Bool,
    /// An integer or a float, as the float that Python `float(value)` gives.
    /// An integer too large for a float is an infinity here. No caller takes
    /// an infinity.
    Number(f64),
    /// A string. A lone surrogate of the Python string is U+FFFD here.
    Text(String),
    /// A list. The reader keeps no item.
    Array,
    /// A dict.
    Object(Object),
}

/// One JSON object: each key at the place of its first use, with its last
/// value.
#[derive(Debug, Default)]
pub(super) struct Object {
    entries: Vec<(String, Value)>,
    places: BTreeMap<String, usize>,
}

impl Object {
    /// An object with no key.
    pub(super) const EMPTY: Self = Self {
        entries: Vec::new(),
        places: BTreeMap::new(),
    };

    fn insert(&mut self, key: String, value: Value) {
        if let Some(entry) = self
            .places
            .get(&key)
            .and_then(|place| self.entries.get_mut(*place))
        {
            entry.1 = value;
            return;
        }

        self.places.insert(key.clone(), self.entries.len());
        self.entries.push((key, value));
    }

    /// The value of one key.
    pub(super) fn get(&self, key: &str) -> Option<&Value> {
        let place = self.places.get(key)?;

        self.entries.get(*place).map(|(_, value)| value)
    }

    /// Each key and its value, in the order of the text.
    pub(super) fn iter(&self) -> impl Iterator<Item = (&str, &Value)> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_str(), value))
    }

    /// Each key, in the order of the code points.
    pub(super) fn sorted_keys(&self) -> impl Iterator<Item = &str> {
        self.places.keys().map(String::as_str)
    }

    /// The count of keys.
    pub(super) fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the object has no key.
    pub(super) fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// A text that Python `json.loads` does not take.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct NotJson;

/// Reads one JSON text, as Python `json.loads` does.
pub(super) fn parse(text: &str) -> Result<Value, NotJson> {
    if text.starts_with(BYTE_ORDER_MARK) {
        return Err(NotJson);
    }

    let mut cursor = Cursor { text, at: 0 };
    cursor.skip_space();
    let value = cursor.value(1)?;
    cursor.skip_space();
    if cursor.at != text.len() {
        return Err(NotJson);
    }

    Ok(value)
}

/// Which collection the skip step is inside.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Open {
    Array,
    Object,
}

/// Whether the reader keeps a string.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Keep {
    Yes,
    No,
}

struct Cursor<'a> {
    text: &'a str,
    /// The offset of the next byte. It is always at the start of a character.
    at: usize,
}

impl<'a> Cursor<'a> {
    fn peek(&self) -> Option<u8> {
        self.text.as_bytes().get(self.at).copied()
    }

    fn rest(&self) -> &'a str {
        self.text.get(self.at..).unwrap_or("")
    }

    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    /// Takes `word` if it is next.
    fn eat(&mut self, word: &str) -> bool {
        if !self.rest().starts_with(word) {
            return false;
        }

        self.at += word.len();

        true
    }

    fn expect(&mut self, byte: u8) -> Result<(), NotJson> {
        if self.peek() != Some(byte) {
            return Err(NotJson);
        }

        self.at += 1;

        Ok(())
    }

    fn value(&mut self, level: usize) -> Result<Value, NotJson> {
        match self.peek() {
            Some(b'{') if level <= KEEP_LEVELS => self.object(level),
            Some(b'{') => {
                self.skip_collection()?;
                Ok(Value::Object(Object::default()))
            }
            Some(b'[') => {
                self.skip_collection()?;
                Ok(Value::Array)
            }
            _ => self.scalar(Keep::Yes),
        }
    }

    /// Reads a value that is no object and no array.
    fn scalar(&mut self, keep: Keep) -> Result<Value, NotJson> {
        match self.peek() {
            Some(b'"') => self.string(keep).map(Value::Text),
            Some(b'n') if self.eat("null") => Ok(Value::Null),
            Some(b't') if self.eat("true") => Ok(Value::Bool),
            Some(b'f') if self.eat("false") => Ok(Value::Bool),
            Some(b'N') if self.eat("NaN") => Ok(Value::Number(f64::NAN)),
            Some(b'I') if self.eat("Infinity") => Ok(Value::Number(f64::INFINITY)),
            Some(b'-') if self.eat("-Infinity") => Ok(Value::Number(f64::NEG_INFINITY)),
            Some(b'-' | b'0'..=b'9') => self.number().map(Value::Number),
            _ => Err(NotJson),
        }
    }

    fn object(&mut self, level: usize) -> Result<Value, NotJson> {
        let mut object = Object::default();
        self.at += 1;
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(Value::Object(object));
        }

        loop {
            let key = self.key()?;
            let value = self.value(level + 1)?;
            object.insert(key, value);
            self.skip_space();
            match self.peek() {
                Some(b'}') => {
                    self.at += 1;
                    return Ok(Value::Object(object));
                }
                Some(b',') => {
                    self.at += 1;
                    self.skip_space();
                }
                _ => return Err(NotJson),
            }
        }
    }

    /// Reads a key, its `:` and the space on each side.
    fn key(&mut self) -> Result<String, NotJson> {
        if self.peek() != Some(b'"') {
            return Err(NotJson);
        }

        let key = self.string(Keep::Yes)?;
        self.skip_space();
        self.expect(b':')?;
        self.skip_space();

        Ok(key)
    }

    /// Reads one array or one object and keeps nothing. The step has no
    /// recursion, so the depth of the text costs no stack.
    fn skip_collection(&mut self) -> Result<(), NotJson> {
        let mut open: Vec<Open> = Vec::new();
        loop {
            // A value starts here.
            let opened = match self.peek() {
                Some(b'[') => Some((Open::Array, b']')),
                Some(b'{') => Some((Open::Object, b'}')),
                _ => None,
            };
            let mut value_done = true;
            match opened {
                Some((kind, close)) => {
                    self.at += 1;
                    self.skip_space();
                    if self.peek() == Some(close) {
                        self.at += 1;
                    } else {
                        open.push(kind);
                        if kind == Open::Object {
                            self.skip_key()?;
                        }

                        value_done = false;
                    }
                }
                None => {
                    self.scalar(Keep::No)?;
                }
            }

            // A value ended. Close each collection that ends here.
            while value_done {
                let Some(kind) = open.last().copied() else {
                    return Ok(());
                };
                self.skip_space();
                let close = match kind {
                    Open::Array => b']',
                    Open::Object => b'}',
                };
                match self.peek() {
                    Some(byte) if byte == close => {
                        self.at += 1;
                        open.pop();
                    }
                    Some(b',') => {
                        self.at += 1;
                        self.skip_space();
                        if kind == Open::Object {
                            self.skip_key()?;
                        }

                        value_done = false;
                    }
                    _ => return Err(NotJson),
                }
            }
        }
    }

    fn skip_key(&mut self) -> Result<(), NotJson> {
        if self.peek() != Some(b'"') {
            return Err(NotJson);
        }

        self.string(Keep::No)?;
        self.skip_space();
        self.expect(b':')?;
        self.skip_space();

        Ok(())
    }

    /// Reads a string. The cursor is at its first `"`.
    fn string(&mut self, keep: Keep) -> Result<String, NotJson> {
        let mut out = String::new();
        self.at += 1;
        loop {
            let start = self.at;
            while self
                .peek()
                .is_some_and(|byte| byte != b'"' && byte != b'\\' && byte >= 0x20)
            {
                self.at += 1;
            }

            if keep == Keep::Yes {
                out.push_str(self.text.get(start..self.at).unwrap_or(""));
            }

            match self.peek() {
                Some(b'"') => {
                    self.at += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.at += 1;
                    let character = self.escape()?;
                    if keep == Keep::Yes {
                        out.push(character);
                    }
                }
                // A control character, or the end of the text.
                _ => return Err(NotJson),
            }
        }
    }

    /// Reads what follows a `\`.
    fn escape(&mut self) -> Result<char, NotJson> {
        let character = match self.peek() {
            Some(b'"') => '"',
            Some(b'\\') => '\\',
            Some(b'/') => '/',
            Some(b'b') => '\u{8}',
            Some(b'f') => '\u{c}',
            Some(b'n') => '\n',
            Some(b'r') => '\r',
            Some(b't') => '\t',
            Some(b'u') => {
                self.at += 1;
                return self.unicode_escape();
            }
            _ => return Err(NotJson),
        };
        self.at += 1;

        Ok(character)
    }

    /// Reads four hex digits.
    fn hex4(&mut self) -> Result<u32, NotJson> {
        let digits = self.rest().get(..4).ok_or(NotJson)?;
        if !digits.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(NotJson);
        }

        self.at += 4;

        u32::from_str_radix(digits, 16).map_err(|_| NotJson)
    }

    /// Reads the digits of a `\u` escape, and the second escape of a
    /// surrogate pair.
    fn unicode_escape(&mut self) -> Result<char, NotJson> {
        let first = self.hex4()?;
        if !(0xd800..=0xdbff).contains(&first) {
            // A low surrogate with no high surrogate before it has no `char`.
            return Ok(char::from_u32(first).unwrap_or(REPLACEMENT));
        }

        let after_first = self.at;
        if self.eat("\\u")
            && let Ok(second) = self.hex4()
            && (0xdc00..=0xdfff).contains(&second)
        {
            let code = 0x1_0000 + ((first - 0xd800) << 10) + (second - 0xdc00);
            return Ok(char::from_u32(code).unwrap_or(REPLACEMENT));
        }

        self.at = after_first;

        Ok(REPLACEMENT)
    }

    fn digits(&mut self) -> usize {
        let start = self.at;
        while self.peek().is_some_and(|byte| byte.is_ascii_digit()) {
            self.at += 1;
        }

        self.at - start
    }

    /// Reads a number: `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`.
    fn number(&mut self) -> Result<f64, NotJson> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }

        let whole_digits = match self.peek() {
            Some(b'0') => {
                self.at += 1;
                1
            }
            Some(b'1'..=b'9') => self.digits(),
            _ => return Err(NotJson),
        };
        let mut whole = true;
        let before_fraction = self.at;
        if self.peek() == Some(b'.') {
            self.at += 1;
            if self.digits() == 0 {
                self.at = before_fraction;
            } else {
                whole = false;
            }
        }

        let before_exponent = self.at;
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.at += 1;
            if matches!(self.peek(), Some(b'-' | b'+')) {
                self.at += 1;
            }

            if self.digits() == 0 {
                self.at = before_exponent;
            } else {
                whole = false;
            }
        }

        // Python reads no longer text as an integer, so `json.loads` refuses
        // the whole text.
        if whole && whole_digits > INT_DIGITS_MAX {
            return Err(NotJson);
        }

        let token = self.text.get(start..self.at).ok_or(NotJson)?;
        let value: f64 = token.parse().map_err(|_| NotJson)?;
        // A Python integer has no negative zero.
        if whole && value == 0.0 {
            return Ok(0.0);
        }

        Ok(value)
    }
}

// --- the writers ---

/// Whether a writer keeps a character that is not ASCII, as the argument
/// `ensure_ascii` of `json.dumps` says.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Charset {
    /// `ensure_ascii=True`: each character outside ` ` to `~` is an escape.
    Ascii,
    /// `ensure_ascii=False`: only a control character is an escape.
    Unicode,
}

fn push_code_unit(out: &mut String, unit: u16) {
    // A write to a `String` cannot fail.
    let _ = write!(out, "\\u{unit:04x}");
}

/// Writes one string as `json.dumps` writes it, with its two `"`.
pub(super) fn push_string(out: &mut String, text: &str, charset: Charset) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            _ if charset == Charset::Unicode && character >= ' ' => out.push(character),
            _ => {
                let mut units = [0_u16; 2];
                for unit in character.encode_utf16(&mut units) {
                    push_code_unit(out, *unit);
                }
            }
        }
    }

    out.push('"');
}

/// The smallest decimal exponent that Python writes with no `e`, less one.
const FIXED_POINT_MIN: i32 = -4;

/// The largest count of digits before the point that Python writes with no
/// `e`.
const FIXED_POINT_MAX: i32 = 16;

/// A float as `json.dumps` writes it: `repr(value)` for a finite float, and
/// `NaN`, `Infinity` or `-Infinity` for each other one.
pub(super) fn float_text(value: f64) -> String {
    if value.is_nan() {
        return "NaN".to_owned();
    }

    if value.is_infinite() {
        let text = if value > 0.0 { "Infinity" } else { "-Infinity" };
        return text.to_owned();
    }

    // The shortest digits that give the same float, and the exponent of the
    // first digit.
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if mantissa.starts_with('-') { "-" } else { "" };
    // The place of the point, as a count of digits from the first digit.
    let point = exponent.saturating_add(1);
    if point <= FIXED_POINT_MIN || point > FIXED_POINT_MAX {
        let (first, rest) = digits.split_at_checked(1).unwrap_or((&digits, ""));
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        let size = exponent.unsigned_abs();

        return format!("{sign}{first}{fraction}e{exponent_sign}{size:02}");
    }

    match usize::try_from(point) {
        Ok(point) if point > 0 => match digits.split_at_checked(point) {
            Some((whole, fraction)) if !fraction.is_empty() => format!("{sign}{whole}.{fraction}"),
            _ => {
                let zeros = "0".repeat(point.saturating_sub(digits.len()));
                format!("{sign}{digits}{zeros}.0")
            }
        },
        _ => {
            let zeros = "0".repeat(usize::try_from(point.unsigned_abs()).unwrap_or(0));
            format!("{sign}0.{zeros}{digits}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text_of(value: &Value) -> String {
        match value {
            Value::Null => "null".to_owned(),
            Value::Bool => "bool".to_owned(),
            Value::Number(value) => float_text(*value),
            Value::Text(text) => format!("{text:?}"),
            Value::Array => "[]".to_owned(),
            Value::Object(object) => {
                let items: Vec<String> = object
                    .iter()
                    .map(|(key, value)| format!("{key}: {}", text_of(value)))
                    .collect();
                format!("{{{}}}", items.join(", "))
            }
        }
    }

    fn read(text: &str) -> String {
        parse(text).map_or_else(|_| "not JSON".to_owned(), |value| text_of(&value))
    }

    #[test]
    fn a_text_that_python_takes_is_read() {
        let table = [
            ("null", "null"),
            (" \t\r\n true \n", "bool"),
            ("-0", "0.0"),
            ("-0.0", "-0.0"),
            ("12", "12.0"),
            ("1.5e3", "1500.0"),
            ("1E+2", "100.0"),
            ("NaN", "NaN"),
            ("-Infinity", "-Infinity"),
            ("1e999", "Infinity"),
            (r#""a\u0062\n\/""#, r#""ab\n/""#),
            (r#""\ud83d\ude00""#, "\"\u{1f600}\""),
            (r#""\ud800""#, "\"\u{fffd}\""),
            (r#""\ud800\u0041""#, "\"\u{fffd}A\""),
            (r#""\udc00""#, "\"\u{fffd}\""),
            ("[1, [2, {\"a\": []}]]", "[]"),
            (
                r#"{"a": 1, "b": {"c": {"d": {"e": 1}}}}"#,
                "{a: 1.0, b: {c: {d: {}}}}",
            ),
            (r#"{"a": 1, "b": 2, "a": 3}"#, "{a: 3.0, b: 2.0}"),
            ("{}", "{}"),
        ];
        for (text, value) in table {
            assert_eq!(read(text), value, "{text:?}");
        }
    }

    #[test]
    fn a_text_that_python_refuses_is_refused() {
        let table = [
            "",
            " ",
            "\u{feff}{}",
            "nul",
            "True",
            "01",
            "+1",
            "1.",
            ".5",
            "1e",
            "0x10",
            "-",
            "-Inf",
            "{} x",
            "{}{}",
            "{'a': 1}",
            "{\"a\": 1,}",
            "{\"a\" 1}",
            "{1: 1}",
            "[1,]",
            "[1 2]",
            "[",
            "[[[",
            "{\"a\": [1,}",
            "\"a",
            "\"a\tb\"",
            "\"\\q\"",
            "\"\\u00\"",
            "\"\\u00zz\"",
            "\"\\ud800\\uzzzz\"",
            "{} \u{0}",
        ];
        for text in table {
            assert_eq!(parse(text).unwrap_err(), NotJson, "{text:?}");
        }
    }

    #[test]
    fn an_integer_has_4300_digits_at_most() {
        let at_limit = "1".repeat(4300);

        assert!(parse(&at_limit).is_ok());
        assert!(parse(&format!("-{at_limit}")).is_ok());
        assert!(parse(&format!("{at_limit}1")).is_err());
        assert!(parse(&format!("{at_limit}1.0")).is_ok());
    }

    #[test]
    fn a_deep_text_needs_no_stack() {
        let depth = 1_000_000;
        let arrays = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        let objects = format!("{}1{}", "{\"a\":".repeat(depth), "}".repeat(depth));

        assert_eq!(read(&arrays), "[]");
        assert_eq!(read(&objects), "{a: {a: {a: {}}}}");
        assert_eq!(read(&"[".repeat(depth)), "not JSON");
    }

    #[test]
    fn a_string_is_written_as_python_writes_it() {
        let text = "a\"\\\n\r\t\u{8}\u{c}\u{1}\u{7f}\u{e9}\u{2028}\u{1f600}";
        let mut ascii = String::new();
        let mut unicode = String::new();
        push_string(&mut ascii, text, Charset::Ascii);
        push_string(&mut unicode, text, Charset::Unicode);

        assert_eq!(
            ascii,
            r#""a\"\\\n\r\t\b\f\u0001\u007f\u00e9\u2028\ud83d\ude00""#
        );
        assert_eq!(
            unicode,
            "\"a\\\"\\\\\\n\\r\\t\\b\\f\\u0001\u{7f}\u{e9}\u{2028}\u{1f600}\""
        );
    }

    #[test]
    fn a_float_is_written_as_python_repr_writes_it() {
        let table = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.5, "-1.5"),
            (0.1, "0.1"),
            (1_758_153_600.0, "1758153600.0"),
            (1_758_153_600.123_456, "1758153600.123456"),
            (0.000_1, "0.0001"),
            (0.000_099_99, "9.999e-05"),
            (0.000_01, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1.234_567_890_123_456_8e16, "1.2345678901234568e+16"),
            (1e22, "1e+22"),
            (1.5e300, "1.5e+300"),
            (f64::MAX, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
            (f64::NAN, "NaN"),
            (f64::INFINITY, "Infinity"),
            (f64::NEG_INFINITY, "-Infinity"),
        ];
        for (value, text) in table {
            assert_eq!(float_text(value), text);
        }
    }
}
