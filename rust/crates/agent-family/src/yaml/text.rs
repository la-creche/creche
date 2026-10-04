//! The text that Python writes for a value: `repr` of a string and of bytes.
//!
//! PyYAML and pydantic put such a text into a message or into a location. The
//! report of this crate holds the same text.

use std::fmt::Write;

/// The last code point that Python writes as `\xNN`.
const LAST_BYTE_ESCAPE: u32 = 0xff;

/// The last code point that Python writes as `\uNNNN`.
const LAST_SHORT_ESCAPE: u32 = 0xffff;

/// The code points outside ASCII that Python does not print as they are:
/// separators, format characters and private use. Each row is one range,
/// with both ends.
///
/// CONTRACT-QUESTION: no contract gives the text of a YAML error. Python
/// takes this set from the Unicode database of its own version. It also
/// escapes each code point that its version does not assign. This table has
/// no such code point, so a message that quotes one differs from the Python
/// message. An equal message costs a table of those code points for one
/// Unicode version, and a new table for each later version.
const NOT_PRINTED: [(u32, u32); 27] = [
    (0x80, 0xa0),
    (0xad, 0xad),
    (0x600, 0x605),
    (0x61c, 0x61c),
    (0x6dd, 0x6dd),
    (0x70f, 0x70f),
    (0x890, 0x891),
    (0x8e2, 0x8e2),
    (0x1680, 0x1680),
    (0x180e, 0x180e),
    (0x2000, 0x200f),
    (0x2028, 0x202f),
    (0x205f, 0x2064),
    (0x2066, 0x206f),
    (0x3000, 0x3000),
    (0xe000, 0xf8ff),
    (0xfeff, 0xfeff),
    (0xfff9, 0xfffb),
    (0x110bd, 0x110bd),
    (0x110cd, 0x110cd),
    (0x13430, 0x1343f),
    (0x1bca0, 0x1bca3),
    (0x1d173, 0x1d17a),
    (0xe0001, 0xe0001),
    (0xe0020, 0xe007f),
    (0xf0000, 0xffffd),
    (0x10_0000, 0x10_fffd),
];

fn printed(code: u32) -> bool {
    !NOT_PRINTED
        .iter()
        .any(|(first, last)| (*first..=*last).contains(&code))
}

/// Which quote Python puts around a text: `'`, or `"` when the text holds a
/// `'` and no `"`.
fn quote_for(holds: impl Fn(char) -> bool) -> char {
    if holds('\'') && !holds('"') {
        '"'
    } else {
        '\''
    }
}

fn push_escape(out: &mut String, code: u32) {
    // A write to a `String` cannot fail.
    let _ = if code <= LAST_BYTE_ESCAPE {
        write!(out, "\\x{code:02x}")
    } else if code <= LAST_SHORT_ESCAPE {
        write!(out, "\\u{code:04x}")
    } else {
        write!(out, "\\U{code:08x}")
    };
}

/// `repr(text)` of a Python `str`.
pub(crate) fn repr_str(text: &str) -> String {
    let quote = quote_for(|c| text.contains(c));
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if c == quote => {
                out.push('\\');
                out.push(c);
            }
            ' '..='~' => out.push(c),
            _ if c.is_ascii() => push_escape(&mut out, u32::from(c)),
            _ if printed(u32::from(c)) => out.push(c),
            _ => push_escape(&mut out, u32::from(c)),
        }
    }

    out.push(quote);
    out
}

/// `repr(c)` of a Python `str` of one character.
pub(crate) fn repr_char(c: char) -> String {
    let mut buffer = [0_u8; 4];

    repr_str(c.encode_utf8(&mut buffer))
}

/// `repr(data)` of a Python `bytes`.
pub(crate) fn repr_bytes(data: &[u8]) -> String {
    let quote = quote_for(|c| data.iter().any(|byte| char::from(*byte) == c));
    let mut out = String::with_capacity(data.len() + 3);
    out.push('b');
    out.push(quote);
    for byte in data {
        let c = char::from(*byte);
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if c == quote => {
                out.push('\\');
                out.push(c);
            }
            ' '..='~' => out.push(c),
            _ => push_escape(&mut out, u32::from(*byte)),
        }
    }

    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::{repr_bytes, repr_char, repr_str};

    #[test]
    fn a_str_repr_is_the_python_text() {
        let cases = [
            ("abc", "'abc'"),
            ("it's", "\"it's\""),
            ("it's \"x\"", "'it\\'s \"x\"'"),
            ("a\tb\n", "'a\\tb\\n'"),
            ("\0", "'\\x00'"),
            ("\u{7f}", "'\\x7f'"),
            ("\u{85}", "'\\x85'"),
            ("\u{a0}", "'\\xa0'"),
            ("\u{2028}", "'\\u2028'"),
            ("\u{feff}", "'\\ufeff'"),
            ("\u{e9}", "'\u{e9}'"),
            ("\u{e0001}", "'\\U000e0001'"),
            ("back\\slash", "'back\\\\slash'"),
        ];
        for (text, said) in cases {
            assert_eq!(repr_str(text), said, "{text:?}");
        }
    }

    #[test]
    fn a_char_repr_is_the_python_text() {
        assert_eq!(repr_char('\t'), "'\\t'");
        assert_eq!(repr_char('\''), "\"'\"");
        assert_eq!(repr_char('@'), "'@'");
    }

    #[test]
    fn a_bytes_repr_is_the_python_text() {
        assert_eq!(repr_bytes(b"hi"), "b'hi'");
        assert_eq!(repr_bytes(b"\xff\x00"), "b'\\xff\\x00'");
        assert_eq!(repr_bytes(b"it's"), "b\"it's\"");
        assert_eq!(repr_bytes(b"a\\b\n"), "b'a\\\\b\\n'");
    }
}
