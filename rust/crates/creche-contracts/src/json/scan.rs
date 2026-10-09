//! The pass of `check` over a text that is UTF-8.
//!
//! The pass goes through the text one time, from left to right, and ends at
//! the lowest offset where a rule fails. The module doc of `json` names the
//! offset of each rule.
//!
//! No function here calls itself. A `Vec` holds one entry for each array and
//! each object that is open, and it never has more than `DEPTH_MAX` entries.
//! A text of each depth thus needs the same stack.
//!
//! Only a key needs its decoded text, for the comparison with the other
//! keys of its object. A key with no escape borrows from the input. The
//! pass checks the escapes of each other string and keeps no text of it.
//!
//! Four rules have one function each: `depth`, `duplicate_key`,
//! `integer_range` and `float_range`. The module also holds what a number
//! token is: [`is_integer`], [`integer_value`] and [`float_value`]. The
//! number types of `json` read a token with the same three functions.
//!
//! The module is the one home of the grammar. The writer and the walk over
//! an opaque value import each of these items and hold no copy:
//! [`FIRST_PLAIN`], [`HEX_DIGITS`], [`HEX`], [`is_space`], [`in_number`],
//! the three words [`TRUE`], [`FALSE`] and [`NULL`], and [`string_at`].

use std::borrow::Cow;
use std::collections::HashSet;

use super::{DEPTH_MAX, Found, NotStrict, Rule};

/// The words that are a number in some JSON dialects and no JSON value.
const CONSTANTS: [&[u8]; 3] = [b"NaN", b"Infinity", b"-Infinity"];

/// The first code unit of the first halves of a surrogate pair, and of the
/// second halves, and the last code unit of each range.
const FIRST_HALF_MIN: u16 = 0xD800;
const FIRST_HALF_MAX: u16 = 0xDBFF;
const SECOND_HALF_MIN: u16 = 0xDC00;
const SECOND_HALF_MAX: u16 = 0xDFFF;

/// The first code point that a surrogate pair holds, and the count of bits
/// that each half gives.
const PAIRS_START: u32 = 0x1_0000;
const HALF_BITS: u32 = 10;

/// The count of hex digits of a `\u` escape, and the base of a hex digit.
pub(super) const HEX_DIGITS: usize = 4;
pub(super) const HEX: u32 = 16;

/// The first character that a string can hold with no escape.
pub(super) const FIRST_PLAIN: u8 = 0x20;

/// The three words of JSON.
pub(super) const TRUE: &str = "true";
pub(super) const FALSE: &str = "false";
pub(super) const NULL: &str = "null";

/// The keys of one open object, each one after the decode of its escapes.
type Keys<'a> = HashSet<Cow<'a, str>>;

/// One open array or object.
enum Open<'a> {
    List,
    Table(Keys<'a>),
}

/// Where the cursor is in the open array or object.
enum Place {
    /// Directly after the bracket that opens it.
    Start,
    /// After a value.
    AfterValue,
}

/// What the reader of a string keeps.
enum Keep {
    /// No character. The reader checks the string only.
    Nothing,
    /// The text of the string, after the decode of each escape.
    Text,
}

/// Reads `text` against rules 3 to 8 of the module doc of `json`.
///
/// The result is the kind of the top-level value.
pub(super) fn pass(text: &str) -> Result<Found, NotStrict> {
    let mut cursor = Cursor { text, at: 0 };
    let mut open: Vec<Open<'_>> = Vec::new();

    cursor.skip_space();
    let (top, mut place) = cursor.value(&mut open)?;

    loop {
        cursor.skip_space();
        let Some(container) = open.last_mut() else {
            break;
        };

        let more = match (container, place) {
            (Open::List, Place::Start) => !cursor.takes(b']'),
            (Open::List, Place::AfterValue) => cursor.item_follows()?,
            (Open::Table(keys), Place::Start) => cursor.first_key(keys)?,
            (Open::Table(keys), Place::AfterValue) => cursor.next_key(keys)?,
        };
        if more {
            cursor.skip_space();
            place = cursor.value(&mut open)?.1;
        } else {
            open.pop();
            place = Place::AfterValue;
        }
    }

    if cursor.peek().is_some() {
        return Err(cursor.refuse(Rule::TrailingData));
    }

    Ok(top)
}

/// Rule 5: refuses `key` when its object already has an equal key. `at` is
/// the offset of the first quote of `key`.
//
// CONTRACT-QUESTION: the contracts say that a text is JSON, for example
// contract 02 §3 rule 3. None says if an object can hold one key two times.
// RFC 8259 permits it, and Python `json.loads` keeps the last value. The
// reading here refuses the text, because two readers can keep two values.
// The owner did not confirm this reading yet (`rust/AGENTS.md`, "Known
// gaps"). A change costs this function with the key set of `Open::Table`,
// `Rule::DuplicateKey` and the rows of that rule in the tests. The writer
// holds the same rule for the keys of a value, so a change also costs the
// key check of `Table` in `write.rs`. `Keep::Text` stays: `string_at` reads
// the text of each string of an opaque value.
fn duplicate_key<'a>(keys: &mut Keys<'a>, key: Cow<'a, str>, at: usize) -> Result<(), NotStrict> {
    if !keys.insert(key) {
        return Err(NotStrict::new(Rule::DuplicateKey, at));
    }

    Ok(())
}

/// Rule 7: an integer is in the range from the smallest `i64` to the largest
/// `u64`. `at` is the first byte of `token`.
//
// CONTRACT-QUESTION: the contracts say that a text is JSON, for example
// contract 02 §3 rule 3. None gives an integer of a JSON text a range.
// Python `json.loads` keeps an integer of each size. The reading here
// refuses an integer outside 64 bits, because a reader with a 64-bit type
// reads such an integer as a float or as another number. The owner did not
// confirm this reading yet (`rust/AGENTS.md`, "Known gaps"). A change costs
// this function, `Rule::IntegerRange` and the rows of that rule in the
// tests. `integer_value` and the type `Integer` then need a wider value.
fn integer_range(token: &str, at: usize) -> Result<(), NotStrict> {
    if integer_value(token).is_none() {
        return Err(NotStrict::new(Rule::IntegerRange, at));
    }

    Ok(())
}

