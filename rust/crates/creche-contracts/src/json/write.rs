//! The writer of a JSON text: the bytes that `json.dumps` of Python gives.
//!
//! The Python services write each file and each wire message with
//! `json.dumps`. A reader on the other side can compare bytes, count bytes or
//! hash them. So the writer here gives the same bytes for the same value, in
//! each [`Style`].
//!
//! The module holds a `Serializer` of its own. `serde_json` cannot do three
//! things that the writer must do:
//!
//! 1. Refuse a float that is not finite. `serde_json` writes `null` for it.
//! 2. Write a float as `repr` of Python writes it: `1e+21` and `1.0`.
//! 3. Sort the keys of each object, with no value tree between the value and
//!    the bytes.
//!
//! The writer keeps the entries of an object in a buffer only when the style
//! sorts the keys. In each other case, the bytes go out as the value gives
//! them.

use std::error::Error;
use std::fmt;

use serde::Serialize;
use serde::ser::{
    self, Impossible, SerializeMap, SerializeSeq, SerializeStruct, SerializeStructVariant,
    SerializeTuple, SerializeTupleStruct, SerializeTupleVariant, Serializer,
};
use serde_json::ser::{CompactFormatter, Formatter};

use super::scan::integer_value;

/// Where `json.dumps` puts white space.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layout {
    /// `separators=(",", ":")`: no white space.
    Compact,
    /// The default of `json.dumps`: one space after each comma and after
    /// each colon.
    Spaced,
    /// `indent=1`: one item on each line, and one space for each level.
    Indent1,
    /// `indent=2`: one item on each line, and two spaces for each level.
    Indent2,
}

impl Layout {
    /// The count of spaces for one level. `None` for a layout that keeps the
    /// text on one line.
    const fn indent(self) -> Option<usize> {
        match self {
            Self::Compact | Self::Spaced => None,
            Self::Indent1 => Some(1),
            Self::Indent2 => Some(2),
        }
    }

    /// What stands between two items.
    const fn comma(self) -> &'static [u8] {
        match self {
            Self::Spaced => b", ",
            Self::Compact | Self::Indent1 | Self::Indent2 => b",",
        }
    }

    /// What stands between a key and its value.
    const fn colon(self) -> &'static [u8] {
        match self {
            Self::Compact => b":",
            Self::Spaced | Self::Indent1 | Self::Indent2 => b": ",
        }
    }
}

/// Which characters of a string the writer gives as a `\u` escape.
///
/// In each charset, the writer gives `"`, `\` and each character below
/// U+0020 as an escape. The set is closed. It does not cross a process
/// boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Charset {
    /// `ensure_ascii=True`, the default of `json.dumps`. Each character
    /// outside printable ASCII is a `\u` escape with four hex digits in
    /// lower case. U+007F is such a character. A character outside the
    /// Basic Multilingual Plane is two escapes, one for each half of its
    /// surrogate pair.
    Ascii,
    /// `ensure_ascii=False`: each other character goes out as it is, in
    /// UTF-8.
    Utf8,
}

/// The order of the keys of each object.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyOrder {
    /// The order in which the value gives its keys. A struct gives its
    /// fields in the order of its definition.
    AsGiven,
    /// `sort_keys=True`: the order of the code points of the keys. The
    /// writer sorts each object of the value, at each level.
    Sorted,
}

/// How [`write()`] forms a text: one argument set of `json.dumps`.
///
/// ```
/// use creche_contracts::json::{self, Charset, KeyOrder, Layout, Style};
///
/// let style = Style::new(Layout::Spaced, Charset::Ascii, KeyOrder::Sorted);
/// assert_eq!(style.layout(), Layout::Spaced);
/// assert_eq!(style.charset(), Charset::Ascii);
/// assert_eq!(style.key_order(), KeyOrder::Sorted);
///
/// let pair = std::collections::BTreeMap::from([("caf\u{e9}", 1.0)]);
/// assert_eq!(json::write(&pair, style)?, br#"{"caf\u00e9": 1.0}"#);
/// # Ok::<(), json::WriteError>(())
/// ```
///
/// Code outside this module builds a style only with [`Style::new`]:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, Charset, KeyOrder, Layout, Style};
///
/// let style = Style {
///     layout: Layout::Spaced,
///     charset: Charset::Ascii,
///     key_order: KeyOrder::Sorted,
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Style {
    layout: Layout,
    charset: Charset,
    key_order: KeyOrder,
}

impl Style {
    /// The style with these three parts. Each combination is a style of
    /// `json.dumps`.
    #[must_use]
    pub const fn new(layout: Layout, charset: Charset, key_order: KeyOrder) -> Self {
        Self {
            layout,
            charset,
            key_order,
        }
    }

    /// Where the white space goes.
    #[must_use]
    pub const fn layout(&self) -> Layout {
        self.layout
    }

    /// Which characters are a `\u` escape.
    #[must_use]
    pub const fn charset(&self) -> Charset {
        self.charset
    }

    /// The order of the keys of each object.
    #[must_use]
    pub const fn key_order(&self) -> KeyOrder {
        self.key_order
    }
}

/// Why [`write()`] gives no text.
///
/// The error holds no part of the value, so a log line can print it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteError {
    /// A float of the value is `NaN` or an infinity. Strict JSON has no
    /// token for such a float, and the writer never writes `null` in its
    /// place.
    NotFinite,
    /// A part of the value has no form in strict JSON. The doc comment of
    /// [`write()`] lists each such part, for example the key of a map that is
    /// no text.
    NoJsonForm,
}

impl fmt::Display for WriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NotFinite => "a float that is not finite",
            Self::NoJsonForm => "a value with no JSON form",
        })
    }
}

impl Error for WriteError {}

/// A `Serialize` impl can refuse its own value with a message. The message
/// can hold a part of the value, so the error drops it.
impl ser::Error for WriteError {
    fn custom<T: fmt::Display>(_: T) -> Self {
        Self::NoJsonForm
    }
}

/// The JSON text of `value`, as `json.dumps` of Python writes it with the
/// arguments of `style`.
///
/// The writer holds the number rules of the reader: it refuses a float that
/// is not finite and an integer outside the range of rule 7 of the module
/// doc. A `str` of Rust is UTF-8 with no half of a surrogate pair, so the
/// encoding rule and the surrogate rule hold too.
///
/// The writer does not hold two rules of the reader. It counts no level,
/// and it does not compare the keys of an object. [`check`](super::check)
/// thus refuses the text of each of these two values:
///
/// - A value that nests more than [`DEPTH_MAX`](super::DEPTH_MAX) arrays and
///   objects. An [`Opaque`](super::Opaque) can nest that many levels by
///   itself, and each object around it adds one level.
/// - A map whose `Serialize` impl gives one key two times.
///
/// | Value | Text |
/// |---|---|
/// | `()`, `None`, a unit struct | `null` |
/// | `bool` | `true` or `false` |
/// | An integer | Its digits. The value must be in the range of rule 7 of the module doc. |
/// | A float | The text of `repr` of Python: the shortest digits that read back as the same float, `1.0` for a whole number and `1e+21` for a large one. An `f32` goes out as the `f64` with the same value. |
/// | `str`, `char` | A string, with the escapes of the [`Charset`]. |
/// | A sequence, a tuple | An array. |
/// | A map, a struct | An object. A key is a text. |
/// | A variant with no value | The name of the variant, as a string. |
/// | A variant with a value | An object with one key, the name of the variant. |
/// | [`Opaque`](super::Opaque) | Its value, in the style. |
///
/// ```
/// use creche_contracts::json::{self, Charset, KeyOrder, Layout, Style, WriteError};
///
/// const LINE: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);
///
/// assert_eq!(json::write(&(1e21, 0.5, 7), LINE)?, b"[1e+21,0.5,7]");
/// assert_eq!(json::write(&f64::NAN, LINE), Err(WriteError::NotFinite));
/// # Ok::<(), WriteError>(())
/// ```
///
/// # Errors
///
/// [`WriteError::NotFinite`] for a float that is `NaN` or an infinity, at
/// each depth of the value.
///
/// [`WriteError::NoJsonForm`] for each of these parts of a value:
///
/// - A key of a map that is no text. A text is a `str`, a `char`, a variant
///   with no value, or a newtype struct around one of them.
/// - An integer outside the range of rule 7 of the module doc. Only an
///   `i128` or a `u128` can hold one.
/// - A byte string. `json.dumps` refuses it too.
/// - A `RawValue` of `serde_json`. Its text has no style. Use
///   [`Opaque`](super::Opaque) for a value that the code does not read.
/// - A value whose `Serialize` impl gives an error of its own.
/// - A map whose `Serialize` impl gives a value with no key, or a key with
///   no value.
//
// CONTRACT-QUESTION: `rust/AGENTS.md`, "JSON", gives the reader a nesting
// limit and a rule against a key that an object holds two times. No rule
// and no contract says what the writer does with a value that breaks one of
// the two. Each Python writer writes such a value. The reading here is the
// same: the writer counts no level and compares no key, so its bytes stay
// the bytes of `json.dumps`. The other reading refuses such a value with a
// `WriteError`. That change costs one check of the level in `Frame::open`
// and one set of keys in `Table`. It also makes a Rust service refuse a
// value that the Python service writes.
pub fn write<T: Serialize + ?Sized>(value: &T, style: Style) -> Result<Vec<u8>, WriteError> {
    render(value, style, RawText::Refused)
}

