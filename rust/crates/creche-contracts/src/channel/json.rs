//! The JSON of one line: the value, the reader and the writer.
//!
//! The reader accepts what `json.loads` of Python accepts, because the Python
//! host reads each line with it. `serde_json` cannot stand in for it:
//!
//! - `json.loads` reads `NaN`, `Infinity` and `-Infinity`.
//! - It reads an integer of 4300 digits and a float of any length.
//! - It reads `\ud800`, a surrogate with no partner.
//! - It reads a line that nests 200 levels. `serde_json` stops at 128.
//!
//! The reader uses no recursion, so the depth of a line cannot exhaust the
//! stack. It stops at [`MAX_LINE_DEPTH`] levels. The writer makes the bytes
//! that `json.dumps(value, separators=(",", ":"), ensure_ascii=False)` makes.

use std::collections::HashMap;
use std::fmt;
use std::mem;

use super::number::{Integer, IntegerError};
use super::text::{Text, TextBuilder};

/// The deepest nesting of objects and arrays that the reader accepts in one
/// line.
// CONTRACT-QUESTION: contract 03 §13 rule 1 demands valid JSON and names no
// nesting limit. The Python host refuses a line when `json.loads` raises
// `RecursionError`. That depth changes with the interpreter: 9997 levels on
// Python 3.12, 9998 on 3.13 and about 116,000 on 3.14, as measured. The
// reader stops below the lowest of them, so it refuses each line that one
// supported interpreter refuses. A lower limit costs a turn: a refused event
// leaves a gap in `turn_seq`, and rule 4 fails the turn on a gap.
pub const MAX_LINE_DEPTH: usize = 9000;

/// The count of fields from which an object under construction keeps an
/// index of its keys.
const INDEX_FROM: usize = 8;

/// One JSON value of a line from the playpen.
///
/// Contract 03 §5.1 passes a pi event through unchanged, so the content of
/// an event is opaque. This type holds it. It holds what Python holds: an
/// integer of any size, a float that is not finite and a text with a lone
/// surrogate.
///
/// `Clone`, `PartialEq`, `Debug` and `Drop` use no recursion. The reader
/// builds a value that nests [`MAX_LINE_DEPTH`] levels, and an impl that the
/// compiler derives uses one stack frame for each level. A stack that
/// overflows stops the process.
pub enum Json {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// A number with no fraction and no exponent.
    Int(Integer),
    /// A number with a fraction or an exponent, or `NaN`, `Infinity` or
    /// `-Infinity`.
    Float(f64),
    /// A string.
    Text(Text),
    /// An array.
    Array(JsonArray),
    /// An object.
    Object(JsonObject),
}

impl Json {
    /// The text of a string value.
    #[must_use]
    pub fn as_text(&self) -> Option<&Text> {
        match self {
            Self::Text(text) => Some(text),
            _ => None,
        }
    }

    /// The integer of a number with no fraction and no exponent.
    #[must_use]
    pub fn as_integer(&self) -> Option<&Integer> {
        match self {
            Self::Int(integer) => Some(integer),
            _ => None,
        }
    }

    /// The fields of an object value.
    #[must_use]
    pub fn as_object(&self) -> Option<&JsonObject> {
        match self {
            Self::Object(object) => Some(object),
            _ => None,
        }
    }

    /// The items of an array value.
    #[must_use]
    pub fn as_array(&self) -> Option<&JsonArray> {
        match self {
            Self::Array(array) => Some(array),
            _ => None,
        }
    }

    /// Whether the value is `true`. Each other value is not `true`, also 1.
    #[must_use]
    pub fn is_true(&self) -> bool {
        matches!(self, Self::Bool(true))
    }

    /// The fields of an object value, taken out of the value.
    pub(super) fn into_object(mut self) -> Option<JsonObject> {
        match &mut self {
            Self::Object(object) => Some(mem::take(object)),
            _ => None,
        }
    }

    /// The values directly inside this value. The value keeps none of them.
    fn take_children(&mut self) -> Vec<Self> {
        match self {
            Self::Array(array) => mem::take(&mut array.0),
            Self::Object(object) => mem::take(&mut object.0)
                .into_iter()
                .map(|(_, value)| value)
                .collect(),
            _ => Vec::new(),
        }
    }
}

/// Drops a value with no recursion. The reader builds a value that nests
/// [`MAX_LINE_DEPTH`] levels, and the drop that the compiler writes would
/// use one stack frame for each level.
impl Drop for Json {
    fn drop(&mut self) {
        let mut pending = self.take_children();
        while let Some(mut value) = pending.pop() {
            pending.append(&mut value.take_children());
        }
    }
}

/// Copies a value with no recursion.
impl Clone for Json {
    fn clone(&self) -> Self {
        let mut copy = Self::Null;
        let mut pending = vec![(self, &mut copy)];
        while let Some((source, target)) = pending.pop() {
            // A container gets a place for each value inside it first.
            *target = match source {
                Self::Null => Self::Null,
                Self::Bool(flag) => Self::Bool(*flag),
                Self::Int(integer) => Self::Int(integer.clone()),
                Self::Float(float) => Self::Float(*float),
                Self::Text(text) => Self::Text(text.clone()),
                Self::Array(array) => {
                    Self::Array(JsonArray(array.0.iter().map(|_| Self::Null).collect()))
                }
                Self::Object(object) => {
                    let places = object.0.iter().map(|(key, _)| (key.clone(), Self::Null));

                    Self::Object(JsonObject(places.collect()))
                }
            };

            match (source, target) {
                (Self::Array(from), Self::Array(to)) => {
                    pending.extend(from.0.iter().zip(to.0.iter_mut()));
                }
                (Self::Object(from), Self::Object(to)) => {
                    let pairs = from.0.iter().zip(to.0.iter_mut());

                    pending.extend(pairs.map(|((_, from), (_, to))| (from, to)));
                }
                _ => {}
            }
        }

        copy
    }
}

/// Compares two values with no recursion. Two floats are equal as two `f64`
/// are, so `NaN` is not equal to `NaN`. The fields of two equal objects have
/// the same order.
impl PartialEq for Json {
    fn eq(&self, other: &Self) -> bool {
        let mut pending = vec![(self, other)];
        while let Some(pair) = pending.pop() {
            let same = match pair {
                (Self::Null, Self::Null) => true,
                (Self::Bool(left), Self::Bool(right)) => left == right,
                (Self::Int(left), Self::Int(right)) => left == right,
                (Self::Float(left), Self::Float(right)) => left == right,
                (Self::Text(left), Self::Text(right)) => left == right,
                (Self::Array(left), Self::Array(right)) => {
                    pending.extend(left.0.iter().zip(&right.0));

                    left.0.len() == right.0.len()
                }
                (Self::Object(left), Self::Object(right)) => {
                    let pairs = || left.0.iter().zip(&right.0);
                    pending.extend(pairs().map(|((_, left), (_, right))| (left, right)));

                    left.0.len() == right.0.len()
                        && pairs().all(|((left, _), (right, _))| left == right)
                }
                _ => false,
            };

            if !same {
                return false;
            }
        }

        true
    }
}

