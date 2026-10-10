//! The check that holds a text to TOML 1.0.
//!
//! The contract of each file kind is TOML 1.0. The crate `toml` reads TOML
//! 1.1 and has no switch for the older version. [`holds`] refuses each of
//! the six forms that TOML 1.1 added:
//!
//! | Form | Example |
//! |---|---|
//! | A line end inside an inline table: between its `{` and its `}`, outside a string and outside a list of that table. | `a = {b = 1` and `}` on two lines |
//! | A comma as the last sign before the `}` of an inline table. | `a = {b = 1,}` |
//! | The escape `\e` in a basic string. | `a = "\e"` |
//! | The escape `\x` in a basic string. | `a = "\x41"` |
//! | A time with no seconds. | `a = 07:32` |
//! | A date-time with no seconds. | `a = 1979-05-27T07:32Z` |
//!
//! A basic string is a string in `"` or in `"""`. A key in `"` is one too. A
//! literal string holds no escape. A character in a string or in a comment
//! starts none of the four other forms.
//!
//! The check reads the events of the crate `toml_parser`, which is the
//! parser of the crate `toml`. An event is one part of the text: a key, a
//! scalar, a comma, a line end, or the start or the end of a list or of an
//! inline table. The first two forms are a sequence of events. The four
//! other forms are in the text of one key or of one scalar.
//!
//! A Python version can read TOML 1.1 in its standard library. The Python
//! reader of a file kind thus has a check of its own for the same six
//! forms. No reader takes a text because its parser is new.

use toml_parser::decoder::Encoding;
use toml_parser::parser::{Event, EventKind, RecursionGuard, parse_document};
use toml_parser::{ParseError, Source};

/// A list or an inline table that is open at an event.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Open {
    List,
    InlineTable,
}

/// Whether `text` holds none of the six forms that TOML 1.1 added.
///
/// Call it for a text that the crate `toml` read. The check of a time reads
/// the place of a colon, and only a valid time has its colons in a fixed
/// place.
///
/// `depth_max` is the most lists and inline tables that the parser opens
/// inside each other. The answer for a deeper text is `false`. The parser
/// calls itself for each one, so the limit bounds its stack.
pub(super) fn holds(text: &str, depth_max: u32) -> bool {
    let source = Source::new(text);
    let tokens = source.lex().into_vec();
    let mut events: Vec<Event> = Vec::with_capacity(tokens.len());
    let mut fault: Option<ParseError> = None;
    parse_document(
        &tokens,
        &mut RecursionGuard::new(&mut events, depth_max),
        &mut fault,
    );
    if fault.is_some() {
        return false;
    }

    let mut open: Vec<Open> = Vec::new();
    let mut after_comma = false;

    for event in &events {
        let kind = event.kind();
        match kind {
            // White space and a comment are no sign: a comma before them is
            // still the last sign.
            EventKind::Whitespace | EventKind::Comment => continue,
            EventKind::Newline => {
                if open.last() == Some(&Open::InlineTable) {
                    return false;
                }
                continue;
            }
            EventKind::InlineTableOpen => open.push(Open::InlineTable),
            EventKind::ArrayOpen => open.push(Open::List),
            EventKind::InlineTableClose => {
                if after_comma {
                    return false;
                }
                open.pop();
            }
            EventKind::ArrayClose => {
                open.pop();
            }
            EventKind::SimpleKey | EventKind::Scalar => {
                let Some(raw) = source.get(event) else {
                    return false;
                };
                let newer = match event.encoding() {
                    Some(Encoding::BasicString | Encoding::MlBasicString) => {
                        has_newer_escape(raw.as_str())
                    }
                    Some(Encoding::LiteralString | Encoding::MlLiteralString) => false,
                    None => kind == EventKind::Scalar && lacks_seconds(raw.as_str()),
                };
                if newer {
                    return false;
                }
            }
            EventKind::Error => return false,
            EventKind::StdTableOpen
            | EventKind::StdTableClose
            | EventKind::ArrayTableOpen
            | EventKind::ArrayTableClose
            | EventKind::KeySep
            | EventKind::KeyValSep
            | EventKind::ValueSep => {}
        }

        after_comma = kind == EventKind::ValueSep;
    }

    true
}

