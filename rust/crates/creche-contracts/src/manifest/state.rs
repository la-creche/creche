//! The live-state document (contract 06 §11): what runs now, and the source
//! facts of a tag.
//!
//! Root builds the document. This module is the form and the parser. A parser
//! is necessary because `handover resolve --state <file>` gives the resolver
//! a document that a test or a person wrote. Such a document is input, so the
//! parser reads it as input: a size cap, a closed key set, a check for each
//! scalar, and a refusal that names the field and does not show its value.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

use super::json::{self, Object, Value};
use super::{ContractId, Digest, RefusalCode, SafeToken, catalog_row};
use crate::ids::{ComponentName, ContractVersion, GitObjectId, Version};

/// The largest live-state document, in bytes. The parser applies the cap
/// before the parse.
pub const MAX_STATE_BYTES: usize = 64 * 1024;

const STATE_FIELDS: [&str; 4] = ["live", "provided", "latest", "facts"];
const FACT_FIELDS: [&str; 3] = ["sha", "input_digest", "artifact_digest"];

/// What the resolver reads for an absent key: an empty object.
static EMPTY_OBJECT: Value = Value::Object(Object::EMPTY);

/// What a tag resolves to: the source columns of one component in contract 06
/// §9.
///
/// Each of the three is `None` for a component that root resolved no tag for.
///
/// ```
/// use creche_contracts::manifest::SourceFacts;
///
/// let sha = "9d1f0c7a5b2e4438a6c0d19f37be5a2c48e1067b".parse()?;
/// let facts = SourceFacts::new(Some(sha), None, None);
/// assert!(facts.sha().is_some());
/// assert!(facts.input_digest().is_none());
/// # Ok::<(), creche_contracts::ids::GitObjectIdError>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::SourceFacts;
///
/// fn clear(mut facts: SourceFacts) {
///     facts.sha = None;
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SourceFacts {
    sha: Option<GitObjectId>,
    input_digest: Option<Digest>,
    artifact_digest: Option<Digest>,
}

impl SourceFacts {
    /// The facts of one component.
    #[must_use]
    pub const fn new(
        sha: Option<GitObjectId>,
        input_digest: Option<Digest>,
        artifact_digest: Option<Digest>,
    ) -> Self {
        Self {
            sha,
            input_digest,
            artifact_digest,
        }
    }

    /// The commit that the tag names.
    #[must_use]
    pub fn sha(&self) -> Option<&GitObjectId> {
        self.sha.as_ref()
    }

    /// The digest over the source of the component at that commit.
    #[must_use]
    pub fn input_digest(&self) -> Option<&Digest> {
        self.input_digest.as_ref()
    }

    /// The digest of the image in an OCI registry.
    #[must_use]
    pub fn artifact_digest(&self) -> Option<&Digest> {
        self.artifact_digest.as_ref()
    }
}

/// One of the three maps of a live-state document that have a component name
/// as key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateMap {
    /// `live`.
    Live,
    /// `latest`.
    Latest,
    /// `facts`.
    Facts,
}

impl StateMap {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::Latest => "latest",
            Self::Facts => "facts",
        }
    }
}

/// One parsed live-state document: each thing that the resolver needs and
/// cannot compute (contract 06 §11).
///
/// Each key of `live`, `latest` and `facts` is a component of the catalog.
///
/// ```
/// use creche_contracts::manifest::{ContractId, ReleaseState};
///
/// let text = r#"{"live": {"chaperone": "2.0.3", "attendance": null},
///                "provided": {"pep-grant": "2.0"}}"#;
/// let state = ReleaseState::parse(text)?;
/// assert_eq!(state.live().len(), 2);
/// let provided = state.provided().get(&ContractId::PepGrant).ok_or("no version")?;
/// assert_eq!(provided.as_str(), "2.0");
/// assert!(state.latest().is_empty());
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::ReleaseState;
///
/// fn clear(mut state: ReleaseState) {
///     state.live.clear();
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ReleaseState {
    live: BTreeMap<ComponentName, Option<Version>>,
    provided: BTreeMap<ContractId, ContractVersion>,
    latest: BTreeMap<ComponentName, Version>,
    facts: BTreeMap<ComponentName, SourceFacts>,
}

impl ReleaseState {
    /// The version of each component that is installed now. `None` is a
    /// component with no install.
    #[must_use]
    pub const fn live(&self) -> &BTreeMap<ComponentName, Option<Version>> {
        &self.live
    }

