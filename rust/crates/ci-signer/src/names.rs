//! The names and ids of a release statement. A value of each type is always
//! valid: its field is private, and `FromStr` and serde both run the one
//! check of the type.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::limits::{MAX_NAME_BYTES, MAX_WORKFLOW_BYTES};

/// The largest integer that JSON keeps exact in every reader: 2^53 - 1.
pub const JS_INTEGER_MAX: u64 = 9_007_199_254_740_991;

/// Why a text is no valid name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NameError {
    /// The type that refused the text.
    pub kind: &'static str,
    /// What is wrong with the text.
    pub fault: NameFault,
}

/// What is wrong with a name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NameFault {
    /// The text is longer than the type allows.
    TooLong,
    /// The text breaks the grammar of the type.
    Grammar,
}

impl fmt::Display for NameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let fault = match self.fault {
            NameFault::TooLong => "too long",
            NameFault::Grammar => "not in the grammar",
        };
        write!(formatter, "{}: {fault}", self.kind)
    }
}

impl std::error::Error for NameError {}

/// One ASCII letter or digit.
fn alnum(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
}

/// One lower-case hex digit.
fn lower_hex(byte: u8) -> bool {
    byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
}

/// `[A-Za-z0-9][A-Za-z0-9<extra>]{0,max-1}`: the shape of a repository
/// part and of an asset name.
fn plain(text: &str, max: usize, extra: &[u8]) -> Result<(), NameFault> {
    let bytes = text.as_bytes();
    if bytes.len() > max {
        return Err(NameFault::TooLong);
    }

    let Some((first, rest)) = bytes.split_first() else {
        return Err(NameFault::Grammar);
    };
    if !alnum(*first) || !rest.iter().all(|byte| alnum(*byte) || extra.contains(byte)) {
        return Err(NameFault::Grammar);
    }
    Ok(())
}

/// `0|[1-9][0-9]{0,8}`: one number of a tag version, with no leading zero.
/// So two tags that root reads as one version (`int()` of each number in
/// `newest_tagged_version`) are one text.
fn version_number(text: &str) -> bool {
    let Some((first, rest)) = text.as_bytes().split_first() else {
        return false;
    };
    if *first == b'0' {
        return rest.is_empty();
    }
    text.len() <= MAX_VERSION_DIGITS && text.bytes().all(|byte| byte.is_ascii_digit())
}

/// The most digits of one number of a tag version. `_TAG_RE` of
/// `handover/executor/source.py` has no cap. So this grammar is a strict
/// subset of `_TAG_RE`.
// CONTRACT-QUESTION: forgejo-release-path.md section 4.5 step 4 names the
// form `<component>-vX.Y.Z` with no digit cap. The reading here caps each
// number at 9 digits, so it fits a u32 and the tag stays short. The signer
// then refuses some tags that root would accept. PR 4 can tighten `_TAG_RE`
// to the same cap. A change is this constant and one vector.
const MAX_VERSION_DIGITS: usize = 9;