/// Whether the text of a basic string holds the escape `\e` or `\x`.
///
/// The character after a `\` is a part of that escape. The `e` of `\\e` is
/// thus a plain letter.
fn has_newer_escape(raw: &str) -> bool {
    let mut chars = raw.chars();
    while let Some(char) = chars.next() {
        if char == '\\' && matches!(chars.next(), Some('e' | 'x')) {
            return true;
        }
    }

    false
}

/// Whether a scalar with no quotes is a time or a date-time with no seconds.
///
/// Only a time holds a colon. Its hour and its minute have two digits each,
/// so the colon of the seconds is the third byte after the first colon. An
/// offset comes after the seconds.
fn lacks_seconds(raw: &str) -> bool {
    const TO_SECONDS: usize = 3;

    raw.find(':')
        .is_some_and(|colon| raw.as_bytes().get(colon + TO_SECONDS) != Some(&b':'))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The most lists and inline tables of a text of the table below.
    const DEPTH: u32 = 8;

    /// Each form that TOML 1.1 added, in three places: at the top level, in
    /// a nested inline table and in a list of inline tables. A key in `"` is
    /// a fourth place of an escape.
    const NEWER: [&str; 26] = [
        // A line end inside an inline table.
        "a = {\n b = 1 }\n",
        "a = {b = 1\n}\n",
        "a = {b = 1,\n c = 2}\n",
        "a = {b = 1 # a comment\n}\n",
        "a = {b = 1\r\n}\n",
        "t = {u = {b = 1\n}}\n",
        "t = {u = {b = 1}\n}\n",
        "t = [{b = 1\n}]\n",
        "t = {u = [{b = 1\n}]}\n",
        // A comma before the `}` of an inline table.
        "a = {b = 1,}\n",
        "a = {b = 1 , }\n",
        "t = {u = {b = 1,}}\n",
        "t = [{b = 1,}, {c = 2}]\n",
        // The escape `\e`.
        "a = \"\\e\"\n",
        "a = \"\"\"\\e\"\"\"\n",
        "a = \"\\\\\\e\"\n",
        "\"\\e\" = 1\n",
        "t = {u = {a = \"\\e\"}}\n",
        "t = [{a = \"\\e\"}]\n",
        // The escape `\x`.
        "a = \"\\x41\"\n",
        "a = \"\"\"\\x41\"\"\"\n",
        "\"\\x41\" = 1\n",
        "t = {u = {a = \"\\x41\"}}\n",
        "t = [{a = \"\\x41\"}]\n",
        // A time with no seconds, and a date-time with no seconds.
        "a = 07:32\n",
        "a = 1979-05-27T07:32Z\n",
    ];

    /// More texts with no seconds: each form of a date-time, and the two
    /// other places.
    const NO_SECONDS: [&str; 9] = [
        "a = 1979-05-27T07:32\n",
        "a = 1979-05-27 07:32\n",
        "a = 1979-05-27t07:32z\n",
        "a = 1979-05-27T07:32+01:00\n",
        "a = 1979-05-27T07:32-08:00\n",
        "t = {u = {a = 07:32}}\n",
        "t = [{a = 07:32}]\n",
        "t = {u = {a = 1979-05-27T07:32Z}}\n",
        "t = [{a = 1979-05-27T07:32Z}]\n",
    ];

    /// TOML 1.0 texts. The first rows hold the characters of a newer form in
    /// a place where they start no form.
    const TOML_10: [&str; 30] = [
        // In a basic string.
        "a = \"07:32 {b = 1,} 1979-05-27T07:32Z\"\n",
        "a = \"\\\\e \\\\x41\"\n",
        "\"07:32 {b = 1,}\" = 1\n",
        // In a literal string, which holds no escape.
        "a = '\\e \\x41 07:32 {b = 1,}'\n",
        "'\\e \\x41' = 1\n",
        // In a multi-line string.
        "a = \"\"\"\n07:32\n{b = 1,\n}\n\"\"\"\n",
        "a = '''\n\\e \\x41 07:32\n{b = 1,\n}\n'''\n",
        "a = {b = \"\"\"\n\"\"\", c = '''\n'''}\n",
        // In a comment.
        "a = 1 # \\e \\x41 07:32 {b = 1,}\n",
        "# {b = 1,\n# }\n",
        // A line end inside a list of an inline table.
        "a = {b = [1,\n 2], c = 3}\n",
        "a = {b = [\n], c = [ # a comment\n 1,\n]}\n",
        "t = [{a = [\n 1,\n]}]\n",
        // A comma at the end of a list.
        "a = [1, 2,]\n",
        "a = [{b = 1}, {c = 2},]\n",
        "a = [\n {b = 1},\n {c = 2},\n]\n",
        // An inline table on one line.
        "a = {}\n",
        "a = { }\n",
        "a = {b = 1, c = {d = 2}}\n",
        "a = {b = 1} # a comment\n",
        "a = {b = 1}\r\n",
        // Each escape of TOML 1.0.
        "a = \"\\b\\t\\n\\f\\r\\\"\\\\\\u00e9\\U0001F600\"\n",
        "a = \"\"\"one \\\n   two\"\"\"\n",
        // A time with seconds.
        "a = 07:32:00\n",
        "a = 07:32:00.5\n",
        "a = 1979-05-27\n",
        "a = 1979-05-27T07:32:00\n",
        "a = 1979-05-27 07:32:00.999999\n",
        "a = 1979-05-27T07:32:00Z\n",
        "a = 1979-05-27T07:32:00+01:00\n",
    ];

    /// The crate reads `text`. A row that the crate refuses proves nothing
    /// about the check.
    fn crate_reads(text: &str) -> bool {
        text.parse::<toml::Table>().is_ok()
    }

    #[test]
    fn each_form_of_toml_11_is_refused_in_each_place() {
        for text in NEWER.iter().chain(&NO_SECONDS) {
            assert!(crate_reads(text), "the crate reads {text:?}");
            assert!(!holds(text, DEPTH), "{text:?}");
        }
    }

    #[test]
    fn a_text_of_toml_10_passes() {
        for text in TOML_10 {
            assert!(crate_reads(text), "the crate reads {text:?}");
            assert!(holds(text, DEPTH), "{text:?}");
        }
    }

    #[test]
    fn a_text_with_no_newer_form_passes() {
        for text in [
            "",
            "\n",
            "# a comment",
            "a = 1",
            "[t]\n[[u]]\na.b = [1, [2]]\n",
        ] {
            assert!(holds(text, DEPTH), "{text:?}");
        }
    }

    #[test]
    fn a_text_that_opens_too_many_lists_is_refused() {
        let lists = |count: usize| format!("a = {}{}\n", "[".repeat(count), "]".repeat(count));
        let tables =
            |count: usize| format!("a = {}1{}\n", "{a = ".repeat(count), "}".repeat(count));

        assert!(holds(&lists(8), DEPTH));
        assert!(!holds(&lists(9), DEPTH));
        assert!(holds(&tables(8), DEPTH));
        assert!(!holds(&tables(9), DEPTH));
    }

    #[test]
    fn a_text_that_is_no_toml_is_refused() {
        for text in ["a = {b = 1", "a = [1", "a = {b = 1}}"] {
            assert!(!crate_reads(text), "the crate refuses {text:?}");
            assert!(!holds(text, DEPTH), "{text:?}");
        }
    }
}
