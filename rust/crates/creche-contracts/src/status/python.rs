//! The differential test of this module against the Python implementation.
//!
//! The vectors of the `status.` surfaces hold what each Python reader and each
//! Python writer of contract 05 does. Each test here walks each vector of its
//! surfaces.

use std::collections::HashSet;

use creche_vectors::{self as vectors, Outcome, Surface, Vector};
use serde_json::{Map, Value, json};

use super::document::{FaultError, FieldFault, StatusDocument, StatusError};
use super::fault_file::{self, FaultFile, OpenFault};
use super::json::{Integer, Json, JsonKind, Object};
use super::outcome;
use super::raw::{RawStatus, ReadError, Reader};
use super::time::{Freshness, Timestamp, TimestampError};
use super::views::{self, FamilyRow, Reason, TuiExit, TuiRefusal};
use super::words::{FaultCode, FaultSource};
use crate::ids::FamilyName;

/// The family of each vector.
const FAMILY: &str = "chat";

/// The time now that the generator gives a reader.
const NOW: &str = "2999-01-01T00:00:30Z";

fn now() -> Timestamp {
    NOW.parse().unwrap()
}

fn chat() -> FamilyName {
    FAMILY.parse().unwrap()
}

// --- a value of this module as the generator writes it ---

/// One JSON value as a vector file holds it: the generator writes an integer
/// past 64 bits and a float that is not finite as a marker object.
fn to_value(json: &Json) -> Value {
    match json {
        Json::Null => Value::Null,
        Json::Bool(flag) => json!(flag),
        Json::Integer(integer) => integer_value(integer),
        Json::Float(float) => float_value(*float),
        Json::String(text) => json!(text),
        Json::Array(items) => Value::Array(items.iter().map(to_value).collect()),
        Json::Object(object) => Value::Object(
            object
                .iter()
                .map(|(key, value)| (key.to_owned(), to_value(value)))
                .collect(),
        ),
    }
}

fn integer_value(integer: &Integer) -> Value {
    if let Some(small) = integer.as_u64() {
        return json!(small);
    }

    match integer.as_i64() {
        Some(small) => json!(small),
        None => json!({"$int": integer.to_string()}),
    }
}

fn float_value(float: f64) -> Value {
    if float.is_nan() {
        return json!({"$float": "NaN"});
    }

    if float.is_infinite() {
        let word = if float > 0.0 { "Infinity" } else { "-Infinity" };

        return json!({"$float": word});
    }

    json!(float)
}

/// One value of a vector file as a value of this module.
fn from_value(value: &Value) -> Json {
    if let Some(marker) = vectors::Marker::of(value).unwrap() {
        return match marker {
            vectors::Marker::Int(digits) => Json::Integer(digits.parse().unwrap()),
            vectors::Marker::Float(vectors::NotFinite::Nan) => Json::Float(f64::NAN),
            vectors::Marker::Float(vectors::NotFinite::Infinity) => Json::Float(f64::INFINITY),
            vectors::Marker::Float(vectors::NotFinite::NegativeInfinity) => {
                Json::Float(f64::NEG_INFINITY)
            }
            other => panic!("no vector of contract 05 holds the marker {other:?}"),
        };
    }

    match value {
        Value::Null => Json::Null,
        Value::Bool(flag) => Json::Bool(*flag),
        Value::Number(number) if number.is_f64() => Json::Float(number.as_f64().unwrap()),
        Value::Number(number) => Json::Integer(number.to_string().parse().unwrap()),
        Value::String(text) => Json::String(text.clone()),
        Value::Array(items) => Json::Array(items.iter().map(from_value).collect()),
        Value::Object(fields) => Json::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), from_value(value)))
                .collect(),
        ),
    }
}

fn optional_integer(integer: Option<&Integer>) -> Value {
    integer.map_or(Value::Null, integer_value)
}

/// A time as `datetime.isoformat` of Python writes a time in UTC.
fn isoformat(instant: Timestamp) -> String {
    format!("{}+00:00", instant.to_string().trim_end_matches('Z'))
}

/// What a Python reader calls the type of a JSON value.
fn python_type(kind: JsonKind) -> &'static str {
    match kind {
        JsonKind::Null => "NoneType",
        JsonKind::Bool => "bool",
        JsonKind::Integer => "int",
        JsonKind::Float => "float",
        JsonKind::String => "str",
        JsonKind::Array => "list",
        JsonKind::Object => "dict",
    }
}

/// The class of a problem of the noticeboard. The noticeboard writes the
/// text of a Python exception into its problem. This module gives the class
/// and no text.
fn problem_class(problem: ReadError) -> String {
    match problem {
        ReadError::TooLarge { .. } => "too large".to_owned(),
        ReadError::NotJson(_) => "not JSON".to_owned(),
        ReadError::NotAnObject(kind) => format!("not an object: {}", python_type(kind)),
    }
}

