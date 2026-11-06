//! The shared wire vectors (decision CH9-1). `vectors/wire.json` holds, for
//! each wire type, JSON texts and what the type does with each one. This
//! crate and the generated Python models (`wire/tests/test_wire_vectors.py`)
//! read the same file, so the two languages cannot drift apart.
//!
//! This crate is the authority. A case says what the Rust type does, and the
//! Python test holds the generated model to it.

use std::collections::BTreeMap;
use std::path::PathBuf;

use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::Value;

use super::schema::wire_schema;
use super::{
    AcpError, Auto, AutoJobId, ConfigMissing, ControlCommand, ControlHello, ControlReady,
    ControlStop, ControlSync, ControlSynced, CreateError, CreateSession, CredsMissing, Delegate,
    DelegationId, Dispatch, DispatchChain, Door, DropError, DropFamily, DropSession, DropTarget,
    Dropped, FacadeExited, FacadeNotStarted, FacadeRunning, FacadeStarting, FacadeState,
    FamilyConfig, FamilyCpus, FamilyCreds, FamilyMemoryMib, FamilyName, FamilyShape, FamilySkills,
    FamilyState, FamilyStateReply, GuestEvent, JobId, ListCursor, ListError, ListSessions,
    LitellmKey, MISMATCH_STATUS, ModelAlias, OpenAcp, PepToken, ProtocolMismatch, RouteError,
    SHARED_CODES, SandboxGrant, SessionCwd, SessionId, SessionOrigin, SessionPage, SessionRow,
    SessionTitle, SkillName, StartCount, StateNotValid, StateTooLarge, Timer, Trigger, TurnEnd,
    TurnEvent, TurnStart, UnixMillis, Webhook, WebhookName, routes::TableRoute,
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
        "AcpError" => round_trip::<AcpError>(json),
        "Auto" => round_trip::<Auto>(json),
        "AutoJobId" => round_trip::<AutoJobId>(json),
        "Delegate" => round_trip::<Delegate>(json),
        "DelegationId" => round_trip::<DelegationId>(json),
        "Dispatch" => round_trip::<Dispatch>(json),
        "ConfigMissing" => round_trip::<ConfigMissing>(json),
        "ControlCommand" => round_trip::<ControlCommand>(json),
        "ControlHello" => round_trip::<ControlHello>(json),
        "ControlReady" => round_trip::<ControlReady>(json),
        "ControlStop" => round_trip::<ControlStop>(json),
        "ControlSync" => round_trip::<ControlSync>(json),
        "ControlSynced" => round_trip::<ControlSynced>(json),
        "CreateError" => round_trip::<CreateError>(json),
        "CreateSession" => round_trip::<CreateSession>(json),
        "CredsMissing" => round_trip::<CredsMissing>(json),
        "DispatchChain" => round_trip::<DispatchChain>(json),
        "Door" => round_trip::<Door>(json),
        "DropError" => round_trip::<DropError>(json),
        "DropFamily" => round_trip::<DropFamily>(json),
        "DropSession" => round_trip::<DropSession>(json),
        "DropTarget" => round_trip::<DropTarget>(json),
        "Dropped" => round_trip::<Dropped>(json),
        "FacadeExited" => round_trip::<FacadeExited>(json),
        "FacadeNotStarted" => round_trip::<FacadeNotStarted>(json),
        "FacadeRunning" => round_trip::<FacadeRunning>(json),
        "FacadeStarting" => round_trip::<FacadeStarting>(json),
        "FacadeState" => round_trip::<FacadeState>(json),
        "FamilyConfig" => round_trip::<FamilyConfig>(json),
        "FamilyCpus" => round_trip::<FamilyCpus>(json),
        "FamilyCreds" => round_trip::<FamilyCreds>(json),
        "FamilyMemoryMib" => round_trip::<FamilyMemoryMib>(json),
        "FamilyName" => round_trip::<FamilyName>(json),
        "FamilyShape" => round_trip::<FamilyShape>(json),
        "FamilySkills" => round_trip::<FamilySkills>(json),
        "FamilyState" => round_trip::<FamilyState>(json),
        "FamilyStateReply" => round_trip::<FamilyStateReply>(json),
        "GuestEvent" => round_trip::<GuestEvent>(json),
        "JobId" => round_trip::<JobId>(json),
        "ListCursor" => round_trip::<ListCursor>(json),
        "ListError" => round_trip::<ListError>(json),
        "ListSessions" => round_trip::<ListSessions>(json),
        "LitellmKey" => round_trip::<LitellmKey>(json),
        "ModelAlias" => round_trip::<ModelAlias>(json),
        "OpenAcp" => round_trip::<OpenAcp>(json),
        "PepToken" => round_trip::<PepToken>(json),
        "ProtocolMismatch" => round_trip::<ProtocolMismatch>(json),
        "SandboxGrant" => round_trip::<SandboxGrant>(json),
        "SessionCwd" => round_trip::<SessionCwd>(json),
        "SessionId" => round_trip::<SessionId>(json),
        "SessionOrigin" => round_trip::<SessionOrigin>(json),
        "SessionPage" => round_trip::<SessionPage>(json),
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

/// The mismatch reply is frozen for all protocol numbers (decision CH33
/// rule 1). A client of any protocol reads it, so a change of its bytes or
/// its status fails here. Never change this test to make a change pass.
#[test]
fn the_mismatch_reply_is_frozen() {
    let replies = [
        (0, r#"{"code":"protocol_mismatch","protocol":0}"#),
        (1, r#"{"code":"protocol_mismatch","protocol":1}"#),
        (2, r#"{"code":"protocol_mismatch","protocol":2}"#),
        (
            u32::MAX,
            r#"{"code":"protocol_mismatch","protocol":4294967295}"#,
        ),
    ];

    assert_eq!(MISMATCH_STATUS, 412);
    for (protocol, text) in replies {
        let reply = ProtocolMismatch::new(protocol);

        assert_eq!(serde_json::to_string(&reply).unwrap(), text);
        assert_eq!(
            serde_json::from_str::<ProtocolMismatch>(text).unwrap(),
            reply
        );
    }
}

/// Each code of each route error has an accepted case `{"code":"<code>"}`
/// that the error writes as the same text. Each route error also has a
/// refused case of a shared code with an extra key. Python and TypeScript
/// read only the codes that have a case, so a code with no case would
/// never reach them.
#[test]
fn each_code_of_a_route_error_has_a_case() {
    let vectors = cases();
    macro_rules! check {
        ($kind:ident $route:path) => {
            let error = <$route as TableRoute>::Error::schema_name();
            let statuses = <<$route as TableRoute>::Error as RouteError>::STATUSES;
            let error_cases = vectors
                .get(&*error)
                .unwrap_or_else(|| panic!("{VECTOR_FILE} has no cases for {error}"));

            for (code, _) in statuses {
                let json = format!("{{\"code\":\"{code}\"}}");
                assert!(
                    error_cases
                        .iter()
                        .any(|case| case.json == json && case.written.as_deref() == Some(&*json)),
                    "{error}: no accepted case {json}"
                );
            }

            let shared_with_a_key = error_cases.iter().any(|case| {
                let Ok(Value::Object(object)) = serde_json::from_str::<Value>(&case.json) else {
                    return false;
                };
                let shared = object
                    .get("code")
                    .and_then(Value::as_str)
                    .is_some_and(|code| SHARED_CODES.iter().any(|(shared, _)| *shared == code));

                case.written.is_none() && shared && object.len() > 1
            });
            assert!(
                shared_with_a_key,
                "{error}: no refused case of a shared code with an extra key"
            );
        };
    }
    crate::route_table!(check);
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
