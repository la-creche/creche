//! The text of one field of a line from the playpen.
//!
//! A JSON string can hold `\ud800`, a surrogate with no partner. The Python
//! host reads such a string and keeps it, so [`Text`] keeps it too. A Rust
//! `String` cannot hold one.

use std::borrow::Cow;
use std::fmt;

/// The count of bytes of one lone surrogate, as the `surrogatepass` handler
/// of Python encodes it.
pub(super) const LONE_SURROGATE_BYTES: usize = 3;

/// The count of UTF-16 code units of one lone surrogate.
const LONE_SURROGATE_UNITS: usize = 1;

/// A text that the sandbox wrote. It is a claim, and it can hold a lone
/// surrogate (contract 03 §13).
///
/// A playpen of an older image cut a long message at a count of UTF-16 code
/// units. That cut can fall inside a surrogate pair, and the line then holds
/// one half. The host keeps that text, as the Python implementation does.
///
/// ```
/// use creche_contracts::channel::text::Text;
///
/// let text = Text::from("exit 137");
/// assert_eq!(text.as_str(), Some("exit 137"));
/// assert_eq!(text, "exit 137");
/// ```
///
/// Code outside this module cannot name the field:
///
/// ```compile_fail,E0616
/// use creche_contracts::channel::text::Text;
///
/// let text = Text::from("exit 137");
/// let form = text.0;
/// ```
#[derive(Clone, PartialEq, Eq, Hash)]
pub struct Text(Repr);

/// The two forms of a [`Text`]. `Units` always holds one lone surrogate or
/// more, so two equal texts have the same form.
#[derive(Clone, PartialEq, Eq, Hash)]
enum Repr {
    Str(String),
    Units(Vec<u16>),
}

impl Text {
    /// The text with no character.
    pub(super) const fn empty() -> Self {
        Self(Repr::Str(String::new()))
    }

    /// The text of these UTF-16 code units.
    pub(super) fn from_units(units: Vec<u16>) -> Self {
        match String::from_utf16(&units) {
            Ok(text) => Self(Repr::Str(text)),
            Err(_) => Self(Repr::Units(units)),
        }
    }

    /// The text as a `str`. `None` for a text that holds a lone surrogate.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match &self.0 {
            Repr::Str(text) => Some(text),
            Repr::Units(_) => None,
        }
    }

    /// The text for a log line or a page. Each lone surrogate becomes U+FFFD.
    #[must_use]
    pub fn to_string_lossy(&self) -> Cow<'_, str> {
        match &self.0 {
            Repr::Str(text) => Cow::Borrowed(text),
            Repr::Units(units) => Cow::Owned(String::from_utf16_lossy(units)),
        }
    }

    /// The UTF-16 code units of the text.
    #[must_use]
    pub fn to_utf16(&self) -> Vec<u16> {
        match &self.0 {
            Repr::Str(text) => text.encode_utf16().collect(),
            Repr::Units(units) => units.clone(),
        }
    }

    /// Whether the text has no character.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        match &self.0 {
            Repr::Str(text) => text.is_empty(),
            Repr::Units(units) => units.is_empty(),
        }
    }

    /// The longest start of the text that has `max` bytes of UTF-8 at most
    /// (contract 03 §8). The cut is between two characters, so the result
    /// holds no part of a character.
    // CONTRACT-QUESTION: contract 03 §8 gives each cap of a text in bytes and
    // does not say what a lone surrogate counts. A lone surrogate has no
    // UTF-8 form. It counts as `LONE_SURROGATE_BYTES` here and stays in the
    // text, as in the Python host. A change costs one arm of this function.
    pub(super) fn cut_bytes(self, max: usize) -> Self {
        match self.0 {
            Repr::Str(mut text) => {
                text.truncate(text.floor_char_boundary(max));

                Self(Repr::Str(text))
            }
            Repr::Units(mut units) => {
                let mut bytes = 0_usize;
                let mut end = 0_usize;
                for point in char::decode_utf16(units.iter().copied()) {
                    let (size, width) = match point {
                        Ok(character) => (character.len_utf8(), character.len_utf16()),
                        Err(_) => (LONE_SURROGATE_BYTES, LONE_SURROGATE_UNITS),
                    };
                    bytes = bytes.saturating_add(size);
                    if bytes > max {
                        break;
                    }

                    end = end.saturating_add(width);
                }

                units.truncate(end);

                Self::from_units(units)
            }
        }
    }

    /// The text before the first `separator`, or the whole text.
    pub(super) fn before(&self, separator: char) -> Self {
        match &self.0 {
            Repr::Str(text) => {
                let head = text.split(separator).next().unwrap_or_default();

                Self(Repr::Str(head.to_owned()))
            }
            Repr::Units(units) => {
                let mut buffer = [0_u16; 2];
                let wanted = separator.encode_utf16(&mut buffer);
                let end = units
                    .windows(wanted.len())
                    .position(|window| window == wanted)
                    .unwrap_or(units.len());

                Self::from_units(units.iter().copied().take(end).collect())
            }
        }
    }
}

