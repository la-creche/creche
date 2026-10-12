//! The white space rules of `str` in Python.
//!
//! The Python code calls `str.isspace`, `str.strip` and `str.split` on text
//! that a Rust reader reads too. The functions here give the Python result.
//! The standard library of Rust has another set: `char::is_whitespace` does
//! not hold for the four separators U+001C to U+001F, and `str::trim` keeps
//! them.
//!
//! Python has more than one white space set. This module holds two of them.
//! One is the set of `str`. The other is the set that `int` and `float` of
//! Python remove around a number: [`number_strip`]. That set does not hold
//! the four separators, so a separator stays at the end of a number text.
//!
//! The module also holds the line end rule of the text mode of Python:
//! [`universal_newlines`]. The Python code reads a file and the output of a
//! child program in that mode.

use std::borrow::Cow;

/// Whether `str.isspace` of Python holds for the character.
///
/// The set is `White_Space` of Unicode and the four separators U+001C to
/// U+001F. `char::is_whitespace` does not hold for the four separators.
///
/// ```
/// use creche_util::pytext;
///
/// assert!(pytext::is_space(' '));
/// assert!(pytext::is_space('\u{1c}'));
/// assert!(!'\u{1c}'.is_whitespace());
/// assert!(!pytext::is_space('a'));
/// ```
#[must_use]
pub fn is_space(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

/// The text without the space at its two ends, as `str.strip` of Python gives
/// it with no argument.
///
/// ```
/// use creche_util::pytext;
///
/// assert_eq!(pytext::strip(" \t token\r\n"), "token");
/// assert_eq!(pytext::strip("\u{1c}token\u{1f}"), "token");
/// assert_eq!("\u{1c}token\u{1f}".trim(), "\u{1c}token\u{1f}");
/// ```
#[must_use]
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// The text without the space at its two ends, as `int` and `float` of
/// Python remove that space before they read a number.
///
/// The set has 25 characters: U+0009 to U+000D, U+0020, U+0085, U+00A0,
/// U+1680, U+2000 to U+200A, U+2028, U+2029, U+202F, U+205F and U+3000. It
/// is `White_Space` of Unicode, the set of `char::is_whitespace`.
///
/// The set of [`strip`] has four characters more: the separators U+001C to
/// U+001F. This function keeps a separator at each end. `int` and `float`
/// then refuse the text, because a separator is no digit.
///
/// ```
/// use creche_util::pytext;
///
/// assert_eq!(pytext::number_strip(" \t7\u{a0}\n"), "7");
/// assert_eq!(pytext::number_strip("\u{1c}7\u{1f}"), "\u{1c}7\u{1f}");
/// assert_eq!(pytext::strip("\u{1c}7\u{1f}"), "7");
/// ```
#[must_use]
pub fn number_strip(text: &str) -> &str {
    text.trim()
}

/// The words of the text, as `str.split` of Python gives them with no
/// argument: each run of characters that are not space. No word is empty.
///
/// ```
/// use creche_util::pytext;
///
/// let words: Vec<&str> = pytext::words(" 0  9\t* *\u{1f}1 ").collect();
/// assert_eq!(words, ["0", "9", "*", "*", "1"]);
/// assert_eq!(pytext::words(" \n ").count(), 0);
/// ```
pub fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(is_space).filter(|word| !word.is_empty())
}

