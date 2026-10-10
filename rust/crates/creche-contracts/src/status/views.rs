//! One view of the status document for each of its five readers.
//!
//! A view is what one reader takes from a document. Each view here takes what
//! its Python reader takes, on each document of the `status.` surfaces: the
//! port of a reader then keeps the behavior of that reader. A view reads a
//! [`RawStatus`], because each Python reader also uses a document that the
//! valid type refuses. `StatusDocument::raw` gives the raw form of a valid
//! document.
//!
//! The five readers do not agree on each document. The test of this module
//! holds a table of the documents on which two readers differ.
//!
//! A view is a record of what a reader took. Only a reader function of this
//! module builds one. Each field of a view is private, and an accessor with
//! the name of the field gives its value.
//!
//! This module also holds the staleness rule of contract 05: [`Age`],
//! [`Freshness`] and [`STALE_AFTER_SECONDS`]. A view reads a time text with
//! the reader of [`Timestamp`]. A text that this reader refuses is no time,
//! and a file with no time is stale.

use std::collections::BTreeSet;

use super::json::Integer;
use super::raw::{RawFault, RawSandbox, RawStatus, ReadError, Reader};
use super::words::{FamilyState, Health, Kind, SandboxLifecycle};
use crate::ids::{FamilyName, SandboxName};
use crate::slot::Slot;
use crate::time::Timestamp;

/// The word of the state `invalid` in a file.
const STATE_INVALID: &str = "invalid";

/// The word of the kinds that the doors compare a document with.
const KIND_ATTENDED: &str = "attended";
const KIND_AUTONOMOUS: &str = "autonomous";

/// The words of the two sandbox states that the terminal door looks for.
const SANDBOX_READY: &str = "ready";
const SANDBOX_DRAINING: &str = "draining";

/// The steps of contract 05 §3.4 that create, switch or destroy a sandbox.
const SWITCH_STEPS: [&str; 3] = ["create_sandbox", "switch_sandbox", "destroy_sandbox"];

/// Each object of a list of the document, in order.
fn objects<T>(slot: &Slot<Vec<Option<T>>>) -> impl Iterator<Item = &T> {
    slot.value().into_iter().flatten().flatten()
}

/// Whether the document says that no revision of the family was valid.
fn never_valid(raw: &RawStatus) -> bool {
    raw.validation
        .value()
        .is_some_and(|validation| validation.never_valid.is_true())
}

/// An integer that is zero or more.
fn not_negative(slot: &Slot<Integer>) -> Option<Integer> {
    slot.value()
        .filter(|integer| !integer.is_negative())
        .cloned()
}

// --- the staleness rule ---

/// A file whose `written_at` is older than this is stale: contract 05 §2
/// rule 5 for a status document, and §3.3.1 rule 7 for a fault file.
pub const STALE_AFTER_SECONDS: i64 = 90;

const MICROS_PER_SECOND: i64 = 1_000_000;

/// How old a file is: zero or more microseconds. Only [`age_at`] builds a
/// value.
///
/// ```
/// use creche_contracts::status::views::{Age, Freshness, age_at};
/// use creche_contracts::time::Timestamp;
///
/// let written: Timestamp = "2031-04-18T06:42:35Z".parse()?;
/// let now: Timestamp = "2031-04-18T06:44:06Z".parse()?;
/// let age: Age = age_at(written, now);
/// assert_eq!(age.whole_seconds(), 91);
/// assert_eq!(age.freshness(), Freshness::Stale);
/// # Ok::<(), creche_contracts::time::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw number:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{Age, Freshness, age_at};
/// use creche_contracts::time::Timestamp;
///
/// let age = Age { micros: -1 };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Age {
    micros: i64,
}

impl Age {
    /// The age in whole seconds. A part of a second is cut off.
    #[must_use]
    pub fn whole_seconds(self) -> i64 {
        self.micros / MICROS_PER_SECOND
    }

    /// The age in seconds, as the nearest `f64`.
    #[must_use]
    pub fn seconds(self) -> f64 {
        // The decimal text has the exact value, and `parse` gives the nearest
        // float. Python divides two integers to the same result.
        let text = format!(
            "{}.{:06}",
            self.whole_seconds(),
            self.micros % MICROS_PER_SECOND
        );

        text.parse().unwrap_or(f64::INFINITY)
    }

    /// Whether a file of this age is stale: older than
    /// [`STALE_AFTER_SECONDS`], and not equal to it.
    #[must_use]
    pub fn freshness(self) -> Freshness {
        if self.micros > STALE_AFTER_SECONDS * MICROS_PER_SECOND {
            Freshness::Stale
        } else {
            Freshness::Fresh
        }
    }
}

/// Whether a reader can take a file as the state now.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Freshness {
    /// The writer wrote the file 90 seconds ago or less.
    Fresh,
    /// The writer wrote the file more than 90 seconds ago, or the file has
    /// no time that a reader can read. The writer is not running.
    Stale,
}

/// How old a file with this `written_at` is at `now`. A time after `now` has
/// the age zero: the clocks of two processes can differ.
#[must_use]
pub fn age_at(written_at: Timestamp, now: Timestamp) -> Age {
    let micros = now.unix_micros().saturating_sub(written_at.unix_micros());

    Age {
        micros: micros.max(0),
    }
}

/// Whether a file with this `written_at` is stale at `now`.
#[must_use]
pub fn freshness_at(written_at: Timestamp, now: Timestamp) -> Freshness {
    age_at(written_at, now).freshness()
}

// CONTRACT-QUESTION: contract 05 §2.1 names RFC 3339 for each time of a file
// and gives no grammar. The Python readers take each text that
// `datetime.fromisoformat` takes, and three of them read a time with no UTC
// offset as UTC. Each reader of the `status` module takes the grammar of
// `Timestamp`: the `date-time` of RFC 3339, section 5.6. It reads each other
// text as no time, and the file is then stale. To take a further form costs
// the reader of `Timestamp`, which each module of the workspace uses.
/// Whether a file with this `written_at` is stale at `now`. A file with no
/// time that a reader can read is stale.
#[must_use]
pub fn freshness(written_at: Option<Timestamp>, now: Timestamp) -> Freshness {
    written_at.map_or(Freshness::Stale, |written| freshness_at(written, now))
}

// --- attendance ---

/// The epoch that `attendance` uses when a document has none.
const FIRST_EPOCH: u64 = 1;

/// One sandbox that `attendance` takes from a document. Only [`attendance`]
/// builds a value.
///
/// ```
/// use creche_contracts::status::views::{AttendanceSandbox, read_attendance};
///
/// let bytes = br#"{"sandboxes": [{"id": "chat-s1", "state": "ready"}]}"#;
/// let status = read_attendance(bytes, &"chat".parse().unwrap()).unwrap();
/// let sandbox: &AttendanceSandbox = &status.sandboxes()[0];
/// assert_eq!(sandbox.id().as_str(), "chat-s1");
/// assert_eq!(sandbox.supervisor_env(), "");
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{AttendanceSandbox, read_attendance};
///
/// let bytes = br#"{"sandboxes": [{"id": "chat-s1", "state": "ready"}]}"#;
/// let status = read_attendance(bytes, &"chat".parse().unwrap()).unwrap();
/// let sandbox: &AttendanceSandbox = &status.sandboxes()[0];
/// let with_path = AttendanceSandbox { supervisor_env: String::from("/env"), ..sandbox.clone() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttendanceSandbox {
    id: SandboxName,
    state: SandboxLifecycle,
    supervisor_env: String,
}

impl AttendanceSandbox {
    /// The name of the sandbox.
    #[must_use]
    pub fn id(&self) -> &SandboxName {
        &self.id
    }