impl From<String> for Text {
    fn from(text: String) -> Self {
        Self(Repr::Str(text))
    }
}

impl From<&str> for Text {
    fn from(text: &str) -> Self {
        Self(Repr::Str(text.to_owned()))
    }
}

impl PartialEq<str> for Text {
    fn eq(&self, other: &str) -> bool {
        self.as_str() == Some(other)
    }
}

impl PartialEq<&str> for Text {
    fn eq(&self, other: &&str) -> bool {
        self.as_str() == Some(*other)
    }
}

impl fmt::Debug for Text {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.0 {
            Repr::Str(text) => write!(f, "{text:?}"),
            Repr::Units(units) => write!(f, "utf16{units:?}"),
        }
    }
}

/// Builds a [`Text`] from the parts of one JSON string.
pub(super) struct TextBuilder(Repr);

impl TextBuilder {
    pub(super) const fn new() -> Self {
        Self(Repr::Str(String::new()))
    }

    /// Adds a run of characters.
    pub(super) fn push_str(&mut self, run: &str) {
        match &mut self.0 {
            Repr::Str(text) => text.push_str(run),
            Repr::Units(units) => units.extend(run.encode_utf16()),
        }
    }

    /// Adds one character.
    pub(super) fn push(&mut self, character: char) {
        let mut buffer = [0_u8; 4];

        self.push_str(character.encode_utf8(&mut buffer));
    }

    /// Adds one UTF-16 code unit that is not half of a pair in the input: a
    /// character of the basic plane, or a lone surrogate.
    pub(super) fn push_unit(&mut self, unit: u16) {
        if let Some(character) = char::from_u32(u32::from(unit)) {
            self.push(character);
            return;
        }

        if let Repr::Str(text) = &self.0 {
            self.0 = Repr::Units(text.encode_utf16().collect());
        }

        if let Repr::Units(units) = &mut self.0 {
            units.push(unit);
        }
    }