/// The same text as [`write()`] gives, with one difference: a `RawValue` of
/// `serde_json` goes out as its own text, byte for byte. The style does not
/// apply to that text, and no check reads it.
///
/// Only a caller that must repeat the text of a file uses this function.
/// The `session` module is the one caller: it writes the body of a stored
/// journal line as the text that the journal file holds.
pub(crate) fn write_raw_kept<T: Serialize + ?Sized>(
    value: &T,
    style: Style,
) -> Result<Vec<u8>, WriteError> {
    render(value, style, RawText::Kept)
}

/// The count of bytes that [`write()`] gives for `value` in the compact
/// layout, with [`Charset::Utf8`] and the keys in their own order. The
/// function keeps no byte.
pub(super) fn compact_len<T: Serialize + ?Sized>(value: &T) -> Result<usize, WriteError> {
    let mut count = Count(0);
    value.serialize(Value {
        sink: &mut count,
        rules: Rules {
            style: Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven),
            raw: RawText::Refused,
        },
        level: 0,
    })?;

    Ok(count.0)
}

fn render<T: Serialize + ?Sized>(
    value: &T,
    style: Style,
    raw: RawText,
) -> Result<Vec<u8>, WriteError> {
    let mut bytes = Vec::new();
    value.serialize(Value {
        sink: &mut bytes,
        rules: Rules { style, raw },
        level: 0,
    })?;

    Ok(bytes)
}

/// What the writer does with the text of a `RawValue` of `serde_json`.
#[derive(Clone, Copy)]
enum RawText {
    /// The value has no JSON form.
    Refused,
    /// The text goes out as it is.
    Kept,
}

/// What each part of one write shares.
#[derive(Clone, Copy)]
struct Rules {
    style: Style,
    raw: RawText,
}

/// Where the bytes of a text go.
trait Sink {
    fn put(&mut self, bytes: &[u8]);
}

impl Sink for Vec<u8> {
    fn put(&mut self, bytes: &[u8]) {
        self.extend_from_slice(bytes);
    }
}

/// A sink that counts the bytes and keeps none.
struct Count(usize);

impl Sink for Count {
    fn put(&mut self, bytes: &[u8]) {
        self.0 += bytes.len();
    }
}

/// The name of each struct that `serde_json` uses to give a text of its own
/// to its own serializer starts with this prefix. Another serializer gets
/// such a value as a struct with one field. The writer refuses each of them,
/// so no text holds that struct as an object.
const SERDE_JSON_PRIVATE: &str = "$serde_json::private::";

/// The struct name and the field name of a `RawValue` of `serde_json`. The
/// crate has no public constant for the name. A test below holds the
/// constant equal to the name that the locked version uses.
const RAW_VALUE: &str = "$serde_json::private::RawValue";

/// An error for a step that cannot fail. The writer has no panic, so the
/// step gives the refusal of the writer.
fn broken<E>(_: E) -> WriteError {
    WriteError::NoJsonForm
}

/// Starts a new line at `level`, in a layout that has lines.
fn new_line(sink: &mut dyn Sink, layout: Layout, level: usize) {
    let Some(indent) = layout.indent() else {
        return;
    };

    sink.put(b"\n");
    for _ in 0..level * indent {
        sink.put(b" ");
    }
}

/// The last character that [`Charset::Ascii`] writes as it is.
const LAST_PRINTABLE: char = '~';

/// The first character that a string holds with no escape.
const FIRST_PLAIN: char = ' ';

/// Writes one string with its quotes.
fn write_text(sink: &mut dyn Sink, charset: Charset, text: &str) {
    sink.put(b"\"");

    // The first byte of the run of characters that need no escape.
    let mut run = 0;
    for (at, character) in text.char_indices() {
        // The escape of two characters, or `None` for a `\u` escape.
        let short: Option<&[u8]> = match character {
            '"' => Some(b"\\\""),
            '\\' => Some(b"\\\\"),
            '\n' => Some(b"\\n"),
            '\r' => Some(b"\\r"),
            '\t' => Some(b"\\t"),
            '\u{8}' => Some(b"\\b"),
            '\u{c}' => Some(b"\\f"),
            _ if character < FIRST_PLAIN => None,
            _ if charset == Charset::Ascii && character > LAST_PRINTABLE => None,
            _ => continue,
        };

        sink.put(text.as_bytes().get(run..at).unwrap_or_default());
        run = at + character.len_utf8();
        if let Some(escape) = short {
            sink.put(escape);
            continue;
        }

        // One escape for each code unit of UTF-16: a character outside the
        // Basic Multilingual Plane has two.
        for unit in character.encode_utf16(&mut [0; 2]) {
            sink.put(format!("\\u{unit:04x}").as_bytes());
        }
    }

    sink.put(text.as_bytes().get(run..).unwrap_or_default());
    sink.put(b"\"");
}

/// The least exponent of ten that Python writes with no exponent.
const FIXED_EXPONENT_MIN: i64 = -4;

/// The least exponent of ten that Python writes with an exponent.
const FIXED_EXPONENT_END: i64 = 16;

/// The text of a float, as `repr` of Python writes it: the shortest digits
/// that read back as the same float, `1.0` for a whole number, and an
/// exponent with a sign and two digits or more.
///
/// This function is the one float writer of the crate
/// (`rust/AGENTS.md`, "JSON").
///
/// The digits are the digits of `serde_json`. Like Python, it gives the
/// shortest digits and takes the even digit when two are equally near. The
/// `{:e}` format of the standard library takes the other digit there.
///
/// # Errors
///
/// [`WriteError::NotFinite`] for `NaN` and for an infinity.
fn float_repr(value: f64) -> Result<String, WriteError> {
    if !value.is_finite() {
        return Err(WriteError::NotFinite);
    }

    let mut shortest = Vec::new();
    CompactFormatter
        .write_f64(&mut shortest, value)
        .map_err(broken)?;
    let shortest = String::from_utf8(shortest).map_err(broken)?;
    let (sign, unsigned) = match shortest.strip_prefix('-') {
        Some(unsigned) => ("-", unsigned),
        None => ("", shortest.as_str()),
    };
    let (mantissa, exponent) = unsigned.split_once(['e', 'E']).unwrap_or((unsigned, "0"));
    let exponent: i64 = exponent.parse().map_err(broken)?;
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let all = format!("{whole}{fraction}");
    let significant = all.trim_start_matches('0');
    let digits = significant.trim_end_matches('0');
    if digits.is_empty() {
        return Ok(format!("{sign}0.0"));
    }

    // The exponent of ten of the first digit.
    let width = |text: &str| i64::try_from(text.len()).map_err(broken);
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
        let zeros = "0".repeat(usize::try_from(-exponent - 1).map_err(broken)?);

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

/// The serializer of one value. `level` is the count of arrays and objects
/// that are open around the value.
struct Value<'a> {
    sink: &'a mut dyn Sink,
    rules: Rules,
    level: usize,
}

impl<'a> Value<'a> {
    fn word(self, word: &[u8]) -> Result<(), WriteError> {
        self.sink.put(word);

        Ok(())
    }

    /// Writes the digits of an integer of 128 bits, when rule 7 of the
    /// module doc of `json` takes them. The reader has the one copy of that
    /// range: `integer_value`.
    fn wide(self, digits: &str) -> Result<(), WriteError> {
        if integer_value(digits).is_none() {
            return Err(WriteError::NoJsonForm);
        }

        self.word(digits.as_bytes())
    }

    /// Opens the object of a variant with a value and writes its one key.
    /// The result holds the serializer of that value.
    fn variant(self, name: &str) -> InVariant<Value<'a>> {
        let Self { sink, rules, level } = self;
        let layout = rules.style.layout;

        sink.put(b"{");
        new_line(sink, layout, level + 1);
        write_text(sink, rules.style.charset, name);
        sink.put(layout.colon());

        InVariant {
            inner: Value {
                sink,
                rules,
                level: level + 1,
            },
            layout,
            level,
        }
    }
}

/// Each integer type of 64 bits or less: the digits of the value.
macro_rules! digits {
    ($($method:ident($integer:ty))*) => {
        $(
            fn $method(self, value: $integer) -> Result<(), WriteError> {
                self.word(value.to_string().as_bytes())
            }
        )*
    };
}

impl<'a> Serializer for Value<'a> {
    type Ok = ();
    type Error = WriteError;
    type SerializeSeq = List<'a>;
    type SerializeTuple = List<'a>;
    type SerializeTupleStruct = List<'a>;
    type SerializeTupleVariant = InVariant<List<'a>>;
    type SerializeMap = Table<'a>;
    type SerializeStruct = Record<'a>;
    type SerializeStructVariant = InVariant<Table<'a>>;

    digits! {
        serialize_i8(i8) serialize_i16(i16) serialize_i32(i32) serialize_i64(i64)
        serialize_u8(u8) serialize_u16(u16) serialize_u32(u32) serialize_u64(u64)
    }

    fn serialize_bool(self, flag: bool) -> Result<(), WriteError> {
        self.word(if flag { b"true" } else { b"false" })
    }

    fn serialize_i128(self, value: i128) -> Result<(), WriteError> {
        self.wide(&value.to_string())
    }

    fn serialize_u128(self, value: u128) -> Result<(), WriteError> {
        self.wide(&value.to_string())
    }

    fn serialize_f32(self, value: f32) -> Result<(), WriteError> {
        self.serialize_f64(f64::from(value))
    }

    fn serialize_f64(self, value: f64) -> Result<(), WriteError> {
        self.word(float_repr(value)?.as_bytes())
    }

