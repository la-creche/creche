//! The status document as a file holds it, before a check.
//!
//! [`RawStatus`] is the raw type of contract 05 §2.1. It reads each JSON
//! object: a field that is missing, `null` or of another JSON type is a fact
//! of the document, not an error. Two things read a raw document:
//!
//! - [`super::document::StatusDocument`] is the valid type. Its conversion
//!   refuses each field that contract 05 does not permit.
//! - [`super::views`] holds one view for each of the five readers. A view
//!   takes what its Python reader takes, also from a document that the valid
//!   type refuses.
//!
//! The raw type has no `serde` derive. [`super::json`] says why: `serde_json`
//! refuses four forms of a JSON text that each Python reader takes.

use std::error::Error;
use std::fmt;

use super::json::{ByteOrderMark, Integer, Json, JsonError, JsonKind, Object};

/// The largest file that `attendance` and the noticeboard read: 1 MiB.
const LARGE_CAP_BYTES: usize = 1 << 20;

/// The largest file that a door reads: 256 KiB.
const DOOR_CAP_BYTES: usize = 256 * 1024;

/// One reader of the status document.
///
/// The set is closed: contract 05 §1 and §8 name the readers. It does not
/// cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reader {
    /// `attendance`.
    Attendance,
    /// The noticeboard.
    Noticeboard,
    /// The terminal door.
    DoorTui,
    /// The trigger door.
    DoorTrigger,
    /// The Open WebUI door.
    DoorOwui,
}

impl Reader {
    /// The largest file that the reader parses, in bytes.
    #[must_use]
    pub const fn size_cap(self) -> usize {
        match self {
            Self::Attendance | Self::Noticeboard => LARGE_CAP_BYTES,
            Self::DoorTui | Self::DoorTrigger | Self::DoorOwui => DOOR_CAP_BYTES,
        }
    }

    /// What the reader does with a byte order mark. The noticeboard skips
    /// one, and each other reader refuses the file.
    #[must_use]
    pub const fn byte_order_mark(self) -> ByteOrderMark {
        match self {
            Self::Noticeboard => ByteOrderMark::Skip,
            Self::Attendance | Self::DoorTui | Self::DoorTrigger | Self::DoorOwui => {
                ByteOrderMark::Refuse
            }
        }
    }
}

/// Why the bytes of a file are not one JSON object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadError {
    /// The file has more bytes than the reader parses.
    TooLarge {
        /// The cap of the reader, in bytes.
        cap: usize,
    },
    /// The bytes are not one JSON text.
    NotJson(JsonError),
    /// The JSON text is not an object.
    NotAnObject(JsonKind),
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge { cap } => write!(f, "the file has more than {cap} bytes"),
            Self::NotJson(error) => write!(f, "the file is not JSON: {error}"),
            Self::NotAnObject(kind) => write!(f, "the file holds {kind:?}, not a JSON object"),
        }
    }
}

impl Error for ReadError {}

/// Reads the bytes of one file as one JSON object.
pub(crate) fn read_object(
    bytes: &[u8],
    cap: usize,
    mark: ByteOrderMark,
) -> Result<Object, ReadError> {
    if bytes.len() > cap {
        return Err(ReadError::TooLarge { cap });
    }

    match Json::parse_bytes(bytes, mark).map_err(ReadError::NotJson)? {
        Json::Object(object) => Ok(object),
        other => Err(ReadError::NotAnObject(other.kind())),
    }
}

/// What a document holds at one key.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Slot<T> {
    /// The document has no such key.
    Missing,
    /// The value is `null`.
    Null,
    /// The value has a JSON type that the field does not take.
    Other(JsonKind),
    /// The value has the JSON type of the field.
    Value(T),
}

impl<T> Slot<T> {
    /// The value, when the document holds one of the JSON type of the field.
    pub(crate) fn value(&self) -> Option<&T> {
        match self {
            Self::Value(value) => Some(value),
            Self::Missing | Self::Null | Self::Other(_) => None,
        }
    }
}

impl Slot<String> {
    /// The text, or the empty text.
    pub(crate) fn text(&self) -> &str {
        self.value().map_or("", String::as_str)
    }
}

impl Slot<bool> {
    /// Whether the document holds `true` here. Each Python reader takes no
    /// other value as true.
    pub(crate) fn is_true(&self) -> bool {
        self.value() == Some(&true)
    }
}

/// A JSON number: an integer or a float.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Number {
    Integer(Integer),
    Float(f64),
}

