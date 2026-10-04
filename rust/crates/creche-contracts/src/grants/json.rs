//! A JSON reader and a JSON writer that do what the Python code does.
//!
//! The chaperone and the caregiver read and write JSON with the `json` module
//! of Python. That module is not strict JSON (RFC 8259). The reader here takes
//! each text that the Python reader takes, with two exceptions that the
//! `CONTRACT-QUESTION` comments below give. The writer here writes the bytes
//! that the Python writer writes.
//!
//! What the Python reader takes and strict JSON does not:
//!
//! - `NaN`, `Infinity` and `-Infinity` as numbers.
//! - An integer of each size, up to 4300 digits.
//! - A number that does not fit a float. It reads as an infinity.
//! - One key more than one time in an object. The last value stays, at the
//!   place of the first.
//! - For a request body: UTF-16 and UTF-32, and UTF-8 with a byte order mark.
//!
//! `serde_json` refuses each of these or loses the difference, so this module
//! has its own reader. The differential test in `python.rs` holds the reader
//! equal to the vectors.

use std::collections::HashMap;
use std::fmt;

// CONTRACT-QUESTION: contract 04 gives no cap on the nesting of a grant file or
// of a request body. The Python reader stops at the recursion limit of its
// interpreter, which differs between two Python versions: about 10 000 levels
// under Python 3.13. This reader stops at 256 levels. The deepest vector that
// the Python code accepts has 202 levels. A change to a larger cap costs the
// one number here, and each function that walks a value by recursion then
// needs more stack.
/// The largest count of lists and maps that nest in one document.
pub(super) const MAX_DEPTH: usize = 256;

/// The largest count of digits in an integer. Python reads no longer text as
/// an integer.
pub(super) const INT_MAX_DIGITS: usize = 4300;

// CONTRACT-QUESTION: contract 04 does not say which characters a string of a
// grant file or of a request body can hold. The Python reader keeps a lone
// surrogate, from a `\u` escape or from a body that is not valid UTF-8. Such a
// string is not Unicode text, and it has no UTF-8 form. This reader refuses
// the document. A change to accept such a string costs a string type that is
// not `String` in `Value` and in each type that holds text of a document.
/// What stands for a character that is not Unicode text while the reader goes
/// on. The reader then refuses the document.
const REPLACEMENT: char = char::REPLACEMENT_CHARACTER;

// --- the value ---

/// One JSON value, as the Python code holds it.
///
/// A value keeps the difference between an integer and a float, as Python
/// does: `1` is an [`Integer`] and `1.0` is a float. A float can be a NaN or
/// an infinity, because the Python reader takes `NaN` and `Infinity`.
///
/// Two values are equal when they hold the same data. Two floats are equal
/// when they have the same bits, so a NaN is equal to itself and `0.0` is not
/// equal to `-0.0`.
#[derive(Debug, Clone)]
pub enum Value {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// A number with no fraction and no exponent, of each size.
    Integer(Integer),
    /// A number with a fraction or an exponent, or `NaN`, or an infinity.
    Float(f64),
    /// A string. It is Unicode text: it holds no lone surrogate.
    Text(String),
    /// An array.
    List(Vec<Value>),
    /// An object.
    Map(Map),
}

impl Value {
    /// The text of a string. `None` for each other value.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Text(text) => Some(text),
            _ => None,
        }
    }

    /// The entries of an object. `None` for each other value.
    #[must_use]
    pub fn as_map(&self) -> Option<&Map> {
        match self {
            Self::Map(map) => Some(map),
            _ => None,
        }
    }

    /// The items of an array. `None` for each other value.
    #[must_use]
    pub fn as_list(&self) -> Option<&[Value]> {
        match self {
            Self::List(items) => Some(items),
            _ => None,
        }
    }
}

impl PartialEq for Value {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Null, Self::Null) => true,
            (Self::Bool(left), Self::Bool(right)) => left == right,
            (Self::Integer(left), Self::Integer(right)) => left == right,
            (Self::Float(left), Self::Float(right)) => left.to_bits() == right.to_bits(),
            (Self::Text(left), Self::Text(right)) => left == right,
            (Self::List(left), Self::List(right)) => left == right,
            (Self::Map(left), Self::Map(right)) => left == right,
            _ => false,
        }
    }
}

impl Eq for Value {}

/// An integer of each size, as a JSON document holds it.
///
/// Python has no largest integer, and a grant file or a call can hold an
/// integer past 64 bits. The type keeps the sign and the decimal digits.
///
/// ```
/// use creche_contracts::grants::Integer;
///
/// let small = Integer::from(60_u64);
/// assert_eq!(small.to_u64(), Some(60));
/// assert_eq!(small.to_string(), "60");
/// ```
///
/// Code outside this module cannot build a value from raw digits:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::Integer;
///
/// let number = Integer { negative: false, digits: String::from("007") };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Integer {
    negative: bool,
    /// The decimal digits. No zero at the start, but for the number zero,
    /// which is `0` and is not negative.
    digits: String,
}

/// The sign of an integer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Sign {
    Plus,
    Minus,
}

impl Integer {
    /// Makes the integer of a sign and a run of ASCII digits. The caller made
    /// sure that `digits` holds one digit or more and nothing else.
    pub(super) fn from_digits(sign: Sign, digits: &str) -> Self {
        let significant = digits.trim_start_matches('0');
        if significant.is_empty() {
            return Self {
                negative: false,
                digits: "0".to_owned(),
            };
        }

        Self {
            negative: sign == Sign::Minus,
            digits: significant.to_owned(),
        }
    }

    /// The integer, when it is 0 to 2^64 - 1.
    #[must_use]
    pub fn to_u64(&self) -> Option<u64> {
        if self.negative {
            return None;
        }

        self.digits.parse().ok()
    }

    /// The integer, when it is -2^63 to 2^63 - 1.
    #[must_use]
    pub fn to_i64(&self) -> Option<i64> {
        self.to_string().parse().ok()
    }

    /// Whether the integer is below zero.
    #[must_use]
    pub fn is_negative(&self) -> bool {
        self.negative
    }

    /// Whether the integer is 1 or more.
    #[must_use]
    pub fn is_positive(&self) -> bool {
        !self.negative && self.digits != "0"
    }
}

impl From<u64> for Integer {
    fn from(number: u64) -> Self {
        Self {
            negative: false,
            digits: number.to_string(),
        }
    }
}

