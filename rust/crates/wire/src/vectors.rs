//! The shared wire vectors (decision CH9-1). `vectors/wire.json` holds, for
//! each wire type, JSON texts and what the type does with each one. This
//! crate and the generated Python models (`wire/tests/test_wire_vectors.py`)
//! read the same file, so the two languages cannot drift apart.
//!
//! This crate is the authority. A case says what the Rust type does, and the
//! Python test holds the generated model to it.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::Deserialize;
use serde_json::Value;

use super::schema::wire_schema;
use super::{
    Auto, AutoJobId, ConfigMissing, ControlHello, ControlReady, CreateError, CreateSession,
    CredsMissing, Delegate, DelegationId, Dispatch, DispatchChain, Door, FacadeExited,
    FacadeNotStarted, FacadeRunning, FacadeStarting, FacadeState, FamilyConfig, FamilyCreds,
    FamilyName, FamilySkills, FamilyState, FamilyStateReply, JobId, LitellmKey, ModelAlias,
    PepToken, SandboxGrant, SessionCwd, SessionId, SessionOrigin, SessionRow, SessionTitle,
    SkillName, StartCount, StateNotValid, StateTooLarge, Timer, Trigger, TurnEnd, TurnEvent,
    TurnStart, UnixMillis, Webhook, WebhookName,
};

/// The file, relative to the crate.
const VECTOR_FILE: &str = "vectors/wire.json";

/// One JSON text on the wire, and what the type does with it.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Case {
    /// The JSON text a reader gets.
    json: String,
    /// The JSON text the type writes after it reads `json`, or `None` when
    /// the type refuses `json`.
    written: Option<String>,
    /// Why the case exists.
    note: String,
}

fn cases() -> BTreeMap<String, Vec<Case>> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(VECTOR_FILE);
    let text = std::fs::read_to_string(path).unwrap();

    serde_json::from_str(&text).unwrap()
}

/// Read `json` as `T`, and write it again.
fn round_trip<T>(json: &str) -> Option<String>
where
    T: for<'de> Deserialize<'de> + serde::Serialize,
{
    let value: T = serde_json::from_str(json).ok()?;

    Some(serde_json::to_string(&value).unwrap())
}

/// One entry per wire type. A new wire type needs a line here.
fn read_and_write(type_name: &str, json: &str) -> Option<String> {
    match type_name {
        "Auto" => round_trip::<Auto>(json),
        "AutoJobId" => round_trip::<AutoJobId>(json),
        "Delegate" => round_trip::<Delegate>(json),
        "DelegationId" => round_trip::<DelegationId>(json),
        "Dispatch" => round_trip::<Dispatch>(json),
        "ConfigMissing" => round_trip::<ConfigMissing>(json),
        "ControlHello" => round_trip::<ControlHello>(json),
        "ControlReady" => round_trip::<ControlReady>(json),
        "CreateError" => round_trip::<CreateError>(json),
        "CreateSession" => round_trip::<CreateSession>(json),
        "CredsMissing" => round_trip::<CredsMissing>(json),
        "DispatchChain" => round_trip::<DispatchChain>(json),
        "Door" => round_trip::<Door>(json),
        "FacadeExited" => round_trip::<FacadeExited>(json),
        "FacadeNotStarted" => round_trip::<FacadeNotStarted>(json),
        "FacadeRunning" => round_trip::<FacadeRunning>(json),
        "FacadeStarting" => round_trip::<FacadeStarting>(json),
        "FacadeState" => round_trip::<FacadeState>(json),
        "FamilyConfig" => round_trip::<FamilyConfig>(json),
        "FamilyCreds" => round_trip::<FamilyCreds>(json),
        "FamilyName" => round_trip::<FamilyName>(json),
        "FamilySkills" => round_trip::<FamilySkills>(json),
        "FamilyState" => round_trip::<FamilyState>(json),
        "FamilyStateReply" => round_trip::<FamilyStateReply>(json),
        "JobId" => round_trip::<JobId>(json),
        "LitellmKey" => round_trip::<LitellmKey>(json),
        "ModelAlias" => round_trip::<ModelAlias>(json),
        "PepToken" => round_trip::<PepToken>(json),
        "SandboxGrant" => round_trip::<SandboxGrant>(json),
        "SessionCwd" => round_trip::<SessionCwd>(json),
        "SessionId" => round_trip::<SessionId>(json),
        "SessionOrigin" => round_trip::<SessionOrigin>(json),
        "SessionRow" => round_trip::<SessionRow>(json),
        "SessionTitle" => round_trip::<SessionTitle>(json),
        "SkillName" => round_trip::<SkillName>(json),
        "StartCount" => round_trip::<StartCount>(json),
        "StateNotValid" => round_trip::<StateNotValid>(json),
        "StateTooLarge" => round_trip::<StateTooLarge>(json),
        "Timer" => round_trip::<Timer>(json),
        "Trigger" => round_trip::<Trigger>(json),
        "TurnEnd" => round_trip::<TurnEnd>(json),
        "TurnEvent" => round_trip::<TurnEvent>(json),
        "TurnStart" => round_trip::<TurnStart>(json),
        "UnixMillis" => round_trip::<UnixMillis>(json),
        "Webhook" => round_trip::<Webhook>(json),
        "WebhookName" => round_trip::<WebhookName>(json),
        other => panic!("{VECTOR_FILE} names {other}, and this test reads no such type"),
    }
}

#[test]
fn each_case_does_what_the_file_says() {
    for (type_name, type_cases) in cases() {
        for case in type_cases {
            assert_eq!(
                read_and_write(&type_name, &case.json),
                case.written,
                "{type_name}: {} ({})",
                case.json,
                case.note
            );
        }
    }
}

#[test]
fn each_wire_type_has_cases_and_each_case_a_wire_type() {
    let schema = wire_schema();
    let defined: Vec<&String> = schema["$defs"].as_object().unwrap().keys().collect();
    let vectors = cases();
    let covered: Vec<&String> = vectors.keys().collect();

    assert_eq!(covered, defined);
    for (type_name, type_cases) in &vectors {
        assert!(
            type_cases.iter().any(|case| case.written.is_some()),
            "{type_name}: no case is accepted"
        );
        assert!(
            type_cases.iter().any(|case| case.written.is_none()),
            "{type_name}: no case is refused"
        );
    }
}

/// Each accepted object case, with one key of its type's `required` left
/// out, is refused. `require_each_key` lists each key whatever serde does,
/// so a `#[serde(default)]` on a field would read a missing key in Rust,
/// while the generated model refuses it, and no schema test would see it.
#[test]
fn a_case_without_a_required_key_is_refused() {
    let schema = wire_schema();

    for (type_name, type_cases) in cases() {
        let Some(required) = schema["$defs"][&type_name]["required"].as_array() else {
            continue;
        };
        for case in type_cases.iter().filter(|case| case.written.is_some()) {
            let Ok(Value::Object(object)) = serde_json::from_str::<Value>(&case.json) else {
                continue;
            };
            for key in required.iter().filter_map(Value::as_str) {
                let mut fewer = object.clone();
                fewer.remove(key);
                let json = Value::Object(fewer).to_string();

                assert_eq!(
                    read_and_write(&type_name, &json),
                    None,
                    "{type_name}: {json}"
                );
            }
        }
    }
}