/// The class of a problem text that the noticeboard wrote.
fn problem_class_of_text(text: &str) -> String {
    if text.contains(" is not JSON: ") {
        return "not JSON".to_owned();
    }

    if let Some((_, rest)) = text.split_once(" is ")
        && let Some((kind, _)) = rest.split_once(", not a JSON object")
    {
        return format!("not an object: {kind}");
    }

    if text.contains(" bytes; refusing to parse it") {
        return "too large".to_owned();
    }

    text.to_owned()
}

/// A row of the noticeboard as a vector holds it, with the class of its
/// problem in place of the text. `keys` names the fields that hold the text.
fn with_problem_class(row: &Value, keys: &[&str]) -> Value {
    let mut row = row.clone();
    for key in keys {
        let text = row[key].as_str().unwrap().to_owned();
        row[key] = json!(problem_class_of_text(&text));
    }

    row
}

/// A value with the array at each of `keys` in one order. The generator
/// writes a set as an array in the order of the compact JSON texts of its
/// items. That order is not the byte order for a text outside ASCII, so the
/// test puts both sides in one order and compares the set.
fn with_sorted_sets(value: &Value, keys: &[&str]) -> Value {
    let mut value = value.clone();
    for key in keys {
        let items = value[key].as_array_mut().unwrap();
        items.sort_by_key(Value::to_string);
    }

    value
}

// --- the five readers of the status document ---

/// What the code did with one vector, as a vector file writes it: the `value`
/// of an accepted input, or the `refusal` of a refused input. `None` stands
/// for a key that the vector does not have.
type Replay = Result<Option<Value>, Option<Value>>;

fn replay_attendance(_: &Surface, vector: &Vector) -> Replay {
    let Ok(status) = views::read_attendance(&vector.input().bytes().unwrap(), &chat()) else {
        return Err(None);
    };
    let sandboxes: Vec<Value> = status
        .sandboxes()
        .iter()
        .map(|sandbox| {
            json!({
                "id": sandbox.id().as_str(),
                "state": sandbox.state().as_str(),
                "playpen_env": sandbox.supervisor_env(),
            })
        })
        .collect();

    Ok(Some(json!({
        "family": status.family().as_str(),
        "kind": status.kind().map(|kind| kind.as_str()),
        "state": status.state().map(|state| state.as_str()),
        "written_at": status.written_at().map(isoformat),
        "config_rev": status.config_rev(),
        "epoch": integer_value(status.epoch()),
        "never_valid": status.never_valid(),
        "blocking_fault": status.blocking_fault(),
        "fault_codes": status.fault_codes(),
        "sandboxes": sandboxes,
        "max_running_turns": optional_integer(status.max_running_turns()),
        "job_timeout_s": optional_integer(status.job_timeout_s()),
        "accepts_dispatch": status.accepts_dispatch(),
    })))
}

/// The reason of a row as the noticeboard writes it.
fn reason_text(reason: Option<&Reason>) -> String {
    match reason {
        None => String::new(),
        Some(Reason::NoWrittenAt) => {
            "no written_at in the status document; caregiver may not be running".to_owned()
        }
        Some(Reason::Stale { age }) => format!(
            "caregiver last wrote {}s ago, past the 90s limit",
            age.whole_seconds()
        ),
        Some(Reason::Invalid {
            never_valid,
            first_error,
        }) => {
            let never = if *never_valid {
                "no revision of this family ever validated; "
            } else {
                ""
            };
            let error = if first_error.is_empty() {
                "see the validation report"
            } else {
                first_error
            };

            format!("{never}{error}")
        }
        Some(Reason::Fault { code, message }) if message.is_empty() => code.clone(),
        Some(Reason::Fault { code, message }) => format!("{code}: {message}"),
        Some(Reason::Reconciling { step, attempts }) => format!("step {step}, attempt {attempts}"),
        Some(Reason::Unreadable(problem)) => problem_class(*problem),
    }
}

