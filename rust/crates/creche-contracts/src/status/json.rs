//! The JSON text of the files of contract 05: one reader and one writer.
//!
//! The reader takes the texts that the Python reader of each component takes.
//! `serde_json` refuses four forms that the Python reader accepts, and
//! `vectors/data/status` holds a document of each form:
//!
//! - the words `NaN`, `Infinity` and `-Infinity`,
//! - an integer of more than 64 bits,
//! - a key that an object holds two times,
//! - a nesting of more than 128 levels.
//!
//! The writer makes the bytes that `json.dumps` of Python makes, in the three
//! layouts that the writers of contract 05 use.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

/// The largest count of digits in an integer. Python reads no longer text as
/// an integer.
pub const INTEGER_DIGITS_MAX: usize = 4300;

// CONTRACT-QUESTION: contract 05 §2 gives no cap on the nesting of a file.
// The Python reader stops at a depth that depends on the interpreter: between
// 5000 and 10000 levels under Python 3.12 and 3.13, and more under Python
// 3.14. The reader here stops at 256 levels. A document of contract 05 nests
// 4 levels, and the deepest document of `vectors/data/status` nests 201.
//
// A larger cap costs stack: `Drop`, `Clone`, `Debug` and the writer use one
// frame for each level. A stack overflow stops the process, and no code can
// catch it. Measured on macOS arm64: a value of 256 levels needs between 256
// and 384 KiB in a build with no optimization, and less than 256 KiB in a
// release build. A value of 1000 levels needs more than 1 MiB, and a spawned
// thread has 2 MiB. A test holds a value of the full depth to a stack of
// 1 MiB.
/// The deepest nesting of arrays and objects that the reader takes.
pub const DEPTH_MAX: usize = 256;

/// An integer of a JSON text.
///
/// The integer has no bound but [`INTEGER_DIGITS_MAX`] digits, because the
/// Python readers keep an integer of each size. The type holds the decimal
/// digits in one form: no zero at the start, and no sign on zero.
///
/// ```
/// use creche_contracts::status::json::Integer;
///
/// let epoch: Integer = "1180591620717411303424".parse()?;
/// assert_eq!(epoch.to_string(), "1180591620717411303424");
/// assert_eq!(epoch.as_u64(), None);
/// assert_eq!(Integer::from(7_u64).as_u64(), Some(7));
/// # Ok::<(), creche_contracts::status::json::IntegerError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::json::Integer;
///
/// let integer = Integer {
///     negative: false,
///     digits: String::from("007"),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Integer {
    negative: bool,
    digits: String,
}

impl Integer {
    /// Whether the integer is below zero.
    #[must_use]
    pub fn is_negative(&self) -> bool {
        self.negative
    }

    /// The integer as a `u64`. `None` when it does not fit.
    #[must_use]
    pub fn as_u64(&self) -> Option<u64> {
        if self.negative {
            return None;
        }

        self.digits.parse().ok()
    }

    /// The integer as an `i64`. `None` when it does not fit.
    #[must_use]
    pub fn as_i64(&self) -> Option<i64> {
        self.to_string().parse().ok()
    }

    /// The nearest `f64`. `None` when the integer is past the range of a
    /// finite `f64`.
    #[must_use]
    pub fn to_f64(&self) -> Option<f64> {
        let nearest: f64 = self.to_string().parse().ok()?;

        nearest.is_finite().then_some(nearest)
    }

    /// Reads `-?(0|[1-9][0-9]*)` with the digit cap. `-0` is zero.
    fn read(text: &str) -> Result<Self, IntegerError> {
        let (negative, digits) = match text.strip_prefix('-') {
            Some(digits) => (true, digits),
            None => (false, text),
        };
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(IntegerError::NotDigits);
        }

        if digits.len() > 1 && digits.starts_with('0') {
            return Err(IntegerError::LeadingZero);
        }

        if digits.len() > INTEGER_DIGITS_MAX {
            return Err(IntegerError::TooLong);
        }

        Ok(Self {
            negative: negative && digits != "0",
            digits: digits.to_owned(),
        })
    }
}

impl std::str::FromStr for Integer {
    type Err = IntegerError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::read(text)
    }
}

impl From<u64> for Integer {
    fn from(value: u64) -> Self {
        Self {
            negative: false,
            digits: value.to_string(),
        }
    }
}

impl fmt::Display for Integer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.negative {
            f.write_str("-")?;
        }

        f.write_str(&self.digits)
    }
}

/// Why a text is not an integer of a JSON text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntegerError {
    /// The text is not an optional `-` and one or more ASCII digits.
    NotDigits,
    /// The text has a zero before another digit.
    LeadingZero,
    /// The text has more digits than [`INTEGER_DIGITS_MAX`].
    TooLong,
}

impl fmt::Display for IntegerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotDigits => f.write_str("an integer is an optional - and ASCII digits"),
            Self::LeadingZero => f.write_str("an integer has no zero before another digit"),
            Self::TooLong => write!(f, "an integer has {INTEGER_DIGITS_MAX} digits at most"),
        }
    }
}

impl Error for IntegerError {}

/// An object of a JSON text: its keys and values, in the order of the text.
///
/// A key is in the object one time. When a text holds a key two times, the
/// object keeps the place of the first and the value of the last, as a Python
/// `dict` does.
///
/// ```
/// use creche_contracts::status::json::{Json, Object};
///
/// let mut object = Object::new();
/// object.insert("message", Json::Null);
/// object.insert("message", Json::Bool(true));
/// assert_eq!(object.len(), 1);
/// assert_eq!(object.get("message"), Some(&Json::Bool(true)));
/// ```
///
/// Code outside this module cannot build an object with one key two times:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::json::{Json, Object};
///
/// let object = Object {
///     pairs: vec![(String::from("a"), Json::Null), (String::from("a"), Json::Null)],
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Object {
    pairs: Vec<(String, Json)>,
}

