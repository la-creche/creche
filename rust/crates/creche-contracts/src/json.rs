//! Strict JSON: the one reader of a JSON text of a contract.
//!
//! `rust/AGENTS.md`, "JSON", says why a text of a contract is strict JSON.
//! This doc comment is the full text of the rules. [`check`] accepts a byte
//! string only when each of these eight rules holds.
//!
//! 1. Size. The text is not longer than the [`ByteCap`] of its surface. The
//!    cap comes from the caller. The module holds no default.
//! 2. Encoding. The bytes are valid UTF-8. An overlong form is not valid
//!    UTF-8, and the three bytes of a surrogate are not. The first character
//!    is not U+FEFF, the byte order mark.
//! 3. Grammar. The bytes are a single value in the grammar of RFC 8259,
//!    section 2, with optional white space before it and after it. Only a
//!    space, a tab, a line feed and a carriage return are white space.
//!    `true`, `false` and `null` have lower-case letters. The text ends
//!    after the white space that follows the value.
//! 4. Strings. A string has no raw character below U+0020. An escape is
//!    `\"`, `\\`, `\/`, `\b`, `\f`, `\n`, `\r`, `\t`, or `\u` and four hex
//!    digits in upper or lower case. A `\u` escape from D800 to DBFF needs
//!    a `\u` escape from DC00 to DFFF as the next six bytes. An escape from
//!    DC00 to DFFF is legal in that place only. `\u0000` and the escape of
//!    a noncharacter are legal.
//! 5. Keys. Each key of an object is in that object one time only. The
//!    check compares two keys by their code points, after it decodes each
//!    escape. It does not normalize a key.
//! 6. Depth. A text nests [`DEPTH_MAX`] levels of arrays and objects, or
//!    less.
//! 7. Integers. A number that has neither a fraction nor an exponent is an
//!    integer. Its value fits an `i64` or a `u64`: the least value is
//!    -9223372036854775808, and the largest is 18446744073709551615. `-0`
//!    is the integer 0.
//! 8. Floats. Each other number is a float. The nearest `f64` of its text
//!    is finite. The check thus refuses `1e999`, and `1e-999` is 0.
//!
//! The top-level value can have each kind: the text `7` is strict JSON. A
//! caller that takes only an object reads [`StrictText::top`] and gives its
//! own error for another kind.
//!
//! # Which rule and which offset a refusal names
//!
//! The size check runs first. The encoding check then reads all the bytes:
//! UTF-8 first, then the mark. The six other rules share one pass from left
//! to right, and the pass ends at the lowest offset where a rule fails. The
//! [`NotStrict`] of a text holds that rule and that offset.
//!
//! | [`Rule`] | The offset |
//! |---|---|
//! | `TooLarge` | The cap. |
//! | `NotUtf8` | The first byte that is not UTF-8. |
//! | `ByteOrderMark` | 0. |
//! | `Syntax` | The first byte that no JSON text can have there. For a text that ends too early, the count of its bytes. For an escape, the `\` of the escape. |
//! | `Constant` | The first byte of `NaN`, of `Infinity` or of `-Infinity`. |
//! | `TrailingData` | The first byte after the value that is not white space. |
//! | `TooDeep` | The bracket that opens level 65. |
//! | `DuplicateKey` | The first quote of the second key. |
//! | `LoneSurrogate` | The `\` of the escape that has no partner. |
//! | `IntegerRange`, `FloatRange` | The first byte of the number. |
//!
//! A number is the longest run of digits and of the bytes `+`, `-`, `.`, `e`
//! and `E`. A run that is not a number of RFC 8259 is `Syntax`: `01`, `1.`
//! and `1.5.2`. A literal has no such run: `truex` is `true` and then
//! `TrailingData`.
//!
//! # How a module reads a JSON text
//!
//! The reader does steps 1 and 3. The module writes steps 2 and 4.
//!
//! 1. [`check`] reads the bytes, with the cap of the surface.
//! 2. The module compares [`StrictText::top`] with the kind that it takes.
//! 3. [`StrictText::parse`] fills the raw type. A raw type of this crate has
//!    a `Slot` at each field, so a value of a wrong kind does not fail this
//!    step.
//! 4. One conversion makes the valid type from the raw type.
//!
//! Each module has an error type of its own. It turns a [`NotStrict`] and a
//! [`Shape`] into that type. A module can tell three rules apart for its
//! caller, for example with a status for each: [`Rule::TooLarge`],
//! [`Rule::NotUtf8`] and [`Rule::TooDeep`].
//!
//! A peer does not learn which rule a text broke. Only the operator does,
//! from a notice. For that notice, the error of a module must not drop the
//! [`NotStrict`]. The error stores the value and returns it from a method
//! `not_strict(&self) -> Option<&NotStrict>`. The service calls the method
//! when it records the notice.
//!
//! ```
//! use creche_contracts::json::{self, ByteCap, Found, NotStrict, Number, Shape};
//! use serde::Deserialize;
//!
//! const CAP: ByteCap = ByteCap::new(4096);
//!
//! #[derive(Deserialize)]
//! struct RawQuota {
//!     #[serde(default)]
//!     turns: Option<Number>,
//! }
//!
//! struct Quota(u64);
//!
//! enum QuotaError {
//!     NotJson(NotStrict),
//!     NotObject,
//!     Shape(Shape),
//!     NoTurns,
//! }
//!
//! impl QuotaError {
//!     fn not_strict(&self) -> Option<&NotStrict> {
//!         match self {
//!             Self::NotJson(refusal) => Some(refusal),
//!             Self::NotObject | Self::Shape(_) | Self::NoTurns => None,
//!         }
//!     }
//! }
//!
//! fn quota(bytes: &[u8]) -> Result<Quota, QuotaError> {
//!     let text = json::check(bytes, CAP).map_err(QuotaError::NotJson)?;
//!     if text.top() != Found::Table {
//!         return Err(QuotaError::NotObject);
//!     }
//!     let raw: RawQuota = text.parse().map_err(QuotaError::Shape)?;
//!
//!     match raw.turns {
//!         Some(Number::Integer(turns)) => turns.to_u64().map(Quota).ok_or(QuotaError::NoTurns),
//!         Some(Number::Float(_)) | None => Err(QuotaError::NoTurns),
//!     }
//! }
//!
//! assert!(matches!(quota(br#"{"turns": 40}"#), Ok(Quota(40))));
//! assert!(matches!(quota(b"[40]"), Err(QuotaError::NotObject)));
//! assert!(matches!(quota(br#"{"turns": 4e1}"#), Err(QuotaError::NoTurns)));
//!
//! let twice = quota(br#"{"turns": 40, "turns": 41}"#).err();
//! let refusal = twice.as_ref().and_then(QuotaError::not_strict);
//! assert_eq!(refusal.map(NotStrict::rule), Some(json::Rule::DuplicateKey));
//! ```
//!
//! This module holds the reader only. It has no writer yet.