/// The text with each line end as one LF, as the text mode of Python gives
/// it: one LF for each CR LF and for each other CR.
///
/// Python reads a file in that mode with `open` and with `Path.read_text`,
/// when the call names no `newline`. `subprocess.run` gives the output of a
/// child program in the same form with `text=True`
/// (`subprocess.py:1098-1100` of CPython, version 3.13). No other line break
/// changes: U+0085, U+2028 and the four separators stay.
///
/// A text with no CR comes back as it is, with no copy.
///
/// ```
/// use creche_util::pytext;
///
/// assert_eq!(
///     pytext::universal_newlines("one\r\ntwo\rthree\n"),
///     "one\ntwo\nthree\n"
/// );
/// assert_eq!(pytext::universal_newlines("a\r\r\nb"), "a\n\nb");
/// assert_eq!(pytext::universal_newlines("a\u{2028}b"), "a\u{2028}b");
/// ```
#[must_use]
pub fn universal_newlines(text: &str) -> Cow<'_, str> {
    if !text.contains('\r') {
        return Cow::Borrowed(text);
    }

    Cow::Owned(text.replace("\r\n", "\n").replace('\r', "\n"))
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

    /// The four information separators. They are space for Python `str` and
    /// not for `char::is_whitespace`.
    const SEPARATORS: [char; 4] = ['\u{1c}', '\u{1d}', '\u{1e}', '\u{1f}'];

    #[test]
    fn the_space_set_is_the_set_of_python() {
        let found: Vec<u32> = (0..=u32::from(char::MAX))
            .filter(|code| char::from_u32(*code).is_some_and(is_space))
            .collect();

        assert_eq!(found, PYTHON_SPACES);
    }

    #[test]
    fn the_four_separators_are_space_and_rust_does_not_trim_them() {
        for separator in SEPARATORS {
            let text = format!("{separator}a{separator}");

            assert!(PYTHON_SPACES.contains(&u32::from(separator)));
            assert!(is_space(separator), "{separator:?}");
            assert!(!separator.is_whitespace(), "{separator:?}");
            assert_eq!(strip(&text), "a", "{separator:?}");
            assert_eq!(text.trim(), text, "{separator:?}");
        }
    }

    /// Each answer is what `int` of Python 3.13 gives for the text: the
    /// number 7 for each space that is no separator, and a refusal for each
    /// separator. `float` gives the same answers for `7.5`.
    #[test]
    fn the_number_space_set_is_the_set_of_python_without_the_separators() {
        for code in PYTHON_SPACES {
            let space = char::from_u32(code).unwrap();
            let text = format!("{space}7{space}");
            let removed = !SEPARATORS.contains(&space);

            assert_eq!(number_strip(&text) == "7", removed, "{code:#x}");
        }

        // The walk of each character finds the same 25, and no other one.
        let found: Vec<u32> = (0..=u32::from(char::MAX))
            .filter(|code| {
                char::from_u32(*code)
                    .is_some_and(|character| number_strip(&character.to_string()).is_empty())
            })
            .collect();
        let wanted: Vec<u32> = PYTHON_SPACES
            .into_iter()
            .filter(|code| {
                !SEPARATORS
                    .iter()
                    .any(|separator| u32::from(*separator) == *code)
            })
            .collect();

        assert_eq!(found, wanted);
        assert_eq!(found.len(), 25);
    }

    #[test]
    fn a_separator_stays_at_each_end_of_a_number() {
        for separator in SEPARATORS {
            let at_the_start = format!("{separator}7");
            let at_the_end = format!("7{separator}");
            let at_both_ends = format!(" {separator}7{separator}\n");

            assert_eq!(number_strip(&at_the_start), at_the_start, "{separator:?}");
            assert_eq!(number_strip(&at_the_end), at_the_end, "{separator:?}");
            // The space outside a separator leaves, and the separator stays.
            assert_eq!(
                number_strip(&at_both_ends),
                format!("{separator}7{separator}"),
                "{separator:?}"
            );
        }
    }

    #[test]
    fn number_strip_keeps_a_character_that_is_no_space() {
        assert_eq!(number_strip("a7a"), "a7a");
        assert_eq!(number_strip(" 1 0\t"), "1 0");
        assert_eq!(number_strip("\u{200b}7"), "\u{200b}7");
        assert_eq!(number_strip(""), "");
    }

    #[test]
    fn strip_removes_the_space_of_python_at_the_two_ends() {
        assert_eq!(strip(" \t a b \u{1f}\u{a0}\n"), "a b");
        assert_eq!(strip("\u{3000}"), "");
        assert_eq!(strip("a"), "a");
    }

    #[test]
    fn strip_removes_each_space_of_python_and_keeps_the_space_inside() {
        for code in PYTHON_SPACES {
            let space = char::from_u32(code).unwrap();
            let inside = format!("a{space}b");

            assert_eq!(
                strip(&format!("{space}{inside}{space}")),
                inside,
                "{code:#x}"
            );
            assert_eq!(strip(&space.to_string()), "", "{code:#x}");
        }
    }

    /// Each answer is what `text.split()` of Python 3.13 gives.
    #[test]
    fn the_words_are_what_split_of_python_gives() {
        let table: [(&str, &[&str]); 8] = [
            ("", &[]),
            (" \t\n", &[]),
            ("  \u{1f} ", &[]),
            ("a", &["a"]),
            (" a  b\tc\n", &["a", "b", "c"]),
            ("a\u{1c}b\u{1d}c\u{1e}d\u{1f}e", &["a", "b", "c", "d", "e"]),
            ("a\u{a0}b\u{3000}c", &["a", "b", "c"]),
            // U+200B has a width of zero, and it is no space.
            ("a\u{200b}b", &["a\u{200b}b"]),
        ];
        for (text, wanted) in table {
            assert_eq!(words(text).collect::<Vec<_>>(), wanted, "{text:?}");
        }
    }

    #[test]
    fn each_space_of_python_ends_a_word() {
        for code in PYTHON_SPACES {
            let space = char::from_u32(code).unwrap();
            let text = format!("{space}a{space}{space}b{space}");

            assert_eq!(words(&text).collect::<Vec<_>>(), ["a", "b"], "{code:#x}");
        }
    }

    /// Each answer is what `Path.read_text` of Python 3.13 gives for a file
    /// with the text. The two `replace` calls of `subprocess.py:1098-1100`
    /// give the same answer.
    #[test]
    fn the_text_mode_of_python_reads_each_cr_as_one_line_feed() {
        let table = [
            ("", ""),
            ("token", "token"),
            ("a\nb", "a\nb"),
            ("a\rb", "a\nb"),
            ("a\r\nb", "a\nb"),
            ("a\r\r\nb", "a\n\nb"),
            ("a\n\rb", "a\n\nb"),
            ("\r", "\n"),
            ("\r\n", "\n"),
            ("token\r\n", "token\n"),
            ("\n\r", "\n\n"),
            ("\r\r\n", "\n\n"),
            ("\r\n\r\n", "\n\n"),
            ("\r\n\n\r", "\n\n\n"),
            ("a\rb\r\nc\nd\r", "a\nb\nc\nd\n"),
            ("caf\u{e9}\r\n", "caf\u{e9}\n"),
            // No other line break changes.
            (
                "\u{b}\u{c}\u{1c}\u{1d}\u{1e}\u{1f}",
                "\u{b}\u{c}\u{1c}\u{1d}\u{1e}\u{1f}",
            ),
            ("\u{85}", "\u{85}"),
            ("\u{2028}\u{2029}", "\u{2028}\u{2029}"),
        ];

        for (file, text) in table {
            assert_eq!(universal_newlines(file), text, "{file:?}");
        }

        assert!(matches!(universal_newlines("a\nb"), Cow::Borrowed(_)));
        assert!(matches!(universal_newlines("a\rb"), Cow::Owned(_)));
    }
}