    /// The contract version that the live provider of each contract provides.
    /// Rule C4 reads it.
    #[must_use]
    pub const fn provided(&self) -> &BTreeMap<ContractId, ContractVersion> {
        &self.provided
    }

    /// The newest released version of each component that the request names.
    #[must_use]
    pub const fn latest(&self) -> &BTreeMap<ComponentName, Version> {
        &self.latest
    }

    /// The source facts of each component at the version that it resolves to.
    #[must_use]
    pub const fn facts(&self) -> &BTreeMap<ComponentName, SourceFacts> {
        &self.facts
    }

    /// Parses the text of one live-state document. Each key is optional.
    ///
    /// The failure action of a caller: refuse with the code `state` and the
    /// detail, and resolve nothing.
    ///
    /// # Errors
    ///
    /// Gives the first [`StateError`], in the order of the checks of the
    /// Python parser.
    pub fn parse(text: &str) -> Result<Self, StateError> {
        if text.len() > MAX_STATE_BYTES {
            return Err(StateError::TooLarge);
        }

        let loaded = json::parse(text).map_err(|_| StateError::NotJson)?;
        let Value::Object(body) = &loaded else {
            return Err(StateError::NotObject(StateLabel::Top));
        };
        if let Some(unknown) = body.sorted_keys().find(|key| !STATE_FIELDS.contains(key)) {
            return Err(StateError::UnknownField(SafeToken::of(unknown)));
        }

        let field = |key: &str| body.get(key).unwrap_or(&EMPTY_OBJECT);
        let mut state = Self::default();
        for (name, item) in named(field("live"), StateMap::Live)? {
            let (name, row) = name?;
            let version = match item {
                Value::Null => None,
                _ => Some(version(item, StateMap::Live, row)?),
            };
            state.live.insert(name, version);
        }

        let Value::Object(provided) = field("provided") else {
            return Err(StateError::NotObject(StateLabel::Provided));
        };
        for (name, item) in provided.iter() {
            let contract: ContractId = name
                .parse()
                .map_err(|_| StateError::UnknownContract(SafeToken::of(name)))?;
            let Value::Text(text) = item else {
                return Err(StateError::ContractVersion(contract));
            };
            let version = text
                .parse()
                .map_err(|_| StateError::ContractVersion(contract))?;
            state.provided.insert(contract, version);
        }

        for (name, item) in named(field("latest"), StateMap::Latest)? {
            let (name, row) = name?;
            state
                .latest
                .insert(name, version(item, StateMap::Latest, row)?);
        }

        for (name, item) in named(field("facts"), StateMap::Facts)? {
            let (name, row) = name?;
            state.facts.insert(name, read_fact(item, row)?);
        }

        Ok(state)
    }
}

/// A component name of a live-state document, and its name in the catalog.
type Named = Result<(ComponentName, &'static str), StateError>;

/// The entries of one map whose keys are component names. The key of each
/// entry is checked when the caller reads the entry, so the first refusal is
/// the first one in the order of the text.
fn named(
    value: &Value,
    map: StateMap,
) -> Result<impl Iterator<Item = (Named, &Value)>, StateError> {
    let Value::Object(entries) = value else {
        return Err(StateError::NotObject(StateLabel::Map(map)));
    };

    Ok(entries.iter().map(move |(name, item)| {
        let known = catalog_row(name)
            .and_then(|row| Some((name.parse().ok()?, row.name())))
            .ok_or_else(|| StateError::UnknownName {
                map,
                name: SafeToken::of(name),
            });

        (known, item)
    }))
}

fn version(value: &Value, map: StateMap, name: &'static str) -> Result<Version, StateError> {
    match value {
        Value::Text(text) => text.parse().ok(),
        _ => None,
    }
    .ok_or(StateError::Version { map, name })
}

fn read_fact(value: &Value, name: &'static str) -> Result<SourceFacts, StateError> {
    let Value::Object(body) = value else {
        return Err(StateError::NotObject(StateLabel::Fact(name)));
    };
    if let Some(unknown) = body.sorted_keys().find(|key| !FACT_FIELDS.contains(key)) {
        return Err(StateError::FactField {
            name,
            field: SafeToken::of(unknown),
        });
    }

    let [sha, input_digest, artifact_digest] = FACT_FIELDS;

    Ok(SourceFacts {
        sha: optional(body.get(sha)).map_err(|()| StateError::Sha(name))?,
        input_digest: optional(body.get(input_digest))
            .map_err(|()| StateError::InputDigest(name))?,
        artifact_digest: optional(body.get(artifact_digest))
            .map_err(|()| StateError::ArtifactDigest(name))?,
    })
}

/// A value that is absent, `null` or a text of the form of `T`.
fn optional<T: std::str::FromStr>(value: Option<&Value>) -> Result<Option<T>, ()> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Text(text)) => text.parse().map(Some).map_err(|_| ()),
        Some(_) => Err(()),
    }
}

