//! The resolved manifest (contract 06 §9), its hash, the approval gate and the
//! approval summary (`stage7-releases.md` §2.5).
//!
//! Root builds the resolved manifest and hashes it. The approval of the
//! operator binds to the hash, so the bytes under the hash are fixed here:
//! canonical JSON with sorted keys, no space, and each list in the order that
//! the resolver gives.

use std::error::Error;
use std::fmt;

use creche_util::{hex, sha256};

use super::json::{self, Charset};
use super::state::SourceFacts;
use super::{
    Action, ContractId, ContractNumber, Digest, MANIFEST_CONTRACT_MAJOR, MANIFEST_CONTRACT_MINOR,
    Requester,
};
use crate::ids::{ComponentName, GateId, GateIdError, GitObjectId, Ulid, Version};

/// The `component-manifest` contract version that root writes into each
/// resolved manifest.
pub const MANIFEST_VERSION: &str = "0.6";

/// What the hash of a manifest is joined with before the hash of the gate, so
/// that the gate id of a release is never the gate id of a tool call.
const GATE_PURPOSE: &str = "release";

/// The count of hex bytes of a gate id.
const GATE_ID_HEX: usize = 16;

/// What the approval flow puts before an approval.
const APPROVE_PREFIX: &str = "AGENT_APPROVE";

/// The largest count of characters of one field of a summary.
const SUMMARY_FIELD_MAX: usize = 120;

/// What ends a field that the summary cut.
const CUT_MARK: char = '\u{2026}';

// CONTRACT-QUESTION: contract 06 §9 calls `resolved_at` a number. The Python
// builder takes each float and writes `NaN` and `Infinity`, which JSON does
// not have. This type refuses a float that is not finite. It takes a time
// below zero, as the Python builder does. A stricter type costs one more
// check in the constructor.
/// When root resolved a release: Unix seconds of its clock, as a finite
/// float (contract 06 §9, the field `resolved_at`).
///
/// ```
/// use creche_contracts::manifest::ResolvedAt;
///
/// let time = ResolvedAt::new(1758153600.0)?;
/// assert_eq!(time.get(), 1758153600.0);
/// assert!(ResolvedAt::new(f64::INFINITY).is_err());
/// # Ok::<(), creche_contracts::manifest::ResolvedAtError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::manifest::ResolvedAt;
///
/// let time = ResolvedAt(f64::NAN);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
pub struct ResolvedAt(f64);

impl ResolvedAt {
    /// Checks that `seconds` is finite. JSON has no form for another float.
    ///
    /// # Errors
    ///
    /// Gives [`ResolvedAtError`] for NaN and for an infinity.
    pub fn new(seconds: f64) -> Result<Self, ResolvedAtError> {
        if !seconds.is_finite() {
            return Err(ResolvedAtError);
        }

        Ok(Self(seconds))
    }

    /// The time in seconds.
    #[must_use]
    pub const fn get(self) -> f64 {
        self.0
    }
}

/// A time that is not finite.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResolvedAtError;

impl fmt::Display for ResolvedAtError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the time of a resolution is a finite count of seconds")
    }
}

impl Error for ResolvedAtError {}

/// One row of the `components` list of a resolved manifest (contract 06 §9).
///
/// ```
/// use creche_contracts::manifest::{Action, ResolvedComponent, SourceFacts};
///
/// let row = ResolvedComponent::new(
///     "chaperone".parse()?,
///     Action::Deploy,
///     Some("2.0.3".parse()?),
///     Some("2.1.0".parse()?),
///     SourceFacts::default(),
/// );
/// assert_eq!(row.tag().as_deref(), Some("chaperone-v2.1.0"));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::{Action, ResolvedComponent};
///
/// fn change(mut row: ResolvedComponent) {
///     row.action = Action::Unchanged;
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedComponent {
    name: ComponentName,
    action: Action,
    from_version: Option<Version>,
    to_version: Option<Version>,
    facts: SourceFacts,
}

impl ResolvedComponent {
    /// One row. The resolver gives each part.
    #[must_use]
    pub const fn new(
        name: ComponentName,
        action: Action,
        from_version: Option<Version>,
        to_version: Option<Version>,
        facts: SourceFacts,
    ) -> Self {
        Self {
            name,
            action,
            from_version,
            to_version,
            facts,
        }
    }

    /// The component.
    #[must_use]
    pub const fn name(&self) -> &ComponentName {
        &self.name
    }

    /// What the release does with the component.
    #[must_use]
    pub const fn action(&self) -> Action {
        self.action
    }