    pub(super) fn finish(self) -> Text {
        match self.0 {
            Repr::Str(text) => Text(Repr::Str(text)),
            Repr::Units(units) => Text::from_units(units),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HIGH: u16 = 0xd83d;
    const LOW: u16 = 0xde00;

    fn units(text: &str) -> Vec<u16> {
        text.encode_utf16().collect()
    }

    #[test]
    fn a_text_with_no_lone_surrogate_is_a_str() {
        let text = Text::from_units(units("caf\u{e9} \u{1f600}"));

        assert_eq!(text.as_str(), Some("caf\u{e9} \u{1f600}"));
        assert_eq!(text, Text::from("caf\u{e9} \u{1f600}"));
        assert_eq!(text.to_utf16(), units("caf\u{e9} \u{1f600}"));
        assert!(!text.is_empty());
        assert!(Text::empty().is_empty());
    }

    #[test]
    fn a_text_with_a_lone_surrogate_is_no_str() {
        let text = Text::from_units(vec![0x61, HIGH, 0x62]);

        assert_eq!(text.as_str(), None);
        assert_eq!(text.to_string_lossy(), "a\u{fffd}b");
        assert_eq!(text.to_utf16(), vec![0x61, HIGH, 0x62]);
        assert_ne!(text, "a\u{fffd}b");
        assert_eq!(format!("{text:?}"), "utf16[97, 55357, 98]");
    }

    #[test]
    fn a_cut_counts_bytes_and_ends_between_two_characters() {
        let text = Text::from("a\u{e9}\u{20ac}\u{1f600}b");
        let cuts = [
            (0, ""),
            (1, "a"),
            (2, "a"),
            (3, "a\u{e9}"),
            (5, "a\u{e9}"),
            (6, "a\u{e9}\u{20ac}"),
            (9, "a\u{e9}\u{20ac}"),
            (10, "a\u{e9}\u{20ac}\u{1f600}"),
            (11, "a\u{e9}\u{20ac}\u{1f600}b"),
            (usize::MAX, "a\u{e9}\u{20ac}\u{1f600}b"),
        ];

        for (max, kept) in cuts {
            assert_eq!(text.clone().cut_bytes(max), kept, "{max}");
        }
    }

    #[test]
    fn a_cut_counts_a_lone_surrogate_as_three_bytes() {
        let text = Text::from_units(vec![0x61, HIGH, HIGH, LOW, 0x62]);
        let cuts = [
            (0, vec![]),
            (1, vec![0x61]),
            (3, vec![0x61]),
            (4, vec![0x61, HIGH]),
            (7, vec![0x61, HIGH]),
            (8, vec![0x61, HIGH, HIGH, LOW]),
            (9, vec![0x61, HIGH, HIGH, LOW, 0x62]),
            (usize::MAX, vec![0x61, HIGH, HIGH, LOW, 0x62]),
        ];

        for (max, kept) in cuts {
            assert_eq!(text.clone().cut_bytes(max).to_utf16(), kept, "{max}");
        }
    }

    #[test]
    fn a_cut_that_drops_each_lone_surrogate_gives_a_str() {
        let text = Text::from_units(vec![0x61, 0xe9, HIGH]);

        assert_eq!(text.clone().cut_bytes(5).as_str(), Some("a\u{e9}"));
        assert_eq!(text.clone().cut_bytes(6).as_str(), None);
        assert_eq!(text.clone().cut_bytes(5), Text::from("a\u{e9}"));
    }

    #[test]
    fn the_part_before_a_separator() {
        assert_eq!(Text::from("1.0").before('.'), "1");
        assert_eq!(Text::from("10").before('.'), "10");
        assert_eq!(Text::from(".7").before('.'), "");
        assert_eq!(Text::from("1.2.3").before('.'), "1");
        assert_eq!(
            Text::from_units(vec![0x31, 0x2e, HIGH, 0x2e, 0x32]).before('.'),
            "1"
        );
        assert_eq!(Text::from_units(vec![0x31, 0x2e, HIGH]).before('.'), "1");
        assert_eq!(
            Text::from_units(vec![HIGH, 0x31]).before('.').to_utf16(),
            vec![HIGH, 0x31]
        );
    }

    #[test]
    fn a_builder_joins_runs_and_units() {
        let mut builder = TextBuilder::new();
        builder.push_str("a");
        builder.push('\u{1f600}');
        builder.push_unit(0xe9);
        let plain = builder.finish();

        let mut builder = TextBuilder::new();
        builder.push_str("a");
        builder.push_unit(LOW);
        builder.push_str("b\u{1f600}");
        builder.push_unit(HIGH);
        let lone = builder.finish();

        assert_eq!(plain, "a\u{1f600}\u{e9}");
        assert_eq!(lone.to_utf16(), vec![0x61, LOW, 0x62, HIGH, LOW, HIGH]);
    }
}