impl Object {
    /// An object with no key.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The value of `key`. `None` when the object has no such key.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.pairs
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    }

    /// Each key and its value, in the order of the text.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Json)> {
        self.pairs.iter().map(|(key, value)| (key.as_str(), value))
    }

    /// The count of keys.
    #[must_use]
    pub fn len(&self) -> usize {
        self.pairs.len()
    }

    /// Whether the object has no key.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.pairs.is_empty()
    }

    /// The object with no key of `keys`. The other keys keep their order.
    pub(crate) fn without(&self, keys: &[&str]) -> Self {
        let kept = self
            .pairs
            .iter()
            .filter(|(key, _)| !keys.contains(&key.as_str()));

        Self {
            pairs: kept.cloned().collect(),
        }
    }

    /// Puts each key of `tail` after the keys of the object. The caller makes
    /// sure that the object holds no key of `tail`.
    pub(crate) fn append(&mut self, tail: &Self) {
        self.pairs.extend(tail.pairs.iter().cloned());
    }

    /// Sets `key` to `value`. A key that the object holds keeps its place.
    pub fn insert(&mut self, key: &str, value: Json) {
        match self.pairs.iter_mut().find(|(name, _)| name == key) {
            Some((_, held)) => *held = value,
            None => self.pairs.push((key.to_owned(), value)),
        }
    }
}

impl FromIterator<(String, Json)> for Object {
    fn from_iter<I: IntoIterator<Item = (String, Json)>>(pairs: I) -> Self {
        let mut object = Self::new();
        for (key, value) in pairs {
            object.insert(&key, value);
        }

        object
    }
}

/// One value of a JSON text, as the Python reader of each component reads it.
///
/// A float can be `NaN` or an infinity: the Python reader takes the words
/// `NaN`, `Infinity` and `-Infinity`. `NaN` is not equal to `NaN`, so two
/// values that hold it are not equal.
///
/// The type is the opaque value of contract 05 §3.3: an extra key of a fault.
/// No other field of a contract type holds it.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// A number with no fraction and no exponent.
    Integer(Integer),
    /// A number with a fraction or an exponent, or one of the three words.
    Float(f64),
    /// A string. It holds no lone surrogate.
    String(String),
    /// An array.
    Array(Vec<Json>),
    /// An object.
    Object(Object),
}

/// The type of a JSON value, for an error that names what a reader found.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JsonKind {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool,
    /// An integer.
    Integer,
    /// A float.
    Float,
    /// A string.
    String,
    /// An array.
    Array,
    /// An object.
    Object,
}

impl Json {
    /// The type of the value.
    #[must_use]
    pub fn kind(&self) -> JsonKind {
        match self {
            Self::Null => JsonKind::Null,
            Self::Bool(_) => JsonKind::Bool,
            Self::Integer(_) => JsonKind::Integer,
            Self::Float(_) => JsonKind::Float,
            Self::String(_) => JsonKind::String,
            Self::Array(_) => JsonKind::Array,
            Self::Object(_) => JsonKind::Object,
        }
    }

    /// The text of a string. `None` for each other type.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(text) => Some(text),
            _ => None,
        }
    }

    /// Reads one JSON text, as `json.loads` of Python reads a `str`.
    ///
    /// # Errors
    ///
    /// [`JsonError`] says why the text is not one JSON value.
    pub fn parse(text: &str) -> Result<Self, JsonError> {
        Parser::new(text).document()
    }

    // CONTRACT-QUESTION: contract 05 §2 does not name the encoding of a file.
    // Each Python writer writes UTF-8. The Python reader of the noticeboard
    // also reads a file in UTF-16 or UTF-32, because `json.loads` finds the
    // encoding of bytes. The reader here takes UTF-8 and no other encoding.
    // To take another encoding, the reader must find it from the first bytes
    // of the file, and two readers of one file then read different bytes.
    /// Reads the bytes of one file. The bytes are UTF-8.
    ///
    /// # Errors
    ///
    /// [`JsonError::NotUtf8`] for bytes that are not UTF-8, and each error of
    /// [`Json::parse`].
    pub fn parse_bytes(bytes: &[u8], mark: ByteOrderMark) -> Result<Self, JsonError> {
        let text = std::str::from_utf8(bytes).map_err(|_| JsonError::NotUtf8)?;
        let text = match mark {
            ByteOrderMark::Refuse => text,
            ByteOrderMark::Skip => text.strip_prefix(BYTE_ORDER_MARK).unwrap_or(text),
        };

        Self::parse(text)
    }

    /// The text of the value, as `json.dumps` of Python writes it.
    pub(crate) fn encode(&self, layout: Layout, charset: Charset) -> String {
        let mut out = String::new();
        Encoder { layout, charset }.value(self, 0, &mut out);

        out
    }
}

/// U+FEFF at the start of a text.
const BYTE_ORDER_MARK: char = '\u{feff}';

/// What a reader does with a byte order mark at the start of a file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ByteOrderMark {
    /// The file is not a JSON text.
    Refuse,
    /// The reader skips one mark.
    Skip,
}

/// Why a text is not one JSON value.
///
/// `at` is the offset in bytes, from 0, at which the reader stopped. No
/// variant holds the text: the text is untrusted, and a caller writes this
/// error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JsonError {
    /// The bytes are not UTF-8.
    NotUtf8,
    /// The text starts with a byte order mark.
    ByteOrderMark,
    /// The text breaks the JSON grammar.
    Syntax {
        /// The offset of the byte, from 0.
        at: usize,
    },
    /// An integer has more digits than [`INTEGER_DIGITS_MAX`].
    IntegerTooLong {
        /// The offset of the first digit, from 0.
        at: usize,
    },
    /// The text nests deeper than [`DEPTH_MAX`] levels.
    TooDeep {
        /// The offset of the bracket, from 0.
        at: usize,
    },
    // CONTRACT-QUESTION: contract 05 §2 does not say what a reader does with an
    // escape of half a surrogate pair. The Python reader keeps it as a lone
    // surrogate in the string. A Rust string cannot hold one. The reader
    // here refuses the text. To keep the surrogate, each string of a
    // contract type needs a type that is not `String`.
    /// A string holds an escape of one half of a surrogate pair.
    LoneSurrogate {
        /// The offset of the escape, from 0.
        at: usize,
    },
}