impl From<i64> for Integer {
    fn from(number: i64) -> Self {
        Self {
            negative: number < 0,
            digits: number.unsigned_abs().to_string(),
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

/// The entries of one JSON object: each key one time, in the order in which
/// the keys first came.
///
/// ```
/// use creche_contracts::grants::{Map, Value};
///
/// let mut map = Map::new();
/// map.insert("b", Value::Bool(true));
/// map.insert("a", Value::Null);
/// map.insert("b", Value::Bool(false));
///
/// let keys: Vec<&str> = map.iter().map(|(key, _)| key).collect();
/// assert_eq!(keys, ["b", "a"]);
/// assert_eq!(map.get("b"), Some(&Value::Bool(false)));
/// ```
///
/// Code outside this module cannot build a map that holds one key two times:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{Map, Value};
///
/// let map = Map { entries: vec![("a".to_owned(), Value::Null), ("a".to_owned(), Value::Null)] };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Map {
    entries: Vec<(String, Value)>,
}

impl Map {
    /// A map with no entry.
    #[must_use]
    pub fn new() -> Self {
        Self {
            entries: Vec::new(),
        }
    }

    /// The value of one key.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.entries
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    }

    /// Each key and its value, in the order in which the keys first came.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Value)> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_str(), value))
    }

    /// The count of entries.
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the map has no entry.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Puts one entry. A key that the map holds keeps its place and takes the
    /// new value, as a Python `dict` does.
    pub fn insert(&mut self, key: &str, value: Value) {
        match self.entries.iter_mut().find(|(name, _)| name == key) {
            Some((_, held)) => *held = value,
            None => self.entries.push((key.to_owned(), value)),
        }
    }

    /// The entries in the order of their keys, as Python sorts text: by code
    /// point.
    fn sorted(&self) -> Vec<(&str, &Value)> {
        let mut entries: Vec<(&str, &Value)> = self.iter().collect();
        entries.sort_by(|(left, _), (right, _)| left.cmp(right));

        entries
    }
}

impl Default for Map {
    fn default() -> Self {
        Self::new()
    }
}

/// A map while the reader fills it. It finds a key again with one lookup, so
/// an object with many keys costs no more than its size.
struct MapBuilder {
    entries: Vec<(String, Value)>,
    places: HashMap<String, usize>,
}

impl MapBuilder {
    fn new() -> Self {
        Self {
            entries: Vec::new(),
            places: HashMap::new(),
        }
    }

    fn insert(&mut self, key: String, value: Value) {
        let held = self
            .places
            .get(&key)
            .and_then(|place| self.entries.get_mut(*place));
        if let Some((_, held)) = held {
            *held = value;
            return;
        }

        self.places.insert(key.clone(), self.entries.len());
        self.entries.push((key, value));
    }

    fn finish(self) -> Map {
        Map {
            entries: self.entries,
        }
    }
}

// --- the reader ---

/// Why the reader refuses a document.
///
/// No variant holds text of the document. The document is untrusted, and a
/// caller writes this error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JsonError {
    /// The bytes are not text in the encoding that the document has.
    NotText,
    /// The text is not JSON as the Python reader takes it. `at` is the count
    /// of characters before the fault.
    Syntax {
        /// The count of characters before the fault.
        at: usize,
    },
    /// The document nests deeper than the reader goes.
    TooDeep,
    /// An integer has more digits than Python reads.
    LongInteger,
    /// A string holds a lone surrogate, so it is not Unicode text.
    LoneSurrogate,
}

impl fmt::Display for JsonError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotText => f.write_str("the bytes are not text in the encoding of the document"),
            Self::Syntax { at } => write!(f, "the text stops being JSON after {at} characters"),
            Self::TooDeep => write!(f, "the document nests deeper than {MAX_DEPTH} levels"),
            Self::LongInteger => write!(f, "an integer has more than {INT_MAX_DIGITS} digits"),
            Self::LoneSurrogate => f.write_str("a string holds a lone surrogate"),
        }
    }
}

impl std::error::Error for JsonError {}

/// Reads a document that is UTF-8 with no byte order mark: `json.loads` on
/// the text that `bytes.decode("utf-8")` gives. The grant file is such a
/// document.
pub(super) fn read_utf8(bytes: &[u8]) -> Result<Value, JsonError> {
    let text = std::str::from_utf8(bytes).map_err(|_| JsonError::NotText)?;
    let points: Vec<u32> = text.chars().map(u32::from).collect();

    Reader::new(&points).document()
}

/// Reads a request body: `json.loads` on the bytes. Python finds the encoding
/// from the first bytes and takes UTF-8, UTF-16 and UTF-32.
pub(super) fn read_body(bytes: &[u8]) -> Result<Value, JsonError> {
    let points = decode(bytes)?;

    Reader::new(&points).document()
}

/// The order of the bytes of one code unit.
#[derive(Clone, Copy)]
enum Order {
    Big,
    Little,
}

const BOM_UTF8: &[u8] = &[0xef, 0xbb, 0xbf];
const BOM_UTF16_BE: &[u8] = &[0xfe, 0xff];
const BOM_UTF16_LE: &[u8] = &[0xff, 0xfe];
const BOM_UTF32_BE: &[u8] = &[0x00, 0x00, 0xfe, 0xff];
const BOM_UTF32_LE: &[u8] = &[0xff, 0xfe, 0x00, 0x00];

/// The code points of a body, as `json.detect_encoding` and
/// `bytes.decode(encoding, "surrogatepass")` give them. A code point can be a
/// surrogate. The reader refuses one in a string.
fn decode(bytes: &[u8]) -> Result<Vec<u32>, JsonError> {
    if let Some(rest) = bytes.strip_prefix(BOM_UTF32_BE) {
        return decode_utf32(rest, Order::Big);
    }

    if let Some(rest) = bytes.strip_prefix(BOM_UTF32_LE) {
        return decode_utf32(rest, Order::Little);
    }

    if let Some(rest) = bytes.strip_prefix(BOM_UTF16_BE) {
        return decode_utf16(rest, Order::Big);
    }

    if let Some(rest) = bytes.strip_prefix(BOM_UTF16_LE) {
        return decode_utf16(rest, Order::Little);
    }

    if let Some(rest) = bytes.strip_prefix(BOM_UTF8) {
        return decode_utf8(rest);
    }

    // With no byte order mark, a zero byte in the first four bytes shows the
    // encoding: the first character of a JSON document is ASCII.
    match bytes {
        [0, 0, ..] if bytes.len() >= 4 => decode_utf32(bytes, Order::Big),
        [0, _, ..] if bytes.len() >= 4 => decode_utf16(bytes, Order::Big),
        [_, 0, 0, 0, ..] => decode_utf32(bytes, Order::Little),
        [_, 0, _, _, ..] => decode_utf16(bytes, Order::Little),
        [0, _] => decode_utf16(bytes, Order::Big),
        [_, 0] => decode_utf16(bytes, Order::Little),
        _ => decode_utf8(bytes),
    }
}

const SURROGATE_FIRST: u32 = 0xd800;
const SURROGATE_LAST: u32 = 0xdfff;
const HIGH_SURROGATE_LAST: u32 = 0xdbff;
const LOW_SURROGATE_FIRST: u32 = 0xdc00;
const CODE_POINT_LAST: u32 = 0x0010_ffff;

/// What a surrogate pair adds to the bits of its two halves.
const PAIR_BASE: u32 = 0x0001_0000;

