//! The strict JSON check of the Rust readers (decision CH65 rule 2).
//!
//! The vmm reads each host API body through `strict`. Each Rust reader of
//! a vsock JSON link, the vmm and guest init, reads each line through it
//! too (`wire/AGENTS.md`, "The vsock JSON links", rule 3). So the two ends
//! of a link cannot drift. A caller then reads the checked bytes as its
//! wire type.

use std::collections::HashSet;
use std::fmt;

use serde::de::{DeserializeSeed, Deserializer, Error as _, MapAccess, SeqAccess, Visitor};

/// The most arrays and objects that are open at one point of a strict
/// text: `DEPTH_MAX` of `wire/src/creche_wire/strictjson.py`.
const DEPTH_MAX: usize = 64;

/// Whether `bytes` is one strict JSON text: the rules of
/// `wire/src/creche_wire/strictjson.py` that serde_json can see. serde_json
/// checks the UTF-8, the grammar, the escapes, the constants, the floats
/// and the trailing data. `Strict` checks the keys and the depth.
// Decision CH40 rule 7: serde_json reads an integer past 64 bits, and the
// integer `-0`, as a float, so this check passes both. strictjson.py
// refuses them. The type check of an integer field then refuses either
// one, as `not_valid` on the host API.
#[must_use]
pub fn strict(bytes: &[u8]) -> bool {
    let mut reader = serde_json::Deserializer::from_slice(bytes);
    Strict { open: 0 }.deserialize(&mut reader).is_ok() && reader.end().is_ok()
}

/// Reads one value and refuses a key given twice in one object, or more
/// than `DEPTH_MAX` arrays and objects open at one point.
#[derive(Debug, Clone, Copy)]
struct Strict {
    /// The arrays and objects open around this value.
    open: usize,
}

impl Strict {
    /// The reader of a value inside one more array or object.
    fn inside<E: serde::de::Error>(self) -> Result<Self, E> {
        let open = self.open.saturating_add(1);
        if open > DEPTH_MAX {
            return Err(E::custom("too deep"));
        }
        Ok(Self { open })
    }
}

impl<'de> DeserializeSeed<'de> for Strict {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Strict {
    type Value = ();

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a strict JSON value")
    }

    fn visit_unit<E>(self) -> Result<(), E> {
        Ok(())
    }

    fn visit_bool<E>(self, _: bool) -> Result<(), E> {
        Ok(())
    }

    fn visit_i64<E>(self, _: i64) -> Result<(), E> {
        Ok(())
    }

    fn visit_u64<E>(self, _: u64) -> Result<(), E> {
        Ok(())
    }

    fn visit_f64<E>(self, _: f64) -> Result<(), E> {
        Ok(())
    }

    fn visit_str<E>(self, _: &str) -> Result<(), E> {
        Ok(())
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut items: A) -> Result<(), A::Error> {
        let inside = self.inside()?;
        while items.next_element_seed(inside)?.is_some() {}
        Ok(())
    }

    fn visit_map<A: MapAccess<'de>>(self, mut entries: A) -> Result<(), A::Error> {
        let inside = self.inside()?;
        // serde_json decodes each escape of a key, so two keys are equal
        // when their code points are equal.
        let mut keys = HashSet::new();
        while let Some(key) = entries.next_key::<String>()? {
            if !keys.insert(key) {
                return Err(A::Error::custom("a key given twice"));
            }
            entries.next_value_seed(inside)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{DEPTH_MAX, strict};

    fn nested(open: usize) -> Vec<u8> {
        let mut text = "[".repeat(open);
        text.push_str(&"]".repeat(open));
        text.into_bytes()
    }

    fn nested_objects(open: usize) -> Vec<u8> {
        let mut text = r#"{"a":"#.repeat(open);
        text.push('1');
        text.push_str(&"}".repeat(open));
        text.into_bytes()
    }

    #[test]
    fn only_a_strict_text_passes() {
        for text in [
            &br#"{"a":1,"b":[true,null,-2,1.5,"\ud83d\ude00"]}"#[..],
            b" \t\r\n{} ",
            b"\"x\"",
            b"0",
        ] {
            assert!(strict(text), "{:?} failed", String::from_utf8_lossy(text));
        }
        assert!(strict(&nested(DEPTH_MAX)));
        assert!(strict(&nested_objects(DEPTH_MAX)));
        for text in [
            &b""[..],
            br#"{"a":1,"a":2}"#,
            br#"{"a":{"b":1,"b":1}}"#,
            br#"["\ud800"]"#,
            b"\"\x01\"",
            b"\"\xff\"",
            b"\xef\xbb\xbf{}",
            b"[NaN]",
            b"[1e999]",
            b"[01]",
            b"{} {}",
            b"{",
        ] {
            assert!(!strict(text), "{:?} passed", String::from_utf8_lossy(text));
        }
        assert!(!strict(&nested(DEPTH_MAX + 1)));
        assert!(!strict(&nested_objects(DEPTH_MAX + 1)));
    }
}
