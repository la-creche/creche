//! The family file: `family.yaml` and its validation report (contract 01).
//!
//! Two layers hold one file:
//!
//! 1. [`RawFamily`] holds each field as the file wrote it. Its fields are
//!    public. It proves nothing.
//! 2. [`Family`] holds a file that obeys each rule that one file can show. Its
//!    fields are private. The one way to a `Family` is `TryFrom<RawFamily>`.
//!
//! The conversion reads each field before it answers, so one call reports
//! each violation (contract 01 §7 rule 2). Each issue has the severity, the
//! location and the message of the Python validator `agent_family`.
//!
//! A rule that one field decides is the type of that field, for example
//! [`Cpus`] and [`MountPath`]. A rule that needs two fields is rule code in
//! this module. A rule that needs another file or the host is rule code in
//! the crate `agent-family`. Each [`Issue`] has a [`Slot`], so the issues of
//! the two crates sort into the one order of the Python report.
//!
//! Code outside this module cannot build a `Family` from raw fields:
//!
//! ```compile_fail,E0451
//! use creche_contracts::family::{Family, RawFamily};
//!
//! fn take(raw: RawFamily) -> Family {
//!     Family { name: raw.name }
//! }
//! ```
//!
//! The conversion is the way in:
//!
//! ```
//! use creche_contracts::family::{Family, RawFamily};
//!
//! fn take(raw: RawFamily) -> Option<Family> {
//!     Family::try_from(raw).ok()
//! }
//! ```

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::de::{self, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::ids::{FamilyName, ServerName, SkillName, ToolName, ToolNameError, WebhookName};

// --- constants of contract 01 ---

/// The one name that no family takes (contract 01 §3.1).
pub const PROBE_FAMILY: &str = "gate-probe";

/// The roots that a mount is under (contract 01 §5.4). A family file cannot
/// add a root.
pub const ALLOWED_ROOTS: [&str; 6] = [
    "/srv/agents/vault",
    "/srv/agents/code",
    "/srv/agents/state/index",
    "/srv/agents/work/projects",
    "/srv/agents/work/platform",
    "/srv/agents/work/code-sandbox",
];

/// The root that only a platform family mounts (contract 01 §5.5).
pub const PLATFORM_ROOT: &str = "/srv/agents/work/platform";

/// The families that hold the platform root and the `release` verb (contract
/// 01 §5.5 rule 1).
pub const PLATFORM_ALLOWLIST: [&str; 1] = ["agent-control"];

/// The clones that a platform family mounts read-only (contract 01 §5.5
/// rule 6).
pub const PLATFORM_MIRRORS: [&str; 3] = [
    "/srv/agents/code/agent-control",
    "/srv/agents/code/agent-mcp",
    "/srv/agents/code/agent-registry",
];

/// The MCP server of the platform, and the tool that no family gets from it
/// (contract 01 §5.5 rule 7).
pub const PLATFORM_SERVER: &str = "github-platform";

/// See [`PLATFORM_SERVER`].
pub const PLATFORM_WITHHELD: [&str; 1] = ["merge_pull_request"];

/// The session store. The runtime mounts it (contract 01 §3.3 rule 6).
pub const SESSION_ROOT: &str = "/srv/agents/sessions";

/// The grant that means each tool of a server (contract 01 §3.4).
pub const ALL_TOOLS: &str = "all";

/// The separator of a call name: `<server>__<tool>` (contract 01 §3.11).
pub const CALL_SEPARATOR: &str = "__";

/// The entry of `approval` for a delegate call (contract 01 §3.5 rule 6).
pub const INVOKE_AGENT: &str = "invoke_agent";

/// The tool that `quiet.board` reads (contract 01 §3.15).
pub const SURVEY_TOOL: &str = "survey_board";

/// The three short forms of a cron trigger (contract 01 §3.13).
pub const CRON_SHORTHANDS: [&str; 3] = ["@hourly", "@daily", "@weekly"];

/// The count of fields in a cron expression.
const CRON_FIELDS: usize = 5;

/// The largest count of characters in a description, after the collapse of
/// its spaces (contract 01 §3.1).
pub const DESCRIPTION_MAX: usize = 200;

/// The largest budget in USD for one day (contract 01 §3.2).
const BUDGET_USD_MAX: f64 = 500.0;

/// The memory of one idle pi process and of the playpen, in megabytes
/// (contract 01 §3.9). Only a warning uses these numbers.
const PI_PROCESS_MB: u128 = 155;
const PLAYPEN_MB: u128 = 62;

const MEMORY_MIN_MB: u128 = 256;
const MEMORY_MAX_MB: u128 = 16 * 1024;
const MB_PER_GB: u128 = 1024;

const JOB_TIMEOUT_MAX_S: u128 = 3600;

/// The time after which the PEP stops a delegate call (contract 04 §7.5).
const DELEGATE_LIMIT_S: u128 = 120;

const PORT_MAX: u32 = 65535;

const DEFAULT_CPUS: i64 = 2;
const DEFAULT_MEMORY: &str = "2g";
const DEFAULT_RESIDENT_PROCS: i64 = 12;
const DEFAULT_IMAGE: &str = "base";
const DEFAULT_JOB_TIMEOUT: &str = "120s";
const DEFAULT_INFLIGHT: i64 = 2;
const DEFAULT_SYSTEM_PROMPT: &str = "append";
const DEFAULT_SANDBOX_TOOLS: [&str; 4] = ["read", "grep", "find", "ls"];
const DEFAULT_FLOOR_HOURS: i64 = 24;

// --- the report (contract 01 §7) ---

/// How bad one issue is.
///
/// The set is closed by contract 01 §7. A reader refuses another value: a
/// report with an unknown severity cannot say whether the family is valid.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    /// The family is invalid.
    Error,
    /// The family is valid. The operator must read the message.
    Warning,
}

impl Severity {
    /// The text of the severity in a report.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Error => "error",
            Self::Warning => "warning",
        }
    }
}

/// One violation of one file (contract 01 §7).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Issue {
    pub severity: Severity,
    /// The path of the field, for example `tools.kagi[1]`.
    pub loc: String,
    pub msg: String,
    /// Whether the issue is a warning in the place of a check that needs the
    /// host (contract 01 §5.1).
    pub downgraded: bool,
}

/// The part of a report that one issue belongs to. The order of the variants
/// is the order of the Python report: the sections of a family file first,
/// then the sections of a server file.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Section {
    Identity,
    Model,
    Files,
    Tools,
    Verbs,
    Delegates,
    Inflight,
    Egress,
    Runtime,
    Sandbox,
    Skills,
    Approval,
    KindFields,
    Quiet,
    ServerName,
    ServerInstall,
    ServerRun,
    ServerTools,
    ServerFences,
    ServerShared,
}

/// Which code wrote an issue. In one place of the report, the Python
/// validator writes the rule of the file before the rule of the registry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Pass {
    /// A rule that one file decides. The conversion of this crate checks it.
    File,
    /// A rule that needs another file or the host.
    Registry,
}

/// The place of one issue in a report.
///
/// Two crates write issues: this one writes each rule that one file decides,
/// and `agent-family` writes each rule that needs the registry or the host.
/// The Python validator runs both kinds in one pass, field after field. A
/// stable sort on the slot puts the issues of the two crates into that order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Slot {
    section: Section,
    item: usize,
    step: u8,
    place: usize,
    pass: Pass,
}

impl Slot {
    /// The start of a section, for a rule of one file.
    #[must_use]
    pub fn of(section: Section) -> Self {
        Self {
            section,
            item: 0,
            step: 0,
            place: 0,
            pass: Pass::File,
        }
    }

    /// The same slot at the item `item` of the section.
    #[must_use]
    pub fn item(self, item: usize) -> Self {
        Self { item, ..self }
    }

    /// The same slot at the step `step` of the item.
    #[must_use]
    pub fn step(self, step: u8) -> Self {
        Self { step, ..self }
    }

    /// The same slot at the position `place` of the step.
    #[must_use]
    pub fn place(self, place: usize) -> Self {
        Self { place, ..self }
    }

    /// The same slot, for a rule that needs the registry or the host.
    #[must_use]
    pub fn registry(self) -> Self {
        Self {
            pass: Pass::Registry,
            ..self
        }
    }
}

/// One issue and its place in the report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Placed {
    pub slot: Slot,
    pub issue: Issue,
}

impl Placed {
    /// An error at `slot`.
    #[must_use]
    pub fn error(slot: Slot, loc: impl Into<String>, msg: impl Into<String>) -> Self {
        Self {
            slot,
            issue: Issue {
                severity: Severity::Error,
                loc: loc.into(),
                msg: msg.into(),
                downgraded: false,
            },
        }
    }

    /// A warning at `slot`.
    #[must_use]
    pub fn warning(slot: Slot, loc: impl Into<String>, msg: impl Into<String>) -> Self {
        Self {
            slot,
            issue: Issue {
                severity: Severity::Warning,
                loc: loc.into(),
                msg: msg.into(),
                downgraded: false,
            },
        }
    }
}

/// Collects the issues of one conversion.
#[derive(Debug, Default)]
pub(crate) struct Issues {
    entries: Vec<Placed>,
}

impl Issues {
    pub(crate) fn error(&mut self, slot: Slot, loc: impl Into<String>, msg: impl Into<String>) {
        self.entries.push(Placed::error(slot, loc, msg));
    }

    pub(crate) fn warn(&mut self, slot: Slot, loc: impl Into<String>, msg: impl Into<String>) {
        self.entries.push(Placed::warning(slot, loc, msg));
    }

    pub(crate) fn has_error(&self) -> bool {
        self.entries
            .iter()
            .any(|placed| placed.issue.severity == Severity::Error)
    }

    pub(crate) fn into_entries(self) -> Vec<Placed> {
        self.entries
    }
}

/// The message of the error that [`Refused`] adds to a list with no error.
const NO_REASON: &str = "the file is not valid, and the validator gave no reason";

/// Why a raw file is no valid file: each issue that the conversion found. At
/// least one issue is an error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    issues: Vec<Placed>,
}

impl Refused {
    /// Each issue of a conversion that gave no valid file.
    ///
    /// A list with no error is a defect of the conversion. The value fails
    /// closed: it then holds one more issue, an error for the whole file. A
    /// report of a refused file thus never reads as a clean report.
    pub(crate) fn new(mut issues: Vec<Placed>) -> Self {
        let has_error = issues
            .iter()
            .any(|placed| placed.issue.severity == Severity::Error);
        if !has_error {
            issues.push(Placed::error(
                Slot::of(Section::Identity),
                "<document>",
                NO_REASON,
            ));
        }

        Self { issues }
    }

    /// Each issue, in the order of the report.
    #[must_use]
    pub fn issues(&self) -> &[Placed] {
        &self.issues
    }

    /// Each issue, in the order of the report.
    #[must_use]
    pub fn into_issues(self) -> Vec<Placed> {
        self.issues
    }
}

impl fmt::Display for Refused {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let first = self
            .issues
            .iter()
            .find(|placed| placed.issue.severity == Severity::Error);
        match first {
            Some(placed) => write!(f, "{}: {}", placed.issue.loc, placed.issue.msg),
            None => f.write_str("the file is not valid"),
        }
    }
}

impl Error for Refused {}

// --- the text that Python writes ---

/// The text that Python writes for a float: `repr(value)`.
///
/// A message of the Python validator holds a budget in this form, for
/// example `1e+22 is outside 0 to 500.0`. A message of this crate holds the
/// same text.
#[must_use]
pub fn python_float_text(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }

    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_owned();
    }

    // `{:e}` gives the shortest digits that read back as the same float, as
    // Python does: `1.2345e-5`.
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if !(-4..16).contains(&exponent) {
        let (first, rest) = digits.split_at_checked(1).unwrap_or((&digits, ""));
        let point = if rest.is_empty() { "" } else { "." };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };

        return format!(
            "{sign}{first}{point}{rest}e{exponent_sign}{:02}",
            exponent.unsigned_abs()
        );
    }

    if exponent < 0 {
        let zeros = "0".repeat(usize::try_from(-exponent - 1).unwrap_or(0));

        return format!("{sign}0.{zeros}{digits}");
    }

    let whole = usize::try_from(exponent).unwrap_or(0) + 1;
    if digits.len() <= whole {
        let zeros = "0".repeat(whole - digits.len());

        return format!("{sign}{digits}{zeros}.0");
    }

    let (first, rest) = digits.split_at_checked(whole).unwrap_or((&digits, ""));

    format!("{sign}{first}.{rest}")
}

/// Whether Python takes `c` as a space: `str.isspace`.
fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// The words of a text, as `str.split()` gives them.
fn py_words(text: &str) -> impl Iterator<Item = &str> {
    text.split(is_py_space).filter(|word| !word.is_empty())
}

/// The text with each run of spaces as one space: `" ".join(text.split())`.
#[must_use]
pub fn collapse(text: &str) -> String {
    py_words(text).collect::<Vec<_>>().join(" ")
}

/// Whether `path` is `root` or is below it. The check reads whole segments.
#[must_use]
pub fn is_under(path: &str, root: &str) -> bool {
    path.strip_prefix(root)
        .is_some_and(|rest| rest.is_empty() || rest.starts_with('/'))
}

// --- an integer of the file ---

/// An integer as the file wrote it. Python holds an integer of any size, and
/// a message of the report writes each digit of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawInt(RawIntForm);

#[derive(Debug, Clone, PartialEq, Eq)]
enum RawIntForm {
    Small(i64),
    /// The decimal text of an integer that does not fit `i64`.
    Large(String),
}

impl RawInt {
    /// The integer of a decimal text: an optional `-`, then ASCII digits.
    #[must_use]
    pub fn from_decimal(text: &str) -> Option<Self> {
        let (sign, digits) = match text.strip_prefix('-') {
            Some(digits) => ("-", digits),
            None => ("", text),
        };
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }

        if let Ok(small) = text.parse::<i64>() {
            return Some(Self(RawIntForm::Small(small)));
        }

        let digits = digits.trim_start_matches('0');

        Some(Self(RawIntForm::Large(format!("{sign}{digits}"))))
    }

    /// The value, when it fits `i64`.
    #[must_use]
    pub fn to_i64(&self) -> Option<i64> {
        match &self.0 {
            RawIntForm::Small(value) => Some(*value),
            RawIntForm::Large(_) => None,
        }
    }
}

impl RawInt {
    /// Whether the integer is below zero, and its digits.
    fn sign_and_digits(&self) -> (bool, String) {
        let text = self.to_string();
        match text.strip_prefix('-') {
            Some(digits) => (true, digits.trim_start_matches('0').to_owned()),
            None => (false, text.trim_start_matches('0').to_owned()),
        }
    }
}

