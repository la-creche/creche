//! Bytes as hex text.
//!
//! [`lower`] writes the base 16 form of RFC 4648 section 8, with the letters
//! `a` to `f` in lower case. `bytes.hex()` of Python gives the same text, and
//! so does `hexdigest()` of `hashlib` for a digest.

/// The bytes as lower-case hex text: two characters for each byte, the high
/// four bits first.
///
/// ```
/// use creche_util::hex;
///
/// assert_eq!(hex::lower(&[0x00, 0x9f, 0xff]), "009fff");
/// assert_eq!(hex::lower(b""), "");
/// ```
#[must_use]
pub fn lower(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::lower;

    /// The 16 characters of the text, in the order of their values.
    const ALPHABET: &str = "0123456789abcdef";

    /// The examples of RFC 4648 section 10, with each letter in lower case.
    #[test]
    fn each_rfc_example_has_the_rfc_text_in_lower_case() {
        let table = [
            ("", ""),
            ("f", "66"),
            ("fo", "666f"),
            ("foo", "666f6f"),
            ("foob", "666f6f62"),
            ("fooba", "666f6f6261"),
            ("foobar", "666f6f626172"),
        ];
        for (bytes, text) in table {
            assert_eq!(lower(bytes.as_bytes()), text, "{bytes:?}");
        }
    }

    #[test]
    fn each_byte_is_two_characters_of_the_alphabet() {
        for byte in u8::MIN..=u8::MAX {
            let text = lower(&[byte]);
            let high = ALPHABET.chars().nth(usize::from(byte >> 4)).unwrap();
            let low = ALPHABET.chars().nth(usize::from(byte & 0x0f)).unwrap();

            assert_eq!(text, format!("{high}{low}"), "{byte:#04x}");
        }
    }

    #[test]
    fn the_text_keeps_the_order_and_each_zero_of_the_bytes() {
        assert_eq!(lower(&[0x00, 0x01, 0x0a, 0x10, 0xff, 0x00]), "00010a10ff00");
    }
}