/// Prints a value with no recursion, in the compact form of the writer. A
/// text has the form of [`Text`], so a lone surrogate prints too.
impl fmt::Debug for Json {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        walk(vec![Visit::Value(self)], |piece| match piece {
            Piece::Raw(raw) => f.write_str(raw),
            Piece::Key(text) | Piece::Scalar(Json::Text(text)) => write!(f, "{text:?}"),
            Piece::Scalar(scalar) => f.write_str(&scalar_text(scalar)),
        })
    }
}

/// The items of one JSON array, in the order of the line.
///
/// ```
/// use creche_contracts::channel::json::JsonArray;
///
/// assert!(JsonArray::default().is_empty());
/// ```
///
/// Code outside this module cannot build a value from raw items:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::json::JsonArray;
///
/// let value = JsonArray(Vec::new());
/// ```
#[derive(Debug, Clone, PartialEq, Default)]
pub struct JsonArray(Vec<Json>);

impl JsonArray {
    /// The items, in the order of the line.
    pub fn iter(&self) -> impl Iterator<Item = &Json> {
        self.0.iter()
    }

    /// The count of items.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// Whether the array has no item.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// The fields of one JSON object. Each key is there one time.
///
/// For a key that a line holds more than one time, the object keeps the last
/// value at the place of the first, as a Python `dict` does.
///
/// ```
/// use creche_contracts::channel::json::JsonObject;
///
/// assert!(JsonObject::default().is_empty());
/// ```
///
/// Code outside this module cannot build a value from raw items:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::json::JsonObject;
///
/// let value = JsonObject(Vec::new());
/// ```
#[derive(Debug, Clone, PartialEq, Default)]
pub struct JsonObject(Vec<(Text, Json)>);

impl JsonObject {
    /// The value of the field `key`.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.0
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    }

    /// The fields, in the order of the line.
    pub fn iter(&self) -> impl Iterator<Item = (&Text, &Json)> {
        self.0.iter().map(|(key, value)| (key, value))
    }

    /// The count of fields.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// Whether the object has no field.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The object of these fields. Each key is there one time.
    pub(super) fn of(fields: Vec<(Text, Json)>) -> Self {
        Self(fields)
    }

    /// Takes the value of the field `key` out. The field keeps `null`.
    pub(super) fn take(&mut self, key: &str) -> Option<Json> {
        self.0
            .iter_mut()
            .find(|(name, _)| name == key)
            .map(|(_, value)| mem::replace(value, Json::Null))
    }
}

/// Which tokens a reader accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Dialect {
    /// What `json.loads` of Python reads: also `NaN`, `Infinity` and
    /// `-Infinity`.
    Python,
    /// What `JSON.parse` of JavaScript reads: no such token.
    Strict,
}

/// Why the reader gave no value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ReadError {
    /// The text is not JSON. `json.loads` raises `JSONDecodeError`.
    NotJson,
    /// The text nests deeper than [`MAX_LINE_DEPTH`]. `json.loads` raises
    /// `RecursionError` at a depth that changes with the interpreter.
    TooDeep,
    /// An integer has more than 4300 digits. `json.loads` raises `ValueError`.
    IntegerTooLong,
}

/// The white space of JSON.
const fn is_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

/// The byte order mark. `json.loads` refuses a text that starts with it.
const BOM: char = '\u{feff}';

/// The count of hex digits in one `\u` escape.
const ESCAPE_DIGITS: usize = 4;

/// The count of bytes in one `\u` escape: `\`, `u` and four hex digits.
const ESCAPE_BYTES: usize = 6;

/// An object or an array that the reader has not closed yet.
enum Open {
    Array(Vec<Json>),
    Object(Fields, Text),
}

/// The fields of an object under construction, and the key of each field
/// from [`INDEX_FROM`] fields.
struct Fields {
    fields: Vec<(Text, Json)>,
    index: HashMap<Text, usize>,
}

impl Fields {
    fn new() -> Self {
        Self {
            fields: Vec::new(),
            index: HashMap::new(),
        }
    }

    /// Where the field `key` is.
    fn place(&self, key: &Text) -> Option<usize> {
        if self.fields.len() < INDEX_FROM {
            return self.fields.iter().position(|(name, _)| name == key);
        }

        self.index.get(key).copied()
    }

    /// Sets the field `key`. A key that is there keeps its place.
    fn set(&mut self, key: Text, value: Json) {
        if let Some(slot) = self.place(&key).and_then(|at| self.fields.get_mut(at)) {
            slot.1 = value;
            return;
        }

        self.fields.push((key, value));
        if self.fields.len() == INDEX_FROM {
            self.index = self
                .fields
                .iter()
                .enumerate()
                .map(|(at, (name, _))| (name.clone(), at))
                .collect();
        } else if self.fields.len() > INDEX_FROM
            && let Some((name, _)) = self.fields.last()
        {
            self.index.insert(name.clone(), self.fields.len() - 1);
        }
    }
}

/// Reads one JSON text, as `json.loads` does for [`Dialect::Python`].
pub(super) fn read(text: &str, dialect: Dialect) -> Result<Json, ReadError> {
    if text.starts_with(BOM) {
        return Err(ReadError::NotJson);
    }

    let mut reader = Reader {
        text,
        bytes: text.as_bytes(),
        at: 0,
        dialect,
    };
    reader.skip_space();
    let value = reader.document()?;
    reader.skip_space();
    if reader.at != reader.bytes.len() {
        return Err(ReadError::NotJson);
    }

    Ok(value)
}

struct Reader<'a> {
    text: &'a str,
    bytes: &'a [u8],
    at: usize,
    dialect: Dialect,
}

/// What the reader does after a step.
enum Step {
    /// A value is complete.
    Value(Json),
    /// A container is open, and the reader is at its first value.
    Descend,
}