    /// The lifecycle state.
    #[must_use]
    pub fn state(&self) -> SandboxLifecycle {
        self.state
    }

    /// The path of the `supervisor.env`. Empty when the row has none.
    #[must_use]
    pub fn supervisor_env(&self) -> &str {
        &self.supervisor_env
    }
}

/// What `attendance` takes from one status document. Only [`attendance`]
/// builds a value.
///
/// ```
/// use creche_contracts::status::views::{AttendanceStatus, read_attendance};
///
/// let bytes = br#"{"kind": "attended", "triggers": {"enqueue": true}}"#;
/// let status: AttendanceStatus = read_attendance(bytes, &"chat".parse().unwrap()).unwrap();
/// assert_eq!(status.family().as_str(), "chat");
/// assert_eq!(status.state(), None);
/// assert!(status.accepts_dispatch());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{AttendanceStatus, read_attendance};
///
/// let bytes = br#"{"kind": "attended", "triggers": {"enqueue": true}}"#;
/// let status: AttendanceStatus = read_attendance(bytes, &"chat".parse().unwrap()).unwrap();
/// let status = AttendanceStatus { accepts_dispatch: false, ..status };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttendanceStatus {
    family: FamilyName,
    kind: Option<Kind>,
    state: Option<FamilyState>,
    written_at: Option<Timestamp>,
    config_rev: String,
    epoch: Integer,
    never_valid: bool,
    blocking_fault: Option<String>,
    fault_codes: BTreeSet<String>,
    sandboxes: Vec<AttendanceSandbox>,
    max_running_turns: Option<Integer>,
    job_timeout_s: Option<Integer>,
    accepts_dispatch: bool,
}

impl AttendanceStatus {
    /// The family that the caller asked for. The reader does not read the
    /// `family` field of the document.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The kind. `None` when the document has no kind, or has an unknown
    /// word. The reader has no default kind: a default opens the family to
    /// the doors of that kind.
    #[must_use]
    pub fn kind(&self) -> Option<Kind> {
        self.kind
    }

    /// The state. `None` when the document has no state, or has an unknown
    /// word.
    #[must_use]
    pub fn state(&self) -> Option<FamilyState> {
        self.state
    }

    /// When `caregiver` wrote the document. `None` when the reader cannot
    /// read the field.
    #[must_use]
    pub fn written_at(&self) -> Option<Timestamp> {
        self.written_at
    }

    /// The revision of the family config mount. Empty when the document has
    /// none.
    #[must_use]
    pub fn config_rev(&self) -> &str {
        &self.config_rev
    }

    /// The epoch of the credentials. An epoch that is missing, negative or
    /// not an integer reads as 1.
    #[must_use]
    pub fn epoch(&self) -> &Integer {
        &self.epoch
    }

    /// Whether `validation.never_valid` is `true`.
    #[must_use]
    pub fn never_valid(&self) -> bool {
        self.never_valid
    }

    /// The code of the first fault with `blocks_turns: true` and a code.
    #[must_use]
    pub fn blocking_fault(&self) -> Option<&str> {
        self.blocking_fault.as_deref()
    }

    /// The code of each fault that has one.
    #[must_use]
    pub fn fault_codes(&self) -> &BTreeSet<String> {
        &self.fault_codes
    }

    /// Each sandbox row with a sandbox name and a known state.
    #[must_use]
    pub fn sandboxes(&self) -> &[AttendanceSandbox] {
        &self.sandboxes
    }

    /// `limits.max_running_turns`, when it is an integer of zero or more.
    #[must_use]
    pub fn max_running_turns(&self) -> Option<&Integer> {
        self.max_running_turns.as_ref()
    }

    /// `limits.job_timeout_s`, when it is an integer of zero or more.
    #[must_use]
    pub fn job_timeout_s(&self) -> Option<&Integer> {
        self.job_timeout_s.as_ref()
    }

    /// Whether `triggers.enqueue` is `true`.
    #[must_use]
    pub fn accepts_dispatch(&self) -> bool {
        self.accepts_dispatch
    }
}

/// The view of `attendance`: `attendance.family_status.StatusReader.read`.
#[must_use]
pub fn attendance(raw: &RawStatus, family: &FamilyName) -> AttendanceStatus {
    let faults = || objects(&raw.faults);
    let code_of = |fault: &RawFault| fault.code.value().cloned();
    let limits = raw.limits.value();

    AttendanceStatus {
        family: family.clone(),
        kind: raw.kind.text().parse().ok(),
        state: raw.state.text().parse().ok(),
        written_at: raw.written_at.text().parse().ok(),
        config_rev: raw.config_rev.text().to_owned(),
        epoch: raw
            .credentials
            .value()
            .and_then(|credentials| not_negative(&credentials.epoch))
            .unwrap_or(Integer::from(FIRST_EPOCH)),
        never_valid: never_valid(raw),
        blocking_fault: faults()
            .filter(|fault| fault.blocks_turns.is_true())
            .find_map(code_of),
        fault_codes: faults().filter_map(code_of).collect(),
        sandboxes: objects(&raw.sandboxes)
            .filter_map(attendance_sandbox)
            .collect(),
        max_running_turns: limits.and_then(|limits| not_negative(&limits.max_running_turns)),
        job_timeout_s: limits.and_then(|limits| not_negative(&limits.job_timeout_s)),
        accepts_dispatch: raw
            .triggers
            .value()
            .is_some_and(|triggers| triggers.enqueue.is_true()),
    }
}

fn attendance_sandbox(row: &RawSandbox) -> Option<AttendanceSandbox> {
    Some(AttendanceSandbox {
        id: row.id.value()?.parse().ok()?,
        state: row.state.value()?.parse().ok()?,
        supervisor_env: row.supervisor_env.text().to_owned(),
    })
}

/// Reads the bytes of one `status.json` as `attendance` reads them.
///
/// # Errors
///
/// [`ReadError`] when `attendance` does not use the file.
pub fn read_attendance(bytes: &[u8], family: &FamilyName) -> Result<AttendanceStatus, ReadError> {
    Ok(attendance(
        &RawStatus::read(bytes, Reader::Attendance)?,
        family,
    ))
}

// --- the noticeboard ---

/// The most characters of one text that the noticeboard shows.
pub const TEXT_CHARS_MAX: usize = 500;

/// The most sandboxes and the most faults that the noticeboard shows. The
/// cap counts each item of the list, also an item that is not an object.
pub const ROWS_MAX: usize = 20;

/// The count of characters of an image digest that the noticeboard shows.
pub const DIGEST_CHARS: usize = 19;

/// The first `count` characters of `text`.
fn first_chars(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// A text field as the noticeboard shows it: 500 characters at most.
fn shown(slot: &Slot<String>) -> String {
    first_chars(slot.text(), TEXT_CHARS_MAX)
}

/// A time field as the noticeboard reads it: the shown text, as a time.
fn moment(slot: &Slot<String>) -> Option<Timestamp> {
    shown(slot).parse().ok()
}

fn any_integer(slot: &Slot<Integer>) -> Integer {
    slot.value().cloned().unwrap_or(Integer::from(0))
}

/// One row of `sandboxes` on the noticeboard. Only [`noticeboard`] builds a
/// value.
///
/// ```
/// use creche_contracts::status::views::{SandboxRow, read_noticeboard};
///
/// let bytes = br#"{"sandboxes": [{"id": "chat-s1", "image": "sha256:0123456789abcdef"}]}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let row: &SandboxRow = &family.sandboxes()[0];
/// assert_eq!(row.id(), "chat-s1");
/// assert_eq!(row.image(), "sha256:0123456789ab");
/// assert!(!row.has_supervisor_env());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{SandboxRow, read_noticeboard};
///
/// let bytes = br#"{"sandboxes": [{"id": "chat-s1", "image": "sha256:0123456789abcdef"}]}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let row: &SandboxRow = &family.sandboxes()[0];
/// let with_path = SandboxRow { has_supervisor_env: true, ..row.clone() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxRow {
    id: String,
    state: String,
    power: String,
    image: String,
    cpus: Integer,
    memory: String,
    created_at: String,
    ready_at: String,
    channel: String,
    has_supervisor_env: bool,
}

