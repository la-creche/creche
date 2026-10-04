//! A secret: a token or a key.
//!
//! [`Secret`] holds the bytes of one secret. The type keeps the bytes away from
//! a log line, a page and a wire. Its `Debug` prints no byte. It has no
//! `Display`, no `Serialize` and no `PartialEq`. `rust/AGENTS.md`, rule 6, holds
//! the reason.

use std::error::Error;
use std::fmt;
use std::hint::black_box;

/// What `Debug` prints in place of the bytes.
const REDACTED: &str = "Secret(<redacted>)";

/// The bytes of one token or one key.
///
/// A secret is never empty. An empty token file is a fault of the deployment,
/// and an empty secret is equal to an empty offer.
///
/// The type does not change the bytes. A caller that reads a token from a file
/// removes the final newline first.
///
/// [`Secret::expose_secret`] is the only way to the bytes. A review finds each
/// place where a secret leaves the type with one search for that name.
///
/// ```
/// use creche_contracts::secret::Secret;
///
/// let token = Secret::try_from(String::from("correct horse"))?;
/// assert!(token.matches(b"correct horse"));
/// assert!(!token.matches(b"correct horsf"));
/// assert_eq!(token.expose_secret(), b"correct horse");
/// assert_eq!(format!("{token:?}"), "Secret(<redacted>)");
/// # Ok::<(), creche_contracts::secret::SecretError>(())
/// ```
///
/// Code outside this module cannot build a value from raw bytes:
///
/// ```compile_fail,E0423
/// use creche_contracts::secret::Secret;
///
/// let token = Secret(Vec::new());
/// ```
///
/// A secret has no `Display`, so it cannot go to a log line or to a page:
///
/// ```compile_fail,E0277
/// use creche_contracts::secret::Secret;
///
/// let token = Secret::try_from(String::from("correct horse")).unwrap();
/// let line = format!("{token}");
/// ```
///
/// A secret has no `Serialize`, so it cannot go to a wire. The first example
/// compiles, and the second example does not:
///
/// ```
/// use creche_contracts::secret::Secret;
///
/// let token = Secret::try_from(String::from("correct horse")).unwrap();
/// let wire = serde_json::to_string(&format!("{token:?}")).unwrap();
/// assert_eq!(wire, r#""Secret(<redacted>)""#);
/// ```
///
/// ```compile_fail,E0277
/// use creche_contracts::secret::Secret;
///
/// let token = Secret::try_from(String::from("correct horse")).unwrap();
/// let wire = serde_json::to_string(&token).unwrap();
/// ```
///
/// A secret has no `PartialEq`. The operator `==` stops at the first byte that
/// differs, so its time tells where that byte is. Use [`Secret::matches`]:
///
/// ```compile_fail,E0369
/// use creche_contracts::secret::Secret;
///
/// let token = Secret::try_from(String::from("correct horse")).unwrap();
/// let offered = Secret::try_from(String::from("correct horse")).unwrap();
/// let equal = token == offered;
/// ```
///
/// A secret has no `Clone`. A second copy of the bytes is a second place to
/// protect:
///
/// ```compile_fail,E0599
/// use creche_contracts::secret::Secret;
///
/// let token = Secret::try_from(String::from("correct horse")).unwrap();
/// let copy = token.clone();
/// ```
pub struct Secret(Vec<u8>);

impl Secret {
    /// The bytes of the secret.
    ///
    /// Each call is a place where the secret can leave the type. Do not write
    /// the result to a log line, to a page, to an argument list or to a URL.
    #[must_use]
    pub fn expose_secret(&self) -> &[u8] {
        &self.0
    }