impl Reader<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn skip_space(&mut self) {
        while self.peek().is_some_and(is_space) {
            self.at += 1;
        }
    }

    /// Whether the text continues with `word` at the reader.
    fn at_word(&self, word: &str) -> bool {
        self.bytes
            .get(self.at..)
            .is_some_and(|rest| rest.starts_with(word.as_bytes()))
    }

    /// Reads one value and each value inside it.
    fn document(&mut self) -> Result<Json, ReadError> {
        let mut open: Vec<Open> = Vec::new();
        loop {
            let mut value = match self.start(&mut open)? {
                Step::Value(value) => value,
                Step::Descend => continue,
            };

            // The value is complete. It goes into the container that holds it,
            // and a container that closes is the next complete value.
            loop {
                let Some(holder) = open.pop() else {
                    return Ok(value);
                };

                match self.put(holder, value)? {
                    Ok(next) => {
                        open.push(next);
                        break;
                    }
                    Err(closed) => value = closed,
                }
            }
        }
    }

    /// Reads the start of one value: a whole scalar, a whole empty container,
    /// or the opening of a container with content.
    fn start(&mut self, open: &mut Vec<Open>) -> Result<Step, ReadError> {
        let Some(byte) = self.peek() else {
            return Err(ReadError::NotJson);
        };

        match byte {
            b'"' => {
                self.at += 1;

                Ok(Step::Value(Json::Text(self.string()?)))
            }
            b'{' | b'[' => {
                // `json.loads` counts a level for an empty container too.
                if open.len() >= MAX_LINE_DEPTH {
                    return Err(ReadError::TooDeep);
                }

                self.at += 1;
                self.skip_space();
                if byte == b'[' {
                    return Ok(self.open_array(open));
                }

                self.open_object(open)
            }
            _ => self.scalar().map(Step::Value),
        }
    }

    fn open_array(&mut self, open: &mut Vec<Open>) -> Step {
        if self.peek() == Some(b']') {
            self.at += 1;
            return Step::Value(Json::Array(JsonArray::default()));
        }

        open.push(Open::Array(Vec::new()));

        Step::Descend
    }

    fn open_object(&mut self, open: &mut Vec<Open>) -> Result<Step, ReadError> {
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(Step::Value(Json::Object(JsonObject::default())));
        }

        let key = self.key()?;
        open.push(Open::Object(Fields::new(), key));

        Ok(Step::Descend)
    }

    /// Reads one key, the `:` after it and the white space around them.
    fn key(&mut self) -> Result<Text, ReadError> {
        if self.peek() != Some(b'"') {
            return Err(ReadError::NotJson);
        }

        self.at += 1;
        let key = self.string()?;
        self.skip_space();
        if self.peek() != Some(b':') {
            return Err(ReadError::NotJson);
        }

        self.at += 1;
        self.skip_space();

        Ok(key)
    }

    /// Puts a complete value into its container. The result is the container,
    /// with the reader at its next value, or the closed container as a value.
    fn put(&mut self, holder: Open, value: Json) -> Result<Result<Open, Json>, ReadError> {
        match holder {
            Open::Array(mut items) => {
                items.push(value);
                self.skip_space();
                match self.peek() {
                    Some(b']') => {
                        self.at += 1;

                        Ok(Err(Json::Array(JsonArray(items))))
                    }
                    Some(b',') => {
                        self.at += 1;
                        self.skip_space();

                        Ok(Ok(Open::Array(items)))
                    }
                    _ => Err(ReadError::NotJson),
                }
            }
            Open::Object(mut fields, key) => {
                fields.set(key, value);
                self.skip_space();
                match self.peek() {
                    Some(b'}') => {
                        self.at += 1;

                        Ok(Err(Json::Object(JsonObject(fields.fields))))
                    }
                    Some(b',') => {
                        self.at += 1;
                        self.skip_space();
                        let key = self.key()?;

                        Ok(Ok(Open::Object(fields, key)))
                    }
                    _ => Err(ReadError::NotJson),
                }
            }
        }
    }

    /// Reads a value that is no string and no container.
    fn scalar(&mut self) -> Result<Json, ReadError> {
        let python = self.dialect == Dialect::Python;
        let words = [
            ("null", Json::Null, true),
            ("true", Json::Bool(true), true),
            ("false", Json::Bool(false), true),
            ("NaN", Json::Float(f64::NAN), python),
            ("Infinity", Json::Float(f64::INFINITY), python),
            ("-Infinity", Json::Float(f64::NEG_INFINITY), python),
        ];
        for (word, value, known) in words {
            if known && self.at_word(word) {
                self.at += word.len();
                return Ok(value);
            }
        }

        self.number()
    }

    /// Reads one number, as the scanner of `json.loads` does: the longest
    /// text that is a number, with no look at what follows it.
    fn number(&mut self) -> Result<Json, ReadError> {
        let start = self.at;
        let mut at = self.at;
        let digit = |at: usize| self.bytes.get(at).is_some_and(u8::is_ascii_digit);
        if self.bytes.get(at) == Some(&b'-') {
            at += 1;
        }

        match self.bytes.get(at) {
            Some(b'0') => at += 1,
            Some(b'1'..=b'9') => {
                while digit(at) {
                    at += 1;
                }
            }
            _ => return Err(ReadError::NotJson),
        }

        let mut float = false;
        if self.bytes.get(at) == Some(&b'.') && digit(at + 1) {
            float = true;
            at += 1;
            while digit(at) {
                at += 1;
            }
        }

        if matches!(self.bytes.get(at), Some(b'e' | b'E')) {
            let mut end = at + 1;
            if matches!(self.bytes.get(end), Some(b'+' | b'-')) {
                end += 1;
            }

            // An exponent with no digit is not part of the number.
            if digit(end) {
                float = true;
                at = end;
                while digit(at) {
                    at += 1;
                }
            }
        }

        let token = self.text.get(start..at).ok_or(ReadError::NotJson)?;
        self.at = at;
        if float {
            return token
                .parse()
                .map(Json::Float)
                .map_err(|_| ReadError::NotJson);
        }

        match token.parse() {
            Ok(integer) => Ok(Json::Int(integer)),
            Err(IntegerError::TooLong) => Err(ReadError::IntegerTooLong),
            Err(IntegerError::NotDigits | IntegerError::ZeroAtStart) => Err(ReadError::NotJson),
        }
    }

    /// Reads one string. The reader is after the opening quote.
    fn string(&mut self) -> Result<Text, ReadError> {
        let mut built = TextBuilder::new();
        loop {
            let start = self.at;
            let end = loop {
                match self.peek() {
                    Some(b'"' | b'\\') => break self.at,
                    // A control character must be an escape.
                    None | Some(0..=0x1f) => return Err(ReadError::NotJson),
                    Some(_) => self.at += 1,
                }
            };

            built.push_str(self.text.get(start..end).ok_or(ReadError::NotJson)?);
            self.at += 1;
            if self.bytes.get(end) == Some(&b'"') {
                return Ok(built.finish());
            }

            self.escape(&mut built)?;
        }
    }

    /// Reads one escape. The reader is after the `\`.
    fn escape(&mut self, built: &mut TextBuilder) -> Result<(), ReadError> {
        let character = match self.peek() {
            Some(b'u') => {
                self.at += 1;
                return self.unicode_escape(built);
            }
            Some(b'"') => '"',
            Some(b'\\') => '\\',
            Some(b'/') => '/',
            Some(b'b') => '\u{8}',
            Some(b'f') => '\u{c}',
            Some(b'n') => '\n',
            Some(b'r') => '\r',
            Some(b't') => '\t',
            _ => return Err(ReadError::NotJson),
        };

        self.at += 1;
        built.push(character);

        Ok(())
    }

    /// Reads the hex digits of one `\u` escape. The reader is after the `u`.
    fn unicode_escape(&mut self, built: &mut TextBuilder) -> Result<(), ReadError> {
        let unit = self.hex(self.at)?;
        self.at += ESCAPE_DIGITS;
        let high = (0xd800..=0xdbff).contains(&unit);

        // `json.loads` joins a high surrogate with a low one that follows it
        // as an escape, when the string goes on after that escape.
        if high && self.at_word("\\u") && self.at + ESCAPE_BYTES < self.bytes.len() {
            let low = self.hex(self.at + 2)?;
            if (0xdc00..=0xdfff).contains(&low) {
                self.at += ESCAPE_BYTES;
                let joined = char::decode_utf16([unit, low]).next().and_then(Result::ok);
                built.push(joined.ok_or(ReadError::NotJson)?);

                return Ok(());
            }
        }

        built.push_unit(unit);

        Ok(())
    }

    /// The value of the four hex digits at `at`.
    fn hex(&self, at: usize) -> Result<u16, ReadError> {
        let digits = self
            .bytes
            .get(at..at + ESCAPE_DIGITS)
            .ok_or(ReadError::NotJson)?;
        let mut unit = 0_u16;
        for byte in digits {
            let digit = char::from(*byte).to_digit(16).ok_or(ReadError::NotJson)?;
            let digit = u16::try_from(digit).map_err(|_| ReadError::NotJson)?;

            unit = (unit << 4) | digit;
        }

        Ok(unit)
    }
}