    /// The version that is live now. `None` on a first install.
    #[must_use]
    pub const fn from_version(&self) -> Option<&Version> {
        self.from_version.as_ref()
    }

    /// The version that is live after the release. `None` for a data
    /// component.
    #[must_use]
    pub const fn to_version(&self) -> Option<&Version> {
        self.to_version.as_ref()
    }

    /// The source facts of the component.
    #[must_use]
    pub const fn facts(&self) -> &SourceFacts {
        &self.facts
    }

    /// The tag of the version after the release: `<name>-v<version>`. `None`
    /// for a component with no version.
    #[must_use]
    pub fn tag(&self) -> Option<String> {
        self.to_version
            .as_ref()
            .map(|version| format!("{}-v{version}", self.name))
    }
}

/// One consumer of a contract: the component and the floor that it requires
/// (contract 06 §9, `contracts[].consumers`).
///
/// ```
/// use creche_contracts::manifest::{Consumer, ContractNumber};
///
/// let consumer = Consumer::new("attendance".parse()?, ContractNumber::new(2)?, ContractNumber::new(0)?);
/// assert_eq!(consumer.name().as_str(), "attendance");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::{Consumer, ContractNumber};
///
/// fn change(mut consumer: Consumer) {
///     consumer.major = ContractNumber::new(0).unwrap();
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Consumer {
    name: ComponentName,
    major: ContractNumber,
    min_minor: ContractNumber,
}

impl Consumer {
    /// One consumer.
    #[must_use]
    pub const fn new(
        name: ComponentName,
        major: ContractNumber,
        min_minor: ContractNumber,
    ) -> Self {
        Self {
            name,
            major,
            min_minor,
        }
    }

    /// The component that requires the contract.
    #[must_use]
    pub const fn name(&self) -> &ComponentName {
        &self.name
    }

    /// The major version that it requires.
    #[must_use]
    pub const fn major(&self) -> ContractNumber {
        self.major
    }

    /// The oldest minor version that it can call.
    #[must_use]
    pub const fn min_minor(&self) -> ContractNumber {
        self.min_minor
    }
}

/// One row of the `contracts` table of a resolved manifest (contract 06 §9).
///
/// ```
/// use creche_contracts::manifest::{ContractId, ContractNumber, ContractRow};
///
/// let row = ContractRow::new(
///     ContractId::PepGrant,
///     "chaperone".parse()?,
///     ContractNumber::new(2)?,
///     ContractNumber::new(1)?,
///     Vec::new(),
/// );
/// assert_eq!(row.version(), "2.1");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::ContractRow;
///
/// fn change(mut row: ContractRow) {
///     row.consumers.clear();
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractRow {
    contract: ContractId,
    provider: ComponentName,
    major: ContractNumber,
    minor: ContractNumber,
    consumers: Vec<Consumer>,
}

impl ContractRow {
    /// One row. The resolver gives each part, and the consumers in the order
    /// of their names.
    #[must_use]
    pub const fn new(
        contract: ContractId,
        provider: ComponentName,
        major: ContractNumber,
        minor: ContractNumber,
        consumers: Vec<Consumer>,
    ) -> Self {
        Self {
            contract,
            provider,
            major,
            minor,
            consumers,
        }
    }

    /// The contract.
    #[must_use]
    pub const fn contract(&self) -> ContractId {
        self.contract
    }

    /// The component that provides the contract in this set.
    #[must_use]
    pub const fn provider(&self) -> &ComponentName {
        &self.provider
    }

    /// The version that the provider provides: `MAJOR.MINOR`.
    #[must_use]
    pub fn version(&self) -> String {
        format!("{}.{}", self.major, self.minor)
    }

    /// Each component that requires the contract.
    #[must_use]
    pub fn consumers(&self) -> &[Consumer] {
        &self.consumers
    }
}

/// The resolved manifest of one release (contract 06 §9).
///
/// Root builds it from the resolution and from its own clock. The type holds
/// each field but the hash, and gives the hash from the fields. A value thus
/// never holds a hash of other fields.
///
/// ```
/// use creche_contracts::manifest::{ResolvedAt, ResolvedManifest};
///
/// let manifest = ResolvedManifest::new(
///     "01K5J8M2Q7V3X9R4T6N0B8C2DE".parse()?,
///     ResolvedAt::new(1758153600.0)?,
///     "human".parse()?,
///     Vec::new(),
///     Vec::new(),
///     Vec::new(),
/// );
/// assert_eq!(
///     manifest.canonical_json(),
///     r#"{"components":[],"contracts":[],"id":"01K5J8M2Q7V3X9R4T6N0B8C2DE","manifest_version":"0.6","order":[],"requested_by":"human","resolved_at":1758153600.0}"#
/// );
/// assert!(manifest.sha256().as_str().starts_with("sha256:"));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change a field of a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::ResolvedManifest;
///
/// fn change(mut manifest: ResolvedManifest) {
///     manifest.order.clear();
/// }
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvedManifest {
    id: Ulid,
    resolved_at: ResolvedAt,
    requested_by: Requester,
    components: Vec<ResolvedComponent>,
    order: Vec<ComponentName>,
    contracts: Vec<ContractRow>,
}

