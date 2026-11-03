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

use super::schema::wire_schema;
use super::{AutoJobId, DelegationId, JobId, SessionId};

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
        "AutoJobId" => round_trip::<AutoJobId>(json),
        "DelegationId" => round_trip::<DelegationId>(json),
        "JobId" => round_trip::<JobId>(json),
        "SessionId" => round_trip::<SessionId>(json),
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
