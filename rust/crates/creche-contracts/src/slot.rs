//! The lenient field type of a raw type.
//!
//! A raw type holds a document before a check (`rust/AGENTS.md`, rule 1). A
//! key of a document can hold a value of a kind that its field does not
//! take. A [`Slot`] keeps that fact, and the conversion to the valid type
//! then reports the field.
//!
//! The module names no format.

/// The kind of a value, as a reader gives it.
///
/// The set is closed: the reader of a text format gives no other kind. It
/// does not cross a process boundary.
// `pub` in a private module: a public module exports the enum, and its error
// type can then name the kind of a value. The `json` module is the first one
// (packet `decisions-json-check`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Found {
    /// No value: `null`.
    Null,
    /// `true` or `false`.
    Boolean,
    /// A number that the reader gives as an integer.
    Integer,
    /// A number that the reader gives as a float.
    Float,
    /// A text.
    Text,
    /// A list of values.
    List,
    /// A table: keys, each with a value.
    Table,
}

/// What a document holds at one key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Slot<T> {
    /// The document has no such key.
    Missing,
    /// The value is `null`.
    Null,
    /// The value has a kind that the field does not take.
    Other(Found),
    /// The value has the kind of the field.
    Value(T),
}

impl<T> Slot<T> {
    /// The value, when the document holds one of the kind of the field.
    pub(crate) fn value(&self) -> Option<&T> {
        match self {
            Self::Value(value) => Some(value),
            Self::Missing | Self::Null | Self::Other(_) => None,
        }
    }
}

impl Slot<String> {
    /// The text, or the empty text.
    pub(crate) fn text(&self) -> &str {
        self.value().map_or("", String::as_str)
    }
}

impl Slot<bool> {
    /// Whether the document holds `true` here. Each Python reader takes no
    /// other value as true.
    pub(crate) fn is_true(&self) -> bool {
        self.value() == Some(&true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_slot_with_no_value_is_the_empty_text_and_not_true() {
        assert_eq!(Slot::Value("thin".to_owned()).text(), "thin");
        assert_eq!(Slot::<String>::Null.text(), "");
        assert_eq!(Slot::<String>::Missing.text(), "");
        assert_eq!(Slot::<String>::Other(Found::Integer).text(), "");

        assert!(Slot::Value(true).is_true());
        assert!(!Slot::Value(false).is_true());
        assert!(!Slot::<bool>::Null.is_true());
        assert!(!Slot::<bool>::Missing.is_true());
        assert!(!Slot::<bool>::Other(Found::Text).is_true());
    }
}