/// A text with a lone surrogate has no UTF-8 form. Python raises
/// `UnicodeEncodeError` when it encodes one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct LoneSurrogate;

/// The count of bytes of one `\u00XX` escape.
const CONTROL_ESCAPE_BYTES: usize = 6;

/// How a string of the output writes `character`: an escape, or `None` for
/// the character itself.
fn escape_of(character: char) -> Option<&'static str> {
    match character {
        '"' => Some("\\\""),
        '\\' => Some("\\\\"),
        '\n' => Some("\\n"),
        '\r' => Some("\\r"),
        '\t' => Some("\\t"),
        '\u{8}' => Some("\\b"),
        '\u{c}' => Some("\\f"),
        _ => None,
    }
}

/// Writes `text` as one JSON string: the escapes of `json.dumps` with
/// `ensure_ascii=False`, which are also the escapes of `JSON.stringify`.
pub(super) fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        if let Some(escape) = escape_of(character) {
            out.push_str(escape);
        } else if character < ' ' {
            out.push_str(&format!("\\u{:04x}", u32::from(character)));
        } else {
            out.push(character);
        }
    }

    out.push('"');
}

/// Writes one JSON object, one field after the other, in compact form.
pub(super) struct ObjectWriter(String);

impl ObjectWriter {
    pub(super) fn new() -> Self {
        Self("{".to_owned())
    }

    fn key(&mut self, key: &str) {
        if self.0.len() > 1 {
            self.0.push(',');
        }

        write_string(&mut self.0, key);
        self.0.push(':');
    }

    /// A field whose value is a string.
    pub(super) fn text(mut self, key: &str, value: &str) -> Self {
        self.key(key);
        write_string(&mut self.0, value);

        self
    }

    /// A field whose value is `value` as it is: a number, `true`, `false`,
    /// `null`, or the text of an object or an array.
    pub(super) fn raw(mut self, key: &str, value: &str) -> Self {
        self.key(key);
        self.0.push_str(value);

        self
    }

    /// A field whose value is a string, or `null` for no string.
    pub(super) fn text_or_null(self, key: &str, value: Option<&str>) -> Self {
        match value {
            Some(value) => self.text(key, value),
            None => self.raw(key, NULL),
        }
    }

    /// A field whose value is a number, or `null` for no number.
    pub(super) fn raw_or_null(self, key: &str, value: Option<String>) -> Self {
        self.raw(key, value.as_deref().unwrap_or(NULL))
    }

    /// A field whose value is a string, and no field for no string.
    pub(super) fn text_if(self, key: &str, value: Option<&str>) -> Self {
        match value {
            Some(value) => self.text(key, value),
            None => self,
        }
    }

    /// A field whose value is `value` as it is, and no field for no value.
    pub(super) fn raw_if(self, key: &str, value: Option<String>) -> Self {
        match value {
            Some(value) => self.raw(key, &value),
            None => self,
        }
    }

    pub(super) fn finish(mut self) -> String {
        self.0.push('}');

        self.0
    }
}

/// The text of `null`.
const NULL: &str = "null";

/// The text of a flag.
pub(super) const fn flag_text(flag: bool) -> &'static str {
    if flag { "true" } else { "false" }
}

/// The text of one array of strings.
pub(super) fn array_text<'a>(items: impl Iterator<Item = &'a str>) -> String {
    let mut out = "[".to_owned();
    for (at, item) in items.enumerate() {
        if at > 0 {
            out.push(',');
        }

        write_string(&mut out, item);
    }

    out.push(']');

    out
}

/// The count of bytes that [`write_string`] writes for `text`.
fn string_size(text: &Text) -> Result<usize, LoneSurrogate> {
    let text = text.as_str().ok_or(LoneSurrogate)?;
    let size = |character: char| match escape_of(character) {
        Some(escape) => escape.len(),
        None if character < ' ' => CONTROL_ESCAPE_BYTES,
        None => character.len_utf8(),
    };

    Ok(text.chars().map(size).sum::<usize>() + 2)
}

/// The smallest decimal exponent for which Python writes a float with no
/// exponent.
const FIXED_FROM: i32 = -4;

/// The decimal exponent from which Python writes a float with an exponent.
const FIXED_BELOW: i32 = 16;

/// The count of bits in the fraction of an `f64`.
const FRACTION_BITS: u32 = 52;

/// The largest exponent field of an `f64`. It marks a float that is not
/// finite.
const EXPONENT_FIELD_MAX: u64 = 0x7ff;

/// What the exponent field of an `f64` is above the power of two of its
/// last bit.
const EXPONENT_BIAS: i32 = 1075;

/// The most decimal places for which a float can be at the same distance
/// from two shortest texts. With more places the float has more than 17
/// digits, and a shortest text has 17 digits at most.
const TIE_PLACES_MAX: u32 = 27;