impl fmt::Display for JsonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotUtf8 => f.write_str("the bytes are not UTF-8"),
            Self::ByteOrderMark => f.write_str("the text starts with a byte order mark"),
            Self::Syntax { at } => write!(f, "the byte at offset {at} breaks the JSON grammar"),
            Self::IntegerTooLong { at } => write!(
                f,
                "the integer at offset {at} has more than {INTEGER_DIGITS_MAX} digits"
            ),
            Self::TooDeep { at } => write!(
                f,
                "the text nests deeper than {DEPTH_MAX} levels at offset {at}"
            ),
            Self::LoneSurrogate { at } => write!(
                f,
                "the escape at offset {at} is one half of a surrogate pair"
            ),
        }
    }
}

impl Error for JsonError {}

// --- the reader ---

/// An array or an object that the reader has not closed.
enum Open {
    Array(Vec<Json>),
    Object {
        pairs: Vec<(String, Json)>,
        /// The place of each key in `pairs`.
        places: BTreeMap<String, usize>,
        /// The key whose value the reader reads now.
        key: String,
    },
}

/// What the reader found after a value.
enum Next {
    /// A comma: one more value follows.
    Value,
    /// The end of the text.
    End,
}

/// The first code unit of the high half of a surrogate pair.
const HIGH_SURROGATE_FIRST: u32 = 0xd800;
/// The first code unit of the low half of a surrogate pair.
const LOW_SURROGATE_FIRST: u32 = 0xdc00;
/// The last code unit of the low half of a surrogate pair.
const LOW_SURROGATE_LAST: u32 = 0xdfff;
/// The first code point outside the basic plane.
const ASTRAL_FIRST: u32 = 0x1_0000;
/// The count of bits that one half of a surrogate pair holds.
const SURROGATE_BITS: u32 = 10;
/// The count of hex digits in a `\u` escape.
const ESCAPE_DIGITS: usize = 4;
/// The first byte that is not a control character.
const FIRST_PRINTABLE: u8 = 0x20;

struct Parser<'a> {
    text: &'a str,
    at: usize,
}

impl<'a> Parser<'a> {
    fn new(text: &'a str) -> Self {
        Self { text, at: 0 }
    }

    fn peek(&self) -> Option<u8> {
        self.text.as_bytes().get(self.at).copied()
    }

    fn syntax<T>(&self) -> Result<T, JsonError> {
        Err(JsonError::Syntax { at: self.at })
    }

    /// Skips the four characters that JSON calls white space.
    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    /// Takes `word` when the text has it here.
    fn take(&mut self, word: &str) -> bool {
        let rest = self.text.as_bytes().get(self.at..).unwrap_or_default();
        if !rest.starts_with(word.as_bytes()) {
            return false;
        }

        self.at += word.len();

        true
    }

    fn expect(&mut self, byte: u8) -> Result<(), JsonError> {
        if self.peek() != Some(byte) {
            return self.syntax();
        }

        self.at += 1;

        Ok(())
    }

    /// One whole text: white space, one value, white space.
    fn document(mut self) -> Result<Json, JsonError> {
        if self.text.starts_with(BYTE_ORDER_MARK) {
            return Err(JsonError::ByteOrderMark);
        }

        let mut open: Vec<Open> = Vec::new();
        loop {
            self.skip_space();
            let Some(mut value) = self.value(&mut open)? else {
                // The reader opened an array or an object. Its first value
                // is next.
                continue;
            };

            loop {
                match self.after(value, &mut open)? {
                    (None, Next::Value) => break,
                    (Some(done), Next::End) => return Ok(done),
                    (Some(closed), Next::Value) => value = closed,
                    (None, Next::End) => return self.syntax(),
                }
            }
        }
    }

    /// Reads the start of one value. `None` when the value is an array or an
    /// object with content: the reader then pushed it to `open`.
    fn value(&mut self, open: &mut Vec<Open>) -> Result<Option<Json>, JsonError> {
        let Some(byte) = self.peek() else {
            return self.syntax();
        };

        match byte {
            b'"' => Ok(Some(Json::String(self.string()?))),
            b'[' => self.array(open),
            b'{' => self.object(open),
            b'-' | b'0'..=b'9' => Ok(Some(self.number()?)),
            _ => self.word().map(Some),
        }
    }

    fn word(&mut self) -> Result<Json, JsonError> {
        let words = [
            ("null", Json::Null),
            ("true", Json::Bool(true)),
            ("false", Json::Bool(false)),
            ("NaN", Json::Float(f64::NAN)),
            ("Infinity", Json::Float(f64::INFINITY)),
        ];
        for (word, value) in words {
            if self.take(word) {
                return Ok(value);
            }
        }

        self.syntax()
    }

    /// Takes the bracket that opens an array or an object. An empty one
    /// counts as a level too.
    fn open_bracket(&mut self, open: &[Open]) -> Result<(), JsonError> {
        if open.len() >= DEPTH_MAX {
            return Err(JsonError::TooDeep { at: self.at });
        }

        self.at += 1;

        Ok(())
    }

    fn array(&mut self, open: &mut Vec<Open>) -> Result<Option<Json>, JsonError> {
        self.open_bracket(open)?;
        self.skip_space();
        if self.peek() == Some(b']') {
            self.at += 1;

            return Ok(Some(Json::Array(Vec::new())));
        }

        open.push(Open::Array(Vec::new()));

        Ok(None)
    }

    fn object(&mut self, open: &mut Vec<Open>) -> Result<Option<Json>, JsonError> {
        self.open_bracket(open)?;
        self.skip_space();
        if self.peek() == Some(b'}') {
            self.at += 1;

            return Ok(Some(Json::Object(Object::new())));
        }

        let key = self.key()?;
        let object = Open::Object {
            pairs: Vec::new(),
            places: BTreeMap::new(),
            key,
        };
        open.push(object);

        Ok(None)
    }

    /// One key and its colon.
    fn key(&mut self) -> Result<String, JsonError> {
        if self.peek() != Some(b'"') {
            return self.syntax();
        }

        let key = self.string()?;
        self.skip_space();
        self.expect(b':')?;

        Ok(key)
    }