    fn serialize_char(self, value: char) -> Result<(), WriteError> {
        self.serialize_str(value.encode_utf8(&mut [0; 4]))
    }

    fn serialize_str(self, value: &str) -> Result<(), WriteError> {
        write_text(self.sink, self.rules.style.charset, value);

        Ok(())
    }

    fn serialize_bytes(self, _: &[u8]) -> Result<(), WriteError> {
        Err(WriteError::NoJsonForm)
    }

    fn serialize_none(self) -> Result<(), WriteError> {
        self.serialize_unit()
    }

    fn serialize_some<T: Serialize + ?Sized>(self, value: &T) -> Result<(), WriteError> {
        value.serialize(self)
    }

    fn serialize_unit(self) -> Result<(), WriteError> {
        self.word(b"null")
    }

    fn serialize_unit_struct(self, _: &'static str) -> Result<(), WriteError> {
        self.serialize_unit()
    }

    fn serialize_unit_variant(
        self,
        _: &'static str,
        _: u32,
        variant: &'static str,
    ) -> Result<(), WriteError> {
        self.serialize_str(variant)
    }

    fn serialize_newtype_struct<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        value: &T,
    ) -> Result<(), WriteError> {
        value.serialize(self)
    }

    fn serialize_newtype_variant<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        _: u32,
        variant: &'static str,
        value: &T,
    ) -> Result<(), WriteError> {
        self.variant(variant).only(value)
    }

    fn serialize_seq(self, _: Option<usize>) -> Result<List<'a>, WriteError> {
        Ok(List::open(self))
    }

    fn serialize_tuple(self, _: usize) -> Result<List<'a>, WriteError> {
        self.serialize_seq(None)
    }

    fn serialize_tuple_struct(self, _: &'static str, _: usize) -> Result<List<'a>, WriteError> {
        self.serialize_seq(None)
    }

    fn serialize_tuple_variant(
        self,
        _: &'static str,
        _: u32,
        variant: &'static str,
        _: usize,
    ) -> Result<InVariant<List<'a>>, WriteError> {
        Ok(self.variant(variant).around(List::open))
    }

    fn serialize_map(self, _: Option<usize>) -> Result<Table<'a>, WriteError> {
        Ok(Table::open(self))
    }

    fn serialize_struct(self, name: &'static str, _: usize) -> Result<Record<'a>, WriteError> {
        if !name.starts_with(SERDE_JSON_PRIVATE) {
            return Ok(Record::Table(Table::open(self)));
        }

        match (name, self.rules.raw) {
            (RAW_VALUE, RawText::Kept) => Ok(Record::Raw(Some(self.sink))),
            _ => Err(WriteError::NoJsonForm),
        }
    }

    fn serialize_struct_variant(
        self,
        _: &'static str,
        _: u32,
        variant: &'static str,
        _: usize,
    ) -> Result<InVariant<Table<'a>>, WriteError> {
        Ok(self.variant(variant).around(Table::open))
    }
}

/// One open array or object: where its bytes go, and the count of its items.
struct Frame<'a> {
    sink: &'a mut dyn Sink,
    rules: Rules,
    level: usize,
    count: usize,
}

impl<'a> Frame<'a> {
    /// Writes the bracket that opens the array or the object of `value`.
    fn open(value: Value<'a>, bracket: &[u8]) -> Self {
        let Value { sink, rules, level } = value;
        sink.put(bracket);

        Self {
            sink,
            rules,
            level,
            count: 0,
        }
    }

    /// Writes what stands before the next item: a comma after an item, and
    /// a new line in a layout that has lines.
    fn next_item(&mut self) {
        let layout = self.rules.style.layout;
        if self.count > 0 {
            self.sink.put(layout.comma());
        }

        new_line(self.sink, layout, self.level + 1);
        self.count += 1;
    }

    /// The serializer of one value of the array or of the object.
    fn inner(&mut self) -> Value<'_> {
        Value {
            sink: &mut *self.sink,
            rules: self.rules,
            level: self.level + 1,
        }
    }

    /// Writes one key and its colon.
    fn key(&mut self, key: &str) {
        self.next_item();
        write_text(self.sink, self.rules.style.charset, key);
        self.sink.put(self.rules.style.layout.colon());
    }

    /// Writes the bracket that closes the array or the object. An empty one
    /// has no white space between its two brackets.
    fn close(self, bracket: &[u8]) -> &'a mut dyn Sink {
        if self.count > 0 {
            new_line(self.sink, self.rules.style.layout, self.level);
        }
        self.sink.put(bracket);

        self.sink
    }
}

/// A part of a value that ends with a bracket of its own.
trait Closes<'a> {
    /// Writes the last bracket. The result is the sink, for the bytes that
    /// follow.
    fn close(self) -> Result<&'a mut dyn Sink, WriteError>;
}

/// An open array.
struct List<'a>(Frame<'a>);

impl<'a> List<'a> {
    fn open(value: Value<'a>) -> Self {
        Self(Frame::open(value, b"["))
    }

    fn item<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), WriteError> {
        self.0.next_item();

        value.serialize(self.0.inner())
    }
}

impl<'a> Closes<'a> for List<'a> {
    fn close(self) -> Result<&'a mut dyn Sink, WriteError> {
        Ok(self.0.close(b"]"))
    }
}

/// The three kinds of a sequence in the data model of `serde`. Each one is
/// an array.
macro_rules! array {
    ($($kind:ident::$item:ident)*) => {
        $(
            impl $kind for List<'_> {
                type Ok = ();
                type Error = WriteError;

                fn $item<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), WriteError> {
                    self.item(value)
                }

                fn end(self) -> Result<(), WriteError> {
                    self.close().map(drop)
                }
            }
        )*
    };
}

array! {
    SerializeSeq::serialize_element
    SerializeTuple::serialize_element
    SerializeTupleStruct::serialize_field
}

/// What an open object waits for.
enum Next {
    /// A key, or the end of the object.
    Key,
    /// The value of the last key.
    Value,
}

/// An open object.
enum Table<'a> {
    /// The entries go out in the order of the value.
    AsGiven { frame: Frame<'a>, next: Next },
    /// The object keeps each entry until its end, and then writes them in
    /// the order of their keys. `key` is a key that has no value yet.
    Sorted {
        frame: Frame<'a>,
        entries: Vec<(String, Vec<u8>)>,
        key: Option<String>,
    },
}

impl<'a> Table<'a> {
    fn open(value: Value<'a>) -> Self {
        let frame = Frame::open(value, b"{");
        match frame.rules.style.key_order {
            KeyOrder::AsGiven => Self::AsGiven {
                frame,
                next: Next::Key,
            },
            KeyOrder::Sorted => Self::Sorted {
                frame,
                entries: Vec::new(),
                key: None,
            },
        }
    }

    /// Takes the next key. The object refuses a key that follows a key with
    /// no value.
    fn key(&mut self, name: &str) -> Result<(), WriteError> {
        match self {
            Self::AsGiven {
                frame,
                next: next @ Next::Key,
            } => {
                frame.key(name);
                *next = Next::Value;
            }
            Self::Sorted {
                key: key @ None, ..
            } => *key = Some(name.to_owned()),
            Self::AsGiven {
                next: Next::Value, ..
            }
            | Self::Sorted { key: Some(_), .. } => return Err(WriteError::NoJsonForm),
        }

        Ok(())
    }

    /// Takes the value of the last key. The object refuses a value that has
    /// no key.
    fn value<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), WriteError> {
        match self {
            Self::AsGiven {
                frame,
                next: next @ Next::Value,
            } => {
                *next = Next::Key;

                value.serialize(frame.inner())
            }
            Self::Sorted {
                frame,
                entries,
                key,
            } => {
                let key = key.take().ok_or(WriteError::NoJsonForm)?;
                let mut bytes = Vec::new();
                value.serialize(Value {
                    sink: &mut bytes,
                    rules: frame.rules,
                    level: frame.level + 1,
                })?;
                entries.push((key, bytes));

                Ok(())
            }
            Self::AsGiven {
                next: Next::Key, ..
            } => Err(WriteError::NoJsonForm),
        }
    }

    fn entry<T: Serialize + ?Sized>(&mut self, key: &str, value: &T) -> Result<(), WriteError> {
        self.key(key)?;

        self.value(value)
    }
}

impl<'a> Closes<'a> for Table<'a> {
    /// The object refuses its end after a key with no value.
    fn close(self) -> Result<&'a mut dyn Sink, WriteError> {
        match self {
            Self::AsGiven {
                frame,
                next: Next::Key,
            } => Ok(frame.close(b"}")),
            Self::Sorted {
                mut frame,
                mut entries,
                key: None,
            } => {
                // The order of `str` is the order of the code points. The
                // sort keeps two equal keys in the order of the value.
                entries.sort_by(|one, other| one.0.cmp(&other.0));
                for (key, bytes) in entries {
                    frame.key(&key);
                    frame.sink.put(&bytes);
                }

                Ok(frame.close(b"}"))
            }
            Self::AsGiven {
                next: Next::Value, ..
            }
            | Self::Sorted { key: Some(_), .. } => Err(WriteError::NoJsonForm),
        }
    }
}

impl SerializeMap for Table<'_> {
    type Ok = ();
    type Error = WriteError;

    fn serialize_key<T: Serialize + ?Sized>(&mut self, key: &T) -> Result<(), WriteError> {
        key.serialize(TextOnly(|name: &str| self.key(name)))?
    }

    fn serialize_value<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), WriteError> {
        self.value(value)
    }

    fn end(self) -> Result<(), WriteError> {
        self.close().map(drop)
    }
}