/// The count of bits that the low half of a surrogate pair holds.
const PAIR_SHIFT: u32 = 10;

fn is_high_surrogate(point: u32) -> bool {
    (SURROGATE_FIRST..=HIGH_SURROGATE_LAST).contains(&point)
}

fn is_low_surrogate(point: u32) -> bool {
    (LOW_SURROGATE_FIRST..=SURROGATE_LAST).contains(&point)
}

/// The code point of one surrogate pair.
fn join_surrogates(high: u32, low: u32) -> u32 {
    PAIR_BASE + ((high - SURROGATE_FIRST) << PAIR_SHIFT) + (low - LOW_SURROGATE_FIRST)
}

/// UTF-8, and one more form: three bytes that encode a surrogate. Python
/// passes that form with `surrogatepass`.
fn decode_utf8(bytes: &[u8]) -> Result<Vec<u32>, JsonError> {
    let mut points = Vec::with_capacity(bytes.len());
    let mut rest = bytes;
    loop {
        let fault = match std::str::from_utf8(rest) {
            Ok(text) => {
                points.extend(text.chars().map(u32::from));

                return Ok(points);
            }
            Err(fault) => fault,
        };
        let (valid, after) = rest.split_at(fault.valid_up_to());
        let text = std::str::from_utf8(valid).map_err(|_| JsonError::NotText)?;
        points.extend(text.chars().map(u32::from));

        // 0xed 0xa0..=0xbf 0x80..=0xbf is a surrogate in three bytes.
        let [0xed, second @ 0xa0..=0xbf, third @ 0x80..=0xbf, tail @ ..] = after else {
            return Err(JsonError::NotText);
        };
        let point = 0xd000 | (u32::from(*second & 0x3f) << 6) | u32::from(*third & 0x3f);
        points.push(point);
        rest = tail;
    }
}

fn decode_utf16(bytes: &[u8], order: Order) -> Result<Vec<u32>, JsonError> {
    let (pairs, odd) = bytes.as_chunks::<2>();
    if !odd.is_empty() {
        return Err(JsonError::NotText);
    }

    let mut units = pairs.iter().map(|pair| match order {
        Order::Big => u32::from(u16::from_be_bytes(*pair)),
        Order::Little => u32::from(u16::from_le_bytes(*pair)),
    });
    let mut points = Vec::with_capacity(pairs.len());
    let mut held = units.next();
    while let Some(unit) = held {
        held = units.next();
        match held {
            Some(low) if is_high_surrogate(unit) && is_low_surrogate(low) => {
                points.push(join_surrogates(unit, low));
                held = units.next();
            }
            _ => points.push(unit),
        }
    }

    Ok(points)
}

fn decode_utf32(bytes: &[u8], order: Order) -> Result<Vec<u32>, JsonError> {
    let (words, odd) = bytes.as_chunks::<4>();
    if !odd.is_empty() {
        return Err(JsonError::NotText);
    }

    words
        .iter()
        .map(|word| match order {
            Order::Big => u32::from_be_bytes(*word),
            Order::Little => u32::from_le_bytes(*word),
        })
        .map(|point| {
            if point > CODE_POINT_LAST {
                return Err(JsonError::NotText);
            }

            Ok(point)
        })
        .collect()
}

/// A list or a map that the reader has opened and not closed.
enum Open {
    List(Vec<Value>),
    Map { map: MapBuilder, key: String },
}

/// What the reader does next.
enum Step {
    /// Read one value.
    Value,
    /// Put a value into the innermost open list or map.
    Put(Value),
}

/// The state of one read: the code points and how far the read is.
struct Reader<'a> {
    points: &'a [u32],
    at: usize,
    /// Whether a string held a lone surrogate. The Python reader takes such a
    /// string, so a fault after it in the text is still a syntax fault.
    lone_surrogate: bool,
}

const fn ascii(byte: u8) -> u32 {
    // A `u8` always fits a `u32`. `u32::from` is not `const`.
    let wide: [u8; 4] = [byte, 0, 0, 0];

    u32::from_le_bytes(wide)
}

const QUOTE: u32 = ascii(b'"');
const BACKSLASH: u32 = ascii(b'\\');
const ZERO: u32 = ascii(b'0');
const NINE: u32 = ascii(b'9');
const BYTE_ORDER_MARK: u32 = 0xfeff;

/// The first code point that a string can hold with no escape.
const FIRST_PLAIN: u32 = 0x20;

/// The count of hex digits in a `\u` escape.
const ESCAPE_DIGITS: usize = 4;

impl<'a> Reader<'a> {
    fn new(points: &'a [u32]) -> Self {
        Self {
            points,
            at: 0,
            lone_surrogate: false,
        }
    }

    fn fault<T>(&self) -> Result<T, JsonError> {
        Err(JsonError::Syntax { at: self.at })
    }

    fn peek(&self) -> Option<u32> {
        self.points.get(self.at).copied()
    }

    fn peek_at(&self, ahead: usize) -> Option<u32> {
        self.points.get(self.at + ahead).copied()
    }

    /// Takes one code point when it is `wanted`.
    fn take(&mut self, wanted: u8) -> bool {
        if self.peek() != Some(ascii(wanted)) {
            return false;
        }

        self.at += 1;

        true
    }

    /// Takes `word` when the text holds it here.
    fn take_word(&mut self, word: &str) -> bool {
        let here = self.points.get(self.at..self.at + word.len());
        if !here.is_some_and(|here| here.iter().copied().eq(word.bytes().map(ascii))) {
            return false;
        }

        self.at += word.len();

        true
    }

    /// Skips the four characters that JSON calls white space.
    fn skip_space(&mut self) {
        while matches!(self.peek(), Some(0x20 | 0x09 | 0x0a | 0x0d)) {
            self.at += 1;
        }
    }

    fn is_digit(point: Option<u32>) -> bool {
        point.is_some_and(|point| (ZERO..=NINE).contains(&point))
    }

    /// The whole document: one value with white space around it.
    fn document(mut self) -> Result<Value, JsonError> {
        if self.peek() == Some(BYTE_ORDER_MARK) {
            return self.fault();
        }

        let value = self.nested()?;
        self.skip_space();
        if self.at != self.points.len() {
            return self.fault();
        }

        if self.lone_surrogate {
            return Err(JsonError::LoneSurrogate);
        }

        Ok(value)
    }

    /// One value with each value inside it. The function holds the open lists
    /// and maps in a `Vec`, so a deep document does not use the stack.
    fn nested(&mut self) -> Result<Value, JsonError> {
        let mut open: Vec<Open> = Vec::new();
        let mut step = Step::Value;
        loop {
            step = match step {
                Step::Value => self.value(&mut open)?,
                Step::Put(value) => match open.pop() {
                    None => return Ok(value),
                    Some(Open::List(items)) => self.after_item(items, value, &mut open)?,
                    Some(Open::Map { map, key }) => self.after_entry(map, key, value, &mut open)?,
                },
            };
        }
    }

