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
//!
//! # Two more refusals
//!
//! The same walk refuses two more kinds of text, before the crate `toml`
//! gets them:
//!
//! - **A text that is too deep for the stack of the crate.** The crate
//!   stops at 80 open lists and inline tables, and at 80 parts of one key.
//!   It counts the two apart, so 80 inline tables can each have a key of 80
//!   parts. Its parser uses the stack for each table of such a text, and a
//!   stack that overflows ends the process. [`holds`] refuses a key with
//!   more parts than its limit, and more open lists and inline tables than
//!   that limit. The reader calls [`holds`] before the crate. Each such
//!   text has more levels than the reader permits.
//! - **A dotted key that goes through a list of tables.** A `[[a.b]]`
//!   header makes the list of tables `a.b`. Under the header `[a]`, the key
//!   `b.c.d` then names a table in that list. No TOML version permits that
//!   key. The crate refuses the key `b.c`, and it reads a key of three
//!   parts or more.

use std::collections::{BTreeMap, BTreeSet};
use std::ops::Bound;

use toml_parser::decoder::Encoding;
use toml_parser::parser::{Event, EventKind, RecursionGuard, parse_document};
use toml_parser::{ParseError, Raw, Source};

/// A list or an inline table that is open at an event.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Open {
    List,
    InlineTable,
}

/// The keys of a text that are in no inline table: the keys of the headers
/// and of the key/value pairs below them.
///
/// A number stands for each part of a key. Two parts with one text have one
/// number. The walk compares and copies numbers, so the length of a key
/// adds nothing to the cost of a path.
#[derive(Default)]
struct Keys {
    /// The number of each part that the walk read.
    numbers: BTreeMap<String, usize>,
    /// The parts of the key that the walk reads.
    key: Vec<usize>,
    /// The path of the open header. The path of the top table is empty.
    header: Vec<usize>,
    /// The path of each list of tables. A `[[...]]` header adds its path.
    lists: BTreeSet<Vec<usize>>,
}

impl Keys {
    /// Adds one part to the key that the walk reads. `false` for a part
    /// that is no key.
    fn part(&mut self, raw: Raw<'_>) -> bool {
        let mut part = String::new();
        let mut fault: Option<ParseError> = None;
        raw.decode_key(&mut part, &mut fault);

        let next = self.numbers.len();
        self.key.push(*self.numbers.entry(part).or_insert(next));

        fault.is_none()
    }

    /// The end of a `[...]` header: its key is the path of the open header.
    fn table_header(&mut self) {
        self.header = std::mem::take(&mut self.key);
    }

    /// The end of a `[[...]]` header: it adds one item to the list at its
    /// path. That item holds no list yet, so each path below it leaves.
    ///
    /// The paths below a path come directly after it in the set. The walk
    /// reads only those, and not the whole set: a text can hold a header on
    /// each line.
    fn list_header(&mut self) {
        self.table_header();

        let after = (Bound::Excluded(&self.header), Bound::Unbounded);
        let below: Vec<Vec<usize>> = self
            .lists
            .range::<Vec<usize>, _>(after)
            .take_while(|path| path.starts_with(&self.header))
            .cloned()
            .collect();
        for path in &below {
            self.lists.remove(path);
        }
        self.lists.insert(self.header.clone());
    }

    /// The end of the key of a key/value pair. Whether the key names a list
    /// of tables, or goes through one. The crate refuses the first kind.
    ///
    /// The key of a pair of an inline table has no part here, so the answer
    /// for that pair is `false`.
    fn pair_enters_list(&mut self) -> bool {
        let mut path = self.header.clone();

        std::mem::take(&mut self.key).into_iter().any(|part| {
            path.push(part);
            self.lists.contains(&path)
        })
    }
}