impl ResolvedManifest {
    /// The resolved manifest of one request. The resolver gives the
    /// components in the order of their names, the deploy order, and the
    /// contracts in the order of their ids.
    #[must_use]
    pub const fn new(
        id: Ulid,
        resolved_at: ResolvedAt,
        requested_by: Requester,
        components: Vec<ResolvedComponent>,
        order: Vec<ComponentName>,
        contracts: Vec<ContractRow>,
    ) -> Self {
        Self {
            id,
            resolved_at,
            requested_by,
            components,
            order,
            contracts,
        }
    }

    /// The id of the release request.
    #[must_use]
    pub const fn id(&self) -> &Ulid {
        &self.id
    }

    /// When root resolved the release.
    #[must_use]
    pub const fn resolved_at(&self) -> ResolvedAt {
        self.resolved_at
    }

    /// Who asked. Nothing decides on it.
    #[must_use]
    pub const fn requested_by(&self) -> &Requester {
        &self.requested_by
    }

    /// One row for each component.
    #[must_use]
    pub fn components(&self) -> &[ResolvedComponent] {
        &self.components
    }

    /// The deploy order.
    #[must_use]
    pub fn order(&self) -> &[ComponentName] {
        &self.order
    }

    /// The result of the contract check, one row for each contract.
    #[must_use]
    pub fn contracts(&self) -> &[ContractRow] {
        &self.contracts
    }

    /// The text that the hash is over: each field but the hash, as JSON with
    /// sorted keys and no space. Python `json.dumps` writes the same bytes
    /// with `sort_keys=True`, `separators=(",", ":")` and
    /// `ensure_ascii=False`.
    #[must_use]
    pub fn canonical_json(&self) -> String {
        let mut out = String::from("{");
        key(&mut out, "components");
        list(&mut out, &self.components, push_component);
        out.push(',');
        key(&mut out, "contracts");
        list(&mut out, &self.contracts, push_contract);
        out.push(',');
        key(&mut out, "id");
        text(&mut out, self.id.as_str());
        out.push(',');
        key(&mut out, "manifest_version");
        text(&mut out, MANIFEST_VERSION);
        out.push(',');
        key(&mut out, "order");
        list(&mut out, &self.order, |out, name| text(out, name.as_str()));
        out.push(',');
        key(&mut out, "requested_by");
        text(&mut out, self.requested_by.as_str());
        out.push(',');
        key(&mut out, "resolved_at");
        out.push_str(&json::float_text(self.resolved_at.get()));
        out.push('}');

        out
    }

    /// The hash that the approval binds to: `sha256:` and the SHA-256 of the
    /// UTF-8 bytes of [`ResolvedManifest::canonical_json`].
    #[must_use]
    pub fn sha256(&self) -> Digest {
        Digest::of(self.canonical_json().as_bytes())
    }
}

fn key(out: &mut String, name: &str) {
    text(out, name);
    out.push(':');
}

fn text(out: &mut String, value: &str) {
    json::push_string(out, value, Charset::Unicode);
}

fn optional<T>(out: &mut String, value: Option<&T>, as_str: fn(&T) -> &str) {
    match value {
        Some(value) => text(out, as_str(value)),
        None => out.push_str("null"),
    }
}

fn list<T>(out: &mut String, items: &[T], push: fn(&mut String, &T)) {
    out.push('[');
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }

        push(out, item);
    }

    out.push(']');
}

fn push_component(out: &mut String, row: &ResolvedComponent) {
    out.push('{');
    key(out, "action");
    text(out, row.action.as_str());
    out.push(',');
    key(out, "artifact_digest");
    optional(out, row.facts.artifact_digest(), Digest::as_str);
    out.push(',');
    key(out, "from_version");
    optional(out, row.from_version.as_ref(), Version::as_str);
    out.push(',');
    key(out, "input_digest");
    optional(out, row.facts.input_digest(), Digest::as_str);
    out.push(',');
    key(out, "name");
    text(out, row.name.as_str());
    out.push(',');
    key(out, "sha");
    optional(out, row.facts.sha(), GitObjectId::as_str);
    out.push(',');
    key(out, "tag");
    match row.tag() {
        Some(tag) => text(out, &tag),
        None => out.push_str("null"),
    }

    out.push(',');
    key(out, "to_version");
    optional(out, row.to_version.as_ref(), Version::as_str);
    out.push('}');
}