    /// Reads the start of one value. A scalar is complete. A list or a map
    /// that is not empty goes to `open`.
    fn value(&mut self, open: &mut Vec<Open>) -> Result<Step, JsonError> {
        self.skip_space();
        let list = self.take(b'[');
        let map = !list && self.take(b'{');
        if !list && !map {
            return self.scalar().map(Step::Put);
        }

        // An empty list or map counts as a level too.
        if open.len() >= MAX_DEPTH {
            return Err(JsonError::TooDeep);
        }

        self.skip_space();
        if list {
            if self.take(b']') {
                return Ok(Step::Put(Value::List(Vec::new())));
            }

            open.push(Open::List(Vec::new()));

            return Ok(Step::Value);
        }

        if self.take(b'}') {
            return Ok(Step::Put(Value::Map(Map::new())));
        }

        let key = self.key()?;
        let map = MapBuilder::new();
        open.push(Open::Map { map, key });

        Ok(Step::Value)
    }

    /// Puts `value` into a list, then reads what follows it.
    fn after_item(
        &mut self,
        mut items: Vec<Value>,
        value: Value,
        open: &mut Vec<Open>,
    ) -> Result<Step, JsonError> {
        items.push(value);
        self.skip_space();
        if self.take(b']') {
            return Ok(Step::Put(Value::List(items)));
        }

        if !self.take(b',') {
            return self.fault();
        }

        open.push(Open::List(items));

        Ok(Step::Value)
    }

    /// Puts `value` into a map, then reads what follows it.
    fn after_entry(
        &mut self,
        mut map: MapBuilder,
        key: String,
        value: Value,
        open: &mut Vec<Open>,
    ) -> Result<Step, JsonError> {
        map.insert(key, value);
        self.skip_space();
        if self.take(b'}') {
            return Ok(Step::Put(Value::Map(map.finish())));
        }

        if !self.take(b',') {
            return self.fault();
        }

        self.skip_space();
        let key = self.key()?;
        open.push(Open::Map { map, key });

        Ok(Step::Value)
    }

    /// One key of an object and the colon after it.
    fn key(&mut self) -> Result<String, JsonError> {
        if !self.take(b'"') {
            return self.fault();
        }

        let key = self.string()?;
        self.skip_space();
        if !self.take(b':') {
            return self.fault();
        }

        Ok(key)
    }

    /// One value that is not a list and not a map.
    fn scalar(&mut self) -> Result<Value, JsonError> {
        if self.take(b'"') {
            return self.string().map(Value::Text);
        }

        if self.take_word("null") {
            return Ok(Value::Null);
        }

        if self.take_word("true") {
            return Ok(Value::Bool(true));
        }

        if self.take_word("false") {
            return Ok(Value::Bool(false));
        }

        if self.take_word("NaN") {
            return Ok(Value::Float(f64::NAN));
        }

        if self.take_word("Infinity") {
            return Ok(Value::Float(f64::INFINITY));
        }

        if self.take_word("-Infinity") {
            return Ok(Value::Float(f64::NEG_INFINITY));
        }

        self.number()
    }

    /// A string, after its first quote.
    fn string(&mut self) -> Result<String, JsonError> {
        let mut text = String::new();
        loop {
            let Some(point) = self.peek() else {
                return self.fault();
            };
            if point == QUOTE {
                self.at += 1;

                return Ok(text);
            }

            if point < FIRST_PLAIN {
                return self.fault();
            }

            self.at += 1;
            let point = if point == BACKSLASH {
                self.escape()?
            } else {
                point
            };
            text.push(self.character(point));
        }
    }

    /// The character of one code point of a string. A surrogate is not a
    /// character: the reader notes it and goes on.
    fn character(&mut self, point: u32) -> char {
        char::from_u32(point).unwrap_or_else(|| {
            self.lone_surrogate = true;

            REPLACEMENT
        })
    }

    /// The code point of one escape, after its backslash.
    fn escape(&mut self) -> Result<u32, JsonError> {
        let Some(point) = self.peek() else {
            return self.fault();
        };
        let plain = match u8::try_from(point) {
            Ok(b'"') => b'"',
            Ok(b'\\') => b'\\',
            Ok(b'/') => b'/',
            Ok(b'b') => 0x08,
            Ok(b'f') => 0x0c,
            Ok(b'n') => b'\n',
            Ok(b'r') => b'\r',
            Ok(b't') => b'\t',
            Ok(b'u') => {
                self.at += 1;

                return self.unicode_escape();
            }
            _ => return self.fault(),
        };
        self.at += 1;

        Ok(ascii(plain))
    }

    /// The code point of a `\u` escape, after the `u`. A high surrogate and
    /// the low surrogate of the next escape are one code point.
    fn unicode_escape(&mut self) -> Result<u32, JsonError> {
        let unit = self.hex_unit()?;
        if !is_high_surrogate(unit) {
            return Ok(unit);
        }

        if self.peek() != Some(BACKSLASH) || self.peek_at(1) != Some(ascii(b'u')) {
            return Ok(unit);
        }

        let before = self.at;
        self.at += 2;
        let low = self.hex_unit()?;
        if is_low_surrogate(low) {
            return Ok(join_surrogates(unit, low));
        }

        self.at = before;

        Ok(unit)
    }

    /// The four hex digits of a `\u` escape.
    fn hex_unit(&mut self) -> Result<u32, JsonError> {
        let mut unit = 0_u32;
        for _ in 0..ESCAPE_DIGITS {
            let digit = self
                .peek()
                .and_then(char::from_u32)
                .and_then(|digit| digit.to_digit(16));
            let Some(digit) = digit else {
                return self.fault();
            };
            unit = (unit << 4) | digit;
            self.at += 1;
        }

        Ok(unit)
    }

    /// A number: `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`, over ASCII
    /// digits. A fraction or an exponent makes a float.
    fn number(&mut self) -> Result<Value, JsonError> {
        let start = self.at;
        let sign = if self.take(b'-') {
            Sign::Minus
        } else {
            Sign::Plus
        };
        let digits_start = self.at;
        if self.take(b'0') {
            // A zero at the start is the whole integer part.
        } else if Self::is_digit(self.peek()) {
            self.digits();
        } else {
            self.at = start;

            return self.fault();
        }

        let digits_end = self.at;
        let mut float = false;
        if self.peek() == Some(ascii(b'.')) && Self::is_digit(self.peek_at(1)) {
            self.at += 1;
            self.digits();
            float = true;
        }

        if matches!(self.peek().map(u8::try_from), Some(Ok(b'e' | b'E'))) {
            let sign = matches!(self.peek_at(1).map(u8::try_from), Some(Ok(b'+' | b'-')));
            let first_digit = if sign { 2 } else { 1 };
            if Self::is_digit(self.peek_at(first_digit)) {
                self.at += first_digit;
                self.digits();
                float = true;
            }
        }

        if float {
            let text = self.ascii_text(start, self.at);

            // The reader matched the text against the number grammar, and
            // Rust reads each such text as a float.
            return text
                .parse()
                .map(Value::Float)
                .or(Err(JsonError::Syntax { at: start }));
        }

        if digits_end - digits_start > INT_MAX_DIGITS {
            return Err(JsonError::LongInteger);
        }

        let digits = self.ascii_text(digits_start, digits_end);

        Ok(Value::Integer(Integer::from_digits(sign, &digits)))
    }