/// Rule 8: a number with a fraction or an exponent is finite as a float of
/// 64 bits. `at` is the first byte of `token`.
fn float_range(token: &str, at: usize) -> Result<(), NotStrict> {
    if float_value(token).is_none() {
        return Err(NotStrict::new(Rule::FloatRange, at));
    }

    Ok(())
}

/// Whether a number token is an integer: it has no `.`, no `e` and no `E`.
pub(super) fn is_integer(token: &str) -> bool {
    !token.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E'))
}

/// The value of an integer token. `None` for a value outside the range of
/// rule 7. The token `-0` is 0.
pub(super) fn integer_value(token: &str) -> Option<i128> {
    let value: i128 = token.parse().ok()?;
    let in_range = i64::try_from(value).is_ok() || u64::try_from(value).is_ok();

    in_range.then_some(value)
}

/// The float of 64 bits that is nearest to the value of `token`, a number
/// that is no integer. `None` when that float is not finite.
///
/// `serde_json` reads the token here, and `StrictText::parse` gives the same
/// text to `serde_json`. The check and each later read thus have one value
/// for a token.
///
/// Do not read the token with `str::parse::<f64>`. The standard library of
/// the toolchain 1.92.0 reads the digits of an exponent until their value is
/// 65,536 or more, and it drops each later digit. It thus reads an exponent
/// of 655,360 or more as a smaller one. A token with such an exponent and
/// with more than 65,000 other digits can then get a wrong value.
pub(super) fn float_value(token: &str) -> Option<f64> {
    serde_json::from_str::<f64>(token)
        .ok()
        .filter(|float| float.is_finite())
}

/// Whether `byte` is white space between two tokens: rule 3 of the module doc
/// of `json`.
pub(super) const fn is_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

/// Whether `byte` can be in the run of a number: a digit, or one of the
/// bytes `+`, `-`, `.`, `e` and `E`.
pub(super) const fn in_number(byte: u8) -> bool {
    matches!(byte, b'0'..=b'9' | b'+' | b'-' | b'.' | b'e' | b'E')
}

/// The text of the string whose first quote is at `at`, after the decode of
/// each escape, and the offset after its last quote. A string with no escape
/// borrows from `text`.
///
/// `None` when no string of rule 4 starts at `at`. The decode is the decode
/// that gives the pass each key, so a key has one text for the reader and
/// for the writer.
pub(super) fn string_at(text: &str, at: usize) -> Option<(Cow<'_, str>, usize)> {
    let mut cursor = Cursor { text, at };
    if cursor.peek() != Some(b'"') {
        return None;
    }
    let decoded = cursor.string(Keep::Text).ok()?;

    Some((decoded, cursor.at))
}

/// The place of the pass in the text.
struct Cursor<'a> {
    text: &'a str,
    /// The offset of the next byte.
    at: usize,
}

impl<'a> Cursor<'a> {
    fn peek(&self) -> Option<u8> {
        self.text.as_bytes().get(self.at).copied()
    }

    fn bump(&mut self) {
        self.at += 1;
    }

    /// Moves past `byte` when it is the next byte.
    fn takes(&mut self, byte: u8) -> bool {
        let found = self.peek() == Some(byte);
        if found {
            self.bump();
        }

        found
    }

    /// A refusal at the cursor.
    const fn refuse(&self, rule: Rule) -> NotStrict {
        NotStrict::new(rule, self.at)
    }

