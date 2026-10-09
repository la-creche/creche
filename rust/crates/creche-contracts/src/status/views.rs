//! One view of the status document for each of its five readers.
//!
//! A view is what one reader takes from a document. Each view here takes what
//! its Python reader takes, on each document of `vectors/data/status`: the
//! port of a reader then keeps the behavior of that reader. A view reads a
//! [`RawStatus`], because each Python reader also uses a document that the
//! valid type refuses. `StatusDocument::raw` gives the raw form of a valid
//! document.
//!
//! The five readers do not agree on each document. The test of this module
//! holds a table of the documents on which two readers differ.
//!
//! A view is a record of what a reader took. Its fields are public: no code
//! takes a view as an input that it trusts.

use std::collections::BTreeSet;

use super::json::Integer;
use super::raw::{RawFault, RawSandbox, RawStatus, ReadError, Reader};
use super::time::{Age, Freshness, STALE_AFTER_SECONDS, Timestamp, freshness};
use super::words::{FamilyState, Health, Kind, SandboxLifecycle};
use crate::ids::{FamilyName, SandboxName};
use crate::slot::Slot;

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

// --- attendance ---

/// The epoch that `attendance` uses when a document has none.
const FIRST_EPOCH: u64 = 1;

/// One sandbox that `attendance` takes from a document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttendanceSandbox {
    /// The name of the sandbox.
    pub id: SandboxName,
    /// The lifecycle state.
    pub state: SandboxLifecycle,
    /// The path of the `supervisor.env`. Empty when the row has none.
    pub supervisor_env: String,
}

/// What `attendance` takes from one status document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttendanceStatus {
    /// The family that the caller asked for. The reader does not read the
    /// `family` field of the document.
    pub family: FamilyName,
    /// The kind. `None` when the document has no kind, or has an unknown
    /// word. The reader has no default kind: a default opens the family to
    /// the doors of that kind.
    pub kind: Option<Kind>,
    /// The state. `None` when the document has no state, or has an unknown
    /// word.
    pub state: Option<FamilyState>,
    /// When `caregiver` wrote the document. `None` when the reader cannot
    /// read the field.
    pub written_at: Option<Timestamp>,
    /// The revision of the family config mount. Empty when the document has
    /// none.
    pub config_rev: String,
    /// The epoch of the credentials. An epoch that is missing, negative or
    /// not an integer reads as 1.
    pub epoch: Integer,
    /// Whether `validation.never_valid` is `true`.
    pub never_valid: bool,
    /// The code of the first fault with `blocks_turns: true` and a code.
    pub blocking_fault: Option<String>,
    /// The code of each fault that has one.
    pub fault_codes: BTreeSet<String>,
    /// Each sandbox row with a sandbox name and a known state.
    pub sandboxes: Vec<AttendanceSandbox>,
    /// `limits.max_running_turns`, when it is an integer of zero or more.
    pub max_running_turns: Option<Integer>,
    /// `limits.job_timeout_s`, when it is an integer of zero or more.
    pub job_timeout_s: Option<Integer>,
    /// Whether `triggers.enqueue` is `true`.
    pub accepts_dispatch: bool,
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

/// One row of `sandboxes` on the noticeboard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SandboxRow {
    /// The `id` text. The noticeboard does not check it.
    pub id: String,
    /// The `state` text.
    pub state: String,
    /// The `power` text.
    pub power: String,
    /// The first 19 characters of the image.
    pub image: String,
    /// The count of CPUs. 0 when the row has none.
    pub cpus: Integer,
    /// The `memory` text.
    pub memory: String,
    /// The `created_at` text.
    pub created_at: String,
    /// The `ready_at` text.
    pub ready_at: String,
    /// The `channel` text.
    pub channel: String,
    /// Whether the row has a `supervisor_env` text that is not empty.
    pub has_supervisor_env: bool,
}

/// One fault on the noticeboard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultRow {
    /// The `code` text.
    pub code: String,
    /// Whether `blocks_turns` is `true`.
    pub blocks_turns: bool,
    /// The `since` text.
    pub since: String,
    /// The `source` text.
    pub source: String,
    /// Whether `stale` is `true`.
    pub stale: bool,
    /// The `message` text of the detail.
    pub message: String,
    /// The `sandbox` text of the detail.
    pub sandbox: String,
}