/// A string newtype with one check. `$pattern` is the same rule as a
/// regular expression, for the JSON Schema.
macro_rules! name_type {
    ($(#[$meta:meta])* $name:ident, $pattern:expr, $check:expr) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        #[cfg_attr(test, schemars(extend("pattern" = $pattern)))]
        pub struct $name(String);

        impl $name {
            /// The text of the value.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = NameError;

            fn from_str(text: &str) -> Result<Self, NameError> {
                let check: fn(&str) -> Result<(), NameFault> = $check;
                match check(text) {
                    Ok(()) => Ok(Self(text.to_owned())),
                    Err(fault) => Err(NameError { kind: stringify!($name), fault }),
                }
            }
        }

        impl TryFrom<String> for $name {
            type Error = NameError;

            fn try_from(text: String) -> Result<Self, NameError> {
                text.parse()
            }
        }

        impl From<$name> for String {
            fn from(value: $name) -> String {
                value.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

/// The most bytes of an owner: 39, from `REPO_RE` of
/// `handover/mcpserver.py`.
pub const MAX_OWNER_BYTES: usize = 39;

/// The most bytes of a repository name: 100, from `REPO_RE`.
pub const MAX_REPO_NAME_BYTES: usize = 100;

name_type!(
    /// The owner of a repository on the Forgejo instance: the first part of
    /// `REPO_RE`, `[A-Za-z0-9][A-Za-z0-9._-]{0,38}`.
    Owner,
    "^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$",
    |text| plain(text, MAX_OWNER_BYTES, b"._-")
);

name_type!(
    /// The name of a repository with no owner: the second part of
    /// `REPO_RE`, `[A-Za-z0-9][A-Za-z0-9._-]{0,99}`.
    RepoName,
    "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
    |text| plain(text, MAX_REPO_NAME_BYTES, b"._-")
);

name_type!(
    /// A workflow file name, not a path: `[A-Za-z0-9][A-Za-z0-9._-]*` with
    /// the end `.yml` or `.yaml`, at most `MAX_WORKFLOW_BYTES`. The run reply
    /// names it in `workflow_id`, for example `release.yml`.
    WorkflowFile,
    "^([A-Za-z0-9][A-Za-z0-9._-]{0,59}\\.yml|[A-Za-z0-9][A-Za-z0-9._-]{0,58}\\.yaml)$",
    |text| {
        plain(text, MAX_WORKFLOW_BYTES, b"._-")?;
        let stem = text.strip_suffix(".yml").or_else(|| text.strip_suffix(".yaml"));
        match stem {
            Some(stem) if !stem.is_empty() => Ok(()),
            _ => Err(NameFault::Grammar),
        }
    }
);

name_type!(
    /// A release tag, `<component>-vX.Y.Z`: the component
    /// `[a-z][a-z0-9-]{1,30}` of `_TAG_RE`, then `-v`, then three numbers
    /// of 1 to 9 digits each with no leading zero (section 4.5 step 4).
    ReleaseTag,
    "^[a-z][a-z0-9-]{1,30}-v(0|[1-9][0-9]{0,8})\\.(0|[1-9][0-9]{0,8})\\.(0|[1-9][0-9]{0,8})$",
    |text| {
        if text.len() > MAX_NAME_BYTES {
            return Err(NameFault::TooLong);
        }

        // The version has no `v`, so the split is at the last `-v`.
        let Some((component, version)) = text.rsplit_once("-v") else {
            return Err(NameFault::Grammar);
        };
        let mut numbers = version.split('.');
        let three = numbers.by_ref().take(3).filter(|part| version_number(part)).count() == 3;
        if !three || numbers.next().is_some() || !component_name(component) {
            return Err(NameFault::Grammar);
        }
        Ok(())
    }
);

/// `[a-z][a-z0-9-]{1,30}`: the component part of a release tag.
fn component_name(text: &str) -> bool {
    let bytes = text.as_bytes();
    let Some((first, rest)) = bytes.split_first() else {
        return false;
    };
    (2..=31).contains(&bytes.len())
        && first.is_ascii_lowercase()
        && rest
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

impl ReleaseTag {
    /// The component of the tag: the text before the last `-v`.
    #[must_use]
    pub fn component(&self) -> &str {
        self.0
            .rsplit_once("-v")
            .map_or("", |(component, _)| component)
    }
}

name_type!(
    /// A plain asset name: `ASSET_RE` of `handover/mcpserver.py`,
    /// `[A-Za-z0-9][A-Za-z0-9._+-]*`, at most `MAX_NAME_BYTES`.
    AssetName,
    "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$",
    |text| plain(text, MAX_NAME_BYTES, b"._+-")
);

name_type!(
    /// A SHA-256 digest: 64 lower-case hex digits.
    Sha256Hex,
    "^[0-9a-f]{64}$",
    |text| hex_of(text, 64)
);

name_type!(
    /// A git commit id: 40 lower-case hex digits. The recordings show a
    /// sha1 repository. A sha256 object id is refused.
    // CONTRACT-QUESTION: forgejo-release-path.md section 4.5 names
    // `<40 hex>`. A repository with the sha256 object format fails closed
    // here. A change is this check and the schema pattern.
    CommitSha,
    "^[0-9a-f]{40}$",
    |text| hex_of(text, 40)
);

impl Sha256Hex {
    /// The hex text of a digest. It is valid by construction.
    pub(crate) fn from_digest(digest: [u8; 32]) -> Self {
        let mut text = String::with_capacity(64);
        for byte in digest {
            text.push(hex_digit(byte >> 4));
            text.push(hex_digit(byte & 0x0f));
        }
        Self(text)
    }
}

/// The lower-case hex digit of the low 4 bits of `bits`. Each arm computes
/// its digit: the mask leaves only 0 to 15, and the second arm takes 10 to
/// 15.
fn hex_digit(bits: u8) -> char {
    match bits & 0x0f {
        value @ 0..=9 => char::from(b'0' + value),
        value => char::from(b'a' + (value - 10)),
    }
}

/// Exactly `len` lower-case hex digits.
fn hex_of(text: &str, len: usize) -> Result<(), NameFault> {
    if text.len() > len {
        return Err(NameFault::TooLong);
    }
    if text.len() != len || !text.bytes().all(lower_hex) {
        return Err(NameFault::Grammar);
    }
    Ok(())
}

/// A repository as `owner/name`, with exactly one slash.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(
    test,
    schemars(
        with = "String",
        extend("pattern" = "^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    )
)]
pub struct RepoFullName {
    /// The owner part.
    owner: Owner,
    /// The name part.
    name: RepoName,
}

impl RepoFullName {
    /// The repository `owner/name`.
    #[must_use]
    pub fn new(owner: Owner, name: RepoName) -> Self {
        Self { owner, name }
    }

    /// The owner part.
    #[must_use]
    pub fn owner(&self) -> &Owner {
        &self.owner
    }

    /// The name part.
    #[must_use]
    pub fn name(&self) -> &RepoName {
        &self.name
    }
}

impl FromStr for RepoFullName {
    type Err = NameError;

    fn from_str(text: &str) -> Result<Self, NameError> {
        let refuse = NameError {
            kind: "RepoFullName",
            fault: NameFault::Grammar,
        };
        let (owner, name) = text.split_once('/').ok_or(refuse)?;
        let owner = owner.parse().map_err(|_| refuse)?;
        let name = name.parse().map_err(|_| refuse)?;
        Ok(Self { owner, name })
    }
}

impl TryFrom<String> for RepoFullName {
    type Error = NameError;

    fn try_from(text: String) -> Result<Self, NameError> {
        text.parse()
    }
}

impl From<RepoFullName> for String {
    fn from(value: RepoFullName) -> String {
        value.to_string()
    }
}

impl fmt::Display for RepoFullName {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}/{}", self.owner, self.name)
    }
}

/// The API id of a Forgejo Actions run: 1 to 2^53 - 1. This is the `id` of
/// the run reply, not the `index_in_repo` of its `html_url`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "u64", into = "u64")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("minimum" = 1, "maximum" = JS_INTEGER_MAX)))]
pub struct RunId(u64);

/// Why a number is no run id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunIdError;

impl fmt::Display for RunIdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a run id is 1 to 2^53 - 1")
    }
}

impl std::error::Error for RunIdError {}

impl RunId {
    /// The number of the id.
    #[must_use]
    pub fn get(self) -> u64 {
        self.0
    }
}

impl TryFrom<u64> for RunId {
    type Error = RunIdError;

    fn try_from(number: u64) -> Result<Self, RunIdError> {
        if (1..=JS_INTEGER_MAX).contains(&number) {
            return Ok(Self(number));
        }
        Err(RunIdError)
    }
}

impl From<RunId> for u64 {
    fn from(value: RunId) -> u64 {
        value.0
    }
}

impl FromStr for RunId {
    type Err = RunIdError;

    /// A decimal number with no sign and no leading zero.
    fn from_str(text: &str) -> Result<Self, RunIdError> {
        if text.starts_with('0') || !text.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(RunIdError);
        }
        text.parse::<u64>().map_err(|_| RunIdError)?.try_into()
    }
}

impl fmt::Display for RunId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_tags() {
        let tag: ReleaseTag = "vmm-v0.1.24".parse().unwrap();
        assert_eq!(tag.component(), "vmm");
        assert_eq!(
            "creche-release-v1.0.0"
                .parse::<ReleaseTag>()
                .unwrap()
                .component(),
            "creche-release"
        );
        assert!("vmm-v0.0.0".parse::<ReleaseTag>().is_ok());
        assert!("vmm-v999999999.10.0".parse::<ReleaseTag>().is_ok());

        for bad in [
            "main",
            "Vmm-v1.2.3",
            "vmm-v1.2",
            "vmm-v1.2.3.4",
            "v-v1.2.3",
            "vmm-v1.2.3\n",
            "vmm-v1.2.x",
            "vmm-v1234567890.0.0",
            "vmm-v000000001.0.0",
            "vmm-v0.1.024",
            "vmm-v00.1.24",
            "vmm-v0.01.24",
            "vmm_x-v1.2.3",
            "-vmm-v1.2.3",
        ] {
            assert!(bad.parse::<ReleaseTag>().is_err(), "{bad}");
        }
    }

    #[test]
    fn plain_names() {
        assert!(
            "vmm-x86_64-unknown-linux-musl.tar.gz"
                .parse::<AssetName>()
                .is_ok()
        );
        assert!("a".repeat(64).parse::<AssetName>().is_ok());
        assert_eq!(
            "a".repeat(65).parse::<AssetName>().unwrap_err().fault,
            NameFault::TooLong
        );
        for bad in ["", ".hidden", "a/b", "a b", "ä"] {
            assert!(bad.parse::<AssetName>().is_err(), "{bad}");
        }

        assert!("release.yml".parse::<WorkflowFile>().is_ok());
        assert!("tags.yaml".parse::<WorkflowFile>().is_ok());
        for bad in [
            ".yml",
            "release.yml.txt",
            "a/release.yml",
            "release",
            ".github",
        ] {
            assert!(bad.parse::<WorkflowFile>().is_err(), "{bad}");
        }
    }

    #[test]
    fn repositories() {
        let full: RepoFullName = "example-owner/creche".parse().unwrap();
        assert_eq!(full.owner().as_str(), "example-owner");
        assert_eq!(full.to_string(), "example-owner/creche");
        for bad in ["creche", "a/b/c", "/creche", "owner/", "o wner/creche"] {
            assert!(bad.parse::<RepoFullName>().is_err(), "{bad}");
        }
    }

    #[test]
    fn digest_hex() {
        let digest: [u8; 32] = std::array::from_fn(|n| u8::try_from(n * 8 + 7).unwrap());
        let expected: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        assert_eq!(Sha256Hex::from_digest(digest).as_str(), expected);
        assert_eq!(Sha256Hex::from_digest([0xff; 32]).as_str(), "f".repeat(64));
        assert_eq!(Sha256Hex::from_digest([0; 32]).as_str(), "0".repeat(64));
    }

    #[test]
    fn digests_and_ids() {
        assert!("a".repeat(64).parse::<Sha256Hex>().is_ok());
        assert!("A".repeat(64).parse::<Sha256Hex>().is_err());
        assert!("a".repeat(40).parse::<CommitSha>().is_ok());
        assert!("a".repeat(39).parse::<CommitSha>().is_err());
        assert!(
            "a".repeat(64).parse::<CommitSha>().is_err(),
            "a sha256 object id"
        );

        assert!(RunId::try_from(1).is_ok());
        assert!(RunId::try_from(JS_INTEGER_MAX).is_ok());
        assert!(RunId::try_from(0).is_err());
        assert!(RunId::try_from(JS_INTEGER_MAX + 1).is_err());
        assert!("048".parse::<RunId>().is_err());
        assert_eq!("48".parse::<RunId>().unwrap().get(), 48);
    }
}