    /// The text from `start` to the cursor. Each caller gives the offset of
    /// an ASCII byte and stands at one, so the range is a text.
    fn since(&self, start: usize) -> &'a str {
        self.text.get(start..self.at).unwrap_or_default()
    }

    fn skip_space(&mut self) {
        while self.peek().is_some_and(is_space) {
            self.bump();
        }
    }

    /// Moves past each digit at the cursor. The result is their count.
    fn digits(&mut self) -> usize {
        let start = self.at;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.bump();
        }

        self.at - start
    }

    /// Rule 6: `DEPTH_MAX` arrays and objects are open at most. The cursor
    /// is at the bracket that opens one more.
    fn depth(&self, open: &[Open<'_>]) -> Result<(), NotStrict> {
        if open.len() >= DEPTH_MAX {
            return Err(self.refuse(Rule::TooDeep));
        }

        Ok(())
    }

    /// Reads the start of one value. A scalar is read to its end. An array
    /// or an object is open after the call, with the cursor after its
    /// bracket.
    fn value(&mut self, open: &mut Vec<Open<'a>>) -> Result<(Found, Place), NotStrict> {
        let rest = self.text.as_bytes().get(self.at..).unwrap_or_default();
        if CONSTANTS.iter().any(|word| rest.starts_with(word)) {
            return Err(self.refuse(Rule::Constant));
        }

        let scalar = match self.peek() {
            Some(b'[') => {
                self.depth(open)?;
                self.bump();
                open.push(Open::List);

                return Ok((Found::List, Place::Start));
            }
            Some(b'{') => {
                self.depth(open)?;
                self.bump();
                open.push(Open::Table(Keys::new()));

                return Ok((Found::Table, Place::Start));
            }
            Some(b'"') => {
                self.string(Keep::Nothing)?;

                Found::Text
            }
            Some(b't') => self.literal(TRUE, Found::Boolean)?,
            Some(b'f') => self.literal(FALSE, Found::Boolean)?,
            Some(b'n') => self.literal(NULL, Found::Null)?,
            Some(b'-' | b'0'..=b'9') => self.number()?,
            Some(_) | None => return Err(self.refuse(Rule::Syntax)),
        };

        Ok((scalar, Place::AfterValue))
    }

    /// Reads each byte of `word`. The refusal is at the first byte that
    /// differs.
    fn literal(&mut self, word: &str, kind: Found) -> Result<Found, NotStrict> {
        for byte in word.bytes() {
            if !self.takes(byte) {
                return Err(self.refuse(Rule::Syntax));
            }
        }

        Ok(kind)
    }

    /// Reads one number: the longest run of digits and of the bytes `+`,
    /// `-`, `.`, `e` and `E`. The run must be a number of RFC 8259.
    fn number(&mut self) -> Result<Found, NotStrict> {
        let start = self.at;

        self.takes(b'-');
        let whole = if self.takes(b'0') { 1 } else { self.digits() };
        if whole == 0 {
            return Err(self.refuse(Rule::Syntax));
        }
        if self.takes(b'.') && self.digits() == 0 {
            return Err(self.refuse(Rule::Syntax));
        }
        if self.takes(b'e') || self.takes(b'E') {
            if !self.takes(b'+') {
                self.takes(b'-');
            }
            if self.digits() == 0 {
                return Err(self.refuse(Rule::Syntax));
            }
        }
        // The grammar ends here, and the run does not: `01`, `1.5.2`, `1e5e3`.
        if self.peek().is_some_and(in_number) {
            return Err(self.refuse(Rule::Syntax));
        }

        let token = self.since(start);
        if is_integer(token) {
            integer_range(token, start)?;

            return Ok(Found::Integer);
        }
        float_range(token, start)?;

        Ok(Found::Float)
    }

    /// Reads the first key of an object and its colon. `false` for an object
    /// that ends with no key.
    fn first_key(&mut self, keys: &mut Keys<'a>) -> Result<bool, NotStrict> {
        if self.takes(b'}') {
            return Ok(false);
        }
        self.key(keys)?;

        Ok(true)
    }

    /// Reads the comma after a value of an object, the next key and its
    /// colon. `false` for an object that ends there.
    fn next_key(&mut self, keys: &mut Keys<'a>) -> Result<bool, NotStrict> {
        if self.takes(b'}') {
            return Ok(false);
        }
        if !self.takes(b',') {
            return Err(self.refuse(Rule::Syntax));
        }
        self.skip_space();
        self.key(keys)?;

        Ok(true)
    }

    /// Reads the comma after an item of an array. `false` for an array that
    /// ends there.
    fn item_follows(&mut self) -> Result<bool, NotStrict> {
        if self.takes(b']') {
            return Ok(false);
        }
        if !self.takes(b',') {
            return Err(self.refuse(Rule::Syntax));
        }

        Ok(true)
    }

    /// Reads one key and the colon after it.
    fn key(&mut self, keys: &mut Keys<'a>) -> Result<(), NotStrict> {
        let start = self.at;
        if self.peek() != Some(b'"') {
            return Err(self.refuse(Rule::Syntax));
        }
        let key = self.string(Keep::Text)?;
        duplicate_key(keys, key, start)?;

        self.skip_space();
        if !self.takes(b':') {
            return Err(self.refuse(Rule::Syntax));
        }

        Ok(())
    }

    /// Reads one string. The cursor is at its first quote.
    ///
    /// With [`Keep::Text`], the result is the text of the string. It borrows
    /// from the input when the string has no escape. With [`Keep::Nothing`],
    /// the result is the empty text.
    fn string(&mut self, keep: Keep) -> Result<Cow<'a, str>, NotStrict> {
        self.bump();
        // The first byte of the run of characters with no escape.
        let mut run = self.at;
        let mut decoded: Option<String> = None;

        loop {
            match self.peek() {
                Some(b'"') => break,
                Some(b'\\') => {
                    let plain = self.since(run);
                    let character = self.escape()?;
                    if matches!(keep, Keep::Text) {
                        let text = decoded.get_or_insert_default();
                        text.push_str(plain);
                        text.push(character);
                    }
                    run = self.at;
                }
                Some(byte) if byte >= FIRST_PLAIN => self.bump(),
                // A control character with no escape, or the end of the text.
                Some(_) | None => return Err(self.refuse(Rule::Syntax)),
            }
        }

        let plain = self.since(run);
        self.bump();

        Ok(match (keep, decoded) {
            (Keep::Nothing, _) => Cow::Borrowed(""),
            (Keep::Text, None) => Cow::Borrowed(plain),
            (Keep::Text, Some(mut text)) => {
                text.push_str(plain);

                Cow::Owned(text)
            }
        })
    }

    /// Rule 4: reads one escape, or the two escapes of a surrogate pair. The
    /// cursor is at the `\`, and each refusal is at that byte.
    fn escape(&mut self) -> Result<char, NotStrict> {
        let start = self.at;
        self.bump();

        let letter = self.peek();
        self.bump();
        let character = match letter {
            Some(b'"') => '"',
            Some(b'\\') => '\\',
            Some(b'/') => '/',
            Some(b'b') => '\u{8}',
            Some(b'f') => '\u{c}',
            Some(b'n') => '\n',
            Some(b'r') => '\r',
            Some(b't') => '\t',
            Some(b'u') => return self.code_point(start),
            Some(_) | None => return Err(NotStrict::new(Rule::Syntax, start)),
        };

        Ok(character)
    }

    /// Reads the hex digits of a `\u` escape. For the first half of a
    /// surrogate pair, it also reads the escape of the second half. `start`
    /// is the `\` of the escape.
    fn code_point(&mut self, start: usize) -> Result<char, NotStrict> {
        let syntax = NotStrict::new(Rule::Syntax, start);
        let lone = NotStrict::new(Rule::LoneSurrogate, start);

        let Some(unit) = self.hex_unit() else {
            return Err(syntax);
        };
        match unit {
            FIRST_HALF_MIN..=FIRST_HALF_MAX => {}
            SECOND_HALF_MIN..=SECOND_HALF_MAX => return Err(lone),
            _ => return char::from_u32(u32::from(unit)).ok_or(syntax),
        }

        // The escape of the second half must be the next six bytes. Another
        // escape, and an escape that is cut, leave the first half alone.
        if !(self.takes(b'\\') && self.takes(b'u')) {
            return Err(lone);
        }
        let second = match self.hex_unit() {
            Some(second @ SECOND_HALF_MIN..=SECOND_HALF_MAX) => second,
            Some(_) | None => return Err(lone),
        };
        let high = u32::from(unit - FIRST_HALF_MIN);
        let low = u32::from(second - SECOND_HALF_MIN);

        char::from_u32(PAIRS_START + (high << HALF_BITS) + low).ok_or(lone)
    }

    /// Reads four hex digits of each letter case. `None` when one of the
    /// next four bytes is no hex digit.
    fn hex_unit(&mut self) -> Option<u16> {
        let mut unit = 0_u32;
        for _ in 0..HEX_DIGITS {
            let digit = char::from(self.peek()?).to_digit(HEX)?;
            unit = unit * HEX + digit;
            self.bump();
        }

        u16::try_from(unit).ok()
    }
}

