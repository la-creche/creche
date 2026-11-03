//! The JSON Schema of the wire types, and the check that holds the committed
//! file equal to it (decision CH9-1). The Python models are generated from
//! that file, so a wire type that changes without the file fails here.
//!
//! To write the file after a change of a wire type, run
//! `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

use std::path::PathBuf;

use schemars::generate::SchemaSettings;
use serde_json::{Value, json};

use super::{AutoJobId, DelegationId, JobId, SessionId, SessionOrigin};

/// The variable that makes the test write the file instead of checking it.
const WRITE_VAR: &str = "CRECHE_WIRE_SCHEMA";
const WRITE: &str = "write";

/// The committed file, relative to the crate.
const SCHEMA_FILE: &str = "schema/wire.json";

/// The schema of every wire type, as one document with a `$defs` entry per
/// type. The contract is the one of a reader: what a reader accepts.
pub(super) fn wire_schema() -> Value {
    let mut generator = SchemaSettings::draft2020_12()
        .for_deserialize()
        .into_generator();
    let _ = generator.subschema_for::<SessionId>();
    let _ = generator.subschema_for::<JobId>();
    let _ = generator.subschema_for::<AutoJobId>();
    let _ = generator.subschema_for::<DelegationId>();
    let _ = generator.subschema_for::<SessionOrigin>();

    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": generator.take_definitions(true),
    })
}

fn rendered() -> String {
    let mut text = serde_json::to_string_pretty(&wire_schema()).unwrap();
    text.push('\n');
    text
}

#[test]
fn the_committed_schema_is_the_schema_of_the_wire_types() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(SCHEMA_FILE);
    let text = rendered();

    if std::env::var(WRITE_VAR).as_deref() == Ok(WRITE) {
        std::fs::write(&path, &text).unwrap();
        return;
    }

    let committed = std::fs::read_to_string(&path).unwrap_or_default();
    assert!(
        committed == text,
        "{SCHEMA_FILE} is not the schema of the wire types. Run `{WRITE_VAR}={WRITE} cargo test -p creche-wire` in rust/ and commit the file."
    );
}

#[test]
fn each_wire_type_has_one_definition() {
    let schema = wire_schema();

    assert_eq!(schema["$defs"]["SessionId"]["type"], "string");
}
