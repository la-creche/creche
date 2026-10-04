//! Text as the Python implementation reads it.
//!
//! The Python code calls `str.strip`, `str.splitlines`, `int` and `float` on
//! the text of a config. Each one has a rule that the Rust standard library
//! does not share. This module holds those rules one time, so a reader here
//! accepts and refuses what the Python reader does.
//!
//! One difference stays on purpose: a decimal digit that is not ASCII. Python
//! reads it as a digit. The functions here refuse it (`rust/AGENTS.md`, rule
//! 9).

/// Whether Python's `str.isspace` holds for the character.
///
/// The set is `White_Space` of Unicode and the four separators U+001C to
/// U+001F. `char::is_whitespace` does not hold for the four separators.
pub(super) fn is_space(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

/// The text without the space at its two ends, as Python's `str.strip`.
pub(super) fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// Whether Python's `str.splitlines` ends a line at the character.
fn is_line_break(character: char) -> bool {
    matches!(
        character,
        '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}'
            ..='\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

/// The lines of the text, as Python's `str.splitlines`.
///
/// A `\r\n` gives one empty line more than Python does. Each caller here
/// skips an empty line, so the two results are equal for that caller.
pub(super) fn lines(text: &str) -> impl Iterator<Item = &str> {
    text.split(is_line_break)
}

/// A text that Python's `int` reads as a decimal integer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Integer {
    /// The number.
    Fits(i64),
    /// The text is a number, and the number does not fit 64 bits.
    TooLarge,
}

/// The digits of `text`, when `text` is one or more ASCII digits with single
/// underscores between two digits: `1_000`, not `_1`, `1_` or `1__0`.
fn digit_run(text: &str) -> Option<String> {
    let mut digits = String::with_capacity(text.len());
    let mut last_was_digit = false;
    for byte in text.bytes() {
        match byte {
            b'0'..=b'9' => {
                digits.push(char::from(byte));
                last_was_digit = true;
            }
            b'_' if last_was_digit => last_was_digit = false,
            _ => return None,
        }
    }

    (last_was_digit).then_some(digits)
}

/// Splits a sign from the start of `text`. The flag is true for `-`.
fn split_sign(text: &str) -> (bool, &str) {
    if let Some(rest) = text.strip_prefix('-') {
        return (true, rest);
    }

    (false, text.strip_prefix('+').unwrap_or(text))
}

/// Reads `text` as Python's `int(text)` does, for ASCII digits.
///
/// Python takes space at the two ends, one sign, and single underscores
/// between digits. It takes no prefix such as `0x`.
pub(super) fn integer(text: &str) -> Option<Integer> {
    let (negative, rest) = split_sign(strip(text));
    let digits = digit_run(rest)?;
    let signed = if negative {
        format!("-{digits}")
    } else {
        digits
    };

    Some(signed.parse().map_or(Integer::TooLarge, Integer::Fits))
}

/// The three words that Python's `float` reads as a value that is not finite,
/// in lower case.
const NOT_FINITE_WORDS: [&str; 3] = ["inf", "infinity", "nan"];

/// The decimal number of `text` without its underscores, when `text` has the
/// form that Python's `float` takes: digits, then an optional point and
/// digits, then an optional exponent. One of the two digit parts can be
/// empty.
fn decimal(text: &str) -> Option<String> {
    let (mantissa, exponent) = match text.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (mantissa, Some(exponent)),
        None => (text, None),
    };
    let (whole, fraction) = match mantissa.split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (mantissa, None),
    };
    if whole.is_empty() && fraction.is_none_or(str::is_empty) {
        return None;
    }

    let mut number = String::with_capacity(text.len());
    if !whole.is_empty() {
        number.push_str(&digit_run(whole)?);
    }

    if let Some(fraction) = fraction {
        number.push('.');
        if !fraction.is_empty() {
            number.push_str(&digit_run(fraction)?);
        }
    }

    if let Some(exponent) = exponent {
        let (negative, digits) = split_sign(exponent);
        number.push('e');
        if negative {
            number.push('-');
        }
        number.push_str(&digit_run(digits)?);
    }

    Some(number)
}