fn family_row_value(row: &FamilyRow) -> Value {
    let sandboxes: Vec<Value> = row
        .sandboxes()
        .iter()
        .map(|one| {
            json!({
                "id": one.id(),
                "state": one.state(),
                "power": one.power(),
                "image": one.image(),
                "cpus": integer_value(one.cpus()),
                "memory": one.memory(),
                "created_at": one.created_at(),
                "ready_at": one.ready_at(),
                "channel": one.channel(),
                "has_playpen_env": one.has_supervisor_env(),
            })
        })
        .collect();
    let faults: Vec<Value> = row
        .faults()
        .iter()
        .map(|one| {
            json!({
                "code": one.code(),
                "blocks_turns": one.blocks_turns(),
                "since": one.since(),
                "source": one.source(),
                "stale": one.stale(),
                "message": one.message(),
                "sandbox": one.sandbox(),
            })
        })
        .collect();
    let spend = row.spend().map(|one| {
        json!({
            "spend_usd": one.spend_usd().map(float_value),
            "budget_usd": one.budget_usd().map(float_value),
            "window": one.window(),
            "source": one.source(),
            "as_of": one.as_of(),
            "stale": one.stale(),
        })
    });
    let validation = row.validation().map(|one| {
        json!({
            "rev": one.rev(),
            "checked_at": one.checked_at(),
            "ok": one.ok(),
            "never_valid": one.never_valid(),
            "error_count": integer_value(one.error_count()),
            "warning_count": integer_value(one.warning_count()),
            "report_path": one.report_path(),
            "first_error": one.first_error(),
        })
    });
    let reconcile = row.reconcile().map(|one| {
        json!({
            "since": one.since(),
            "from_rev": one.from_rev(),
            "to_rev": one.to_rev(),
            "step": one.step(),
            "attempts": integer_value(one.attempts()),
            "needs_switch": one.needs_switch(),
        })
    });

    json!({
        "name": row.name(),
        "kind": row.kind(),
        "health": row.health().as_str(),
        "reason": reason_text(row.reason()),
        "written_at": row.written_at(),
        "age_s": row.age().map(|age| age.seconds()),
        "registry_rev": row.registry_rev(),
        "applied_rev": row.applied_rev(),
        "config_rev": row.config_rev(),
        "epoch": integer_value(row.epoch()),
        "sandboxes": sandboxes,
        "faults": faults,
        "spend": spend,
        "validation": validation,
        "reconcile": reconcile,
        "limits": {
            "max_running_turns": optional_integer(row.limits().max_running_turns()),
            "max_queued_turns": optional_integer(row.limits().max_queued_turns()),
            "job_timeout_s": optional_integer(row.limits().job_timeout_s()),
        },
        "problem": row.problem().map(problem_class).unwrap_or_default(),
    })
}

fn replay_noticeboard(_: &Surface, vector: &Vector) -> Replay {
    let row = views::read_noticeboard(&vector.input().bytes().unwrap(), FAMILY, now());
    let value = Some(family_row_value(&row));

    if row.problem().is_some() {
        Err(value)
    } else {
        Ok(value)
    }
}

/// The message that the terminal door of Python gives for a refusal. The
/// generator writes `<root>` for the directory of the test.
fn tui_message(refusal: &TuiRefusal) -> String {
    let or = |text: &str, empty: &str| {
        if text.is_empty() {
            empty.to_owned()
        } else {
            text.to_owned()
        }
    };

    match refusal {
        TuiRefusal::Unreadable(ReadError::TooLarge { .. }) => {
            format!("<root>/families/{FAMILY}/status.json is too large to be a status document.")
        }
        TuiRefusal::Unreadable(_) => format!(
            "no readable status document for family {FAMILY} under <root>/families. caregiver \
             publishes it; check that caregiver runs and the family exists."
        ),
        TuiRefusal::WrongKind { kind } => format!(
            "family {FAMILY} is {}, and a terminal reaches an attended family only (contract 02 \
             \u{a7}3.1).",
            or(kind, "of no stated kind")
        ),
        TuiRefusal::NeverValid => {
            format!("no revision of family {FAMILY} has ever validated, so nothing can serve it.")
        }
        TuiRefusal::Switching { step } => format!(
            "family {FAMILY} is switching sandboxes (step {}). Wait for the switch to finish, \
             then run this again.",
            or(step, "unknown")
        ),
        TuiRefusal::Draining { sandbox } => format!(
            "family {FAMILY} is switching sandboxes: {sandbox} is draining. Wait, then run this \
             again."
        ),
        TuiRefusal::NoReadySandbox { states } => {
            let states: Vec<&str> = states.iter().map(String::as_str).collect();

            format!(
                "no sandbox of family {FAMILY} is ready (states: {}). A terminal runs no \
                 handshake, so only a ready sandbox serves one.",
                or(&states.join(", "), "none")
            )
        }
        TuiRefusal::NoEnvPath { sandbox } => format!(
            "sandbox {sandbox} publishes no supervisor.env path. Contract 05 \u{a7}4.1.1 calls \
             that a fault: without it the terminal has no mounts."
        ),
    }
}

/// The warning that the terminal door of Python prints.
fn tui_warning(serving: &views::Serving) -> String {
    let mut parts = Vec::new();
    if !serving.blocking().is_empty() {
        parts.push(format!(
            "turns are blocked by {}",
            serving.blocking().join(", ")
        ));
    }

    if serving.freshness() == Freshness::Stale {
        parts.push("the status document is stale (over 90s old)".to_owned());
    }

    parts.join("; ")
}