/// What a struct of the data model of `serde` is for the writer.
enum Record<'a> {
    /// An object: each field is one entry.
    Table(Table<'a>),
    /// A `RawValue` of `serde_json`. Its one field is its text, and the
    /// sink is here until that field comes.
    Raw(Option<&'a mut dyn Sink>),
}

impl SerializeStruct for Record<'_> {
    type Ok = ();
    type Error = WriteError;

    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        key: &'static str,
        value: &T,
    ) -> Result<(), WriteError> {
        match self {
            Self::Table(table) => table.entry(key, value),
            Self::Raw(sink) => {
                let sink = sink
                    .take()
                    .filter(|_| key == RAW_VALUE)
                    .ok_or(WriteError::NoJsonForm)?;

                value.serialize(TextOnly(|text: &str| sink.put(text.as_bytes())))
            }
        }
    }

    fn end(self) -> Result<(), WriteError> {
        match self {
            Self::Table(table) => table.close().map(drop),
            Self::Raw(None) => Ok(()),
            // No field gave a text.
            Self::Raw(Some(_)) => Err(WriteError::NoJsonForm),
        }
    }
}

/// The value of a variant: `inner` is inside an object with one key, at
/// `level`.
struct InVariant<Inner> {
    inner: Inner,
    layout: Layout,
    level: usize,
}

impl<Inner> InVariant<Inner> {
    /// The same place in the object, with `open` around the value.
    fn around<Opened>(self, open: impl FnOnce(Inner) -> Opened) -> InVariant<Opened> {
        InVariant {
            inner: open(self.inner),
            layout: self.layout,
            level: self.level,
        }
    }

    /// Closes the object around the value.
    fn end(sink: &mut dyn Sink, layout: Layout, level: usize) {
        new_line(sink, layout, level);
        sink.put(b"}");
    }
}

impl InVariant<Value<'_>> {
    /// Writes a value with no bracket of its own, and closes the object.
    fn only<T: Serialize + ?Sized>(self, value: &T) -> Result<(), WriteError> {
        let Value { sink, rules, level } = self.inner;
        value.serialize(Value {
            sink: &mut *sink,
            rules,
            level,
        })?;
        Self::end(sink, self.layout, self.level);

        Ok(())
    }
}

impl<'a, Inner: Closes<'a>> InVariant<Inner> {
    /// Closes the value, and then the object around it.
    fn finish(self) -> Result<(), WriteError> {
        Self::end(self.inner.close()?, self.layout, self.level);

        Ok(())
    }
}

impl SerializeTupleVariant for InVariant<List<'_>> {
    type Ok = ();
    type Error = WriteError;

    fn serialize_field<T: Serialize + ?Sized>(&mut self, value: &T) -> Result<(), WriteError> {
        self.inner.item(value)
    }

    fn end(self) -> Result<(), WriteError> {
        self.finish()
    }
}

impl SerializeStructVariant for InVariant<Table<'_>> {
    type Ok = ();
    type Error = WriteError;

    fn serialize_field<T: Serialize + ?Sized>(
        &mut self,
        key: &'static str,
        value: &T,
    ) -> Result<(), WriteError> {
        self.inner.entry(key, value)
    }

    fn end(self) -> Result<(), WriteError> {
        self.finish()
    }
}

/// A serializer that takes a text and no value of another kind. `F` gets
/// the text.
///
/// The key of a map is a text. A `str`, a `char`, a variant with no value
/// and a newtype struct around one of the three each give one.
struct TextOnly<F>(F);

/// Each method of a serializer that refuses its value.
macro_rules! refuses {
    ($($method:ident($($part:ty),*) -> $result:ty;)*) => {
        $(
            fn $method(self $(, _: $part)*) -> Result<$result, WriteError> {
                Err(WriteError::NoJsonForm)
            }
        )*
    };
}

impl<F: FnOnce(&str) -> Taken, Taken> Serializer for TextOnly<F> {
    type Ok = Taken;
    type Error = WriteError;
    type SerializeSeq = Impossible<Taken, WriteError>;
    type SerializeTuple = Impossible<Taken, WriteError>;
    type SerializeTupleStruct = Impossible<Taken, WriteError>;
    type SerializeTupleVariant = Impossible<Taken, WriteError>;
    type SerializeMap = Impossible<Taken, WriteError>;
    type SerializeStruct = Impossible<Taken, WriteError>;
    type SerializeStructVariant = Impossible<Taken, WriteError>;