impl Number {
    /// The number as a float. `None` for an integer past the range of a
    /// finite float.
    pub(crate) fn to_f64(&self) -> Option<f64> {
        match self {
            Self::Integer(integer) => integer.to_f64(),
            Self::Float(float) => Some(*float),
        }
    }
}

fn slot<T>(object: &Object, key: &str, pick: impl Fn(&Json) -> Option<T>) -> Slot<T> {
    match object.get(key) {
        None => Slot::Missing,
        Some(Json::Null) => Slot::Null,
        Some(value) => pick(value).map_or(Slot::Other(value.kind()), Slot::Value),
    }
}

pub(crate) fn text(object: &Object, key: &str) -> Slot<String> {
    slot(object, key, |value| value.as_str().map(str::to_owned))
}

pub(crate) fn flag(object: &Object, key: &str) -> Slot<bool> {
    slot(object, key, |value| match value {
        Json::Bool(flag) => Some(*flag),
        _ => None,
    })
}

pub(crate) fn integer(object: &Object, key: &str) -> Slot<Integer> {
    slot(object, key, |value| match value {
        Json::Integer(integer) => Some(integer.clone()),
        _ => None,
    })
}

pub(crate) fn number(object: &Object, key: &str) -> Slot<Number> {
    slot(object, key, |value| match value {
        Json::Integer(integer) => Some(Number::Integer(integer.clone())),
        Json::Float(float) => Some(Number::Float(*float)),
        _ => None,
    })
}

/// An object at `key`, read with `read`.
pub(crate) fn block<T>(object: &Object, key: &str, read: fn(&Object) -> T) -> Slot<T> {
    slot(object, key, |value| match value {
        Json::Object(inner) => Some(read(inner)),
        _ => None,
    })
}

/// A list at `key`. An item that is not an object is `None`: it keeps its
/// place, because a reader with a cap counts it.
pub(crate) fn list<T>(object: &Object, key: &str, read: fn(&Object) -> T) -> Slot<Vec<Option<T>>> {
    slot(object, key, |value| match value {
        Json::Array(items) => Some(
            items
                .iter()
                .map(|item| match item {
                    Json::Object(inner) => Some(read(inner)),
                    _ => None,
                })
                .collect(),
        ),
        _ => None,
    })
}

/// The `validation` block (contract 05 §3.2).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawValidation {
    pub(crate) rev: Slot<String>,
    pub(crate) checked_at: Slot<String>,
    pub(crate) ok: Slot<bool>,
    pub(crate) never_valid: Slot<bool>,
    pub(crate) error_count: Slot<Integer>,
    pub(crate) warning_count: Slot<Integer>,
    pub(crate) report_path: Slot<String>,
    pub(crate) first_error: Slot<String>,
}

impl RawValidation {
    fn read(object: &Object) -> Self {
        Self {
            rev: text(object, "rev"),
            checked_at: text(object, "checked_at"),
            ok: flag(object, "ok"),
            never_valid: flag(object, "never_valid"),
            error_count: integer(object, "error_count"),
            warning_count: integer(object, "warning_count"),
            report_path: text(object, "report_path"),
            first_error: text(object, "first_error"),
        }
    }
}

/// The five keys of a fault that contract 05 §3.3 names. Each other key is a
/// detail of the fault.
pub(crate) const FAULT_KEYS: [&str; 5] = ["code", "blocks_turns", "since", "source", "stale"];

/// One fault (contract 05 §3.3).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawFault {
    pub(crate) code: Slot<String>,
    pub(crate) blocks_turns: Slot<bool>,
    pub(crate) since: Slot<String>,
    pub(crate) source: Slot<String>,
    pub(crate) stale: Slot<bool>,
    /// Each key that §3.3 does not name, in the order of the file.
    pub(crate) detail: Object,
}

impl RawFault {
    pub(crate) fn read(object: &Object) -> Self {
        Self {
            code: text(object, "code"),
            blocks_turns: flag(object, "blocks_turns"),
            since: text(object, "since"),
            source: text(object, "source"),
            stale: flag(object, "stale"),
            detail: object.without(&FAULT_KEYS),
        }
    }
}

/// The `reconcile` block (contract 05 §3.4).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawReconcile {
    pub(crate) since: Slot<String>,
    pub(crate) from_rev: Slot<String>,
    pub(crate) to_rev: Slot<String>,
    pub(crate) step: Slot<String>,
    pub(crate) attempts: Slot<Integer>,
    pub(crate) needs_switch: Slot<bool>,
}