mod read;
mod scan;

use std::error::Error;
use std::fmt;

use serde::Deserialize;

pub use self::read::{Integer, Number, ReadError, Shape, read};
/// The kind of a JSON value. The module `slot` of this crate defines it, and
/// this module adds no second enum for a kind.
pub use crate::slot::Found;

/// The deepest nesting of arrays and objects in a strict text.
///
/// A text of 64 arrays, each one inside the one before, passes the check.
/// One more level does not. No contract names a nesting limit for a JSON
/// text. `rust/AGENTS.md`, "JSON", sets this one limit for each text.
pub const DEPTH_MAX: usize = 64;

/// U+FEFF, the byte order mark. In UTF-8 it is the bytes EF BB BF.
const BYTE_ORDER_MARK: char = '\u{feff}';

/// The most bytes that the JSON text of one surface has.
///
/// The caller of [`check`] gives the cap of its surface. The type has no
/// `Default`: no one cap fits each surface.
///
/// ```
/// use creche_contracts::json::{self, ByteCap};
///
/// const CAP: ByteCap = ByteCap::new(4);
///
/// assert!(json::check(b"1234", CAP).is_ok());
/// assert!(json::check(b"12345", CAP).is_err());
/// ```
///
/// Code outside this module builds a cap only with [`ByteCap::new`]:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, ByteCap};
///
/// const CAP: ByteCap = ByteCap { bytes: 4 };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ByteCap {
    bytes: usize,
}