fn replay_door_tui(_: &Surface, vector: &Vector) -> Replay {
    match views::read_door_tui(&vector.input().bytes().unwrap(), now()) {
        Ok(serving) => Ok(Some(json!({
            "sandbox": serving.sandbox().as_str(),
            "playpen_env": serving.supervisor_env(),
            "warning": tui_warning(&serving),
        }))),
        Err(refusal) => {
            let exit = match refusal.exit() {
                TuiExit::BadUsage => "bad_usage",
                TuiExit::NoSandbox => "no_sandbox",
            };

            Err(Some(
                json!({"exit": exit, "message": tui_message(&refusal)}),
            ))
        }
    }
}

fn replay_door_trigger(_: &Surface, vector: &Vector) -> Replay {
    match views::read_door_trigger(&vector.input().bytes().unwrap()) {
        Ok(()) => Ok(None),
        Err(_) => Err(None),
    }
}

fn replay_door_owui(_: &Surface, vector: &Vector) -> Replay {
    match views::read_door_owui(&vector.input().bytes().unwrap()) {
        Ok(()) => Ok(None),
        Err(_) => Err(None),
    }
}

// --- the reader of a fault file, and the reader of an outcome record ---

/// The writer whose directory holds the fault file of a vector.
fn source_of(vector: &Vector) -> FaultSource {
    let params = vector.field("params").unwrap();

    params["source"].as_str().unwrap().parse().unwrap()
}

fn replay_fault_reader(_: &Surface, vector: &Vector) -> Replay {
    let source = source_of(vector);
    let Ok(read) = fault_file::caregiver(&vector.input().bytes().unwrap(), source, now()) else {
        return Err(None);
    };
    let faults: Vec<Value> = read
        .faults
        .iter()
        .map(|fault| {
            json!({
                "code": fault.code.as_str(),
                "blocks_turns": fault.blocks_turns,
                "since": fault.since,
                "source": fault.source.as_str(),
                "stale": fault.stale,
                "detail": to_value(&Json::Object(fault.detail.clone())),
            })
        })
        .collect();

    Ok(Some(json!({
        "family": read.family,
        "source": read.source.as_str(),
        "written_at": read.written_at,
        "faults": faults,
    })))
}

fn replay_outcome(surface: &Surface, vector: &Vector) -> Replay {
    let stem = surface.context()["stem"].as_str().unwrap();
    let row = outcome::noticeboard(&vector.input().bytes().unwrap(), stem);
    let value = Some(json!({
        "id": row.id,
        "family": row.family,
        "session": row.session,
        "trigger": row.trigger,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "status": row.status,
        "error": row.error,
        "turns": integer_value(&row.turns),
        "spend_usd": row.spend_usd.map(float_value),
        "sandbox": row.sandbox,
        "problem": row.problem.map(problem_class).unwrap_or_default(),
    }));

    if row.problem.is_some() {
        Err(value)
    } else {
        Ok(value)
    }
}

/// One surface that a function of this module reads as its Python reader.
struct Reads {
    surface: &'static str,
    replay: fn(&Surface, &Vector) -> Replay,
    /// The keys of a refused row that hold the text of a Python exception.
    /// The test compares the class of the problem in their place.
    problem_keys: &'static [&'static str],
    /// The keys of an accepted value that hold a set. The test compares each
    /// one as a set.
    set_keys: &'static [&'static str],
}

const fn reads(surface: &'static str, replay: fn(&Surface, &Vector) -> Replay) -> Reads {
    Reads {
        surface,
        replay,
        problem_keys: &[],
        set_keys: &[],
    }
}

const READERS: &[Reads] = &[
    Reads {
        surface: "status.attendance",
        replay: replay_attendance,
        problem_keys: &[],
        set_keys: &["fault_codes"],
    },
    Reads {
        surface: "status.noticeboard",
        replay: replay_noticeboard,
        problem_keys: &["problem", "reason"],
        set_keys: &[],
    },
    reads("status.door_tui", replay_door_tui),
    reads("status.door_trigger", replay_door_trigger),
    reads("status.door_owui", replay_door_owui),
    reads("status.fault_file.caregiver", replay_fault_reader),
    Reads {
        surface: "status.outcome.noticeboard",
        replay: replay_outcome,
        problem_keys: &["problem"],
        set_keys: &[],
    },
];

#[test]
fn each_reader_does_what_its_python_reader_does() {
    for reader in READERS {
        let surface = vectors::surface(reader.surface).unwrap();
        for vector in surface.vectors() {
            let at = format!("{} {}", reader.surface, vector.id());
            let replayed = (reader.replay)(&surface, vector);
            match (vector.result(), replayed) {
                (Outcome::Accepted, Ok(value)) => {
                    let sets = |value: &Value| with_sorted_sets(value, reader.set_keys);

                    assert_eq!(value.as_ref().map(sets), vector.value().map(sets), "{at}");
                }
                (Outcome::Refused, Err(refusal)) => {
                    let wanted = vector
                        .refusal()
                        .map(|row| with_problem_class(row, reader.problem_keys));

                    assert_eq!(refusal, wanted, "{at}");
                }
                (Outcome::Raised, Err(_)) => {}
                (result, replayed) => panic!("{at}: Python {result:?}, this module {replayed:?}"),
            }
        }
    }
}

