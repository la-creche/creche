//! The placeholders of one session (openshell ideas plan, idea 1, PR P2).
//!
//! The family-state reply on host port 1100 sends these two values in
//! place of the family LiteLLM key and the `pep_token`. The plane proxies
//! of ports 1101 and 1102 swap each one for the real value of
//! `creds.json`, per request. So the guest memory, and each snapshot of
//! it, holds no real credential.
//!
//! 1. `Placeholders::fresh` makes both values from `/dev/urandom`, in the
//!    formats of the real values: `sk-` and 22 base64url characters (16
//!    bytes) for LiteLLM, 64 lowercase hex digits (32 bytes) for the
//!    chaperone. So each value passes the check of its wire type, and a
//!    guest program sees the length that it saw before.
//! 2. The row file of the session holds them (`rows`). So a restore, also
//!    after a vmm restart, finds the values that the guest memory holds.
//! 3. A file value that breaks either format refuses the row file, so the
//!    vmm does not start.

use std::fs::File;
use std::io::{self, Read};

use creche_wire::{FamilyCreds, LitellmKey, PepToken};
use serde::{Deserialize, Serialize};

/// The source of the random bytes.
const RANDOM: &str = "/dev/urandom";

/// The prefix of a LiteLLM key.
const LITELLM_PREFIX: &str = "sk-";

/// The random bytes of the LiteLLM placeholder: 16, which base64url
/// writes as 22 characters, the length of a real family key.
const LITELLM_BYTES: usize = 16;

/// The characters after `LITELLM_PREFIX`.
const LITELLM_CHARS: usize = 22;

/// The random bytes of the `pep_token` placeholder: 32, as 64 hex digits.
const PEP_BYTES: usize = 32;

/// The base64url alphabet (RFC 4648 section 5).
const BASE64URL: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// The lowercase hex digits.
const HEX: &[u8; 16] = b"0123456789abcdef";

/// The two placeholders of one session. Its `Debug` writes no value,
/// because each field is a wire secret type.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "Raw")]
pub struct Placeholders {
    litellm_key: LitellmKey,
    pep_token: PepToken,
}

/// The file form of `Placeholders`, before the format check.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Raw {
    litellm_key: LitellmKey,
    pep_token: PepToken,
}

/// Why a file value is no placeholder.
#[derive(Debug)]
pub struct NotPlaceholder;

impl std::fmt::Display for NotPlaceholder {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a LiteLLM placeholder is sk- and 22 base64url characters")
    }
}

impl TryFrom<Raw> for Placeholders {
    type Error = NotPlaceholder;

    fn try_from(raw: Raw) -> Result<Self, Self::Error> {
        let rest = raw.litellm_key.reveal().strip_prefix(LITELLM_PREFIX);
        let shaped = rest.is_some_and(|rest| {
            rest.len() == LITELLM_CHARS && rest.bytes().all(|byte| BASE64URL.contains(&byte))
        });
        if !shaped {
            return Err(NotPlaceholder);
        }
        Ok(Self {
            litellm_key: raw.litellm_key,
            pep_token: raw.pep_token,
        })
    }
}

impl Placeholders {
    /// Two new values from `/dev/urandom`.
    ///
    /// # Errors
    ///
    /// The error of the open or the read of `/dev/urandom`.
    pub fn fresh() -> io::Result<Self> {
        let mut bytes = [0; LITELLM_BYTES + PEP_BYTES];
        File::open(RANDOM)?.read_exact(&mut bytes)?;
        let (litellm, pep) = bytes.split_at(LITELLM_BYTES);
        let litellm = format!("{LITELLM_PREFIX}{}", base64url(litellm));
        let shaped = |_| io::Error::other("a fresh placeholder breaks its format");
        Ok(Self {
            litellm_key: LitellmKey::try_from(litellm).map_err(shaped)?,
            pep_token: PepToken::try_from(hex(pep)).map_err(shaped)?,
        })
    }

    /// The LiteLLM placeholder: the value of port 1101.
    #[must_use]
    pub fn litellm_key(&self) -> &LitellmKey {
        &self.litellm_key
    }

