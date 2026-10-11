//! Base64 text of RFC 4648: one decoder and one encoder.
//!
//! [`decode`] reads the form of section 4 of the RFC: the standard alphabet,
//! with the pad `=`. `base64.b64encode` of Python writes that form.
//!
//! [`encode_url`] writes the form of section 5: the URL-safe alphabet, with no
//! pad. `base64.urlsafe_b64encode` of Python gives the same text after a cut
//! of each `=` at its end, and so does `secrets.token_urlsafe` for random
//! bytes.
//!
//! The two functions are not the two directions of one form. `decode`
//! refuses a text of `encode_url` that holds `-` or `_`, and one whose last
//! group has less than 4 characters.

use std::error::Error;
use std::fmt;

/// The count of bytes that one full group holds.
const GROUP_BYTES: usize = 3;

/// The count of characters of one full group.
const GROUP_CHARS: usize = 4;

/// The count of bits that one character holds.
const CHAR_BITS: u32 = 6;

/// The character that fills the last group of a text with padding.
const PAD: u8 = b'=';

/// The character of the value 0 in each of the two alphabets. The decoder
/// reads a pad as this character, so a pad adds no bit.
const ZERO: u8 = b'A';

/// The URL-safe alphabet of RFC 4648 section 5: the character of each value
/// of 6 bits, from 0 to 63. It differs from the standard alphabet in the last
/// two places.
///
/// ```
/// use creche_util::base64;
///
/// assert_eq!(base64::URL_SAFE.len(), 64);
/// assert!(base64::URL_SAFE.ends_with(b"89-_"));
/// assert!(!base64::URL_SAFE.contains(&b'='));
/// ```
pub const URL_SAFE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// The shift of each character in the 24 bits of one group, first character
/// first.
const CHAR_SHIFTS: [u32; GROUP_CHARS] = [3 * CHAR_BITS, 2 * CHAR_BITS, CHAR_BITS, 0];

/// The bits of one character.
const CHAR_MASK: u32 = (1 << CHAR_BITS) - 1;

/// Why [`decode`] refused a text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecodeError {
    /// The count of characters is not a multiple of 4. A text with padding
    /// holds full groups only.
    Length,
    /// A place holds a byte that is no character of the standard alphabet.
    /// The pad `=` is such a byte in each place but the end of the last
    /// group.
    Character,
    /// The last group ends with a pad, and a pad bit of that group is not
    /// zero. A pad bit is a bit of the last character before the pad that
    /// belongs to no byte.
    PadBits,
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Length => "the count of characters is not a multiple of 4",
            Self::Character => "a character is not in the base64 alphabet",
            Self::PadBits => "a pad bit of the last group is not zero",
        })
    }
}

impl Error for DecodeError {}