/// The `spend` block on the noticeboard.
#[derive(Debug, Clone, PartialEq)]
pub struct SpendRow {
    /// What the family spent. `None` when the field is not a number. The
    /// number can be `NaN` or an infinity.
    pub spend_usd: Option<f64>,
    /// The budget. `None` when the field is not a number.
    pub budget_usd: Option<f64>,
    /// The `window` text.
    pub window: String,
    /// The `source` text.
    pub source: String,
    /// The `as_of` text.
    pub as_of: String,
    /// Whether `as_of` is more than 90 seconds before `written_at`, or before
    /// the time now when the document has no `written_at` (§7 rule 4).
    pub stale: bool,
}

/// The `validation` block on the noticeboard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationRow {
    /// The `rev` text.
    pub rev: String,
    /// The `checked_at` text.
    pub checked_at: String,
    /// Whether `ok` is `true`.
    pub ok: bool,
    /// Whether `never_valid` is `true`.
    pub never_valid: bool,
    /// The count of errors. 0 when the block has none.
    pub error_count: Integer,
    /// The count of warnings. 0 when the block has none.
    pub warning_count: Integer,
    /// The `report_path` text.
    pub report_path: String,
    /// The `first_error` text.
    pub first_error: String,
}

/// The `reconcile` block on the noticeboard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconcileRow {
    /// The `since` text.
    pub since: String,
    /// The `from_rev` text.
    pub from_rev: String,
    /// The `to_rev` text.
    pub to_rev: String,
    /// The `step` text.
    pub step: String,
    /// The count of attempts. 0 when the block has none.
    pub attempts: Integer,
    /// Whether `needs_switch` is `true`.
    pub needs_switch: bool,
}

/// The `limits` block on the noticeboard. A limit is each integer, also a
/// negative one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LimitsRow {
    /// `max_running_turns`.
    pub max_running_turns: Option<Integer>,
    /// `max_queued_turns`.
    pub max_queued_turns: Option<Integer>,
    /// `job_timeout_s`.
    pub job_timeout_s: Option<Integer>,
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

/// One family on the noticeboard: `noticeboard.statusdocs.read_family`.
#[derive(Debug, Clone, PartialEq)]
pub struct FamilyRow {
    /// The `family` text of the document. The name of the directory when the
    /// document has none.
    pub name: String,
    /// The `kind` text. The noticeboard does not check it.
    pub kind: String,
    /// What the noticeboard shows as the state.
    pub health: Health,
    /// Why the family is not `in_sync`. `None` when the noticeboard has
    /// nothing to say.
    pub reason: Option<Reason>,
    /// The `written_at` text.
    pub written_at: String,
    /// The age of the document. `None` when the noticeboard cannot read
    /// `written_at`.
    pub age: Option<Age>,
    /// The `registry_rev` text.
    pub registry_rev: String,
    /// The `applied_rev` text.
    pub applied_rev: String,
    /// The `config_rev` text.
    pub config_rev: String,
    /// The epoch of the credentials: each integer. 0 when the document has
    /// none.
    pub epoch: Integer,
    /// The first 20 items of `sandboxes` that are objects.
    pub sandboxes: Vec<SandboxRow>,
    /// The first 20 items of `faults` that are objects.
    pub faults: Vec<FaultRow>,
    /// The `spend` block. `None` when the document has none.
    pub spend: Option<SpendRow>,
    /// The `validation` block. `None` when the document has none.
    pub validation: Option<ValidationRow>,
    /// The `reconcile` block. `None` when the document has none.
    pub reconcile: Option<ReconcileRow>,
    /// The `limits` block.
    pub limits: LimitsRow,
    /// Why the noticeboard cannot read the file. `None` for a file that is
    /// one JSON object.
    pub problem: Option<ReadError>,
}