/// A place in a live-state document that must be an object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateLabel {
    /// The document.
    Top,
    /// `provided`.
    Provided,
    /// `live`, `latest` or `facts`.
    Map(StateMap),
    /// The facts of this component.
    Fact(&'static str),
}

impl fmt::Display for StateLabel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Top => f.write_str("<top level>"),
            Self::Provided => f.write_str("provided"),
            Self::Map(map) => f.write_str(map.as_str()),
            Self::Fact(name) => write!(f, "facts.{name}"),
        }
    }
}

/// Why a text is not a live-state document.
///
/// A variant holds the name of a component only when the catalog holds that
/// name. Each other text of the input is a [`SafeToken`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StateError {
    /// The text is larger than [`MAX_STATE_BYTES`].
    TooLarge,
    /// The text is not JSON that Python reads.
    NotJson,
    /// This place is not an object.
    NotObject(StateLabel),
    /// The document has a key that the schema does not name.
    UnknownField(SafeToken),
    /// A map names a component that the catalog does not list.
    UnknownName {
        /// The map.
        map: StateMap,
        /// The name, when it is safe to show.
        name: SafeToken,
    },
    /// The version of this component is not `MAJOR.MINOR.PATCH`.
    Version {
        /// The map: `live` or `latest`.
        map: StateMap,
        /// The component.
        name: &'static str,
    },
    /// `provided` names a contract that contract 06 §3 does not list.
    UnknownContract(SafeToken),
    /// The version of this contract is not `MAJOR.MINOR`.
    ContractVersion(ContractId),
    /// The facts of this component have a key that the schema does not name.
    FactField {
        /// The component.
        name: &'static str,
        /// The key, when it is safe to show.
        field: SafeToken,
    },
    /// The `sha` of this component is not 40 lower-case hex bytes or `null`.
    Sha(&'static str),
    /// The `input_digest` of this component is not a digest or `null`.
    InputDigest(&'static str),
    /// The `artifact_digest` of this component is not a digest or `null`.
    ArtifactDigest(&'static str),
}

impl StateError {
    /// The check that the ledger names: `state`.
    #[must_use]
    pub const fn code(&self) -> RefusalCode {
        RefusalCode::State
    }

    /// The detail of the refusal, as the Python parser writes it.
    #[must_use]
    pub fn detail(&self) -> String {
        self.to_string()
    }
}

impl fmt::Display for StateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        const DIGEST_FORM: &str = "must be sha256:<64 hex> or null";
        match self {
            Self::TooLarge => write!(f, "larger than {MAX_STATE_BYTES} bytes"),
            Self::NotJson => f.write_str("does not parse as JSON"),
            Self::NotObject(label) => write!(f, "'{label}' must be an object"),
            Self::UnknownField(token) => write!(f, "unknown field: {token}"),
            Self::UnknownName { map, name } => write!(
                f,
                "'{}' names {name}, which contract 06 §1 omits",
                map.as_str()
            ),
            Self::Version { map, name } => {
                write!(f, "'{}.{name}' must be MAJOR.MINOR.PATCH", map.as_str())
            }
            Self::UnknownContract(token) => {
                write!(f, "'provided' names {token}, which is no contract id")
            }
            Self::ContractVersion(contract) => {
                write!(f, "'provided.{contract}' must be MAJOR.MINOR")
            }
            Self::FactField { name, field } => {
                write!(f, "'facts.{name}' has an unknown field: {field}")
            }
            Self::Sha(name) => write!(f, "'facts.{name}.sha' must be 40 lower-case hex or null"),
            Self::InputDigest(name) => write!(f, "'facts.{name}.input_digest' {DIGEST_FORM}"),
            Self::ArtifactDigest(name) => {
                write!(f, "'facts.{name}.artifact_digest' {DIGEST_FORM}")
            }
        }
    }
}

impl Error for StateError {}