impl ByteCap {
    /// A cap of `bytes` bytes. A text of exactly that count is under the cap.
    #[must_use]
    pub const fn new(bytes: usize) -> Self {
        Self { bytes }
    }
}

/// The rule that a refused text breaks.
///
/// The word of a rule goes into a log line and into the notice file of a
/// service, so a rule crosses a process boundary. The set is closed. A
/// reader refuses a word that is not in the set: [`Rule::from_word`] gives
/// `None` for it, and the type has no variant for an unknown word.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Rule {
    /// The text has more bytes than the cap of its surface.
    TooLarge,
    /// The bytes are not UTF-8. UTF-16 and UTF-32 with a byte order mark are
    /// in this rule too.
    NotUtf8,
    /// The text starts with the byte order mark of UTF-8.
    ByteOrderMark,
    /// The text is not one JSON text of RFC 8259.
    Syntax,
    /// The text holds the word `NaN`, `Infinity` or `-Infinity` in the place
    /// of a value.
    Constant,
    /// A byte that is not white space follows the value.
    TrailingData,
    /// The text nests more than [`DEPTH_MAX`] levels of arrays and objects.
    TooDeep,
    /// One object holds a key two times.
    DuplicateKey,
    /// A string holds the escape of one half of a surrogate pair, with no
    /// partner.
    LoneSurrogate,
    /// An integer is outside the range from the smallest `i64` to the
    /// largest `u64`.
    IntegerRange,
    /// A number with a fraction or an exponent is not finite as a float of
    /// 64 bits.
    FloatRange,
}

impl Rule {
    /// Each rule, in the order of the variants.
    const ALL: [Self; 11] = [
        Self::TooLarge,
        Self::NotUtf8,
        Self::ByteOrderMark,
        Self::Syntax,
        Self::Constant,
        Self::TrailingData,
        Self::TooDeep,
        Self::DuplicateKey,
        Self::LoneSurrogate,
        Self::IntegerRange,
        Self::FloatRange,
    ];

    /// The word of the rule in a log line and in a notice file: the name of
    /// the variant in snake case.
    #[must_use]
    pub const fn word(self) -> &'static str {
        match self {
            Self::TooLarge => "too_large",
            Self::NotUtf8 => "not_utf8",
            Self::ByteOrderMark => "byte_order_mark",
            Self::Syntax => "syntax",
            Self::Constant => "constant",
            Self::TrailingData => "trailing_data",
            Self::TooDeep => "too_deep",
            Self::DuplicateKey => "duplicate_key",
            Self::LoneSurrogate => "lone_surrogate",
            Self::IntegerRange => "integer_range",
            Self::FloatRange => "float_range",
        }
    }

    /// The rule that has this word. The function is the exact reverse of
    /// [`Rule::word`]: it takes no other letter case and no white space.
    ///
    /// `None` for each other text. The reader of a notice file and a service
    /// that reads the rule word of a log line refuse such a word.
    ///
    /// ```
    /// use creche_contracts::json::Rule;
    ///
    /// assert_eq!(Rule::from_word("lone_surrogate"), Some(Rule::LoneSurrogate));
    /// assert_eq!(Rule::from_word("LoneSurrogate"), None);
    /// ```
    #[must_use]
    pub fn from_word(word: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|rule| rule.word() == word)
    }
}

/// Why [`check`] refuses a text: the rule that the text breaks, and the
/// place.
///
/// The two fields are a [`Rule`] and a number. A log line or a notice can
/// thus print the value, also when the text holds a secret. The module doc
/// has the table of the offsets.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, NotStrict, Rule};
///
/// let refusal: NotStrict = json::check(b"[1,]", ByteCap::new(64)).unwrap_err();
///
/// assert_eq!(refusal.rule(), Rule::Syntax);
/// assert_eq!(refusal.at(), 3);
/// assert_eq!(refusal.to_string(), "syntax at byte 3");
/// ```
///
/// Only [`check`] builds a value:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, ByteCap, NotStrict, Rule};
///
/// let refusal = NotStrict { rule: Rule::Syntax, at: 3 };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotStrict {
    rule: Rule,
    at: usize,
}