impl SandboxRow {
    /// The `id` text. The noticeboard does not check it.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    /// The `state` text.
    #[must_use]
    pub fn state(&self) -> &str {
        &self.state
    }

    /// The `power` text.
    #[must_use]
    pub fn power(&self) -> &str {
        &self.power
    }

    /// The first 19 characters of the image.
    #[must_use]
    pub fn image(&self) -> &str {
        &self.image
    }

    /// The count of CPUs. 0 when the row has none.
    #[must_use]
    pub fn cpus(&self) -> &Integer {
        &self.cpus
    }

    /// The `memory` text.
    #[must_use]
    pub fn memory(&self) -> &str {
        &self.memory
    }

    /// The `created_at` text.
    #[must_use]
    pub fn created_at(&self) -> &str {
        &self.created_at
    }

    /// The `ready_at` text.
    #[must_use]
    pub fn ready_at(&self) -> &str {
        &self.ready_at
    }

    /// The `channel` text.
    #[must_use]
    pub fn channel(&self) -> &str {
        &self.channel
    }

    /// Whether the row has a `supervisor_env` text that is not empty.
    #[must_use]
    pub fn has_supervisor_env(&self) -> bool {
        self.has_supervisor_env
    }
}

/// One fault on the noticeboard. Only [`noticeboard`] builds a value.
///
/// ```
/// use creche_contracts::status::views::{FaultRow, read_noticeboard};
///
/// let bytes = br#"{"faults": [{"code": "grants_stale", "message": "no grant file"}]}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let fault: &FaultRow = &family.faults()[0];
/// assert_eq!(fault.code(), "grants_stale");
/// assert_eq!(fault.message(), "no grant file");
/// assert!(!fault.blocks_turns());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{FaultRow, read_noticeboard};
///
/// let bytes = br#"{"faults": [{"code": "grants_stale", "message": "no grant file"}]}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let fault: &FaultRow = &family.faults()[0];
/// let blocking = FaultRow { blocks_turns: true, ..fault.clone() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultRow {
    code: String,
    blocks_turns: bool,
    since: String,
    source: String,
    stale: bool,
    message: String,
    sandbox: String,
}

impl FaultRow {
    /// The `code` text.
    #[must_use]
    pub fn code(&self) -> &str {
        &self.code
    }

    /// Whether `blocks_turns` is `true`.
    #[must_use]
    pub fn blocks_turns(&self) -> bool {
        self.blocks_turns
    }

    /// The `since` text.
    #[must_use]
    pub fn since(&self) -> &str {
        &self.since
    }

    /// The `source` text.
    #[must_use]
    pub fn source(&self) -> &str {
        &self.source
    }

    /// Whether `stale` is `true`.
    #[must_use]
    pub fn stale(&self) -> bool {
        self.stale
    }

    /// The `message` text of the detail.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The `sandbox` text of the detail.
    #[must_use]
    pub fn sandbox(&self) -> &str {
        &self.sandbox
    }
}

/// The `spend` block on the noticeboard. Only [`noticeboard`] builds a value.
///
/// ```
/// use creche_contracts::status::views::{SpendRow, read_noticeboard};
///
/// let bytes = br#"{"spend": {"spend_usd": 1.5, "window": "day"}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let spend: &SpendRow = family.spend().unwrap();
/// assert_eq!(spend.spend_usd(), Some(1.5));
/// assert_eq!(spend.budget_usd(), None);
/// assert!(spend.stale());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{SpendRow, read_noticeboard};
///
/// let bytes = br#"{"spend": {"spend_usd": 1.5, "window": "day"}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let spend: &SpendRow = family.spend().unwrap();
/// let fresh = SpendRow { stale: false, ..spend.clone() };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct SpendRow {
    spend_usd: Option<f64>,
    budget_usd: Option<f64>,
    window: String,
    source: String,
    as_of: String,
    stale: bool,
}

impl SpendRow {
    /// What the family spent. `None` when the field is not a number. The
    /// number can be `NaN` or an infinity.
    #[must_use]
    pub fn spend_usd(&self) -> Option<f64> {
        self.spend_usd
    }

    /// The budget. `None` when the field is not a number.
    #[must_use]
    pub fn budget_usd(&self) -> Option<f64> {
        self.budget_usd
    }

    /// The `window` text.
    #[must_use]
    pub fn window(&self) -> &str {
        &self.window
    }

    /// The `source` text.
    #[must_use]
    pub fn source(&self) -> &str {
        &self.source
    }

    /// The `as_of` text.
    #[must_use]
    pub fn as_of(&self) -> &str {
        &self.as_of
    }

    /// Whether `as_of` is more than 90 seconds before `written_at`, or before
    /// the time now when the document has no `written_at` (§7 rule 4).
    #[must_use]
    pub fn stale(&self) -> bool {
        self.stale
    }
}

/// The `validation` block on the noticeboard. Only [`noticeboard`] builds a
/// value.
///
/// ```
/// use creche_contracts::status::views::{ValidationRow, read_noticeboard};
///
/// let bytes = br#"{"validation": {"rev": "4f2a", "ok": true, "error_count": 0}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let validation: &ValidationRow = family.validation().unwrap();
/// assert_eq!(validation.rev(), "4f2a");
/// assert!(validation.ok());
/// assert!(!validation.never_valid());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{ValidationRow, read_noticeboard};
///
/// let bytes = br#"{"validation": {"rev": "4f2a", "ok": true, "error_count": 0}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let validation: &ValidationRow = family.validation().unwrap();
/// let failed = ValidationRow { ok: false, ..validation.clone() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationRow {
    rev: String,
    checked_at: String,
    ok: bool,
    never_valid: bool,
    error_count: Integer,
    warning_count: Integer,
    report_path: String,
    first_error: String,
}

impl ValidationRow {
    /// The `rev` text.
    #[must_use]
    pub fn rev(&self) -> &str {
        &self.rev
    }

    /// The `checked_at` text.
    #[must_use]
    pub fn checked_at(&self) -> &str {
        &self.checked_at
    }

    /// Whether `ok` is `true`.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.ok
    }

    /// Whether `never_valid` is `true`.
    #[must_use]
    pub fn never_valid(&self) -> bool {
        self.never_valid
    }

    /// The count of errors. 0 when the block has none.
    #[must_use]
    pub fn error_count(&self) -> &Integer {
        &self.error_count
    }

    /// The count of warnings. 0 when the block has none.
    #[must_use]
    pub fn warning_count(&self) -> &Integer {
        &self.warning_count
    }

    /// The `report_path` text.
    #[must_use]
    pub fn report_path(&self) -> &str {
        &self.report_path
    }

    /// The `first_error` text.
    #[must_use]
    pub fn first_error(&self) -> &str {
        &self.first_error
    }
}