impl RawReconcile {
    fn read(object: &Object) -> Self {
        Self {
            since: text(object, "since"),
            from_rev: text(object, "from_rev"),
            to_rev: text(object, "to_rev"),
            step: text(object, "step"),
            attempts: integer(object, "attempts"),
            needs_switch: flag(object, "needs_switch"),
        }
    }
}

/// One sandbox (contract 05 §4.1).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawSandbox {
    pub(crate) id: Slot<String>,
    pub(crate) state: Slot<String>,
    pub(crate) power: Slot<String>,
    pub(crate) image: Slot<String>,
    pub(crate) spec_hash: Slot<String>,
    pub(crate) cpus: Slot<Integer>,
    pub(crate) memory: Slot<String>,
    pub(crate) created_at: Slot<String>,
    pub(crate) ready_at: Slot<String>,
    pub(crate) channel: Slot<String>,
    pub(crate) supervisor_env: Slot<String>,
}

impl RawSandbox {
    fn read(object: &Object) -> Self {
        Self {
            id: text(object, "id"),
            state: text(object, "state"),
            power: text(object, "power"),
            image: text(object, "image"),
            spec_hash: text(object, "spec_hash"),
            cpus: integer(object, "cpus"),
            memory: text(object, "memory"),
            created_at: text(object, "created_at"),
            ready_at: text(object, "ready_at"),
            channel: text(object, "channel"),
            supervisor_env: text(object, "supervisor_env"),
        }
    }
}

/// The `credentials` block (contract 05 §6.1).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawCredentials {
    pub(crate) epoch: Slot<Integer>,
    pub(crate) key_id: Slot<String>,
    pub(crate) token_id: Slot<String>,
    pub(crate) rotated_at: Slot<String>,
    pub(crate) next_rotation_at: Slot<String>,
    pub(crate) rotation_state: Slot<String>,
}

impl RawCredentials {
    fn read(object: &Object) -> Self {
        Self {
            epoch: integer(object, "epoch"),
            key_id: text(object, "key_id"),
            token_id: text(object, "token_id"),
            rotated_at: text(object, "rotated_at"),
            next_rotation_at: text(object, "next_rotation_at"),
            rotation_state: text(object, "rotation_state"),
        }
    }
}

/// The `spend` block (contract 05 §7).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawSpend {
    pub(crate) window: Slot<String>,
    pub(crate) spend_usd: Slot<Number>,
    pub(crate) budget_usd: Slot<Number>,
    pub(crate) as_of: Slot<String>,
    pub(crate) source: Slot<String>,
}

impl RawSpend {
    fn read(object: &Object) -> Self {
        Self {
            window: text(object, "window"),
            spend_usd: number(object, "spend_usd"),
            budget_usd: number(object, "budget_usd"),
            as_of: text(object, "as_of"),
            source: text(object, "source"),
        }
    }
}

/// The `limits` block (contract 05 §2.1).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawLimits {
    pub(crate) max_running_turns: Slot<Integer>,
    pub(crate) max_queued_turns: Slot<Integer>,
    pub(crate) job_timeout_s: Slot<Integer>,
}

impl RawLimits {
    fn read(object: &Object) -> Self {
        Self {
            max_running_turns: integer(object, "max_running_turns"),
            max_queued_turns: integer(object, "max_queued_turns"),
            job_timeout_s: integer(object, "job_timeout_s"),
        }
    }
}

/// One declared webhook (contract 05 §6.4).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawWebhook {
    pub(crate) name: Slot<String>,
    pub(crate) token_path: Slot<String>,
}

impl RawWebhook {
    fn read(object: &Object) -> Self {
        Self {
            name: text(object, "name"),
            token_path: text(object, "token_path"),
        }
    }
}

/// The `triggers` block (contract 05 §2.1, §6.4).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawTriggers {
    pub(crate) webhooks: Slot<Vec<Option<RawWebhook>>>,
    pub(crate) enqueue: Slot<bool>,
}

impl RawTriggers {
    fn read(object: &Object) -> Self {
        Self {
            webhooks: list(object, "webhooks", RawWebhook::read),
            enqueue: flag(object, "enqueue"),
        }
    }
}

/// The `pep` block (contract 05 §2.2).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RawPep {
    pub(crate) watch: Slot<String>,
    pub(crate) url: Slot<String>,
    pub(crate) checked_at: Slot<String>,
    pub(crate) unreachable_since: Slot<String>,
}

impl RawPep {
    fn read(object: &Object) -> Self {
        Self {
            watch: text(object, "watch"),
            url: text(object, "url"),
            checked_at: text(object, "checked_at"),
            unreachable_since: text(object, "unreachable_since"),
        }
    }
}

