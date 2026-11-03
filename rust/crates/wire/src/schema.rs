//! The JSON Schema of the wire types, and the check that holds the committed
//! file equal to it (decision CH9-1). The Python models are generated from
//! that file, so a wire type that changes without the file fails here.
//!
//! The same test writes `agent/src/wire.ts`, a TypeScript copy of the
//! schema, from which TypeBox gives the facade its static types and its
//! runtime checks (decision CH22).
//!
//! To write the files after a change of a wire type, run
//! `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

use std::path::PathBuf;

use schemars::Schema;
use schemars::generate::SchemaSettings;
use schemars::transform::transform_subschemas;
use serde_json::{Value, json};

use super::{
    AutoJobId, ControlHello, ControlReady, CreateSession, DelegationId, FamilyConfig,
    FamilyStateReply, JS_INTEGER_MAX, JobId, SessionId, SessionOrigin, SessionRow, TurnEvent,
    UnixMillis,
};

/// The variable that makes the test write the file instead of checking it.
const WRITE_VAR: &str = "CRECHE_WIRE_SCHEMA";
const WRITE: &str = "write";

/// The committed file, relative to the crate.
const SCHEMA_FILE: &str = "schema/wire.json";

/// The committed TypeScript copy of the schema, relative to the crate.
const TYPESCRIPT_FILE: &str = "../../../agent/src/wire.ts";

/// The lines before the schema in the TypeScript copy.
const TYPESCRIPT_HEAD: &str = "\
// The JSON Schema of the wire types, a copy of rust/crates/wire/schema/wire.json
// (decision CH22). The schema test of rust/crates/wire writes this file. Never
// edit it: run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

export const wire = ";

/// The schema of every wire type, as one document with a `$defs` entry per
/// type. The contract is the one of a reader: what a reader accepts.
pub(super) fn wire_schema() -> Value {
    let mut generator = SchemaSettings::draft2020_12()
        .for_deserialize()
        .with_transform(bound_integers)
        .into_generator();
    let _ = generator.subschema_for::<SessionId>();
    let _ = generator.subschema_for::<JobId>();
    let _ = generator.subschema_for::<AutoJobId>();
    let _ = generator.subschema_for::<DelegationId>();
    let _ = generator.subschema_for::<SessionOrigin>();
    let _ = generator.subschema_for::<UnixMillis>();
    let _ = generator.subschema_for::<CreateSession>();
    let _ = generator.subschema_for::<SessionRow>();
    let _ = generator.subschema_for::<TurnEvent>();
    let _ = generator.subschema_for::<FamilyConfig>();
    let _ = generator.subschema_for::<FamilyStateReply>();
    let _ = generator.subschema_for::<ControlHello>();
    let _ = generator.subschema_for::<ControlReady>();

    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": generator.take_definitions(true),
    })
}

/// Give each integer of `schema` its `minimum` and `maximum` (decision CH22
/// rule 3). datamodel-codegen drops the `format` that schemars writes, so
/// the bounds are the only width the generated model checks.
///
/// schemars bounds an integer of 8 or 16 bits itself. This transform gives
/// an integer of 32 bits the bounds of its width. Any other integer (`u64`,
/// `i64`, `usize`, `isize`) needs bounds of its own type, at most 2^53 - 1
/// from zero, or the schema test fails. JavaScript reads no larger integer
/// exactly.
///
/// A nullable integer (`Option<u64>`) has the type `["integer", "null"]`, so
/// the transform checks it too.
fn bound_integers(schema: &mut Schema) {
    transform_subschemas(&mut bound_integers, schema);
    if !is_integer(schema) {
        return;
    }

    let width = match schema.get("format").and_then(Value::as_str) {
        Some("uint32") => Some((0, i64::from(u32::MAX))),
        Some("int32") => Some((i64::from(i32::MIN), i64::from(i32::MAX))),
        _ => None,
    };
    if let (Some((min, max)), Some(node)) = (width, schema.as_object_mut()) {
        node.entry("minimum").or_insert(json!(min));
        node.entry("maximum").or_insert(json!(max));
    }

    let limit = i64::try_from(JS_INTEGER_MAX).unwrap();
    let bound = |key: &str| schema.get(key).and_then(Value::as_i64);
    assert!(
        bound("minimum").is_some_and(|min| min >= -limit)
            && bound("maximum").is_some_and(|max| max <= limit),
        "an integer of a wire type needs a minimum and a maximum within 2^53 - 1 of zero: {schema:?}"
    );
}

/// List each property of `schema` in its `required`. schemars leaves out a
/// field of type `Option`, because a derived reader takes a missing key as
/// `None`. A struct whose `Option` fields read with `Option::deserialize`
/// refuses a missing key, and this transform makes its schema say so.
pub(super) fn require_each_key(schema: &mut Schema) {
    let keys: Vec<Value> = schema
        .get("properties")
        .and_then(Value::as_object)
        .map(|properties| properties.keys().cloned().map(Value::String).collect())
        .unwrap_or_default();
    schema.insert("required".to_owned(), Value::Array(keys));
}