/// The view of the noticeboard for a file that is one JSON object. `name` is
/// the name of the directory of the family.
#[must_use]
pub fn noticeboard(raw: &RawStatus, name: &str, now: Timestamp) -> FamilyRow {
    let written = moment(&raw.written_at);
    let age = written.map(|written| written.age_at(now));
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
        .is_some_and(|as_of| lag_micros(as_of) <= STALE_AFTER_SECONDS * 1_000_000);

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
/// `agent_door_tui.status.StatusFiles.serving`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Serving {
    /// The newest sandbox in the state `ready`.
    pub sandbox: SandboxName,
    /// The path of its `supervisor.env`. It is not empty.
    pub supervisor_env: String,
    /// The `code` text of each fault with `blocks_turns: true`. A fault with
    /// no code gives an empty text. The door warns and opens the terminal.
    pub blocking: Vec<String>,
    /// Whether the document is stale. The door warns and opens the terminal.
    pub freshness: Freshness,
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

    fn now() -> Timestamp {
        "2031-04-18T10:20:30Z".parse().unwrap()
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

        assert_eq!(attendance(&read, &chat()).sandboxes.len(), 1);
        assert_eq!(row.health, Health::InSync);
        assert_eq!(row.reason, None);
        assert_eq!(row.age.unwrap().whole_seconds(), 30);
        assert_eq!(serving.sandbox.as_str(), "chat-s1");
        assert_eq!(serving.freshness, Freshness::Fresh);
        assert_eq!(door_owui(&read), Ok(()));
        assert_eq!(door_trigger(&read), Err(ListingRefusal::WrongKind));
    }

    #[test]
    fn attendance_has_no_default_kind_and_no_default_state() {
        let served = attendance(&raw(&document("")), &chat());
        let unknown = attendance(&raw(r#"{"kind": "robot", "state": "in sync"}"#), &chat());
        let empty = attendance(&raw("{}"), &chat());

        assert_eq!(served.kind, Some(Kind::Attended));
        assert_eq!(served.state, Some(FamilyState::InSync));
        assert_eq!((unknown.kind, unknown.state), (None, None));
        assert_eq!((empty.kind, empty.state), (None, None));
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
        let newest = |ids: &[&str]| door_tui(&rows(ids), now()).unwrap().sandbox;

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

        assert_eq!(attendance(&read, &chat()).written_at, None);
        assert_eq!(row.health, Health::Unknown);
        assert_eq!(row.reason, Some(Reason::NoWrittenAt));
        assert_eq!(row.age, None);
        assert_eq!(door_tui(&read, now()).unwrap().freshness, Freshness::Stale);
    }

    #[test]
    fn a_state_word_of_a_reader_shows_as_that_health() {
        let unknown = raw(&document("").replace("in_sync", "unknown"));
        let unreadable = raw(&document("").replace("in_sync", "unreadable"));
        let row = noticeboard(&unknown, "chat", now());

        assert_eq!(row.health, Health::Unknown);
        assert!(matches!(row.reason, Some(Reason::Stale { age }) if age.whole_seconds() == 30));
        assert_eq!(
            noticeboard(&unreadable, "chat", now()).health,
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

        assert_eq!(row.kind.chars().count(), TEXT_CHARS_MAX);
        assert_eq!(row.written_at.chars().count(), TEXT_CHARS_MAX);
        assert_eq!(row.age, None);
    }

    #[test]
    fn a_spend_past_the_range_of_a_float_shows_as_no_number() {
        let spend = format!(
            r#", "spend": {{"spend_usd": 1{}, "budget_usd": 2}}"#,
            "0".repeat(400)
        );
        let row = noticeboard(&raw(&document(&spend)), "chat", now());
        let spend = row.spend.unwrap();

        assert_eq!(spend.spend_usd, None);
        assert_eq!(spend.budget_usd, Some(2.0));
        assert!(spend.stale);
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
            read_noticeboard(text.as_bytes(), "chat", now()).problem,
            None
        );
    }

    #[test]
    fn each_reader_refuses_bytes_that_are_not_utf8() {
        let bytes = b"{\"kind\": \"attended\", \"x\": \"\xff\"}";
        let not_utf8 = ReadError::NotJson(JsonError::NotUtf8);
        let row = read_noticeboard(bytes, "chat", now());

        assert_eq!(read_attendance(bytes, &chat()), Err(not_utf8));
        assert_eq!(row.problem, Some(not_utf8));
        assert_eq!(row.health, Health::Unreadable);
        assert_eq!(row.reason, Some(Reason::Unreadable(not_utf8)));
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
