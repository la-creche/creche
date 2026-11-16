//! The message of an annotated release tag (FR8, section 4.5): three lines,
//! the header `release-statement-1`, the statement in base64 and the
//! bundle in base64.
//!
//! ```text
//! release-statement-1
//! eyJzdGF0ZW1lbnQiOjEs...      <- base64 of the canonical statement
//! eyJtZWRpYVR5cGUiOi...        <- base64 of the cosign bundle
//! ```
//!
//! Two types hold a message. `TagMessage` is what the signer writes: only a
//! `Signable` makes one, so it always holds a statement that
//! `IssuedRuns::issue` built. `UnverifiedTagMessage` is what a reader
//! decodes from a Forgejo reply. Nothing turns it into a `TagMessage`.
//!
//! Decode does not parse the statement. Root parses it only after cosign
//! passes on the bundle (section 5).

use std::fmt;

use crate::base64;
use crate::issue::Signable;
use crate::limits::{
    MAX_BUNDLE_BYTES, MAX_TAG_MESSAGE_BYTES, MESSAGE_HEADER, WORST_STATEMENT_BYTES,
};

/// A cosign bundle: 1 to `MAX_BUNDLE_BYTES` opaque bytes.
// CONTRACT-QUESTION: forgejo-release-path.md section 3 M7 and section 4.5:
// the content and the size of a cosign v3 bundle with a key and no log
// entry are not measured. The reading here keeps the bytes opaque, caps
// them at the proposal of 4096 bytes and refuses an empty bundle. cosign
// at root is the check. A tighter rule changes only `Bundle::new`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bundle(Vec<u8>);

/// Why bytes are no bundle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BundleError {
    /// No byte.
    Empty,
    /// More than `MAX_BUNDLE_BYTES` bytes.
    TooLarge,
}

impl fmt::Display for BundleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Empty => "the bundle is empty",
            Self::TooLarge => "the bundle is larger than 4096 bytes",
        })
    }
}

impl std::error::Error for BundleError {}

impl Bundle {
    /// The bundle of `bytes`.
    ///
    /// # Errors
    ///
    /// No byte, or more than `MAX_BUNDLE_BYTES`.
    pub fn new(bytes: Vec<u8>) -> Result<Self, BundleError> {
        if bytes.is_empty() {
            return Err(BundleError::Empty);
        }
        if bytes.len() > MAX_BUNDLE_BYTES {
            return Err(BundleError::TooLarge);
        }
        Ok(Self(bytes))
    }

    /// The bytes of the bundle.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.0
    }
}

/// Which Forgejo reply gave a tag message. The two forms differ in their
/// last byte (measured on Forgejo 17.0.1, recording facts M3 and 3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageSource {
    /// `POST /repos/{o}/{r}/tags` or `GET /repos/{o}/{r}/tags/{tag}`: the
    /// message as written, with no final LF.
    TagsApi,
    /// `GET /repos/{o}/{r}/git/tags/{sha}`: the message with one LF added.
    GitTagObject,
}

impl MessageSource {
    /// The message as written, from the `message` field of this reply.
    ///
    /// # Errors
    ///
    /// `MessageError::Suffix` when the end is not the end of this source:
    /// for `GitTagObject` exactly one final LF, for `TagsApi` none.
    pub fn written(self, message: &str) -> Result<&str, MessageError> {
        let body = match self {
            Self::TagsApi => message,
            Self::GitTagObject => message.strip_suffix('\n').ok_or(MessageError::Suffix)?,
        };
        if body.ends_with('\n') {
            return Err(MessageError::Suffix);
        }
        Ok(body)
    }

    /// The bytes that this source adds to the written message.
    fn added(self) -> usize {
        match self {
            Self::TagsApi => 0,
            Self::GitTagObject => 1,
        }
    }
}

/// Why a text is no tag message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageError {
    /// More bytes than `MAX_TAG_MESSAGE_BYTES` and the suffix of the source.
    TooLarge,
    /// The end does not fit the source.
    Suffix,
    /// Not exactly three lines.
    Lines,
    /// The first line is not `release-statement-1`.
    Header,
    /// A line is no strict base64.
    Base64,
    /// The statement is empty or larger than any canonical statement.
    Statement,
    /// The bundle is refused.
    Bundle(BundleError),
}

impl fmt::Display for MessageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge => formatter.write_str("the tag message is too large"),
            Self::Suffix => formatter.write_str("the tag message has a wrong end"),
            Self::Lines => formatter.write_str("the tag message has not three lines"),
            Self::Header => formatter.write_str("the tag message has a wrong header"),
            Self::Base64 => formatter.write_str("a line of the tag message is no strict base64"),
            Self::Statement => {
                formatter.write_str("the statement of the tag message has a wrong size")
            }
            Self::Bundle(error) => write!(formatter, "the bundle of the tag message: {error}"),
        }
    }
}