// --- the writers ---

/// The JSON object of a status document, from the arguments of a
/// `status.write` vector. The arguments hold the detail of a fault as a list
/// of pairs, in the order of the writer.
fn document_object(args: &Map<String, Value>) -> Object {
    let Json::Object(mut object) = from_value(&Value::Object(args.clone())) else {
        panic!("the arguments are an object");
    };
    let faults = args["faults"].as_array().unwrap().iter().map(|fault| {
        let mut entry = Object::new();
        for key in ["code", "blocks_turns", "since", "source", "stale"] {
            entry.insert(key, from_value(&fault[key]));
        }

        for pair in fault["detail"].as_array().unwrap() {
            entry.insert(pair[0].as_str().unwrap(), from_value(&pair[1]));
        }

        Json::Object(entry)
    });
    object.insert("faults", Json::Array(faults.collect()));

    object
}

fn document_of(args: &Map<String, Value>) -> Result<StatusDocument, StatusError> {
    StatusDocument::try_from(&RawStatus::from_object(&document_object(args)))
}

/// The bytes of the `output` of a vector.
fn output_of(vector: &Vector) -> Vec<u8> {
    vector.output().unwrap().bytes().unwrap()
}

/// One vector of `status.write` on which the writer here differs from the
/// Python writer on purpose: the Python writer writes a document that
/// contract 05 does not permit, and the valid type refuses it.
struct Deviation {
    vector: &'static str,
    /// The section of contract 05 that the document breaks.
    contract: &'static str,
    /// Why the valid type refuses the document.
    refusal: StatusError,
}

const fn field(field: &'static str, fault: FieldFault) -> StatusError {
    StatusError::Field {
        field,
        item: None,
        fault,
    }
}

const fn item(field: &'static str, fault: FieldFault) -> StatusError {
    StatusError::Field {
        field,
        item: Some(0),
        fault,
    }
}

const fn deviation(
    vector: &'static str,
    contract: &'static str,
    refusal: StatusError,
) -> Deviation {
    Deviation {
        vector,
        contract,
        refusal,
    }
}

const DEVIATIONS: &[Deviation] = &[
    deviation(
        "lax-kind-unknown",
        "§2.1",
        field("kind", FieldFault::UnknownWord),
    ),
    deviation(
        "lax-written-at-not-a-time",
        "§2.1",
        field("written_at", FieldFault::NotATime(TimestampError::Form)),
    ),
    deviation(
        "lax-epoch-zero",
        "§6.2",
        field("credentials.epoch", FieldFault::OutOfRange),
    ),
    deviation(
        "lax-epoch-negative",
        "§6.2",
        field("credentials.epoch", FieldFault::OutOfRange),
    ),
    deviation(
        "lax-count-negative",
        "§3.2",
        field("validation.warning_count", FieldFault::OutOfRange),
    ),
    deviation(
        "lax-first-error-over-cap",
        "§3.2",
        field("validation.first_error", FieldFault::TooLong),
    ),
    deviation(
        "lax-reconcile-in-sync",
        "§2.1",
        field("reconcile", FieldFault::NotPermitted),
    ),
    deviation(
        "lax-reconcile-step-unknown",
        "§3.4",
        field("reconcile.step", FieldFault::UnknownWord),
    ),
    deviation(
        "lax-sandbox-of-other-family",
        "§4.1",
        StatusError::SandboxOfOtherFamily { item: 0 },
    ),
    deviation(
        "lax-sandbox-twice",
        "§4.1",
        StatusError::SandboxTwice { item: 1 },
    ),
    deviation(
        "lax-never-valid-in-sync",
        "§3.1",
        field("validation.never_valid", FieldFault::NotPermitted),
    ),
    deviation(
        "lax-fault-code-unknown",
        "§3.3",
        item("faults[].code", FieldFault::UnknownWord),
    ),
    deviation(
        "lax-fault-blocks-against-table",
        "§3.3",
        StatusError::Fault {
            item: 0,
            error: FaultError::BlocksTurnsAgainstTable,
        },
    ),
    deviation(
        "lax-fault-of-other-writer",
        "§3.3.1",
        StatusError::Fault {
            item: 0,
            error: FaultError::SourceDoesNotDetect,
        },
    ),
    deviation(
        "lax-fault-source-unknown",
        "§3.3",
        item("faults[].source", FieldFault::UnknownWord),
    ),
    deviation(
        "lax-spend-not-finite",
        "§7",
        field("spend.spend_usd", FieldFault::NotFinite),
    ),
    deviation(
        "lax-pep-off-with-url",
        "§2.2",
        field("pep.url", FieldFault::NotPermitted),
    ),
    deviation(
        "lax-pep-ok-with-since",
        "§2.2",
        field("pep.unreachable_since", FieldFault::NotPermitted),
    ),
];