    /// The `pep_token` placeholder: the value of port 1102.
    #[must_use]
    pub fn pep_token(&self) -> &PepToken {
        &self.pep_token
    }

    /// The creds part of the family-state reply: both placeholders.
    #[must_use]
    pub fn creds(&self) -> FamilyCreds {
        FamilyCreds {
            litellm_key: self.litellm_key.clone(),
            pep_token: self.pep_token.clone(),
        }
    }
}

/// `bytes` as base64url with no padding.
fn base64url(bytes: &[u8]) -> String {
    let mut text = String::new();
    for chunk in bytes.chunks(3) {
        let mut group = [0_u8; 3];
        for (slot, byte) in group.iter_mut().zip(chunk) {
            *slot = *byte;
        }
        let [first, second, third] = group.map(u32::from);
        let bits = first << 16 | second << 8 | third;
        for shift in [18, 12, 6, 0].into_iter().take(chunk.len() + 1) {
            let six = usize::try_from((bits >> shift) & 0x3f).unwrap_or(0);
            if let Some(&character) = BASE64URL.get(six) {
                text.push(char::from(character));
            }
        }
    }
    text
}

/// `bytes` as lowercase hex.
fn hex(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        for nibble in [byte >> 4, byte & 0x0f] {
            if let Some(&digit) = HEX.get(usize::from(nibble)) {
                text.push(char::from(digit));
            }
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use super::{Placeholders, base64url, hex};

    /// The encoders give the RFC 4648 test vectors.
    #[test]
    fn the_encoders_give_the_known_texts() {
        assert_eq!(base64url(b"f"), "Zg");
        assert_eq!(base64url(b"fo"), "Zm8");
        assert_eq!(base64url(b"foo"), "Zm9v");
        assert_eq!(base64url(&[0xfb, 0xff]), "-_8");
        assert_eq!(hex(&[0x00, 0x9f, 0xff]), "009fff");
    }

    /// A fresh LiteLLM placeholder is `sk-` and 22 characters, and a fresh
    /// `pep_token` placeholder is 64 hex digits. Two calls differ.
    #[test]
    fn fresh_values_have_the_real_formats() {
        let first = Placeholders::fresh().unwrap();
        let key = first.litellm_key().reveal();
        assert_eq!(key.len(), 25);
        assert!(key.starts_with("sk-"));
        assert_eq!(first.pep_token().reveal().len(), 64);
        let second = Placeholders::fresh().unwrap();
        assert_ne!(first.litellm_key(), second.litellm_key());
        assert_ne!(first.pep_token(), second.pep_token());
    }

    /// The file form reads back, and a key of another format refuses.
    #[test]
    fn the_file_form_reads_back_and_checks_the_format() {
        let fresh = Placeholders::fresh().unwrap();
        let text = serde_json::to_string(&fresh).unwrap();
        assert_eq!(serde_json::from_str::<Placeholders>(&text).unwrap(), fresh);
        let token = "ab".repeat(32);
        for key in [
            "sk-short",
            "sk-AAAAAAAAAAAAAAAAAAAAA+",
            "xx-AAAAAAAAAAAAAAAAAAAAAA",
        ] {
            let text = format!(r#"{{"litellm_key":"{key}","pep_token":"{token}"}}"#);
            assert!(
                serde_json::from_str::<Placeholders>(&text).is_err(),
                "{key}"
            );
        }
        let text =
            format!(r#"{{"litellm_key":"sk-AAAAAAAAAAAAAAAAAAAAAA","pep_token":"{token}"}}"#);
        assert!(serde_json::from_str::<Placeholders>(&text).is_ok());
    }

    /// The `Debug` text holds no value.
    #[test]
    fn debug_writes_no_value() {
        let fresh = Placeholders::fresh().unwrap();
        let text = format!("{fresh:?}");
        assert!(!text.contains(fresh.litellm_key().reveal()));
        assert!(!text.contains(fresh.pep_token().reveal()));
    }
}
