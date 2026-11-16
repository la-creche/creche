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
// Decided by the operator on 2026-11-16: the job VM and the signer exchange
// one strict JSON line each, with a version field (forgejo-release-path.md
// section 4.5). Here the field is `v` 1 and no unknown key passes. A change
// moves only this module and `vectors/exchange.json`. A version 2 can sit
// beside version 1.

use std::fmt;

use serde::Deserialize;

use crate::base64;
use crate::message::{Bundle, TagMessage};
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
#[derive(Debug, PartialEq, Eq)]
pub enum Answer {
    /// The statement and its bundle. Only a `TagMessage` of an issued
    /// statement fits here, never a decoded one.
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
            Self::Signed(message) => signed_line(message.statement(), message.bundle()),
            Self::Refused(code) => format!("{{\"v\":{VERSION},\"refused\":\"{}\"}}\n", code.word()),
        };
        line.into_bytes()
    }
}

/// The signed answer line of `statement` and `bundle`, with its LF.
fn signed_line(statement: &[u8], bundle: &Bundle) -> String {
    format!(
        "{{\"v\":{VERSION},\"signed\":{{\"statement\":\"{}\",\"bundle\":\"{}\"}}}}\n",
        base64::encode(statement),
        base64::encode(bundle.bytes())
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct SignedCase {
        statement: String,
        bundle: String,
    }

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields, rename_all = "snake_case")]
    enum AnswerCase {
        Refused(String),
        Signed(SignedCase),
    }

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct AnswerLine {
        answer: AnswerCase,
        line: String,
        note: String,
    }

    #[derive(Deserialize)]
    struct ExchangeFile {
        answers: Vec<AnswerLine>,
    }

    fn code_of(word: &str) -> AnswerCode {
        [
            AnswerCode::Grammar,
            AnswerCode::Limits,
            AnswerCode::AlreadyIssued,
            AnswerCode::NotEligible,
            AnswerCode::Fault,
        ]
        .into_iter()
        .find(|code| code.word() == word)
        .unwrap_or_else(|| panic!("{word}"))
    }

    /// The answers of `vectors/exchange.json`. A signed answer of a vector
    /// holds a statement that no `Eligible` run of the recordings gives, so
    /// this test writes its line from the bytes. The test
    /// `signed_answer_of_an_issued_run` in `tests/signer/issue.rs` writes a
    /// line through `Answer::Signed`.
    #[test]
    fn answer_vectors() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/vectors/exchange.json");
        let file: ExchangeFile =
            serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        assert!(!file.answers.is_empty());
        for case in file.answers {
            let line = match case.answer {
                AnswerCase::Refused(word) => {
                    String::from_utf8(Answer::Refused(code_of(&word)).to_line()).unwrap()
                }
                AnswerCase::Signed(signed) => {
                    let statement = base64::decode(&signed.statement).unwrap();
                    let bundle = Bundle::new(base64::decode(&signed.bundle).unwrap()).unwrap();
                    signed_line(&statement, &bundle)
                }
            };
            assert_eq!(line, case.line, "{}", case.note);
        }
    }
}