impl std::error::Error for MessageError {}

/// The text of a tag message, with no final LF.
fn encode_lines(statement: &[u8], bundle: &Bundle) -> String {
    format!(
        "{MESSAGE_HEADER}\n{}\n{}",
        base64::encode(statement),
        base64::encode(bundle.bytes())
    )
}

/// The message that the signer writes for one release tag: the statement
/// that `IssuedRuns::issue` built and its bundle.
#[derive(Debug, PartialEq, Eq)]
pub struct TagMessage {
    signable: Signable,
    bundle: Bundle,
}

impl TagMessage {
    /// The message of a signed statement. It cannot fail: the limits of the
    /// statement keep every message below `MAX_TAG_MESSAGE_BYTES`
    /// (`limits.rs`).
    #[must_use]
    pub fn new(signable: Signable, bundle: Bundle) -> Self {
        Self { signable, bundle }
    }

    /// The message text, with no final LF.
    #[must_use]
    pub fn encode(&self) -> String {
        encode_lines(self.signable.bytes(), &self.bundle)
    }

    /// The canonical statement that the signer signed.
    #[must_use]
    pub fn statement(&self) -> &[u8] {
        self.signable.bytes()
    }

    /// The bundle.
    #[must_use]
    pub fn bundle(&self) -> &Bundle {
        &self.bundle
    }
}

/// A tag message that a reader decoded from a Forgejo reply. Its statement
/// is not verified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnverifiedTagMessage {
    statement: Vec<u8>,
    bundle: Bundle,
}

impl UnverifiedTagMessage {
    /// The message of the `message` field of a Forgejo reply.
    ///
    /// # Errors
    ///
    /// Each `MessageError`, checked in the order of the variants.
    pub fn decode(message: &str, source: MessageSource) -> Result<Self, MessageError> {
        if message.len() > MAX_TAG_MESSAGE_BYTES + source.added() {
            return Err(MessageError::TooLarge);
        }

        let body = source.written(message)?;
        let mut lines = body.split('\n');
        let (Some(header), Some(statement), Some(bundle), None) =
            (lines.next(), lines.next(), lines.next(), lines.next())
        else {
            return Err(MessageError::Lines);
        };
        if header != MESSAGE_HEADER {
            return Err(MessageError::Header);
        }

        let statement = base64::decode(statement).map_err(|_| MessageError::Base64)?;
        let bundle = base64::decode(bundle).map_err(|_| MessageError::Base64)?;
        if statement.is_empty() || statement.len() > WORST_STATEMENT_BYTES {
            return Err(MessageError::Statement);
        }
        let bundle = Bundle::new(bundle).map_err(MessageError::Bundle)?;
        Ok(Self { statement, bundle })
    }

    /// The statement bytes. They are not verified: give them to cosign with
    /// the bundle first, then to `Statement::from_canonical`.
    #[must_use]
    pub fn unverified_statement(&self) -> &[u8] {
        &self.statement
    }