impl Ord for RawInt {
    /// The order of the values.
    fn cmp(&self, other: &Self) -> Ordering {
        let (negative, digits) = self.sign_and_digits();
        let (other_negative, other_digits) = other.sign_and_digits();
        let size = digits
            .len()
            .cmp(&other_digits.len())
            .then_with(|| digits.cmp(&other_digits));
        match (negative, other_negative) {
            (false, false) => size,
            (true, true) => size.reverse(),
            (true, false) if digits.is_empty() && other_digits.is_empty() => Ordering::Equal,
            (true, false) => Ordering::Less,
            (false, true) if digits.is_empty() && other_digits.is_empty() => Ordering::Equal,
            (false, true) => Ordering::Greater,
        }
    }
}

impl PartialOrd for RawInt {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl From<i64> for RawInt {
    fn from(value: i64) -> Self {
        Self(RawIntForm::Small(value))
    }
}

impl fmt::Display for RawInt {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.0 {
            RawIntForm::Small(value) => write!(f, "{value}"),
            RawIntForm::Large(text) => f.write_str(text),
        }
    }
}

impl Serialize for RawInt {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match &self.0 {
            RawIntForm::Small(value) => serializer.serialize_i64(*value),
            RawIntForm::Large(text) => serializer.serialize_str(text),
        }
    }
}

struct RawIntVisitor;

impl Visitor<'_> for RawIntVisitor {
    type Value = RawInt;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("an integer")
    }

    fn visit_i64<E: de::Error>(self, value: i64) -> Result<RawInt, E> {
        Ok(RawInt::from(value))
    }

    fn visit_u64<E: de::Error>(self, value: u64) -> Result<RawInt, E> {
        RawInt::from_decimal(&value.to_string()).ok_or_else(|| E::custom("not an integer"))
    }
}

impl<'de> Deserialize<'de> for RawInt {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_i64(RawIntVisitor)
    }
}

// --- the raw file ---

fn default_cpus() -> RawInt {
    RawInt::from(DEFAULT_CPUS)
}

fn default_memory() -> String {
    DEFAULT_MEMORY.to_owned()
}

fn default_resident() -> RawInt {
    RawInt::from(DEFAULT_RESIDENT_PROCS)
}

fn default_image() -> String {
    DEFAULT_IMAGE.to_owned()
}

fn default_job_timeout() -> String {
    DEFAULT_JOB_TIMEOUT.to_owned()
}

fn default_inflight() -> RawInt {
    RawInt::from(DEFAULT_INFLIGHT)
}

fn default_system_prompt() -> String {
    DEFAULT_SYSTEM_PROMPT.to_owned()
}

fn default_sandbox_tools() -> Vec<String> {
    DEFAULT_SANDBOX_TOOLS.map(str::to_owned).to_vec()
}

fn default_floor_hours() -> RawInt {
    RawInt::from(DEFAULT_FLOOR_HOURS)
}

/// `model`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawModel {
    pub router: String,
    pub budget_usd_per_day: f64,
}

/// One entry of `files`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawMount {
    pub path: String,
    pub mode: String,
}

/// `sandbox`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawSandbox {
    #[serde(default = "default_cpus")]
    pub cpus: RawInt,
    #[serde(default = "default_memory")]
    pub memory: String,
    #[serde(default = "default_resident")]
    pub max_resident_processes: RawInt,
    #[serde(default = "default_image")]
    pub image: String,
}

impl Default for RawSandbox {
    fn default() -> Self {
        Self {
            cpus: default_cpus(),
            memory: default_memory(),
            max_resident_processes: default_resident(),
            image: default_image(),
        }
    }
}

/// `job`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawJob {
    #[serde(default = "default_job_timeout")]
    pub timeout: String,
}

impl Default for RawJob {
    fn default() -> Self {
        Self {
            timeout: default_job_timeout(),
        }
    }
}

/// One entry of `triggers`, as the file wrote it. A valid entry has exactly
/// one of the three keys.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawTrigger {
    #[serde(default)]
    pub cron: Option<String>,
    #[serde(default)]
    pub webhook: Option<String>,
    #[serde(default)]
    pub enqueue: Option<bool>,
}

/// `quiet.daily`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawDaily {
    pub call: String,
    pub hour: RawInt,
    pub zone: String,
    #[serde(default)]
    pub weekdays_only: bool,
}

/// `quiet`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawQuiet {
    #[serde(default)]
    pub board: Option<String>,
    #[serde(default)]
    pub daily: Option<RawDaily>,
    #[serde(default = "default_floor_hours")]
    pub floor_hours: RawInt,
}

impl Default for RawQuiet {
    fn default() -> Self {
        Self {
            board: None,
            daily: None,
            floor_hours: default_floor_hours(),
        }
    }
}

/// One entry of `verbs.ha_call.allow`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawHaTriple {
    pub domain: String,
    pub service: String,
    #[serde(default)]
    pub entity_id: Option<String>,
}

/// The fence of a verb that takes no fence: `{}`.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawNoFence {}

/// `verbs.ha_call`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawHaCall {
    pub allow: Vec<RawHaTriple>,
}

/// `verbs.enqueue`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawEnqueue {
    pub targets: Vec<String>,
}

/// `verbs.release`, as the file wrote it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawRelease {
    pub components: Vec<String>,
}

/// `verbs`, as the file wrote it: the five verbs of version 1.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawVerbs {
    #[serde(default)]
    pub embed: Option<RawNoFence>,
    #[serde(default)]
    pub ha_call: Option<RawHaCall>,
    #[serde(default)]
    pub enqueue: Option<RawEnqueue>,
    #[serde(default)]
    pub job_status: Option<RawNoFence>,
    #[serde(default)]
    pub release: Option<RawRelease>,
}

impl RawVerbs {
    /// The verbs that the file grants, in the order of the contract.
    #[must_use]
    pub fn granted(&self) -> Vec<Verb> {
        let held = [
            (Verb::Embed, self.embed.is_some()),
            (Verb::HaCall, self.ha_call.is_some()),
            (Verb::Enqueue, self.enqueue.is_some()),
            (Verb::JobStatus, self.job_status.is_some()),
            (Verb::Release, self.release.is_some()),
        ];

        held.into_iter()
            .filter(|(_, granted)| *granted)
            .map(|(verb, _)| verb)
            .collect()
    }
}

/// One grant of `tools`, as the file wrote it: a list of tool names, or one
/// text. The one valid text is `all`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RawToolGrant {
    Named(Vec<String>),
    Text(String),
}

/// One `family.yaml`, as the file wrote it, with each default.
///
/// The shape is the shape of the Python model `FamilyFile`: the fields, their
/// types and their defaults. No value rule holds for a value of this type.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawFamily {
    pub name: String,
    pub kind: String,
    pub description: String,
    pub model: RawModel,
    #[serde(default)]
    pub files: Vec<RawMount>,
    #[serde(default)]
    pub tools: BTreeMap<String, RawToolGrant>,
    #[serde(default)]
    pub verbs: RawVerbs,
    #[serde(default)]
    pub delegates: Vec<String>,
    #[serde(default = "default_inflight")]
    pub max_inflight_delegations: RawInt,
    #[serde(default)]
    pub egress: Vec<String>,
    #[serde(default)]
    pub shell: bool,
    #[serde(default = "default_sandbox_tools")]
    pub sandbox_tools: Vec<String>,
    #[serde(default = "default_system_prompt")]
    pub system_prompt: String,
    #[serde(default)]
    pub sandbox: RawSandbox,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub approval: Vec<String>,
    #[serde(default)]
    pub job: Option<RawJob>,
    #[serde(default)]
    pub triggers: Option<Vec<RawTrigger>>,
    #[serde(default)]
    pub max_running_turns: Option<RawInt>,
    #[serde(default)]
    pub quiet: Option<RawQuiet>,
}

impl RawFamily {
    /// The file with each field that has a default at its default.
    #[must_use]
    pub fn with_defaults(name: String, kind: String, description: String, model: RawModel) -> Self {
        Self {
            name,
            kind,
            description,
            model,
            files: Vec::new(),
            tools: BTreeMap::new(),
            verbs: RawVerbs::default(),
            delegates: Vec::new(),
            max_inflight_delegations: default_inflight(),
            egress: Vec::new(),
            shell: false,
            sandbox_tools: default_sandbox_tools(),
            system_prompt: default_system_prompt(),
            sandbox: RawSandbox::default(),
            skills: Vec::new(),
            approval: Vec::new(),
            job: None,
            triggers: None,
            max_running_turns: None,
            quiet: None,
        }
    }

    /// The named tools that the file grants from one server. `all` and a
    /// server with no grant give an empty list.
    #[must_use]
    pub fn tool_names(&self, server: &str) -> &[String] {
        match self.tools.get(server) {
            Some(RawToolGrant::Named(tools)) => tools,
            _ => &[],
        }
    }

    /// Whether the file grants each tool of one server.
    #[must_use]
    pub fn grants_all(&self, server: &str) -> bool {
        matches!(self.tools.get(server), Some(RawToolGrant::Text(text)) if text == ALL_TOOLS)
    }
}

/// Makes a struct with private fields, and the two doc tests that show that
/// code outside this module can hold a value and cannot build one.
macro_rules! sealed {
    ($(#[$attribute:meta])* pub struct $name:ident { $($field:tt)* }) => {
        $(#[$attribute])*
        ///
        /// Code outside this module cannot build a value from raw fields:
        ///
        #[doc = "```compile_fail,E0451"]
        #[doc = concat!("use creche_contracts::family::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn build(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = concat!("    ", stringify!($name), " { ..other }")]
        #[doc = "}"]
        #[doc = "```"]
        ///
        /// Code outside this module can hold a value:
        ///
        #[doc = "```"]
        #[doc = concat!("use creche_contracts::family::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn keep(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = "    other"]
        #[doc = "}"]
        #[doc = "```"]
        pub struct $name { $($field)* }
    };
}

// --- closed sets ---

/// Makes an enum of texts: the text of each variant, a parsing constructor
/// and the list of each text.
macro_rules! text_enum {
    (
        $(#[$attribute:meta])*
        $name:ident { $($variant:ident => $text:literal),+ $(,)? }
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub enum $name {
            $($variant,)+
        }

        impl $name {
            /// Each value, in the order of the contract.
            pub const ALL: &[Self] = &[$(Self::$variant,)+];

            /// The text of the value in a file.
            #[must_use]
            pub fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $text,)+
                }
            }

            /// The value of a text. `None` for a text that is not in the set.
            #[must_use]
            pub fn parse(text: &str) -> Option<Self> {
                match text {
                    $($text => Some(Self::$variant),)+
                    _ => None,
                }
            }

            /// Each text, with `, ` between two texts.
            #[must_use]
            pub fn listed() -> String {
                Self::ALL
                    .iter()
                    .map(|value| value.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }
    };
}

text_enum! {
    /// The kind of a family (contract 01 §2).
    ///
    /// The set is closed. A reader refuses another value: the kind decides
    /// which fields a file holds and how a session starts.
    Kind {
        Attended => "attended",
        Thin => "thin",
        Autonomous => "autonomous",
    }
}

text_enum! {
    /// The mode of a mount (contract 01 §3.3).
    ///
    /// The set is closed. A reader refuses another value: an unknown mode
    /// can give write access.
    Mode {
        Ro => "ro",
        Rw => "rw",
    }
}

text_enum! {
    /// One built-in tool of the sandbox (contract 01 §3.8). `bash` is not in
    /// the set: `shell` is the one way to a shell.
    ///
    /// The set is closed. A reader refuses another value.
    SandboxTool {
        Read => "read",
        Write => "write",
        Edit => "edit",
        Grep => "grep",
        Find => "find",
        Ls => "ls",
        Codemode => "codemode",
    }
}

text_enum! {
    /// How `instructions.md` meets the system prompt of pi (contract 01
    /// §3.8).
    ///
    /// The set is closed. A reader refuses another value.
    SystemPrompt {
        Append => "append",
        Replace => "replace",
    }
}

text_enum! {
    /// The image that the platform builds a sandbox from (contract 01 §3.9).
    /// It is not an image reference.
    ///
    /// The set is closed. A reader refuses another value: a file must not
    /// select the code that runs in the sandbox.
    SandboxFlavor {
        Base => "base",
        Python => "python",
    }
}

text_enum! {
    /// One of the five verbs of version 1 (contract 01 §3.5).
    ///
    /// The set is closed. A reader refuses another value: a new verb comes
    /// with a new version of the contract and a release of the PEP.
    Verb {
        Embed => "embed",
        HaCall => "ha_call",
        Enqueue => "enqueue",
        JobStatus => "job_status",
        Release => "release",
    }
}

text_enum! {
    /// A component that takes a release (contract 06 §1). The order is the
    /// order of the names as text.
    ///
    /// The set is closed. A reader refuses another value.
    Releasable {
        Attendance => "attendance",
        Caregiver => "caregiver",
        Chaperone => "chaperone",
        Handover => "handover",
        Infra => "infra",
        McpServers => "mcp-servers",
        Noticeboard => "noticeboard",
        Playpen => "playpen",
    }
}

// --- numbers with a range ---

/// Why an integer is not in the range of its type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OutOfRange {
    /// The smallest value of the type.
    pub min: u8,
    /// The largest value of the type.
    pub max: u8,
}

impl fmt::Display for OutOfRange {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the number is outside {} to {}", self.min, self.max)
    }
}

impl Error for OutOfRange {}