/// True when `schema` admits an integer: its type is `"integer"`, or an array
/// that holds `"integer"`.
fn is_integer(schema: &Schema) -> bool {
    match schema.get("type") {
        Some(Value::String(name)) => name == "integer",
        Some(Value::Array(names)) => names.iter().any(|name| name == "integer"),
        _ => false,
    }
}

/// Each committed file, relative to the crate, and the text it must hold.
fn rendered() -> [(&'static str, String); 2] {
    let json = serde_json::to_string_pretty(&wire_schema()).unwrap();
    let typescript = format!("{TYPESCRIPT_HEAD}{json} as const;\n");

    [
        (SCHEMA_FILE, format!("{json}\n")),
        (TYPESCRIPT_FILE, typescript),
    ]
}

#[test]
fn the_committed_schema_is_the_schema_of_the_wire_types() {
    let write = std::env::var(WRITE_VAR).as_deref() == Ok(WRITE);

    for (file, text) in rendered() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(file);
        if write {
            std::fs::write(&path, &text).unwrap();
            continue;
        }

        let committed = std::fs::read_to_string(&path).unwrap_or_default();
        assert!(
            committed == text,
            "{file} is not the schema of the wire types. Run `{WRITE_VAR}={WRITE} cargo test -p creche-wire` in rust/ and commit the file."
        );
    }
}

#[test]
fn each_wire_type_has_one_definition() {
    let schema = wire_schema();

    assert_eq!(schema["$defs"]["SessionId"]["type"], "string");
}

/// The paths of the objects in `node` that leave a property out of
/// `required` (`wire/AGENTS.md`, "A struct on the wire", rule 4).
fn optional_keys(node: &Value, path: &str, found: &mut Vec<String>) {
    match node {
        Value::Object(map) => {
            if let Some(Value::Object(properties)) = map.get("properties") {
                let required = map.get("required").and_then(Value::as_array);
                for key in properties.keys() {
                    if !required.is_some_and(|keys| keys.contains(&json!(key))) {
                        found.push(format!("{path}/{key}"));
                    }
                }
            }
            for (key, child) in map {
                optional_keys(child, &format!("{path}/{key}"), found);
            }
        }
        Value::Array(items) => {
            for (index, child) in items.iter().enumerate() {
                optional_keys(child, &format!("{path}/{index}"), found);
            }
        }
        _ => {}
    }
}

#[test]
fn each_key_of_each_wire_object_is_required() {
    let mut found = Vec::new();
    optional_keys(&wire_schema(), "", &mut found);

    assert!(found.is_empty(), "keys not in `required`: {found:?}");
}

#[test]
fn a_key_left_out_of_required_is_found() {
    let mut found = Vec::new();
    let node = json!({"$defs": {"A": {"properties": {"a": {}, "b": {}}, "required": ["a"]}}});
    optional_keys(&node, "", &mut found);

    assert_eq!(found, ["/$defs/A/b"]);
}

#[test]
fn an_integer_of_32_bits_gets_the_bounds_of_its_width() {
    let cases = [
        (schemars::schema_for!(u32), 0, i64::from(u32::MAX)),
        (
            schemars::schema_for!(i32),
            i64::from(i32::MIN),
            i64::from(i32::MAX),
        ),
        (schemars::schema_for!(Option<u32>), 0, i64::from(u32::MAX)),
    ];

    for (mut schema, min, max) in cases {
        bound_integers(&mut schema);

        assert_eq!(schema.get("minimum"), Some(&json!(min)), "{schema:?}");
        assert_eq!(schema.get("maximum"), Some(&json!(max)), "{schema:?}");
    }
}

#[test]
#[should_panic(expected = "within 2^53 - 1 of zero")]
fn an_integer_of_64_bits_without_a_maximum_fails() {
    bound_integers(&mut schemars::schema_for!(u64));
}

#[test]
#[should_panic(expected = "within 2^53 - 1 of zero")]
fn a_nullable_integer_of_64_bits_without_a_maximum_fails() {
    bound_integers(&mut schemars::schema_for!(Option<u64>));
}

#[test]
#[should_panic(expected = "within 2^53 - 1 of zero")]
fn an_integer_with_a_minimum_past_the_cap_fails() {
    let limit = i64::try_from(JS_INTEGER_MAX).unwrap();
    let mut schema = Schema::try_from(json!({
        "type": "integer",
        "minimum": -limit - 1,
        "maximum": 0,
    }))
    .unwrap();

    bound_integers(&mut schema);
}