#[test]
fn the_document_writer_makes_the_bytes_of_the_python_writer() {
    let mut deviated = HashSet::new();
    for vector in vectors::surface("status.write").unwrap().vectors() {
        let built = document_of(vector.input().args().unwrap());
        let deviation = DEVIATIONS.iter().find(|row| row.vector == vector.id());

        assert_eq!(vector.result(), Outcome::Accepted, "{}", vector.id());
        match (built, deviation) {
            (Ok(document), None) => {
                let written = String::from_utf8(document.encode()).unwrap();
                let wanted = String::from_utf8(output_of(vector)).unwrap();

                assert_eq!(written, wanted, "{}", vector.id());
            }
            (Err(error), Some(row)) => {
                assert_eq!(
                    error,
                    row.refusal,
                    "{} (contract 05 {})",
                    vector.id(),
                    row.contract
                );
                deviated.insert(row.vector);
            }
            (Ok(_), Some(row)) => panic!("{}: the row names no difference", row.vector),
            (Err(error), None) => panic!("{}: {error}", vector.id()),
        }
    }

    for row in DEVIATIONS {
        assert!(
            deviated.contains(row.vector),
            "{}: no such vector",
            row.vector
        );
        assert!(row.contract.starts_with('\u{a7}'), "{}", row.vector);
    }
}

#[test]
fn a_document_that_the_writer_makes_reads_back_as_the_same_document() {
    for vector in vectors::surface("status.write").unwrap().vectors() {
        let Ok(document) = document_of(vector.input().args().unwrap()) else {
            continue;
        };
        let bytes = document.encode();
        let raw = RawStatus::read(&bytes, Reader::Attendance).unwrap();

        assert_eq!(
            StatusDocument::read(&bytes).as_ref(),
            Ok(&document),
            "{}",
            vector.id()
        );
        assert_eq!(
            StatusDocument::try_from(&document.raw()).as_ref(),
            Ok(&document)
        );
        assert_eq!(
            views::attendance(&document.raw(), &chat()),
            views::attendance(&raw, &chat()),
            "{}",
            vector.id()
        );
        assert_eq!(
            views::noticeboard(&document.raw(), FAMILY, now()),
            views::read_noticeboard(&bytes, FAMILY, now()),
            "{}",
            vector.id()
        );
        assert_eq!(
            views::door_tui(&document.raw(), now()),
            views::read_door_tui(&bytes, now())
        );
        assert_eq!(
            views::door_trigger(&document.raw()),
            views::read_door_trigger(&bytes)
        );
        assert_eq!(
            views::door_owui(&document.raw()),
            views::read_door_owui(&bytes)
        );
    }
}

/// A time of the arguments of a vector.
fn time_of(value: &Value) -> Timestamp {
    value.as_str().unwrap().parse().unwrap()
}

/// The open faults of the arguments of a vector. `fault` makes the code and
/// the detail of one fault from one item of `args.faults`.
fn open_faults(
    args: &Map<String, Value>,
    fault: fn(&Value) -> (FaultCode, Object),
) -> Vec<OpenFault> {
    let faults = args["faults"].as_array().unwrap().iter().map(|one| {
        let (code, detail) = fault(one);

        OpenFault::new(code, time_of(&one["since"])).with_detail(detail)
    });

    faults.collect()
}

/// The fault file of one writer from the arguments of a vector. `None` when
/// the family of the arguments is not a family name.
fn fault_file_of(args: &Map<String, Value>, writer: &Writes) -> Option<FaultFile> {
    let family = args["family"].as_str().and_then(|name| name.parse().ok())?;
    let faults = open_faults(args, writer.fault);

    FaultFile::new(family, writer.source, time_of(&args["written_at"]), faults).ok()
}

/// One fault of `attendance`: the message and the sandbox are in the file
/// only when the caller gave one.
fn fault_of_attendance(fault: &Value) -> (FaultCode, Object) {
    let detail = ["message", "sandbox"]
        .iter()
        .filter(|key| !fault[**key].is_null())
        .map(|key| ((*key).to_owned(), from_value(&fault[*key])))
        .collect();

    (fault["code"].as_str().unwrap().parse().unwrap(), detail)
}

/// One fault of the chaperone: its one code, a message and a revision. The
/// revision is `null` when the chaperone parsed no grant file.
fn fault_of_chaperone(fault: &Value) -> (FaultCode, Object) {
    let detail = ["message", "rev"]
        .iter()
        .map(|key| ((*key).to_owned(), from_value(&fault[*key])))
        .collect();

    (FaultCode::GrantsStale, detail)
}

