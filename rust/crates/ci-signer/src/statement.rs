//! The release statement (`docs/design/forgejo-release-path.md`, FR7 and
//! section 4.5): what the signer signs for one run, and what root reads at
//! P1 after cosign passes.
//!
//! The canonical bytes are compact JSON in the key order of the note, with
//! no whitespace and no final LF. The tags are sorted by name and the
//! assets of each tag by name, so the bytes depend only on the content.
//! `from_canonical` takes exactly these bytes and nothing else.
//!
//! A `Statement` has no serde reader and no way to the bytes to sign.
//! `from_canonical` is its one reader. Only `IssuedRuns::issue` makes a
//! `Signable` (`issue.rs`). So a statement that a caller parsed can never
//! be signed.

use std::fmt;

use serde::{Deserialize, Deserializer};

use crate::limits::{MAX_ASSETS_PER_TAG, MAX_TAGS, WORST_STATEMENT_BYTES};
use crate::names::{
    AssetName, CommitSha, ReleaseTag, RepoFullName, RunId, Sha256Hex, WorkflowFile,
};
use crate::signed_at::SignedAt;

/// The version of the statement format: the value of the key `statement`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct StatementVersion;

/// The one version that this crate writes and reads.
const STATEMENT_VERSION: u64 = 1;

impl<'de> Deserialize<'de> for StatementVersion {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let number = u64::deserialize(deserializer)?;
        if number != STATEMENT_VERSION {
            return Err(serde::de::Error::custom("the statement version is not 1"));
        }
        Ok(Self)
    }
}

/// The event of a statement. Check 2 of section 4.4 passes only a push.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum Event {
    /// A push to `main`.
    #[serde(rename = "push")]
    Push,
}

/// The ref of a statement. Check 3 of section 4.4 passes only
/// `refs/heads/main`, and the statement names it `main`.
// Decided by the operator on 2026-11-16: the statement spells the ref
// `main`, as the example of section 4.5 and the P1 row of section 5 do.
// Check 3 reads the full ref. A change is one serde rename and the vectors.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub enum Branch {
    /// The branch `main`.
    #[serde(rename = "main")]
    Main,
}

/// Why a list of tags or assets is refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LimitError {
    /// No tag.
    NoTag,
    /// More than `MAX_TAGS` tags.
    TooManyTags,
    /// More than `MAX_ASSETS_PER_TAG` assets in one tag.
    TooManyAssets,
    /// One tag twice.
    DuplicateTag,
    /// One asset name twice in one tag.
    DuplicateAsset,
    /// Two tags of one component.
    DuplicateComponent,
}

impl fmt::Display for LimitError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::NoTag => "a statement has no tag",
            Self::TooManyTags => "a statement has more than 32 tags",
            Self::TooManyAssets => "a tag has more than 16 assets",
            Self::DuplicateTag => "a tag is given twice",
            Self::DuplicateAsset => "an asset name is given twice in one tag",
            Self::DuplicateComponent => "a component has two tags",
        })
    }
}

impl std::error::Error for LimitError {}

/// One asset of a tag: its name and the SHA-256 of its bytes.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = crate::schema::require_each_key))]
pub struct Asset {
    /// The plain asset name.
    pub name: AssetName,
    /// The SHA-256 of the asset.
    pub sha256: Sha256Hex,
}

/// The assets of one tag: 0 to `MAX_ASSETS_PER_TAG`, sorted by name, each
/// name once. A `venv` or `units` tag has none (section 4.5 step 3).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(try_from = "Vec<Asset>")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("maxItems" = MAX_ASSETS_PER_TAG)))]
pub struct Assets(Vec<Asset>);

impl Assets {
    /// The assets of `items`, sorted by name.
    ///
    /// # Errors
    ///
    /// More than `MAX_ASSETS_PER_TAG` items, or one name twice.
    pub fn new(mut items: Vec<Asset>) -> Result<Self, LimitError> {
        if items.len() > MAX_ASSETS_PER_TAG {
            return Err(LimitError::TooManyAssets);
        }

        items.sort_by(|a, b| a.name.cmp(&b.name));
        if items
            .windows(2)
            .any(|pair| pair.first().map(|a| &a.name) == pair.get(1).map(|b| &b.name))
        {
            return Err(LimitError::DuplicateAsset);
        }
        Ok(Self(items))
    }