/// The bytes of a base64 text in the standard alphabet, with padding
/// (RFC 4648 section 4).
///
/// A text has full groups of 4 characters only. The last group can end with
/// one pad or with two. The function takes no line break and no other byte
/// outside the alphabet.
///
/// Each run of bytes has one text only. The function refuses a last group
/// with a pad bit that is not zero, as section 3.5 of the RFC permits. For
/// example, `Zg==` is the text of the byte `f`, and `Zh==` is no text.
///
/// The function reads each group from the start of the text and stops at the
/// first fault. It then makes sure that no character is left after the last
/// full group.
///
/// ```
/// use creche_util::base64::{self, DecodeError};
///
/// assert_eq!(base64::decode("Zm9vYg=="), Ok(b"foob".to_vec()));
/// assert_eq!(base64::decode(""), Ok(Vec::new()));
/// assert_eq!(base64::decode("Zm9vYg"), Err(DecodeError::Length));
/// assert_eq!(base64::decode("Zm9_"), Err(DecodeError::Character));
/// assert_eq!(base64::decode("Zh=="), Err(DecodeError::PadBits));
/// ```
///
/// # Errors
///
/// [`DecodeError`] names the first fault of a text that is no such base64.
pub fn decode(text: &str) -> Result<Vec<u8>, DecodeError> {
    let mut groups = text.as_bytes().chunks_exact(GROUP_CHARS);
    let mut decoded = Vec::new();

    while let Some(group) = groups.next() {
        let [first, second, third, fourth] = *group else {
            return Err(DecodeError::Length);
        };
        let last = groups.len() == 0;
        let (characters, kept): ([u8; GROUP_CHARS], usize) = match (third, fourth) {
            (PAD, PAD) if last => ([first, second, ZERO, ZERO], 1),
            (_, PAD) if last => ([first, second, third, ZERO], 2),
            _ => ([first, second, third, fourth], GROUP_BYTES),
        };

        let mut word = 0_u32;
        for character in characters {
            word = (word << CHAR_BITS) | value_of(character).ok_or(DecodeError::Character)?;
        }
        let [_, bytes @ ..] = word.to_be_bytes();
        // CONTRACT-QUESTION: RFC 4648 section 3.5 says that a decoder can
        // refuse a text with a pad bit that is not zero. It does not demand
        // that refusal. The reading here refuses such a text, so each run of
        // bytes has one text. The owner did not confirm this reading yet
        // (`AGENTS.md` of this crate, "Known gaps"). A change costs this
        // condition, the variant `DecodeError::PadBits` and the rows of that
        // variant in the tests.
        //
        // A byte that the group does not keep holds only pad bits.
        if bytes.iter().skip(kept).any(|byte| *byte != 0) {
            return Err(DecodeError::PadBits);
        }
        decoded.extend(bytes.into_iter().take(kept));
    }

    if !groups.remainder().is_empty() {
        return Err(DecodeError::Length);
    }

    Ok(decoded)
}

/// The value of one character of the standard alphabet. `None` for each
/// other byte, and for the pad.
fn value_of(character: u8) -> Option<u32> {
    let value = match character {
        b'A'..=b'Z' => character - b'A',
        b'a'..=b'z' => character - b'a' + 26,
        b'0'..=b'9' => character - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => return None,
    };

    Some(u32::from(value))
}

/// Why [`encode_url`] gave no text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodeError {
    /// The process cannot hold the text in memory.
    NoMemory,
    /// A value of 6 bits has no character in [`URL_SAFE`]. No input gives
    /// this fault, because the alphabet has a character for each such value.
    /// The lookup can fail in its type only, and the function gives that
    /// failure a value and does not panic.
    NoCharacter,
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NoMemory => "the process cannot hold the base64 text in memory",
            Self::NoCharacter => "a value of 6 bits has no character in the alphabet",
        })
    }
}

impl Error for EncodeError {}

/// The bytes as base64 text in the URL-safe alphabet, with no padding
/// (RFC 4648 section 5).
///
/// A group of 3 bytes gives 4 characters. A last group of 1 byte gives 2
/// characters, and a last group of 2 bytes gives 3. No bytes give the empty
/// text.
///
/// The function asks for the memory of the text before it writes the first
/// character. A caller thus gets an error, and no panic, for bytes whose text
/// the process cannot hold.
///
/// ```
/// use creche_util::base64;
///
/// assert_eq!(base64::encode_url(b"foob").as_deref(), Ok("Zm9vYg"));
/// assert_eq!(base64::encode_url(&[0xfb, 0xff, 0xbf]).as_deref(), Ok("-_-_"));
/// assert_eq!(base64::encode_url(b"").as_deref(), Ok(""));
/// ```
///
/// # Errors
///
/// [`EncodeError::NoMemory`] when the process cannot hold the text. No input
/// gives [`EncodeError::NoCharacter`].
pub fn encode_url(bytes: &[u8]) -> Result<String, EncodeError> {
    let mut text = with_room_for(bytes.len())?;

    for group in bytes.chunks(GROUP_BYTES) {
        // The bytes of the group are the first bytes of 24 bits. A short
        // group has zero bits after its last byte.
        let bits = (0..GROUP_BYTES).fold(0_u32, |bits, place| {
            (bits << u8::BITS) | group.get(place).map_or(0, |byte| u32::from(*byte))
        });

        for shift in CHAR_SHIFTS.iter().take(group.len() + 1) {
            text.push(character_of((bits >> shift) & CHAR_MASK)?);
        }
    }

    Ok(text)
}