/// One surface whose bytes a writer of this module makes.
struct Writes {
    surface: &'static str,
    source: FaultSource,
    fault: fn(&Value) -> (FaultCode, Object),
}

const FAULT_WRITERS: &[Writes] = &[
    Writes {
        surface: "status.fault_file.attendance",
        source: FaultSource::Sessiond,
        fault: fault_of_attendance,
    },
    Writes {
        surface: "status.fault_file.chaperone",
        source: FaultSource::Pep,
        fault: fault_of_chaperone,
    },
];

#[test]
fn each_fault_file_writer_makes_the_bytes_of_its_python_writer() {
    for writer in FAULT_WRITERS {
        for vector in vectors::surface(writer.surface).unwrap().vectors() {
            let at = format!("{} {}", writer.surface, vector.id());
            let file = fault_file_of(vector.input().args().unwrap(), writer);
            match (vector.result(), file) {
                (Outcome::Accepted, Some(file)) => {
                    let written = String::from_utf8(file.encode()).unwrap();
                    let wanted = String::from_utf8(output_of(vector)).unwrap();

                    assert_eq!(written, wanted, "{at}");
                }
                (Outcome::Refused | Outcome::Raised, None) => {}
                (result, file) => panic!("{at}: Python {result:?}, this module {file:?}"),
            }
        }
    }
}

#[test]
fn a_fault_file_has_the_order_of_the_python_writer_for_each_order_of_the_caller() {
    let writer = FAULT_WRITERS
        .iter()
        .find(|writer| writer.source == FaultSource::Sessiond)
        .unwrap();
    let surface = vectors::surface(writer.surface).unwrap();
    let vector = surface.vector("each-code").unwrap();
    let mut reversed = vector.input().args().unwrap().clone();
    let faults = reversed["faults"].as_array_mut().unwrap();
    let given = faults.clone();
    faults.reverse();

    assert_ne!(*faults, given, "the vector holds more than one fault");

    let file = fault_file_of(&reversed, writer).unwrap();
    let written = String::from_utf8(file.encode()).unwrap();
    let wanted = String::from_utf8(output_of(vector)).unwrap();

    assert_eq!(written, wanted);
}

#[test]
fn each_surface_of_contract_05_has_a_row_in_a_table() {
    let named: HashSet<&str> = READERS
        .iter()
        .map(|reader| reader.surface)
        .chain(FAULT_WRITERS.iter().map(|writer| writer.surface))
        .chain(["status.write"])
        .collect();
    let count = READERS.len() + FAULT_WRITERS.len() + 1;

    assert_eq!(named.len(), count, "two rows name one surface");
    for row in vectors::index().unwrap() {
        if row.surface().starts_with("status.") {
            assert!(
                named.contains(row.surface()),
                "{}: no table names it",
                row.surface()
            );
        }
    }
}

// --- where the five readers differ ---