/// Makes a type that holds one integer of a range.
macro_rules! bounded {
    ($(#[$attribute:meta])* $name:ident, $min:literal, $max:literal) => {
        $(#[$attribute])*
        ///
        /// Code outside this module cannot build a value from a raw value:
        ///
        #[doc = "```compile_fail,E0423"]
        #[doc = concat!("use creche_contracts::family::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("let value = ", stringify!($name), "(Default::default());")]
        #[doc = "```"]
        ///
        /// Code outside this module can hold a value:
        ///
        #[doc = "```"]
        #[doc = concat!("use creche_contracts::family::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn keep(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = "    other"]
        #[doc = "}"]
        #[doc = "```"]
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(u8);

        impl $name {
            /// The smallest value.
            pub const MIN: u8 = $min;
            /// The largest value.
            pub const MAX: u8 = $max;

            /// The number.
            #[must_use]
            pub fn get(self) -> u8 {
                self.0
            }
        }

        impl TryFrom<&RawInt> for $name {
            type Error = OutOfRange;

            fn try_from(value: &RawInt) -> Result<Self, OutOfRange> {
                value
                    .to_i64()
                    .and_then(|value| u8::try_from(value).ok())
                    .filter(|value| ($min..=$max).contains(value))
                    .map(Self)
                    .ok_or(OutOfRange { min: $min, max: $max })
            }
        }

        impl From<$name> for RawInt {
            fn from(value: $name) -> RawInt {
                RawInt::from(i64::from(value.0))
            }
        }
    };
}

bounded! {
    /// The count of CPUs of a sandbox: 1 to 8 (contract 01 §3.9).
    ///
    /// ```
    /// use creche_contracts::family::{Cpus, RawInt};
    ///
    /// assert_eq!(Cpus::try_from(&RawInt::from(2)).map(Cpus::get), Ok(2));
    /// assert!(Cpus::try_from(&RawInt::from(9)).is_err());
    /// ```
    Cpus, 1, 8
}

bounded! {
    /// The count of pi processes that a sandbox keeps: 1 to 32 (contract 01
    /// §3.9).
    ResidentProcs, 1, 32
}

bounded! {
    /// The count of delegate calls of one family in flight: 1 to 8 (contract
    /// 01 §3.6.1).
    InflightCap, 1, 8
}

bounded! {
    /// The count of turns of one autonomous family that run at one time: 1
    /// to 8 (contract 01 §3.14).
    RunningTurns, 1, 8
}

bounded! {
    /// An hour of the day in a zone: 0 to 23 (contract 01 §3.15).
    LocalHour, 0, 23
}

bounded! {
    /// The longest time in hours between two wakes of a quiet family: 1 to
    /// 168 (contract 01 §3.15).
    FloorHours, 1, 168
}

// --- texts with a grammar ---

/// Makes the shell of a type that holds one checked text. The module gives
/// the type its `FromStr`.
macro_rules! checked_text {
    ($(#[$attribute:meta])* $name:ident) => {
        $(#[$attribute])*
        ///
        /// Code outside this module cannot build a value from a raw value:
        ///
        #[doc = "```compile_fail,E0423"]
        #[doc = concat!("use creche_contracts::family::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("let value = ", stringify!($name), "(Default::default());")]
        #[doc = "```"]
        ///
        /// Code outside this module can hold a value:
        ///
        #[doc = "```"]
        #[doc = concat!("use creche_contracts::family::", stringify!($name), ";")]
        #[doc = ""]
        #[doc = concat!("fn keep(other: ", stringify!($name), ") -> ", stringify!($name), " {")]
        #[doc = "    other"]
        #[doc = "}"]
        #[doc = "```"]
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);

        impl $name {
            /// The text, as the file wrote it.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

/// Makes the `Display` and the `Error` of an error enum from one text for
/// each variant.
macro_rules! error_texts {
    ($name:ident => $text:literal) => {
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str($text)
            }
        }

        impl Error for $name {}
    };
    ($name:ident { $($variant:ident => $text:literal),+ $(,)? }) => {
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(match self {
                    $(Self::$variant => $text,)+
                })
            }
        }

        impl Error for $name {}
    };
}

checked_text! {
    /// What a family is for: 1 to 200 characters after the collapse of each
    /// run of spaces (contract 01 §3.1). The type holds the text as the file
    /// wrote it.
    ///
    /// ```
    /// use creche_contracts::family::Description;
    ///
    /// let text: Description = "Reads the vault.".parse()?;
    /// assert_eq!(text.as_str(), "Reads the vault.");
    /// # Ok::<(), creche_contracts::family::DescriptionError>(())
    /// ```
    Description
}

/// Why a text is not a description. The field is the count of characters
/// after the collapse.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DescriptionError {
    /// The count of characters after the collapse.
    pub length: usize,
}

impl fmt::Display for DescriptionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "a description has 1 to {DESCRIPTION_MAX} characters, not {}",
            self.length
        )
    }
}

impl Error for DescriptionError {}

impl FromStr for Description {
    type Err = DescriptionError;

    fn from_str(text: &str) -> Result<Self, DescriptionError> {
        let length = collapse(text).chars().count();
        if !(1..=DESCRIPTION_MAX).contains(&length) {
            return Err(DescriptionError { length });
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// A model alias of the router: `[a-z0-9][a-z0-9._/-]*`, with no wildcard
    /// (contract 01 §3.2).
    ModelAlias
}

/// Why a text is not a model alias.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModelAliasError {
    /// The text holds `*`.
    Wildcard,
    /// The text is not `[a-z0-9][a-z0-9._/-]*`.
    Grammar,
}

error_texts!(ModelAliasError {
    Wildcard => "a model alias holds no wildcard",
    Grammar => "a model alias is [a-z0-9][a-z0-9._/-]*",
});

impl FromStr for ModelAlias {
    type Err = ModelAliasError;

    fn from_str(text: &str) -> Result<Self, ModelAliasError> {
        if text.contains('*') {
            return Err(ModelAliasError::Wildcard);
        }

        let head = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
        let tail = |byte: &u8| head(byte) || matches!(byte, b'.' | b'_' | b'/' | b'-');
        let bytes = text.as_bytes();
        if !bytes.first().is_some_and(head) || !bytes.iter().all(tail) {
            return Err(ModelAliasError::Grammar);
        }

        Ok(Self(text.to_owned()))
    }
}

/// The budget of a family in USD for one day: above 0, and 500 at most
/// (contract 01 §3.2).
#[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
pub struct DailyBudget(f64);

/// Why a number is not a budget for one day.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DailyBudgetError;

error_texts!(DailyBudgetError => "a budget is above 0 and is 500 at most");

impl DailyBudget {
    /// The budget in USD.
    #[must_use]
    pub fn get(self) -> f64 {
        self.0
    }
}

impl TryFrom<f64> for DailyBudget {
    type Error = DailyBudgetError;

    fn try_from(value: f64) -> Result<Self, DailyBudgetError> {
        if value > 0.0 && value <= BUDGET_USD_MAX {
            Ok(Self(value))
        } else {
            Err(DailyBudgetError)
        }
    }
}

checked_text! {
    /// The path of a mount on the host (contract 01 §3.3, §5.4): absolute,
    /// with no glob, no `.`, no `..` and no empty segment, not the session
    /// store, and under an allowed root.
    ///
    /// ```
    /// use creche_contracts::family::{MountPath, MountPathError};
    ///
    /// assert!("/srv/agents/vault/notes".parse::<MountPath>().is_ok());
    /// assert_eq!("/etc".parse::<MountPath>(), Err(MountPathError::NoRoot));
    /// ```
    MountPath
}

/// Why a text is not the path of a mount.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MountPathError {
    /// The text holds `*`, `?` or `[`.
    Glob,
    /// The text does not start with `/`.
    Relative,
    /// The text holds a `.` segment, a `..` segment or `//`.
    Segment,
    /// The text holds a character outside `[A-Za-z0-9._/-]`.
    Character,
    /// The path is the session store or is below it.
    SessionStore,
    /// The path is under no allowed root.
    NoRoot,
}

error_texts!(MountPathError {
    Glob => "a mount path is no glob",
    Relative => "a mount path is absolute",
    Segment => "a mount path holds no '.', '..' or empty segment",
    Character => "a mount path is [A-Za-z0-9._/-]",
    SessionStore => "a mount path is not the session store",
    NoRoot => "a mount path is under an allowed root",
});

impl MountPathError {
    /// Whether the text has the form of a path. A path with the form can be
    /// under no allowed root.
    #[must_use]
    pub fn has_path_form(self) -> bool {
        self == Self::NoRoot
    }
}

impl FromStr for MountPath {
    type Err = MountPathError;

    fn from_str(text: &str) -> Result<Self, MountPathError> {
        if text.contains(['*', '?', '[']) {
            return Err(MountPathError::Glob);
        }

        if !text.starts_with('/') {
            return Err(MountPathError::Relative);
        }

        if text.contains("//")
            || text
                .split('/')
                .skip(1)
                .any(|part| matches!(part, "." | ".."))
        {
            return Err(MountPathError::Segment);
        }

        let allowed =
            |byte: u8| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'/' | b'-');
        if !text.bytes().all(allowed) {
            return Err(MountPathError::Character);
        }

        if is_under(text, SESSION_ROOT) {
            return Err(MountPathError::SessionStore);
        }

        if !ALLOWED_ROOTS.iter().any(|root| is_under(text, root)) {
            return Err(MountPathError::NoRoot);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// A domain or a service of Home Assistant: `[a-z][a-z0-9_]*` (contract
    /// 01 §3.5).
    HaIdentifier
}

/// Why a text is not a Home Assistant identifier.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HaIdentifierError;

error_texts!(HaIdentifierError => "a Home Assistant identifier is [a-z][a-z0-9_]*");

fn is_ha_identifier(text: &str) -> bool {
    let bytes = text.as_bytes();
    let tail = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_';

    bytes.first().is_some_and(u8::is_ascii_lowercase) && bytes.iter().all(tail)
}

impl FromStr for HaIdentifier {
    type Err = HaIdentifierError;

    fn from_str(text: &str) -> Result<Self, HaIdentifierError> {
        if !is_ha_identifier(text) {
            return Err(HaIdentifierError);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// An entity of Home Assistant: `domain.object_id` (contract 01 §3.5).
    HaEntityId
}

/// Why a text is not a Home Assistant entity id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HaEntityIdError;

error_texts!(HaEntityIdError => "a Home Assistant entity id is domain.object_id");

impl FromStr for HaEntityId {
    type Err = HaEntityIdError;

    fn from_str(text: &str) -> Result<Self, HaEntityIdError> {
        let object = |byte: u8| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_';
        let valid = text.split_once('.').is_some_and(|(domain, object_id)| {
            is_ha_identifier(domain) && !object_id.is_empty() && object_id.bytes().all(object)
        });
        if !valid {
            return Err(HaEntityIdError);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// One host that a sandbox reaches: `hostname` or `hostname:port`
    /// (contract 01 §3.7). It is no IP literal and holds no wildcard.
    EgressHost
}

/// Why a text is not an egress host.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EgressHostError {
    /// The text is an IPv4 address or an IPv6 address.
    IpLiteral,
    /// The text holds `*`.
    Wildcard,
    /// The port is not a number from 1 to 65535.
    BadPort,
    /// The text is not a hostname.
    BadHostname,
}

error_texts!(EgressHostError {
    IpLiteral => "an egress host is no IP literal",
    Wildcard => "an egress host holds no wildcard",
    BadPort => "the port of an egress host is 1 to 65535",
    BadHostname => "an egress host is a hostname or hostname:port",
});

fn is_ipv4(host: &str) -> bool {
    let parts: Vec<&str> = host.split('.').collect();

    parts.len() == 4
        && parts.iter().all(|part| {
            (1..=3).contains(&part.len()) && part.bytes().all(|byte| byte.is_ascii_digit())
        })
}

fn is_host_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    let edge = |byte: &u8| byte.is_ascii_alphanumeric();

    bytes.first().is_some_and(edge)
        && bytes.last().is_some_and(edge)
        && bytes.iter().all(|byte| edge(byte) || *byte == b'-')
}

fn is_port(port: &str) -> bool {
    let digits = port.trim_start_matches('0');

    port.bytes().all(|byte| byte.is_ascii_digit())
        && digits.len() <= 5
        && digits
            .parse::<u32>()
            .is_ok_and(|number| (1..=PORT_MAX).contains(&number))
}

impl FromStr for EgressHost {
    type Err = EgressHostError;

    fn from_str(text: &str) -> Result<Self, EgressHostError> {
        let parts = text.split_once(':');
        let (host, port) = parts.unwrap_or((text, ""));
        if text.contains('*') {
            return Err(EgressHostError::Wildcard);
        }

        if is_ipv4(host) || port.contains(':') {
            return Err(EgressHostError::IpLiteral);
        }

        // A colon with no port after it is neither of the two forms.
        if parts.is_some() && !is_port(port) {
            return Err(EgressHostError::BadPort);
        }

        if host.is_empty() || !host.split('.').all(is_host_label) {
            return Err(EgressHostError::BadHostname);
        }

        Ok(Self(text.to_owned()))
    }
}

/// A count and one unit letter: `[1-9][0-9]*` and one byte of `units`. A
/// count too large for the type is the largest value of the type.
fn count_and_unit(text: &str, units: &[u8]) -> Option<(u128, u8)> {
    let (unit, digits) = text.as_bytes().split_last()?;
    let counted = digits
        .first()
        .is_some_and(|byte| (b'1'..=b'9').contains(byte))
        && digits.iter().all(u8::is_ascii_digit);
    if !units.contains(unit) || !counted {
        return None;
    }

    let count = digits.iter().fold(0_u128, |count, byte| {
        count
            .saturating_mul(10)
            .saturating_add(u128::from(byte - b'0'))
    });

    Some((count, *unit))
}

/// The megabytes of a memory text: `[1-9][0-9]*[mg]`. `None` for another
/// text. A size too large for the type is the largest value of the type.
#[must_use]
pub fn memory_mb(text: &str) -> Option<u128> {
    let (count, unit) = count_and_unit(text, b"mg")?;

    Some(if unit == b'g' {
        count.saturating_mul(MB_PER_GB)
    } else {
        count
    })
}

/// The seconds of a duration text: `[1-9][0-9]*[smh]`. `None` for another
/// text. A time too large for the type is the largest value of the type.
#[must_use]
pub fn duration_s(text: &str) -> Option<u128> {
    let (count, unit) = count_and_unit(text, b"smh")?;
    let scale = match unit {
        b'm' => 60,
        b'h' => 3600,
        _ => 1,
    };

    Some(count.saturating_mul(scale))
}

checked_text! {
    /// The memory of a sandbox: `[1-9][0-9]*[mg]`, from 256m to 16g (contract
    /// 01 §3.9).
    Memory
}

/// Why a text is not the memory of a sandbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MemoryError {
    /// The text is not `[1-9][0-9]*[mg]`.
    Grammar,
    /// The size is below 256m or above 16g.
    Range,
}

error_texts!(MemoryError {
    Grammar => "a memory size is [1-9][0-9]*[mg]",
    Range => "a memory size is 256m to 16g",
});

impl Memory {
    /// The size in megabytes.
    #[must_use]
    pub fn megabytes(&self) -> u32 {
        memory_mb(&self.0)
            .and_then(|megabytes| u32::try_from(megabytes).ok())
            .unwrap_or(0)
    }
}

impl FromStr for Memory {
    type Err = MemoryError;

    fn from_str(text: &str) -> Result<Self, MemoryError> {
        let megabytes = memory_mb(text).ok_or(MemoryError::Grammar)?;
        if !(MEMORY_MIN_MB..=MEMORY_MAX_MB).contains(&megabytes) {
            return Err(MemoryError::Range);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// The longest run of one job of a thin family: `[1-9][0-9]*[smh]`, from
    /// 1s to 1h (contract 01 §3.12).
    JobTimeout
}

/// Why a text is not a job timeout.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobTimeoutError {
    /// The text is not `[1-9][0-9]*[smh]`.
    Grammar,
    /// The time is above 1h.
    Range,
}

error_texts!(JobTimeoutError {
    Grammar => "a job timeout is [1-9][0-9]*[smh]",
    Range => "a job timeout is 1s to 1h",
});

impl JobTimeout {
    /// The time in seconds.
    #[must_use]
    pub fn seconds(&self) -> u32 {
        duration_s(&self.0)
            .and_then(|seconds| u32::try_from(seconds).ok())
            .unwrap_or(0)
    }
}

impl FromStr for JobTimeout {
    type Err = JobTimeoutError;