/// Reads `text` as Python's `float(text)` does, for ASCII digits.
///
/// The result can be a value that is not finite: Python reads `inf`,
/// `infinity` and `nan` in each case of letters, and a number such as
/// `1e999` as infinity. The caller decides what to do with such a value.
pub(super) fn float(text: &str) -> Option<f64> {
    let (negative, rest) = split_sign(strip(text));
    let number = if NOT_FINITE_WORDS.contains(&rest.to_ascii_lowercase().as_str()) {
        rest.to_owned()
    } else {
        decimal(rest)?
    };
    let value: f64 = number.parse().ok()?;

    Some(if negative { -value } else { value })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Each character for which Python's `str.isspace` holds, from Python
    /// 3.13: `[c for c in range(0x110000) if chr(c).isspace()]`.
    const PYTHON_SPACES: [u32; 29] = [
        0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000,
        0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028,
        0x2029, 0x202f, 0x205f, 0x3000,
    ];

    /// Each character at which Python's `str.splitlines` ends a line.
    const PYTHON_LINE_BREAKS: [u32; 10] = [
        0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029,
    ];

    #[test]
    fn the_space_set_is_the_set_of_python() {
        let found: Vec<u32> = (0..=u32::from(char::MAX))
            .filter(|code| char::from_u32(*code).is_some_and(is_space))
            .collect();

        assert_eq!(found, PYTHON_SPACES);
    }

    #[test]
    fn the_line_break_set_is_the_set_of_python() {
        let found: Vec<u32> = (0..=u32::from(char::MAX))
            .filter(|code| char::from_u32(*code).is_some_and(is_line_break))
            .collect();

        assert_eq!(found, PYTHON_LINE_BREAKS);
    }

    #[test]
    fn strip_removes_the_space_of_python_at_the_two_ends() {
        assert_eq!(strip(" \t a b \u{1f}\u{a0}\n"), "a b");
        assert_eq!(strip("\u{3000}"), "");
        assert_eq!(strip("a"), "a");
    }

    #[test]
    fn lines_split_at_each_line_break_of_python() {
        let found: Vec<&str> = lines("a\nb\rc\u{0b}d\u{85}e\u{2028}f").collect();

        assert_eq!(found, ["a", "b", "c", "d", "e", "f"]);
        assert_eq!(lines("a\r\nb").collect::<Vec<_>>(), ["a", "", "b"]);
    }

    #[test]
    fn an_integer_is_what_python_int_takes_in_ascii() {
        for (text, value) in [
            ("0", 0),
            ("8350", 8350),
            (" 8350\n", 8350),
            ("+7", 7),
            ("-7", -7),
            ("007", 7),
            ("1_000", 1000),
            ("-9223372036854775808", i64::MIN),
        ] {
            assert_eq!(integer(text), Some(Integer::Fits(value)), "{text:?}");
        }

        assert_eq!(integer("9223372036854775808"), Some(Integer::TooLarge));
        assert_eq!(integer(&"9".repeat(5000)), Some(Integer::TooLarge));
    }

    #[test]
    fn a_text_that_python_int_refuses_is_no_integer() {
        for text in [
            "",
            " ",
            "+",
            "-",
            "1.0",
            "1e3",
            "0x10",
            "_1",
            "1_",
            "1__0",
            "1 0",
            "+-1",
            "--1",
            "a",
            "١",
            "８３５０",
        ] {
            assert_eq!(integer(text), None, "{text:?}");
        }
    }

    #[test]
    fn a_float_is_what_python_float_takes_in_ascii() {
        for (text, value) in [
            ("1", 1.0),
            (" 20.5 ", 20.5),
            (".5", 0.5),
            ("5.", 5.0),
            ("+1e3", 1000.0),
            ("1E-3", 0.001),
            ("1_0.2_5e0_1", 102.5),
            ("-0", 0.0),
            ("-2.5", -2.5),
            ("1e-400", 0.0),
        ] {
            assert_eq!(float(text), Some(value), "{text:?}");
        }
    }

    #[test]
    fn a_float_can_be_a_value_that_is_not_finite() {
        for text in ["inf", "Infinity", "+INF", "1e999"] {
            assert_eq!(float(text), Some(f64::INFINITY), "{text:?}");
        }

        assert_eq!(float("-inf"), Some(f64::NEG_INFINITY));
        for text in ["nan", "NaN", "-nan"] {
            assert!(float(text).is_some_and(f64::is_nan), "{text:?}");
        }
    }

    #[test]
    fn a_text_that_python_float_refuses_is_no_float() {
        for text in [
            "", " ", ".", "e5", "1e", "1e+", "+", "_1", "1_", "1__0", "1_.5", "1._5", "1e_5",
            "0x10", "1,5", "1 5", "infinit", "na", "١",
        ] {
            assert_eq!(float(text), None, "{text:?}");
        }
    }
}
