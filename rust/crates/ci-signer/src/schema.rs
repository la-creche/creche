//! The JSON Schema of the release statement, and the check that holds the
//! committed file equal to it. Root's verifier in Python (PR 4 of
//! section 8) can generate its model from that file, as the wire types do
//! (decision CH9-1). The rules that a schema cannot say, the canonical
//! bytes and the sort order, are in `vectors/statement.json`.
//!
//! To write the file after a change of a statement type, run
//! `CRECHE_CI_SIGNER_SCHEMA=write cargo test -p creche-ci-signer` in rust/.

use std::path::PathBuf;

use schemars::generate::SchemaSettings;
use schemars::{JsonSchema, Schema};
use serde_json::Value;

use crate::statement::WireStatement;

/// The variable that makes the test write the file instead of checking it.
const WRITE_VAR: &str = "CRECHE_CI_SIGNER_SCHEMA";
const WRITE: &str = "write";

/// The committed file, relative to the crate.
const SCHEMA_FILE: &str = "schema/release-statement.json";

/// Each key of the object is required, also a key of an `Option` field.
pub(crate) fn require_each_key(schema: &mut Schema) {
    let keys: Vec<Value> = schema
        .get("properties")
        .and_then(Value::as_object)
        .map(|properties| properties.keys().cloned().map(Value::String).collect())
        .unwrap_or_default();
    schema.insert("required".to_owned(), Value::Array(keys));
}

fn statement_schema() -> Schema {
    SchemaSettings::draft2020_12()
        .for_deserialize()
        .into_generator()
        .into_root_schema_for::<WireStatement>()
}

#[test]
fn committed_schema_is_current() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(SCHEMA_FILE);
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(&statement_schema()).unwrap()
    );

    if std::env::var(WRITE_VAR).as_deref() == Ok(WRITE) {
        std::fs::write(&path, &text).unwrap();
        return;
    }
    let committed = std::fs::read_to_string(&path).unwrap();
    assert_eq!(
        committed, text,
        "run `{WRITE_VAR}={WRITE} cargo test -p creche-ci-signer` in rust/"
    );
}

#[test]
fn schema_names_each_key() {
    let schema = statement_schema();
    let required = schema.get("required").and_then(Value::as_array).unwrap();
    let keys: Vec<&str> = required.iter().filter_map(Value::as_str).collect();
    assert_eq!(
        keys.len(),
        9,
        "statement, repository, workflow, event, ref, commit, run, signed_at, tags: {keys:?}"
    );
    assert_eq!(
        schema.get("additionalProperties"),
        Some(&Value::Bool(false))
    );
    let _ = <WireStatement as JsonSchema>::schema_name();
}