/// What one reader does with one document, as its vector file records it.
#[derive(Debug, Clone, Copy)]
enum Took {
    /// The reader refuses the document.
    Refused,
    /// The reader takes the document.
    Accepted,
    /// The reader takes the document, and this field of its value has this
    /// JSON text. The field is a JSON pointer.
    Field(&'static str, &'static str),
    /// The reader takes the document, and its value has no such field.
    Lacks(&'static str),
}

/// One document on which two readers of `status.json` differ. `took` is in
/// the order of [`STATUS_READERS`].
struct Disagreement {
    vector: &'static str,
    /// The section of contract 05, and the reading that it supports.
    contract: &'static str,
    took: [Took; 5],
}

/// The five readers of `status.json`, in the order of `Disagreement::took`.
const STATUS_READERS: [&str; 5] = [
    "status.attendance",
    "status.noticeboard",
    "status.door_tui",
    "status.door_trigger",
    "status.door_owui",
];

use Took::{Accepted, Field, Lacks, Refused};

/// Each class of document on which two readers of `status.json` differ, with
/// one or two documents of the class. The table does not hold each document.
/// The vectors hold more documents of some classes: `empty-object`,
/// `kind-upper` and `kind-number` for the kind, `state-display-form` for the
/// state, `fault-code-number` for a fault with no code, and `epoch-true` and
/// `epoch-float` for the epoch.
///
/// The test below holds each row equal to the vector files. No test fails
/// when a new document splits the readers and no row names it.
const DISAGREEMENTS: &[Disagreement] = &[
    Disagreement {
        vector: "kind-unknown",
        contract: "§2.1: kind is one of three words. The document has no kind.",
        took: [
            Field("/kind", "null"),
            Field("/kind", r#""robot""#),
            Refused,
            Refused,
            Refused,
        ],
    },
    Disagreement {
        vector: "kind-missing",
        contract: "§2.1: kind is one of three words. The document has no kind.",
        took: [
            Field("/kind", "null"),
            Field("/kind", r#""""#),
            Refused,
            Refused,
            Refused,
        ],
    },
    Disagreement {
        vector: "state-unknown-word",
        contract: "§3: state is one of four words. The document has no state.",
        took: [
            Field("/state", "null"),
            Field("/health", r#""unreadable""#),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "state-missing",
        contract: "§3: state is one of four words. The document has no state.",
        took: [
            Field("/state", "null"),
            Field("/health", r#""unreadable""#),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "never-valid-in-sync",
        contract: "§3.1: never_valid refuses turns. It does not depend on the state.",
        took: [
            Field("/never_valid", "true"),
            Field("/health", r#""in_sync""#),
            Refused,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "auto-never-valid-in-sync",
        contract: "§3.1: never_valid refuses turns. It does not depend on the state.",
        took: [
            Field("/never_valid", "true"),
            Field("/health", r#""in_sync""#),
            Refused,
            Accepted,
            Refused,
        ],
    },
    Disagreement {
        vector: "bytes-bom",
        contract: "§2: the file is JSON. A JSON text has no byte order mark.",
        took: [Refused, Accepted, Refused, Refused, Refused],
    },
    Disagreement {
        vector: "family-other-name",
        contract: "§2.1: family is the name of the family of the directory.",
        took: [
            Field("/family", r#""chat""#),
            Field("/name", r#""code""#),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "epoch-negative",
        contract: "§6.2: the epoch starts at 1 and rises. The document has no epoch.",
        took: [
            Field("/epoch", "1"),
            Field("/epoch", "-1"),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "epoch-text",
        contract: "§6.2: the epoch starts at 1 and rises. The document has no epoch.",
        took: [
            Field("/epoch", "1"),
            Field("/epoch", "0"),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "limits-negative",
        contract: "§2.1: a limit comes from the family file, which has no negative limit.",
        took: [
            Field("/max_running_turns", "null"),
            Field("/limits/max_running_turns", "-1"),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "sandbox-unknown-state",
        contract: "§4.2: state is one of seven words. The row is not a sandbox row.",
        took: [
            Field("/sandboxes", "[]"),
            Field("/sandboxes/0/state", r#""sleeping""#),
            Refused,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "sandbox-id-upper",
        contract: "§4.1: id is <family>-s<N>. The row is not a sandbox row.",
        took: [
            Field("/sandboxes", "[]"),
            Field("/sandboxes/0/id", r#""Chat-s1""#),
            Refused,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "sandbox-env-empty",
        contract: "§4.1.1 rule 4: a row with no path is a fault.",
        took: [
            Field("/sandboxes/0/playpen_env", r#""""#),
            Field("/sandboxes/0/has_playpen_env", "false"),
            Refused,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "sandbox-21",
        contract: "§4: sandboxes is a list. The contract gives no cap.",
        took: [
            Field("/sandboxes/20/id", r#""chat-s21""#),
            Lacks("/sandboxes/20"),
            Refused,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "faults-21",
        contract: "§3.3: faults is a list. The contract gives no cap.",
        took: [
            Field("/fault_codes/20", r#""fault_9""#),
            Lacks("/faults/20"),
            Accepted,
            Refused,
            Accepted,
        ],
    },
    Disagreement {
        vector: "fault-no-code",
        contract: "§3.3: a fault with blocks_turns: true stops turns.",
        took: [
            Field("/blocking_fault", "null"),
            Field("/faults/0/blocks_turns", "true"),
            Field("/warning", r#""turns are blocked by ""#),
            Refused,
            Accepted,
        ],
    },
];

#[test]
fn the_table_of_disagreements_says_what_each_python_reader_does() {
    let surfaces = STATUS_READERS.map(|name| vectors::surface(name).unwrap());
    for row in DISAGREEMENTS {
        assert!(row.contract.starts_with('\u{a7}'), "{}", row.vector);
        for (surface, took) in surfaces.iter().zip(row.took) {
            let at = format!("{} {}", surface.name(), row.vector);
            let vector = surface
                .vectors()
                .iter()
                .find(|vector| vector.id() == row.vector)
                .unwrap_or_else(|| panic!("{at}: no such vector"));
            match took {
                Refused => assert_eq!(vector.result(), Outcome::Refused, "{at}"),
                Accepted => assert_eq!(vector.result(), Outcome::Accepted, "{at}"),
                Field(pointer, text) => {
                    let wanted: Value = serde_json::from_str(text).unwrap();

                    assert_eq!(
                        vector.value().unwrap().pointer(pointer),
                        Some(&wanted),
                        "{at}"
                    );
                }
                Lacks(pointer) => {
                    assert_eq!(vector.value().unwrap().pointer(pointer), None, "{at}");
                }
            }
        }
    }
}