impl NotStrict {
    const fn new(rule: Rule, at: usize) -> Self {
        Self { rule, at }
    }

    /// The rule that the text breaks.
    #[must_use]
    pub const fn rule(&self) -> Rule {
        self.rule
    }

    /// The byte offset of the refusal, from 0.
    #[must_use]
    pub const fn at(&self) -> usize {
        self.at
    }
}

impl fmt::Display for NotStrict {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte {}", self.rule.word(), self.at)
    }
}

impl Error for NotStrict {}

/// A text that passed [`check`]. A value exists only as the result of that
/// function, so code that holds one needs no second check.
///
/// The `Debug` form shows the length and the kind of the top-level value.
/// It shows no character of the text, because a text can hold a secret.
///
/// ```
/// use creche_contracts::json::{self, ByteCap, Found, StrictText};
///
/// let text: StrictText<'_> = json::check(b" [1, 2]\n", ByteCap::new(64))?;
///
/// assert_eq!(text.as_str(), " [1, 2]\n");
/// assert_eq!(text.top(), Found::List);
/// assert_eq!(text.parse::<Vec<u8>>()?, [1, 2]);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot give a text that proof:
///
/// ```compile_fail,E0451
/// use creche_contracts::json::{self, ByteCap, Found, StrictText};
///
/// let text = StrictText { text: "[1, 2,]", top: Found::List };
/// ```
#[derive(Clone, Copy)]
pub struct StrictText<'a> {
    text: &'a str,
    top: Found,
}

impl<'a> StrictText<'a> {
    /// The whole text, with the white space around the value.
    #[must_use]
    pub const fn as_str(&self) -> &'a str {
        self.text
    }

    /// The kind of the top-level value. A number is [`Found::Integer`] when
    /// its token has no `.`, no `e` and no `E`. Each other number is
    /// [`Found::Float`].
    #[must_use]
    pub const fn top(&self) -> Found {
        self.top
    }

    /// The text as a `T`, through `serde_json`.
    ///
    /// The read of a strict text fails only when `T` refuses the value. A
    /// raw type with a `Slot` at each field refuses no object.
    ///
    /// # Errors
    ///
    /// [`Shape`] when `T` does not take the value of the text.
    pub fn parse<T: Deserialize<'a>>(&self) -> Result<T, Shape> {
        serde_json::from_str(self.text).map_err(|error| Shape::of(&error))
    }
}

impl fmt::Debug for StrictText<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StrictText")
            .field("bytes", &self.text.len())
            .field("top", &self.top)
            .finish()
    }
}

/// Reads `bytes` against each rule of the module doc.
///
/// Each rule applies to each byte. A raw type can skip a member of an
/// object, and the check does not: a fault inside that member refuses the
/// text.
///
/// # Errors
///
/// [`NotStrict`] with the first rule that the text breaks.
pub fn check(bytes: &[u8], cap: ByteCap) -> Result<StrictText<'_>, NotStrict> {
    if bytes.len() > cap.bytes {
        return Err(NotStrict::new(Rule::TooLarge, cap.bytes));
    }

    let text = std::str::from_utf8(bytes)
        .map_err(|error| NotStrict::new(Rule::NotUtf8, error.valid_up_to()))?;
    if text.starts_with(BYTE_ORDER_MARK) {
        return Err(NotStrict::new(Rule::ByteOrderMark, 0));
    }

    let top = scan::pass(text)?;

    Ok(StrictText { text, top })
}

#[cfg(test)]
mod tests {
    use super::*;

    const ROOMY: ByteCap = ByteCap::new(1024);

    fn refusal(bytes: &[u8], cap: ByteCap) -> (Rule, usize) {
        let refused = check(bytes, cap).unwrap_err();

        (refused.rule(), refused.at())
    }