    refuses! {
        serialize_bool(bool) -> Taken;
        serialize_i8(i8) -> Taken;
        serialize_i16(i16) -> Taken;
        serialize_i32(i32) -> Taken;
        serialize_i64(i64) -> Taken;
        serialize_i128(i128) -> Taken;
        serialize_u8(u8) -> Taken;
        serialize_u16(u16) -> Taken;
        serialize_u32(u32) -> Taken;
        serialize_u64(u64) -> Taken;
        serialize_u128(u128) -> Taken;
        serialize_f32(f32) -> Taken;
        serialize_f64(f64) -> Taken;
        serialize_bytes(&[u8]) -> Taken;
        serialize_none() -> Taken;
        serialize_unit() -> Taken;
        serialize_unit_struct(&'static str) -> Taken;
        serialize_seq(Option<usize>) -> Self::SerializeSeq;
        serialize_tuple(usize) -> Self::SerializeTuple;
        serialize_tuple_struct(&'static str, usize) -> Self::SerializeTupleStruct;
        serialize_tuple_variant(&'static str, u32, &'static str, usize)
            -> Self::SerializeTupleVariant;
        serialize_map(Option<usize>) -> Self::SerializeMap;
        serialize_struct(&'static str, usize) -> Self::SerializeStruct;
        serialize_struct_variant(&'static str, u32, &'static str, usize)
            -> Self::SerializeStructVariant;
    }

    fn serialize_str(self, value: &str) -> Result<Taken, WriteError> {
        Ok((self.0)(value))
    }

    fn serialize_char(self, value: char) -> Result<Taken, WriteError> {
        self.serialize_str(value.encode_utf8(&mut [0; 4]))
    }

    fn serialize_unit_variant(
        self,
        _: &'static str,
        _: u32,
        variant: &'static str,
    ) -> Result<Taken, WriteError> {
        self.serialize_str(variant)
    }

    fn serialize_newtype_struct<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        value: &T,
    ) -> Result<Taken, WriteError> {
        value.serialize(self)
    }

    fn serialize_some<T: Serialize + ?Sized>(self, _: &T) -> Result<Taken, WriteError> {
        Err(WriteError::NoJsonForm)
    }

    fn serialize_newtype_variant<T: Serialize + ?Sized>(
        self,
        _: &'static str,
        _: u32,
        _: &'static str,
        _: &T,
    ) -> Result<Taken, WriteError> {
        Err(WriteError::NoJsonForm)
    }
}

#[cfg(test)]
pub(super) mod tests {
    use std::collections::BTreeMap;

    use serde_json::value::RawValue;

    use super::*;

    const LAYOUTS: [Layout; 4] = [
        Layout::Compact,
        Layout::Spaced,
        Layout::Indent1,
        Layout::Indent2,
    ];
    const CHARSETS: [Charset; 2] = [Charset::Ascii, Charset::Utf8];
    const KEY_ORDERS: [KeyOrder; 2] = [KeyOrder::AsGiven, KeyOrder::Sorted];

    const COMPACT: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::AsGiven);
    const SORTED: Style = Style::new(Layout::Compact, Charset::Utf8, KeyOrder::Sorted);

    /// Each style: 16 argument sets of `json.dumps`.
    fn styles() -> Vec<Style> {
        let mut styles = Vec::new();
        for layout in LAYOUTS {
            for charset in CHARSETS {
                for key_order in KEY_ORDERS {
                    styles.push(Style::new(layout, charset, key_order));
                }
            }
        }

        styles
    }

    fn text_of<T: Serialize + ?Sized>(value: &T, style: Style) -> String {
        String::from_utf8(write(value, style).unwrap()).unwrap()
    }

    /// A value of a test. A table gives its keys in the order of its list,
    /// so no map type decides the order.
    enum Node {
        Null,
        Flag(bool),
        Int(i64),
        Float(f64),
        Text(&'static str),
        List(Vec<Node>),
        Table(Vec<(&'static str, Node)>),
    }

    impl Serialize for Node {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            match self {
                Self::Null => serializer.serialize_unit(),
                Self::Flag(flag) => serializer.serialize_bool(*flag),
                Self::Int(integer) => serializer.serialize_i64(*integer),
                Self::Float(float) => serializer.serialize_f64(*float),
                Self::Text(text) => serializer.serialize_str(text),
                Self::List(items) => serializer.collect_seq(items),
                Self::Table(entries) => {
                    serializer.collect_map(entries.iter().map(|(key, value)| (key, value)))
                }
            }
        }
    }

    /// One value with each kind of JSON value, an empty array, an empty
    /// object and keys that are not in sorted order.
    fn sample() -> Node {
        Node::Table(vec![
            (
                "b",
                Node::List(vec![
                    Node::Int(1),
                    Node::Float(-2.5),
                    Node::Text("caf\u{e9} \u{7f}\n"),
                    Node::List(Vec::new()),
                    Node::Table(Vec::new()),
                ]),
            ),
            (
                "a",
                Node::Table(vec![
                    ("d", Node::Null),
                    ("c", Node::Flag(true)),
                    ("\u{e9}", Node::Flag(false)),
                ]),
            ),
            ("\u{1f600}", Node::Float(1e21)),
        ])
    }

    /// What `json.dumps` of Python 3.13 gives for the value of `sample`, in
    /// each style. The test of `Opaque` reads the same value from a text and
    /// compares it with this table.
    pub(in crate::json) const SAMPLE_TEXTS: [(Layout, Charset, KeyOrder, &str); 16] = [
        (
            Layout::Compact,
            Charset::Ascii,
            KeyOrder::AsGiven,
            "{\"b\":[1,-2.5,\"caf\\u00e9 \\u007f\\n\",[],{}],\"a\":{\"d\":null,\"c\":true,\"\\u00e9\":false},\"\\ud83d\\ude00\":1e+21}",
        ),
        (
            Layout::Compact,
            Charset::Ascii,
            KeyOrder::Sorted,
            "{\"a\":{\"c\":true,\"d\":null,\"\\u00e9\":false},\"b\":[1,-2.5,\"caf\\u00e9 \\u007f\\n\",[],{}],\"\\ud83d\\ude00\":1e+21}",
        ),
        (
            Layout::Compact,
            Charset::Utf8,
            KeyOrder::AsGiven,
            "{\"b\":[1,-2.5,\"caf\u{e9} \u{7f}\\n\",[],{}],\"a\":{\"d\":null,\"c\":true,\"\u{e9}\":false},\"\u{1f600}\":1e+21}",
        ),
        (
            Layout::Compact,
            Charset::Utf8,
            KeyOrder::Sorted,
            "{\"a\":{\"c\":true,\"d\":null,\"\u{e9}\":false},\"b\":[1,-2.5,\"caf\u{e9} \u{7f}\\n\",[],{}],\"\u{1f600}\":1e+21}",
        ),
        (
            Layout::Spaced,
            Charset::Ascii,
            KeyOrder::AsGiven,
            "{\"b\": [1, -2.5, \"caf\\u00e9 \\u007f\\n\", [], {}], \"a\": {\"d\": null, \"c\": true, \"\\u00e9\": false}, \"\\ud83d\\ude00\": 1e+21}",
        ),
        (
            Layout::Spaced,
            Charset::Ascii,
            KeyOrder::Sorted,
            "{\"a\": {\"c\": true, \"d\": null, \"\\u00e9\": false}, \"b\": [1, -2.5, \"caf\\u00e9 \\u007f\\n\", [], {}], \"\\ud83d\\ude00\": 1e+21}",
        ),
        (
            Layout::Spaced,
            Charset::Utf8,
            KeyOrder::AsGiven,
            "{\"b\": [1, -2.5, \"caf\u{e9} \u{7f}\\n\", [], {}], \"a\": {\"d\": null, \"c\": true, \"\u{e9}\": false}, \"\u{1f600}\": 1e+21}",
        ),
        (
            Layout::Spaced,
            Charset::Utf8,
            KeyOrder::Sorted,
            "{\"a\": {\"c\": true, \"d\": null, \"\u{e9}\": false}, \"b\": [1, -2.5, \"caf\u{e9} \u{7f}\\n\", [], {}], \"\u{1f600}\": 1e+21}",
        ),
        (
            Layout::Indent1,
            Charset::Ascii,
            KeyOrder::AsGiven,
            "{\n \"b\": [\n  1,\n  -2.5,\n  \"caf\\u00e9 \\u007f\\n\",\n  [],\n  {}\n ],\n \"a\": {\n  \"d\": null,\n  \"c\": true,\n  \"\\u00e9\": false\n },\n \"\\ud83d\\ude00\": 1e+21\n}",
        ),
        (
            Layout::Indent1,
            Charset::Ascii,
            KeyOrder::Sorted,
            "{\n \"a\": {\n  \"c\": true,\n  \"d\": null,\n  \"\\u00e9\": false\n },\n \"b\": [\n  1,\n  -2.5,\n  \"caf\\u00e9 \\u007f\\n\",\n  [],\n  {}\n ],\n \"\\ud83d\\ude00\": 1e+21\n}",
        ),
        (
            Layout::Indent1,
            Charset::Utf8,
            KeyOrder::AsGiven,
            "{\n \"b\": [\n  1,\n  -2.5,\n  \"caf\u{e9} \u{7f}\\n\",\n  [],\n  {}\n ],\n \"a\": {\n  \"d\": null,\n  \"c\": true,\n  \"\u{e9}\": false\n },\n \"\u{1f600}\": 1e+21\n}",
        ),
        (
            Layout::Indent1,
            Charset::Utf8,
            KeyOrder::Sorted,
            "{\n \"a\": {\n  \"c\": true,\n  \"d\": null,\n  \"\u{e9}\": false\n },\n \"b\": [\n  1,\n  -2.5,\n  \"caf\u{e9} \u{7f}\\n\",\n  [],\n  {}\n ],\n \"\u{1f600}\": 1e+21\n}",
        ),
        (
            Layout::Indent2,
            Charset::Ascii,
            KeyOrder::AsGiven,
            "{\n  \"b\": [\n    1,\n    -2.5,\n    \"caf\\u00e9 \\u007f\\n\",\n    [],\n    {}\n  ],\n  \"a\": {\n    \"d\": null,\n    \"c\": true,\n    \"\\u00e9\": false\n  },\n  \"\\ud83d\\ude00\": 1e+21\n}",
        ),
        (
            Layout::Indent2,
            Charset::Ascii,
            KeyOrder::Sorted,
            "{\n  \"a\": {\n    \"c\": true,\n    \"d\": null,\n    \"\\u00e9\": false\n  },\n  \"b\": [\n    1,\n    -2.5,\n    \"caf\\u00e9 \\u007f\\n\",\n    [],\n    {}\n  ],\n  \"\\ud83d\\ude00\": 1e+21\n}",
        ),
        (
            Layout::Indent2,
            Charset::Utf8,
            KeyOrder::AsGiven,
            "{\n  \"b\": [\n    1,\n    -2.5,\n    \"caf\u{e9} \u{7f}\\n\",\n    [],\n    {}\n  ],\n  \"a\": {\n    \"d\": null,\n    \"c\": true,\n    \"\u{e9}\": false\n  },\n  \"\u{1f600}\": 1e+21\n}",
        ),
        (
            Layout::Indent2,
            Charset::Utf8,
            KeyOrder::Sorted,
            "{\n  \"a\": {\n    \"c\": true,\n    \"d\": null,\n    \"\u{e9}\": false\n  },\n  \"b\": [\n    1,\n    -2.5,\n    \"caf\u{e9} \u{7f}\\n\",\n    [],\n    {}\n  ],\n  \"\u{1f600}\": 1e+21\n}",
        ),
    ];

    #[test]
    fn each_style_gives_the_bytes_of_json_dumps() {
        let sample = sample();

        for (layout, charset, key_order, text) in SAMPLE_TEXTS {
            let style = Style::new(layout, charset, key_order);

            assert_eq!(text_of(&sample, style), text, "{style:?}");
        }
    }

    #[test]
    fn the_table_of_the_styles_holds_each_style_one_time() {
        let listed: Vec<Style> = SAMPLE_TEXTS
            .iter()
            .map(|(layout, charset, key_order, _)| Style::new(*layout, *charset, *key_order))
            .collect();

        assert_eq!(listed, styles());
    }

    #[test]
    fn a_style_gives_back_its_three_parts() {
        for style in styles() {
            let again = Style::new(style.layout(), style.charset(), style.key_order());

            assert_eq!(again, style);
        }
    }

    #[test]
    fn a_float_is_written_as_python_writes_it() {
        let floats = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (1.25, "1.25"),
            (1.5, "1.5"),
            (-1.5, "-1.5"),
            (2.5, "2.5"),
            (10.0, "10.0"),
            (-12.25, "-12.25"),
            (100.0, "100.0"),
            (-123.456, "-123.456"),
            (123_456.789, "123456.789"),
            (123_456.789_012_345, "123456.789012345"),
            (123_456_789.0, "123456789.0"),
            (0.25, "0.25"),
            (0.21, "0.21"),
            (0.1, "0.1"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1.0 / 3.0, "0.3333333333333333"),
            (0.014, "0.014"),
            (0.002, "0.002"),
            (0.001, "0.001"),
            (0.000_123, "0.000123"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (0.000_012_5, "1.25e-05"),
            (1.5e-7, "1.5e-07"),
            (-1.5e-7, "-1.5e-07"),
            (2.5e-7, "2.5e-07"),
            (3e-10, "3e-10"),
            (5e-324, "5e-324"),
            (1e15, "1000000000000000.0"),
            (1_234_567_890_123_456.0, "1234567890123456.0"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1.234_567_890_123_456_8e16, "1.2345678901234568e+16"),
            (1.234_567_890_123_456_8e17, "1.2345678901234568e+17"),
            (1e21, "1e+21"),
            (1e22, "1e+22"),
            (1e100, "1e+100"),
            (-1e100, "-1e+100"),
            (1.5e300, "1.5e+300"),
            (f64::MAX, "1.7976931348623157e+308"),
            // The exact value of each float below is in the middle of two
            // shortest texts: it ends in 25, in 125, in 625 or in 75. Python
            // takes the text with the even last digit.
            (5_261_963_047_596_905.0 / 4.0, "1315490761899226.2"),
            (16_842_045_457_081.0 / 64.0, "263156960266.89062"),
            (1_673_733_205_695_233.0 + 0.25, "1673733205695233.2"),
            (-146_656_950_681_247.0 - 0.625, "-146656950681247.62"),
            (107_649_869_691_857.0 + 0.125, "107649869691857.12"),
            (-1_840_449_785_444_225.0 - 0.25, "-1840449785444225.2"),
            (1_679_558_812_176_105.0 + 0.25, "1679558812176105.2"),
            (1_125_899_906_842_624.0 + 0.25, "1125899906842624.2"),
            (123_456.0 + 2.0_f64.powi(-12), "123456.00024414062"),
            (669_758_432_410_385.0 + 0.25, "669758432410385.2"),
            (-669_758_432_410_385.0 - 0.25, "-669758432410385.2"),
            (20_278_963_822_605.0 + 0.3125, "20278963822605.312"),
            (163_151_449_678_028.0 + 0.625, "163151449678028.62"),
            (669_758_432_410_385.0 + 0.75, "669758432410385.8"),
            // A small power of two. It is a tie only when the lower text
            // reads back as the same float.
            (2.0_f64.powi(-25), "2.9802322387695312e-08"),
            (5.0 * 2.0_f64.powi(-23), "5.960464477539062e-07"),
            (2.0_f64.powi(-24), "5.960464477539063e-08"),
            (3.0 * 2.0_f64.powi(-25), "8.940696716308594e-08"),
        ];

        for (value, text) in floats {
            assert_eq!(float_repr(value).as_deref(), Ok(text));
            assert_eq!(text_of(&value, COMPACT), text);
            // The text reads back as the same float.
            assert_eq!(
                serde_json::from_str::<f64>(text).unwrap().to_bits(),
                value.to_bits()
            );
        }
    }

    #[test]
    fn a_float_that_is_not_finite_is_refused_and_never_null() {
        for float in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(float_repr(float), Err(WriteError::NotFinite));

            // `serde_json` alone writes `null` for the same float.
            assert_eq!(serde_json::to_string(&float).unwrap(), "null");

            // The float inside a list inside a map.
            let nested = Node::Table(vec![
                ("first", Node::Float(1.5)),
                (
                    "list",
                    Node::List(vec![Node::Float(0.5), Node::Float(float)]),
                ),
            ]);
            let in_a_map = BTreeMap::from([("list", vec![0.5, float])]);

            for style in styles() {
                assert_eq!(write(&float, style), Err(WriteError::NotFinite));
                assert_eq!(write(&[float], style), Err(WriteError::NotFinite));
                assert_eq!(write(&nested, style), Err(WriteError::NotFinite));
                assert_eq!(write(&in_a_map, style), Err(WriteError::NotFinite));
                assert_eq!(write(&Some(float), style), Err(WriteError::NotFinite));
            }
            assert_eq!(compact_len(&nested), Err(WriteError::NotFinite));
        }

        for float in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert_eq!(write(&float, COMPACT), Err(WriteError::NotFinite));
        }
    }

    #[test]
    fn an_f32_goes_out_as_the_f64_with_the_same_value() {
        assert_eq!(text_of(&0.5_f32, COMPACT), "0.5");
        assert_eq!(text_of(&-0.0_f32, COMPACT), "-0.0");
        // The nearest `f32` of 0.1 is a little above 0.1.
        assert_eq!(text_of(&0.1_f32, COMPACT), "0.10000000149011612");
    }

    #[test]
    fn an_empty_array_and_an_empty_object_have_no_white_space() {
        let no_items: [u8; 0] = [];
        let no_entries: BTreeMap<String, u8> = BTreeMap::new();
        let rows = [
            (Layout::Compact, "[[]]", "[{}]", "{\"a\":[]}"),
            (Layout::Spaced, "[[]]", "[{}]", "{\"a\": []}"),
            (
                Layout::Indent1,
                "[\n []\n]",
                "[\n {}\n]",
                "{\n \"a\": []\n}",
            ),
            (
                Layout::Indent2,
                "[\n  []\n]",
                "[\n  {}\n]",
                "{\n  \"a\": []\n}",
            ),
        ];

        for (layout, list_in_list, table_in_list, list_in_table) in rows {
            for key_order in KEY_ORDERS {
                let style = Style::new(layout, Charset::Ascii, key_order);

                assert_eq!(text_of(&no_items, style), "[]");
                assert_eq!(text_of(&no_entries, style), "{}");
                assert_eq!(text_of(&[no_items], style), list_in_list);
                assert_eq!(text_of(&[&no_entries], style), table_in_list);
                assert_eq!(
                    text_of(&BTreeMap::from([("a", no_items)]), style),
                    list_in_table
                );
            }
        }
    }

    /// Keys in an order that no sort gives. U+FFFF is before U+1F600 by
    /// code point, and after it by UTF-16 code unit.
    const KEYS: [&str; 11] = [
        "b",
        "a",
        "B",
        "",
        "\u{ffff}",
        "\u{1f600}",
        "\u{e9}",
        "aa",
        "a\u{0}",
        "~",
        "\u{7f}",
    ];

    fn keyed() -> Node {
        let entries = KEYS
            .into_iter()
            .zip(0..)
            .map(|(key, place)| (key, Node::Int(place)));

        Node::Table(entries.collect())
    }

    #[test]
    fn the_writer_sorts_the_keys_by_their_code_points() {
        // What `json.dumps` gives with `sort_keys=True`.
        assert_eq!(
            text_of(&keyed(), SORTED),
            "{\"\":3,\"B\":2,\"a\":1,\"a\\u0000\":8,\"aa\":7,\"b\":0,\"~\":9,\"\u{7f}\":10,\"\u{e9}\":6,\"\u{ffff}\":4,\"\u{1f600}\":5}"
        );
        assert_eq!(
            text_of(
                &keyed(),
                Style::new(Layout::Compact, Charset::Ascii, KeyOrder::Sorted)
            ),
            "{\"\":3,\"B\":2,\"a\":1,\"a\\u0000\":8,\"aa\":7,\"b\":0,\"~\":9,\"\\u007f\":10,\"\\u00e9\":6,\"\\uffff\":4,\"\\ud83d\\ude00\":5}"
        );
    }

    #[test]
    fn the_writer_keeps_the_order_of_the_keys_with_no_sort() {
        assert_eq!(
            text_of(&keyed(), COMPACT),
            "{\"b\":0,\"a\":1,\"B\":2,\"\":3,\"\u{ffff}\":4,\"\u{1f600}\":5,\"\u{e9}\":6,\"aa\":7,\"a\\u0000\":8,\"~\":9,\"\u{7f}\":10}"
        );
    }

    #[test]
    fn the_sort_does_not_come_from_the_map_type_of_the_value() {
        // A map of `serde_json` gives its keys in an order that a feature of
        // that crate decides. The sorted text is the same for each order.
        let value: serde_json::Value =
            serde_json::from_str(r#"{"b": {"z": 1, "y": 2}, "a": [{"d": 3, "c": 4}]}"#).unwrap();

        assert_eq!(
            text_of(&value, SORTED),
            r#"{"a":[{"c":4,"d":3}],"b":{"y":2,"z":1}}"#
        );
        assert_eq!(
            text_of(
                &Node::Table(vec![
                    (
                        "b",
                        Node::Table(vec![("z", Node::Int(1)), ("y", Node::Int(2))])
                    ),
                    (
                        "a",
                        Node::List(vec![Node::Table(vec![
                            ("d", Node::Int(3)),
                            ("c", Node::Int(4))
                        ])])
                    ),
                ]),
                SORTED
            ),
            r#"{"a":[{"c":4,"d":3}],"b":{"y":2,"z":1}}"#
        );
    }

    #[test]
    fn two_equal_keys_keep_the_order_of_the_value() {
        let twice = Node::Table(vec![
            ("b", Node::Int(1)),
            ("a", Node::Int(2)),
            ("b", Node::Int(3)),
            ("a", Node::Int(4)),
        ]);

        assert_eq!(text_of(&twice, SORTED), r#"{"a":2,"a":4,"b":1,"b":3}"#);
        assert_eq!(text_of(&twice, COMPACT), r#"{"b":1,"a":2,"b":3,"a":4}"#);
    }

    #[test]
    fn the_writer_counts_no_level_and_compares_no_key() {
        use super::super::{ByteCap, DEPTH_MAX, Rule, check};

        const CAP: ByteCap = ByteCap::new(4096);

        let rule = |text: &[u8]| check(text, CAP).err().map(|refusal| refusal.rule());
        let nested = |levels: usize| {
            let mut value = Node::List(Vec::new());
            for _ in 1..levels {
                value = Node::List(vec![value]);
            }

            write(&value, COMPACT).unwrap()
        };
        let twice = Node::Table(vec![("a", Node::Int(1)), ("a", Node::Int(2))]);

        assert_eq!(rule(&nested(DEPTH_MAX)), None);
        assert_eq!(rule(&nested(DEPTH_MAX + 1)), Some(Rule::TooDeep));
        assert_eq!(
            rule(&write(&twice, COMPACT).unwrap()),
            Some(Rule::DuplicateKey)
        );
    }

    #[derive(Serialize)]
    struct Inner {
        y: [u8; 0],
        x: f64,
    }

    #[derive(Serialize)]
    struct Outer {
        zone: &'static str,
        at: f64,
        #[serde(skip_serializing_if = "Option::is_none")]
        absent: Option<u8>,
        more: Inner,
    }

    #[test]
    fn a_struct_gives_its_fields_in_their_order_and_the_writer_sorts_them() {
        let value = Outer {
            zone: "b",
            at: 1.0,
            absent: None,
            more: Inner { y: [], x: 0.1 },
        };

        assert_eq!(
            text_of(&value, COMPACT),
            r#"{"zone":"b","at":1.0,"more":{"y":[],"x":0.1}}"#
        );
        assert_eq!(
            text_of(&value, SORTED),
            r#"{"at":1.0,"more":{"x":0.1,"y":[]},"zone":"b"}"#
        );
        assert_eq!(
            text_of(
                &value,
                Style::new(Layout::Indent1, Charset::Ascii, KeyOrder::Sorted)
            ),
            "{\n \"at\": 1.0,\n \"more\": {\n  \"x\": 0.1,\n  \"y\": []\n },\n \"zone\": \"b\"\n}"
        );
    }

    #[test]
    fn a_text_has_the_escapes_of_its_charset() {
        let text = "caf\u{e9} \"q\" \\ / \u{2028} \u{1f600} \u{7f}\u{0}\u{1f}\n\r\t\u{8}\u{c}\u{80}\u{ffff}~ \u{1b}";

        assert_eq!(
            text_of(
                text,
                Style::new(Layout::Compact, Charset::Ascii, KeyOrder::AsGiven)
            ),
            "\"caf\\u00e9 \\\"q\\\" \\\\ / \\u2028 \\ud83d\\ude00 \\u007f\\u0000\\u001f\\n\\r\\t\\b\\f\\u0080\\uffff~ \\u001b\""
        );
        assert_eq!(
            text_of(text, COMPACT),
            "\"caf\u{e9} \\\"q\\\" \\\\ / \u{2028} \u{1f600} \u{7f}\\u0000\\u001f\\n\\r\\t\\b\\f\u{80}\u{ffff}~ \\u001b\""
        );
        assert_eq!(text_of("", COMPACT), "\"\"");
        assert_eq!(text_of(&'\u{e9}', COMPACT), "\"\u{e9}\"");
        assert_eq!(
            text_of(
                &'\u{1f600}',
                Style::new(Layout::Compact, Charset::Ascii, KeyOrder::AsGiven)
            ),
            "\"\\ud83d\\ude00\""
        );
    }

    #[test]
    fn each_text_reads_back_as_the_same_text() {
        // Each character of the first three planes that is a `char`, in
        // runs of 64 characters.
        let characters: Vec<char> = (0..0x3_0000_u32).filter_map(char::from_u32).collect();

        for run in characters.chunks(64) {
            let text: String = run.iter().collect();

            for charset in CHARSETS {
                let style = Style::new(Layout::Compact, charset, KeyOrder::AsGiven);
                let written = write(&text, style).unwrap();

                assert_eq!(serde_json::from_slice::<String>(&written).unwrap(), text);
                if charset == Charset::Ascii {
                    assert!(written.iter().all(|byte| (b' '..=b'~').contains(byte)));
                }
            }
        }
    }

    #[test]
    fn an_integer_goes_out_as_its_digits() {
        assert_eq!(text_of(&i8::MIN, COMPACT), "-128");
        assert_eq!(text_of(&i16::MIN, COMPACT), "-32768");
        assert_eq!(text_of(&i32::MIN, COMPACT), "-2147483648");
        assert_eq!(text_of(&i64::MIN, COMPACT), "-9223372036854775808");
        assert_eq!(text_of(&u8::MAX, COMPACT), "255");
        assert_eq!(text_of(&u16::MAX, COMPACT), "65535");
        assert_eq!(text_of(&u32::MAX, COMPACT), "4294967295");
        assert_eq!(text_of(&u64::MAX, COMPACT), "18446744073709551615");
        assert_eq!(
            text_of(&9_007_199_254_740_993_u64, COMPACT),
            "9007199254740993"
        );
        assert_eq!(text_of(&0_i128, COMPACT), "0");
        assert_eq!(
            text_of(&i128::from(i64::MIN), COMPACT),
            "-9223372036854775808"
        );
        assert_eq!(
            text_of(&u128::from(u64::MAX), COMPACT),
            "18446744073709551615"
        );
        assert_eq!(
            text_of(&i128::from(u64::MAX), COMPACT),
            "18446744073709551615"
        );
    }

    #[test]
    fn an_integer_outside_the_range_of_the_reader_has_no_json_form() {
        let below = i128::from(i64::MIN) - 1;
        let above = i128::from(u64::MAX) + 1;

        assert_eq!(write(&below, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(write(&above, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(write(&i128::MIN, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(write(&i128::MAX, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(
            write(&(u128::from(u64::MAX) + 1), COMPACT),
            Err(WriteError::NoJsonForm)
        );
        assert_eq!(write(&u128::MAX, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(write(&[above], SORTED), Err(WriteError::NoJsonForm));
    }

    /// A value that gives a byte string to its serializer.
    struct Bytes(&'static [u8]);

    impl Serialize for Bytes {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            serializer.serialize_bytes(self.0)
        }
    }

    #[test]
    fn a_byte_string_has_no_json_form() {
        assert_eq!(write(&Bytes(b"ab"), COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(write(&Bytes(b""), COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(
            write(&BTreeMap::from([(Bytes(b"ab"), 1)]), COMPACT),
            Err(WriteError::NoJsonForm)
        );
    }

    impl PartialEq for Bytes {
        fn eq(&self, other: &Self) -> bool {
            self.0 == other.0
        }
    }

    impl Eq for Bytes {}

    impl PartialOrd for Bytes {
        fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
            Some(self.cmp(other))
        }
    }

    impl Ord for Bytes {
        fn cmp(&self, other: &Self) -> std::cmp::Ordering {
            self.0.cmp(other.0)
        }
    }

    #[derive(Serialize, PartialEq, Eq, PartialOrd, Ord)]
    enum Door {
        #[serde(rename = "tui")]
        Tui,
        #[serde(rename = "owui")]
        Owui,
    }

    #[derive(Serialize, PartialEq, Eq, PartialOrd, Ord)]
    struct Name(String);

    #[test]
    fn a_key_is_a_text() {
        assert_eq!(text_of(&BTreeMap::from([("a", 1)]), COMPACT), r#"{"a":1}"#);
        assert_eq!(
            text_of(&BTreeMap::from([(String::from("a"), 1)]), COMPACT),
            r#"{"a":1}"#
        );
        assert_eq!(text_of(&BTreeMap::from([('a', 1)]), COMPACT), r#"{"a":1}"#);
        assert_eq!(
            text_of(&BTreeMap::from([(Door::Owui, 1), (Door::Tui, 2)]), SORTED),
            r#"{"owui":1,"tui":2}"#
        );
        assert_eq!(
            text_of(&BTreeMap::from([(Name(String::from("a")), 1)]), SORTED),
            r#"{"a":1}"#
        );
    }

    #[derive(Serialize, PartialEq, Eq, PartialOrd, Ord)]
    enum Tagged {
        Wrap(u8),
    }

    #[test]
    fn a_key_that_is_no_text_has_no_json_form() {
        for style in [COMPACT, SORTED] {
            assert_eq!(
                write(&BTreeMap::from([(1_u8, 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(-1_i64, 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(1_u128, 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(true, 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(Some("a"), 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(None::<&str>, 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([((), 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(("a", "b"), 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(vec!["a"], 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(BTreeMap::from([("a", 1)]), 1)]), style),
                Err(WriteError::NoJsonForm)
            );
            assert_eq!(
                write(&BTreeMap::from([(Tagged::Wrap(1), 1)]), style),
                Err(WriteError::NoJsonForm)
            );
        }
    }

    /// A key that is a float. No map of the standard library takes one.
    struct FloatKey;

    impl Serialize for FloatKey {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            let mut map = serializer.serialize_map(Some(2))?;
            map.serialize_entry(&1.5_f64, &1)?;
            map.serialize_entry(&1.5_f32, &1)?;

            map.end()
        }
    }

    #[test]
    fn a_float_key_has_no_json_form() {
        assert_eq!(write(&FloatKey, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(write(&FloatKey, SORTED), Err(WriteError::NoJsonForm));
    }

    #[derive(Serialize)]
    enum Shape {
        Unit,
        Wrap(u8),
        Pair(u8, u8),
        Named { z: u8, a: Option<u8> },
    }

    #[derive(Serialize)]
    struct Marker;

    #[derive(Serialize)]
    struct Pair(u8, char);

    #[derive(Serialize)]
    struct Each {
        nothing: (),
        marker: Marker,
        absent: Option<u8>,
        present: Option<u8>,
        flags: (bool, bool),
        pair: Pair,
        letter: char,
        shapes: Vec<Shape>,
        wide: i128,
        name: Name,
    }

    fn shapes() -> Vec<Shape> {
        vec![
            Shape::Unit,
            Shape::Wrap(7),
            Shape::Pair(1, 2),
            Shape::Named { z: 1, a: None },
        ]
    }

    #[test]
    fn a_value_with_no_float_has_the_compact_text_of_serde_json() {
        let each = Each {
            nothing: (),
            marker: Marker,
            absent: None,
            present: Some(3),
            flags: (true, false),
            pair: Pair(9, 'x'),
            letter: '"',
            shapes: shapes(),
            wide: -5,
            name: Name(String::from("line\nbreak\u{1}")),
        };

        assert_eq!(
            write(&each, COMPACT).unwrap(),
            serde_json::to_vec(&each).unwrap()
        );
        assert_eq!(
            write(&sample_with_no_float(), COMPACT).unwrap(),
            serde_json::to_vec(&sample_with_no_float()).unwrap()
        );
    }

    fn sample_with_no_float() -> Node {
        Node::Table(vec![
            (
                "b",
                Node::List(vec![Node::Int(-1), Node::Text("caf\u{e9}")]),
            ),
            ("a", Node::Table(vec![("d", Node::Null)])),
        ])
    }

    #[test]
    fn a_variant_with_a_value_is_an_object_with_one_key() {
        let spaced = Style::new(Layout::Spaced, Charset::Ascii, KeyOrder::AsGiven);
        let indent1 = Style::new(Layout::Indent1, Charset::Ascii, KeyOrder::AsGiven);
        let indent2 = Style::new(Layout::Indent2, Charset::Ascii, KeyOrder::AsGiven);
        let indent2_sorted = Style::new(Layout::Indent2, Charset::Ascii, KeyOrder::Sorted);
        let mut all = shapes();
        all.push(Shape::Pair(3, 4));

        // What `json.dumps` gives for the same objects.
        assert_eq!(text_of(&Shape::Unit, indent2), "\"Unit\"");
        assert_eq!(text_of(&Shape::Wrap(7), COMPACT), r#"{"Wrap":7}"#);
        assert_eq!(text_of(&Shape::Wrap(7), spaced), r#"{"Wrap": 7}"#);
        assert_eq!(text_of(&Shape::Wrap(7), indent1), "{\n \"Wrap\": 7\n}");
        assert_eq!(text_of(&Shape::Wrap(7), indent2), "{\n  \"Wrap\": 7\n}");
        assert_eq!(text_of(&Shape::Pair(1, 2), COMPACT), r#"{"Pair":[1,2]}"#);
        assert_eq!(text_of(&Shape::Pair(1, 2), spaced), r#"{"Pair": [1, 2]}"#);
        assert_eq!(
            text_of(&Shape::Pair(1, 2), indent1),
            "{\n \"Pair\": [\n  1,\n  2\n ]\n}"
        );
        assert_eq!(
            text_of(&Shape::Pair(1, 2), indent2),
            "{\n  \"Pair\": [\n    1,\n    2\n  ]\n}"
        );
        assert_eq!(
            text_of(&Shape::Named { z: 1, a: None }, COMPACT),
            r#"{"Named":{"z":1,"a":null}}"#
        );
        assert_eq!(
            text_of(&Shape::Named { z: 1, a: None }, spaced),
            r#"{"Named": {"z": 1, "a": null}}"#
        );
        assert_eq!(
            text_of(&Shape::Named { z: 1, a: None }, indent1),
            "{\n \"Named\": {\n  \"z\": 1,\n  \"a\": null\n }\n}"
        );
        assert_eq!(
            text_of(&Shape::Named { z: 1, a: None }, indent2_sorted),
            "{\n  \"Named\": {\n    \"a\": null,\n    \"z\": 1\n  }\n}"
        );
        assert_eq!(
            text_of(&all, indent1),
            "[\n \"Unit\",\n {\n  \"Wrap\": 7\n },\n {\n  \"Pair\": [\n   1,\n   2\n  ]\n },\n {\n  \"Named\": {\n   \"z\": 1,\n   \"a\": null\n  }\n },\n {\n  \"Pair\": [\n   3,\n   4\n  ]\n }\n]"
        );
    }

    #[derive(Serialize)]
    struct Stored<'a> {
        seq: u8,
        body: &'a RawValue,
    }

    #[test]
    fn a_raw_value_is_refused_and_one_writer_keeps_its_text() {
        // White space, a number form and a key order that no style gives.
        let text = "{\"z\" : 1.50,\t\"a\":[ 1e2 ]}";
        let body: Box<RawValue> = serde_json::from_str(text).unwrap();
        let stored = Stored {
            seq: 7,
            body: &body,
        };

        for style in styles() {
            assert_eq!(write(&body, style), Err(WriteError::NoJsonForm));
            assert_eq!(write(&stored, style), Err(WriteError::NoJsonForm));
            assert_eq!(write_raw_kept(&body, style).unwrap(), text.as_bytes());
        }
        assert_eq!(compact_len(&body), Err(WriteError::NoJsonForm));
        assert_eq!(
            String::from_utf8(write_raw_kept(&stored, COMPACT).unwrap()).unwrap(),
            format!("{{\"seq\":7,\"body\":{text}}}")
        );
        assert_eq!(
            String::from_utf8(write_raw_kept(&stored, SORTED).unwrap()).unwrap(),
            format!("{{\"body\":{text},\"seq\":7}}")
        );
        // The same bytes as `serde_json` gives for the value.
        assert_eq!(
            write_raw_kept(&stored, COMPACT).unwrap(),
            serde_json::to_vec(&stored).unwrap()
        );
    }

    /// A struct with a name of its own and a given count of fields with the
    /// name of the struct.
    struct Private(&'static str, usize);

    impl Serialize for Private {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            let mut fields = serializer.serialize_struct(self.0, self.1)?;
            for _ in 0..self.1 {
                fields.serialize_field(self.0, "7")?;
            }

            fields.end()
        }
    }

    #[test]
    fn a_struct_with_a_private_name_of_serde_json_is_refused() {
        // The name of a number of `serde_json` with the feature
        // `arbitrary_precision`. The workspace does not turn it on.
        let number = Private("$serde_json::private::Number", 1);
        let other = Private("$serde_json::private::Other", 1);

        assert_eq!(write(&number, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(
            write_raw_kept(&number, COMPACT),
            Err(WriteError::NoJsonForm)
        );
        assert_eq!(write_raw_kept(&other, COMPACT), Err(WriteError::NoJsonForm));
        assert_eq!(text_of(&Private("plain", 1), COMPACT), r#"{"plain":"7"}"#);
    }

    #[test]
    fn a_raw_value_has_one_field_with_its_text() {
        assert_eq!(
            write_raw_kept(&Private(RAW_VALUE, 1), COMPACT).unwrap(),
            b"7"
        );
        assert_eq!(
            write_raw_kept(&Private(RAW_VALUE, 0), COMPACT),
            Err(WriteError::NoJsonForm)
        );
        assert_eq!(
            write_raw_kept(&Private(RAW_VALUE, 2), COMPACT),
            Err(WriteError::NoJsonForm)
        );
    }

    /// A raw value whose one field has another name, or holds no text.
    struct WrongRaw(&'static str, bool);

    impl Serialize for WrongRaw {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            let mut fields = serializer.serialize_struct(RAW_VALUE, 1)?;
            if self.1 {
                fields.serialize_field(self.0, "7")?;
            } else {
                fields.serialize_field(self.0, &7)?;
            }

            fields.end()
        }
    }

    #[test]
    fn a_raw_value_with_another_field_is_refused() {
        assert_eq!(
            write_raw_kept(&WrongRaw("text", true), COMPACT),
            Err(WriteError::NoJsonForm)
        );
        assert_eq!(
            write_raw_kept(&WrongRaw(RAW_VALUE, false), COMPACT),
            Err(WriteError::NoJsonForm)
        );
        assert_eq!(
            write_raw_kept(&WrongRaw(RAW_VALUE, true), COMPACT).unwrap(),
            b"7"
        );
    }

    /// What a map gives to its serializer, step by step.
    #[derive(Clone, Copy)]
    enum Step {
        Key,
        Value,
    }

    struct Steps(&'static [Step]);

    impl Serialize for Steps {
        fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
            let mut map = serializer.serialize_map(None)?;
            for step in self.0 {
                match step {
                    Step::Key => map.serialize_key("k")?,
                    Step::Value => map.serialize_value(&1)?,
                }
            }

            map.end()
        }
    }

    #[test]
    fn a_map_gives_one_value_after_each_key() {
        let refused: [&[Step]; 5] = [
            &[Step::Value],
            &[Step::Key],
            &[Step::Key, Step::Key],
            &[Step::Key, Step::Value, Step::Value],
            &[Step::Key, Step::Value, Step::Key],
        ];

        for style in [COMPACT, SORTED] {
            assert_eq!(text_of(&Steps(&[]), style), "{}");
            assert_eq!(
                text_of(&Steps(&[Step::Key, Step::Value]), style),
                r#"{"k":1}"#
            );
            for steps in refused {
                assert_eq!(write(&Steps(steps), style), Err(WriteError::NoJsonForm));
            }
        }
    }

    /// A value whose `Serialize` gives an error of its own.
    struct Fails;

    impl Serialize for Fails {
        fn serialize<S: Serializer>(&self, _: S) -> Result<S::Ok, S::Error> {
            Err(ser::Error::custom("hunter2"))
        }
    }

    #[test]
    fn an_error_of_a_value_has_no_part_of_the_value() {
        let error = write(&[Fails], COMPACT).unwrap_err();

        assert_eq!(error, WriteError::NoJsonForm);
        assert_eq!(error.to_string(), "a value with no JSON form");
        assert_eq!(format!("{error:?}"), "NoJsonForm");
        assert_eq!(
            WriteError::NotFinite.to_string(),
            "a float that is not finite"
        );
    }

    #[test]
    fn the_compact_length_is_the_count_of_the_bytes_of_the_compact_form() {
        assert_eq!(
            compact_len(&sample()),
            Ok(write(&sample(), COMPACT).unwrap().len())
        );
        assert_eq!(
            compact_len(&keyed()),
            Ok(write(&keyed(), COMPACT).unwrap().len())
        );
        assert_eq!(compact_len(&()), Ok(4));
        assert_eq!(compact_len("\u{e9}\n"), Ok(6));
    }
}