    fn digits(&mut self) {
        while Self::is_digit(self.peek()) {
            self.at += 1;
        }
    }

    /// The text of a run of code points that the reader found to be ASCII.
    fn ascii_text(&self, start: usize, end: usize) -> String {
        self.points
            .get(start..end)
            .unwrap_or_default()
            .iter()
            .filter_map(|point| char::from_u32(*point))
            .collect()
    }
}

// --- the writer ---

/// Where one item ends and the next starts, as `json.dumps` writes it.
#[derive(Clone, Copy)]
pub(super) enum Layout {
    /// `json.dumps(value)`: all on one line, `, ` between two items.
    Line,
    /// `json.dumps(value, indent=2)`: one item on each line.
    Indented,
}

/// What the writer does with a character outside ASCII.
#[derive(Clone, Copy)]
pub(super) enum Charset {
    /// `ensure_ascii=True`, the default of Python: a `\u` escape.
    Ascii,
    /// `ensure_ascii=False`: the character as it is.
    Unicode,
}

/// The order of the keys of an object.
#[derive(Clone, Copy)]
pub(super) enum KeyOrder {
    /// The order of the map.
    Kept,
    /// `sort_keys=True`: by code point.
    Sorted,
}

/// How `json.dumps` writes a value.
#[derive(Clone, Copy)]
pub(super) struct Style {
    pub(super) layout: Layout,
    pub(super) charset: Charset,
    pub(super) keys: KeyOrder,
}

/// The count of spaces for one level of an indented document.
const INDENT: usize = 2;

/// Writes `value` as `json.dumps` of Python writes it.
pub(super) fn write(value: &Value, style: Style, out: &mut String) {
    write_at(value, style, 0, out);
}

fn write_at(value: &Value, style: Style, level: usize, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Integer(number) => out.push_str(&number.to_string()),
        Value::Float(number) => out.push_str(&float_repr(*number)),
        Value::Text(text) => write_text(text, style.charset, out),
        Value::List(items) => {
            if items.is_empty() {
                out.push_str("[]");

                return;
            }

            out.push('[');
            for (index, item) in items.iter().enumerate() {
                separate(index, style.layout, level + 1, out);
                write_at(item, style, level + 1, out);
            }

            close(style.layout, level, out);
            out.push(']');
        }
        Value::Map(map) => {
            if map.is_empty() {
                out.push_str("{}");

                return;
            }

            let entries = match style.keys {
                KeyOrder::Kept => map.iter().collect(),
                KeyOrder::Sorted => map.sorted(),
            };
            out.push('{');
            for (index, (key, item)) in entries.into_iter().enumerate() {
                separate(index, style.layout, level + 1, out);
                write_text(key, style.charset, out);
                out.push_str(": ");
                write_at(item, style, level + 1, out);
            }

            close(style.layout, level, out);
            out.push('}');
        }
    }
}

/// What stands before item `index` of a list or of a map.
fn separate(index: usize, layout: Layout, level: usize, out: &mut String) {
    match layout {
        Layout::Line if index > 0 => out.push_str(", "),
        Layout::Line => {}
        Layout::Indented => {
            if index > 0 {
                out.push(',');
            }

            new_line(level, out);
        }
    }
}

/// What stands before the bracket that closes a list or a map.
fn close(layout: Layout, level: usize, out: &mut String) {
    if matches!(layout, Layout::Indented) {
        new_line(level, out);
    }
}

fn new_line(level: usize, out: &mut String) {
    out.push('\n');
    out.push_str(&" ".repeat(level * INDENT));
}

fn write_text(text: &str, charset: Charset, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            _ if character < ' ' || matches!(charset, Charset::Ascii) => {
                let mut units = [0_u16; 2];
                for unit in character.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            _ => out.push(character),
        }
    }

    out.push('"');
}

/// The first decimal exponent for which Python writes a float with `e`.
const REPR_EXPONENT_FROM: i32 = 16;

/// The last decimal exponent below 0 for which Python writes no `e`.
const REPR_FIXED_FROM: i32 = -4;

/// The count of digits after the first one that shows if a float can be in
/// the middle of two decimals with 17 digits or less. The check with each
/// digit follows only for a float that passes this one.
const MIDDLE_PROBE_DIGITS: usize = 40;

/// The count of digits after the first one that holds each digit of each
/// `f64`: no `f64` has more than 767 significant decimal digits.
const EXACT_DIGITS: usize = 766;

/// A number that is not negative, as decimal digits and a decimal exponent:
/// `d.ddd` times 10 to the exponent.
struct Decimal {
    digits: String,
    exponent: i32,
}

impl Decimal {
    /// Reads what `{:e}` writes for a number that is not negative.
    fn of(text: &str) -> Self {
        let (mantissa, exponent) = text.split_once('e').unwrap_or((text, "0"));

        Self {
            digits: mantissa.chars().filter(char::is_ascii_digit).collect(),
            exponent: exponent.parse().unwrap_or(0),
        }
    }

    /// Whether the decimal reads back to the float `size`.
    fn reads_back_to(&self, size: f64) -> bool {
        let (first, rest) = self
            .digits
            .split_at_checked(1)
            .unwrap_or((&self.digits, ""));
        let text = format!("{first}.{rest}0e{}", self.exponent);

        text.parse::<f64>().is_ok_and(|read| read == size)
    }
}

/// The shortest digits that read back to `size`, as Python picks them. `size`
/// is finite and not negative.
///
/// `{:e}` writes the shortest digits too. The two differ for one kind of
/// float: a float that is exactly in the middle of two shortest candidates.
/// There Rust takes the candidate above, and Python takes the candidate with
/// an even last digit.
fn shortest(size: f64) -> Decimal {
    let near = Decimal::of(&format!("{size:e}"));
    let count = near.digits.len();

    // A float in the middle has one more digit than `near`, a 5, and then
    // only zeros. Most floats fail the first check, which reads two digits.
    let mut below = String::new();
    for precision in [count + 1, MIDDLE_PROBE_DIGITS, EXACT_DIGITS] {
        let exact = Decimal::of(&format!("{size:.precision$e}"));
        let Some((head, tail)) = exact.digits.split_at_checked(count) else {
            return near;
        };
        let middle = tail
            .strip_prefix('5')
            .is_some_and(|rest| rest.bytes().all(|digit| digit == b'0'));
        if !middle || exact.exponent != near.exponent {
            return near;
        }

        head.clone_into(&mut below);
    }

    // `below` is the candidate below the float. An ASCII digit is even when
    // its byte is even.
    let Some(last) = below.pop() else {
        return near;
    };
    let last = match u8::try_from(last) {
        Ok(digit) if digit % 2 == 0 => char::from(digit),
        Ok(digit @ b'1'..=b'7') => char::from(digit + 1),
        // A 9 would carry. Then a shorter decimal reads back, and `near` has
        // fewer digits: the float is not in the middle.
        _ => return near,
    };
    below.push(last);
    let even = Decimal {
        digits: below,
        exponent: near.exponent,
    };
    if even.reads_back_to(size) { even } else { near }
}