    /// The place of a rule in [`Rule::ALL`]. The match fails the build for a
    /// new variant: give it the next place here and in the list.
    const fn place(rule: Rule) -> usize {
        match rule {
            Rule::TooLarge => 0,
            Rule::NotUtf8 => 1,
            Rule::ByteOrderMark => 2,
            Rule::Syntax => 3,
            Rule::Constant => 4,
            Rule::TrailingData => 5,
            Rule::TooDeep => 6,
            Rule::DuplicateKey => 7,
            Rule::LoneSurrogate => 8,
            Rule::IntegerRange => 9,
            Rule::FloatRange => 10,
        }
    }

    #[test]
    fn the_list_holds_each_rule_one_time() {
        for (index, rule) in Rule::ALL.into_iter().enumerate() {
            assert_eq!(place(rule), index);
        }
    }

    #[test]
    fn the_word_of_each_rule_reads_back_as_the_rule() {
        let words = [
            "too_large",
            "not_utf8",
            "byte_order_mark",
            "syntax",
            "constant",
            "trailing_data",
            "too_deep",
            "duplicate_key",
            "lone_surrogate",
            "integer_range",
            "float_range",
        ];

        for (rule, word) in Rule::ALL.into_iter().zip(words) {
            assert_eq!(rule.word(), word);
            assert_eq!(Rule::from_word(word), Some(rule));
            assert_eq!(Rule::from_word(rule.word()), Some(rule));
        }
        assert_eq!(words.len(), Rule::ALL.len());
    }

    #[test]
    fn another_text_is_the_word_of_no_rule() {
        for text in [
            "",
            "Syntax",
            "SYNTAX",
            "syntax ",
            " syntax",
            "syntax\n",
            "TooLarge",
            "too-large",
            "too large",
            "toolarge",
            "not_utf_8",
            "unknown",
            "lone_surrogate\0",
            "syntax\u{feff}",
        ] {
            assert_eq!(Rule::from_word(text), None, "{text:?}");
        }
    }

    /// Rule 1: the text, the cap and the offset, which is the cap.
    const TOO_LARGE: [(&[u8], usize, usize); 5] = [
        (b"12345", 4, 4),
        (b"1", 0, 0),
        (b"[1, 2, 3]", 8, 8),
        // The size rule is first: no later rule reads a text over its cap.
        (b"\xff\xff\xff", 2, 2),
        (b"nope", 1, 1),
    ];

