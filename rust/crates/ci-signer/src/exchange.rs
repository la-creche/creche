//! The exchange with the job VM (section 4.5 steps 3, 4 and 6): one
//! request line from the allocator, one answer line from the signer.
//!
//! ```text
//! request: {"v":1,"tags":[{"tag":"vmm-v0.1.24","assets":[{"name":"..","sha256":".."}]}]}
//! answer:  {"v":1,"signed":{"statement":"<base64>","bundle":"<base64>"}}
//!     or:  {"v":1,"refused":"<code>"}
//! ```
//!
//! The job gives only tags and digests (FR7). A request with any other key,
//! a repository, a commit or a run included, is refused.
// CONTRACT-QUESTION: forgejo-release-path.md section 4.5 gives only the
// content of the request and of the answer. The reading here is one strict
// JSON line each, with the version field `v` 1 and no unknown key. A change
// moves only this module and `vectors/exchange.json`. A version 2 can sit
// beside version 1.

use std::fmt;

use serde::Deserialize;

use crate::base64;
use crate::message::TagMessage;
use crate::names::{AssetName, NameError, ReleaseTag, Sha256Hex};
use crate::statement::{Asset, Assets, LimitError, TagEntry, Tags};

/// The most bytes of one request line, without its LF.
pub const REQUEST_MAX_BYTES: usize = 131_072;

/// The one version of the exchange.
const VERSION: u64 = 1;

/// Why a request line is refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RequestError {
    /// More than `REQUEST_MAX_BYTES` bytes.
    TooLarge,
    /// No strict JSON text.
    NotJson,
    /// A missing or unknown key, or a wrong type.
    Shape,
    /// `v` is not 1.
    Version,
    /// A tag, an asset name or a digest breaks its grammar.
    Grammar(NameError),
    /// The list breaks a limit of section 4.5 step 4.
    Limits(LimitError),
}

impl RequestError {
    /// The code of the answer for this refusal.
    #[must_use]
    pub fn answer_code(&self) -> AnswerCode {
        match self {
            Self::TooLarge | Self::Limits(_) => AnswerCode::Limits,
            Self::NotJson | Self::Shape | Self::Version | Self::Grammar(_) => AnswerCode::Grammar,
        }
    }
}

impl fmt::Display for RequestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge => formatter.write_str("the request is too large"),
            Self::NotJson => formatter.write_str("the request is no strict JSON text"),
            Self::Shape => formatter.write_str("the request has a wrong key or type"),
            Self::Version => formatter.write_str("the request version is not 1"),
            Self::Grammar(error) => write!(formatter, "the request: {error}"),
            Self::Limits(error) => write!(formatter, "the request: {error}"),
        }
    }
}

impl std::error::Error for RequestError {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawAsset {
    name: String,
    sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawTag {
    tag: String,
    assets: Vec<RawAsset>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRequest {
    v: u64,
    tags: Vec<RawTag>,
}

/// One request of the allocator: the tags of the run with their assets.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    tags: Tags,
}

impl Request {
    /// The request of one line, without its LF.
    ///
    /// # Errors
    ///
    /// The first `RequestError` in the order of its variants.
    pub fn parse(line: &[u8]) -> Result<Self, RequestError> {
        if line.len() > REQUEST_MAX_BYTES {
            return Err(RequestError::TooLarge);
        }
        if !creche_wire::strict(line) {
            return Err(RequestError::NotJson);
        }

        let raw: RawRequest = serde_json::from_slice(line).map_err(|_| RequestError::Shape)?;
        if raw.v != VERSION {
            return Err(RequestError::Version);
        }

        let mut entries = Vec::with_capacity(raw.tags.len());
        for tag in raw.tags {
            let mut assets = Vec::with_capacity(tag.assets.len());
            for asset in tag.assets {
                let name: AssetName = asset.name.parse().map_err(RequestError::Grammar)?;
                let sha256: Sha256Hex = asset.sha256.parse().map_err(RequestError::Grammar)?;
                assets.push(Asset { name, sha256 });
            }
            let tag: ReleaseTag = tag.tag.parse().map_err(RequestError::Grammar)?;
            let assets = Assets::new(assets).map_err(RequestError::Limits)?;
            entries.push(TagEntry { tag, assets });
        }
        let tags = Tags::new(entries).map_err(RequestError::Limits)?;
        Ok(Self { tags })
    }

    /// The tags of the request, sorted.
    #[must_use]
    pub fn tags(&self) -> &Tags {
        &self.tags
    }

    pub(crate) fn into_tags(self) -> Tags {
        self.tags
    }
}

/// The code of a refused answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnswerCode {
    /// The request breaks the grammar.
    Grammar,
    /// The request breaks a limit.
    Limits,
    /// The run already got its statement.
    AlreadyIssued,
    /// The run is not eligible.
    NotEligible,
    /// The signer failed: a reply, cosign or a tag creation.
    Fault,
}

impl AnswerCode {
    /// The word on the line.
    #[must_use]
    pub fn word(self) -> &'static str {
        match self {
            Self::Grammar => "grammar",
            Self::Limits => "limits",
            Self::AlreadyIssued => "already_issued",
            Self::NotEligible => "not_eligible",
            Self::Fault => "fault",
        }
    }
}

/// The answer of the signer to one request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Answer {
    /// The statement and its bundle.
    Signed(TagMessage),
    /// A refusal.
    Refused(AnswerCode),
}

impl Answer {
    /// The answer as one JSON line with its LF.
    #[must_use]
    pub fn to_line(&self) -> Vec<u8> {
        // Each value is base64 or a fixed word, so no text needs a JSON
        // escape.
        let line = match self {
            Self::Signed(message) => format!(
                "{{\"v\":{VERSION},\"signed\":{{\"statement\":\"{}\",\"bundle\":\"{}\"}}}}\n",
                base64::encode(message.unverified_statement()),
                base64::encode(message.bundle().bytes())
            ),
            Self::Refused(code) => format!("{{\"v\":{VERSION},\"refused\":\"{}\"}}\n", code.word()),
        };
        line.into_bytes()
    }
}
