//! Base64 of RFC 4648 section 4, with padding. No crate is added: the code
//! is short, and the strict decode is the part that matters.

use std::fmt;

/// The alphabet of RFC 4648 section 4.
const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// The pad character.
const PAD: u8 = b'=';

/// Why a text is no strict base64.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Base64Error {
    /// The length is no multiple of 4.
    Length,
    /// A character is outside the alphabet, or a pad is not at the end.
    Character,
    /// The pad bits of the last group are not zero.
    PadBits,
}

impl fmt::Display for Base64Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Length => "the base64 length is no multiple of 4",
            Self::Character => "a base64 character is wrong",
            Self::PadBits => "the base64 pad bits are not zero",
        })
    }
}

impl std::error::Error for Base64Error {}

/// The character of the low 6 bits of `bits`.
fn symbol(bits: u32) -> char {
    let index = usize::try_from(bits & 0x3f).unwrap_or(0);
    ALPHABET.get(index).map_or('A', |byte| char::from(*byte))
}

/// The base64 text of `bytes`, with padding.
#[must_use]
pub fn encode(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(crate::limits::base64_len(bytes.len()));
    for group in bytes.chunks(3) {
        let mut word = [0_u8; 3];
        for (slot, byte) in word.iter_mut().zip(group) {
            *slot = *byte;
        }
        let [a, b, c] = word;
        let bits = u32::from(a) << 16 | u32::from(b) << 8 | u32::from(c);

        text.push(symbol(bits >> 18));
        text.push(symbol(bits >> 12));
        text.push(if group.len() > 1 {
            symbol(bits >> 6)
        } else {
            '='
        });
        text.push(if group.len() > 2 { symbol(bits) } else { '=' });
    }
    text
}

/// The 6-bit value of one character.
fn value(byte: u8) -> Option<u32> {
    let index = ALPHABET.iter().position(|symbol| *symbol == byte)?;
    u32::try_from(index).ok()
}

/// The bytes of a strict base64 text.
///
/// # Errors
///
/// A length that is no multiple of 4, a character outside the alphabet
/// (whitespace, `-`, `_` included), a pad that is not at the end, and pad
/// bits that are not zero.
pub fn decode(text: &str) -> Result<Vec<u8>, Base64Error> {
    let bytes = text.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return Err(Base64Error::Length);
    }

    let groups = bytes.len() / 4;
    let mut out = Vec::with_capacity(groups * 3);
    for (number, group) in bytes.chunks(4).enumerate() {
        let last = number + 1 == groups;
        let pads = group.iter().rev().take_while(|byte| **byte == PAD).count();
        if pads > 2 || (pads > 0 && !last) {
            return Err(Base64Error::Character);
        }

        let mut bits = 0_u32;
        for byte in group.iter().take(4 - pads) {
            bits = bits << 6 | value(*byte).ok_or(Base64Error::Character)?;
        }
        bits <<= 6 * u32::try_from(pads).map_err(|_| Base64Error::Character)?;

        let [_, a, b, c] = bits.to_be_bytes();
        match pads {
            0 => out.extend([a, b, c]),
            1 if c == 0 => out.extend([a, b]),
            2 if b == 0 && c == 0 => out.push(a),
            _ => return Err(Base64Error::PadBits),
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc_4648_section_10() {
        for (plain, coded) in [
            ("", ""),
            ("f", "Zg=="),
            ("fo", "Zm8="),
            ("foo", "Zm9v"),
            ("foob", "Zm9vYg=="),
            ("fooba", "Zm9vYmE="),
            ("foobar", "Zm9vYmFy"),
        ] {
            assert_eq!(encode(plain.as_bytes()), coded);
            assert_eq!(decode(coded).unwrap(), plain.as_bytes());
        }
    }

    #[test]
    fn every_byte_round_trips() {
        let bytes: Vec<u8> = (0..=255).collect();
        for len in 0..bytes.len() {
            let part = &bytes[..len];
            assert_eq!(decode(&encode(part)).unwrap(), part);
        }
    }

    #[test]
    fn strict_refusals() {
        assert_eq!(decode("Zh=="), Err(Base64Error::PadBits));
        assert_eq!(decode("Zm9="), Err(Base64Error::PadBits));
        assert_eq!(decode("Zg"), Err(Base64Error::Length));
        assert_eq!(decode("-_-_"), Err(Base64Error::Character));
        assert_eq!(decode("Zg==Zg=="), Err(Base64Error::Character));
        assert_eq!(decode("Z==="), Err(Base64Error::Character));
        assert_eq!(decode("Zm 9"), Err(Base64Error::Character));
        assert_eq!(decode("Zm9v\n"), Err(Base64Error::Length));
        assert_eq!(decode("Z=g="), Err(Base64Error::Character));
    }
}