/// The `reconcile` block on the noticeboard. Only [`noticeboard`] builds a
/// value.
///
/// ```
/// use creche_contracts::status::views::{ReconcileRow, read_noticeboard};
///
/// let bytes = br#"{"reconcile": {"step": "write_grants", "needs_switch": true}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let reconcile: &ReconcileRow = family.reconcile().unwrap();
/// assert_eq!(reconcile.step(), "write_grants");
/// assert_eq!(reconcile.attempts().as_u64(), Some(0));
/// assert!(reconcile.needs_switch());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{ReconcileRow, read_noticeboard};
///
/// let bytes = br#"{"reconcile": {"step": "write_grants", "needs_switch": true}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let reconcile: &ReconcileRow = family.reconcile().unwrap();
/// let in_place = ReconcileRow { needs_switch: false, ..reconcile.clone() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconcileRow {
    since: String,
    from_rev: String,
    to_rev: String,
    step: String,
    attempts: Integer,
    needs_switch: bool,
}

impl ReconcileRow {
    /// The `since` text.
    #[must_use]
    pub fn since(&self) -> &str {
        &self.since
    }

    /// The `from_rev` text.
    #[must_use]
    pub fn from_rev(&self) -> &str {
        &self.from_rev
    }

    /// The `to_rev` text.
    #[must_use]
    pub fn to_rev(&self) -> &str {
        &self.to_rev
    }

    /// The `step` text.
    #[must_use]
    pub fn step(&self) -> &str {
        &self.step
    }

    /// The count of attempts. 0 when the block has none.
    #[must_use]
    pub fn attempts(&self) -> &Integer {
        &self.attempts
    }

    /// Whether `needs_switch` is `true`.
    #[must_use]
    pub fn needs_switch(&self) -> bool {
        self.needs_switch
    }
}

/// The `limits` block on the noticeboard. A limit is each integer, also a
/// negative one. Only [`noticeboard`] and [`noticeboard_unreadable`] build a
/// value.
///
/// ```
/// use creche_contracts::status::views::{LimitsRow, read_noticeboard};
///
/// let bytes = br#"{"limits": {"max_running_turns": 2}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let limits: &LimitsRow = family.limits();
/// assert_eq!(limits.max_running_turns().and_then(|limit| limit.as_u64()), Some(2));
/// assert_eq!(limits.max_queued_turns(), None);
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{LimitsRow, read_noticeboard};
///
/// let bytes = br#"{"limits": {"max_running_turns": 2}}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family = read_noticeboard(bytes, "chat", now);
/// let limits: &LimitsRow = family.limits();
/// let no_limit = LimitsRow { max_running_turns: None, ..limits.clone() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LimitsRow {
    max_running_turns: Option<Integer>,
    max_queued_turns: Option<Integer>,
    job_timeout_s: Option<Integer>,
}

impl LimitsRow {
    /// `max_running_turns`.
    #[must_use]
    pub fn max_running_turns(&self) -> Option<&Integer> {
        self.max_running_turns.as_ref()
    }

    /// `max_queued_turns`.
    #[must_use]
    pub fn max_queued_turns(&self) -> Option<&Integer> {
        self.max_queued_turns.as_ref()
    }

    /// `job_timeout_s`.
    #[must_use]
    pub fn job_timeout_s(&self) -> Option<&Integer> {
        self.job_timeout_s.as_ref()
    }
}

/// Why a family is not `in_sync` on the noticeboard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reason {
    /// The document has no `written_at` that the noticeboard can read.
    NoWrittenAt,
    /// `caregiver` wrote the document this long ago.
    Stale {
        /// The age of the document.
        age: Age,
    },
    /// The family file failed validation.
    Invalid {
        /// Whether no revision of the family was valid.
        never_valid: bool,
        /// The first error. Empty when the block has none.
        first_error: String,
    },
    /// A fault is open: the first fault that stops turns, or the first
    /// fault.
    Fault {
        /// The code of the fault.
        code: String,
        /// The message of the fault. Empty when it has none.
        message: String,
    },
    /// `caregiver` applies a change.
    Reconciling {
        /// The step in flight.
        step: String,
        /// The count of attempts.
        attempts: Integer,
    },
    /// The noticeboard cannot read the file.
    Unreadable(ReadError),
}

/// One family on the noticeboard: `noticeboard.statusdocs.read_family`. Only
/// [`noticeboard`] and [`noticeboard_unreadable`] build a value.
///
/// ```
/// use creche_contracts::status::views::{FamilyRow, read_noticeboard};
/// use creche_contracts::status::words::Health;
///
/// let bytes = br#"{"state": "in_sync", "written_at": "2031-04-18T10:20:00Z"}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family: FamilyRow = read_noticeboard(bytes, "chat", now);
/// assert_eq!(family.name(), "chat");
/// assert_eq!(family.health(), Health::InSync);
/// assert_eq!(family.reason(), None);
/// assert_eq!(family.problem(), None);
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{FamilyRow, read_noticeboard};
/// use creche_contracts::status::words::Health;
///
/// let bytes = br#"{"state": "in_sync", "written_at": "2031-04-18T10:20:00Z"}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let family: FamilyRow = read_noticeboard(bytes, "chat", now);
/// let family = FamilyRow { health: Health::Degraded, ..family };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct FamilyRow {
    name: String,
    kind: String,
    health: Health,
    reason: Option<Reason>,
    written_at: String,
    age: Option<Age>,
    registry_rev: String,
    applied_rev: String,
    config_rev: String,
    epoch: Integer,
    sandboxes: Vec<SandboxRow>,
    faults: Vec<FaultRow>,
    spend: Option<SpendRow>,
    validation: Option<ValidationRow>,
    reconcile: Option<ReconcileRow>,
    limits: LimitsRow,
    problem: Option<ReadError>,
}

impl FamilyRow {
    /// The `family` text of the document. The name of the directory when the
    /// document has none.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The `kind` text. The noticeboard does not check it.
    #[must_use]
    pub fn kind(&self) -> &str {
        &self.kind
    }

    /// What the noticeboard shows as the state.
    #[must_use]
    pub fn health(&self) -> Health {
        self.health
    }

    /// Why the family is not `in_sync`. `None` when the noticeboard has
    /// nothing to say.
    #[must_use]
    pub fn reason(&self) -> Option<&Reason> {
        self.reason.as_ref()
    }

    /// The `written_at` text.
    #[must_use]
    pub fn written_at(&self) -> &str {
        &self.written_at
    }

    /// The age of the document. `None` when the noticeboard cannot read
    /// `written_at`.
    #[must_use]
    pub fn age(&self) -> Option<Age> {
        self.age
    }

    /// The `registry_rev` text.
    #[must_use]
    pub fn registry_rev(&self) -> &str {
        &self.registry_rev
    }

    /// The `applied_rev` text.
    #[must_use]
    pub fn applied_rev(&self) -> &str {
        &self.applied_rev
    }

    /// The `config_rev` text.
    #[must_use]
    pub fn config_rev(&self) -> &str {
        &self.config_rev
    }

    /// The epoch of the credentials: each integer. 0 when the document has
    /// none.
    #[must_use]
    pub fn epoch(&self) -> &Integer {
        &self.epoch
    }

    /// The first 20 items of `sandboxes` that are objects.
    #[must_use]
    pub fn sandboxes(&self) -> &[SandboxRow] {
        &self.sandboxes
    }

    /// The first 20 items of `faults` that are objects.
    #[must_use]
    pub fn faults(&self) -> &[FaultRow] {
        &self.faults
    }

    /// The `spend` block. `None` when the document has none.
    #[must_use]
    pub fn spend(&self) -> Option<&SpendRow> {
        self.spend.as_ref()
    }

    /// The `validation` block. `None` when the document has none.
    #[must_use]
    pub fn validation(&self) -> Option<&ValidationRow> {
        self.validation.as_ref()
    }

    /// The `reconcile` block. `None` when the document has none.
    #[must_use]
    pub fn reconcile(&self) -> Option<&ReconcileRow> {
        self.reconcile.as_ref()
    }