#[cfg(test)]
mod tests {
    use std::thread;

    use serde_json::Value;

    use super::super::{ByteCap, check};
    use super::*;

    const MEBIBYTE: usize = 1_048_576;

    /// A cap that each text of these tests is under.
    const ROOMY: ByteCap = ByteCap::new(MEBIBYTE);

    /// One table of refused texts: each text, with the offset of its refusal.
    type Table = &'static [(&'static [u8], usize)];

    /// What `check` gives for one text.
    #[derive(Debug, PartialEq)]
    enum Outcome {
        Strict(Found),
        Refused(Rule, usize),
    }

    fn outcome(text: &[u8]) -> Outcome {
        match check(text, ROOMY) {
            Ok(strict) => Outcome::Strict(strict.top()),
            Err(refused) => Outcome::Refused(refused.rule(), refused.at()),
        }
    }

    fn refusal(text: &[u8]) -> (Rule, usize) {
        let refused = check(text, ROOMY).unwrap_err();

        (refused.rule(), refused.at())
    }

    /// `count` times `open`, then `inside`, then `count` times `close`.
    fn nested(count: usize, open: &str, inside: &str, close: &str) -> String {
        format!("{}{inside}{}", open.repeat(count), close.repeat(count))
    }

    /// Each strict text, with the kind of its top-level value.
    const ACCEPTED: [(&str, Found); 78] = [
        // The literals.
        ("null", Found::Null),
        ("true", Found::Boolean),
        ("false", Found::Boolean),
        // Integers: a token with no fraction and no exponent.
        ("0", Found::Integer),
        ("-0", Found::Integer),
        ("7", Found::Integer),
        ("-7", Found::Integer),
        ("10", Found::Integer),
        ("9007199254740993", Found::Integer),
        ("9223372036854775807", Found::Integer),
        ("9223372036854775808", Found::Integer),
        ("18446744073709551615", Found::Integer),
        ("-9223372036854775808", Found::Integer),
        // Floats: a token with a fraction or an exponent.
        ("0.0", Found::Float),
        ("-0.0", Found::Float),
        ("7.000", Found::Float),
        ("1e3", Found::Float),
        ("1E3", Found::Float),
        ("1e+3", Found::Float),
        ("1E-3", Found::Float),
        ("0e0", Found::Float),
        ("-0E-0", Found::Float),
        ("1.5e10", Found::Float),
        ("0.1e1", Found::Float),
        ("1e007", Found::Float),
        ("1e-999", Found::Float),
        ("-1e-999", Found::Float),
        ("1e308", Found::Float),
        ("1.7976931348623157e308", Found::Float),
        ("-1.7976931348623157E+308", Found::Float),
        // A text above the largest float that rounds to that float.
        ("1.7976931348623158e308", Found::Float),
        // An integer past 64 bits with a fraction is a float, and it is finite.
        ("18446744073709551616.0", Found::Float),
        ("1e19", Found::Float),
        // Strings.
        (r#""""#, Found::Text),
        (r#""a""#, Found::Text),
        (r#""\"\\\/\b\f\n\r\t""#, Found::Text),
        (r#""\u0041\u00e9\u00E9\uabcd\uABCD\uAbCd""#, Found::Text),
        (r#""\u0000""#, Found::Text),
        (r#""\ufffe\uffff""#, Found::Text),
        (r#""\ud83d\ude00""#, Found::Text),
        (r#""\uD83D\uDE00""#, Found::Text),
        (r#""\ud800\udc00""#, Found::Text),
        (r#""\udbff\udfff""#, Found::Text),
        // The code points directly below and directly above the surrogates.
        (r#""\ud7ff\ue000""#, Found::Text),
        (r#""a\ud83d\ude00b\ud83d\ude00""#, Found::Text),
        ("\"caf\u{e9} \u{1f600}\"", Found::Text),
        // DEL, a noncharacter and the last code point need no escape.
        ("\"\u{7f}\"", Found::Text),
        ("\"\u{ffff}\u{10ffff}\"", Found::Text),
        // A line separator in a string, and the words of rule 3 in a string.
        ("\"\u{2028}\u{2029}\"", Found::Text),
        (r#""NaN Infinity -Infinity // /* */""#, Found::Text),
        // Arrays.
        ("[]", Found::List),
        ("[ ]", Found::List),
        ("[1]", Found::List),
        ("[1,2]", Found::List),
        ("[ 1 , 2 ]", Found::List),
        ("[[],[]]", Found::List),
        (r#"[null,true,false,0,-0.0,"",[],{}]"#, Found::List),
        // Objects.
        ("{}", Found::Table),
        ("{ }", Found::Table),
        (r#"{"a":1}"#, Found::Table),
        (r#"{ "a" : 1 , "b" : 2 }"#, Found::Table),
        (r#"{"":1}"#, Found::Table),
        (r#"{"a":[],"b":{}}"#, Found::Table),
        // One key in two objects is one time in each object.
        (r#"{"a":{"a":{"a":1}}}"#, Found::Table),
        (r#"[{"a":1},{"a":2}]"#, Found::List),
        (r#"{"a":{"b":1},"b":{"a":1}}"#, Found::Table),
        // Keys that differ in a code point are two keys.
        (r#"{"a":1,"A":2}"#, Found::Table),
        (r#"{"a":1,"a ":2}"#, Found::Table),
        (r#"{"1":1,"1.0":2,"01":3,"true":4}"#, Found::Table),
        (r#"{"":1,"\u0000":2}"#, Found::Table),
        (r#"{"a":1,"\u0041":2}"#, Found::Table),
        // No normalization: U+00E9 is not `e` with U+0301.
        ("{\"\u{e9}\":1,\"e\u{301}\":2}", Found::Table),
        (r#"{"\u00e9":1,"e\u0301":2}"#, Found::Table),
        (r#"{"\ud83d\ude00":1,"\ud83d\ude01":2}"#, Found::Table),
        // White space around the value: each of the four characters.
        (" \t\n\r1 \t\n\r", Found::Integer),
        ("\n{\n\t\"a\" :\r\n[ 1 ,\n2 ]\n}\n", Found::Table),
        ("\r\n\"a\"", Found::Text),
        ("[\n]", Found::List),
    ];

    #[test]
    fn each_strict_text_is_accepted() {
        for (text, kind) in ACCEPTED {
            let strict = check(text.as_bytes(), ROOMY);

            assert_eq!(strict.map(|strict| strict.top()), Ok(kind), "{text:?}");
        }
    }

    #[test]
    fn the_string_at_an_offset_has_its_decoded_text_and_its_end() {
        let text = r#"{"plain": "caf\u00e9 \ud83d\ude00\n\/", "é": ""}"#;

        // A string with no escape borrows from the text.
        assert_eq!(string_at(text, 1), Some((Cow::Borrowed("plain"), 8)));
        assert!(matches!(string_at(text, 1), Some((Cow::Borrowed(_), _))));
        assert_eq!(
            string_at(text, 10),
            Some((Cow::Owned(String::from("caf\u{e9} \u{1f600}\n/")), 38))
        );
        assert_eq!(string_at(text, 40), Some((Cow::Borrowed("\u{e9}"), 44)));
        assert_eq!(string_at(text, 46), Some((Cow::Borrowed(""), 48)));

        // No string starts at a byte that is no quote, or past the text.
        assert_eq!(string_at(text, 0), None);
        assert_eq!(string_at(text, 2), None);
        assert_eq!(string_at(text, 8), None);
        assert_eq!(string_at(text, text.len()), None);
        assert_eq!(string_at(text, text.len() + 7), None);

        // A string that breaks rule 4 has no text.
        for broken in [
            "\"abc",
            "\"abc\\",
            "\"\\x\"",
            "\"\\u12\"",
            "\"\\ud800\"",
            "\"\\udc00\"",
            "\"a\nb\"",
        ] {
            assert_eq!(string_at(broken, 0), None, "{broken:?}");
        }
    }

    #[test]
    fn serde_json_reads_each_strict_text() {
        // `StrictText::parse` gives a strict text to `serde_json`. A text
        // that `serde_json` refuses would be a `Shape` for each type.
        for (text, _) in ACCEPTED {
            assert!(serde_json::from_str::<Value>(text).is_ok(), "{text:?}");
        }
    }

    /// Rule 3 and rule 4: a text that is no JSON text, and the offset.
    const SYNTAX: [(&[u8], usize); 118] = [
        // No value.
        (b"", 0),
        (b" ", 1),
        (b"\n\t \r", 4),
        // A word that is no literal: the first byte that differs.
        (b"nope", 1),
        (b"nul", 3),
        (b"nulL", 3),
        (b"tru", 3),
        (b"trUe", 2),
        (b"fals", 4),
        (b"falsy", 4),
        (b"nan", 1),
        (b"Null", 0),
        (b"NULL", 0),
        (b"True", 0),
        (b"TRUE", 0),
        (b"False", 0),
        (b"Nan", 0),
        (b"NAN", 0),
        (b"inf", 0),
        (b"Inf", 0),
        (b"infinity", 0),
        (b"-inf", 1),
        (b"-Inf", 1),
        (b"-NaN", 1),
        (b"+Infinity", 0),
        (b"undefined", 0),
        (b"None", 0),
        // A run of number bytes that is no number.
        (b"01", 1),
        (b"00", 1),
        (b"-01", 2),
        (b"1.", 2),
        (b"0.", 2),
        (b"-0.", 3),
        (b".5", 0),
        (b"-.5", 1),
        (b"-", 1),
        (b"--1", 1),
        (b"+1", 0),
        (b"1e", 2),
        (b"1E", 2),
        (b"1e+", 3),
        (b"1E-", 3),
        (b"1.e3", 2),
        (b"1.0e", 4),
        (b"1.5.2", 3),
        (b"1e5e3", 3),
        (b"1e5.3", 3),
        (b"1+2", 1),
        (b"1-2", 1),
        (b"1e5-", 3),
        (b"[01]", 2),
        (b"[1.]", 3),
        (b"[0x10]", 2),
        (b"[1_000]", 2),
        (b"[1 000]", 3),
        // A digit that is not ASCII.
        ("\u{661}".as_bytes(), 0),
        ("[1\u{661}]".as_bytes(), 2),
        // A string with no end, and a string with other quotes.
        (b"\"", 1),
        (b"\"abc", 4),
        (b"'a'", 0),
        (b"[\"a]", 4),
        // A control character with no escape.
        (b"\"a\tb\"", 2),
        (b"\"a\nb\"", 2),
        (b"\"a\rb\"", 2),
        (b"\"a\x00b\"", 2),
        (b"\"a\x1fb\"", 2),
        (b"\"\x08\"", 1),
        // An escape that rule 4 does not list: the offset of its `\`.
        (br#""\a""#, 1),
        (br#""\'""#, 1),
        (br#""\0""#, 1),
        (br#""\v""#, 1),
        (br#""\x41""#, 1),
        (br#""\U0041""#, 1),
        (br#""\N""#, 1),
        (br#""ab\ ""#, 3),
        (b"\"\\\n\"", 1),
        // A `\u` escape with less than four hex digits.
        (br#""\u""#, 1),
        (br#""\u1""#, 1),
        (br#""\u12""#, 1),
        (br#""\u123""#, 1),
        (br#""\u12G4""#, 1),
        (br#""\u+123""#, 1),
        (br#""\u 123""#, 1),
        (br#""\u-123""#, 1),
        (br#""a\u00zz""#, 2),
        ("\"\\u00\u{e9}0\"".as_bytes(), 1),
        // An escape that the text cuts.
        (b"\"\\", 1),
        (b"\"\\u", 1),
        (b"\"\\u123", 1),
        // An array with a part that is absent.
        (b"[", 1),
        (b"[1", 2),
        (b"[1,", 3),
        (b"[1,]", 3),
        (b"[,1]", 1),
        (b"[,]", 1),
        (b"[1,,2]", 3),
        (b"[1 2]", 3),
        (b"[1:2]", 2),
        (b"]", 0),
        (b"[}", 1),
        (b"[1}", 2),
        (b"[truex]", 5),
        // An object with a part that is absent.
        (b"{", 1),
        (b"{\"a\"", 4),
        (b"{\"a\":", 5),
        (b"{\"a\":1", 6),
        (b"{\"a\":1,", 7),
        (b"{\"a\":1,}", 7),
        (b"{,}", 1),
        (b"{\"a\"}", 4),
        (b"{\"a\" 1}", 5),
        (b"{\"a\":}", 5),
        (b"{\"a\":1 \"b\":2}", 7),
        (b"{\"a\":1]", 6),
        (b"{\"a\",1}", 4),
        (b"}", 0),
        // A key that is no string.
        (b"{a:1}", 1),
        (b"{1:2}", 1),
    ];

    /// More rows of rule 3 and rule 4.
    const SYNTAX_MORE: [(&[u8], usize); 17] = [
        (b"{null:1}", 1),
        (b"{'a':1}", 1),
        (b"{[]:1}", 1),
        (b"{\"a\":1,2}", 7),
        // A comment.
        (b"/* c */ 1", 0),
        (b"// c\n1", 0),
        (b"# c\n1", 0),
        (b"[1, /* c */ 2]", 4),
        // White space that is not one of the four characters.
        (b"\x0c1", 0),
        (b"\x0b1", 0),
        ("\u{a0}1".as_bytes(), 0),
        ("\u{2028}1".as_bytes(), 0),
        ("[1,\u{3000}2]".as_bytes(), 3),
        // A separator alone, and a character that starts no value.
        (b":", 0),
        (b",", 0),
        ("\u{e9}".as_bytes(), 0),
        (b"\x00", 0),
    ];

    /// Rule 3: the three words, at the first byte of the word.
    const CONSTANT: [(&[u8], usize); 10] = [
        (b"NaN", 0),
        (b"Infinity", 0),
        (b"-Infinity", 0),
        (b" NaN", 1),
        (b"[NaN]", 1),
        (b"[1,-Infinity]", 3),
        (b"{\"a\":Infinity}", 5),
        (b"{\"a\": -Infinity}", 6),
        // The word is the refusal, with each byte after it.
        (b"NaN1", 0),
        (b"Infinitys", 0),
    ];

    /// Rule 3: a byte after the value that is not white space.
    const TRAILING_DATA: [(&[u8], usize); 24] = [
        (b"1 2", 2),
        (b"1x", 1),
        (b"1,", 1),
        (b"1]", 1),
        (b"0x10", 1),
        (b"1_000", 1),
        (b"truex", 4),
        (b"true1", 4),
        (b"nullnull", 4),
        (b"falsefalse", 5),
        (b"\"a\"\"b\"", 3),
        (b"\"a\"x", 3),
        (b"[]]", 2),
        (b"[][]", 2),
        (b"{}}", 2),
        (b"{}{}", 2),
        (b"[1] 2", 4),
        (b"{\"a\":1},", 7),
        (b"1 \n x", 4),
        (b"1 // c", 2),
        (b"1 /* c */", 2),
        (b"1\x00", 1),
        (b"1\x0c", 1),
        ("1\u{661}".as_bytes(), 1),
    ];

    /// Rule 5: the offset of the first quote of the second key.
    const DUPLICATE_KEY: [(&[u8], usize); 16] = [
        (br#"{"a":1,"a":2}"#, 7),
        (br#"{"a":1,"a":1}"#, 7),
        (br#"{"a":1, "a":2}"#, 8),
        (br#"{"a":1,"b":2,"a":3}"#, 13),
        (br#"{"":1,"":2}"#, 6),
        // An escape and its character are one key.
        (br#"{"a":1,"\u0061":2}"#, 7),
        (br#"{"\u0061":1,"a":2}"#, 12),
        (br#"{"\u0041":1,"\u0041":2}"#, 12),
        (br#"{"A":1,"\u0041":2}"#, 7),
        (br#"{"a/b":1,"a\/b":2}"#, 9),
        (br#"{"\n":1,"\u000a":2}"#, 8),
        (
            br#"{"\"\\\b\f\r\t":1,"\u0022\u005c\u0008\u000c\u000d\u0009":2}"#,
            18,
        ),
        (br#"{"\u00e9":1,"\u00E9":2}"#, 12),
        ("{\"\u{1f600}\":1,\"\\ud83d\\ude00\":2}".as_bytes(), 10),
        // An inner object.
        (br#"{"x":{"a":1,"a":2}}"#, 12),
        (br#"[{"a":1,"a":1}]"#, 8),
    ];

    /// Rule 4: the offset of the `\` of the escape that has no partner.
    const LONE_SURROGATE: [(&[u8], usize); 23] = [
        // A first half alone.
        (br#""\ud800""#, 1),
        (br#""\uD800""#, 1),
        (br#""\udbff""#, 1),
        // A second half alone.
        (br#""\udc00""#, 1),
        (br#""\uDFFF""#, 1),
        // A first half with another character after it.
        (br#""\ud800\u0041""#, 1),
        (br#""\ud800a""#, 1),
        (br#""\ud800\n""#, 1),
        (br#""\ud800\\udc00""#, 1),
        (br#""\ud800 \udc00""#, 1),
        // The two halves in the wrong order, and a half two times.
        (br#""\udc00\ud800""#, 1),
        (br#""\udc00\udc00""#, 1),
        (br#""\ud83d\ud83d\ude00""#, 1),
        (br#""a\ud83d\ude00\ude00""#, 14),
        // A first half, then an escape that is cut or that is no escape.
        (br#""\ud800\u12G4""#, 1),
        (br#""\ud800\udc0""#, 1),
        (b"\"\\ud800", 1),
        (b"\"\\ud800\\udc0", 1),
        // In a key, in an item and in a member that a raw type can ignore.
        (br#"{"\ud800":1}"#, 2),
        (br#"["\ud800"]"#, 2),
        (br#"{"a":1,"b":"\udc00"}"#, 12),
        (br#"{"a":1,"b":{"c":["\ud800"]}}"#, 18),
        (br#"{"\udc00":1,"\udc00":2}"#, 2),
    ];

    /// Rule 7: an integer outside the range, at the first byte of its token.
    const INTEGER_RANGE: [(&[u8], usize); 10] = [
        (b"18446744073709551616", 0),
        (b"-9223372036854775809", 0),
        (b"-18446744073709551615", 0),
        (b"99999999999999999999", 0),
        // 2^127 and 2^128.
        (b"170141183460469231731687303715884105728", 0),
        (b"340282366920938463463374607431768211456", 0),
        (b"-340282366920938463463374607431768211456", 0),
        (b" 18446744073709551616", 1),
        (b"[1,18446744073709551616]", 3),
        (b"{\"a\":-9223372036854775809}", 5),
    ];

    /// Rule 8: a float that is not finite, at the first byte of its token.
    const FLOAT_RANGE: [(&[u8], usize); 10] = [
        (b"1e999", 0),
        (b"-1e999", 0),
        (b"1E+999", 0),
        (b"1e309", 0),
        (b"2e308", 0),
        // A text above the largest float that rounds to an infinity.
        (b"1.7976931348623159e308", 0),
        (b"-1.7976931348623159e308", 0),
        (b"[1.0,1e400]", 5),
        (b"{\"a\":-1E+999}", 5),
        (b"1e999 x", 0),
    ];

    #[test]
    fn each_refused_text_has_its_rule_and_its_offset() {
        let tables: [(Rule, Table); 8] = [
            (Rule::Syntax, &SYNTAX),
            (Rule::Syntax, &SYNTAX_MORE),
            (Rule::Constant, &CONSTANT),
            (Rule::TrailingData, &TRAILING_DATA),
            (Rule::DuplicateKey, &DUPLICATE_KEY),
            (Rule::LoneSurrogate, &LONE_SURROGATE),
            (Rule::IntegerRange, &INTEGER_RANGE),
            (Rule::FloatRange, &FLOAT_RANGE),
        ];

        for (rule, table) in tables {
            for (text, at) in table {
                let shown = String::from_utf8_lossy(text);

                assert_eq!(refusal(text), (rule, *at), "{shown:?}");
            }
        }
    }

    #[test]
    fn serde_json_refuses_each_text_that_is_no_json_text() {
        // These five rules are rules of RFC 8259 or of a float of 64 bits,
        // so a second reader must refuse the same texts.
        let tables: [Table; 6] = [
            &SYNTAX,
            &SYNTAX_MORE,
            &CONSTANT,
            &TRAILING_DATA,
            &LONE_SURROGATE,
            &FLOAT_RANGE,
        ];

        for (text, _) in tables.into_iter().flatten() {
            let shown = String::from_utf8_lossy(text);

            assert!(serde_json::from_slice::<Value>(text).is_err(), "{shown:?}");
        }
    }

    #[test]
    fn serde_json_reads_each_text_of_the_rules_that_rfc_8259_does_not_have() {
        // RFC 8259 permits a key two times and an integer of each size. A
        // second reader thus reads each row of the two tables.
        for (text, _) in DUPLICATE_KEY.into_iter().chain(INTEGER_RANGE) {
            let shown = String::from_utf8_lossy(text);

            assert!(serde_json::from_slice::<Value>(text).is_ok(), "{shown:?}");
        }
    }

    #[test]
    fn a_long_float_token_counts_each_digit_of_its_exponent() {
        // The digits before the exponent move the value by 66,001 or by
        // 700,001 places. Only the full exponent gives the right value.
        let zeros = "0".repeat(66_000);
        let past_the_range = format!("0.{zeros}1e655360");
        let rounds_to_zero = format!("1{zeros}e-655360");
        let one = format!("0.{}1e700001", "0".repeat(700_000));

        assert_eq!(float_value(&past_the_range), None);
        assert_eq!(float_value(&rounds_to_zero), Some(0.0));
        assert_eq!(float_value(&one), Some(1.0));

        assert_eq!(refusal(past_the_range.as_bytes()), (Rule::FloatRange, 0));
        for text in [&rounds_to_zero, &one] {
            assert_eq!(outcome(text.as_bytes()), Outcome::Strict(Found::Float));
        }
    }

    #[test]
    fn a_text_of_64_levels_is_strict_and_65_levels_are_too_deep() {
        let lists = nested(DEPTH_MAX, "[", "", "]");
        let lists_with_scalar = nested(DEPTH_MAX, "[", "1", "]");
        let tables = nested(DEPTH_MAX, r#"{"a":"#, "1", "}");
        let mixed = nested(DEPTH_MAX / 2, r#"[{"a":"#, "null", "}]");

        for text in [&lists, &lists_with_scalar, &tables, &mixed] {
            assert!(check(text.as_bytes(), ROOMY).is_ok(), "{text}");
            assert!(serde_json::from_str::<Value>(text).is_ok(), "{text}");
        }

        let one_more = DEPTH_MAX + 1;
        let lists = nested(one_more, "[", "", "]");
        let tables = nested(one_more, r#"{"a":"#, "1", "}");
        let list_in_tables = nested(DEPTH_MAX, r#"{"a":"#, "[]", "}");
        let table_in_lists = nested(DEPTH_MAX, "[", "{}", "]");
        let open_only = "[".repeat(one_more);

        // RFC 8259 gives no limit: a second reader reads 65 levels.
        assert!(serde_json::from_str::<Value>(&lists).is_ok());
        // The offset is the bracket that opens level 65.
        assert_eq!(refusal(lists.as_bytes()), (Rule::TooDeep, DEPTH_MAX));
        assert_eq!(refusal(open_only.as_bytes()), (Rule::TooDeep, DEPTH_MAX));
        assert_eq!(
            refusal(tables.as_bytes()),
            (Rule::TooDeep, DEPTH_MAX * r#"{"a":"#.len())
        );
        assert_eq!(
            refusal(list_in_tables.as_bytes()),
            (Rule::TooDeep, DEPTH_MAX * r#"{"a":"#.len())
        );
        assert_eq!(
            refusal(table_in_lists.as_bytes()),
            (Rule::TooDeep, DEPTH_MAX)
        );
    }

    #[test]
    fn the_depth_counts_only_what_is_open_at_one_point() {
        // 1000 arrays, each one closed before the next one opens.
        let siblings = format!("[{}[]]", "[],".repeat(999));
        // 100 times a value of 63 levels, inside one array.
        let deep = nested(DEPTH_MAX - 1, "[", "", "]");
        let deep_siblings = format!("[{}{deep}]", format!("{deep},").repeat(99));

        assert!(check(siblings.as_bytes(), ROOMY).is_ok());
        assert!(check(deep_siblings.as_bytes(), ROOMY).is_ok());
    }

    /// Runs the check of `text` on a thread with a stack of 64 KiB. The pass
    /// uses no stack for a level of a text, so a text of each depth and of
    /// each size fits there.
    fn on_a_small_stack(text: Vec<u8>) -> Outcome {
        const STACK_BYTES: usize = 64 * 1024;

        let checker = thread::Builder::new()
            .stack_size(STACK_BYTES)
            .spawn(move || outcome(&text))
            .unwrap();

        checker.join().unwrap()
    }

    #[test]
    fn a_text_of_500_000_open_brackets_is_too_deep_on_a_small_stack() {
        const OPEN: usize = 500_000;

        let mut text = vec![b'['; OPEN];
        text.resize(OPEN * 2, b']');
        text.resize(MEBIBYTE, b' ');

        assert_eq!(text.len(), MEBIBYTE);
        assert_eq!(
            on_a_small_stack(text),
            Outcome::Refused(Rule::TooDeep, DEPTH_MAX)
        );
    }

    #[test]
    fn a_strict_text_of_one_mebibyte_is_read_on_a_small_stack() {
        // One array of values of 63 levels: 64 levels are open at most.
        let deep = nested(DEPTH_MAX - 1, "[", "", "]");
        let count = (MEBIBYTE - 2) / (deep.len() + 1);
        let mut text = format!("[{}{deep}]", format!("{deep},").repeat(count - 1)).into_bytes();
        text.resize(MEBIBYTE, b' ');

        assert_eq!(on_a_small_stack(text), Outcome::Strict(Found::List));
    }

    #[test]
    fn the_first_offset_that_breaks_a_rule_is_the_refusal() {
        let rows: [(&[u8], Rule, usize); 12] = [
            // A lone surrogate at byte 2, a second key at byte 12.
            (br#"{"\ud800":1,"\ud800":2}"#, Rule::LoneSurrogate, 2),
            // An integer past the range at byte 1, the end of the text later.
            (b"[18446744073709551616,", Rule::IntegerRange, 1),
            // A float past the range at byte 0, a second number at byte 6.
            (b"1e999 1", Rule::FloatRange, 0),
            // A run that is no number: the grammar is before the range.
            (b"1e999e5", Rule::Syntax, 5),
            (b"18446744073709551616.", Rule::Syntax, 21),
            // A word of rule 3 at byte 1, a final comma at byte 4.
            (b"[NaN,]", Rule::Constant, 1),
            // A raw control character at byte 2, a lone surrogate at byte 3.
            (b"\"a\x01\\ud800\"", Rule::Syntax, 2),
            // A second key at byte 7, a value that is absent at byte 11.
            (br#"{"a":1,"a":}"#, Rule::DuplicateKey, 7),
            // A second key at byte 7, a word of rule 3 at byte 11.
            (br#"{"a":1,"a":NaN}"#, Rule::DuplicateKey, 7),
            // A second key at byte 7, the end of the text at byte 10.
            (br#"{"a":1,"a""#, Rule::DuplicateKey, 7),
            // A second key at byte 7, an integer past the range at byte 11.
            (
                br#"{"a":1,"a":18446744073709551616}"#,
                Rule::DuplicateKey,
                7,
            ),
            // An integer past the range at byte 0, a byte after it at byte 20.
            (b"18446744073709551616x", Rule::IntegerRange, 0),
        ];

        for (text, rule, at) in rows {
            let shown = String::from_utf8_lossy(text);

            assert_eq!(refusal(text), (rule, at), "{shown:?}");
        }
    }
}