    /// Puts `value` into the innermost open array or object, and reads what
    /// follows the value. The first part of the result is an array or an
    /// object that the reader closed, or the value of the whole text.
    fn after(
        &mut self,
        value: Json,
        open: &mut Vec<Open>,
    ) -> Result<(Option<Json>, Next), JsonError> {
        self.skip_space();
        let Some(innermost) = open.last_mut() else {
            if self.at != self.text.len() {
                return self.syntax();
            }

            return Ok((Some(value), Next::End));
        };

        let closer = match innermost {
            Open::Array(items) => {
                items.push(value);
                b']'
            }
            Open::Object { pairs, places, key } => {
                let key = std::mem::take(key);
                match places.get(&key).and_then(|place| pairs.get_mut(*place)) {
                    Some((_, held)) => *held = value,
                    None => {
                        places.insert(key.clone(), pairs.len());
                        pairs.push((key, value));
                    }
                }
                b'}'
            }
        };

        if self.peek() == Some(b',') {
            self.at += 1;
            self.skip_space();
            if let Some(Open::Object { key, .. }) = open.last_mut() {
                *key = self.key()?;
            }

            return Ok((None, Next::Value));
        }

        self.expect(closer)?;
        let closed = match open.pop() {
            Some(Open::Array(items)) => Json::Array(items),
            Some(Open::Object { pairs, .. }) => Json::Object(Object { pairs }),
            None => return self.syntax(),
        };

        Ok((Some(closed), Next::Value))
    }

    /// One string. The reader is at its first quote.
    fn string(&mut self) -> Result<String, JsonError> {
        self.at += 1;
        let mut out = String::new();
        loop {
            let start = self.at;
            while !matches!(self.peek(), None | Some(b'"' | b'\\' | 0..FIRST_PRINTABLE)) {
                self.at += 1;
            }

            out.push_str(self.text.get(start..self.at).unwrap_or_default());
            match self.peek() {
                Some(b'"') => {
                    self.at += 1;

                    return Ok(out);
                }
                Some(b'\\') => out.push(self.escape()?),
                _ => return self.syntax(),
            }
        }
    }

    /// One escape. The reader is at its backslash.
    fn escape(&mut self) -> Result<char, JsonError> {
        let start = self.at;
        self.at += 1;
        let Some(letter) = self.peek() else {
            return self.syntax();
        };

        self.at += 1;
        let plain = match letter {
            b'"' => '"',
            b'\\' => '\\',
            b'/' => '/',
            b'b' => '\u{8}',
            b'f' => '\u{c}',
            b'n' => '\n',
            b'r' => '\r',
            b't' => '\t',
            b'u' => return self.unicode_escape(start),
            _ => {
                self.at = start;

                return self.syntax();
            }
        };

        Ok(plain)
    }

    /// The character of a `\u` escape, or of two that make a surrogate pair.
    /// The reader is after the `u`. `start` is the offset of the backslash.
    fn unicode_escape(&mut self, start: usize) -> Result<char, JsonError> {
        let lone = Err(JsonError::LoneSurrogate { at: start });
        let unit = self.hex_unit()?;
        if !(HIGH_SURROGATE_FIRST..=LOW_SURROGATE_LAST).contains(&unit) {
            return char::from_u32(unit).ok_or(JsonError::Syntax { at: start });
        }

        if unit >= LOW_SURROGATE_FIRST || !self.take("\\u") {
            return lone;
        }

        let low = self.hex_unit()?;
        if !(LOW_SURROGATE_FIRST..=LOW_SURROGATE_LAST).contains(&low) {
            return lone;
        }

        let high_bits = (unit - HIGH_SURROGATE_FIRST) << SURROGATE_BITS;
        let point = ASTRAL_FIRST + high_bits + (low - LOW_SURROGATE_FIRST);

        char::from_u32(point).ok_or(JsonError::Syntax { at: start })
    }

    /// The four hex digits of a `\u` escape.
    fn hex_unit(&mut self) -> Result<u32, JsonError> {
        let mut unit = 0_u32;
        for _ in 0..ESCAPE_DIGITS {
            let digit = self.peek().map(char::from).and_then(|hex| hex.to_digit(16));
            let Some(digit) = digit else {
                return self.syntax();
            };

            unit = unit * 16 + digit;
            self.at += 1;
        }

        Ok(unit)
    }

    fn skip_digits(&mut self) -> usize {
        let start = self.at;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.at += 1;
        }

        self.at - start
    }

    /// One number, or `-Infinity`. A fraction or an exponent that is not
    /// whole is not part of the number: the text after the number then breaks
    /// the grammar.
    fn number(&mut self) -> Result<Json, JsonError> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
            if self.take("Infinity") {
                return Ok(Json::Float(f64::NEG_INFINITY));
            }
        }

        let digits_at = self.at;
        if self.peek() == Some(b'0') {
            self.at += 1;
        } else if self.skip_digits() == 0 {
            self.at = start;

            return self.syntax();
        }

        let digits = self.at - digits_at;
        let mut float = false;
        let whole = self.at;
        if self.peek() == Some(b'.') {
            self.at += 1;
            if self.skip_digits() == 0 {
                self.at = whole;
            } else {
                float = true;
            }
        }

        let mantissa = self.at;
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }

            if self.skip_digits() == 0 {
                self.at = mantissa;
            } else {
                float = true;
            }
        }

        let text = self.text.get(start..self.at).unwrap_or_default();
        if float {
            return match text.parse() {
                Ok(float) => Ok(Json::Float(float)),
                Err(_) => Err(JsonError::Syntax { at: start }),
            };
        }

        if digits > INTEGER_DIGITS_MAX {
            return Err(JsonError::IntegerTooLong { at: digits_at });
        }

        match Integer::read(text) {
            Ok(integer) => Ok(Json::Integer(integer)),
            Err(_) => Err(JsonError::Syntax { at: start }),
        }
    }
}

// --- the writer ---

/// Where `json.dumps` puts white space.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Layout {
    /// `indent=2`: one item on each line. `caregiver` writes the status
    /// document in this layout.
    Indented,
    /// `separators=(",", ":")`: no white space. `attendance` writes its fault
    /// file in this layout.
    Compact,
    /// The default: a space after each comma and each colon. The chaperone
    /// writes its fault file in this layout.
    Spaced,
}

/// Which characters `json.dumps` writes as a `\u` escape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Charset {
    /// `ensure_ascii=True`: each character outside the printable ASCII range.
    Ascii,
    /// `ensure_ascii=False`: each control character below U+0020 and no other.
    Unicode,
}

/// The white space of one indent level.
const INDENT: &str = "  ";

/// The last character that `ensure_ascii=True` writes as it is.
const LAST_PRINTABLE: char = '~';