    /// The `limits` block.
    #[must_use]
    pub fn limits(&self) -> &LimitsRow {
        &self.limits
    }

    /// Why the noticeboard cannot read the file. `None` for a file that is
    /// one JSON object.
    #[must_use]
    pub fn problem(&self) -> Option<ReadError> {
        self.problem
    }
}

/// The view of the noticeboard for a file that is one JSON object. `name` is
/// the name of the directory of the family.
#[must_use]
pub fn noticeboard(raw: &RawStatus, name: &str, now: Timestamp) -> FamilyRow {
    let written = moment(&raw.written_at);
    let age = written.map(|written| age_at(written, now));
    let validation = raw.validation.value().map(|block| ValidationRow {
        rev: shown(&block.rev),
        checked_at: shown(&block.checked_at),
        ok: block.ok.is_true(),
        never_valid: block.never_valid.is_true(),
        error_count: any_integer(&block.error_count),
        warning_count: any_integer(&block.warning_count),
        report_path: shown(&block.report_path),
        first_error: shown(&block.first_error),
    });
    let reconcile = raw.reconcile.value().map(|block| ReconcileRow {
        since: shown(&block.since),
        from_rev: shown(&block.from_rev),
        to_rev: shown(&block.to_rev),
        step: shown(&block.step),
        attempts: any_integer(&block.attempts),
        needs_switch: block.needs_switch.is_true(),
    });
    let faults = fault_rows(raw);
    let health = health_of(raw, age);
    let family = shown(&raw.family);
    let limits = raw.limits.value();

    FamilyRow {
        name: if family.is_empty() {
            name.to_owned()
        } else {
            family
        },
        kind: shown(&raw.kind),
        health,
        reason: reason_of(
            health,
            age,
            validation.as_ref(),
            &faults,
            reconcile.as_ref(),
        ),
        written_at: shown(&raw.written_at),
        age,
        registry_rev: shown(&raw.registry_rev),
        applied_rev: shown(&raw.applied_rev),
        config_rev: shown(&raw.config_rev),
        epoch: raw
            .credentials
            .value()
            .map_or(Integer::from(0), |credentials| {
                any_integer(&credentials.epoch)
            }),
        sandboxes: sandbox_rows(raw),
        faults,
        spend: spend_row(raw, written.unwrap_or(now)),
        validation,
        reconcile,
        limits: LimitsRow {
            max_running_turns: limits.and_then(|limits| limits.max_running_turns.value().cloned()),
            max_queued_turns: limits.and_then(|limits| limits.max_queued_turns.value().cloned()),
            job_timeout_s: limits.and_then(|limits| limits.job_timeout_s.value().cloned()),
        },
        problem: None,
    }
}

/// A stale document is `unknown`, whatever its state says (§2 rule 5). A
/// state word that the noticeboard does not know is `unreadable`.
fn health_of(raw: &RawStatus, age: Option<Age>) -> Health {
    if age.is_none_or(|age| age.freshness() == Freshness::Stale) {
        return Health::Unknown;
    }

    shown(&raw.state).parse().unwrap_or(Health::Unreadable)
}

fn reason_of(
    health: Health,
    age: Option<Age>,
    validation: Option<&ValidationRow>,
    faults: &[FaultRow],
    reconcile: Option<&ReconcileRow>,
) -> Option<Reason> {
    match health {
        Health::Unknown => Some(age.map_or(Reason::NoWrittenAt, |age| Reason::Stale { age })),
        Health::Invalid => validation.map(|block| Reason::Invalid {
            never_valid: block.never_valid,
            first_error: block.first_error.clone(),
        }),
        Health::Degraded => {
            let blocking = faults.iter().find(|fault| fault.blocks_turns);

            blocking.or(faults.first()).map(|fault| Reason::Fault {
                code: fault.code.clone(),
                message: fault.message.clone(),
            })
        }
        Health::Reconciling => reconcile.map(|block| Reason::Reconciling {
            step: block.step.clone(),
            attempts: block.attempts.clone(),
        }),
        Health::InSync | Health::Unreadable => None,
    }
}

/// The text of one key of the detail of a fault, as the noticeboard shows it.
fn detail_text(fault: &RawFault, key: &str) -> String {
    let text = fault.detail.get(key).and_then(|value| value.as_str());

    first_chars(text.unwrap_or_default(), TEXT_CHARS_MAX)
}

fn fault_rows(raw: &RawStatus) -> Vec<FaultRow> {
    let items = raw.faults.value().map(Vec::as_slice).unwrap_or_default();

    items
        .iter()
        .take(ROWS_MAX)
        .flatten()
        .map(|fault| FaultRow {
            code: shown(&fault.code),
            blocks_turns: fault.blocks_turns.is_true(),
            since: shown(&fault.since),
            source: shown(&fault.source),
            stale: fault.stale.is_true(),
            message: detail_text(fault, "message"),
            sandbox: detail_text(fault, "sandbox"),
        })
        .collect()
}

fn sandbox_rows(raw: &RawStatus) -> Vec<SandboxRow> {
    let items = raw.sandboxes.value().map(Vec::as_slice).unwrap_or_default();

    items
        .iter()
        .take(ROWS_MAX)
        .flatten()
        .map(|row| SandboxRow {
            id: shown(&row.id),
            state: shown(&row.state),
            power: shown(&row.power),
            image: first_chars(&shown(&row.image), DIGEST_CHARS),
            cpus: any_integer(&row.cpus),
            memory: shown(&row.memory),
            created_at: shown(&row.created_at),
            ready_at: shown(&row.ready_at),
            channel: shown(&row.channel),
            has_supervisor_env: !row.supervisor_env.text().is_empty(),
        })
        .collect()
}

/// The `spend` block. `reference` is `written_at`, or the time now for a
/// document with no `written_at`.
fn spend_row(raw: &RawStatus, reference: Timestamp) -> Option<SpendRow> {
    let block = raw.spend.value()?;
    let lag_micros = |as_of: Timestamp| reference.unix_micros() - as_of.unix_micros();
    let fresh = moment(&block.as_of)
        .is_some_and(|as_of| lag_micros(as_of) <= STALE_AFTER_SECONDS * MICROS_PER_SECOND);

    Some(SpendRow {
        spend_usd: block.spend_usd.value().and_then(|number| number.to_f64()),
        budget_usd: block.budget_usd.value().and_then(|number| number.to_f64()),
        window: shown(&block.window),
        source: shown(&block.source),
        as_of: shown(&block.as_of),
        stale: !fresh,
    })
}

/// The row of a family whose file the noticeboard cannot read.
#[must_use]
pub fn noticeboard_unreadable(name: &str, problem: ReadError) -> FamilyRow {
    FamilyRow {
        name: name.to_owned(),
        kind: String::new(),
        health: Health::Unreadable,
        reason: Some(Reason::Unreadable(problem)),
        written_at: String::new(),
        age: None,
        registry_rev: String::new(),
        applied_rev: String::new(),
        config_rev: String::new(),
        epoch: Integer::from(0),
        sandboxes: Vec::new(),
        faults: Vec::new(),
        spend: None,
        validation: None,
        reconcile: None,
        limits: LimitsRow {
            max_running_turns: None,
            max_queued_turns: None,
            job_timeout_s: None,
        },
        problem: Some(problem),
    }
}

/// Reads the bytes of one `status.json` as the noticeboard reads them. The
/// noticeboard makes a row for each file.
#[must_use]
pub fn read_noticeboard(bytes: &[u8], name: &str, now: Timestamp) -> FamilyRow {
    match RawStatus::read(bytes, Reader::Noticeboard) {
        Ok(raw) => noticeboard(&raw, name, now),
        Err(problem) => noticeboard_unreadable(name, problem),
    }
}