    /// Whether `offered` holds the same bytes as the secret.
    ///
    /// The time of the call depends on the count of offered bytes. It does not
    /// depend on the byte at which the two differ: the loop reads each offered
    /// byte and has no branch on a byte. The compiler gives no proof of that.
    /// `black_box` keeps it from a shorter comparison of the result.
    #[must_use]
    pub fn matches(&self, offered: &[u8]) -> bool {
        let mut difference = self.0.len() ^ offered.len();
        // An offer that is longer than the secret meets the secret again from
        // its start. The count of bytes already differs, so the answer is no.
        for (ours, theirs) in self.0.iter().cycle().zip(offered) {
            difference |= usize::from(ours ^ theirs);
        }

        black_box(difference) == 0
    }
}

impl TryFrom<Vec<u8>> for Secret {
    type Error = SecretError;

    fn try_from(bytes: Vec<u8>) -> Result<Self, Self::Error> {
        if bytes.is_empty() {
            return Err(SecretError::Empty);
        }

        Ok(Self(bytes))
    }
}

impl TryFrom<String> for Secret {
    type Error = SecretError;

    fn try_from(text: String) -> Result<Self, Self::Error> {
        Self::try_from(text.into_bytes())
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(REDACTED)
    }
}

/// Why bytes are not a secret.
///
/// No variant holds the bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecretError {
    /// The secret has no byte.
    Empty,
}

impl fmt::Display for SecretError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("a secret has 1 byte or more"),
        }
    }
}

impl Error for SecretError {}

#[cfg(test)]
mod tests {
    use super::*;

    /// A token that no deployment uses.
    const TOKEN: &str = "correct horse battery staple";

    fn secret(text: &str) -> Secret {
        Secret::try_from(text.to_owned()).unwrap()
    }

    #[test]
    fn an_empty_secret_is_refused() {
        assert_eq!(
            Secret::try_from(String::new()).unwrap_err(),
            SecretError::Empty
        );
        assert_eq!(
            Secret::try_from(Vec::new()).unwrap_err(),
            SecretError::Empty
        );
    }

    #[test]
    fn a_secret_keeps_each_byte_as_it_is() {
        let bytes = vec![0x00, 0xff, b'\n', b' '];

        assert_eq!(
            Secret::try_from(bytes.clone()).unwrap().expose_secret(),
            bytes
        );
        assert_eq!(secret("token\n").expose_secret(), b"token\n");
    }

    #[test]
    fn debug_prints_no_byte_of_the_secret() {
        #[derive(Debug)]
        struct Config {
            #[expect(dead_code, reason = "the test reads the field through Debug")]
            token: Secret,
        }

        let config = Config {
            token: secret(TOKEN),
        };

        assert_eq!(format!("{:?}", secret(TOKEN)), REDACTED);
        assert_eq!(format!("{:#?}", secret(TOKEN)), REDACTED);
        assert_eq!(
            format!("{config:?}"),
            "Config { token: Secret(<redacted>) }"
        );
        for word in TOKEN.split(' ') {
            assert!(!format!("{config:#?}").contains(word));
        }
    }

    #[test]
    fn the_same_bytes_match() {
        assert!(secret(TOKEN).matches(TOKEN.as_bytes()));
        assert!(secret("a").matches(b"a"));
        assert!(
            Secret::try_from(vec![0x00, 0xff])
                .unwrap()
                .matches(&[0x00, 0xff])
        );
    }

    #[test]
    fn other_bytes_do_not_match() {
        let token = secret("abc");
        for offered in ["", "a", "ab", "abd", "xbc", "abcd", "abca", "ABC", "abc\n"] {
            assert!(!token.matches(offered.as_bytes()), "{offered:?}");
        }
    }

    #[test]
    fn an_offer_that_repeats_the_secret_does_not_match() {
        assert!(!secret("ab").matches(b"abab"));
        assert!(!secret("a").matches(b"aaaa"));
        assert!(!Secret::try_from(vec![0x00]).unwrap().matches(&[0x00, 0x00]));
    }

    #[test]
    fn the_error_says_which_rule_failed() {
        let error: Box<dyn Error + Send + Sync> = Box::new(SecretError::Empty);

        assert_eq!(error.to_string(), "a secret has 1 byte or more");
        assert!(error.source().is_none());
    }
}