    fn from_str(text: &str) -> Result<Self, JobTimeoutError> {
        let seconds = duration_s(text).ok_or(JobTimeoutError::Grammar)?;
        if !(1..=JOB_TIMEOUT_MAX_S).contains(&seconds) {
            return Err(JobTimeoutError::Range);
        }

        Ok(Self(text.to_owned()))
    }
}

checked_text! {
    /// When a cron trigger fires: five fields, or one of `@hourly`, `@daily`
    /// and `@weekly` (contract 01 §3.13). A field holds ASCII digits and
    /// `*`, `,`, `-`, `/`.
    Cron
}

/// Why a text is not a cron expression.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CronError;

error_texts!(CronError => "a cron expression has five fields, or is @hourly, @daily or @weekly");

/// Whether a text is one field of a cron expression.
fn is_cron_field(field: &str) -> bool {
    // Contract 01 §3.13 gives no grammar for a field. The Python validator
    // takes ASCII digits and the signs of a list, a range and a step, and
    // this check does the same. `family/AGENTS.md` holds the question.
    field
        .bytes()
        .all(|byte| byte.is_ascii_digit() || matches!(byte, b'*' | b',' | b'/' | b'-'))
}

impl FromStr for Cron {
    type Err = CronError;

    fn from_str(text: &str) -> Result<Self, CronError> {
        if CRON_SHORTHANDS.contains(&text) {
            return Ok(Self(text.to_owned()));
        }

        if py_words(text).count() != CRON_FIELDS || !py_words(text).all(is_cron_field) {
            return Err(CronError);
        }

        Ok(Self(text.to_owned()))
    }
}

// --- the valid file ---

sealed! {
    /// `model` of a valid file.
    #[derive(Debug, Clone, PartialEq)]
    pub struct Model {
        router: ModelAlias,
        budget_usd_per_day: DailyBudget,
    }
}

impl Model {
    #[must_use]
    pub fn router(&self) -> &ModelAlias {
        &self.router
    }

    #[must_use]
    pub fn budget_usd_per_day(&self) -> DailyBudget {
        self.budget_usd_per_day
    }
}

sealed! {
    /// One mount of a valid file.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Mount {
        path: MountPath,
        mode: Mode,
    }
}

impl Mount {
    #[must_use]
    pub fn path(&self) -> &MountPath {
        &self.path
    }

    #[must_use]
    pub fn mode(&self) -> Mode {
        self.mode
    }
}

/// The tools that a valid file grants from one server (contract 01 §3.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolGrant {
    /// Each tool that the server file declares when a reader reads it.
    All,
    /// These tools: one or more, each one time.
    Named(Vec<ToolName>),
}

sealed! {
    /// One permitted call of Home Assistant (contract 01 §3.5).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct HaTriple {
        domain: HaIdentifier,
        service: HaIdentifier,
        entity_id: Option<HaEntityId>,
    }
}

impl HaTriple {
    #[must_use]
    pub fn domain(&self) -> &HaIdentifier {
        &self.domain
    }

    #[must_use]
    pub fn service(&self) -> &HaIdentifier {
        &self.service
    }

    #[must_use]
    pub fn entity_id(&self) -> Option<&HaEntityId> {
        self.entity_id.as_ref()
    }
}

/// `enqueue` and `job_status` as one value. `job_status` reads only the
/// sessions that this family started, so the type cannot hold it alone.
///
/// A target is the name of another family, as the file wrote it. Only the
/// registry check proves that the family exists and is autonomous.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JobReach {
    None,
    /// One target or more.
    Enqueue {
        targets: Vec<String>,
    },
    /// One target or more, and the `job_status` verb.
    EnqueueAndStatus {
        targets: Vec<String>,
    },
}

impl JobReach {
    /// The targets. The list is empty only for [`JobReach::None`].
    #[must_use]
    pub fn targets(&self) -> &[String] {
        match self {
            Self::None => &[],
            Self::Enqueue { targets } | Self::EnqueueAndStatus { targets } => targets,
        }
    }
}

sealed! {
    /// `verbs` of a valid file.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Verbs {
        embed: bool,
        ha_call: Option<Vec<HaTriple>>,
        jobs: JobReach,
        release: Option<Vec<Releasable>>,
    }
}

impl Verbs {
    #[must_use]
    pub fn embed(&self) -> bool {
        self.embed
    }

    /// The permitted calls: one or more when the verb is granted.
    #[must_use]
    pub fn ha_call(&self) -> Option<&[HaTriple]> {
        self.ha_call.as_deref()
    }

    #[must_use]
    pub fn jobs(&self) -> &JobReach {
        &self.jobs
    }

    /// The components: one or more when the verb is granted.
    #[must_use]
    pub fn release(&self) -> Option<&[Releasable]> {
        self.release.as_deref()
    }

    /// The verbs that the file grants, in the order of the contract.
    #[must_use]
    pub fn granted(&self) -> Vec<Verb> {
        let held = [
            (Verb::Embed, self.embed),
            (Verb::HaCall, self.ha_call.is_some()),
            (Verb::Enqueue, self.jobs != JobReach::None),
            (
                Verb::JobStatus,
                matches!(self.jobs, JobReach::EnqueueAndStatus { .. }),
            ),
            (Verb::Release, self.release.is_some()),
        ];

        held.into_iter()
            .filter(|(_, granted)| *granted)
            .map(|(verb, _)| verb)
            .collect()
    }
}

sealed! {
    /// `sandbox` of a valid file.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Sandbox {
        cpus: Cpus,
        memory: Memory,
        max_resident_processes: ResidentProcs,
        image: SandboxFlavor,
    }
}

impl Sandbox {
    #[must_use]
    pub fn cpus(&self) -> Cpus {
        self.cpus
    }

    #[must_use]
    pub fn memory(&self) -> &Memory {
        &self.memory
    }

    #[must_use]
    pub fn max_resident_processes(&self) -> ResidentProcs {
        self.max_resident_processes
    }

    #[must_use]
    pub fn image(&self) -> SandboxFlavor {
        self.image
    }
}

/// One trigger of an autonomous family: exactly one of the three forms
/// (contract 01 §3.13).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Trigger {
    Cron(Cron),
    Webhook(WebhookName),
    /// `enqueue: true`: the `enqueue` verb of another family starts the
    /// session.
    Dispatch,
}

/// One call by name: a verb, or a tool of a server that the file grants
/// (contract 01 §3.11).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Call {
    Verb(Verb),
    /// The tool is the text of the file. For a server with the grant `all`,
    /// only the registry check proves that the server declares the tool.
    Tool {
        server: ServerName,
        tool: String,
    },
}

impl fmt::Display for Call {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Verb(verb) => f.write_str(verb.as_str()),
            Self::Tool { server, tool } => write!(f, "{server}{CALL_SEPARATOR}{tool}"),
        }
    }
}

/// One entry of `approval`: a call that needs the approval of the operator
/// (contract 01 §3.11).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApprovalEntry {
    /// `invoke_agent`: each delegate call.
    InvokeAgent,
    /// One verb or one tool.
    Call(Call),
    /// `<server>__*`: each tool of one server.
    Server(ServerName),
}

impl fmt::Display for ApprovalEntry {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvokeAgent => f.write_str(INVOKE_AGENT),
            Self::Call(call) => call.fmt(f),
            Self::Server(server) => write!(f, "{server}{CALL_SEPARATOR}*"),
        }
    }
}

sealed! {
    /// `quiet.daily` of a valid file. The zone is the text of the file. Only the
    /// host knows whether its time zone database holds the zone.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct DailyCall {
        call: Call,
        hour: LocalHour,
        zone: String,
        weekdays_only: bool,
    }
}

impl DailyCall {
    #[must_use]
    pub fn call(&self) -> &Call {
        &self.call
    }

    #[must_use]
    pub fn hour(&self) -> LocalHour {
        self.hour
    }

    #[must_use]
    pub fn zone(&self) -> &str {
        &self.zone
    }

    #[must_use]
    pub fn weekdays_only(&self) -> bool {
        self.weekdays_only
    }
}

sealed! {
    /// `quiet` of a valid file (contract 01 §3.15).
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Quiet {
        board: Option<ServerName>,
        daily: Option<DailyCall>,
        floor_hours: FloorHours,
    }
}

impl Quiet {
    #[must_use]
    pub fn board(&self) -> Option<&ServerName> {
        self.board.as_ref()
    }

    #[must_use]
    pub fn daily(&self) -> Option<&DailyCall> {
        self.daily.as_ref()
    }

    #[must_use]
    pub fn floor_hours(&self) -> FloorHours {
        self.floor_hours
    }
}

/// The kind of a valid file, with the fields that only that kind holds
/// (contract 01 §2). A `job` on an attended family is no value of this type.
///
/// A delegate is the name of another family, as the file wrote it. Only the
/// registry check proves that the family exists and is thin.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ByKind {
    Attended {
        delegates: Vec<String>,
    },
    /// A thin family has no delegate, so a chain has one hop at most.
    Thin {
        job: Option<JobTimeout>,
    },
    Autonomous {
        delegates: Vec<String>,
        /// One trigger or more.
        triggers: Vec<Trigger>,
        max_running_turns: Option<RunningTurns>,
        quiet: Option<Quiet>,
    },
}

impl ByKind {
    #[must_use]
    pub fn kind(&self) -> Kind {
        match self {
            Self::Attended { .. } => Kind::Attended,
            Self::Thin { .. } => Kind::Thin,
            Self::Autonomous { .. } => Kind::Autonomous,
        }
    }

    /// The delegates. A thin family has none.
    #[must_use]
    pub fn delegates(&self) -> &[String] {
        match self {
            Self::Attended { delegates } | Self::Autonomous { delegates, .. } => delegates,
            Self::Thin { .. } => &[],
        }
    }
}

sealed! {
    /// One `family.yaml` that obeys each rule that one file can show.
    ///
    /// Two kinds of rule are not in this type. A rule that needs another file of
    /// the registry: a delegate exists and is thin, a server declares a tool. A
    /// rule that needs the host: the router serves the model alias, a mount
    /// resolves to a path under an allowed root. `agent-family` checks them.
    #[derive(Debug, Clone, PartialEq, Deserialize)]
    #[serde(try_from = "RawFamily")]
    pub struct Family {
        name: FamilyName,
        description: Description,
        model: Model,
        files: Vec<Mount>,
        tools: BTreeMap<ServerName, ToolGrant>,
        verbs: Verbs,
        max_inflight_delegations: InflightCap,
        egress: Vec<EgressHost>,
        shell: bool,
        sandbox_tools: Vec<SandboxTool>,
        system_prompt: SystemPrompt,
        sandbox: Sandbox,
        skills: Vec<SkillName>,
        approval: Vec<ApprovalEntry>,
        by_kind: ByKind,
        warnings: Vec<Placed>,
    }
}

impl Family {
    #[must_use]
    pub fn name(&self) -> &FamilyName {
        &self.name
    }

    #[must_use]
    pub fn kind(&self) -> Kind {
        self.by_kind.kind()
    }

    #[must_use]
    pub fn description(&self) -> &Description {
        &self.description
    }

    #[must_use]
    pub fn model(&self) -> &Model {
        &self.model
    }

    #[must_use]
    pub fn files(&self) -> &[Mount] {
        &self.files
    }

    #[must_use]
    pub fn tools(&self) -> &BTreeMap<ServerName, ToolGrant> {
        &self.tools
    }

    #[must_use]
    pub fn verbs(&self) -> &Verbs {
        &self.verbs
    }

    #[must_use]
    pub fn max_inflight_delegations(&self) -> InflightCap {
        self.max_inflight_delegations
    }

    #[must_use]
    pub fn egress(&self) -> &[EgressHost] {
        &self.egress
    }

    #[must_use]
    pub fn shell(&self) -> bool {
        self.shell
    }

    #[must_use]
    pub fn sandbox_tools(&self) -> &[SandboxTool] {
        &self.sandbox_tools
    }

    #[must_use]
    pub fn system_prompt(&self) -> SystemPrompt {
        self.system_prompt
    }

    #[must_use]
    pub fn sandbox(&self) -> &Sandbox {
        &self.sandbox
    }

    #[must_use]
    pub fn skills(&self) -> &[SkillName] {
        &self.skills
    }

    #[must_use]
    pub fn approval(&self) -> &[ApprovalEntry] {
        &self.approval
    }

    #[must_use]
    pub fn by_kind(&self) -> &ByKind {
        &self.by_kind
    }

    /// The warnings of the conversion. A valid file can have warnings.
    #[must_use]
    pub fn warnings(&self) -> &[Placed] {
        &self.warnings
    }
}

// --- the conversion ---

/// The parts of a mount check that need the name and the other mounts of the
/// file: the platform fence, in both directions (contract 01 §5.5).
///
/// `path` has the form of a path. The answer is the message of the issue, or
/// `None` for a path that the file can mount. `agent-family` gives this
/// function the path that a mount resolves to on the host.
#[must_use]
pub fn mount_root_issue(raw: &RawFamily, path: &str) -> Option<String> {
    if !ALLOWED_ROOTS.iter().any(|root| is_under(path, root)) {
        return Some(format!(
            "'{path}' is under no allowed root; the roots are {}",
            ALLOWED_ROOTS.join(", ")
        ));
    }

    let inside = is_under(path, PLATFORM_ROOT);
    let allowlisted = PLATFORM_ALLOWLIST.contains(&raw.name.as_str());
    if inside && !allowlisted {
        return Some(format!(
            "'{path}' is under the platform fence; only {} may mount the platform root or hold \
             the 'release' verb",
            PLATFORM_ALLOWLIST.join(", ")
        ));
    }

    if !inside && allowlisted && !is_platform_mirror(raw, path) {
        return Some(format!(
            "'{}' reaches only the platform root, so it may not mount '{path}'; a family that \
             edits the platform holds no other mount",
            raw.name
        ));
    }

    None
}

/// Whether `path` is one of the three clones of the platform, and each mount
/// of it in the file is read-only (contract 01 §5.5 rule 6).
fn is_platform_mirror(raw: &RawFamily, path: &str) -> bool {
    PLATFORM_MIRRORS.contains(&path)
        && raw
            .files
            .iter()
            .filter(|mount| mount.path == path)
            .all(|mount| mount.mode == Mode::Ro.as_str())
}

/// Whether the Python pattern for a tool name takes `text`. The pattern has
/// no size limit, and `ids::ToolName` has one.
fn python_tool_name(text: &str) -> bool {
    let bytes = text.as_bytes();
    let tail = |byte: &u8| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-');

    bytes.first().is_some_and(u8::is_ascii_alphabetic) && bytes.iter().all(tail)
}