// --- the terminal door ---

/// The sandbox that the terminal door attaches to:
/// `agent_door_tui.status.StatusFiles.serving`. Only [`door_tui`] builds a
/// value.
///
/// ```
/// use creche_contracts::status::views::{Serving, read_door_tui};
///
/// let bytes = br#"{"kind": "attended", "sandboxes": [
///     {"id": "chat-s1", "state": "ready", "supervisor_env": "/env"}
/// ]}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let serving: Serving = read_door_tui(bytes, now).unwrap();
/// assert_eq!(serving.sandbox().as_str(), "chat-s1");
/// assert_eq!(serving.supervisor_env(), "/env");
/// assert!(serving.blocking().is_empty());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::views::{Serving, read_door_tui};
///
/// let bytes = br#"{"kind": "attended", "sandboxes": [
///     {"id": "chat-s1", "state": "ready", "supervisor_env": "/env"}
/// ]}"#;
/// let now = "2031-04-18T10:20:30Z".parse().unwrap();
/// let serving: Serving = read_door_tui(bytes, now).unwrap();
/// let serving = Serving { supervisor_env: String::new(), ..serving };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Serving {
    sandbox: SandboxName,
    supervisor_env: String,
    blocking: Vec<String>,
    freshness: Freshness,
}

impl Serving {
    /// The newest sandbox in the state `ready`.
    #[must_use]
    pub fn sandbox(&self) -> &SandboxName {
        &self.sandbox
    }

    /// The path of its `supervisor.env`. It is not empty.
    #[must_use]
    pub fn supervisor_env(&self) -> &str {
        &self.supervisor_env
    }

    /// The `code` text of each fault with `blocks_turns: true`. A fault with
    /// no code gives an empty text. The door warns and opens the terminal.
    #[must_use]
    pub fn blocking(&self) -> &[String] {
        &self.blocking
    }

    /// Whether the document is stale. The door warns and opens the terminal.
    #[must_use]
    pub fn freshness(&self) -> Freshness {
        self.freshness
    }
}

/// The exit code of the terminal door for a refusal.
///
/// The set is closed: the door has no other code for a refusal that comes
/// from the status document.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TuiExit {
    /// The command line asked for what the door cannot do: 64.
    BadUsage,
    /// A switch is in progress, or no sandbox serves: 66.
    NoSandbox,
}

impl TuiExit {
    /// The exit code as a number.
    #[must_use]
    pub const fn code(self) -> u8 {
        match self {
            Self::BadUsage => 64,
            Self::NoSandbox => 66,
        }
    }
}

/// Why the terminal door opens no terminal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TuiRefusal {
    /// The door cannot read the file.
    Unreadable(ReadError),
    /// The family is not attended.
    WrongKind {
        /// The `kind` text of the document. Empty when it has none.
        kind: String,
    },
    /// No revision of the family was valid.
    NeverValid,
    /// A pass needs a switch, or its step creates, switches or destroys a
    /// sandbox.
    Switching {
        /// The `step` text of the reconcile block. Empty when it has none.
        step: String,
    },
    /// A sandbox is in the state `draining`.
    Draining {
        /// The `id` text of the first such sandbox. The door does not check
        /// it.
        sandbox: String,
    },
    /// No row with a sandbox name is in the state `ready`.
    NoReadySandbox {
        /// The `state` text of each row with a sandbox name. A row with no
        /// state gives `?`.
        states: BTreeSet<String>,
    },
    /// The newest ready sandbox has no `supervisor_env`.
    NoEnvPath {
        /// The sandbox.
        sandbox: SandboxName,
    },
}

impl TuiRefusal {
    /// The exit code that the door ends with.
    #[must_use]
    pub fn exit(&self) -> TuiExit {
        match self {
            Self::WrongKind { .. } => TuiExit::BadUsage,
            Self::Unreadable(_)
            | Self::NeverValid
            | Self::Switching { .. }
            | Self::Draining { .. }
            | Self::NoReadySandbox { .. }
            | Self::NoEnvPath { .. } => TuiExit::NoSandbox,
        }
    }
}

/// The view of the terminal door for a file that is one JSON object.
///
/// # Errors
///
/// [`TuiRefusal`] says why the door opens no terminal.
pub fn door_tui(raw: &RawStatus, now: Timestamp) -> Result<Serving, TuiRefusal> {
    if raw.kind.text() != KIND_ATTENDED {
        return Err(TuiRefusal::WrongKind {
            kind: raw.kind.text().to_owned(),
        });
    }

    if never_valid(raw) {
        return Err(TuiRefusal::NeverValid);
    }

    check_switch(raw)?;
    let (sandbox, supervisor_env) = ready_sandbox(raw)?;

    Ok(Serving {
        sandbox,
        supervisor_env,
        blocking: objects(&raw.faults)
            .filter(|fault| fault.blocks_turns.is_true())
            .map(|fault| fault.code.text().to_owned())
            .collect(),
        freshness: freshness(raw.written_at.text().parse().ok(), now),
    })
}

fn check_switch(raw: &RawStatus) -> Result<(), TuiRefusal> {
    if let Some(reconcile) = raw.reconcile.value() {
        let step = reconcile.step.text();
        if reconcile.needs_switch.is_true() || SWITCH_STEPS.contains(&step) {
            return Err(TuiRefusal::Switching {
                step: step.to_owned(),
            });
        }
    }

    let draining = objects(&raw.sandboxes).find(|row| row.state.text() == SANDBOX_DRAINING);
    match draining {
        Some(row) => Err(TuiRefusal::Draining {
            sandbox: row.id.text().to_owned(),
        }),
        None => Ok(()),
    }
}

/// The newest ready sandbox and the path of its `supervisor.env`.
fn ready_sandbox(raw: &RawStatus) -> Result<(SandboxName, String), TuiRefusal> {
    let named: Vec<(SandboxName, &RawSandbox)> = objects(&raw.sandboxes)
        .filter_map(|row| Some((row.id.text().parse().ok()?, row)))
        .collect();
    let mut newest: Option<&(SandboxName, &RawSandbox)> = None;
    for row in named
        .iter()
        .filter(|(_, row)| row.state.text() == SANDBOX_READY)
    {
        // The first row wins when two rows have one number, as `max` of
        // Python does.
        if newest.is_none_or(|(held, _)| row.0.number() > held.number()) {
            newest = Some(row);
        }
    }

    let Some((sandbox, row)) = newest else {
        let state_of = |row: &RawSandbox| match row.state.text() {
            "" => "?".to_owned(),
            state => state.to_owned(),
        };

        return Err(TuiRefusal::NoReadySandbox {
            states: named.iter().map(|(_, row)| state_of(row)).collect(),
        });
    };

    match row.supervisor_env.text() {
        "" => Err(TuiRefusal::NoEnvPath {
            sandbox: sandbox.clone(),
        }),
        path => Ok((sandbox.clone(), path.to_owned())),
    }
}

/// Reads the bytes of one `status.json` as the terminal door reads them.
///
/// # Errors
///
/// [`TuiRefusal`] says why the door opens no terminal.
pub fn read_door_tui(bytes: &[u8], now: Timestamp) -> Result<Serving, TuiRefusal> {
    let raw = RawStatus::read(bytes, Reader::DoorTui).map_err(TuiRefusal::Unreadable)?;

    door_tui(&raw, now)
}

// --- the trigger door and the Open WebUI door ---

/// Why a door does not list a family.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListingRefusal {
    /// The door cannot read the file.
    Unreadable(ReadError),
    /// The family has another kind than the door serves.
    WrongKind,
    /// The state is `invalid` and no revision of the family was valid.
    NeverValid,
}