fn push_consumer(out: &mut String, consumer: &Consumer) {
    out.push('{');
    key(out, "major");
    out.push_str(&consumer.major.to_string());
    out.push(',');
    key(out, "min_minor");
    out.push_str(&consumer.min_minor.to_string());
    out.push(',');
    key(out, "name");
    text(out, consumer.name.as_str());
    out.push('}');
}

fn push_contract(out: &mut String, row: &ContractRow) {
    out.push('{');
    key(out, "consumers");
    list(out, &row.consumers, push_consumer);
    out.push(',');
    key(out, "contract");
    text(out, row.contract.as_str());
    out.push(',');
    key(out, "provider");
    text(out, row.provider.as_str());
    out.push(',');
    key(out, "version");
    text(out, &row.version());
    out.push('}');
}

// --- the approval gate ---

/// The id of the approval gate of one release: the first 16 hex bytes of the
/// SHA-256 of the manifest hash and the word `release`
/// (`stage7-releases.md` §2.5).
///
/// The hash of a manifest holds the request id and the time of the
/// resolution, so two requests never share a gate.
///
/// # Errors
///
/// The error does not occur: 16 lower-case hex bytes are a gate id. The
/// signature keeps the conversion free of a panic. A caller that gets an
/// error denies the release.
pub fn gate_id(manifest_sha256: &Digest) -> Result<GateId, GateIdError> {
    let joined = format!("{manifest_sha256}{GATE_PURPOSE}");
    let digest = hex::lower(&sha256::digest(joined.as_bytes()));

    digest.get(..GATE_ID_HEX).unwrap_or(&digest).parse()
}

/// The request id in the form that the approval flow takes: lower case.
///
/// Each ULID of the platform is upper-case Crockford base32. The pattern of
/// the flow is lower case, so the id changes case here and in no other place.
#[must_use]
pub fn action_id(id: &Ulid) -> String {
    id.as_str().to_ascii_lowercase()
}

/// The action text that root checks against the pattern of the approval flow
/// before it sends the request for approval:
/// `AGENT_APPROVE_<action id>_<gate>`.
#[must_use]
pub fn release_action(id: &Ulid, gate: &GateId) -> String {
    format!("{APPROVE_PREFIX}_{}_{gate}", action_id(id))
}

// --- the approval summary ---

/// The seven fields of an approval summary, as root writes them before the
/// cut.
///
/// Code builds a value by hand: [`SummaryFields::new`] takes each field, in
/// the order that the operator reads them. [`Summary::new`] makes the
/// summary.
///
/// ```
/// use creche_contracts::manifest::{Summary, SummaryFields};
///
/// let fields = SummaryFields::new(
///     String::from("safe: each manifest verified"),
///     String::from("chaperone 2.0.3 to 2.1.0"),
///     String::from("6 contracts satisfied"),
///     String::from("creche-chaperone.service"),
///     String::from("automatic"),
///     String::from("human"),
///     String::from("0123456789ab"),
/// );
/// let summary = Summary::new(fields);
/// assert_eq!(summary.review(), "safe: each manifest verified");
/// assert_eq!(summary.restore(), "automatic");
/// assert_eq!(summary.manifest(), "0123456789ab");
/// ```
///
/// Code outside this module cannot name a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::manifest::{Summary, SummaryFields};
///
/// let fields = SummaryFields::new(
///     String::from("safe: each manifest verified"),
///     String::from("chaperone 2.0.3 to 2.1.0"),
///     String::from("6 contracts satisfied"),
///     String::from("creche-chaperone.service"),
///     String::from("automatic"),
///     String::from("human"),
///     String::from("0123456789ab"),
/// );
/// let fields = SummaryFields {
///     review: String::from("suspect: one manifest changed"),
///     ..fields
/// };
/// let summary = Summary::new(fields);
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SummaryFields {
    review: String,
    components: String,
    contracts: String,
    restarts: String,
    restore: String,
    requested_by: String,
    manifest: String,
}