/// The message for a text that is no tool name. A text that only the size
/// limit of `ids::ToolName` refuses gets the rule of that type.
pub(crate) fn tool_name_issue(tool: &str, error: ToolNameError) -> String {
    if python_tool_name(tool) {
        return format!("'{tool}' is not a tool name; {error}");
    }

    format!("'{tool}' is not a tool name; use [A-Za-z][A-Za-z0-9_-]*")
}

fn slot(section: Section) -> Slot {
    Slot::of(section)
}

fn vet_name(raw: &RawFamily, out: &mut Issues) -> Option<FamilyName> {
    let at = slot(Section::Identity);
    let name = raw.name.parse::<FamilyName>();
    if name.is_err() {
        out.error(
            at,
            "name",
            format!(
                "'{}' is not a family name; use [a-z][a-z0-9-]{{1,30}}",
                raw.name
            ),
        );
    }

    if raw.name == PROBE_FAMILY {
        out.error(
            at.step(1),
            "name",
            format!(
                "'{PROBE_FAMILY}' is reserved for the gate scripts' invariant 9 probe, which \
                 writes a grant file under it and deletes it again; pick another name"
            ),
        );

        return None;
    }

    name.ok()
}

fn vet_kind(raw: &RawFamily, out: &mut Issues) -> Option<Kind> {
    let kind = Kind::parse(&raw.kind);
    if kind.is_none() {
        out.error(
            slot(Section::Identity).step(3),
            "kind",
            format!(
                "'{}' is not a kind; use one of {}",
                raw.kind,
                Kind::listed()
            ),
        );
    }

    kind
}

fn vet_description(raw: &RawFamily, out: &mut Issues) -> Option<Description> {
    match raw.description.parse::<Description>() {
        Ok(description) => Some(description),
        Err(DescriptionError { length }) => {
            out.error(
                slot(Section::Identity).step(4),
                "description",
                format!(
                    "description is {length} characters; it must be 1 to {DESCRIPTION_MAX} after \
                     whitespace collapse"
                ),
            );

            None
        }
    }
}

fn vet_model(raw: &RawFamily, out: &mut Issues) -> Option<Model> {
    let at = slot(Section::Model);
    let text = &raw.model.router;
    let router = match text.parse::<ModelAlias>() {
        Ok(router) => Some(router),
        Err(error) => {
            let msg = match error {
                ModelAliasError::Wildcard => format!(
                    "'{text}' holds a wildcard; a wildcard alias would grant every model LiteLLM \
                     serves"
                ),
                ModelAliasError::Grammar => {
                    format!("'{text}' is not a LiteLLM alias; use [a-z0-9][a-z0-9._/-]*")
                }
            };
            out.error(at, "model.router", msg);

            None
        }
    };
    let number = raw.model.budget_usd_per_day;
    let budget = DailyBudget::try_from(number).ok();
    if budget.is_none() {
        out.error(
            at.step(1),
            "model.budget_usd_per_day",
            format!(
                "{} is outside 0 to {}; the ceiling refuses a typed extra zero here rather than \
                 on a bill",
                python_float_text(number),
                python_float_text(BUDGET_USD_MAX)
            ),
        );
    }

    Some(Model {
        router: router?,
        budget_usd_per_day: budget?,
    })
}

fn vet_mount_path(raw: &RawFamily, path: &str, at: Slot, loc: &str, out: &mut Issues) -> bool {
    let loc = format!("{loc}.path");
    let error = path.parse::<MountPath>().err();
    let msg = match error {
        Some(MountPathError::Glob) => Some(format!(
            "'{path}' is a glob; name each directory, because a glob widens a family silently \
             when a new directory appears"
        )),
        Some(MountPathError::Relative) => Some(format!("'{path}' must be an absolute path")),
        Some(MountPathError::Segment) => {
            Some(format!("'{path}' must hold no '.', '..' or empty segment"))
        }
        Some(MountPathError::Character) => Some(format!(
            "'{path}' holds a character outside [A-Za-z0-9._/-]"
        )),
        Some(MountPathError::SessionStore) => Some(format!(
            "'{path}' is the session store; the runtime mounts it, and a family file never \
             declares it"
        )),
        Some(MountPathError::NoRoot) | None => mount_root_issue(raw, path),
    };
    match msg {
        Some(msg) => {
            out.error(at, loc, msg);

            false
        }
        None => true,
    }
}

fn vet_files(raw: &RawFamily, out: &mut Issues) -> Option<Vec<Mount>> {
    let mut mounts = Vec::new();
    let mut valid = true;
    let mut seen = BTreeSet::new();
    for (index, mount) in raw.files.iter().enumerate() {
        let at = slot(Section::Files).item(index);
        let loc = format!("files[{index}]");
        let twice = !seen.insert(mount.path.as_str());
        if twice {
            out.error(at, &loc, format!("'{}' is mounted twice", mount.path));
        }

        let mode = Mode::parse(&mount.mode);
        if mode.is_none() {
            out.error(
                at,
                format!("{loc}.mode"),
                format!("'{}' is not a mode; use 'ro' or 'rw'", mount.mode),
            );
        }

        let path_valid = vet_mount_path(raw, &mount.path, at, &loc, out);
        match (mount.path.parse::<MountPath>(), mode) {
            (Ok(path), Some(mode)) if path_valid && !twice => mounts.push(Mount { path, mode }),
            _ => valid = false,
        }
    }

    valid.then_some(mounts)
}

fn vet_named_tools(
    server: &str,
    tools: &[String],
    at: Slot,
    out: &mut Issues,
) -> Option<Vec<ToolName>> {
    let loc = format!("tools.{server}");
    let mut valid = true;
    if server == PLATFORM_SERVER {
        for (position, tool) in tools.iter().enumerate() {
            if PLATFORM_WITHHELD.contains(&tool.as_str()) {
                valid = false;
                out.error(
                    at.step(2).place(position),
                    format!("{loc}[{position}]"),
                    format!(
                        "'{tool}' is never granted from '{server}': a platform change lands only \
                         when the operator merges it (§5.5 rule 7)"
                    ),
                );
            }
        }
    }

    if tools.is_empty() {
        out.error(
            at.step(3),
            loc,
            "an empty list grants nothing; write no key instead",
        );

        return None;
    }

    let mut names = Vec::new();
    let mut seen = BTreeSet::new();
    for (position, tool) in tools.iter().enumerate() {
        let here = at.step(4).place(position);
        let loc = format!("{loc}[{position}]");
        if !seen.insert(tool.as_str()) {
            valid = false;
            out.error(here, &loc, format!("'{tool}' is granted twice"));
        }

        match tool.parse::<ToolName>() {
            Ok(name) => names.push(name),
            Err(error) => {
                valid = false;
                out.error(here, loc, tool_name_issue(tool, error));
            }
        }
    }

    valid.then_some(names)
}

fn vet_tools(
    raw: &RawFamily,
    kind: Option<Kind>,
    out: &mut Issues,
) -> Option<BTreeMap<ServerName, ToolGrant>> {
    let mut grants = BTreeMap::new();
    let mut valid = true;
    for (rank, (server, grant)) in raw.tools.iter().enumerate() {
        let at = slot(Section::Tools).item(rank);
        let loc = format!("tools.{server}");
        let name = server.parse::<ServerName>().ok();
        if name.is_none() {
            out.error(
                at,
                &loc,
                format!(
                    "'{server}' is not an MCP server name; use [a-z][a-z0-9-]{{1,30}}, because \
                     the PEP's call name is <server>__<tool>"
                ),
            );
        }

        let grant = match grant {
            RawToolGrant::Named(tools) => {
                vet_named_tools(server, tools, at, out).map(ToolGrant::Named)
            }
            RawToolGrant::Text(text) if text != ALL_TOOLS => {
                out.error(
                    at.step(3),
                    &loc,
                    format!("'{text}' must be a list of tool names or '{ALL_TOOLS}'"),
                );

                None
            }
            RawToolGrant::Text(_) if kind == Some(Kind::Autonomous) => {
                out.error(
                    at.step(3),
                    &loc,
                    format!("'{ALL_TOOLS}' is refused on an autonomous family; name each tool"),
                );

                None
            }
            RawToolGrant::Text(_) => Some(ToolGrant::All),
        };
        match (name, grant) {
            (Some(name), Some(grant)) => {
                grants.insert(name, grant);
            }
            _ => valid = false,
        }
    }

    valid.then_some(grants)
}

fn vet_ha_call(fence: &RawHaCall, out: &mut Issues) -> Option<Vec<HaTriple>> {
    let at = slot(Section::Verbs);
    if fence.allow.is_empty() {
        out.error(
            at,
            "verbs.ha_call.allow",
            "ha_call needs at least one {domain, service} triple",
        );

        return None;
    }

    let mut triples = Vec::new();
    let mut valid = true;
    for (position, triple) in fence.allow.iter().enumerate() {
        let loc = format!("verbs.ha_call.allow[{position}]");
        let domain = triple.domain.parse::<HaIdentifier>().ok();
        if domain.is_none() {
            out.error(
                at,
                format!("{loc}.domain"),
                format!("'{}' must match [a-z][a-z0-9_]*", triple.domain),
            );
        }

        let service = triple.service.parse::<HaIdentifier>().ok();
        if service.is_none() {
            out.error(
                at,
                format!("{loc}.service"),
                format!("'{}' must match [a-z][a-z0-9_]*", triple.service),
            );
        }

        let entity_id = match &triple.entity_id {
            None => Some(None),
            Some(text) => {
                let entity = text.parse::<HaEntityId>().ok();
                if entity.is_none() {
                    out.error(
                        at,
                        format!("{loc}.entity_id"),
                        format!("'{text}' must be domain.object_id"),
                    );
                }

                entity.map(Some)
            }
        };
        match (domain, service, entity_id) {
            (Some(domain), Some(service), Some(entity_id)) => triples.push(HaTriple {
                domain,
                service,
                entity_id,
            }),
            _ => valid = false,
        }
    }

    valid.then_some(triples)
}

fn vet_jobs(verbs: &RawVerbs, out: &mut Issues) -> Option<JobReach> {
    let at = slot(Section::Verbs);
    let targets = match &verbs.enqueue {
        None => Some(None),
        Some(fence) if fence.targets.is_empty() => {
            out.error(
                at.item(1),
                "verbs.enqueue.targets",
                "enqueue needs at least one target family",
            );

            None
        }
        Some(fence) => Some(Some(fence.targets.clone())),
    };
    if verbs.job_status.is_some() && verbs.enqueue.is_none() {
        out.error(
            at.item(2),
            "verbs.job_status",
            "job_status without enqueue can never match a record; its scope is the sessions this \
             family enqueued",
        );

        return None;
    }

    Some(match (targets?, verbs.job_status.is_some()) {
        (None, _) => JobReach::None,
        (Some(targets), false) => JobReach::Enqueue { targets },
        (Some(targets), true) => JobReach::EnqueueAndStatus { targets },
    })
}

fn vet_release(raw: &RawFamily, fence: &RawRelease, out: &mut Issues) -> Option<Vec<Releasable>> {
    let at = slot(Section::Verbs).item(3);
    let mut valid = true;
    if !PLATFORM_ALLOWLIST.contains(&raw.name.as_str()) {
        valid = false;
        out.error(
            at,
            "verbs.release",
            format!(
                "'{}' is outside the platform fence; only {} may hold the 'release' verb or \
                 mount the platform root",
                raw.name,
                PLATFORM_ALLOWLIST.join(", ")
            ),
        );
    }

    if fence.components.is_empty() {
        out.error(
            at,
            "verbs.release.components",
            "release needs at least one component name",
        );

        return None;
    }

    let mut components = Vec::new();
    for (position, text) in fence.components.iter().enumerate() {
        match Releasable::parse(text) {
            Some(component) => components.push(component),
            None => {
                valid = false;
                out.error(
                    at,
                    format!("verbs.release.components[{position}]"),
                    format!(
                        "'{text}' is not a releasable component; contract 06 §1 names {}",
                        Releasable::listed()
                    ),
                );
            }
        }
    }

    valid.then_some(components)
}

fn vet_verbs(raw: &RawFamily, out: &mut Issues) -> Option<Verbs> {
    let ha_call = match &raw.verbs.ha_call {
        None => Some(None),
        Some(fence) => vet_ha_call(fence, out).map(Some),
    };
    let jobs = vet_jobs(&raw.verbs, out);
    let release = match &raw.verbs.release {
        None => Some(None),
        Some(fence) => vet_release(raw, fence, out).map(Some),
    };

    Some(Verbs {
        embed: raw.verbs.embed.is_some(),
        ha_call: ha_call?,
        jobs: jobs?,
        release: release?,
    })
}

fn vet_delegates(raw: &RawFamily, kind: Option<Kind>, out: &mut Issues) -> Option<Vec<String>> {
    let at = slot(Section::Delegates);
    let mut valid = true;
    if kind == Some(Kind::Thin) && !raw.delegates.is_empty() {
        valid = false;
        out.error(
            at,
            "delegates",
            "a thin family's delegates must be empty, so a chain is at most one hop and cannot \
             loop",
        );
    }

    let mut seen = BTreeSet::new();
    for (position, target) in raw.delegates.iter().enumerate() {
        let here = at.item(position + 1);
        let loc = format!("delegates[{position}]");
        if !seen.insert(target.as_str()) {
            valid = false;
            out.error(here, &loc, format!("'{target}' is named twice"));
        }

        if *target == raw.name {
            valid = false;
            out.error(here, loc, "a family may not delegate to itself");
        }
    }

    valid.then(|| raw.delegates.clone())
}

fn vet_inflight(raw: &RawFamily, out: &mut Issues) -> Option<InflightCap> {
    let at = slot(Section::Inflight);
    let number = &raw.max_inflight_delegations;
    let Ok(cap) = InflightCap::try_from(number) else {
        out.error(
            at,
            "max_inflight_delegations",
            format!(
                "{number} is outside {} to {}",
                InflightCap::MIN,
                InflightCap::MAX
            ),
        );

        return None;
    };

    if i64::from(cap.get()) != DEFAULT_INFLIGHT && raw.delegates.is_empty() {
        out.warn(
            at,
            "max_inflight_delegations",
            format!(
                "'delegates' is empty, so there is no delegate call to bound; {number} changes \
                 nothing"
            ),
        );
    }

    Some(cap)
}

fn vet_egress(raw: &RawFamily, out: &mut Issues) -> Option<Vec<EgressHost>> {
    let mut hosts = Vec::new();
    let mut valid = true;
    for (position, entry) in raw.egress.iter().enumerate() {
        let said = match entry.parse::<EgressHost>() {
            Ok(host) => {
                hosts.push(host);
                continue;
            }
            Err(EgressHostError::IpLiteral) => {
                "is an IP literal; name a hostname, so an egress list can never quietly reach a \
                 LAN host"
            }
            Err(EgressHostError::Wildcard) => "holds a wildcard, which is ungrantable",
            Err(EgressHostError::BadPort) => "has a port outside 1 to 65535",
            Err(EgressHostError::BadHostname) => "is not a hostname or hostname:port",
        };
        valid = false;
        out.error(
            slot(Section::Egress).item(position),
            format!("egress[{position}]"),
            format!("'{entry}' {said}"),
        );
    }

    valid.then_some(hosts)
}