/// The rule that the two doors share: the kind, then `never_valid`. These
/// doors count `never_valid` only in the state `invalid`.
fn listing(raw: &RawStatus, kind: &str) -> Result<(), ListingRefusal> {
    if raw.kind.text() != kind {
        return Err(ListingRefusal::WrongKind);
    }

    if raw.state.text() == STATE_INVALID && never_valid(raw) {
        return Err(ListingRefusal::NeverValid);
    }

    Ok(())
}

/// The view of the trigger door:
/// `agent_door_trigger.families.StatusFiles.servable`. `Ok` means that a
/// trigger can start the family.
///
/// # Errors
///
/// [`ListingRefusal`] says why the door does not list the family.
pub fn door_trigger(raw: &RawStatus) -> Result<(), ListingRefusal> {
    listing(raw, KIND_AUTONOMOUS)
}

/// The view of the Open WebUI door:
/// `agent_door_owui.families.StatusFiles.serving`. `Ok` means that the model
/// picker shows the family.
///
/// # Errors
///
/// [`ListingRefusal`] says why the door does not list the family.
pub fn door_owui(raw: &RawStatus) -> Result<(), ListingRefusal> {
    listing(raw, KIND_ATTENDED)
}

/// Reads the bytes of one `status.json` as the trigger door reads them.
///
/// # Errors
///
/// [`ListingRefusal`] says why the door does not list the family.
pub fn read_door_trigger(bytes: &[u8]) -> Result<(), ListingRefusal> {
    let raw = RawStatus::read(bytes, Reader::DoorTrigger).map_err(ListingRefusal::Unreadable)?;

    door_trigger(&raw)
}