/// The digits of `value` when it is at the same distance from two shortest
/// texts and the lower one has an even last digit. `None` in each other case.
///
/// `repr` of Python rounds such a tie to the even digit: `0.25` past
/// `669758432410385` is `...385.2`. The formatter of Rust rounds it up.
fn even_tie(value: f64, digits: &str) -> Option<(String, i32)> {
    let bits = value.abs().to_bits();
    let field = bits >> FRACTION_BITS;
    if field == 0 || field == EXPONENT_FIELD_MAX {
        return None;
    }

    // `value` is `mantissa * 2^power`. A tie needs binary places.
    let mantissa = (bits & ((1 << FRACTION_BITS) - 1)) | (1 << FRACTION_BITS);
    let power = i32::try_from(field).ok()? - EXPONENT_BIAS;
    let places = u32::try_from(-power)
        .ok()?
        .checked_sub(mantissa.trailing_zeros())?;
    if !(2..=TIE_PLACES_MAX).contains(&places) {
        return None;
    }

    // Each digit of `value`. The last one is 5, so the text with one digit
    // less is a tie.
    let odd = u128::from(mantissa >> mantissa.trailing_zeros());
    let exact = (odd * 5_u128.pow(places)).to_string();
    let lower = exact.strip_suffix('5')?;
    if lower.len() != digits.len() {
        return None;
    }

    let last = lower.chars().next_back()?.to_digit(10)?;
    let exponent = i32::try_from(exact.len()).ok()? - 1 - i32::try_from(places).ok()?;
    let scale = exponent + 1 - i32::try_from(lower.len()).ok()?;
    let back: f64 = format!("{lower}e{scale}").parse().ok()?;

    (last % 2 == 0 && back.to_bits() == bits).then(|| (lower.to_owned(), exponent))
}