fn vet_sandbox_tools(raw: &RawFamily, out: &mut Issues) -> Option<Vec<SandboxTool>> {
    let at = slot(Section::Runtime);
    let mut tools: Vec<SandboxTool> = Vec::new();
    let mut valid = true;
    for (position, text) in raw.sandbox_tools.iter().enumerate() {
        let loc = format!("sandbox_tools[{position}]");
        if text == "bash" {
            valid = false;
            out.error(
                at,
                loc,
                "'bash' is never a sandbox tool; 'shell: true' is the only way to get one",
            );
            continue;
        }

        let Some(tool) = SandboxTool::parse(text) else {
            valid = false;
            out.error(
                at,
                loc,
                format!(
                    "'{text}' is not a sandbox tool; use one of {}",
                    SandboxTool::listed()
                ),
            );
            continue;
        };

        if tools.contains(&tool) {
            valid = false;
            out.error(at, loc, format!("'{text}' is listed twice"));
        }

        tools.push(tool);
    }

    valid.then_some(tools)
}

fn vet_system_prompt(raw: &RawFamily, out: &mut Issues) -> Option<SystemPrompt> {
    let mode = SystemPrompt::parse(&raw.system_prompt);
    if mode.is_none() {
        out.error(
            slot(Section::Runtime).step(1),
            "system_prompt",
            format!(
                "'{}' is not a system prompt mode; use one of {}",
                raw.system_prompt,
                SystemPrompt::listed()
            ),
        );
    }

    mode
}

fn vet_sandbox(raw: &RawFamily, out: &mut Issues) -> Option<Sandbox> {
    let at = slot(Section::Sandbox);
    let block = &raw.sandbox;
    let image = SandboxFlavor::parse(&block.image);
    if image.is_none() {
        out.error(
            at,
            "sandbox.image",
            format!(
                "'{}' is not an image flavor; use one of {}",
                block.image,
                SandboxFlavor::listed()
            ),
        );
    }

    let cpus = Cpus::try_from(&block.cpus).ok();
    if cpus.is_none() {
        out.error(
            at,
            "sandbox.cpus",
            format!("{} is outside {} to {}", block.cpus, Cpus::MIN, Cpus::MAX),
        );
    }

    let megabytes = memory_mb(&block.memory);
    let memory = match block.memory.parse::<Memory>() {
        Ok(memory) => Some(memory),
        Err(MemoryError::Grammar) => {
            out.error(
                at,
                "sandbox.memory",
                format!("'{}' must match [1-9][0-9]*[mg]", block.memory),
            );

            None
        }
        Err(MemoryError::Range) => {
            out.error(
                at,
                "sandbox.memory",
                format!("'{}' is outside 256m to 16g", block.memory),
            );

            None
        }
    };
    let number = &block.max_resident_processes;
    let Ok(resident) = ResidentProcs::try_from(number) else {
        out.error(
            at,
            "sandbox.max_resident_processes",
            format!(
                "{number} is outside {} to {}",
                ResidentProcs::MIN,
                ResidentProcs::MAX
            ),
        );

        return None;
    };

    if let Some(megabytes) = megabytes {
        let carried = megabytes.saturating_sub(PLAYPEN_MB) / PI_PROCESS_MB;
        let wanted = u128::from(resident.get());
        if wanted > carried {
            out.warn(
                at,
                "sandbox.max_resident_processes",
                format!(
                    "{wanted} held-open processes need about {} MB; '{}' carries about {carried}",
                    wanted * PI_PROCESS_MB + PLAYPEN_MB,
                    block.memory
                ),
            );
        }
    }

    Some(Sandbox {
        cpus: cpus?,
        memory: memory?,
        max_resident_processes: resident,
        image: image?,
    })
}

fn vet_skills(raw: &RawFamily, out: &mut Issues) -> Option<Vec<SkillName>> {
    let mut skills = Vec::new();
    let mut valid = true;
    let mut seen = BTreeSet::new();
    for (position, text) in raw.skills.iter().enumerate() {
        let at = slot(Section::Skills).item(position);
        let loc = format!("skills[{position}]");
        if !seen.insert(text.as_str()) {
            valid = false;
            out.error(at, &loc, format!("'{text}' is granted twice"));
        }

        match text.parse::<SkillName>() {
            Ok(skill) => skills.push(skill),
            Err(_) => {
                valid = false;
                out.error(
                    at,
                    loc,
                    format!("'{text}' is not a skill name; use [a-z][a-z0-9-]{{1,30}}"),
                );
            }
        }
    }

    valid.then_some(skills)
}

/// One call by name, when the file grants it as far as one file shows.
///
/// For a server with the grant `all`, the answer is `Some`: only the server
/// file says which tools the grant holds, and `agent-family` reads it.
fn granted_call(raw: &RawFamily, text: &str) -> Option<Call> {
    let Some((server, tool)) = text.split_once(CALL_SEPARATOR) else {
        let verb = Verb::parse(text)?;

        return raw
            .verbs
            .granted()
            .contains(&verb)
            .then_some(Call::Verb(verb));
    };

    let granted = raw.grants_all(server) || raw.tool_names(server).iter().any(|name| name == tool);
    if !granted {
        return None;
    }

    Some(Call::Tool {
        server: server.parse().ok()?,
        tool: tool.to_owned(),
    })
}

/// One entry of `approval`: the entry when the types can hold it, and the
/// message of its issue when the entry breaks a rule.
///
/// An entry with no issue can still have no value: its server is a key of
/// `tools` that is no server name. That key has its own issue.
fn vet_approval_entry(raw: &RawFamily, entry: &str) -> (Option<ApprovalEntry>, Option<String>) {
    let Some((server, tool)) = entry.split_once(CALL_SEPARATOR) else {
        if entry != INVOKE_AGENT {
            let call = granted_call(raw, entry).map(ApprovalEntry::Call);
            let msg = call
                .is_none()
                .then(|| format!("'{entry}' is not a verb this file grants"));

            return (call, msg);
        }

        if raw.delegates.is_empty() {
            let msg = "invoke_agent is granted by a non-empty 'delegates', which is empty";

            return (None, Some(msg.to_owned()));
        }

        return (Some(ApprovalEntry::InvokeAgent), None);
    };

    if !raw.tools.contains_key(server) {
        return (
            None,
            Some(format!("'{server}' is not a server this file grants")),
        );
    }

    let name = server.parse::<ServerName>().ok();
    if tool == "*" {
        return (name.map(ApprovalEntry::Server), None);
    }

    let granted = raw.grants_all(server) || raw.tool_names(server).iter().any(|name| name == tool);
    if !granted {
        return (
            None,
            Some(format!(
                "'{entry}' is not a tool this file grants from '{server}'"
            )),
        );
    }

    let call = name.map(|server| Call::Tool {
        server,
        tool: tool.to_owned(),
    });

    (call.map(ApprovalEntry::Call), None)
}

fn vet_approval(raw: &RawFamily, out: &mut Issues) -> Option<Vec<ApprovalEntry>> {
    let section = slot(Section::Approval);
    let mut entries = Vec::new();
    let mut valid = true;
    let mut covered: Vec<&str> = Vec::new();
    let mut seen = BTreeSet::new();
    for (position, entry) in raw.approval.iter().enumerate() {
        let loc = format!("approval[{position}]");
        if !seen.insert(entry.as_str()) {
            valid = false;
            out.error(
                section.item(position),
                loc,
                format!("'{entry}' is listed twice"),
            );
            continue;
        }

        if let Some((server, "*")) = entry.split_once(CALL_SEPARATOR)
            && raw.tools.contains_key(server)
        {
            covered.push(server);
        }

        let (vetted, msg) = vet_approval_entry(raw, entry);
        if let Some(msg) = msg {
            out.error(section.item(position), loc, msg);
        }

        match vetted {
            Some(vetted) => entries.push(vetted),
            None => valid = false,
        }
    }

    for (position, entry) in raw.approval.iter().enumerate() {
        let Some((server, tool)) = entry.split_once(CALL_SEPARATOR) else {
            continue;
        };

        if tool == "*" || !covered.contains(&server) {
            continue;
        }

        valid = false;
        out.error(
            section.item(usize::MAX).place(position),
            format!("approval[{position}]"),
            format!("'{entry}' overlaps '{server}{CALL_SEPARATOR}*'; keep one of them"),
        );
    }

    valid.then_some(entries)
}

fn vet_job(raw: &RawFamily, kind: Kind, out: &mut Issues) -> Option<Option<JobTimeout>> {
    let at = slot(Section::KindFields);
    let Some(job) = &raw.job else {
        return Some(None);
    };

    if kind != Kind::Thin {
        out.error(
            at,
            "job",
            format!("'job' is thin only; this family is '{kind}'"),
        );

        return None;
    }

    let text = &job.timeout;
    match text.parse::<JobTimeout>() {
        Ok(timeout) => {
            if duration_s(text).is_some_and(|seconds| seconds > DELEGATE_LIMIT_S) {
                out.warn(
                    at,
                    "job.timeout",
                    format!(
                        "'{text}' is past the PEP's {DELEGATE_LIMIT_S}s delegate limit; the \
                         caller gives up first"
                    ),
                );
            }

            Some(Some(timeout))
        }
        Err(JobTimeoutError::Grammar) => {
            out.error(
                at,
                "job.timeout",
                format!("'{text}' must match [1-9][0-9]*[smh]"),
            );

            None
        }
        Err(JobTimeoutError::Range) => {
            out.error(at, "job.timeout", format!("'{text}' is outside 1s to 1h"));

            None
        }
    }
}

fn vet_trigger(
    trigger: &RawTrigger,
    position: usize,
    dispatch_seen: &mut bool,
    out: &mut Issues,
) -> Option<Trigger> {
    let at = slot(Section::KindFields).step(1);
    let loc = format!("triggers[{position}]");
    match (&trigger.cron, &trigger.webhook, trigger.enqueue) {
        (Some(expression), None, None) => {
            let cron = expression.parse::<Cron>().ok();
            if cron.is_none() {
                out.error(
                    at,
                    format!("{loc}.cron"),
                    format!(
                        "'{expression}' is not a five-field cron expression or one of {}",
                        CRON_SHORTHANDS.join(", ")
                    ),
                );
            }

            cron.map(Trigger::Cron)
        }
        (None, Some(name), None) => {
            let webhook = name.parse::<WebhookName>().ok();
            if webhook.is_none() {
                out.error(
                    at,
                    format!("{loc}.webhook"),
                    format!("'{name}' must match [a-z][a-z0-9-]{{1,30}}"),
                );
            }

            webhook.map(Trigger::Webhook)
        }
        (None, None, Some(false)) => {
            out.error(
                at,
                format!("{loc}.enqueue"),
                "'enqueue: false' is not a trigger; absence is denial, so write no entry instead",
            );

            None
        }
        (None, None, Some(true)) => {
            let second = std::mem::replace(dispatch_seen, true);
            if second {
                out.error(
                    at,
                    loc,
                    "a second 'enqueue' trigger says the same thing twice; keep one",
                );
            }

            (!second).then_some(Trigger::Dispatch)
        }
        _ => {
            out.error(
                at,
                loc,
                "a trigger takes exactly one of 'cron', 'webhook' or 'enqueue'",
            );

            None
        }
    }
}

fn vet_triggers(raw: &RawFamily, kind: Kind, out: &mut Issues) -> Option<Vec<Trigger>> {
    let at = slot(Section::KindFields).step(1);
    if kind != Kind::Autonomous {
        if raw.triggers.is_some() {
            out.error(
                at,
                "triggers",
                format!("'triggers' is autonomous only; this family is '{kind}'"),
            );

            return None;
        }

        return Some(Vec::new());
    }

    let written = raw.triggers.as_deref().unwrap_or_default();
    if written.is_empty() {
        out.error(
            at,
            "triggers",
            "an autonomous family needs at least one trigger",
        );

        return None;
    }

    let mut dispatch_seen = false;
    let mut triggers = Vec::new();
    let mut valid = true;
    for (position, trigger) in written.iter().enumerate() {
        match vet_trigger(trigger, position, &mut dispatch_seen, out) {
            Some(trigger) => triggers.push(trigger),
            None => valid = false,
        }
    }

    valid.then_some(triggers)
}

fn vet_running_turns(
    raw: &RawFamily,
    kind: Kind,
    out: &mut Issues,
) -> Option<Option<RunningTurns>> {
    let at = slot(Section::KindFields).step(2);
    let Some(number) = &raw.max_running_turns else {
        return Some(None);
    };

    if kind != Kind::Autonomous {
        out.error(
            at,
            "max_running_turns",
            format!("'max_running_turns' is autonomous only; this family is '{kind}'"),
        );

        return None;
    }

    let turns = RunningTurns::try_from(number).ok();
    if turns.is_none() {
        out.error(
            at,
            "max_running_turns",
            format!(
                "{number} is outside {} to {}",
                RunningTurns::MIN,
                RunningTurns::MAX
            ),
        );
    }

    turns.map(Some)
}

fn vet_board(raw: &RawFamily, server: &str, out: &mut Issues) -> Option<ServerName> {
    let at = slot(Section::Quiet).step(3);
    if !raw.tools.contains_key(server) {
        out.error(
            at,
            "quiet.board",
            format!("'{server}' is not a server this file grants"),
        );

        return None;
    }

    let surveys = raw.grants_all(server)
        || raw
            .tool_names(server)
            .iter()
            .any(|tool| tool == SURVEY_TOOL);
    if !surveys {
        out.error(
            at,
            "quiet.board",
            format!("'{server}' does not grant '{SURVEY_TOOL}', which 'quiet.board' reads"),
        );

        return None;
    }

    server.parse().ok()
}

fn vet_daily(raw: &RawFamily, daily: &RawDaily, out: &mut Issues) -> Option<DailyCall> {
    let at = slot(Section::Quiet);
    let call = granted_call(raw, &daily.call);
    // A server name that is no server name is an error of `tools`, and the
    // Python validator writes no issue for the call.
    let named = daily
        .call
        .split_once(CALL_SEPARATOR)
        .is_some_and(|(server, tool)| {
            raw.grants_all(server) || raw.tool_names(server).iter().any(|name| name == tool)
        });
    if call.is_none() && !named {
        out.error(
            at.step(4),
            "quiet.daily.call",
            format!(
                "'{}' is not a verb or a '<server>{CALL_SEPARATOR}<tool>' this file grants",
                daily.call
            ),
        );
    }

    let hour = LocalHour::try_from(&daily.hour).ok();
    if hour.is_none() {
        out.error(
            at.step(5),
            "quiet.daily.hour",
            format!(
                "{} is outside {} to {}",
                daily.hour,
                LocalHour::MIN,
                LocalHour::MAX
            ),
        );
    }

    Some(DailyCall {
        call: call?,
        hour: hour?,
        zone: daily.zone.clone(),
        weekdays_only: daily.weekdays_only,
    })
}