/// Reads the bytes of one `status.json` as the Open WebUI door reads them.
///
/// # Errors
///
/// [`ListingRefusal`] says why the door does not list the family.
pub fn read_door_owui(bytes: &[u8]) -> Result<(), ListingRefusal> {
    let raw = RawStatus::read(bytes, Reader::DoorOwui).map_err(ListingRefusal::Unreadable)?;

    door_owui(&raw)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::status::json::JsonError;

    /// The largest file that a door reads, in bytes.
    const DOOR_CAP_BYTES: usize = 256 * 1024;

    fn time(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    fn now() -> Timestamp {
        time("2031-04-18T10:20:30Z")
    }

    fn chat() -> FamilyName {
        "chat".parse().unwrap()
    }

    /// A small document of an attended family that each reader serves, with
    /// `more` as further fields.
    fn document(more: &str) -> String {
        format!(
            r#"{{"kind": "attended", "state": "in_sync", "written_at": "2031-04-18T10:20:00Z",
                "sandboxes": [{{"id": "chat-s1", "state": "ready", "supervisor_env": "/env"}}]
                {more}}}"#
        )
    }

    fn raw(text: &str) -> RawStatus {
        RawStatus::read(text.as_bytes(), Reader::Noticeboard).unwrap()
    }

    #[test]
    fn the_small_document_serves_in_each_view() {
        let text = document("");
        let read = raw(&text);
        let row = noticeboard(&read, "chat", now());
        let serving = door_tui(&read, now()).unwrap();

        assert_eq!(attendance(&read, &chat()).sandboxes().len(), 1);
        assert_eq!(row.health(), Health::InSync);
        assert_eq!(row.reason(), None);
        assert_eq!(row.age().unwrap().whole_seconds(), 30);
        assert_eq!(serving.sandbox().as_str(), "chat-s1");
        assert_eq!(serving.freshness(), Freshness::Fresh);
        assert_eq!(door_owui(&read), Ok(()));
        assert_eq!(door_trigger(&read), Err(ListingRefusal::WrongKind));
    }

    #[test]
    fn attendance_has_no_default_kind_and_no_default_state() {
        let served = attendance(&raw(&document("")), &chat());
        let unknown = attendance(&raw(r#"{"kind": "robot", "state": "in sync"}"#), &chat());
        let empty = attendance(&raw("{}"), &chat());

        assert_eq!(served.kind(), Some(Kind::Attended));
        assert_eq!(served.state(), Some(FamilyState::InSync));
        assert_eq!((unknown.kind(), unknown.state()), (None, None));
        assert_eq!((empty.kind(), empty.state()), (None, None));
    }

    #[test]
    fn the_terminal_door_takes_the_newest_ready_sandbox_and_the_first_of_a_tie() {
        let rows = |ids: &[&str]| {
            let rows: Vec<String> = ids
                .iter()
                .map(|id| format!(r#"{{"id": "{id}", "state": "ready", "supervisor_env": "/e"}}"#))
                .collect();

            raw(&format!(
                r#"{{"kind": "attended", "sandboxes": [{}]}}"#,
                rows.join(",")
            ))
        };
        let newest = |ids: &[&str]| door_tui(&rows(ids), now()).unwrap().sandbox().clone();

        assert_eq!(
            newest(&["chat-s2", "chat-s10", "chat-s9"]).as_str(),
            "chat-s10"
        );
        assert_eq!(newest(&["chat-s007", "chat-s7"]).as_str(), "chat-s007");
        assert_eq!(newest(&["chat-s7", "chat-s007"]).as_str(), "chat-s7");
    }

    #[test]
    fn a_written_at_that_names_no_offset_reads_as_no_time() {
        let text = document("").replace("10:20:00Z", "10:20:00");
        let read = raw(&text);
        let row = noticeboard(&read, "chat", now());

        assert_eq!(attendance(&read, &chat()).written_at(), None);
        assert_eq!(row.health(), Health::Unknown);
        assert_eq!(row.reason(), Some(&Reason::NoWrittenAt));
        assert_eq!(row.age(), None);
        assert_eq!(
            door_tui(&read, now()).unwrap().freshness(),
            Freshness::Stale
        );
    }

    /// Three inputs that are in no vector. Each one is the `written_at` of a
    /// document, 30 seconds before the time now of the test. The Python
    /// readers of today give another answer for the first two. The third one
    /// has no vector on a reader surface.
    #[test]
    fn each_view_reads_a_time_with_the_grammar_of_the_one_type() {
        let at = time("2999-01-01T00:00:30Z");
        let midnight = time("2999-01-01T00:00:00Z");
        let inputs = [
            // A space in place of the `T`: no time.
            ("2999-01-01 00:00:00Z", None),
            // A lower-case `z`: a time.
            ("2999-01-01T00:00:00z", Some(midnight)),
            // No UTC offset: no time.
            ("2999-01-01T00:00:00", None),
        ];

        for (written_at, read_as) in inputs {
            let text = document("").replace("2031-04-18T10:20:00Z", written_at);
            let read = raw(&text);
            let row = noticeboard(&read, "chat", at);
            let fresh = freshness(read_as, at);

            assert!(text.contains(written_at), "{written_at}");
            assert_eq!(
                attendance(&read, &chat()).written_at(),
                read_as,
                "{written_at}"
            );
            assert_eq!(row.written_at(), written_at);
            assert_eq!(
                row.age().map(Age::whole_seconds),
                read_as.map(|_| 30),
                "{written_at}"
            );
            assert_eq!(
                row.health() == Health::InSync,
                read_as.is_some(),
                "{written_at}"
            );
            assert_eq!(
                door_tui(&read, at).unwrap().freshness(),
                fresh,
                "{written_at}"
            );
            assert_eq!(fresh == Freshness::Fresh, read_as.is_some());
            assert_eq!(door_owui(&read), Ok(()), "{written_at}");
            assert_eq!(
                door_trigger(&read),
                Err(ListingRefusal::WrongKind),
                "{written_at}"
            );
        }
    }

    #[test]
    fn a_file_is_stale_after_more_than_90_seconds() {
        let now = time("2999-01-01T00:00:30Z");
        let ages = [
            ("2999-01-01T00:00:00Z", 30, Freshness::Fresh),
            ("2998-12-31T23:59:00Z", 90, Freshness::Fresh),
            ("2998-12-31T23:58:59.999999Z", 90, Freshness::Stale),
            ("2998-12-31T23:58:59Z", 91, Freshness::Stale),
            ("2020-01-01T00:00:00Z", 30_894_307_230, Freshness::Stale),
            ("2999-01-01T00:00:30Z", 0, Freshness::Fresh),
            ("2999-01-01T00:01:00Z", 0, Freshness::Fresh),
        ];

        for (written, seconds, expected) in ages {
            let age = age_at(time(written), now);

            assert_eq!(age.whole_seconds(), seconds, "{written}");
            assert_eq!(age.freshness(), expected, "{written}");
            assert_eq!(freshness_at(time(written), now), expected, "{written}");
            assert_eq!(freshness(Some(time(written)), now), expected, "{written}");
        }

        assert_eq!(freshness(None, now), Freshness::Stale);
    }

    #[test]
    fn the_age_of_a_file_from_the_first_instant_to_the_last_one_has_no_overflow() {
        let whole_range = age_at(Timestamp::MIN, Timestamp::MAX);

        assert_eq!(whole_range.whole_seconds(), 315_537_897_599);
        assert_eq!(whole_range.freshness(), Freshness::Stale);
        assert_eq!(age_at(Timestamp::MAX, Timestamp::MIN).whole_seconds(), 0);
        assert_eq!(
            freshness_at(Timestamp::MAX, Timestamp::MIN),
            Freshness::Fresh
        );
    }

    #[test]
    fn an_age_in_seconds_is_the_nearest_float() {
        let now = time("2999-01-01T00:00:30Z");
        let ages = [
            ("2999-01-01T00:00:00Z", 30.0),
            ("2999-01-01T00:00:00.123456Z", 29.876_544),
            ("2020-01-01T00:00:00Z", 30_894_307_230.0),
            ("2999-01-01T00:00:30Z", 0.0),
            ("2999-01-01T00:00:31Z", 0.0),
        ];

        for (written, seconds) in ages {
            assert_eq!(
                age_at(time(written), now).seconds().to_bits(),
                f64::to_bits(seconds)
            );
        }
    }

    #[test]
    fn a_state_word_of_a_reader_shows_as_that_health() {
        let unknown = raw(&document("").replace("in_sync", "unknown"));
        let unreadable = raw(&document("").replace("in_sync", "unreadable"));
        let row = noticeboard(&unknown, "chat", now());

        assert_eq!(row.health(), Health::Unknown);
        assert!(matches!(row.reason(), Some(Reason::Stale { age }) if age.whole_seconds() == 30));
        assert_eq!(
            noticeboard(&unreadable, "chat", now()).health(),
            Health::Unreadable
        );
    }

    #[test]
    fn the_noticeboard_shows_500_characters_of_a_text() {
        let long = "\u{e9}".repeat(TEXT_CHARS_MAX + 100);
        let read = raw(&format!(
            r#"{{"kind": "{long}", "written_at": "2031-04-18T10:20:00Z{long}"}}"#
        ));
        let row = noticeboard(&read, "chat", now());

        assert_eq!(row.kind().chars().count(), TEXT_CHARS_MAX);
        assert_eq!(row.written_at().chars().count(), TEXT_CHARS_MAX);
        assert_eq!(row.age(), None);
    }

    #[test]
    fn a_spend_past_the_range_of_a_float_shows_as_no_number() {
        let spend = format!(
            r#", "spend": {{"spend_usd": 1{}, "budget_usd": 2}}"#,
            "0".repeat(400)
        );
        let row = noticeboard(&raw(&document(&spend)), "chat", now());
        let spend = row.spend().unwrap();

        assert_eq!(spend.spend_usd(), None);
        assert_eq!(spend.budget_usd(), Some(2.0));
        assert!(spend.stale());
    }

    #[test]
    fn a_door_refuses_a_file_over_its_cap() {
        let padding = format!(r#", "x": "{}""#, "a".repeat(DOOR_CAP_BYTES));
        let text = document(&padding);
        let too_large = ReadError::TooLarge {
            cap: DOOR_CAP_BYTES,
        };
        let of_tui = read_door_tui(text.as_bytes(), now());

        assert_eq!(
            read_door_owui(text.as_bytes()),
            Err(ListingRefusal::Unreadable(too_large))
        );
        assert_eq!(
            read_door_trigger(text.as_bytes()),
            Err(ListingRefusal::Unreadable(too_large))
        );
        assert_eq!(of_tui, Err(TuiRefusal::Unreadable(too_large)));
        assert_eq!(of_tui.unwrap_err().exit(), TuiExit::NoSandbox);
        assert!(read_attendance(text.as_bytes(), &chat()).is_ok());
        assert_eq!(
            read_noticeboard(text.as_bytes(), "chat", now()).problem(),
            None
        );
    }

    #[test]
    fn each_reader_refuses_bytes_that_are_not_utf8() {
        let bytes = b"{\"kind\": \"attended\", \"x\": \"\xff\"}";
        let not_utf8 = ReadError::NotJson(JsonError::NotUtf8);
        let row = read_noticeboard(bytes, "chat", now());

        assert_eq!(read_attendance(bytes, &chat()), Err(not_utf8));
        assert_eq!(row.problem(), Some(not_utf8));
        assert_eq!(row.health(), Health::Unreadable);
        assert_eq!(row.reason(), Some(&Reason::Unreadable(not_utf8)));
        assert_eq!(
            read_door_tui(bytes, now()),
            Err(TuiRefusal::Unreadable(not_utf8))
        );
        assert_eq!(
            read_door_trigger(bytes),
            Err(ListingRefusal::Unreadable(not_utf8))
        );
        assert_eq!(
            read_door_owui(bytes),
            Err(ListingRefusal::Unreadable(not_utf8))
        );
    }

    #[test]
    fn each_text_of_a_noticeboard_row_comes_from_its_own_field() {
        let read = raw(r#"{"registry_rev": "reg-1", "applied_rev": "reg-2",
                "sandboxes": [{"id": "chat-s1", "created_at": "created", "ready_at": "ready"}],
                "faults": [{"code": "grants_stale", "since": "since", "source": "source",
                    "stale": true, "message": "message", "sandbox": "sandbox"}]}"#);
        let row = noticeboard(&read, "chat", now());
        let sandbox = &row.sandboxes()[0];
        let fault = &row.faults()[0];

        assert_eq!((row.registry_rev(), row.applied_rev()), ("reg-1", "reg-2"));
        assert_eq!(
            (sandbox.created_at(), sandbox.ready_at()),
            ("created", "ready")
        );
        assert_eq!((fault.since(), fault.source()), ("since", "source"));
        assert_eq!((fault.message(), fault.sandbox()), ("message", "sandbox"));
        assert!(fault.stale());
    }

    #[test]
    fn the_terminal_door_has_two_exit_codes() {
        let wrong_kind = TuiRefusal::WrongKind {
            kind: "thin".to_owned(),
        };

        assert_eq!(wrong_kind.exit(), TuiExit::BadUsage);
        assert_eq!(TuiRefusal::NeverValid.exit(), TuiExit::NoSandbox);
        assert_eq!(TuiExit::BadUsage.code(), 64);
        assert_eq!(TuiExit::NoSandbox.code(), 66);
    }
}