/// A float as `repr` of Python writes it: the shortest digits that read back
/// to the same float, with `e` for a large and for a small exponent.
fn float_repr(number: f64) -> String {
    if number.is_nan() {
        return "NaN".to_owned();
    }

    if number.is_infinite() {
        let text = if number > 0.0 {
            "Infinity"
        } else {
            "-Infinity"
        };

        return text.to_owned();
    }

    let sign = if number.is_sign_negative() { "-" } else { "" };
    if number == 0.0 {
        return format!("{sign}0.0");
    }

    let Decimal { digits, exponent } = shortest(number.abs());
    if !(REPR_FIXED_FROM..REPR_EXPONENT_FROM).contains(&exponent) {
        let (first, rest) = digits.split_at_checked(1).unwrap_or((&digits, ""));
        let point = if rest.is_empty() { "" } else { "." };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        let size = exponent.unsigned_abs();

        return format!("{sign}{first}{point}{rest}e{exponent_sign}{size:02}");
    }

    if exponent < 0 {
        let zeros = "0".repeat(usize::try_from(-exponent - 1).unwrap_or(0));

        return format!("{sign}0.{zeros}{digits}");
    }

    let whole = usize::try_from(exponent + 1).unwrap_or(0);
    match digits.split_at_checked(whole) {
        Some((before, after)) if !after.is_empty() => format!("{sign}{before}.{after}"),
        _ => {
            let zeros = "0".repeat(whole.saturating_sub(digits.len()));

            format!("{sign}{digits}{zeros}.0")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const LINE: Style = Style {
        layout: Layout::Line,
        charset: Charset::Unicode,
        keys: KeyOrder::Kept,
    };

    fn read(text: &str) -> Result<Value, JsonError> {
        read_utf8(text.as_bytes())
    }

    fn written(value: &Value, style: Style) -> String {
        let mut out = String::new();
        write(value, style, &mut out);

        out
    }

    fn integer(number: i64) -> Value {
        Value::Integer(Integer::from(number))
    }

    #[test]
    fn each_scalar_reads() {
        assert_eq!(read("null"), Ok(Value::Null));
        assert_eq!(read(" true\n"), Ok(Value::Bool(true)));
        assert_eq!(read("\tfalse\r"), Ok(Value::Bool(false)));
        assert_eq!(read("0"), Ok(integer(0)));
        assert_eq!(read("-0"), Ok(integer(0)));
        assert_eq!(read("-17"), Ok(integer(-17)));
        assert_eq!(read("1.5"), Ok(Value::Float(1.5)));
        assert_eq!(read("-0.0"), Ok(Value::Float(-0.0)));
        assert_eq!(read("1E2"), Ok(Value::Float(100.0)));
        assert_eq!(read("1e+2"), Ok(Value::Float(100.0)));
        assert_eq!(read("1e-2"), Ok(Value::Float(0.01)));
        assert_eq!(read("\"a\""), Ok(Value::Text("a".to_owned())));
    }

    #[test]
    fn the_constants_of_python_read() {
        assert_eq!(read("NaN"), Ok(Value::Float(f64::NAN)));
        assert_eq!(read("Infinity"), Ok(Value::Float(f64::INFINITY)));
        assert_eq!(read("-Infinity"), Ok(Value::Float(f64::NEG_INFINITY)));
        assert_eq!(read("1e400"), Ok(Value::Float(f64::INFINITY)));
        assert_eq!(read("-1e400"), Ok(Value::Float(f64::NEG_INFINITY)));
    }

    #[test]
    fn a_float_is_not_an_integer() {
        assert_ne!(read("1.0"), read("1"));
        assert_ne!(read("0.0"), read("-0.0"));
        assert_eq!(read("NaN"), read("NaN"));
    }

    #[test]
    fn an_integer_keeps_each_digit() {
        let big = "36893488147419103232";

        assert_eq!(read(big).unwrap(), {
            Value::Integer(Integer::from_digits(Sign::Plus, big))
        });
        assert_eq!(Integer::from_digits(Sign::Plus, big).to_u64(), None);
        assert_eq!(Integer::from_digits(Sign::Plus, big).to_string(), big);
        assert_eq!(
            Integer::from_digits(Sign::Minus, "000"),
            Integer::from(0_u64)
        );
        assert_eq!(Integer::from(-5_i64).to_string(), "-5");
        assert_eq!(Integer::from(i64::MIN).to_i64(), Some(i64::MIN));
        assert_eq!(Integer::from(u64::MAX).to_u64(), Some(u64::MAX));
        assert_eq!(Integer::from(u64::MAX).to_i64(), None);
        assert!(Integer::from(-5_i64).is_negative());
        assert!(Integer::from(1_u64).is_positive());
        assert!(!Integer::from(0_u64).is_positive());
        assert!(!Integer::from(-1_i64).is_positive());
    }

    #[test]
    fn an_integer_has_4300_digits_or_less() {
        let most = "9".repeat(INT_MAX_DIGITS);
        let more = "9".repeat(INT_MAX_DIGITS + 1);

        assert!(read(&most).is_ok());
        assert!(read(&format!("-{most}")).is_ok());
        assert_eq!(read(&more), Err(JsonError::LongInteger));
        assert_eq!(read(&format!("-{more}")), Err(JsonError::LongInteger));
        assert_eq!(
            read(&format!("{more}.0")),
            Ok(Value::Float(f64::INFINITY)),
            "a float has no cap on its digits"
        );
    }

    #[test]
    fn a_text_that_is_not_a_python_number_is_refused() {
        for text in [
            "01", "1.", ".5", "+1", "-", "1e", "1e+", "--1", "0x10", "1_0", "-NaN", "nan",
            "infinity", "True", "None", "",
        ] {
            assert!(
                matches!(read(text), Err(JsonError::Syntax { .. })),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_digit_that_is_not_ascii_is_no_digit() {
        // ARABIC-INDIC DIGIT ONE and FULLWIDTH DIGIT ONE.
        for text in ["\u{661}", "\u{ff11}", "1\u{661}"] {
            assert!(
                matches!(read(text), Err(JsonError::Syntax { .. })),
                "{text:?}"
            );
        }
    }

    #[test]
    fn each_escape_of_a_string_reads() {
        let text = read(r#""\" \\ \/ \b \f \n \r \t \u00e9 \u00E9 \ud83d\ude00""#).unwrap();

        assert_eq!(
            text.as_str().unwrap(),
            "\" \\ / \x08 \x0c \n \r \t \u{e9} \u{e9} \u{1f600}"
        );
    }

    #[test]
    fn a_string_with_a_fault_is_refused() {
        for text in [
            "\"a",
            "\"a\tb\"",
            "\"a\nb\"",
            "\"\\x\"",
            "\"\\u12G4\"",
            "\"\\u12\"",
            "\"\\",
            "'a'",
        ] {
            assert!(
                matches!(read(text), Err(JsonError::Syntax { .. })),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_lone_surrogate_is_refused_after_the_syntax() {
        assert_eq!(read(r#""\ud800""#), Err(JsonError::LoneSurrogate));
        assert_eq!(read(r#""\udc00\ud800""#), Err(JsonError::LoneSurrogate));
        assert_eq!(read(r#""\ud800\u0041""#), Err(JsonError::LoneSurrogate));
        assert_eq!(read(r#"{"\ud800": 1}"#), Err(JsonError::LoneSurrogate));
        assert!(matches!(
            read(r#"["\ud800", }"#),
            Err(JsonError::Syntax { .. })
        ));
    }

    #[test]
    fn a_list_and_a_map_read() {
        let value = read(r#" { "a" : [ 1 , [ ] , { } ] , "b" : { "c" : null } } "#).unwrap();
        let map = value.as_map().unwrap();

        assert_eq!(map.len(), 2);
        assert!(!map.is_empty());
        assert_eq!(
            map.get("a").unwrap().as_list().unwrap(),
            [integer(1), Value::List(vec![]), Value::Map(Map::new())]
        );
        assert_eq!(
            map.get("b").unwrap().as_map().unwrap().get("c"),
            Some(&Value::Null)
        );
        assert_eq!(map.get("c"), None);
        assert_eq!(Value::Null.as_map(), None);
        assert_eq!(Value::Null.as_list(), None);
        assert_eq!(Value::Null.as_str(), None);
    }

    #[test]
    fn the_last_value_of_a_key_stays_at_the_first_place() {
        let value = read(r#"{"b": 1, "a": 2, "b": 3}"#).unwrap();
        let entries: Vec<(&str, &Value)> = value.as_map().unwrap().iter().collect();

        assert_eq!(entries, [("b", &integer(3)), ("a", &integer(2))]);
    }

    #[test]
    fn a_list_or_a_map_with_a_fault_is_refused() {
        for text in [
            "[",
            "[1",
            "[1,",
            "[1,]",
            "[,1]",
            "[1 2]",
            "{",
            "{\"a\"",
            "{\"a\":",
            "{\"a\":1",
            "{\"a\":1,",
            "{\"a\":1,}",
            "{a:1}",
            "{1:1}",
            "{\"a\" 1}",
            "[]]",
            "{}{}",
            "1 2",
            "] [",
        ] {
            assert!(
                matches!(read(text), Err(JsonError::Syntax { .. })),
                "{text:?}"
            );
        }
    }

    #[test]
    fn white_space_is_four_characters() {
        assert!(read(" \t\r\n1 \t\r\n").is_ok());
        for space in ["\x0b", "\x0c", "\u{a0}", "\u{feff}", "\u{2028}"] {
            assert!(read(&format!("{space}1")).is_err(), "{space:?}");
            assert!(read(&format!("1{space}")).is_err(), "{space:?}");
        }
    }

    #[test]
    fn the_nesting_stops_at_the_cap() {
        let at_cap = format!("{}{}", "[".repeat(MAX_DEPTH), "]".repeat(MAX_DEPTH));
        let past_cap = format!("{}{}", "[".repeat(MAX_DEPTH + 1), "]".repeat(MAX_DEPTH + 1));
        let maps = format!(
            "{}1{}",
            "{\"a\":".repeat(MAX_DEPTH + 1),
            "}".repeat(MAX_DEPTH + 1)
        );
        let far = "[".repeat(400_000);

        assert!(read(&at_cap).is_ok());
        assert_eq!(read(&past_cap), Err(JsonError::TooDeep));
        assert_eq!(read(&maps), Err(JsonError::TooDeep));
        assert_eq!(read(&far), Err(JsonError::TooDeep));
    }

    #[test]
    fn a_grant_file_is_utf8_with_no_byte_order_mark() {
        assert_eq!(read_utf8(b"\xff"), Err(JsonError::NotText));
        assert_eq!(read_utf8(b"\"\xed\xa0\x80\""), Err(JsonError::NotText));
        assert_eq!(
            read_utf8(b"\xef\xbb\xbf1"),
            Err(JsonError::Syntax { at: 0 })
        );
        assert!(read_utf8(b"{\x00}\x00").is_err());
    }

    fn utf16(text: &str, order: Order) -> Vec<u8> {
        text.encode_utf16()
            .flat_map(|unit| match order {
                Order::Big => unit.to_be_bytes(),
                Order::Little => unit.to_le_bytes(),
            })
            .collect()
    }

    fn utf32(text: &str, order: Order) -> Vec<u8> {
        text.chars()
            .flat_map(|character| match order {
                Order::Big => u32::from(character).to_be_bytes(),
                Order::Little => u32::from(character).to_le_bytes(),
            })
            .collect()
    }

    #[test]
    fn a_body_reads_in_each_encoding_that_python_finds() {
        let text = "[\"\u{e9}\u{1f600}\"]";
        let wanted = read(text);
        let with = |mark: &[u8], bytes: Vec<u8>| [mark, &bytes].concat();

        assert!(wanted.is_ok());
        for body in [
            text.as_bytes().to_vec(),
            with(BOM_UTF8, text.as_bytes().to_vec()),
            utf16(text, Order::Big),
            utf16(text, Order::Little),
            with(BOM_UTF16_BE, utf16(text, Order::Big)),
            with(BOM_UTF16_LE, utf16(text, Order::Little)),
            utf32(text, Order::Big),
            utf32(text, Order::Little),
            with(BOM_UTF32_BE, utf32(text, Order::Big)),
            with(BOM_UTF32_LE, utf32(text, Order::Little)),
        ] {
            assert_eq!(read_body(&body), wanted, "{body:?}");
        }
    }

    #[test]
    fn a_short_body_reads_as_python_reads_it() {
        assert_eq!(read_body(b"5"), Ok(integer(5)));
        assert_eq!(read_body(b"\x005"), Ok(integer(5)));
        assert_eq!(read_body(b"5\x00"), Ok(integer(5)));
        assert_eq!(read_body(b"{}"), Ok(Value::Map(Map::new())));
        assert!(matches!(
            read_body(b"{}\x00"),
            Err(JsonError::Syntax { at: 2 })
        ));
        assert!(matches!(read_body(b""), Err(JsonError::Syntax { at: 0 })));
    }

    #[test]
    fn a_body_that_is_not_text_is_refused() {
        assert_eq!(read_body(b"\"\xff\""), Err(JsonError::NotText));
        assert_eq!(read_body(b"\"\xc3\""), Err(JsonError::NotText));
        assert_eq!(read_body(b"\xff\xfe1\x00\x00"), Err(JsonError::NotText));
        assert_eq!(
            read_body(b"\xff\xfe\x00\x001\x00\x00\x00\x00"),
            Err(JsonError::NotText)
        );
        assert_eq!(
            read_body(b"\x00\x00\xfe\xff\x00\x11\x00\x00"),
            Err(JsonError::NotText)
        );
    }

    #[test]
    fn a_surrogate_in_a_body_is_refused_as_python_would_keep_it() {
        // Python decodes each of these with `surrogatepass` and keeps the
        // surrogate. A string of this crate holds none.
        let utf8 = b"\"\xed\xa0\x80\"";
        let utf16 = [BOM_UTF16_LE, b"\"\x00\x00\xd8\"\x00"].concat();
        let utf32 = [BOM_UTF32_LE, &utf32("\"", Order::Little)[..]].concat();
        let utf32 = [&utf32[..], b"\x00\xd8\x00\x00\"\x00\x00\x00"].concat();

        assert_eq!(read_body(utf8), Err(JsonError::LoneSurrogate));
        assert_eq!(read_body(&utf16), Err(JsonError::LoneSurrogate));
        assert_eq!(read_body(&utf32), Err(JsonError::LoneSurrogate));
        assert!(matches!(
            read_body(b"\xed\xa0\x80"),
            Err(JsonError::Syntax { at: 0 })
        ));
    }

    #[test]
    fn a_surrogate_pair_in_utf16_is_one_character() {
        let body = [BOM_UTF16_BE, &utf16("\"\u{1f600}\"", Order::Big)[..]].concat();

        assert_eq!(read_body(&body), Ok(Value::Text("\u{1f600}".to_owned())));
    }

    #[test]
    fn a_float_is_written_as_python_writes_it() {
        for (number, text) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.5, "-1.5"),
            (100.0, "100.0"),
            (0.1, "0.1"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (-1.5e-7, "-1.5e-07"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e22, "1e+22"),
            (123_456_789.125, "123456789.125"),
            (9_007_199_254_740_993.0, "9007199254740992.0"),
            (1.234_567_890_123_456_7e19, "1.2345678901234567e+19"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (f64::MIN_POSITIVE, "2.2250738585072014e-308"),
            (1e100, "1e+100"),
            (f64::NAN, "NaN"),
            (f64::INFINITY, "Infinity"),
            (f64::NEG_INFINITY, "-Infinity"),
        ] {
            assert_eq!(float_repr(number), text);
        }
    }

    #[test]
    fn a_float_in_the_middle_of_two_candidates_takes_the_even_digit() {
        // Between 2^50 and 2^51 a float is a multiple of 0.25, so `.25` and
        // `.75` are exact. Each one is in the middle of two decimals with 17
        // digits. Rust writes `.3` for the first, and Python writes `.2`.
        let base = 1_160_972_656_570_364.0;
        let even = 2_000_000_000_000_000.0;
        for (number, text) in [
            (base + 0.25, "1160972656570364.2"),
            (base + 0.75, "1160972656570364.8"),
            (-base - 0.25, "-1160972656570364.2"),
            (even + 0.25, "2000000000000000.2"),
            (even + 1.75, "2000000000000001.8"),
            (base + 0.5, "1160972656570364.5"),
            (0.5, "0.5"),
            (9.5, "9.5"),
            (2.5, "2.5"),
        ] {
            assert_eq!(float_repr(number), text);
            assert_eq!(text.parse::<f64>().unwrap().to_bits(), number.to_bits());
        }
    }

    #[test]
    fn the_exact_digits_of_a_float_fit_the_probe() {
        // The smallest float has the longest expansion of each `f64` that
        // starts at its first digit: 751 digits.
        let exact = Decimal::of(&format!("{:.EXACT_DIGITS$e}", 5e-324));
        let digits = exact.digits.trim_end_matches('0');

        assert_eq!(exact.digits.len(), EXACT_DIGITS + 1);
        assert_eq!(exact.exponent, -324);
        assert_eq!(digits.len(), 751);
        assert!(digits.starts_with("4940656458412465441765687928682213723650598026143"));
        assert!(digits.ends_with("447265625"));
    }

    #[test]
    fn a_text_is_written_with_the_escapes_of_python() {
        let text =
            Value::Text("\" \\ / \x08 \x0c \n \r \t \0 \x1f \x7f \u{e9} \u{1f600}".to_owned());
        let ascii = Style {
            charset: Charset::Ascii,
            ..LINE
        };

        assert_eq!(
            written(&text, LINE),
            "\"\\\" \\\\ / \\b \\f \\n \\r \\t \\u0000 \\u001f \x7f \u{e9} \u{1f600}\""
        );
        assert_eq!(
            written(&text, ascii),
            "\"\\\" \\\\ / \\b \\f \\n \\r \\t \\u0000 \\u001f \\u007f \\u00e9 \\ud83d\\ude00\""
        );
    }

    #[test]
    fn a_document_is_written_on_one_line_or_with_an_indent() {
        let value = read(r#"{"b": [1, [], {}], "a": {"d": null, "c": [true, false]}}"#).unwrap();
        let indented = Style {
            layout: Layout::Indented,
            ..LINE
        };
        let sorted = Style {
            keys: KeyOrder::Sorted,
            ..LINE
        };

        assert_eq!(
            written(&value, LINE),
            r#"{"b": [1, [], {}], "a": {"d": null, "c": [true, false]}}"#
        );
        assert_eq!(
            written(&value, sorted),
            r#"{"a": {"c": [true, false], "d": null}, "b": [1, [], {}]}"#
        );
        assert_eq!(
            written(&value, indented),
            "{\n  \"b\": [\n    1,\n    [],\n    {}\n  ],\n  \"a\": {\n    \"d\": null,\n    \
             \"c\": [\n      true,\n      false\n    ]\n  }\n}"
        );
        assert_eq!(written(&Value::List(vec![]), indented), "[]");
        assert_eq!(written(&Value::Map(Map::default()), indented), "{}");
    }

    #[test]
    fn the_error_says_which_rule_failed() {
        assert_eq!(
            JsonError::Syntax { at: 7 }.to_string(),
            "the text stops being JSON after 7 characters"
        );
        assert_eq!(
            JsonError::TooDeep.to_string(),
            "the document nests deeper than 256 levels"
        );
        assert_eq!(
            JsonError::LongInteger.to_string(),
            "an integer has more than 4300 digits"
        );
        assert_eq!(
            JsonError::NotText.to_string(),
            "the bytes are not text in the encoding of the document"
        );
        assert_eq!(
            JsonError::LoneSurrogate.to_string(),
            "a string holds a lone surrogate"
        );

        let error: &dyn std::error::Error = &JsonError::TooDeep;

        assert!(error.source().is_none());
    }
}