fn vet_quiet(raw: &RawFamily, kind: Kind, out: &mut Issues) -> Option<Option<Quiet>> {
    let at = slot(Section::Quiet);
    let Some(quiet) = &raw.quiet else {
        return Some(None);
    };

    if kind != Kind::Autonomous {
        out.error(
            at,
            "quiet",
            format!("'quiet' is autonomous only; this family is '{kind}'"),
        );

        return None;
    }

    let has_cron = raw
        .triggers
        .iter()
        .flatten()
        .any(|trigger| trigger.cron.is_some());
    if !has_cron {
        out.warn(
            at.step(1),
            "quiet",
            "'quiet' checks cron firings, and this family has no cron trigger",
        );
    }

    let mut valid = true;
    let granted = raw.verbs.granted();
    if granted.contains(&Verb::Enqueue) && !granted.contains(&Verb::JobStatus) {
        valid = false;
        out.error(
            at.step(2),
            "quiet",
            format!(
                "'quiet' reads this family's jobs with '{}', which this file does not grant",
                Verb::JobStatus
            ),
        );
    }

    let board = match &quiet.board {
        None => Some(None),
        Some(server) => vet_board(raw, server, out).map(Some),
    };
    let daily = match &quiet.daily {
        None => Some(None),
        Some(daily) => vet_daily(raw, daily, out).map(Some),
    };
    let floor_hours = FloorHours::try_from(&quiet.floor_hours).ok();
    if floor_hours.is_none() {
        out.error(
            at.step(7),
            "quiet.floor_hours",
            format!(
                "{} is outside {} to {}",
                quiet.floor_hours,
                FloorHours::MIN,
                FloorHours::MAX
            ),
        );
    }

    let quiet = Quiet {
        board: board?,
        daily: daily?,
        floor_hours: floor_hours?,
    };

    valid.then_some(Some(quiet))
}

fn vet_by_kind(
    raw: &RawFamily,
    kind: Option<Kind>,
    delegates: Option<Vec<String>>,
    out: &mut Issues,
) -> Option<ByKind> {
    // The Python validator checks no field of a kind when the kind is no
    // kind.
    let kind = kind?;
    let job = vet_job(raw, kind, out);
    let triggers = vet_triggers(raw, kind, out);
    let max_running_turns = vet_running_turns(raw, kind, out);
    let quiet = vet_quiet(raw, kind, out);
    let (job, triggers, max_running_turns, quiet) = (job?, triggers?, max_running_turns?, quiet?);
    let delegates = delegates?;

    Some(match kind {
        Kind::Attended => ByKind::Attended { delegates },
        Kind::Thin => ByKind::Thin { job },
        Kind::Autonomous => ByKind::Autonomous {
            delegates,
            triggers,
            max_running_turns,
            quiet,
        },
    })
}

impl TryFrom<RawFamily> for Family {
    type Error = Refused;

    /// Checks each rule that one file decides (contract 01 §3, §5.4, §5.5).
    /// The conversion reads each field before it answers.
    fn try_from(raw: RawFamily) -> Result<Self, Refused> {
        let mut out = Issues::default();
        let name = vet_name(&raw, &mut out);
        let kind = vet_kind(&raw, &mut out);
        let description = vet_description(&raw, &mut out);
        let model = vet_model(&raw, &mut out);
        let files = vet_files(&raw, &mut out);
        let tools = vet_tools(&raw, kind, &mut out);
        let verbs = vet_verbs(&raw, &mut out);
        let delegates = vet_delegates(&raw, kind, &mut out);
        let max_inflight_delegations = vet_inflight(&raw, &mut out);
        let egress = vet_egress(&raw, &mut out);
        let sandbox_tools = vet_sandbox_tools(&raw, &mut out);
        let system_prompt = vet_system_prompt(&raw, &mut out);
        let sandbox = vet_sandbox(&raw, &mut out);
        let skills = vet_skills(&raw, &mut out);
        let approval = vet_approval(&raw, &mut out);
        let by_kind = vet_by_kind(&raw, kind, delegates, &mut out);
        let failed = out.has_error();
        let mut issues = out.into_entries();
        issues.sort_by_key(|placed| placed.slot);
        let family = (|| {
            Some(Self {
                name: name?,
                description: description?,
                model: model?,
                files: files?,
                tools: tools?,
                verbs: verbs?,
                max_inflight_delegations: max_inflight_delegations?,
                egress: egress?,
                shell: raw.shell,
                sandbox_tools: sandbox_tools?,
                system_prompt: system_prompt?,
                sandbox: sandbox?,
                skills: skills?,
                approval: approval?,
                by_kind: by_kind?,
                warnings: Vec::new(),
            })
        })();
        match family {
            Some(family) if !failed => Ok(Self {
                warnings: issues,
                ..family
            }),
            _ => Err(Refused::new(issues)),
        }
    }
}

// --- back to the raw form ---

fn raw_trigger(trigger: &Trigger) -> RawTrigger {
    match trigger {
        Trigger::Cron(cron) => RawTrigger {
            cron: Some(cron.as_str().to_owned()),
            ..RawTrigger::default()
        },
        Trigger::Webhook(name) => RawTrigger {
            webhook: Some(name.as_str().to_owned()),
            ..RawTrigger::default()
        },
        Trigger::Dispatch => RawTrigger {
            enqueue: Some(true),
            ..RawTrigger::default()
        },
    }
}

fn raw_quiet(quiet: &Quiet) -> RawQuiet {
    RawQuiet {
        board: quiet
            .board
            .as_ref()
            .map(|server| server.as_str().to_owned()),
        daily: quiet.daily.as_ref().map(|daily| RawDaily {
            call: daily.call.to_string(),
            hour: daily.hour.into(),
            zone: daily.zone.clone(),
            weekdays_only: daily.weekdays_only,
        }),
        floor_hours: quiet.floor_hours.into(),
    }
}

fn raw_verbs(verbs: &Verbs) -> RawVerbs {
    let status = matches!(verbs.jobs, JobReach::EnqueueAndStatus { .. });

    RawVerbs {
        embed: verbs.embed.then_some(RawNoFence {}),
        ha_call: verbs.ha_call.as_ref().map(|allow| RawHaCall {
            allow: allow
                .iter()
                .map(|triple| RawHaTriple {
                    domain: triple.domain.as_str().to_owned(),
                    service: triple.service.as_str().to_owned(),
                    entity_id: triple
                        .entity_id
                        .as_ref()
                        .map(|entity| entity.as_str().to_owned()),
                })
                .collect(),
        }),
        enqueue: (verbs.jobs != JobReach::None).then(|| RawEnqueue {
            targets: verbs.jobs.targets().to_vec(),
        }),
        job_status: status.then_some(RawNoFence {}),
        release: verbs.release.as_ref().map(|components| RawRelease {
            components: components
                .iter()
                .map(|component| component.as_str().to_owned())
                .collect(),
        }),
    }
}