    /// The bundle.
    #[must_use]
    pub fn bundle(&self) -> &Bundle {
        &self.bundle
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::limits::{
        MAX_ASSETS_PER_TAG, MAX_NAME_BYTES, MAX_TAGS, MAX_WORKFLOW_BYTES, WORST_TAG_MESSAGE_BYTES,
    };
    use crate::names::{MAX_OWNER_BYTES, MAX_REPO_NAME_BYTES, RunId};
    use crate::statement::tests::{DIGEST, example};
    use crate::statement::{Asset, Assets, Statement, TagEntry, Tags};

    fn bundle(len: usize) -> Bundle {
        Bundle::new(vec![b'b'; len]).unwrap()
    }

    /// The text of the example statement with a bundle of 10 bytes.
    fn example_text() -> String {
        encode_lines(&example().canonical_bytes(), &bundle(10))
    }

    #[test]
    fn round_trip() {
        let text = example_text();
        assert!(text.starts_with("release-statement-1\n"));
        assert!(!text.ends_with('\n'));

        let expected = UnverifiedTagMessage {
            statement: example().canonical_bytes(),
            bundle: bundle(10),
        };
        assert_eq!(
            UnverifiedTagMessage::decode(&text, MessageSource::TagsApi).unwrap(),
            expected
        );
        assert_eq!(
            UnverifiedTagMessage::decode(&format!("{text}\n"), MessageSource::GitTagObject)
                .unwrap(),
            expected
        );
    }

    #[test]
    fn refusals() {
        let text = example_text();
        let decode = |text: &str, source| UnverifiedTagMessage::decode(text, source).unwrap_err();

        assert_eq!(
            decode(&text, MessageSource::GitTagObject),
            MessageError::Suffix
        );
        assert_eq!(
            decode(&format!("{text}\n\n"), MessageSource::GitTagObject),
            MessageError::Suffix
        );
        assert_eq!(
            decode(&format!("{text}\n"), MessageSource::TagsApi),
            MessageError::Suffix
        );
        assert_eq!(
            decode(&text.replacen("-1\n", "-2\n", 1), MessageSource::TagsApi),
            MessageError::Header
        );

        let lines: Vec<&str> = text.split('\n').collect();
        assert_eq!(
            decode(&lines[..2].join("\n"), MessageSource::TagsApi),
            MessageError::Lines
        );
        assert_eq!(
            decode(&format!("{text}\nAAAA"), MessageSource::TagsApi),
            MessageError::Lines
        );
        assert_eq!(
            decode(&text.replacen('\n', "\r\n", 1), MessageSource::TagsApi),
            MessageError::Header
        );
        assert_eq!(
            decode(
                &format!("{}\n{} \n{}", lines[0], lines[1], lines[2]),
                MessageSource::TagsApi
            ),
            MessageError::Base64
        );
        assert_eq!(
            decode(
                &format!("{}\n\n{}", lines[0], lines[2]),
                MessageSource::TagsApi
            ),
            MessageError::Statement
        );
        assert_eq!(
            decode(
                &format!("{}\n{}\n", lines[0], lines[1]),
                MessageSource::TagsApi
            ),
            MessageError::Suffix
        );
        assert_eq!(
            decode(
                &format!(
                    "{}\n{}\n{}",
                    lines[0],
                    lines[1],
                    base64::encode(&[b'b'; 4097])
                ),
                MessageSource::TagsApi
            ),
            MessageError::Bundle(BundleError::TooLarge)
        );
        assert_eq!(
            decode(
                &"A".repeat(MAX_TAG_MESSAGE_BYTES + 1),
                MessageSource::TagsApi
            ),
            MessageError::TooLarge
        );
        assert_eq!(Bundle::new(vec![]), Err(BundleError::Empty));
        assert_eq!(Bundle::new(vec![0; 4097]), Err(BundleError::TooLarge));
    }

    /// A text of exactly `len` bytes that starts with `prefix`.
    fn padded(prefix: &str, len: usize) -> String {
        format!("{prefix}{}", "a".repeat(len - prefix.len()))
    }

    /// A statement at every cap: the longest repository, workflow and run
    /// id, 32 tags of the longest form, 16 assets of 64-byte names each.
    #[test]
    fn worst_case_fits() {
        let tags = (0..MAX_TAGS)
            .map(|t| {
                let assets = (0..MAX_ASSETS_PER_TAG)
                    .map(|a| Asset {
                        name: padded(&format!("{a:02}"), MAX_NAME_BYTES).parse().unwrap(),
                        sha256: DIGEST.parse().unwrap(),
                    })
                    .collect();
                let tag = format!(
                    "{}-v999999999.999999999.999999999",
                    padded(&format!("c{t:02}"), 31)
                );
                TagEntry {
                    tag: tag.parse().unwrap(),
                    assets: Assets::new(assets).unwrap(),
                }
            })
            .collect();
        let repository = format!(
            "{}/{}",
            "o".repeat(MAX_OWNER_BYTES),
            "r".repeat(MAX_REPO_NAME_BYTES)
        );
        let statement = Statement::new(
            repository.parse().unwrap(),
            format!("{}.yml", "w".repeat(MAX_WORKFLOW_BYTES - 4))
                .parse()
                .unwrap(),
            "f".repeat(40).parse().unwrap(),
            RunId::try_from(crate::names::JS_INTEGER_MAX).unwrap(),
            "9999-12-31T23:59:59Z".parse().unwrap(),
            Tags::new(tags).unwrap(),
        );

        let text = encode_lines(&statement.canonical_bytes(), &bundle(MAX_BUNDLE_BYTES));
        assert!(
            text.len() <= WORST_TAG_MESSAGE_BYTES,
            "{} > {WORST_TAG_MESSAGE_BYTES}",
            text.len()
        );
        assert!(text.len() <= MAX_TAG_MESSAGE_BYTES);
        // The tags are 62 bytes, not 64, so the bound is a little loose.
        assert!(
            WORST_TAG_MESSAGE_BYTES - text.len() < 4000,
            "{}",
            text.len()
        );
        assert!(
            UnverifiedTagMessage::decode(&format!("{text}\n"), MessageSource::GitTagObject).is_ok()
        );
    }
}