    /// The assets, sorted by name.
    #[must_use]
    pub fn items(&self) -> &[Asset] {
        &self.0
    }

    fn write(&self, out: &mut String) {
        write_list(out, &self.0, Asset::write);
    }
}

impl TryFrom<Vec<Asset>> for Assets {
    type Error = LimitError;

    fn try_from(items: Vec<Asset>) -> Result<Self, LimitError> {
        Self::new(items)
    }
}

impl Asset {
    fn write(&self, out: &mut String) {
        out.push_str("{\"name\":");
        write_string(out, self.name.as_str());
        out.push_str(",\"sha256\":");
        write_string(out, self.sha256.as_str());
        out.push('}');
    }
}

/// One tag of a statement with its assets.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(transform = crate::schema::require_each_key))]
pub struct TagEntry {
    /// The release tag.
    pub tag: ReleaseTag,
    /// Its assets.
    pub assets: Assets,
}

/// The tags of a statement: 1 to `MAX_TAGS`, sorted by tag, one tag for
/// each component. The schema does not express the order, the uniqueness
/// or the rule of one tag for each component. A reader must apply the
/// canonical check (re-encode and compare) and pass the vectors.
// Decided by the operator on 2026-11-16: the signer sorts the tags and the
// assets by name. It refuses a duplicate, an empty tag list and two tags of
// one component: one run releases one version of a component. A change is
// this type and the vector files.
// Decided by the operator on 2026-11-16: root checks every tag of a
// statement against the catalog, and the signer does not know component
// kinds. So the signer does not check the rule of section 4.5 step 3 (a
// `venv` or `units` tag has no asset). It does not check that a component
// belongs to the repository either. Root's P1 refuses both.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(try_from = "Vec<TagEntry>")]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(test, schemars(extend("minItems" = 1, "maxItems" = MAX_TAGS)))]
pub struct Tags(Vec<TagEntry>);

impl Tags {
    /// The tags of `items`, sorted by tag.
    ///
    /// # Errors
    ///
    /// No item, more than `MAX_TAGS` items, one tag twice, or two tags of
    /// one component.
    pub fn new(mut items: Vec<TagEntry>) -> Result<Self, LimitError> {
        if items.is_empty() {
            return Err(LimitError::NoTag);
        }
        if items.len() > MAX_TAGS {
            return Err(LimitError::TooManyTags);
        }

        items.sort_by(|a, b| a.tag.cmp(&b.tag));
        if items
            .windows(2)
            .any(|pair| pair.first().map(|a| &a.tag) == pair.get(1).map(|b| &b.tag))
        {
            return Err(LimitError::DuplicateTag);
        }
        let mut components: Vec<&str> = items.iter().map(|entry| entry.tag.component()).collect();
        components.sort_unstable();
        if components
            .windows(2)
            .any(|pair| pair.first() == pair.get(1))
        {
            return Err(LimitError::DuplicateComponent);
        }
        Ok(Self(items))
    }

    /// The canonical JSON of the list, as the statement holds it.
    #[must_use]
    pub fn canonical_json(&self) -> String {
        let mut out = String::new();
        self.write(&mut out);
        out
    }

    fn write(&self, out: &mut String) {
        write_list(out, &self.0, TagEntry::write);
    }

    /// The tags, sorted by tag.
    #[must_use]
    pub fn items(&self) -> &[TagEntry] {
        &self.0
    }
}

impl TryFrom<Vec<TagEntry>> for Tags {
    type Error = LimitError;

    fn try_from(items: Vec<TagEntry>) -> Result<Self, LimitError> {
        Self::new(items)
    }
}

impl TagEntry {
    fn write(&self, out: &mut String) {
        out.push_str("{\"tag\":");
        write_string(out, self.tag.as_str());
        out.push_str(",\"assets\":");
        self.assets.write(out);
        out.push('}');
    }
}

/// Writes `text` as a JSON string. Each string of a statement holds only
/// the bytes of its grammar: ASCII letters, digits and `._+-:/`. JSON
/// escapes none of them, so the text goes in as it is. The test
/// `grammar_bytes_need_no_escape` holds this.
fn write_string(out: &mut String, text: &str) {
    out.push('"');
    out.push_str(text);
    out.push('"');
}

/// Writes `items` as a JSON array, with a comma between two.
fn write_list<T>(out: &mut String, items: &[T], write: fn(&T, &mut String)) {
    out.push('[');
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        write(item, out);
    }
    out.push(']');
}