/// The text that `json.dumps` of Python writes for one float: `repr`, and the
/// three names for a float that is not finite.
pub(super) fn float_text(value: f64) -> String {
    if value.is_nan() {
        return "NaN".to_owned();
    }

    if value.is_infinite() {
        let name = if value > 0.0 { "Infinity" } else { "-Infinity" };
        return name.to_owned();
    }

    // The shortest digits that read back as `value`, as `d.ddde<exponent>`.
    let shortest = format!("{:e}", value.abs());
    let (mantissa, exponent) = shortest.split_once('e').unwrap_or((&shortest, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let (digits, exponent) = even_tie(value, &digits).unwrap_or((digits, exponent));
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if !(FIXED_FROM..FIXED_BELOW).contains(&exponent) {
        return format!("{sign}{}", exponent_form(&digits, exponent));
    }

    format!("{sign}{}", fixed_form(&digits, exponent))
}

/// `d.ddde+XX`: one digit, the other digits when there are any, and an
/// exponent of two digits or more.
fn exponent_form(digits: &str, exponent: i32) -> String {
    let mut chars = digits.chars();
    let first = chars.next().unwrap_or('0');
    let rest = chars.as_str();
    let point = if rest.is_empty() { "" } else { "." };
    let sign = if exponent < 0 { '-' } else { '+' };

    format!("{first}{point}{rest}e{sign}{:02}", exponent.unsigned_abs())
}

/// The digits with the point in its place, and `.0` for a whole number.
fn fixed_form(digits: &str, exponent: i32) -> String {
    let Ok(whole) = usize::try_from(exponent + 1) else {
        let zeros = usize::try_from(-(exponent + 1)).unwrap_or(0);

        return format!("0.{}{digits}", "0".repeat(zeros));
    };

    if whole == 0 {
        return format!("0.{digits}");
    }

    if digits.len() <= whole {
        return format!("{digits}{}.0", "0".repeat(whole - digits.len()));
    }

    let (head, tail) = digits.split_at_checked(whole).unwrap_or((digits, ""));

    format!("{head}.{tail}")
}

/// One piece of the compact text of a value.
enum Piece<'a> {
    /// A value that is no container.
    Scalar(&'a Json),
    /// The key of one field.
    Key(&'a Text),
    /// Punctuation.
    Raw(&'static str),
}

/// What a walk has still to visit.
enum Visit<'a> {
    Value(&'a Json),
    Piece(Piece<'a>),
}

/// Queues the pieces of one array after its `[`.
fn queue_array<'a>(pending: &mut Vec<Visit<'a>>, array: &'a JsonArray) {
    pending.push(Visit::Piece(Piece::Raw("]")));
    for (at, item) in array.0.iter().enumerate().rev() {
        pending.push(Visit::Value(item));
        if at > 0 {
            pending.push(Visit::Piece(Piece::Raw(",")));
        }
    }
}

/// Queues the pieces of one object after its `{`.
fn queue_object<'a>(pending: &mut Vec<Visit<'a>>, object: &'a JsonObject) {
    pending.push(Visit::Piece(Piece::Raw("}")));
    for (at, (name, item)) in object.0.iter().enumerate().rev() {
        pending.push(Visit::Value(item));
        pending.push(Visit::Piece(Piece::Raw(":")));
        pending.push(Visit::Piece(Piece::Key(name)));
        if at > 0 {
            pending.push(Visit::Piece(Piece::Raw(",")));
        }
    }
}

/// Gives `take` each piece of a compact text, in order, with no recursion.
fn walk<'a, E>(
    mut pending: Vec<Visit<'a>>,
    mut take: impl FnMut(Piece<'a>) -> Result<(), E>,
) -> Result<(), E> {
    while let Some(visit) = pending.pop() {
        match visit {
            Visit::Piece(piece) => take(piece)?,
            Visit::Value(Json::Array(array)) => {
                queue_array(&mut pending, array);
                take(Piece::Raw("["))?;
            }
            Visit::Value(Json::Object(object)) => {
                queue_object(&mut pending, object);
                take(Piece::Raw("{"))?;
            }
            Visit::Value(scalar) => take(Piece::Scalar(scalar))?,
        }
    }

    Ok(())
}

/// The text of a value that is no string and no container.
fn scalar_text(value: &Json) -> String {
    match value {
        Json::Bool(true) => "true".to_owned(),
        Json::Bool(false) => "false".to_owned(),
        Json::Int(integer) => integer.as_str().to_owned(),
        Json::Float(float) => float_text(*float),
        Json::Null | Json::Text(_) | Json::Array(_) | Json::Object(_) => "null".to_owned(),
    }
}

/// The count of bytes of the compact text of `value`: what
/// `len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())`
/// gives in Python.
pub(super) fn compact_size(value: &Json) -> Result<usize, LoneSurrogate> {
    let mut size = 0_usize;
    walk(vec![Visit::Value(value)], |piece| {
        size += match piece {
            Piece::Raw(raw) => raw.len(),
            Piece::Key(text) | Piece::Scalar(Json::Text(text)) => string_size(text)?,
            Piece::Scalar(scalar) => scalar_text(scalar).len(),
        };

        Ok(())
    })?;

    Ok(size)
}

/// Writes each piece of a compact text.
fn write_pieces(pending: Vec<Visit<'_>>, mut out: String) -> Result<String, LoneSurrogate> {
    walk(pending, |piece| {
        match piece {
            Piece::Raw(raw) => out.push_str(raw),
            Piece::Key(text) | Piece::Scalar(Json::Text(text)) => {
                write_string(&mut out, text.as_str().ok_or(LoneSurrogate)?);
            }
            Piece::Scalar(scalar) => out.push_str(&scalar_text(scalar)),
        }

        Ok(())
    })?;

    Ok(out)
}

/// The compact text of `value`: what
/// `json.dumps(value, separators=(",", ":"), ensure_ascii=False)` gives in
/// Python.
#[cfg(test)]
pub(super) fn compact_text(value: &Json) -> Result<String, LoneSurrogate> {
    write_pieces(vec![Visit::Value(value)], String::new())
}

/// The compact text of one object.
pub(super) fn object_text(object: &JsonObject) -> Result<String, LoneSurrogate> {
    let mut pending = Vec::new();
    queue_object(&mut pending, object);

    write_pieces(pending, "{".to_owned())
}

/// Whether `value` nests more than `limit` objects and arrays, itself
/// included.
pub(super) fn nests_past(value: &Json, limit: usize) -> bool {
    let mut pending = vec![(value, 1_usize)];
    while let Some((holder, level)) = pending.pop() {
        let below = level + 1;
        match holder {
            Json::Array(array) if level <= limit => {
                pending.extend(array.0.iter().map(|item| (item, below)));
            }
            Json::Object(object) if level <= limit => {
                pending.extend(object.0.iter().map(|(_, item)| (item, below)));
            }
            Json::Array(_) | Json::Object(_) => return true,
            _ => {}
        }
    }

    false
}

/// Whether a value of `object` holds a float that is not finite: `NaN`,
/// `Infinity` or `-Infinity`. JSON has no text for such a number.
pub(super) fn holds_non_finite(object: &JsonObject) -> bool {
    let mut pending: Vec<&Json> = object.0.iter().map(|(_, item)| item).collect();
    while let Some(holder) = pending.pop() {
        match holder {
            Json::Float(float) if !float.is_finite() => return true,
            Json::Array(array) => pending.extend(array.0.iter()),
            Json::Object(object) => pending.extend(object.0.iter().map(|(_, item)| item)),
            _ => {}
        }
    }

    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn python(text: &str) -> Result<Json, ReadError> {
        read(text, Dialect::Python)
    }

    fn compact(text: &str) -> String {
        compact_text(&python(text).unwrap()).unwrap()
    }

    fn nested(levels: usize) -> String {
        format!("{}1{}", "[".repeat(levels), "]".repeat(levels))
    }

    #[test]
    fn each_kind_of_value_reads() {
        let value = python(r#" {"a": [1, -0, 2.5, true, false, null, "x"], "b": {}, "c": []} "#);
        let value = value.unwrap();
        let object = value.as_object().unwrap();
        let items: Vec<&Json> = object
            .get("a")
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .collect();

        assert_eq!(object.len(), 3);
        assert_eq!(items.len(), 7);
        assert_eq!(items[0].as_integer().unwrap().as_str(), "1");
        assert_eq!(items[1].as_integer().unwrap().as_str(), "0");
        assert_eq!(items[2], &Json::Float(2.5));
        assert!(items[3].is_true());
        assert!(!items[4].is_true());
        assert_eq!(items[5], &Json::Null);
        assert_eq!(items[6].as_text().unwrap(), &Text::from("x"));
        assert!(object.get("b").unwrap().as_object().unwrap().is_empty());
        assert!(object.get("c").unwrap().as_array().unwrap().is_empty());
        assert_eq!(object.get("d"), None);
        assert_eq!(object.iter().count(), 3);
    }

    #[test]
    fn the_python_dialect_reads_the_three_names() {
        let value = python("[NaN, Infinity, -Infinity]").unwrap();
        let items: Vec<&Json> = value.as_array().unwrap().iter().collect();

        assert!(matches!(items[0], Json::Float(float) if float.is_nan()));
        assert_eq!(items[1], &Json::Float(f64::INFINITY));
        assert_eq!(items[2], &Json::Float(f64::NEG_INFINITY));
        for text in ["NaN", "Infinity", "-Infinity", "[NaN]"] {
            assert_eq!(read(text, Dialect::Strict), Err(ReadError::NotJson));
        }
    }

    #[test]
    fn a_text_that_is_not_json_is_refused() {
        let refused = [
            "",
            " ",
            "\u{feff}1",
            "\u{a0}1",
            "nul",
            "nulll",
            "True",
            "-",
            "-x",
            "+1",
            "01",
            "-01",
            ".5",
            "1.",
            "1.e5",
            "1e",
            "1e+",
            "0x1",
            "-NaN",
            "infinity",
            "[1,]",
            "[,1]",
            "[1 2]",
            "[",
            "{",
            "{\"a\"}",
            "{\"a\":}",
            "{\"a\":1,}",
            "{a:1}",
            "{1:1}",
            "{'a':1}",
            "\"a",
            "\"a\tb\"",
            "\"a\u{0}b\"",
            "\"\\x\"",
            "\"\\u12\"",
            "\"\\u12g4\"",
            "\"\\ud83d\\uzzzz\"",
            "\"\\",
            "1 2",
            "{} x",
            "[1]]",
        ];

        for text in refused {
            assert_eq!(python(text), Err(ReadError::NotJson), "{text:?}");
        }
    }

    #[test]
    fn a_number_is_an_integer_or_a_float() {
        let floats = [
            ("1.0", 1.0),
            ("-0.0", -0.0),
            ("1e2", 100.0),
            ("1E+2", 100.0),
            ("1e-2", 0.01),
            ("0e0", 0.0),
            ("1e999", f64::INFINITY),
            ("-1e999", f64::NEG_INFINITY),
            ("1e-999", 0.0),
        ];
        for (text, float) in floats {
            let Ok(Json::Float(read)) = python(text) else {
                panic!("{text} is no float");
            };

            assert_eq!(read.to_bits(), float.to_bits(), "{text}");
        }

        for text in ["0", "-0", "7", "-7", "36893488147419103232"] {
            assert!(matches!(python(text), Ok(Json::Int(_))), "{text}");
        }

        let long = "9".repeat(4301);
        assert!(matches!(python(&"9".repeat(4300)), Ok(Json::Int(_))));
        assert_eq!(python(&long), Err(ReadError::IntegerTooLong));
        assert_eq!(python(&format!("-{long}")), Err(ReadError::IntegerTooLong));
        assert_eq!(python(&format!("[{long}")), Err(ReadError::IntegerTooLong));
        assert!(matches!(python(&format!("{long}.5")), Ok(Json::Float(_))));
        assert!(matches!(python(&format!("{long}e1")), Ok(Json::Float(_))));
        assert_eq!(python(&format!("{long}e")), Err(ReadError::IntegerTooLong));
    }

    #[test]
    fn a_string_reads_each_escape() {
        let value = python(r#""\"\\\/\b\f\n\r\t\u00e9\u0041\ud83d\ude00\u0000""#).unwrap();

        assert_eq!(
            value.as_text().unwrap(),
            &Text::from("\"\\/\u{8}\u{c}\n\r\t\u{e9}A\u{1f600}\u{0}")
        );
        assert_eq!(
            python("\"\u{7f}\u{2028}\u{1f600}\"")
                .unwrap()
                .as_text()
                .unwrap(),
            &Text::from("\u{7f}\u{2028}\u{1f600}")
        );
    }

    #[test]
    fn a_lone_surrogate_stays_in_the_text() {
        let units = |text: &str| python(text).unwrap().as_text().unwrap().to_utf16();

        assert_eq!(units(r#""a\ud800b""#), vec![0x61, 0xd800, 0x62]);
        assert_eq!(units(r#""\udc00""#), vec![0xdc00]);
        assert_eq!(units(r#""\ud83d\u0041""#), vec![0xd83d, 0x41]);
        assert_eq!(
            units(r#""\ud83d\ud83d\ude00""#),
            vec![0xd83d, 0xd83d, 0xde00]
        );
        assert_eq!(units(r#""\ude00\ud83d""#), vec![0xde00, 0xd83d]);
        assert_eq!(units(r#""\ud83d\\ude00""#).len(), 7);
    }

    #[test]
    fn the_last_value_of_a_key_stays_at_the_first_place() {
        assert_eq!(compact(r#"{"a":1,"b":2,"a":3}"#), r#"{"a":3,"b":2}"#);

        let many: Vec<String> = (0..40).map(|at| format!("\"k{}\":{at}", at % 20)).collect();
        let value = python(&format!("{{{}}}", many.join(","))).unwrap();
        let object = value.as_object().unwrap();

        assert_eq!(object.len(), 20);
        assert_eq!(object.iter().next().unwrap().0, &Text::from("k0"));
        assert_eq!(
            object.get("k0").unwrap().as_integer().unwrap().as_str(),
            "20"
        );
        assert_eq!(
            object.get("k19").unwrap().as_integer().unwrap().as_str(),
            "39"
        );
    }

    #[test]
    fn a_line_nests_to_the_limit_and_no_deeper() {
        assert!(python(&nested(MAX_LINE_DEPTH)).is_ok());
        assert_eq!(python(&nested(MAX_LINE_DEPTH + 1)), Err(ReadError::TooDeep));
        assert_eq!(
            python(&"{\"a\":".repeat(MAX_LINE_DEPTH + 1)),
            Err(ReadError::TooDeep)
        );

        // An empty container is a level too, and the depth is found before a
        // later fault of the text.
        let empty = format!(
            "{}[]{}",
            "[".repeat(MAX_LINE_DEPTH),
            "]".repeat(MAX_LINE_DEPTH)
        );
        assert_eq!(python(&empty), Err(ReadError::TooDeep));
        assert_eq!(python(&"[".repeat(400_000)), Err(ReadError::TooDeep));
        assert_eq!(python(&"[".repeat(MAX_LINE_DEPTH)), Err(ReadError::NotJson));
    }

    #[test]
    fn a_deep_value_drops_with_no_recursion() {
        let value = python(&nested(MAX_LINE_DEPTH)).unwrap();

        assert!(nests_past(&value, MAX_LINE_DEPTH - 1));
        assert!(!nests_past(&value, MAX_LINE_DEPTH));
        assert_eq!(compact_size(&value), Ok(2 * MAX_LINE_DEPTH + 1));
        drop(value);
    }

    #[test]
    fn a_deep_value_copies_with_no_recursion() {
        let value = python(&nested(MAX_LINE_DEPTH)).unwrap();
        let copy = value.clone();

        assert!(nests_past(&copy, MAX_LINE_DEPTH - 1));
        assert_eq!(compact_size(&copy), Ok(2 * MAX_LINE_DEPTH + 1));
    }

    #[test]
    fn two_deep_values_compare_with_no_recursion() {
        let value = python(&nested(MAX_LINE_DEPTH)).unwrap();
        let same = python(&nested(MAX_LINE_DEPTH)).unwrap();
        let other = python(&nested(MAX_LINE_DEPTH).replace('1', "2")).unwrap();

        assert!(value == same);
        assert!(value != other);
    }

    #[test]
    fn a_deep_value_prints_with_no_recursion() {
        let value = python(&nested(MAX_LINE_DEPTH)).unwrap();

        assert_eq!(format!("{value:?}"), nested(MAX_LINE_DEPTH));
    }

    #[test]
    fn a_copy_is_equal_and_holds_its_own_values() {
        let text = r#"{"a":[1,-2.5,true,false,null,"x",{"b":[]}],"c":{},"d":"\ud800"}"#;
        let value = python(text).unwrap();
        let mut copy = value.clone().into_object().unwrap();

        assert_eq!(Json::Object(copy.clone()), value);
        assert_eq!(
            copy.take("a").map(|taken| taken.as_array().is_some()),
            Some(true)
        );
        assert_ne!(Json::Object(copy), value);
        assert!(
            value
                .as_object()
                .unwrap()
                .get("a")
                .unwrap()
                .as_array()
                .is_some()
        );
    }

    #[test]
    fn two_values_are_equal_only_with_the_same_content() {
        let equal = [
            "null",
            "true",
            "7",
            "-0.0",
            "\"x\"",
            "\"\\ud800\"",
            "[]",
            "{}",
            r#"[1,[2,{"a":null}]]"#,
            r#"{"a":1,"b":[true]}"#,
        ];
        let differ = [
            ("null", "false"),
            ("true", "false"),
            ("1", "2"),
            ("1", "1.0"),
            ("1.5", "2.5"),
            ("\"x\"", "\"y\""),
            ("[]", "{}"),
            ("[1]", "[1,1]"),
            ("[1,2]", "[1,3]"),
            ("[[1]]", "[[2]]"),
            (r#"{"a":1}"#, r#"{"b":1}"#),
            (r#"{"a":1}"#, r#"{"a":2}"#),
            (r#"{"a":1}"#, r#"{"a":1,"b":2}"#),
            (r#"{"a":1,"b":2}"#, r#"{"b":2,"a":1}"#),
            // A float that is not a number is equal to no float.
            ("NaN", "NaN"),
            ("[NaN]", "[NaN]"),
        ];

        for text in equal {
            assert!(python(text).unwrap() == python(text).unwrap(), "{text}");
        }

        for (left, right) in differ {
            assert!(
                python(left).unwrap() != python(right).unwrap(),
                "{left} {right}"
            );
            assert!(
                python(right).unwrap() != python(left).unwrap(),
                "{left} {right}"
            );
        }
    }

    #[test]
    fn a_value_prints_in_the_compact_form() {
        let text = r#"{"a":[1,-2.5,true,false,null,"x\n",{"b":[]}],"c":{},"d":NaN}"#;

        assert_eq!(format!("{:?}", python(text).unwrap()), text);
        assert_eq!(
            format!("{:?}", python("[1e999, 2]").unwrap()),
            "[Infinity,2]"
        );
        assert_eq!(
            format!("{:?}", python(r#"{"\ud800":"a\ud800"}"#).unwrap()),
            "{utf16[55296]:utf16[97, 55296]}"
        );
    }

    #[test]
    fn the_writer_makes_the_bytes_of_python() {
        let same = [
            r#"{"a":[1,2.5,true,false,null,"x"],"b":{},"c":[]}"#,
            r#"["caf\u00e9 \u2028 \ud83d\ude00 \u007f"]"#,
            r#"["\"\\\b\f\n\r\t\u0000\u001f"]"#,
        ];
        let written = [
            r#"{"a":[1,2.5,true,false,null,"x"],"b":{},"c":[]}"#,
            "[\"caf\u{e9} \u{2028} \u{1f600} \u{7f}\"]",
            r#"["\"\\\b\f\n\r\t\u0000\u001f"]"#,
        ];

        for (text, expected) in same.iter().zip(written) {
            let value = python(text).unwrap();

            assert_eq!(compact_text(&value).unwrap(), expected);
            assert_eq!(compact_size(&value), Ok(expected.len()));
        }

        let lone = python(r#"{"a":["\ud800"]}"#).unwrap();
        assert_eq!(compact_size(&lone), Err(LoneSurrogate));
        assert_eq!(compact_text(&lone), Err(LoneSurrogate));
        assert_eq!(
            compact_size(&python(r#"{"\ud800":1}"#).unwrap()),
            Err(LoneSurrogate)
        );
    }

    #[test]
    fn a_float_has_the_text_of_python() {
        let texts = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (100.0, "100.0"),
            (0.1, "0.1"),
            (0.002, "0.002"),
            (1.5, "1.5"),
            (-12.25, "-12.25"),
            (123_456.789, "123456.789"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (0.000_012_5, "1.25e-05"),
            (1e15, "1000000000000000.0"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e100, "1e+100"),
            (-1e100, "-1e+100"),
            (1.797_693_134_862_315_7e308, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
            (2.5e-7, "2.5e-07"),
            (0.25, "0.25"),
            (2.5, "2.5"),
            (f64::NAN, "NaN"),
            (f64::INFINITY, "Infinity"),
            (f64::NEG_INFINITY, "-Infinity"),
        ];

        for (float, text) in texts {
            assert_eq!(float_text(float), text);
        }

        // A float at the same distance from two shortest texts. Python takes
        // the even digit. The formatter of Rust takes the higher digit.
        let ties = [
            (669_758_432_410_385.0, 0.25, "669758432410385.2"),
            (-669_758_432_410_385.0, -0.25, "-669758432410385.2"),
            (20_278_963_822_605.0, 0.3125, "20278963822605.312"),
            (163_151_449_678_028.0, 0.625, "163151449678028.62"),
            (669_758_432_410_385.0, 0.75, "669758432410385.8"),
        ];

        for (whole, fraction, text) in ties {
            assert_eq!(float_text(whole + fraction), text);
        }

        // A tie has 25 decimal places at most. A power of two is a tie only
        // when the lower text reads back as the same float.
        let small = [
            (1.0, -25, "2.9802322387695312e-08"),
            (5.0, -23, "5.960464477539062e-07"),
            (1.0, -24, "5.960464477539063e-08"),
            (3.0, -25, "8.940696716308594e-08"),
        ];

        for (odd, power, text) in small {
            assert_eq!(float_text(odd * 2.0_f64.powi(power)), text);
        }
    }

    #[test]
    fn an_object_writer_writes_each_kind_of_field() {
        let written = ObjectWriter::new()
            .text("type", "x\n")
            .raw("n", "7")
            .text_or_null("a", Some("b"))
            .text_or_null("c", None)
            .raw_or_null("d", Some("1.5".to_owned()))
            .raw_or_null("e", None)
            .text_if("f", Some("g"))
            .text_if("h", None)
            .raw_if("i", Some(flag_text(true).to_owned()))
            .raw_if("j", None)
            .raw("k", &array_text(["l", "m\""].into_iter()))
            .raw("o", &array_text(std::iter::empty()))
            .raw("p", flag_text(false))
            .finish();

        assert_eq!(
            written,
            r#"{"type":"x\n","n":7,"a":"b","c":null,"d":1.5,"e":null,"f":"g","i":true,"k":["l","m\""],"o":[],"p":false}"#
        );
        assert_eq!(ObjectWriter::new().finish(), "{}");
        assert_eq!(object_text(&JsonObject::default()), Ok("{}".to_owned()));
        assert_eq!(
            object_text(&python(&written).unwrap().into_object().unwrap()),
            Ok(written)
        );
    }

    #[test]
    fn nesting_counts_the_value_itself() {
        let value = python(r#"{"a":[[1]],"b":2}"#).unwrap();

        assert!(nests_past(&value, 2));
        assert!(!nests_past(&value, 3));
        assert!(!nests_past(&python("1").unwrap(), 0));
        assert!(nests_past(&python("[]").unwrap(), 0));
    }

    #[test]
    fn a_float_that_is_not_finite_is_found_at_each_depth() {
        let holds = |text: &str| holds_non_finite(&python(text).unwrap().into_object().unwrap());

        for text in [
            r#"{"n":NaN}"#,
            r#"{"n":1e999}"#,
            r#"{"n":-Infinity}"#,
            r#"{"a":1,"b":[2,{"c":[Infinity]}]}"#,
        ] {
            assert!(holds(text), "{text}");
        }

        for text in [
            "{}",
            r#"{"n":1e308}"#,
            r#"{"n":-0.0,"text":"NaN","a":[null,true,7,{"b":[]}]}"#,
        ] {
            assert!(!holds(text), "{text}");
        }

        let deep = format!(
            "{{\"a\":{}NaN{}}}",
            "[".repeat(MAX_LINE_DEPTH - 1),
            "]".repeat(MAX_LINE_DEPTH - 1)
        );
        assert!(holds(&deep));
    }

    #[test]
    fn a_field_can_leave_its_object() {
        let mut object = python(r#"{"a":[1],"b":2}"#).unwrap().into_object().unwrap();

        assert_eq!(
            object.take("a").map(|value| value.as_array().is_some()),
            Some(true)
        );
        assert_eq!(object.get("a"), Some(&Json::Null));
        assert_eq!(object.take("z"), None);
        assert_eq!(JsonObject::of(Vec::new()), JsonObject::default());
        assert_eq!(python("[]").unwrap().into_object(), None);
    }
}