impl SummaryFields {
    /// The fields of a summary, in the order that the operator reads them.
    ///
    /// - `review` is the verdict of the review: `safe: ...` or
    ///   `suspect: ...`. The operator reads it first.
    /// - `components` holds one line for each component that changes.
    /// - `contracts` is the result of the contract check.
    /// - `restarts` holds the units that the release restarts, in order.
    /// - `restore` is the restore rule: `automatic`, or `manual:` and the
    ///   component.
    /// - `requested_by` says who asks.
    /// - `manifest` is the start of the manifest hash: its first 12 hex
    ///   bytes.
    #[must_use]
    pub fn new(
        review: String,
        components: String,
        contracts: String,
        restarts: String,
        restore: String,
        requested_by: String,
        manifest: String,
    ) -> Self {
        Self {
            review,
            components,
            contracts,
            restarts,
            restore,
            requested_by,
            manifest,
        }
    }
}

/// The approval summary that the operator reads before the approval
/// (`stage7-releases.md` §2.5).
///
/// Each field has 120 characters at most. A longer field is cut to 119
/// characters and one ellipsis, so the operator sees that text is missing.
///
/// ```
/// use creche_contracts::manifest::{Summary, SummaryFields};
///
/// let fields = SummaryFields::new(
///     "r".repeat(121),
///     String::from("chaperone 2.0.3 to 2.1.0"),
///     String::from("6 contracts satisfied"),
///     String::from("creche-chaperone.service"),
///     String::from("automatic"),
///     String::from("human"),
///     String::from("0123456789ab"),
/// );
/// let summary = Summary::new(fields);
/// assert_eq!(summary.review().chars().count(), 120);
/// assert!(summary.review().ends_with('\u{2026}'));
/// assert_eq!(summary.restore(), "automatic");
/// ```
///
/// Code outside this module cannot put a longer field into a value:
///
/// ```compile_fail,E0616
/// use creche_contracts::manifest::{Summary, SummaryFields};
///
/// let fields = SummaryFields::new(
///     "r".repeat(121),
///     String::from("chaperone 2.0.3 to 2.1.0"),
///     String::from("6 contracts satisfied"),
///     String::from("creche-chaperone.service"),
///     String::from("automatic"),
///     String::from("human"),
///     String::from("0123456789ab"),
/// );
/// let mut summary = Summary::new(fields.clone());
/// summary.fields = fields;
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary {
    fields: SummaryFields,
}

/// One field, with 120 characters at most. A character is one code point.
fn cut(value: String) -> String {
    if value.chars().count() <= SUMMARY_FIELD_MAX {
        return value;
    }

    value
        .chars()
        .take(SUMMARY_FIELD_MAX - 1)
        .chain([CUT_MARK])
        .collect()
}

impl Summary {
    /// The summary of the fields, each one cut to its largest size.
    #[must_use]
    pub fn new(fields: SummaryFields) -> Self {
        Self {
            fields: SummaryFields {
                review: cut(fields.review),
                components: cut(fields.components),
                contracts: cut(fields.contracts),
                restarts: cut(fields.restarts),
                restore: cut(fields.restore),
                requested_by: cut(fields.requested_by),
                manifest: cut(fields.manifest),
            },
        }
    }

    /// The verdict of the review.
    #[must_use]
    pub fn review(&self) -> &str {
        &self.fields.review
    }

    /// The components that change.
    #[must_use]
    pub fn components(&self) -> &str {
        &self.fields.components
    }

    /// The result of the contract check.
    #[must_use]
    pub fn contracts(&self) -> &str {
        &self.fields.contracts
    }

    /// The units that the release restarts.
    #[must_use]
    pub fn restarts(&self) -> &str {
        &self.fields.restarts
    }

    /// The restore rule of the release.
    #[must_use]
    pub fn restore(&self) -> &str {
        &self.fields.restore
    }

    /// Who asked.
    #[must_use]
    pub fn requested_by(&self) -> &str {
        &self.fields.requested_by
    }

    /// The start of the manifest hash.
    #[must_use]
    pub fn manifest(&self) -> &str {
        &self.fields.manifest
    }

    /// Each field with its name, in the order that the operator reads them.
    #[must_use]
    pub fn fields(&self) -> [(&'static str, &str); 7] {
        [
            ("review", self.review()),
            ("components", self.components()),
            ("contracts", self.contracts()),
            ("restarts", self.restarts()),
            ("restore", self.restore()),
            ("requested_by", self.requested_by()),
            ("manifest", self.manifest()),
        ]
    }
}

// `MANIFEST_VERSION` is the two numbers of the contract version, as one text.
const _: () = assert!(MANIFEST_CONTRACT_MAJOR == 0 && MANIFEST_CONTRACT_MINOR == 6);