/// One release statement, as `IssuedRuns::issue` builds it or as
/// `Statement::from_canonical` reads it. It has no serde reader, and it
/// cannot become the bytes to sign.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Statement {
    repository: RepoFullName,
    workflow: WorkflowFile,
    commit: CommitSha,
    run: RunId,
    signed_at: SignedAt,
    tags: Tags,
}

/// One release statement. The field order is the key order of the
/// canonical bytes.
///
/// Only `IssuedRuns::issue` builds a statement to sign, from an `Eligible`
/// run. Root reads one with `from_canonical`.
// The wire form that `from_canonical` reads, and the source of the JSON
// Schema. It is private, so no caller reads a statement past the byte cap
// and the canonical check.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
#[cfg_attr(
    test,
    schemars(rename = "Statement", transform = crate::schema::require_each_key)
)]
pub(crate) struct WireStatement {
    #[cfg_attr(test, schemars(with = "u64", extend("const" = STATEMENT_VERSION)))]
    statement: StatementVersion,
    repository: RepoFullName,
    workflow: WorkflowFile,
    event: Event,
    #[serde(rename = "ref")]
    ref_: Branch,
    commit: CommitSha,
    run: RunId,
    signed_at: SignedAt,
    tags: Tags,
}

/// Why bytes are no canonical statement.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatementError {
    /// More than `WORST_STATEMENT_BYTES` bytes.
    TooLarge,
    /// No strict JSON text: bad UTF-8, bad JSON, a key twice.
    NotJson,
    /// A missing, extra or invalid field, or a limit broken.
    Shape,
    /// Valid, but not the canonical bytes of the statement.
    NotCanonical,
}

impl fmt::Display for StatementError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::TooLarge => "the statement is too large",
            Self::NotJson => "the statement is no strict JSON text",
            Self::Shape => "the statement has a wrong field",
            Self::NotCanonical => "the statement is not in its canonical form",
        })
    }
}

impl std::error::Error for StatementError {}

impl Statement {
    /// The statement of these facts. `issue.rs` calls it with the facts of
    /// an `Eligible` run, and `from_canonical` with the facts it read.
    pub(crate) fn new(
        repository: RepoFullName,
        workflow: WorkflowFile,
        commit: CommitSha,
        run: RunId,
        signed_at: SignedAt,
        tags: Tags,
    ) -> Self {
        Self {
            repository,
            workflow,
            commit,
            run,
            signed_at,
            tags,
        }
    }

    /// The canonical bytes: compact JSON in the key order of the note,
    /// ASCII only, no final LF. The writer cannot fail.
    #[must_use]
    pub fn canonical_bytes(&self) -> Vec<u8> {
        let mut out = String::with_capacity(WORST_STATEMENT_BYTES);
        out.push_str("{\"statement\":");
        out.push_str(&STATEMENT_VERSION.to_string());
        out.push_str(",\"repository\":");
        write_string(&mut out, &self.repository.to_string());
        out.push_str(",\"workflow\":");
        write_string(&mut out, self.workflow.as_str());
        out.push_str(",\"event\":\"push\",\"ref\":\"main\",\"commit\":");
        write_string(&mut out, self.commit.as_str());
        out.push_str(",\"run\":");
        out.push_str(&self.run.to_string());
        out.push_str(",\"signed_at\":");
        write_string(&mut out, &self.signed_at.to_string());
        out.push_str(",\"tags\":");
        self.tags.write(&mut out);
        out.push('}');
        out.into_bytes()
    }

    /// The statement of `bytes`, which must be its canonical bytes.
    ///
    /// # Errors
    ///
    /// `TooLarge`, then `NotJson`, then `Shape`, then `NotCanonical`: a key
    /// in another order, whitespace, tags or assets out of order.
    pub fn from_canonical(bytes: &[u8]) -> Result<Self, StatementError> {
        if bytes.len() > WORST_STATEMENT_BYTES {
            return Err(StatementError::TooLarge);
        }
        if !creche_wire::strict(bytes) {
            return Err(StatementError::NotJson);
        }

        let wire: WireStatement =
            serde_json::from_slice(bytes).map_err(|_| StatementError::Shape)?;
        // Serde checked the version, the event and the ref. Each has one
        // value, so the pattern cannot fail.
        let WireStatement {
            statement: StatementVersion,
            repository,
            workflow,
            event: Event::Push,
            ref_: Branch::Main,
            commit,
            run,
            signed_at,
            tags,
        } = wire;
        let statement = Self::new(repository, workflow, commit, run, signed_at, tags);
        if statement.canonical_bytes() != bytes {
            return Err(StatementError::NotCanonical);
        }
        Ok(statement)
    }