/// One status document as its file holds it (contract 05 §2.1).
///
/// The type holds each field that the contract names and no other key. It
/// checks no field. Read it through a view of [`super::views`], or make the
/// valid type from it.
///
/// ```
/// use creche_contracts::status::raw::{RawStatus, Reader};
///
/// let raw = RawStatus::read(br#"{"kind": 5}"#, Reader::Attendance)?;
/// assert!(RawStatus::read(b"[]", Reader::Attendance).is_err());
/// # let _ = raw;
/// # Ok::<(), creche_contracts::status::raw::ReadError>(())
/// ```
///
/// Code outside this crate cannot read a field that no check passed:
///
/// ```compile_fail,E0616
/// use creche_contracts::status::raw::{RawStatus, Reader};
///
/// let raw = RawStatus::read(br#"{"kind": 5}"#, Reader::Attendance).unwrap();
/// let kind = raw.kind;
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct RawStatus {
    pub(crate) family: Slot<String>,
    pub(crate) kind: Slot<String>,
    pub(crate) state: Slot<String>,
    pub(crate) written_at: Slot<String>,
    pub(crate) registry_rev: Slot<String>,
    pub(crate) applied_rev: Slot<String>,
    pub(crate) config_rev: Slot<String>,
    pub(crate) validation: Slot<RawValidation>,
    pub(crate) faults: Slot<Vec<Option<RawFault>>>,
    pub(crate) reconcile: Slot<RawReconcile>,
    pub(crate) sandboxes: Slot<Vec<Option<RawSandbox>>>,
    pub(crate) credentials: Slot<RawCredentials>,
    pub(crate) spend: Slot<RawSpend>,
    pub(crate) limits: Slot<RawLimits>,
    pub(crate) triggers: Slot<RawTriggers>,
    pub(crate) pep: Slot<RawPep>,
}

impl RawStatus {
    /// Reads the bytes of one `status.json`, with the size cap and the byte
    /// order mark rule of `reader`.
    ///
    /// # Errors
    ///
    /// [`ReadError`] when the bytes are not one JSON object.
    pub fn read(bytes: &[u8], reader: Reader) -> Result<Self, ReadError> {
        let object = read_object(bytes, reader.size_cap(), reader.byte_order_mark())?;

        Ok(Self::from_object(&object))
    }