impl From<&Family> for RawFamily {
    /// The file that the conversion took, field for field.
    fn from(family: &Family) -> Self {
        let (job, triggers, max_running_turns, quiet) = match &family.by_kind {
            ByKind::Attended { .. } => (None, None, None, None),
            ByKind::Thin { job } => (
                job.as_ref().map(|timeout| RawJob {
                    timeout: timeout.as_str().to_owned(),
                }),
                None,
                None,
                None,
            ),
            ByKind::Autonomous {
                triggers,
                max_running_turns,
                quiet,
                ..
            } => (
                None,
                Some(triggers.iter().map(raw_trigger).collect()),
                max_running_turns.map(RawInt::from),
                quiet.as_ref().map(raw_quiet),
            ),
        };

        Self {
            name: family.name.as_str().to_owned(),
            kind: family.kind().as_str().to_owned(),
            description: family.description.as_str().to_owned(),
            model: RawModel {
                router: family.model.router.as_str().to_owned(),
                budget_usd_per_day: family.model.budget_usd_per_day.get(),
            },
            files: family
                .files
                .iter()
                .map(|mount| RawMount {
                    path: mount.path.as_str().to_owned(),
                    mode: mount.mode.as_str().to_owned(),
                })
                .collect(),
            tools: family
                .tools
                .iter()
                .map(|(server, grant)| {
                    let grant = match grant {
                        ToolGrant::All => RawToolGrant::Text(ALL_TOOLS.to_owned()),
                        ToolGrant::Named(tools) => RawToolGrant::Named(
                            tools.iter().map(|tool| tool.as_str().to_owned()).collect(),
                        ),
                    };

                    (server.as_str().to_owned(), grant)
                })
                .collect(),
            verbs: raw_verbs(&family.verbs),
            delegates: family.by_kind.delegates().to_vec(),
            max_inflight_delegations: family.max_inflight_delegations.into(),
            egress: family
                .egress
                .iter()
                .map(|host| host.as_str().to_owned())
                .collect(),
            shell: family.shell,
            sandbox_tools: family
                .sandbox_tools
                .iter()
                .map(|tool| tool.as_str().to_owned())
                .collect(),
            system_prompt: family.system_prompt.as_str().to_owned(),
            sandbox: RawSandbox {
                cpus: family.sandbox.cpus.into(),
                memory: family.sandbox.memory.as_str().to_owned(),
                max_resident_processes: family.sandbox.max_resident_processes.into(),
                image: family.sandbox.image.as_str().to_owned(),
            },
            skills: family
                .skills
                .iter()
                .map(|skill| skill.as_str().to_owned())
                .collect(),
            approval: family.approval.iter().map(ToString::to_string).collect(),
            job,
            triggers,
            max_running_turns,
            quiet,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::str::FromStr;

    use super::{
        Cpus, Cron, DailyBudget, Description, EgressHost, EgressHostError, Family, FloorHours,
        HaEntityId, HaIdentifier, InflightCap, JobTimeout, JobTimeoutError, Kind, LocalHour,
        Memory, MemoryError, ModelAlias, ModelAliasError, MountPath, MountPathError, NO_REASON,
        Placed, RawFamily, RawInt, RawModel, RawToolGrant, RawTrigger, Refused, ResidentProcs,
        RunningTurns, Section, Severity, Slot, collapse, duration_s, is_under, memory_mb,
        python_float_text,
    };
    use crate::vectors::{self, Outcome};

    /// Each text of `accepted` parses, and each text of `refused` does not.
    fn check_tables<T: FromStr>(accepted: &[&str], refused: &[&str]) {
        for text in accepted {
            assert!(text.parse::<T>().is_ok(), "{text:?} is refused");
        }

        for text in refused {
            assert!(text.parse::<T>().is_err(), "{text:?} is accepted");
        }
    }

    fn thin(name: &str) -> RawFamily {
        RawFamily::with_defaults(
            name.to_owned(),
            "thin".to_owned(),
            "One written input.".to_owned(),
            RawModel {
                router: "fast".to_owned(),
                budget_usd_per_day: 1.0,
            },
        )
    }

    /// The location and the message of each issue of a file, in order.
    fn issues_of(raw: RawFamily) -> Vec<(Severity, String, String)> {
        let placed = match Family::try_from(raw) {
            Ok(family) => family.warnings().to_vec(),
            Err(refused) => refused.into_issues(),
        };

        placed
            .into_iter()
            .map(|placed| (placed.issue.severity, placed.issue.loc, placed.issue.msg))
            .collect()
    }

    #[test]
    fn a_refusal_with_no_error_gets_one_error() {
        let slot = Slot::of(Section::Sandbox);
        let warning = Placed::warning(slot, "sandbox.memory", "a warning");
        let error = Placed::error(slot, "sandbox.cpus", "an error");
        let added = Placed::error(Slot::of(Section::Identity), "<document>", NO_REASON);

        assert_eq!(
            Refused::new(Vec::new()).into_issues(),
            std::slice::from_ref(&added)
        );
        assert_eq!(
            Refused::new(vec![warning.clone()]).into_issues(),
            [warning.clone(), added]
        );
        assert_eq!(
            Refused::new(vec![warning.clone(), error.clone()]).into_issues(),
            [warning, error]
        );
    }

    /// What Python 3.13 answers for `repr(value)`.
    #[test]
    fn a_float_has_the_text_that_python_writes() {
        let cases = [
            (0.0_f64, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (1.5, "1.5"),
            (500.0, "500.0"),
            (1000.0, "1000.0"),
            (0.1, "0.1"),
            (0.0001, "0.0001"),
            (1e-05, "1e-05"),
            (1.5e-07, "1.5e-07"),
            (123_456_789.125, "123456789.125"),
            (1e15, "1000000000000000.0"),
            (9_999_999_999_999_998.0, "9999999999999998.0"),
            (1e16, "1e+16"),
            (1.234_567_890_123_456_6e17, "1.2345678901234566e+17"),
            (1e22, "1e+22"),
            (1e100, "1e+100"),
            (f64::MAX, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
            (-2.5, "-2.5"),
            (-1e22, "-1e+22"),
            (2.5e-05, "2.5e-05"),
            (500.01, "500.01"),
            (1.0 / 3.0, "0.3333333333333333"),
            (123_456.0, "123456.0"),
            (f64::INFINITY, "inf"),
            (f64::NEG_INFINITY, "-inf"),
            (f64::NAN, "nan"),
        ];
        for (value, wanted) in cases {
            assert_eq!(python_float_text(value), wanted);
        }
    }

    #[test]
    fn the_collapse_is_the_python_collapse() {
        assert_eq!(collapse("  a \t b\n\nc  "), "a b c");
        assert_eq!(collapse("a\u{a0}b\u{1c}c\u{2028}d"), "a b c d");
        assert_eq!(collapse(" \n "), "");
    }

    #[test]
    fn a_path_is_under_a_root_by_whole_segments() {
        assert!(is_under("/srv/agents/vault", "/srv/agents/vault"));
        assert!(is_under("/srv/agents/vault/a", "/srv/agents/vault"));
        assert!(!is_under("/srv/agents/vault-x", "/srv/agents/vault"));
        assert!(!is_under("/srv/agents", "/srv/agents/vault"));
    }

    #[test]
    fn a_raw_integer_holds_each_digit() {
        let large = RawInt::from_decimal("99999999999999999999999").unwrap();
        assert_eq!(large.to_string(), "99999999999999999999999");
        assert_eq!(large.to_i64(), None);
        assert_eq!(RawInt::from_decimal("-12"), Some(RawInt::from(-12)));
        assert_eq!(
            RawInt::from_decimal("0099999999999999999999999"),
            Some(large.clone())
        );
        for text in ["", "-", "1.0", "+1", "1e3", "\u{661}", "1\n"] {
            assert_eq!(RawInt::from_decimal(text), None, "{text:?}");
        }

        let negative = RawInt::from_decimal("-99999999999999999999999").unwrap();
        assert!(negative < RawInt::from(i64::MIN));
        assert!(RawInt::from(i64::MAX) < large);
        assert!(RawInt::from(1) < RawInt::from(2));
        assert!(negative < large);
    }

    #[test]
    fn a_bounded_number_takes_its_range_only() {
        let int = |value: i64| RawInt::from(value);
        assert_eq!(Cpus::try_from(&int(1)).map(Cpus::get), Ok(1));
        assert_eq!(Cpus::try_from(&int(8)).map(Cpus::get), Ok(8));
        assert!(Cpus::try_from(&int(0)).is_err());
        assert!(Cpus::try_from(&int(9)).is_err());
        assert!(Cpus::try_from(&int(-1)).is_err());
        assert!(Cpus::try_from(&RawInt::from_decimal("99999999999999999999999").unwrap()).is_err());
        assert!(ResidentProcs::try_from(&int(32)).is_ok());
        assert!(ResidentProcs::try_from(&int(33)).is_err());
        assert!(InflightCap::try_from(&int(8)).is_ok());
        assert!(InflightCap::try_from(&int(0)).is_err());
        assert!(RunningTurns::try_from(&int(8)).is_ok());
        assert!(RunningTurns::try_from(&int(9)).is_err());
        assert!(LocalHour::try_from(&int(0)).is_ok());
        assert!(LocalHour::try_from(&int(24)).is_err());
        assert!(FloorHours::try_from(&int(168)).is_ok());
        assert!(FloorHours::try_from(&int(169)).is_err());
        assert!(FloorHours::try_from(&int(0)).is_err());
    }

    #[test]
    fn a_closed_set_refuses_another_text() {
        assert_eq!(Kind::parse("thin"), Some(Kind::Thin));
        for text in ["Thin", "thin\n", "", "thin "] {
            assert_eq!(Kind::parse(text), None, "{text:?}");
        }

        assert_eq!(Kind::listed(), "attended, thin, autonomous");
    }

    #[test]
    fn a_description_has_1_to_200_characters_after_the_collapse() {
        let max = "x".repeat(200);
        let spaced = "x   ".repeat(100);
        check_tables::<Description>(&["a", &max, &spaced, " a \n b "], &["", "   ", "\n"]);
        assert!("x".repeat(201).parse::<Description>().is_err());
        assert_eq!(
            "x".repeat(201)
                .parse::<Description>()
                .map_err(|error| error.length),
            Err(201)
        );
    }

    #[test]
    fn a_model_alias_has_its_grammar() {
        check_tables::<ModelAlias>(
            &["fast", "a/b.c-d_e", "0", "agent-router"],
            &["", "Fast", "-fast", "fast\n", "a b", "\u{661}", "f\u{e9}"],
        );
        assert_eq!("a*".parse::<ModelAlias>(), Err(ModelAliasError::Wildcard));
        assert_eq!("A".parse::<ModelAlias>(), Err(ModelAliasError::Grammar));
    }

    #[test]
    fn a_budget_is_above_0_and_500_at_most() {
        for value in [1e-9, 1.0, 500.0] {
            assert!(DailyBudget::try_from(value).is_ok(), "{value}");
        }

        for value in [0.0, -1.0, 500.01, f64::INFINITY, f64::NAN] {
            assert!(DailyBudget::try_from(value).is_err(), "{value}");
        }
    }

    #[test]
    fn a_mount_path_has_its_grammar_and_its_roots() {
        check_tables::<MountPath>(
            &[
                "/srv/agents/vault",
                "/srv/agents/vault/",
                "/srv/agents/code/a.b_c-d",
                "/srv/agents/work/platform",
            ],
            &["/srv/agents/vault\n", "/srv/agents/vault/\u{661}"],
        );
        let refused = [
            ("/srv/agents/vault/*", MountPathError::Glob),
            ("/srv/agents/vault/a?", MountPathError::Glob),
            ("/srv/agents/vault/[ab]", MountPathError::Glob),
            ("srv/agents/vault", MountPathError::Relative),
            ("", MountPathError::Relative),
            ("/srv/agents/../etc", MountPathError::Segment),
            ("/srv/agents/vault/.", MountPathError::Segment),
            ("/srv/agents//vault", MountPathError::Segment),
            ("/srv/agents/vault/a b", MountPathError::Character),
            ("/srv/agents/sessions", MountPathError::SessionStore),
            ("/srv/agents/sessions/chat", MountPathError::SessionStore),
            ("/etc", MountPathError::NoRoot),
            ("/", MountPathError::NoRoot),
            ("/srv/agents/vault-other", MountPathError::NoRoot),
        ];
        for (text, error) in refused {
            assert_eq!(text.parse::<MountPath>(), Err(error), "{text:?}");
        }
    }

    #[test]
    fn a_home_assistant_name_has_its_grammar() {
        check_tables::<HaIdentifier>(
            &["notify", "a", "a_1"],
            &["", "Notify", "1a", "_a", "a-b", "a\n", "a\u{661}"],
        );
        check_tables::<HaEntityId>(
            &["light.kitchen", "a.1", "a_b.c_d", "a._"],
            &[
                "",
                "light",
                "light.",
                ".kitchen",
                "Light.kitchen",
                "a.b.c",
                "a.b\n",
                "a.\u{661}",
            ],
        );
    }

    #[test]
    fn an_egress_host_has_its_grammar() {
        check_tables::<EgressHost>(
            &[
                "example.com",
                "example.com:443",
                "example.com:65535",
                "EXAMPLE.com",
                "localhost",
                "1.2.3",
                "a-b.example",
                "example.com:00443",
                "example.com:000443",
            ],
            &["", "example.com\n", "example.com:443\n"],
        );
        let refused = [
            ("*.example.com", EgressHostError::Wildcard),
            ("192.0.2.10", EgressHostError::IpLiteral),
            ("192.0.2.10:443", EgressHostError::IpLiteral),
            ("999.999.999.999", EgressHostError::IpLiteral),
            ("::1", EgressHostError::IpLiteral),
            ("example.com:", EgressHostError::BadPort),
            ("example.com:0", EgressHostError::BadPort),
            ("example.com:65536", EgressHostError::BadPort),
            ("example.com:+1", EgressHostError::BadPort),
            ("example.com:\u{661}", EgressHostError::BadPort),
            ("example.com:99999999999999999999", EgressHostError::BadPort),
            ("https://example.com", EgressHostError::BadPort),
            ("example.com.", EgressHostError::BadHostname),
            ("a_b.example", EgressHostError::BadHostname),
            ("-a.example", EgressHostError::BadHostname),
            (":443", EgressHostError::BadHostname),
        ];
        for (text, error) in refused {
            assert_eq!(text.parse::<EgressHost>(), Err(error), "{text:?}");
        }
    }

    #[test]
    fn a_size_and_a_time_have_a_count_and_a_unit() {
        assert_eq!(memory_mb("2g"), Some(2048));
        assert_eq!(memory_mb("256m"), Some(256));
        assert_eq!(duration_s("90s"), Some(90));
        assert_eq!(duration_s("2m"), Some(120));
        assert_eq!(duration_s("1h"), Some(3600));
        assert_eq!(memory_mb(&format!("{}g", "9".repeat(60))), Some(u128::MAX));
        for text in [
            "", "g", "02g", "2G", "2", "2gb", "-2g", "2g\n", "\u{662}g", "2 g",
        ] {
            assert_eq!(memory_mb(text), None, "{text:?}");
            assert_eq!(duration_s(text), None, "{text:?}");
        }

        check_tables::<Memory>(&["256m", "16g", "16384m", "2g"], &[]);
        assert_eq!("255m".parse::<Memory>(), Err(MemoryError::Range));
        assert_eq!("17g".parse::<Memory>(), Err(MemoryError::Range));
        assert_eq!("2x".parse::<Memory>(), Err(MemoryError::Grammar));
        assert_eq!(
            "2g".parse::<Memory>().map(|memory| memory.megabytes()),
            Ok(2048)
        );
        check_tables::<JobTimeout>(&["1s", "120s", "60m", "1h"], &[]);
        assert_eq!("2h".parse::<JobTimeout>(), Err(JobTimeoutError::Range));
        assert_eq!("3601s".parse::<JobTimeout>(), Err(JobTimeoutError::Range));
        assert_eq!("0s".parse::<JobTimeout>(), Err(JobTimeoutError::Grammar));
        assert_eq!("soon".parse::<JobTimeout>(), Err(JobTimeoutError::Grammar));
        assert_eq!(
            "1h".parse::<JobTimeout>().map(|timeout| timeout.seconds()),
            Ok(3600)
        );
    }

    #[test]
    fn a_cron_expression_has_five_fields_or_a_short_form() {
        check_tables::<Cron>(
            &[
                "@hourly",
                "@daily",
                "@weekly",
                "0 6 * * 1-5",
                " 0  6 * *\t1 ",
                "*/15 0-6,22 1 1,7 1-5",
            ],
            &[
                "",
                "@yearly",
                "0 6 * *",
                "0 6 * * 1 2",
                "@hourly ",
                "0 6 * * mon",
                "0 \u{669} * * *",
                "*/\u{b2} * * * *",
                "? ? ? ? ?",
            ],
        );
    }

    #[test]
    fn a_minimal_file_is_a_family_with_each_default() {
        let family = Family::try_from(thin("oracle")).unwrap();
        assert_eq!(family.name().as_str(), "oracle");
        assert_eq!(family.kind(), Kind::Thin);
        assert_eq!(family.sandbox().cpus().get(), 2);
        assert_eq!(family.sandbox().memory().megabytes(), 2048);
        assert_eq!(family.max_inflight_delegations().get(), 2);
        assert!(family.warnings().is_empty());
        assert_eq!(RawFamily::from(&family), thin("oracle"));
    }

    #[test]
    fn one_conversion_reports_each_violation_in_the_order_of_the_contract() {
        let mut raw = thin("Bad_Name");
        raw.kind = "attended".to_owned();
        raw.description = String::new();
        raw.model.router = "Agent-*".to_owned();
        raw.model.budget_usd_per_day = 1000.0;
        raw.tools
            .insert("kagi".to_owned(), RawToolGrant::Named(Vec::new()));
        raw.delegates = vec!["a".to_owned(), "a".to_owned()];
        raw.max_inflight_delegations = RawInt::from(9);
        raw.egress = vec!["192.0.2.10".to_owned()];
        raw.sandbox_tools = vec!["bash".to_owned()];
        raw.sandbox.memory = "2x".to_owned();
        raw.skills = vec!["Bad_Skill".to_owned()];
        raw.approval = vec!["embed".to_owned()];
        raw.triggers = Some(vec![RawTrigger::default()]);
        let locs: Vec<String> = issues_of(raw).into_iter().map(|(_, loc, _)| loc).collect();
        assert_eq!(
            locs,
            [
                "name",
                "description",
                "model.router",
                "model.budget_usd_per_day",
                "tools.kagi",
                "delegates[1]",
                "max_inflight_delegations",
                "egress[0]",
                "sandbox_tools[0]",
                "sandbox.memory",
                "skills[0]",
                "approval[0]",
                "triggers",
            ]
        );
    }

    #[test]
    fn a_warning_does_not_refuse_a_file() {
        let mut raw = thin("oracle");
        raw.max_inflight_delegations = RawInt::from(4);
        raw.sandbox.memory = "256m".to_owned();
        let family = Family::try_from(raw).unwrap();
        let warnings: Vec<&str> = family
            .warnings()
            .iter()
            .map(|placed| placed.issue.loc.as_str())
            .collect();
        assert_eq!(
            warnings,
            ["max_inflight_delegations", "sandbox.max_resident_processes"]
        );
        assert!(
            family
                .warnings()
                .iter()
                .all(|placed| placed.issue.severity == Severity::Warning)
        );
    }

    #[test]
    fn the_reserved_name_is_refused() {
        let issues = issues_of(thin("gate-probe"));
        assert_eq!(issues.len(), 1);
        assert!(issues[0].2.starts_with("'gate-probe' is reserved"));
    }

    /// The surfaces whose accepted vectors hold a parsed family file.
    const SURFACES: [&str; 2] = ["family_file", "family_file.host"];

    /// Where the Rust code stops on a vector that the Python code accepts.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Stop {
        /// The raw type cannot hold the value of the vector.
        NoRawValue,
        /// The stance `stricter` of `rust/AGENTS.md`: an id type of `ids`
        /// refuses one text of the file, so the conversion refuses the file.
        Stricter,
    }

    /// The vectors of [`SURFACES`] that the Python code accepts and that
    /// this type does not hold, with the contract section.
    const DEVIATIONS: [(&str, &str, Stop, &str); 2] = [
        (
            "family_file",
            "yaml-escape-surrogate",
            // The Python model holds a string with one lone surrogate. A
            // Rust `String` cannot hold that value.
            Stop::NoRawValue,
            "contract 01 §1: the file is UTF-8 text",
        ),
        (
            "family_file",
            "long-tool-name",
            // `agent_family` has no limit for a tool name. `ids::ToolName`
            // takes the limit of 64 bytes of the strictest Python copy.
            Stop::Stricter,
            "contract 01 §3.4: no limit for the size of a tool name",
        ),
    ];

    /// Each family file that the Python validator accepts is a `Family`, and
    /// the `Family` holds each field as the Python model holds it. The crate
    /// `agent-family` compares the report of each vector.
    #[test]
    fn each_accepted_vector_is_a_family_with_the_python_fields() {
        let mut deviations = 0;
        for name in SURFACES {
            let surface = vectors::surface(name);
            for vector in &surface.vectors {
                let Some(value) = vector
                    .value()
                    .filter(|_| vector.result == Outcome::Accepted)
                else {
                    continue;
                };

                let id = &vector.id;
                let raw = serde_json::from_value::<RawFamily>(value.clone());
                let stop = DEVIATIONS
                    .iter()
                    .find(|(surface, vector, _, _)| *surface == name && vector == id)
                    .map(|(_, _, stop, _)| *stop);
                if stop == Some(Stop::NoRawValue) {
                    assert!(raw.is_err(), "{name} {id}: the row names no difference");
                    deviations += 1;
                    continue;
                }

                let raw = raw.unwrap_or_else(|error| panic!("{name} {id}: {error}"));
                assert_eq!(&serde_json::to_value(&raw).unwrap(), value, "{name} {id}");
                let family = Family::try_from(raw);
                if stop == Some(Stop::Stricter) {
                    assert!(family.is_err(), "{name} {id}: the row names no difference");
                    deviations += 1;
                    continue;
                }

                let family = family.unwrap_or_else(|refused| {
                    panic!("{name} {id}: {refused}");
                });
                let back = serde_json::to_value(RawFamily::from(&family)).unwrap();
                assert_eq!(&back, value, "{name} {id}");
            }
        }

        assert_eq!(deviations, DEVIATIONS.len(), "a row names no vector");
    }
}