    /// The repository of the run.
    #[must_use]
    pub fn repository(&self) -> &RepoFullName {
        &self.repository
    }

    /// The workflow file of the run.
    #[must_use]
    pub fn workflow(&self) -> &WorkflowFile {
        &self.workflow
    }

    /// The event: always a push.
    #[must_use]
    pub fn event(&self) -> Event {
        Event::Push
    }

    /// The ref: always `main`.
    #[must_use]
    pub fn branch(&self) -> Branch {
        Branch::Main
    }

    /// The commit of the run.
    #[must_use]
    pub fn commit(&self) -> &CommitSha {
        &self.commit
    }

    /// The run id.
    #[must_use]
    pub fn run(&self) -> RunId {
        self.run
    }

    /// When the signer built the statement.
    #[must_use]
    pub fn signed_at(&self) -> SignedAt {
        self.signed_at
    }

    /// The tags with their assets.
    #[must_use]
    pub fn tags(&self) -> &Tags {
        &self.tags
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) const COMMIT: &str = "5791a8cdae55087207f12ab919c5eb964494d654";
    pub(crate) const DIGEST: &str =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    /// The example of section 4.5 with the hex filled in.
    pub(crate) fn example() -> Statement {
        let vmm = TagEntry {
            tag: "vmm-v0.1.24".parse().unwrap(),
            assets: Assets::new(vec![Asset {
                name: "vmm-x86_64-unknown-linux-musl.tar.gz".parse().unwrap(),
                sha256: DIGEST.parse().unwrap(),
            }])
            .unwrap(),
        };
        let handover = TagEntry {
            tag: "handover-v0.1.96".parse().unwrap(),
            assets: Assets::new(vec![]).unwrap(),
        };

        Statement::new(
            "example-owner/creche".parse().unwrap(),
            "release.yml".parse().unwrap(),
            COMMIT.parse().unwrap(),
            RunId::try_from(1234).unwrap(),
            "2026-11-15T12:00:00Z".parse().unwrap(),
            Tags::new(vec![vmm, handover]).unwrap(),
        )
    }

    const EXAMPLE_BYTES: &str = concat!(
        r#"{"statement":1,"repository":"example-owner/creche","workflow":"release.yml","#,
        r#""event":"push","ref":"main","commit":"5791a8cdae55087207f12ab919c5eb964494d654","#,
        r#""run":1234,"signed_at":"2026-11-15T12:00:00Z","tags":["#,
        r#"{"tag":"handover-v0.1.96","assets":[]},"#,
        r#"{"tag":"vmm-v0.1.24","assets":[{"name":"vmm-x86_64-unknown-linux-musl.tar.gz","#,
        r#""sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]}]}"#,
    );

    #[test]
    fn example_bytes() {
        let statement = example();
        assert_eq!(
            String::from_utf8(statement.canonical_bytes()).unwrap(),
            EXAMPLE_BYTES
        );
        assert_eq!(
            Statement::from_canonical(EXAMPLE_BYTES.as_bytes()).unwrap(),
            statement
        );
    }

    /// Each byte that a grammar of a statement string allows needs no JSON
    /// escape, so `write_string` writes valid JSON.
    #[test]
    fn grammar_bytes_need_no_escape() {
        let allowed = (b'0'..=b'9')
            .chain(b'A'..=b'Z')
            .chain(b'a'..=b'z')
            .chain(*b"._+-:/");
        for byte in allowed {
            let text = char::from(byte).to_string();
            assert_eq!(serde_json::to_string(&text).unwrap(), format!("\"{text}\""));
        }
    }

    fn refuse(text: &str) -> StatementError {
        Statement::from_canonical(text.as_bytes()).unwrap_err()
    }