/// An empty text with the memory for the characters of `count` bytes.
fn with_room_for(count: usize) -> Result<String, EncodeError> {
    let chars = count
        .div_ceil(GROUP_BYTES)
        .checked_mul(GROUP_CHARS)
        .ok_or(EncodeError::NoMemory)?;
    let mut text = String::new();
    text.try_reserve_exact(chars)
        .map_err(|_| EncodeError::NoMemory)?;

    Ok(text)
}

/// The character of one value of 6 bits in [`URL_SAFE`].
fn character_of(value: u32) -> Result<char, EncodeError> {
    usize::try_from(value)
        .ok()
        .and_then(|place| URL_SAFE.get(place))
        .map(|byte| char::from(*byte))
        .ok_or(EncodeError::NoCharacter)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The standard alphabet of RFC 4648 section 4, in the order of its
    /// values.
    const STANDARD: &str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    /// The 48 bytes that hold each value of 6 bits one time, from 0 to 63.
    const EACH_VALUE: [u8; 48] = [
        0x00, 0x10, 0x83, 0x10, 0x51, 0x87, 0x20, 0x92, 0x8b, 0x30, 0xd3, 0x8f, 0x41, 0x14, 0x93,
        0x51, 0x55, 0x97, 0x61, 0x96, 0x9b, 0x71, 0xd7, 0x9f, 0x82, 0x18, 0xa3, 0x92, 0x59, 0xa7,
        0xa2, 0x9a, 0xab, 0xb2, 0xdb, 0xaf, 0xc3, 0x1c, 0xb3, 0xd3, 0x5d, 0xb7, 0xe3, 0x9e, 0xbb,
        0xf3, 0xdf, 0xbf,
    ];

    /// The examples of RFC 4648 section 10: the bytes, their text in the
    /// form of section 4 and their text in the form of [`encode_url`]. The
    /// rows hold each length of a last group.
    const RFC_EXAMPLES: [(&str, &str, &str); 7] = [
        ("", "", ""),
        ("f", "Zg==", "Zg"),
        ("fo", "Zm8=", "Zm8"),
        ("foo", "Zm9v", "Zm9v"),
        ("foob", "Zm9vYg==", "Zm9vYg"),
        ("fooba", "Zm9vYmE=", "Zm9vYmE"),
        ("foobar", "Zm9vYmFy", "Zm9vYmFy"),
    ];

    // --- the decoder ---

    #[test]
    fn each_rfc_example_decodes_to_its_bytes() {
        for (bytes, text, _) in RFC_EXAMPLES {
            assert_eq!(decode(text), Ok(bytes.as_bytes().to_vec()), "{text:?}");
        }
    }

    #[test]
    fn a_text_decodes_to_bytes_that_are_no_text() {
        assert_eq!(decode("/w=="), Ok(vec![0xff]));
        assert_eq!(decode("+/8="), Ok(vec![0xfb, 0xff]));
        assert_eq!(decode("AAAA////"), Ok(vec![0, 0, 0, 0xff, 0xff, 0xff]));
    }

    #[test]
    fn each_character_of_the_standard_alphabet_has_its_value() {
        assert_eq!(decode(STANDARD), Ok(EACH_VALUE.to_vec()));

        for (value, character) in (0_u32..).zip(STANDARD.bytes()) {
            assert_eq!(value_of(character), Some(value), "{character:#04x}");
        }
    }

    #[test]
    fn a_byte_outside_the_standard_alphabet_has_no_value() {
        let outside = (u8::MIN..=u8::MAX).filter(|byte| !STANDARD.as_bytes().contains(byte));

        assert_eq!(outside.clone().count(), 192);
        for byte in outside {
            assert_eq!(value_of(byte), None, "{byte:#04x}");
        }
    }

    #[test]
    fn a_count_of_characters_that_is_no_multiple_of_4_is_refused() {
        for text in [
            "Z", "Zg", "Zg=", "Zm8", "Zm9vYg", "Zm9vYmE", "Zm9v\n", "Zm9vZ",
        ] {
            assert_eq!(decode(text), Err(DecodeError::Length), "{text:?}");
        }
    }

    #[test]
    fn a_pad_in_a_wrong_place_is_refused() {
        // A pad in a group that is not the last one.
        let early = ["Zg==Zg==", "Zm8=Zm9v"];
        // More pads than a last group can hold.
        let many = ["Z===", "===="];
        // A pad before a character of the same group.
        let inside = ["=g==", "Zm=v", "Z=9v", "=m9v"];

        for text in early.into_iter().chain(many).chain(inside) {
            assert_eq!(decode(text), Err(DecodeError::Character), "{text:?}");
        }
    }

    #[test]
    fn a_character_outside_the_standard_alphabet_is_refused() {
        for text in [
            "Zm 9",
            "Zm9v\nZm9v",
            "Zm9-",
            "Zm9_",
            "Zm9\u{e9}",
            "Zm9v\u{e9}\u{e9}",
            "Zm9\0",
        ] {
            assert_eq!(decode(text), Err(DecodeError::Character), "{text:?}");
        }
    }

    /// RFC 4648 section 3.5 names the pad bits. A last group of one byte has 4
    /// of them in its second character. A last group of two bytes has 2 of
    /// them in its third character. The rows hold each pad bit alone.
    #[test]
    fn a_pad_bit_that_is_not_zero_is_refused() {
        let one_byte = ["Zh==", "Zi==", "Zk==", "Zo==", "//=="];
        let two_bytes = ["Zm9=", "Zm+=", "+/9="];

        for text in one_byte.into_iter().chain(two_bytes) {
            assert_eq!(decode(text), Err(DecodeError::PadBits), "{text:?}");
        }

        // The same groups with each pad bit at zero.
        assert_eq!(decode("Zg=="), Ok(b"f".to_vec()));
        assert_eq!(decode("Zw=="), Ok(b"g".to_vec()));
        assert_eq!(decode("Zm8="), Ok(b"fo".to_vec()));
    }

    #[test]
    fn the_decoder_gives_the_first_fault_of_a_text() {
        // A fault in a full group comes before the count of the characters.
        assert_eq!(decode("Zm 9Z"), Err(DecodeError::Character));
        assert_eq!(decode("Zh==Z"), Err(DecodeError::PadBits));
        assert_eq!(decode("Zg==Z"), Err(DecodeError::Length));
        // The character comes before the pad bits of the same group.
        assert_eq!(decode("Z-=="), Err(DecodeError::Character));
    }

    #[test]
    fn a_decode_error_says_what_the_fault_is() {
        assert_eq!(
            DecodeError::Length.to_string(),
            "the count of characters is not a multiple of 4"
        );
        assert_eq!(
            DecodeError::Character.to_string(),
            "a character is not in the base64 alphabet"
        );
        assert_eq!(
            DecodeError::PadBits.to_string(),
            "a pad bit of the last group is not zero"
        );
    }

    // --- the encoder ---

    /// Bytes and the text that Python gives for them:
    /// `base64.urlsafe_b64encode` with each `=` cut from the end.
    const PYTHON_TEXTS: [(&[u8], &str); 8] = [
        (&[0xff; 32], "__________________________________________8"),
        (
            &[
                200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215,
                216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229, 230, 231,
            ],
            "yMnKy8zNzs_Q0dLT1NXW19jZ2tvc3d7f4OHi4-Tl5uc",
        ),
        (&[0xfb, 0xff, 0xbf], "-_-_"),
        (&[0x00], "AA"),
        (&[0xff], "_w"),
        (&[0xff, 0xff], "__8"),
        (&[0, 1, 2, 3], "AAECAw"),
        (&[0, 1, 2, 3, 4], "AAECAwQ"),
    ];

    /// The bytes 0, 1, 2 and so on, as `bytes(range(count))` of Python.
    fn counted(count: u8) -> Vec<u8> {
        (0..count).collect()
    }

    #[test]
    fn each_rfc_example_encodes_to_its_text_with_no_padding() {
        for (bytes, _, text) in RFC_EXAMPLES {
            assert_eq!(
                encode_url(bytes.as_bytes()).as_deref(),
                Ok(text),
                "{bytes:?}"
            );
        }
    }

    #[test]
    fn each_python_row_encodes_to_the_python_text() {
        for (row, (bytes, text)) in PYTHON_TEXTS.into_iter().enumerate() {
            assert_eq!(encode_url(bytes).as_deref(), Ok(text), "row {row}");
        }

        assert_eq!(
            encode_url(&counted(32)).as_deref(),
            Ok("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
        );
    }

    #[test]
    fn each_value_of_6_bits_has_its_character_of_the_alphabet() {
        let alphabet = String::from_utf8(URL_SAFE.to_vec()).unwrap();

        assert_eq!(encode_url(&EACH_VALUE), Ok(alphabet));

        for (value, byte) in (0_u32..).zip(URL_SAFE) {
            assert_eq!(character_of(value), Ok(char::from(*byte)), "{value}");
        }
    }

    #[test]
    fn the_alphabet_is_the_standard_one_but_for_the_last_two_places() {
        let (same, last_two) = URL_SAFE.split_at(62);

        assert_eq!(same, &STANDARD.as_bytes()[..62]);
        assert_eq!(last_two, b"-_");
        assert_eq!(&STANDARD.as_bytes()[62..], b"+/");
    }

    #[test]
    fn a_text_has_no_padding_for_each_count_of_bytes() {
        for count in 0..=100_u8 {
            let text = encode_url(&counted(count)).unwrap();
            let bytes = usize::from(count);

            // 3 bytes give 4 characters, and a last group of 1 or 2 bytes
            // gives 2 or 3.
            assert_eq!(text.len(), (bytes * 4).div_ceil(3), "{count} bytes");
            assert!(
                text.bytes().all(|byte| URL_SAFE.contains(&byte)),
                "{count} bytes"
            );
        }
    }

    /// A text of [`encode_url`] in the form that [`decode`] reads: `+` for
    /// each `-`, `/` for each `_`, and the pad at the end.
    fn standard_form(text: &str) -> String {
        let mut standard = text.replace('-', "+").replace('_', "/");
        while !standard.len().is_multiple_of(GROUP_CHARS) {
            standard.push(char::from(PAD));
        }

        standard
    }

    #[test]
    fn the_decoder_reads_each_text_of_the_encoder_in_the_standard_form() {
        for count in 0..=100_u8 {
            let low = counted(count);
            let high: Vec<u8> = low.iter().map(|byte| !byte).collect();

            for bytes in [low, high] {
                let text = standard_form(&encode_url(&bytes).unwrap());

                assert_eq!(decode(&text), Ok(bytes), "{count} bytes");
            }
        }
    }

    #[test]
    fn a_text_that_no_process_holds_is_an_error() {
        // The count of the characters does not fit `usize`.
        assert_eq!(with_room_for(usize::MAX), Err(EncodeError::NoMemory));
        // The count fits `usize`, and no text holds that many bytes.
        assert_eq!(with_room_for(usize::MAX / 2), Err(EncodeError::NoMemory));
        assert_eq!(with_room_for(0), Ok(String::new()));
        assert!(with_room_for(32).unwrap().capacity() >= 44);
    }

    #[test]
    fn a_value_past_6_bits_has_no_character() {
        assert_eq!(character_of(63), Ok('_'));
        assert_eq!(character_of(64), Err(EncodeError::NoCharacter));
        assert_eq!(character_of(u32::MAX), Err(EncodeError::NoCharacter));
    }

    #[test]
    fn an_encode_error_says_what_the_fault_is() {
        assert_eq!(
            EncodeError::NoMemory.to_string(),
            "the process cannot hold the base64 text in memory"
        );
        assert_eq!(
            EncodeError::NoCharacter.to_string(),
            "a value of 6 bits has no character in the alphabet"
        );
    }
}