    #[test]
    fn a_text_over_its_cap_is_too_large() {
        for (text, cap, at) in TOO_LARGE {
            assert_eq!(
                refusal(text, ByteCap::new(cap)),
                (Rule::TooLarge, at),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_text_of_exactly_its_cap_is_under_the_cap() {
        assert!(check(b"12345", ByteCap::new(5)).is_ok());
        assert!(check(b"[1, 2, 3]", ByteCap::new(9)).is_ok());
        // The empty text is under each cap. It is no JSON text.
        assert_eq!(refusal(b"", ByteCap::new(0)), (Rule::Syntax, 0));
    }

    /// Rule 2: the text and the offset of the first byte that is not UTF-8.
    const NOT_UTF8: [(&[u8], usize); 14] = [
        (b"\xff", 0),
        (b"\x80", 0),
        // The bytes of U+D800, the first half of a surrogate pair.
        (b"\"\xed\xa0\x80\"", 1),
        // The bytes of U+DFFF, the last second half.
        (b"\"\xed\xbf\xbf\"", 1),
        // An overlong form of `/`, and one of U+0000.
        (b"\"\xc0\xaf\"", 1),
        (b"\"\xe0\x80\x80\"", 1),
        // A code point past U+10FFFF.
        (b"\"\xf4\x90\x80\x80\"", 1),
        // A character that the text cuts.
        (b"\"\xc3", 1),
        // UTF-16 and UTF-32 with a byte order mark.
        (b"\xff\xfe1\x00", 0),
        (b"\xfe\xff\x001", 0),
        (b"\xff\xfe\x00\x001\x00\x00\x00", 0),
        (b"\x00\x00\xfe\xff\x00\x00\x001", 2),
        // The encoding rule reads the whole text before the pass starts.
        (b"nope\xff", 4),
        // The UTF-8 check is before the check of the mark.
        (b"\xef\xbb\xbf\xff", 3),
    ];

    #[test]
    fn bytes_that_are_not_utf8_are_refused() {
        for (text, at) in NOT_UTF8 {
            assert_eq!(refusal(text, ROOMY), (Rule::NotUtf8, at), "{text:?}");
        }
    }

    /// Rule 2: a text that starts with the mark. The offset is 0.
    const MARKED: [&[u8]; 4] = [
        b"\xef\xbb\xbf",
        b"\xef\xbb\xbf1",
        b"\xef\xbb\xbf{}",
        b"\xef\xbb\xbfnope",
    ];

    #[test]
    fn a_text_that_starts_with_the_mark_is_refused() {
        for text in MARKED {
            assert_eq!(refusal(text, ROOMY), (Rule::ByteOrderMark, 0), "{text:?}");
        }
    }

    #[test]
    fn the_mark_in_another_place_is_a_character() {
        assert!(check("\"\u{feff}\"".as_bytes(), ROOMY).is_ok());
        assert_eq!(
            refusal("1\u{feff}".as_bytes(), ROOMY),
            (Rule::TrailingData, 1)
        );
        assert_eq!(refusal(" \u{feff}1".as_bytes(), ROOMY), (Rule::Syntax, 1));
    }

    #[test]
    fn a_text_of_utf_16_with_no_mark_is_no_json_text() {
        // Each second byte is U+0000, which is UTF-8 and no white space.
        assert_eq!(refusal(b"1\x00", ROOMY), (Rule::TrailingData, 1));
        assert_eq!(refusal(b"{\x00}\x00", ROOMY), (Rule::Syntax, 1));
        assert_eq!(refusal(b"\x001", ROOMY), (Rule::Syntax, 0));
        assert_eq!(refusal(b"\x00\x00\x001", ROOMY), (Rule::Syntax, 0));
    }

    #[test]
    fn an_accepted_text_keeps_its_bytes_and_the_kind_of_its_value() {
        let texts = [
            ("null", Found::Null),
            ("true", Found::Boolean),
            ("false", Found::Boolean),
            ("7", Found::Integer),
            ("-0", Found::Integer),
            ("-0.0", Found::Float),
            ("1e3", Found::Float),
            ("7E0", Found::Float),
            ("\"7\"", Found::Text),
            ("[7]", Found::List),
            ("{\"count\": 7}", Found::Table),
            (" \t\r\n[7] \t\r\n", Found::List),
        ];

        for (text, kind) in texts {
            let strict = check(text.as_bytes(), ROOMY).unwrap();

            assert_eq!(strict.as_str(), text);
            assert_eq!(strict.top(), kind, "{text}");
        }
    }

    #[test]
    fn the_debug_form_holds_no_byte_of_the_text() {
        let strict = check(br#"{"token": "hunter2"}"#, ROOMY).unwrap();

        assert_eq!(
            format!("{strict:?}"),
            "StrictText { bytes: 20, top: Table }"
        );
    }

    #[test]
    fn a_refusal_prints_its_word_and_its_offset() {
        let refused = check(br#"["hunter2", "\ud800"]"#, ROOMY).unwrap_err();

        assert_eq!(refused.to_string(), "lone_surrogate at byte 13");
        assert_eq!(
            format!("{refused:?}"),
            "NotStrict { rule: LoneSurrogate, at: 13 }"
        );
        assert_eq!(refused.clone(), refused);
    }

    #[test]
    fn a_strict_text_parses_into_a_type_that_borrows() {
        #[derive(Debug, PartialEq, Deserialize)]
        struct Named<'a> {
            name: &'a str,
        }

        let bytes = br#"{"name": "thin"}"#.to_vec();
        let strict = check(&bytes, ROOMY).unwrap();

        assert_eq!(strict.parse::<Named<'_>>(), Ok(Named { name: "thin" }));
    }
}