    #[test]
    fn refusals() {
        let swap = |from: &str, to: &str| EXAMPLE_BYTES.replacen(from, to, 1);

        assert_eq!(
            refuse(&format!("{EXAMPLE_BYTES}\n")),
            StatementError::NotCanonical
        );
        assert_eq!(
            refuse(&format!("{EXAMPLE_BYTES}x")),
            StatementError::NotJson
        );
        assert_eq!(
            refuse(&swap(
                "{\"statement\":1,",
                "{\"statement\":1,\"statement\":1,"
            )),
            StatementError::NotJson
        );
        assert_eq!(
            refuse(&swap("\"statement\":1", "\"statement\":2")),
            StatementError::Shape
        );
        assert_eq!(
            refuse(&swap("\"ref\":\"main\"", "\"ref\":\"refs/heads/main\"")),
            StatementError::Shape
        );
        assert_eq!(
            refuse(&swap("\"push\"", "\"pull_request\"")),
            StatementError::Shape
        );
        assert_eq!(
            refuse(&swap("\"run\":1234,", "\"run\":1234,\"extra\":0,")),
            StatementError::Shape
        );
        assert_eq!(refuse(&swap("\"run\":1234,", "")), StatementError::Shape);
        assert_eq!(
            refuse(&swap("\"run\":1234", "\"run\":0")),
            StatementError::Shape
        );
        assert_eq!(
            refuse(&swap("\"run\":1234", "\"run\":1234.0")),
            StatementError::Shape
        );
        assert_eq!(
            refuse(&swap(",\"tags\":[", ", \"tags\":[")),
            StatementError::NotCanonical
        );
        assert_eq!(
            refuse(&swap(
                "\"workflow\":\"release.yml\",\"event\":\"push\"",
                "\"event\":\"push\",\"workflow\":\"release.yml\""
            )),
            StatementError::NotCanonical
        );

        let unsorted = swap("{\"tag\":\"handover-v0.1.96\",\"assets\":[]},", "").replacen(
            "]}]}",
            "]},{\"tag\":\"handover-v0.1.96\",\"assets\":[]}]}",
            1,
        );
        assert_eq!(refuse(&unsorted), StatementError::NotCanonical);

        let (head, _) = EXAMPLE_BYTES.split_once("\"tags\":[").unwrap();
        let no_tags = format!("{head}\"tags\":[]}}");
        assert_eq!(refuse(&no_tags), StatementError::Shape);

        assert_eq!(
            refuse(&" ".repeat(WORST_STATEMENT_BYTES + 1)),
            StatementError::TooLarge
        );
    }

    fn entry(tag: String, assets: usize) -> TagEntry {
        let assets = (0..assets)
            .map(|n| Asset {
                name: format!("asset-{n:02}").parse().unwrap(),
                sha256: DIGEST.parse().unwrap(),
            })
            .collect();
        TagEntry {
            tag: tag.parse().unwrap(),
            assets: Assets::new(assets).unwrap(),
        }
    }

    #[test]
    fn list_limits() {
        let tags = |n: usize| {
            (0..n)
                .map(|i| entry(format!("c{i:02}-v1.0.0"), 0))
                .collect::<Vec<_>>()
        };
        assert!(Tags::new(tags(32)).is_ok());
        assert_eq!(Tags::new(tags(33)), Err(LimitError::TooManyTags));
        assert_eq!(Tags::new(vec![]), Err(LimitError::NoTag));
        assert_eq!(
            Tags::new(vec![
                entry("vmm-v1.0.0".into(), 0),
                entry("vmm-v1.0.0".into(), 1)
            ]),
            Err(LimitError::DuplicateTag)
        );
        assert_eq!(
            Tags::new(vec![
                entry("vmm-v0.1.24".into(), 0),
                entry("vmm-v0.1.25".into(), 0)
            ]),
            Err(LimitError::DuplicateComponent)
        );
        // `vmm` and `vmm-x` are two components.
        assert!(
            Tags::new(vec![
                entry("vmm-v0.1.24".into(), 0),
                entry("vmm-x-v0.1.24".into(), 0)
            ])
            .is_ok()
        );

        let asset = |name: &str| Asset {
            name: name.parse().unwrap(),
            sha256: DIGEST.parse().unwrap(),
        };
        assert!(Assets::new((0..16).map(|n| asset(&format!("a{n}"))).collect()).is_ok());
        assert_eq!(
            Assets::new((0..17).map(|n| asset(&format!("a{n}"))).collect()),
            Err(LimitError::TooManyAssets)
        );
        assert_eq!(
            Assets::new(vec![asset("a"), asset("a")]),
            Err(LimitError::DuplicateAsset)
        );
    }
}