    /// The fields of one JSON object.
    #[must_use]
    pub fn from_object(object: &Object) -> Self {
        Self {
            family: text(object, "family"),
            kind: text(object, "kind"),
            state: text(object, "state"),
            written_at: text(object, "written_at"),
            registry_rev: text(object, "registry_rev"),
            applied_rev: text(object, "applied_rev"),
            config_rev: text(object, "config_rev"),
            validation: block(object, "validation", RawValidation::read),
            faults: list(object, "faults", RawFault::read),
            reconcile: block(object, "reconcile", RawReconcile::read),
            sandboxes: list(object, "sandboxes", RawSandbox::read),
            credentials: block(object, "credentials", RawCredentials::read),
            spend: block(object, "spend", RawSpend::read),
            limits: block(object, "limits", RawLimits::read),
            triggers: block(object, "triggers", RawTriggers::read),
            pep: block(object, "pep", RawPep::read),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(text: &str) -> RawStatus {
        RawStatus::read(text.as_bytes(), Reader::Attendance).unwrap()
    }

    #[test]
    fn a_field_is_missing_null_of_another_type_or_a_value() {
        let read = raw(r#"{"kind": "thin", "state": null, "written_at": 5, "faults": {}}"#);

        assert_eq!(read.family, Slot::Missing);
        assert_eq!(read.kind, Slot::Value("thin".to_owned()));
        assert_eq!(read.state, Slot::Null);
        assert_eq!(read.written_at, Slot::Other(JsonKind::Integer));
        assert_eq!(read.faults, Slot::Other(JsonKind::Object));
        assert_eq!(read.kind.text(), "thin");
        assert_eq!(read.state.text(), "");
    }

    #[test]
    fn only_true_is_true() {
        let read = raw(
            r#"{"triggers": {"enqueue": true}, "validation": {"ok": 1, "never_valid": "true"}}"#,
        );
        let triggers = read.triggers.value().unwrap();
        let validation = read.validation.value().unwrap();

        assert!(triggers.enqueue.is_true());
        assert!(!validation.ok.is_true());
        assert!(!validation.never_valid.is_true());
        assert_eq!(validation.ok, Slot::Other(JsonKind::Integer));
    }

    #[test]
    fn a_list_keeps_the_place_of_an_item_that_is_not_an_object() {
        let read = raw(r#"{"sandboxes": ["chat-s1", {"id": "chat-s2"}, null]}"#);
        let sandboxes = read.sandboxes.value().unwrap();

        assert_eq!(sandboxes.len(), 3);
        assert!(sandboxes[0].is_none());
        assert_eq!(sandboxes[1].as_ref().unwrap().id.text(), "chat-s2");
        assert!(sandboxes[2].is_none());
    }

    #[test]
    fn each_key_that_the_contract_does_not_name_is_a_detail_of_a_fault() {
        let read = raw(
            r#"{"faults": [{"sandbox": "chat-s4", "code": "x", "stale": true, "attempts": 3}]}"#,
        );
        let faults = read.faults.value().unwrap();
        let fault = faults[0].as_ref().unwrap();
        let keys: Vec<&str> = fault.detail.iter().map(|(key, _)| key).collect();

        assert_eq!(keys, ["sandbox", "attempts"]);
        assert_eq!(fault.code.text(), "x");
        assert!(fault.stale.is_true());
    }

    #[test]
    fn a_number_is_an_integer_or_a_float() {
        let read = raw(r#"{"spend": {"spend_usd": 2, "budget_usd": 2.5, "window": true}}"#);
        let spend = read.spend.value().unwrap();

        assert_eq!(spend.spend_usd.value().unwrap().to_f64(), Some(2.0));
        assert_eq!(spend.budget_usd.value().unwrap().to_f64(), Some(2.5));
        assert_eq!(spend.window, Slot::Other(JsonKind::Bool));
    }

    #[test]
    fn each_reader_has_its_cap_and_its_rule_for_a_byte_order_mark() {
        let marked = "\u{feff}{}".as_bytes();
        let large = format!(r#"{{"x": "{}"}}"#, "a".repeat(DOOR_CAP_BYTES));

        for reader in [
            Reader::Attendance,
            Reader::DoorTui,
            Reader::DoorTrigger,
            Reader::DoorOwui,
        ] {
            assert_eq!(
                RawStatus::read(marked, reader),
                Err(ReadError::NotJson(JsonError::ByteOrderMark)),
                "{reader:?}"
            );
        }

        assert!(RawStatus::read(marked, Reader::Noticeboard).is_ok());
        assert!(RawStatus::read(large.as_bytes(), Reader::Attendance).is_ok());
        assert!(RawStatus::read(large.as_bytes(), Reader::Noticeboard).is_ok());
        assert_eq!(
            RawStatus::read(large.as_bytes(), Reader::DoorOwui),
            Err(ReadError::TooLarge {
                cap: DOOR_CAP_BYTES
            })
        );
    }

    #[test]
    fn a_file_of_exactly_the_cap_is_read() {
        // The test writes each cap a second time, as a number. A constant of
        // the module that moves then fails the test.
        let caps = [
            (Reader::Attendance, 1 << 20),
            (Reader::Noticeboard, 1 << 20),
            (Reader::DoorTui, 256 << 10),
            (Reader::DoorTrigger, 256 << 10),
            (Reader::DoorOwui, 256 << 10),
        ];

        for (reader, cap) in caps {
            let padding = cap - r#"{"x": ""}"#.len();
            let at_cap = format!(r#"{{"x": "{}"}}"#, "a".repeat(padding));
            let over_cap = format!(r#"{{"x": "{}"}}"#, "a".repeat(padding + 1));

            assert_eq!(at_cap.len(), cap);
            assert!(
                RawStatus::read(at_cap.as_bytes(), reader).is_ok(),
                "{reader:?}"
            );
            assert_eq!(
                RawStatus::read(over_cap.as_bytes(), reader),
                Err(ReadError::TooLarge { cap }),
                "{reader:?}"
            );
        }
    }

    #[test]
    fn a_text_that_is_not_an_object_names_what_it_is() {
        let kinds = [
            ("null", JsonKind::Null),
            ("[]", JsonKind::Array),
            (r#""in_sync""#, JsonKind::String),
            ("5", JsonKind::Integer),
            ("true", JsonKind::Bool),
            ("NaN", JsonKind::Float),
        ];

        for (text, kind) in kinds {
            assert_eq!(
                RawStatus::read(text.as_bytes(), Reader::Noticeboard),
                Err(ReadError::NotAnObject(kind)),
                "{text}"
            );
        }

        assert_eq!(
            ReadError::NotAnObject(JsonKind::Array).to_string(),
            "the file holds Array, not a JSON object"
        );
    }
}