/// The largest position of the decimal point at which `repr` of Python writes
/// a float with no exponent.
const FIXED_POINT_MAX: i32 = 16;

/// The smallest position of the decimal point at which `repr` of Python
/// writes a float with no exponent.
const FIXED_POINT_MIN: i32 = -3;

struct Encoder {
    layout: Layout,
    charset: Charset,
}

impl Encoder {
    fn value(&self, value: &Json, depth: usize, out: &mut String) {
        match value {
            Json::Null => out.push_str("null"),
            Json::Bool(true) => out.push_str("true"),
            Json::Bool(false) => out.push_str("false"),
            Json::Integer(integer) => out.push_str(&integer.to_string()),
            Json::Float(float) => out.push_str(&float_text(*float)),
            Json::String(text) => self.string(text, out),
            Json::Array(items) => self.array(items, depth, out),
            Json::Object(object) => self.object(object, depth, out),
        }
    }

    /// What stands before an item: nothing, or a new line and the indent.
    fn line(&self, depth: usize, out: &mut String) {
        if self.layout != Layout::Indented {
            return;
        }

        out.push('\n');
        for _ in 0..depth {
            out.push_str(INDENT);
        }
    }

    fn comma(&self, out: &mut String) {
        out.push_str(match self.layout {
            Layout::Indented | Layout::Compact => ",",
            Layout::Spaced => ", ",
        });
    }

    fn array(&self, items: &[Json], depth: usize, out: &mut String) {
        if items.is_empty() {
            out.push_str("[]");

            return;
        }

        out.push('[');
        for (place, item) in items.iter().enumerate() {
            if place > 0 {
                self.comma(out);
            }

            self.line(depth + 1, out);
            self.value(item, depth + 1, out);
        }

        self.line(depth, out);
        out.push(']');
    }

    fn object(&self, object: &Object, depth: usize, out: &mut String) {
        if object.is_empty() {
            out.push_str("{}");

            return;
        }

        out.push('{');
        for (place, (key, value)) in object.iter().enumerate() {
            if place > 0 {
                self.comma(out);
            }

            self.line(depth + 1, out);
            self.string(key, out);
            out.push_str(match self.layout {
                Layout::Indented | Layout::Spaced => ": ",
                Layout::Compact => ":",
            });
            self.value(value, depth + 1, out);
        }

        self.line(depth, out);
        out.push('}');
    }

    fn string(&self, text: &str, out: &mut String) {
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
                _ if self.is_plain(character) => out.push(character),
                _ => {
                    let mut units = [0_u16; 2];
                    for unit in character.encode_utf16(&mut units) {
                        out.push_str(&format!("\\u{unit:04x}"));
                    }
                }
            }
        }

        out.push('"');
    }

    /// Whether the writer puts `character` in a string as it is.
    fn is_plain(&self, character: char) -> bool {
        match self.charset {
            Charset::Ascii => (' '..=LAST_PRINTABLE).contains(&character),
            Charset::Unicode => character >= ' ',
        }
    }
}