/// Whether `text` holds none of the six forms that TOML 1.1 added, and
/// neither of the two other forms that the doc comment of the module names.
///
/// The answer for a text that is no TOML can be `true`: the caller then
/// gives the text to the crate `toml`. The check of a time reads the place
/// of a colon, and only a valid time has its colons in a fixed place.
///
/// `depth_max` is the most lists and inline tables that the parser opens
/// inside each other, and the most parts of one key. The answer for a
/// deeper text is `false`. The parser calls itself for each list and for
/// each inline table, so the limit bounds its stack.
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
    let mut key_parts: u32 = 0;
    let mut keys = Keys::default();

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

                if kind == EventKind::SimpleKey {
                    key_parts = key_parts.saturating_add(1);
                    if key_parts > depth_max || (open.is_empty() && !keys.part(raw)) {
                        return false;
                    }
                }
            }
            EventKind::KeyValSep => {
                key_parts = 0;
                if keys.pair_enters_list() {
                    return false;
                }
            }
            EventKind::StdTableClose => {
                key_parts = 0;
                keys.table_header();
            }
            EventKind::ArrayTableClose => {
                key_parts = 0;
                keys.list_header();
            }
            EventKind::Error => return false,
            EventKind::StdTableOpen
            | EventKind::ArrayTableOpen
            | EventKind::KeySep
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

    /// What the check says about a text.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Is {
        /// The text is TOML 1.0: the check passes it.
        Toml10,
        /// The text holds a form that TOML 1.1 added: the check refuses it.
        Newer,
    }

    /// Each form that TOML 1.1 added, in three places: at the top level, in
    /// a nested inline table and in a list of inline tables. A key in `"` is
    /// a fourth place of an escape. Then TOML 1.0 texts. The first of them
    /// hold the characters of a newer form in a place where they start no
    /// form.
    const TEXTS: [(Is, &str); 70] = [
        // A line end inside an inline table.
        (Is::Newer, "a = {\n b = 1 }\n"),
        (Is::Newer, "a = {b = 1\n}\n"),
        (Is::Newer, "a = {b = 1,\n c = 2}\n"),
        (Is::Newer, "a = {b = 1 # a comment\n}\n"),
        (Is::Newer, "a = {b = 1\r\n}\n"),
        (Is::Newer, "t = {u = {b = 1\n}}\n"),
        (Is::Newer, "t = {u = {b = 1}\n}\n"),
        (Is::Newer, "t = [{b = 1\n}]\n"),
        (Is::Newer, "t = {u = [{b = 1\n}]}\n"),
        // A comma before the `}` of an inline table.
        (Is::Newer, "a = {b = 1,}\n"),
        (Is::Newer, "a = {b = 1 , }\n"),
        (Is::Newer, "t = {u = {b = 1,}}\n"),
        (Is::Newer, "t = [{b = 1,}, {c = 2}]\n"),
        // The escape `\e`.
        (Is::Newer, "a = \"\\e\"\n"),
        (Is::Newer, "a = \"\"\"\\e\"\"\"\n"),
        (Is::Newer, "a = \"\\\\\\e\"\n"),
        (Is::Newer, "\"\\e\" = 1\n"),
        (Is::Newer, "t = {u = {a = \"\\e\"}}\n"),
        (Is::Newer, "t = [{a = \"\\e\"}]\n"),
        // The escape `\x`.
        (Is::Newer, "a = \"\\x41\"\n"),
        (Is::Newer, "a = \"\"\"\\x41\"\"\"\n"),
        (Is::Newer, "\"\\x41\" = 1\n"),
        (Is::Newer, "t = {u = {a = \"\\x41\"}}\n"),
        (Is::Newer, "t = [{a = \"\\x41\"}]\n"),
        // A time with no seconds.
        (Is::Newer, "a = 07:32\n"),
        (Is::Newer, "t = {u = {a = 07:32}}\n"),
        (Is::Newer, "t = [{a = 07:32}]\n"),
        // A date-time with no seconds, in each form of a date-time.
        (Is::Newer, "a = 1979-05-27T07:32Z\n"),
        (Is::Newer, "a = 1979-05-27T07:32\n"),
        (Is::Newer, "a = 1979-05-27 07:32\n"),
        (Is::Newer, "a = 1979-05-27t07:32z\n"),
        (Is::Newer, "a = 1979-05-27T07:32+01:00\n"),
        (Is::Newer, "a = 1979-05-27T07:32-08:00\n"),
        (Is::Newer, "t = {u = {a = 1979-05-27T07:32Z}}\n"),
        (Is::Newer, "t = [{a = 1979-05-27T07:32Z}]\n"),
        // The characters of a newer form in a basic string.
        (Is::Toml10, "a = \"07:32 {b = 1,} 1979-05-27T07:32Z\"\n"),
        (Is::Toml10, "a = \"\\\\e \\\\x41\"\n"),
        (Is::Toml10, "\"07:32 {b = 1,}\" = 1\n"),
        // In a literal string, which holds no escape.
        (Is::Toml10, "a = '\\e \\x41 07:32 {b = 1,}'\n"),
        (Is::Toml10, "'\\e \\x41' = 1\n"),
        // In a multi-line string.
        (Is::Toml10, "a = \"\"\"\n07:32\n{b = 1,\n}\n\"\"\"\n"),
        (Is::Toml10, "a = '''\n\\e \\x41 07:32\n{b = 1,\n}\n'''\n"),
        (Is::Toml10, "a = {b = \"\"\"\n\"\"\", c = '''\n'''}\n"),
        // In a comment.
        (Is::Toml10, "a = 1 # \\e \\x41 07:32 {b = 1,}\n"),
        (Is::Toml10, "# {b = 1,\n# }\n"),
        // A line end inside a list of an inline table.
        (Is::Toml10, "a = {b = [1,\n 2], c = 3}\n"),
        (Is::Toml10, "a = {b = [\n], c = [ # a comment\n 1,\n]}\n"),
        (Is::Toml10, "t = [{a = [\n 1,\n]}]\n"),
        // A comma at the end of a list.
        (Is::Toml10, "a = [1, 2,]\n"),
        (Is::Toml10, "a = [{b = 1}, {c = 2},]\n"),
        (Is::Toml10, "a = [\n {b = 1},\n {c = 2},\n]\n"),
        // An inline table on one line.
        (Is::Toml10, "a = {}\n"),
        (Is::Toml10, "a = { }\n"),
        (Is::Toml10, "a = {b = 1, c = {d = 2}}\n"),
        (Is::Toml10, "a = {b = 1} # a comment\n"),
        (Is::Toml10, "a = {b = 1}\r\n"),
        // Each escape of TOML 1.0.
        (
            Is::Toml10,
            "a = \"\\b\\t\\n\\f\\r\\\"\\\\\\u00e9\\U0001F600\"\n",
        ),
        (Is::Toml10, "a = \"\"\"one \\\n   two\"\"\"\n"),
        // A time with seconds.
        (Is::Toml10, "a = 07:32:00\n"),
        (Is::Toml10, "a = 07:32:00.5\n"),
        (Is::Toml10, "a = 1979-05-27\n"),
        (Is::Toml10, "a = 1979-05-27T07:32:00\n"),
        (Is::Toml10, "a = 1979-05-27 07:32:00.999999\n"),
        (Is::Toml10, "a = 1979-05-27T07:32:00Z\n"),
        (Is::Toml10, "a = 1979-05-27T07:32:00+01:00\n"),
        // A text with none of the characters.
        (Is::Toml10, ""),
        (Is::Toml10, "\n"),
        (Is::Toml10, "# a comment"),
        (Is::Toml10, "a = 1"),
        (Is::Toml10, "[t]\n[[u]]\na.b = [1, [2]]\n"),
    ];

    /// The crate reads `text`. A row that the crate refuses proves nothing
    /// about the check.
    fn crate_reads(text: &str) -> bool {
        text.parse::<toml::Table>().is_ok()
    }

    #[test]
    fn the_check_passes_toml_10_and_refuses_each_newer_form() {
        for (wanted, text) in TEXTS {
            let found = if holds(text, DEPTH) {
                Is::Toml10
            } else {
                Is::Newer
            };

            assert!(crate_reads(text), "the crate reads {text:?}");
            assert_eq!(found, wanted, "{text:?}");
        }
    }

    #[test]
    fn the_table_holds_each_form_in_each_place() {
        // The six forms, each as the text after a key and its `=`.
        let forms = [
            "{b = 1\n}",
            "{b = 1,}",
            "\"\\e\"",
            "\"\\x41\"",
            "07:32",
            "1979-05-27T07:32Z",
        ];
        // The three places, as the text before a scalar and before an inline
        // table: the top level, a nested inline table and a list of inline
        // tables.
        let places = [
            ("a = ", "a = "),
            ("t = {u = {a = ", "t = {u = "),
            ("t = [{a = ", "t = ["),
        ];

        for form in forms {
            for (before_scalar, before_table) in places {
                let before = if form.starts_with('{') {
                    before_table
                } else {
                    before_scalar
                };
                let start = format!("{before}{form}");
                let found = TEXTS
                    .iter()
                    .any(|(is, text)| *is == Is::Newer && text.starts_with(&start));

                assert!(found, "no row starts with {start:?}");
            }
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

    #[test]
    fn a_key_with_more_parts_than_the_limit_is_refused() {
        let key = |parts: usize| vec!["a"; parts].join(".");
        let quoted = |parts: usize| vec!["\"a\""; parts].join(" . ");
        // Each place of a key: a key/value pair, the two kinds of header and
        // a pair of an inline table.
        let places: [fn(&str) -> String; 4] = [
            |key| format!("{key} = 1\n"),
            |key| format!("[{key}]\n"),
            |key| format!("[[{key}]]\n"),
            |key| format!("t = {{{key} = 1}}\n"),
        ];

        for place in places {
            assert!(holds(&place(&key(8)), DEPTH));
            assert!(!holds(&place(&key(9)), DEPTH));
            assert!(holds(&place(&quoted(8)), DEPTH));
            assert!(!holds(&place(&quoted(9)), DEPTH));
        }
    }

    #[test]
    fn the_count_of_the_parts_starts_again_at_each_key() {
        // Each text has more than 8 parts in two keys that follow each
        // other, and no key with more than 5.
        let texts = [
            "a.b = 1\nc.d = 2\ne.f = 3\ng.h = 4\ni.j = 5\n",
            "t = {a.b = 1, c.d = 2, e.f = 3, g.h = 4, i.j = 5}\n",
            "[a.b.c.d.e]\n[f.g.h.i]\n[[j.k.l.m.n]]\no.p.q.r = 1\n",
            "[a.b.c.d]\ne.f.g = {h.i.j = [{k.l.m = 1}]}\n",
        ];

        for text in texts {
            assert!(crate_reads(text), "the crate reads {text:?}");
            assert!(holds(text, DEPTH), "{text:?}");
        }
    }

    #[test]
    fn a_key_through_a_list_of_tables_is_refused() {
        let refused = [
            "[[a.b]]\n[a]\nb.c = 1\n",
            "[[a.b]]\n[a]\nb.c.d = 1\n",
            // Each form of one key part is that part.
            "[[a.b]]\n[a]\n\"b\".c.d = 1\n",
            "[[a.b]]\n[a]\n'b'.c.d = 1\n",
            "[[a.\"\\u0062\"]]\n[a]\nb.c.d = 1\n",
            // The list `a.b` has two items.
            "[[a.b]]\n[[a.b]]\n[a]\nb.c.d = 1\n",
            // A list below an item of another list.
            "[[a]]\n[[a.b.c]]\n[a.b]\nc.d.e = 1\n",
            // A new item of the list `a` removes the list `a.b.c`, and no
            // list at another path.
            "[[a]]\n[[a.b.c]]\n[[x.y]]\n[[a]]\n[x]\ny.z.w = 1\n",
            "[[x.y]]\n[[a]]\n[[a.b.c]]\n[[a]]\n[x]\ny.z.w = 1\n",
        ];
        let passed = [
            "[[a]]\n[[a.b.c]]\n[[a]]\n[a.b]\nc.d.e = 1\n",
            "[[a]]\n[[a.b]]\n[[a]]\nb.c.d = 1\n",
            // A key of an inline table starts at that table.
            "[[a.b]]\n[a]\nx = {b.c.d = 1}\ny = 2\n",
            "[[a.b]]\n[a]\nx = [{b.c.d = 1}]\ny = 2\n",
            // A key below the header of a list is a key of its last item.
            "[[a.b]]\nb.c.d = 1\n",
            "[[a.b]]\n[a]\nc.d.e = 1\n",
            "[[a.b]]\n[c]\nb.c.d = 1\n",
            "[[a.b]]\n[a]\n\"B\".c.d = 1\n",
        ];

        for text in refused {
            assert!(!holds(text, DEPTH), "{text:?}");
        }
        for text in passed {
            assert!(crate_reads(text), "the crate reads {text:?}");
            assert!(holds(text, DEPTH), "{text:?}");
        }
    }

    #[test]
    fn a_key_part_that_is_no_key_is_refused() {
        for text in ["\"\\q\" = 1\n", "[\"\\q\"]\n", "[[a.\"\\q\"]]\n"] {
            assert!(!crate_reads(text), "the crate refuses {text:?}");
            assert!(!holds(text, DEPTH), "{text:?}");
        }
    }
}