/// The text of a float, as `json.dumps` of Python writes it: `repr` of the
/// float, or one of the three words.
fn float_text(float: f64) -> String {
    if float.is_nan() {
        return "NaN".to_owned();
    }

    if float.is_infinite() {
        let word = if float > 0.0 { "Infinity" } else { "-Infinity" };

        return word.to_owned();
    }

    // `{:e}` gives the shortest digits that read back as the same float, as
    // `repr` does: `1.25e0`, `-5e-324`.
    let scientific = format!("{float:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let exponent: i32 = exponent.parse().unwrap_or_default();
    let digits = tie_to_even(float.abs(), digits, exponent);
    // The position of the decimal point, counted from the first digit.
    let point = exponent + 1;
    if !(FIXED_POINT_MIN..=FIXED_POINT_MAX).contains(&point) {
        return format!("{sign}{}", with_exponent(&digits, exponent));
    }

    format!("{sign}{}", with_fixed_point(&digits, point))
}

/// More digits than the exact decimal form of a float has. The longest form
/// has 767 digits.
const EXACT_DIGITS: usize = 800;

/// Each digit of the exact decimal form of `float`, with no zero at the end.
fn exact_digits(float: f64) -> String {
    let exact = format!("{float:.EXACT_DIGITS$e}");
    let mantissa = exact
        .split_once('e')
        .map_or(exact.as_str(), |(mantissa, _)| mantissa);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();

    digits.trim_end_matches('0').to_owned()
}

/// Whether `digits` with the decimal point after the first digit, times ten
/// to the power of `exponent`, reads as `float`.
fn reads_as(digits: &str, exponent: i32, float: f64) -> bool {
    let (first, rest) = digits.split_at_checked(1).unwrap_or((digits, ""));

    format!("{first}.{rest}e{exponent}").parse() == Ok(float)
}

/// The shortest digits of `float` that `repr` of Python gives, from the
/// shortest digits that `{:e}` of Rust gives.
///
/// The two differ in one case. A float can be exactly in the middle of two
/// shortest candidates. Rust then takes the upper candidate, and Python
/// takes the candidate with an even last digit.
fn tie_to_even(float: f64, digits: String, exponent: i32) -> String {
    let Some((head, last)) = digits.split_at_checked(digits.len().saturating_sub(1)) else {
        return digits;
    };
    let Some(last) = last.chars().next().and_then(|digit| digit.to_digit(10)) else {
        return digits;
    };
    if last % 2 == 0 {
        return digits;
    }

    // An odd digit is 1 or more, so the candidate below needs no borrow.
    let below = format!("{head}{}", last - 1);
    if reads_as(&below, exponent, float) && exact_digits(float) == format!("{below}5") {
        return below;
    }

    digits
}

/// `digits` with the decimal point `point` places after the first digit's
/// left side, and no exponent.
fn with_fixed_point(digits: &str, point: i32) -> String {
    let count = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    if point <= 0 {
        let zeros = "0".repeat(usize::try_from(-point).unwrap_or_default());

        return format!("0.{zeros}{digits}");
    }

    if point >= count {
        let zeros = "0".repeat(usize::try_from(point - count).unwrap_or_default());

        return format!("{digits}{zeros}.0");
    }

    let (whole, fraction) = digits
        .split_at_checked(usize::try_from(point).unwrap_or_default())
        .unwrap_or((digits, ""));

    format!("{whole}.{fraction}")
}

/// `digits` with the decimal point after the first digit, and an exponent of
/// two digits or more.
fn with_exponent(digits: &str, exponent: i32) -> String {
    let (first, rest) = digits.split_at_checked(1).unwrap_or((digits, ""));
    let fraction = if rest.is_empty() {
        String::new()
    } else {
        format!(".{rest}")
    };
    let sign = if exponent < 0 { '-' } else { '+' };

    format!("{first}{fraction}e{sign}{:02}", exponent.unsigned_abs())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn integer(text: &str) -> Json {
        Json::Integer(text.parse().unwrap())
    }

    fn string(text: &str) -> Json {
        Json::String(text.to_owned())
    }

    fn object(pairs: &[(&str, Json)]) -> Json {
        Json::Object(
            pairs
                .iter()
                .map(|(key, value)| ((*key).to_owned(), value.clone()))
                .collect(),
        )
    }

    /// 4300 digits: the longest integer.
    fn longest_integer() -> String {
        "9".repeat(INTEGER_DIGITS_MAX)
    }

    #[test]
    fn a_text_that_python_reads_gives_the_same_value() {
        let longest = longest_integer();
        let accepted: Vec<(&str, Json)> = vec![
            ("null", Json::Null),
            ("true", Json::Bool(true)),
            ("false", Json::Bool(false)),
            ("0", integer("0")),
            ("-0", integer("0")),
            ("-5", integer("-5")),
            ("1180591620717411303424", integer("1180591620717411303424")),
            (&longest, integer(&longest)),
            ("-0.0", Json::Float(-0.0)),
            ("1E5", Json::Float(100_000.0)),
            ("1.5e-3", Json::Float(0.0015)),
            ("1.5E+3", Json::Float(1500.0)),
            ("1e999", Json::Float(f64::INFINITY)),
            ("-1e999", Json::Float(f64::NEG_INFINITY)),
            ("1.0e400", Json::Float(f64::INFINITY)),
            ("Infinity", Json::Float(f64::INFINITY)),
            ("-Infinity", Json::Float(f64::NEG_INFINITY)),
            (" \t\n\r1 \t\n\r", integer("1")),
            (r#""\/""#, string("/")),
            (r#""\b\f\n\r\t\"\\""#, string("\u{8}\u{c}\n\r\t\"\\")),
            (r#""\u00e9\u00E9""#, string("\u{e9}\u{e9}")),
            (r#""\ud83d\ude00""#, string("\u{1f600}")),
            (r#""\uD83D\uDE00""#, string("\u{1f600}")),
            ("\"a\u{7f}b\"", string("a\u{7f}b")),
            ("\"\u{e9}\u{1f600}\"", string("\u{e9}\u{1f600}")),
            ("[]", Json::Array(Vec::new())),
            ("[ ]", Json::Array(Vec::new())),
            ("{}", Json::Object(Object::new())),
            ("{ }", Json::Object(Object::new())),
            (
                "[1, [2]]",
                Json::Array(vec![integer("1"), Json::Array(vec![integer("2")])]),
            ),
            (
                r#"{"a": [Infinity], "b": {"c": null}}"#,
                object(&[
                    ("a", Json::Array(vec![Json::Float(f64::INFINITY)])),
                    ("b", object(&[("c", Json::Null)])),
                ]),
            ),
            (r#"{"": 1}"#, object(&[("", integer("1"))])),
        ];

        for (text, value) in accepted {
            assert_eq!(Json::parse(text).unwrap(), value, "{text}");
        }
    }

    #[test]
    fn the_word_nan_reads_as_a_float_that_is_not_a_number() {
        let Json::Float(float) = Json::parse("NaN").unwrap() else {
            panic!("NaN is a float");
        };

        assert!(float.is_nan());
    }

    #[test]
    fn a_key_that_an_object_holds_two_times_keeps_its_place_and_the_last_value() {
        let read = Json::parse(r#"{"a":1,"b":2,"a":3}"#).unwrap();

        assert_eq!(read, object(&[("a", integer("3")), ("b", integer("2"))]));
    }

    #[test]
    fn a_text_that_python_refuses_is_refused() {
        let refused: &[(&str, usize)] = &[
            ("", 0),
            (" ", 1),
            ("nan", 0),
            ("-NaN", 0),
            ("+1", 0),
            (".5", 0),
            ("1.", 1),
            ("01", 1),
            ("-01", 2),
            ("1e", 1),
            ("1e+", 1),
            ("1e5x", 3),
            ("-", 0),
            ("--1", 0),
            ("0x10", 1),
            ("1_000", 1),
            ("\u{661}", 0),
            ("[1,]", 3),
            (r#"{"a":1,}"#, 7),
            ("[1 2]", 3),
            (r#"{"a":1 "b":2}"#, 7),
            ("[", 1),
            ("]", 0),
            ("{1:2}", 1),
            ("{'a':1}", 1),
            (r#"{"a" 1}"#, 5),
            (r#"{"a":}"#, 5),
            (r#"{"a":1}{"#, 7),
            ("\"", 1),
            ("\"a\tb\"", 2),
            (r#""\x41""#, 1),
            (r#""\a""#, 1),
            (r#""\U0041""#, 1),
            (r#""\u00""#, 5),
            (r#""\u00zz""#, 5),
            (r#""\u+123""#, 3),
            (r#""\u 123""#, 3),
            ("tru", 0),
            ("true1", 4),
            ("nul", 0),
            ("Infinit", 0),
            ("I", 0),
            ("N", 0),
            ("\u{c}1", 0),
            ("\u{b}1", 0),
            ("\u{a0}1", 0),
            ("1\u{a0}", 1),
            ("1 /*c*/", 2),
            ("//x\n1", 0),
            ("null x", 5),
        ];

        for (text, at) in refused {
            assert_eq!(
                Json::parse(text),
                Err(JsonError::Syntax { at: *at }),
                "{text}"
            );
        }
    }

    #[test]
    fn an_integer_of_4301_digits_is_refused() {
        let text = format!("[{}9]", longest_integer());

        assert_eq!(Json::parse(&text), Err(JsonError::IntegerTooLong { at: 1 }));
        assert_eq!(
            format!("9{}", longest_integer()).parse::<Integer>(),
            Err(IntegerError::TooLong)
        );
    }

    #[test]
    fn a_float_of_any_count_of_digits_reads() {
        let text = format!("{}.5", longest_integer().repeat(2));

        assert_eq!(Json::parse(&text), Ok(Json::Float(f64::INFINITY)));
    }

    #[test]
    fn half_a_surrogate_pair_is_refused() {
        for text in [
            r#""\ud800""#,
            r#""\udc00""#,
            r#""\ud800A""#,
            r#""\ud800\u0041""#,
            r#""\ude00\ud83d""#,
        ] {
            assert_eq!(
                Json::parse(text),
                Err(JsonError::LoneSurrogate { at: 1 }),
                "{text}"
            );
        }
    }

    #[test]
    fn a_byte_order_mark_is_refused_or_skipped_one_time() {
        let marked = "\u{feff}{}".as_bytes();
        let twice = "\u{feff}\u{feff}{}".as_bytes();
        let empty = Json::Object(Object::new());

        assert_eq!(Json::parse("\u{feff}{}"), Err(JsonError::ByteOrderMark));
        assert_eq!(
            Json::parse_bytes(marked, ByteOrderMark::Refuse),
            Err(JsonError::ByteOrderMark)
        );
        assert_eq!(Json::parse_bytes(marked, ByteOrderMark::Skip), Ok(empty));
        assert_eq!(
            Json::parse_bytes(twice, ByteOrderMark::Skip),
            Err(JsonError::ByteOrderMark)
        );
    }

    #[test]
    fn bytes_that_are_not_utf8_are_refused() {
        for bytes in [
            &b"\"\xff\""[..],
            b"\"\xed\xa0\x80\"",
            b"{\0\"\0a\0\"\0:\x001\0}\0",
        ] {
            let read = Json::parse_bytes(bytes, ByteOrderMark::Skip);

            assert!(
                matches!(read, Err(JsonError::NotUtf8 | JsonError::Syntax { .. })),
                "{bytes:?}"
            );
        }
    }

    #[test]
    fn the_nesting_has_a_cap() {
        let arrays = |levels: usize| format!("{}{}", "[".repeat(levels), "]".repeat(levels));
        let objects =
            |levels: usize| format!("{}1{}", r#"{"a":"#.repeat(levels), "}".repeat(levels));

        assert!(Json::parse(&arrays(DEPTH_MAX)).is_ok());
        assert!(Json::parse(&objects(DEPTH_MAX)).is_ok());
        assert_eq!(
            Json::parse(&arrays(DEPTH_MAX + 1)),
            Err(JsonError::TooDeep { at: DEPTH_MAX })
        );
        assert!(matches!(
            Json::parse(&objects(DEPTH_MAX + 1)),
            Err(JsonError::TooDeep { .. })
        ));
        assert!(matches!(
            Json::parse(&"[".repeat(400_000)),
            Err(JsonError::TooDeep { .. })
        ));
    }

    #[test]
    fn a_value_of_the_full_depth_fits_a_small_stack() {
        // The stack of the thread that does the work. A spawned thread has
        // 2 MiB when no code asks for another size.
        const STACK_BYTES: usize = 1 << 20;
        let arrays = format!("{}{}", "[".repeat(DEPTH_MAX), "]".repeat(DEPTH_MAX));
        let objects = format!("{}1{}", r#"{"a":"#.repeat(DEPTH_MAX), "}".repeat(DEPTH_MAX));

        for text in [arrays, objects] {
            let work = move || {
                let value = Json::parse(&text).unwrap();
                let copy = value.clone();
                let shown = format!("{copy:?}");
                let written = copy.encode(Layout::Compact, Charset::Ascii);

                assert_eq!(copy, value);
                assert_eq!(written, text);

                shown.len()
            };
            let thread = std::thread::Builder::new()
                .stack_size(STACK_BYTES)
                .spawn(work)
                .unwrap();

            assert!(thread.join().unwrap() > DEPTH_MAX);
        }
    }

    #[test]
    fn an_integer_has_one_form() {
        let accepted = [
            ("0", "0"),
            ("-0", "0"),
            ("7", "7"),
            ("-7", "-7"),
            ("18446744073709551616", "18446744073709551616"),
        ];
        let refused = [
            ("", IntegerError::NotDigits),
            ("-", IntegerError::NotDigits),
            ("+1", IntegerError::NotDigits),
            ("1.0", IntegerError::NotDigits),
            ("1\n", IntegerError::NotDigits),
            ("\u{661}", IntegerError::NotDigits),
            ("01", IntegerError::LeadingZero),
            ("-00", IntegerError::LeadingZero),
        ];

        for (text, form) in accepted {
            assert_eq!(text.parse::<Integer>().unwrap().to_string(), form, "{text}");
        }

        for (text, error) in refused {
            assert_eq!(text.parse::<Integer>(), Err(error), "{text:?}");
        }
    }

    #[test]
    fn an_integer_converts_only_when_it_fits() {
        let big: Integer = "18446744073709551616".parse().unwrap();
        let negative: Integer = "-9223372036854775808".parse().unwrap();
        let past_float: Integer = format!("1{}", "0".repeat(400)).parse().unwrap();

        assert_eq!(big.as_u64(), None);
        assert_eq!(big.as_i64(), None);
        assert_eq!(big.to_f64(), Some(18_446_744_073_709_551_616.0));
        assert_eq!(negative.as_u64(), None);
        assert_eq!(negative.as_i64(), Some(i64::MIN));
        assert!(negative.is_negative());
        assert_eq!(past_float.to_f64(), None);
        assert_eq!(Integer::from(u64::MAX).as_u64(), Some(u64::MAX));
    }

    #[test]
    fn the_error_is_a_std_error() {
        let boxed: Box<dyn Error> = Box::new(JsonError::Syntax { at: 3 });
        let integer: Box<dyn Error> = Box::new(IntegerError::TooLong);

        assert_eq!(
            boxed.to_string(),
            "the byte at offset 3 breaks the JSON grammar"
        );
        assert_eq!(integer.to_string(), "an integer has 4300 digits at most");
    }

    #[test]
    fn a_float_has_the_text_that_python_gives_it() {
        let floats: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.25, "1.25"),
            (10.0, "10.0"),
            (100.0, "100.0"),
            (0.1, "0.1"),
            (0.21, "0.21"),
            (1.0 / 3.0, "0.3333333333333333"),
            (123_456_789.0, "123456789.0"),
            (1e15, "1000000000000000.0"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1.234_567_890_123_456_8e17, "1.2345678901234568e+17"),
            (1e21, "1e+21"),
            (1e22, "1e+22"),
            (1e100, "1e+100"),
            (1.5e300, "1.5e+300"),
            (f64::MAX, "1.7976931348623157e+308"),
            (0.001, "0.001"),
            (0.0001, "0.0001"),
            (0.000_123, "0.000123"),
            (1e-5, "1e-05"),
            (2.5e-7, "2.5e-07"),
            (3e-10, "3e-10"),
            (5e-324, "5e-324"),
            (-1.5e-7, "-1.5e-07"),
            (-123.456, "-123.456"),
            (f64::NAN, "NaN"),
            (f64::INFINITY, "Infinity"),
            (f64::NEG_INFINITY, "-Infinity"),
        ];

        for (float, text) in floats {
            assert_eq!(float_text(*float), *text);
        }
    }

    #[test]
    fn a_float_in_the_middle_of_two_shortest_texts_takes_the_even_digit() {
        // Each float is exactly a quarter or an eighth past its text. `{:e}`
        // of Rust gives the digit above.
        let ties = [
            "1673733205695233.2",
            "-146656950681247.62",
            "107649869691857.12",
            "-1840449785444225.2",
            "1679558812176105.2",
            "1125899906842624.2",
            "123456.00024414062",
        ];

        for text in ties {
            let float: f64 = text.parse().unwrap();

            assert_eq!(float_text(float), text);
            assert_ne!(format!("{float}"), text);
        }
    }

    #[test]
    fn the_exact_digits_of_a_float_have_no_zero_at_the_end() {
        assert_eq!(exact_digits(0.5), "5");
        assert_eq!(exact_digits(1.0), "1");
        assert_eq!(
            exact_digits(0.1),
            "1000000000000000055511151231257827021181583404541015625"
        );
        assert_eq!(exact_digits(2_f64.powi(50) + 0.25), "112589990684262425");
        assert_eq!(exact_digits(5e-324).len(), 751);
        assert_eq!(exact_digits(f64::MAX).len(), 309);
    }

    /// A value with each type in it, and each empty form.
    fn sample() -> Json {
        object(&[
            ("a", Json::Array(Vec::new())),
            ("b", Json::Object(Object::new())),
            (
                "c",
                Json::Array(vec![
                    integer("1"),
                    Json::Array(vec![Json::Float(2.5), Json::Object(Object::new())]),
                    object(&[("d", Json::Array(Vec::new()))]),
                ]),
            ),
            ("e", Json::Null),
            ("f", Json::Bool(true)),
        ])
    }

    #[test]
    fn each_layout_puts_the_white_space_where_python_puts_it() {
        let indented = "{\n  \"a\": [],\n  \"b\": {},\n  \"c\": [\n    1,\n    [\n      2.5,\n      \
                        {}\n    ],\n    {\n      \"d\": []\n    }\n  ],\n  \"e\": null,\n  \"f\": \
                        true\n}";
        let compact = r#"{"a":[],"b":{},"c":[1,[2.5,{}],{"d":[]}],"e":null,"f":true}"#;
        let spaced = r#"{"a": [], "b": {}, "c": [1, [2.5, {}], {"d": []}], "e": null, "f": true}"#;

        assert_eq!(sample().encode(Layout::Indented, Charset::Ascii), indented);
        assert_eq!(sample().encode(Layout::Compact, Charset::Ascii), compact);
        assert_eq!(sample().encode(Layout::Spaced, Charset::Ascii), spaced);
    }

    #[test]
    fn each_charset_escapes_what_python_escapes() {
        let text =
            string("caf\u{e9} \u{1f600} \u{2028} \"q\" b\\s a/b \n\r\t\u{8}\u{c} \0\u{1f} \u{7f}");
        let ascii =
            r#""caf\u00e9 \ud83d\ude00 \u2028 \"q\" b\\s a/b \n\r\t\b\f \u0000\u001f \u007f""#;
        let unicode = "\"caf\u{e9} \u{1f600} \u{2028} \\\"q\\\" b\\\\s a/b \\n\\r\\t\\b\\f \
                       \\u0000\\u001f \u{7f}\"";

        assert_eq!(text.encode(Layout::Compact, Charset::Ascii), ascii);
        assert_eq!(text.encode(Layout::Compact, Charset::Unicode), unicode);
    }

    #[test]
    fn a_text_that_the_writer_makes_reads_back_as_the_same_value() {
        for layout in [Layout::Indented, Layout::Compact, Layout::Spaced] {
            for charset in [Charset::Ascii, Charset::Unicode] {
                let text = sample().encode(layout, charset);

                assert_eq!(Json::parse(&text), Ok(sample()), "{text}");
            }
        }
    }

    #[test]
    fn an_object_keeps_the_place_of_a_key_that_is_set_again() {
        let mut object = Object::new();
        object.insert("a", Json::Null);
        object.insert("b", Json::Null);
        object.insert("a", Json::Bool(true));
        let keys: Vec<&str> = object.iter().map(|(key, _)| key).collect();

        assert_eq!(keys, ["a", "b"]);
        assert_eq!(object.get("a"), Some(&Json::Bool(true)));
        assert_eq!(object.get("c"), None);
        assert_eq!(object.len(), 2);
        assert!(!object.is_empty());
    }

    #[test]
    fn a_value_names_its_type() {
        let kinds = [
            (Json::Null, JsonKind::Null),
            (Json::Bool(false), JsonKind::Bool),
            (integer("1"), JsonKind::Integer),
            (Json::Float(1.0), JsonKind::Float),
            (string("a"), JsonKind::String),
            (Json::Array(Vec::new()), JsonKind::Array),
            (Json::Object(Object::new()), JsonKind::Object),
        ];

        for (value, kind) in kinds {
            assert_eq!(value.kind(), kind);
        }

        assert_eq!(string("a").as_str(), Some("a"));
        assert_eq!(Json::Null.as_str(), None);
    }
}
